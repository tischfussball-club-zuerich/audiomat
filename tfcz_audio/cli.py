"""Command line: run the daemon, inspect devices, or control a running daemon."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from .api import serve
from .config import Config, ConfigError, default_config_paths, find_config, load, load_or_recover
from .meters import FakeMeterManager, MeterManager
from .pw import PipeWireBackend, PwError, cubic_to_db, kill_stale_helpers
from .router import Router
from .sdnotify import Notifier

log = logging.getLogger("tfcz")

DEVICE_PROPS = ("alsa.card_name", "alsa.long_card_name", "api.alsa.card", "object.path", "device.api")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def _load_config(args: argparse.Namespace) -> Config:
    return load(find_config(args.config))


# ---------------------------------------------------------------------- run


def _populate_fake(backend: Any, cfg: Config) -> None:
    """Build a believable fake graph that satisfies the config: the config's
    devices grouped into headsets (static names or serial/port matchers), plus
    unassigned hardware so the setup wizard has choices."""
    import re

    groups: dict[str, dict[str, Any]] = {}
    for alias, spec in cfg.devices.items():
        base, _, kind = alias.rpartition("_")
        if kind not in ("mic", "out"):
            base, kind = alias, "mic"
        groups.setdefault(base, {})[kind] = spec

    used_ports: set[str] = set()
    for i, (base, parts) in enumerate(groups.items()):
        specs = list(parts.values())
        serial = next((s.match["device.serial"] for s in specs if "device.serial" in s.match), None)
        bus_path = next((s.match["device.bus-path"] for s in specs if "device.bus-path" in s.match), None)
        label = cfg.labels.get(base, base).replace("_", " ").title()
        is_hdmi = not any(k == "out" for k in parts) and ("hdmi" in base or "game" in base or "hws" in base)
        if is_hdmi:
            node = specs[0].node or f"alsa_input.pci-0000_03_00.0.hws-{i + 1}"
            backend.add_physical("HWS", "pci", node, None, extra={"alsa.card_name": "HWS", "api.alsa.card": str(i + 1)})
            continue
        safe = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_") or f"Headset{i}"
        mic = parts["mic"].node if "mic" in parts and parts["mic"].node else f"alsa_input.usb-{safe}-00.mono-fallback"
        out = parts["out"].node if "out" in parts and parts["out"].node else (f"alsa_output.usb-{safe}-00.analog-stereo" if "out" in parts else None)
        extra = {"device.vendor.id": "046d", "device.product.id": "0a44"}  # identical model unless a serial says otherwise
        if serial:
            extra.update({"device.serial": serial, "device.vendor.id": "0b0e", "device.product.id": "0412"})
        else:
            extra["device.serial"] = "Logitech_Logitech_USB_Headset"
        extra["device.bus-path"] = bus_path or f"pci-0000:00:14.0-usb-0:{i + 1}:1.0"
        used_ports.add(extra["device.bus-path"])
        backend.add_physical("Jabra Speak 510" if serial else "Logitech USB Headset", "usb", mic, out, form_factor="headset", extra=extra)

    # extra hardware that is not assigned yet
    if not any("Jabra" in d.description for d in backend.graph().devices.values()):
        backend.add_physical("Jabra Speak 510", "usb", "alsa_input.usb-Jabra_Speak_510_A1B2C3-00.mono-fallback",
                             "alsa_output.usb-Jabra_Speak_510_A1B2C3-00.analog-stereo", form_factor="headset",
                             extra={"device.serial": "Jabra_Speak_510_A1B2C3", "device.bus-path": "pci-0000:00:14.0-usb-0:4:1.0",
                                    "device.vendor.id": "0b0e", "device.product.id": "0412"})
    port = 5
    if len([g for g in groups.values() if "out" in g]) < 2:
        backend.add_physical("Logitech USB Headset", "usb", "alsa_input.usb-Logitech_Logitech_USB_Headset-01.mono-fallback",
                             "alsa_output.usb-Logitech_Logitech_USB_Headset-01.analog-stereo", form_factor="headset",
                             extra={"device.serial": "Logitech_Logitech_USB_Headset", "device.bus-path": f"pci-0000:00:14.0-usb-0:{port}:1.0",
                                    "device.vendor.id": "046d", "device.product.id": "0a44"})
    for n in (2, 3, 4):
        backend.add_physical("HWS", "pci", f"alsa_input.pci-0000_03_00.0.hws-{n}", None,
                             extra={"alsa.card_name": "HWS", "api.alsa.card": str(n)})
    backend.add_physical("Built-in Audio", "pci", "alsa_input.pci-0000_00_1f.3.analog-stereo",
                         "alsa_output.pci-0000_00_1f.3.analog-stereo", form_factor="internal")
    backend.add_physical("HDMI Monitor", "pci", None, "alsa_output.pci-0000_01_00.1.hdmi-stereo")


def _runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/tfcz-audio-{os.getuid()}")


def _single_instance() -> Any:
    """Hold an exclusive lock for the lifetime of the daemon. A second copy
    (e.g. started by hand while the service runs) would fight over the same
    loopbacks; refuse with a clear message instead."""
    path = _runtime_dir() / "tfcz-audio.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")  # noqa: SIM115 - kept open on purpose
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        other = fh.read().strip() or "unknown pid"
        raise SystemExit(
            f"tfcz-audio is already running ({other}). Use 'systemctl --user status tfcz-audio' / "
            "'systemctl --user stop tfcz-audio' if you want to run it by hand."
        ) from None
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid {os.getpid()}")
    fh.flush()
    return fh


def cmd_run(args: argparse.Namespace) -> int:
    lock = _single_instance()  # noqa: F841 - must stay referenced
    path = find_config(args.config)
    cfg, config_error = load_or_recover(path)
    if config_error:
        log.error("CONFIG PROBLEM: %s", config_error)
    if args.no_state:
        cfg.state_file = None
    if args.fake:
        from .pw import FakeBackend

        backend = FakeBackend()
        _populate_fake(backend, cfg)
        log.warning("running against a FAKE in-memory PipeWire (demo/UI development only)")
    else:
        backend = PipeWireBackend(dry_run=args.dry_run)
    router = Router(cfg, backend, node_wait=args.node_wait)
    router.config_error = config_error
    if not args.fake:
        router.graph_cache_ttl = 0.4  # the UI polls often; pw-dump is not free
    stop = threading.Event()

    def _signal(signum, _frame):  # noqa: ANN001
        log.info("received signal %d, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    notifier = Notifier()
    log.info("tfcz-audio %s starting with %s", __version__, cfg.path)
    if not args.fake and not args.dry_run:
        kill_stale_helpers()  # leftovers from a crashed previous instance
    router.start()

    if args.fake:
        meters = FakeMeterManager(lambda: router.cfg, lambda: router.desired, router.resolved_nodes)
    elif args.no_meters or args.dry_run:
        meters = None
    else:
        meters = MeterManager(lambda: router.cfg, resolved_getter=router.resolved_nodes)

    # The HTTP server is optional for the audio: if the port is taken we keep
    # routing and retry binding from the supervisor loop.
    server_box: dict[str, Any] = {"server": None}

    def ensure_http() -> None:
        if server_box["server"] is not None:
            return
        try:
            server = serve(router, cfg.api.listen, cfg.api.port, cfg.api.token, meters)
        except OSError as exc:
            log.error("cannot bind API on %s:%d (%s); routing continues, retrying", cfg.api.listen, cfg.api.port, exc)
            return
        threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
        server_box["server"] = server

    def heartbeat() -> None:
        notifier.watchdog()
        problems = [p for p in router.problems() if p["level"] in ("error", "warning")]
        notifier.status("OK: all routes linked" if not problems else f"{len(problems)} problem(s): {problems[0]['title']}")

    ticks = [ensure_http, heartbeat]
    if meters is not None:
        ticks.insert(1, meters.reconcile)

    ensure_http()
    if meters is not None:
        meters.reconcile()
    notifier.ready("routes started")
    interval = args.interval
    wd = notifier.watchdog_interval
    if wd is not None:
        interval = min(interval, wd)
    try:
        router.run_forever(stop, interval=interval, on_tick=ticks)
    finally:
        notifier.stopping()
        if server_box["server"] is not None:
            server_box["server"].shutdown()
            server_box["server"].server_close()
        if meters is not None:
            meters.stop()
        router.stop()
        log.info("stopped")
    return 0


# ------------------------------------------------------------------- doctor


def _tool(name: str) -> bool:
    return shutil.which(name) is not None


def _run(cmd: list[str], timeout: float = 5) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check everything the daemon needs and say how to fix what is missing."""
    problems = 0

    def ok(msg: str) -> None:
        print(f"  [ok]   {msg}")

    def bad(msg: str, fix: str) -> None:
        nonlocal problems
        problems += 1
        print(f"  [FAIL] {msg}\n         -> {fix}")

    def warn(msg: str, fix: str) -> None:
        print(f"  [warn] {msg}\n         -> {fix}")

    print("tfcz-audio doctor")
    if sys.version_info < (3, 11):
        bad(f"Python {sys.version.split()[0]} is too old", "Install Python 3.11 or newer (Ubuntu 24.04 ships 3.12).")
    else:
        ok(f"Python {sys.version.split()[0]}")

    for tool, pkg in (("pw-loopback", "pipewire-bin"), ("pw-dump", "pipewire-bin"), ("pw-record", "pipewire-bin"), ("wpctl", "wireplumber")):
        if _tool(tool):
            ok(f"{tool} found")
        else:
            bad(f"{tool} not found", f"sudo apt install {pkg}")

    if not os.environ.get("XDG_RUNTIME_DIR"):
        bad("XDG_RUNTIME_DIR is not set (no user session)",
            f"Run this from the desktop session, or: export XDG_RUNTIME_DIR=/run/user/{os.getuid()}")
    else:
        ok(f"user session runtime dir {os.environ['XDG_RUNTIME_DIR']}")

    if _tool("pw-dump"):
        rc, out = _run(["pw-dump"])
        if rc != 0:
            bad("PipeWire is not reachable", "systemctl --user start pipewire wireplumber  (log out and in if that fails)")
        else:
            ok("PipeWire answers")
            if _tool("pactl"):
                rc2, info = _run(["pactl", "info"])
                if rc2 == 0 and "PipeWire" not in info:
                    bad("The sound server is PulseAudio, not PipeWire",
                        "sudo apt install pipewire-audio wireplumber && systemctl --user --now disable pulseaudio.service pulseaudio.socket && systemctl --user --now enable pipewire pipewire-pulse wireplumber")
                elif rc2 == 0:
                    ok("PipeWire is the sound server")
    if _tool("systemctl"):
        rc, out = _run(["systemctl", "--user", "is-active", "wireplumber"])
        if out.strip() == "active":
            ok("WirePlumber session manager active")
        elif rc == 127 or "Failed to connect" in out:
            bad("cannot talk to the user systemd instance", "Run from a logged-in desktop session (or ssh with a running session and XDG_RUNTIME_DIR set).")
        else:
            bad("WirePlumber is not active", "systemctl --user enable --now wireplumber")
    if _tool("pw-record"):
        rc, out = _run(["pw-record", "--help"])
        if "--raw" in out:
            ok("pw-record supports --raw (level bars available)")
        else:
            warn("pw-record lacks --raw: level bars will be off", "Newer PipeWire (>= 0.3.60) enables them; routing works without.")

    try:
        path = find_config(args.config)
        cfg, err = load_or_recover(path)
        if err:
            bad(f"config {path} is invalid: {err}", "Fix the file or run Setup in the web UI.")
        else:
            ok(f"config {path} ({len(cfg.routes)} connections)")
            if _tool("pw-dump") and cfg.devices:
                from .router import resolve_devices

                graph = PipeWireBackend().graph()
                for alias, res in resolve_devices(cfg, graph).items():
                    if res.present:
                        ok(f"device {alias}: {res.node}")
                    else:
                        warn(f"device {alias} is not connected right now", "Plug it in; routes using it stay silent until then.")
        lock = _runtime_dir() / "tfcz-audio.lock"
        if lock.exists():
            try:
                fh = open(lock)
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
                ok("daemon is not running (port free for a manual start)")
            except OSError:
                ok(f"daemon is running ({open(lock).read().strip()})")
        import socket

        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((cfg.api.listen if cfg.api.listen != "0.0.0.0" else "127.0.0.1", cfg.api.port)) == 0:
                ok(f"web UI answers on http://{cfg.api.listen}:{cfg.api.port}/")
            else:
                warn(f"nothing listens on port {cfg.api.port}", "systemctl --user start tfcz-audio   (then: journalctl --user -u tfcz-audio -n 50)")
    except ConfigError as exc:
        bad(str(exc), "tfcz-audio init-config, then edit the [devices] or run Setup in the web UI.")

    if _tool("systemctl"):
        rc, out = _run(["systemctl", "--user", "is-enabled", "tfcz-audio"])
        if out.strip() in ("enabled", "static"):
            ok("service enabled at login")
        else:
            warn("service is not enabled", "./install.sh   (or: systemctl --user enable --now tfcz-audio)")
        user = os.environ.get("USER", "")
        rc, out = _run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger"])
        if "Linger=yes" in out:
            ok("starts at boot without login (lingering enabled)")
        elif rc == 0:
            bad("does not start at boot: lingering is off, so the router only runs after someone logs in",
                f"loginctl enable-linger {user}   (sudo if refused). ./install.sh does this too.")
        try:
            import grp

            groups = [g.gr_name for g in grp.getgrall() if user in g.gr_mem]
            if "audio" in groups or grp.getgrgid(os.getgid()).gr_name == "audio":
                ok("member of the 'audio' group (devices usable before login)")
            else:
                warn("not in the 'audio' group: before the first login PipeWire may not be allowed to open the sound devices",
                     f"sudo usermod -aG audio {user}   (then reboot)")
        except (KeyError, OSError):
            pass

    print()
    if problems:
        print(f"{problems} problem(s) found. Fix the [FAIL] lines above, then run 'tfcz-audio doctor' again.")
        return 1
    print("Everything needed is in place.")
    return 0


# -------------------------------------------------------------------- check


def cmd_check(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    print(f"config: {cfg.path}")
    print(f"routes: {len(cfg.routes)}, presets: {len(cfg.presets)}")
    try:
        graph = PipeWireBackend().graph()
    except PwError as exc:
        print(f"warning: cannot inspect PipeWire graph ({exc}); config syntax is OK")
        return 0
    missing = 0
    for alias, node in cfg.devices.items():
        present = graph.by_name(node) is not None
        missing += not present
        print(f"  [{'ok' if present else 'MISSING'}] {alias:16} {node}")
    if missing:
        print(f"{missing} device(s) not present; routes using them stay silent until they appear.")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    try:
        nodes = PipeWireBackend().graph().audio_devices()
    except PwError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([{**n.to_dict(), "props": {k: n.props.get(k) for k in DEVICE_PROPS}} for n in nodes], indent=2))
        return 0
    current = None
    for n in nodes:
        if n.media_class != current:
            current = n.media_class
            print(f"\n{current}")
        print(f"  {n.name}")
        print(f"      {n.description}")
        if args.verbose_props:
            for key in DEVICE_PROPS:
                if key in n.props:
                    print(f"      {key} = {n.props[key]}")
    return 0


def cmd_init_config(args: argparse.Namespace) -> int:
    target = Path(args.path).expanduser() if args.path else default_config_paths()[0]
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    with resources.files("tfcz_audio").joinpath("example_config.toml").open("rb") as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    print(f"wrote {target}\nnext: run 'tfcz-audio devices' and fill in the [devices] node names")
    return 0


# ------------------------------------------------------------------- client


class Client:
    def __init__(self, base: str, token: str = ""):
        self.base = base.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - local API
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode())
            except ValueError:
                payload = {"ok": False, "error": exc.reason}
            raise SystemExit(f"error: {payload.get('error', exc.reason)}") from None
        except urllib.error.URLError as exc:
            raise SystemExit(f"error: cannot reach daemon at {self.base} ({exc.reason}); is tfcz-audio running?") from None


def _client(args: argparse.Namespace) -> Client:
    token = ""
    base = args.url
    if not base or not token:
        try:
            cfg = _load_config(args)
            base = base or f"http://{cfg.api.listen}:{cfg.api.port}"
            token = cfg.api.token
        except ConfigError:
            base = base or "http://127.0.0.1:8787"
    return Client(base, token)


def _print_routes(routes: dict[str, Any]) -> None:
    width = max((len(n) for n in routes), default=10)
    print(f"{'route':{width}}  volume   dB      mute  running  linked  from -> to")
    for name, r in routes.items():
        db = r.get("volume_db")
        db_text = f"{db:+.1f}" if db is not None else "-inf"
        print(
            f"{name:{width}}  {r['volume']:<7.3f}  {db_text:>6}  {'yes ' if r['mute'] else 'no  '}  "
            f"{'yes' if r['running'] else 'NO '}      {'yes' if r['connected'] else 'no '}     "
            f"{r['from']} -> {r['to']}"
        )


def cmd_status(args: argparse.Namespace) -> int:
    status = _client(args).call("GET", "/status")
    if args.json:
        print(json.dumps(status, indent=2))
        return 0
    vm = status["virtual_mic"]
    print(f"virtual mic: {vm['description']} ({vm['node']}) running={vm['running']} present={vm['present']}")
    print("devices:")
    for alias, d in status["devices"].items():
        print(f"  [{'ok' if d['present'] else 'MISSING'}] {alias:16} {d['node']}")
    print()
    _print_routes(status["routes"])
    if status.get("presets"):
        print("\npresets: " + ", ".join(status["presets"]))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.toggle:
        result = client.call("POST", f"/routes/{args.route}/toggle")
    else:
        body: dict[str, Any] = {}
        if args.volume is not None:
            body["volume"] = args.volume
        if args.db is not None:
            body["volume_db"] = args.db
        if args.mute:
            body["mute"] = True
        if args.unmute:
            body["mute"] = False
        if not body:
            raise SystemExit("nothing to do: give --volume, --db, --mute, --unmute or --toggle")
        result = client.call("PUT", f"/routes/{args.route}", body)
    _print_routes({args.route: result["route"]})
    return 0


def cmd_preset(args: argparse.Namespace) -> int:
    client = _client(args)
    if not args.name:
        presets = client.call("GET", "/presets")["presets"]
        for name, entries in presets.items():
            print(f"{name}:")
            for route, entry in entries.items():
                print(f"  {route}: {entry}")
        return 0
    result = client.call("POST", f"/presets/{args.name}")
    _print_routes(result["routes"])
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    _print_routes(_client(args).call("POST", "/reset")["routes"])
    return 0


# --------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tfcz-audio", description="PipeWire audio router with HTTP API")
    p.add_argument("-c", "--config", help="config file (default: ~/.config/tfcz-audio/config.toml)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"tfcz-audio {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run", help="run the daemon (foreground)")
    s.add_argument("--dry-run", action="store_true", help="log commands instead of executing them")
    s.add_argument("--fake", action="store_true", help="use an in-memory fake PipeWire (UI demo, no audio)")
    s.add_argument("--no-meters", action="store_true", help="disable the pw-record level meters")
    s.add_argument("--no-state", action="store_true", help="do not persist or restore volumes")
    s.add_argument("--node-wait", type=float, default=5.0, help="seconds to wait for spawned nodes")
    s.add_argument("--interval", type=float, default=1.0, help="supervisor poll interval in seconds")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("check", help="validate config and report missing devices")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("doctor", help="check tools, PipeWire, config, service; explains how to fix problems")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("devices", help="list PipeWire audio sources and sinks")
    s.add_argument("--json", action="store_true")
    s.add_argument("-p", "--props", dest="verbose_props", action="store_true", help="show ALSA card properties")
    s.set_defaults(func=cmd_devices)

    s = sub.add_parser("init-config", help="write the example config")
    s.add_argument("path", nargs="?")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init_config)

    for name, func, helptext in (
        ("status", cmd_status, "show routes and device state from the running daemon"),
        ("set", cmd_set, "change one route"),
        ("preset", cmd_preset, "apply a preset (or list them)"),
        ("reset", cmd_reset, "reset all routes to the config values"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--url", help="API base URL (default from config)")
        if name == "status":
            s.add_argument("--json", action="store_true")
        if name == "set":
            s.add_argument("route")
            s.add_argument("--volume", type=float)
            s.add_argument("--db", type=float, help="volume in dB (0 = unity)")
            g = s.add_mutually_exclusive_group()
            g.add_argument("--mute", action="store_true")
            g.add_argument("--unmute", action="store_true")
            g.add_argument("--toggle", action="store_true")
        if name == "preset":
            s.add_argument("name", nargs="?")
        s.set_defaults(func=func)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
