"""Command line: run the daemon, inspect devices, or control a running daemon."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from importlib import resources
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

from . import __version__
from .api import serve
from .config import Config, ConfigError, default_config_paths, find_config, load, load_or_recover
from .meters import FakeMeterManager, MeterManager
from .pw import PipeWireBackend, PwError, cubic_to_db, kill_stale_helpers
from .router import Router, RouterError, UnknownRoute
from .sdnotify import Notifier

log = logging.getLogger("tfcz")

DEVICE_PROPS = ("alsa.card_name", "alsa.long_card_name", "api.alsa.card", "object.path", "device.api")


def _setup_logging(verbose: bool) -> None:
    from . import logbuf

    logbuf.install(logging.DEBUG if verbose else logging.INFO)
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
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")  # noqa: SIM115 - kept open on purpose
    except OSError:
        path = Path(f"/tmp/tfcz-audio-{os.getuid()}.lock")
        fh = open(path, "a+")  # noqa: SIM115
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        other = fh.read().strip() or "unknown pid"
        print(
            f"tfcz-audio is already running ({other}). Use 'systemctl --user status tfcz-audio' / "
            "'systemctl --user stop tfcz-audio' if you want to run it by hand.",
            file=sys.stderr,
        )
        raise SystemExit(3) from None
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid {os.getpid()}")
    fh.flush()
    return fh


def cmd_run(args: argparse.Namespace) -> int:
    lock = _single_instance()  # noqa: F841 - must stay referenced
    try:
        path = find_config(args.config)
    except ConfigError as exc:
        if args.config:
            print(f"config error: {exc}", file=sys.stderr)
            raise SystemExit(2) from None
        path = default_config_paths()[0]
        log.warning("%s -- writing a starter config to %s; open the web UI and run Setup to connect the devices", exc, path)
        _write_starter(path)
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
    elif args.no_meters or args.dry_run or not cfg.audio.meters:
        meters = None
        if not cfg.audio.meters:
            log.info("level meters are switched off in the config ([audio] meters = false)")
    else:
        meters = MeterManager(
            lambda: router.cfg,
            resolved_getter=router.resolved_nodes,
            known_nodes=lambda: {d["name"] for d in router.devices()},
            pw_recovered=lambda: router.pw_recovered_count,
        )

    # The HTTP server is optional for the audio: if the port is taken we keep
    # routing and retry binding from the supervisor loop.
    server_box: dict[str, Any] = {"server": None}

    def ensure_http() -> None:
        if server_box["server"] is not None:
            return
        try:
            server = serve(router, cfg.api.listen, cfg.api.port, cfg.api.token, meters)
        except OSError as exc:
            hint = " (use an IPv4 address such as 0.0.0.0 for LAN access)" if ":" in cfg.api.listen else ""
            log.error("cannot bind API on %s:%d (%s)%s; routing continues, retrying", cfg.api.listen, cfg.api.port, exc, hint)
            return
        threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
        server_box["server"] = server

    last_tick = {"t": time.monotonic()}
    stall_limit = 25.0  # a supervisor pass longer than this counts as hung (worst legit pass is ~15 s)

    def heartbeat() -> None:
        last_tick["t"] = time.monotonic()
        problems = [p for p in router.problems() if p["level"] in ("error", "warning")]
        notifier.status("OK: all routes linked" if not problems else f"{len(problems)} problem(s): {problems[0]['title']}")

    def watchdog_pinger() -> None:
        # Pings on its own schedule while the supervisor loop makes progress.
        # A slow pass (PipeWire busy) therefore does not get the process killed;
        # a truly hung loop stops the pings within stall_limit and systemd restarts us.
        period = (notifier.watchdog_interval or 10.0) / 2
        while not stop.wait(period):
            if time.monotonic() - last_tick["t"] < stall_limit:
                notifier.watchdog()
            else:
                log.error("supervisor loop has not ticked for %.0fs; letting the systemd watchdog restart us", stall_limit)

    ticks = [ensure_http, heartbeat]
    if meters is not None:
        ticks.insert(1, meters.reconcile)

    ensure_http()
    if meters is not None:
        meters.reconcile()
    notifier.ready("routes started")
    notifier.watchdog()
    if notifier.enabled:
        threading.Thread(target=watchdog_pinger, name="watchdog", daemon=True).start()
    try:
        router.run_forever(stop, interval=args.interval, on_tick=ticks)
    finally:
        # every shutdown step is isolated: whatever fails, the helpers still get terminated
        for step_name, step in (
            ("notify", notifier.stopping),
            ("http", lambda: _shutdown_http(server_box)),
            ("meters", lambda: meters.stop(deadline=3.0) if meters is not None else None),
            ("router", lambda: router.stop(deadline=5.0)),
        ):
            try:
                step()
            except Exception:  # noqa: BLE001
                log.exception("shutdown step %s failed", step_name)
        log.info("stopped")
    return 0


def _shutdown_http(server_box: dict[str, Any]) -> None:
    server = server_box.get("server")
    if server is not None:
        server.shutdown()
        server.server_close()


# ----------------------------------------------------------------- selftest


@dataclass
class Probe:
    """What a short recording from one device produced."""

    total: int = 0
    peak: float = 0.0
    error: str = ""
    rc: int | None = None
    stats: dict[str, Any] = dc_field(default_factory=dict)


PROBE_KEEP = 4 * 1024 * 1024  # enough samples to judge the signal, never unbounded


def _probe(cmd: list[str], seconds: float = 2.0, skip_wav: bool = False) -> Probe:
    """Run a capture command for a while and describe what came out of it."""
    import select

    from .meters import peak_of, wav_data_offset

    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
    except OSError as exc:
        return Probe(error=str(exc), rc=127)
    total, peak = 0, 0.0
    kept = bytearray()
    header = bytearray()
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.2)
            if ready:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                if skip_wav:
                    header += chunk
                    at = wav_data_offset(bytes(header))
                    if at is None:
                        continue
                    chunk = bytes(header[at:])
                    skip_wav = False
                    if not chunk:
                        continue
                total += len(chunk)
                peak = max(peak, peak_of(chunk))
                if len(kept) < PROBE_KEEP:
                    kept += chunk
            elif proc.poll() is not None:
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    err = ""
    try:
        err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        pass
    from .meters import signal_stats

    return Probe(total=total, peak=peak, error=err, rc=proc.returncode, stats=signal_stats(bytes(kept)))


def cmd_selftest(args: argparse.Namespace) -> int:
    """Record briefly from every configured device and report what arrives.

    This is the tool for 'I hear nothing / the bars stay empty / it sounds
    terrible': it shows whether each device delivers audio at all, what the
    graph looks like, and whether anything is dropping samples.
    """
    from .meters import UNSUPPORTED_MARKERS, MeterSpec, to_db
    from .router import resolve_devices

    cfg = getattr(args, "cfg", None)
    if cfg is None:
        cfg, err = load_or_recover(find_config(args.config))
        if err:
            print(f"config problem: {err}\n")
    if getattr(args, "fake", False):
        from .pw import FakeBackend

        backend = FakeBackend()
        _populate_fake(backend, cfg)
    else:
        backend = PipeWireBackend()
    try:
        graph = backend.graph()
    except PwError as exc:
        print(f"cannot read the PipeWire graph: {exc}")
        print("Is PipeWire running? Try: systemctl --user restart pipewire wireplumber")
        return 1

    print("=== graph ===")
    driver_hint = ""
    if shutil.which("pw-top"):
        rc, out = _run(["pw-top", "-b", "-n", "2"], timeout=15)
        lines = [l for l in out.splitlines() if l.strip()]
        if rc == 0 and lines:
            from .pw import parse_pw_top

            print("\n".join(lines[-40:]))
            print("\nERR counts what a node has lost since it started, so an old node looks worse")
            print("than a recently restarted one. The web UI compares two samples instead.")
            top = parse_pw_top(out)
            driver_hint = ", ".join(
                f"{r['name']} ({r['quantum']})" for r in top["rows"] if r["driver"] and r["quantum"]
            )
        else:
            print(f"pw-top did not run ({out.strip()[:200]})")
    else:
        print("pw-top not installed (part of pipewire-bin)")

    print("\n=== devices ===")
    resolved = resolve_devices(cfg, graph)
    problems = 0
    working_shape: tuple[bool, bool] | None = None
    for alias, res in sorted(resolved.items()):
        label = f"{alias}"
        if not res.present or res.node is None:
            print(f"  [MISSING] {label}: not connected")
            problems += 1
            continue
        node = graph.by_name(res.node)
        is_sink = node is not None and node.media_class.startswith("Audio/Sink")
        spec = MeterSpec(alias, res.node, capture_sink=is_sink)
        kind = "output (monitored)" if is_sink else "input"
        # try the full command first, then drop whatever pw-record refuses, the
        # same way the daemon's meters do
        probe = Probe()
        used = None
        shapes = [(True, True), (False, True), (False, False)]
        if working_shape in shapes:
            shapes.insert(0, shapes.pop(shapes.index(working_shape)))
        for raw, props in shapes:
            cmd = spec.command(raw=raw, props=props)
            probe = _probe(cmd, args.seconds, skip_wav=not raw)
            used = cmd
            if probe.total or not any(m in probe.error.lower() for m in UNSUPPORTED_MARKERS):
                break
            print(f"  [note] {label}: pw-record refused an option, retrying without it ({probe.error.splitlines()[0][:90]})")
        total, peak, err_text, rc = probe.total, probe.peak, probe.error, probe.rc
        if total == 0:
            problems += 1
            print(f"  [NO DATA] {label} ({kind}) -> {res.node}")
            print(f"            command: {shlex.join(used)}")
            print(f"            exit {rc}: {(err_text.splitlines() or ['no output, no error message'])[0][:200]}")
        else:
            working_shape = (("--raw" in used), ("-P" in used))
            state = "silent" if peak < 0.001 else f"peak {to_db(peak):.1f} dB"
            shape = "" if used == spec.command() else "  (reduced command shape)"
            print(f"  [ok] {label} ({kind}): {total} bytes in {args.seconds:.0f}s, {state}{shape}")
            stats = probe.stats
            if stats.get("frames"):
                print(f"       rms {stats['rms']:.3f} · Scheitelfaktor {stats['crest']} · Nulldurchgänge "
                      f"{stats['zcr'] * 100:.0f}% · Kanäle {', '.join(f'{c:.3f}' for c in stats['channels'])}")
            from .meters import signal_verdict

            for finding in signal_verdict(stats, "output" if is_sink else "input"):
                problems += finding["level"] == "error"
                mark = {"error": "FAIL", "warning": "warn"}.get(finding["level"], "note")
                print(f"       [{mark}] {finding['title']}")
                if finding.get("why"):
                    print(f"              {finding['why']}")
                if finding.get("fix"):
                    print(f"              -> {finding['fix']}")

    print("\n=== router streams ===")
    for name, route in sorted(cfg.routes.items()):
        cap = graph.by_name(route.in_node)
        play = graph.by_name(route.out_node)
        if cap is None or play is None:
            print(f"  [MISSING] {name}: the router's own streams are not running")
            problems += 1
            continue
        peers_in = {graph.nodes[p].name for p in graph.peers_of_input(cap.id) if p in graph.nodes}
        peers_out = {graph.nodes[p].name for p in graph.peers_of_output(play.id) if p in graph.nodes}
        print(f"  {name}: in <- {', '.join(sorted(peers_in)) or 'NOTHING'} | out -> {', '.join(sorted(peers_out)) or 'NOTHING'}")
        if not peers_in or not peers_out:
            problems += 1

    print()
    if driver_hint:
        print(f"graph driver(s): {driver_hint}")
    if problems:
        print(f"{problems} thing(s) need attention. See docs/first-run-checklist.md and the notes above.")
        return 1
    print("Every configured device delivers audio and every connection is linked.")
    return 0


# ------------------------------------------------------------------- doctor


def _tool(name: str) -> bool:
    return shutil.which(name) is not None


def _run(cmd: list[str], timeout: float = 5) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
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
    if _tool("pw-cli"):
        rc, out = _run(["pw-cli", "--version"])
        import re as _re

        m = _re.search(r"(\d+)\.(\d+)\.(\d+)", out.replace("Compiled with libpipewire", ""))
        if rc == 0 and m:
            ver = tuple(int(x) for x in m.groups())
            if ver >= (0, 3, 60):
                ok(f"PipeWire {'.'.join(map(str, ver))}")
            else:
                bad(f"PipeWire {'.'.join(map(str, ver))} is too old (need 0.3.60+, Ubuntu 22.10+)",
                    "upgrade to Ubuntu 24.04 or install PipeWire >= 0.3.60 (the pipewire-upstream PPA on 22.04)")
    # HDMI capture card: an out-of-tree DKMS module can silently fail to rebuild
    # after a kernel update, and then only the game sound is missing
    cards = ""
    try:
        cards = Path("/proc/asound/cards").read_text(errors="replace")
    except OSError:
        pass
    if Path("/sys/module/hws").exists() or "hws" in cards.lower():
        ok(f"HDMI capture driver loaded ({cards.lower().count('hws')} input(s))" if "hws" in cards.lower() else "HDMI capture driver (hws) is loaded")
    else:
        warn("no HDMI capture card found (the 'hws' driver is not loaded)",
             "only needed for the game sound. After a kernel update the driver must be rebuilt: "
             "sudo dkms autoinstall && sudo modprobe hws   (see docs/hdmi-capture.md)")


    try:
        live = getattr(args, "cfg", None)
        path = live.path if live is not None and live.path else find_config(args.config)
        cfg, err = (live, "") if live is not None else load_or_recover(path)
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
            probe_host = "127.0.0.1" if cfg.api.listen in ("0.0.0.0", "::", "", "localhost") else cfg.api.listen
            if sock.connect_ex((probe_host, cfg.api.port)) == 0:
                ok(f"web UI answers on http://{cfg.api.listen}:{cfg.api.port}/")
                # ask the running daemon whether metering actually works, instead
                # of guessing from a tool's help text
                import urllib.error
                import urllib.request

                req = urllib.request.Request(f"http://{probe_host}:{cfg.api.port}/levels")
                if cfg.api.token:
                    req.add_header("Authorization", f"Bearer {cfg.api.token}")
                try:
                    with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310 - own daemon
                        levels = json.loads(resp.read().decode())
                    entries = levels.get("levels") or {}
                    moving = sum(1 for lvl in entries.values() if lvl.get("active"))
                    if not levels.get("available"):
                        warn("level bars are switched off: " + (levels.get("reason") or "unknown reason"),
                             "run 'tfcz-audio selftest' for the exact command and its error; routing is unaffected")
                    elif moving:
                        ok(f"level bars are working ({moving} of {len(entries)} devices delivering audio right now)")
                    else:
                        warn(f"level bars deliver nothing ({len(entries)} meters running, none with data)",
                             "run 'tfcz-audio selftest': it prints the exact command and its error. "
                             "The router and the OBS microphone are unaffected either way")
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    warn(f"cannot ask the daemon about the level bars ({exc})", "check the token under Advanced if one is set")
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
        import pwd

        user = pwd.getpwuid(os.getuid()).pw_name
        rc, out = _run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger"])
        if "Linger=yes" in out:
            ok("starts at boot without login (lingering enabled)")
        elif rc == 0:
            bad("does not start at boot: lingering is off, so the router only runs after someone logs in",
                f"loginctl enable-linger {user}   (sudo if refused). ./install.sh does this too.")
        try:
            import grp

            effective = set()
            for gid in os.getgroups():
                try:
                    effective.add(grp.getgrgid(gid).gr_name)
                except KeyError:
                    pass
            on_disk = {g.gr_name for g in grp.getgrall() if user in g.gr_mem}
            for group, why in (("audio", "devices usable before login"), ("pipewire", "realtime priority for the audio helpers without a login")):
                if group in effective:
                    ok(f"member of the '{group}' group ({why})")
                elif group in on_disk:
                    warn(f"'{group}' group was added but is not active yet for running services",
                         "reboot (or: sudo systemctl restart user@$(id -u), which restarts your session services)")
                else:
                    warn(f"not in the '{group}' group ({why})", f"sudo usermod -aG {group} {user}   (then reboot)")
        except (KeyError, OSError):
            pass
        # realtime limit as seen by user services (PAM limits must reach user@.service)
        rc, out = _run(["systemd-run", "--user", "--quiet", "--pipe", "--wait", "cat", "/proc/self/limits"], timeout=10)
        if rc == 0:
            line = next((l for l in out.splitlines() if "realtime priority" in l.lower()), "")
            parts = line.split()
            hard = parts[-2] if len(parts) >= 3 else "?"
            if hard not in ("0", "?"):
                ok(f"user services may use realtime priority (limit {hard})")
            else:
                warn("user services have no realtime priority limit: audio helpers run without RT when nobody is logged in",
                     "add the user to the 'pipewire' group and make sure /etc/pam.d/systemd-user contains 'session required pam_limits.so'; reboot")

    print()
    if problems:
        print(f"{problems} problem(s) found. Fix the [FAIL] lines above, then run 'tfcz-audio doctor' again.")
        return 1
    print("Everything needed is in place.")
    return 0


# -------------------------------------------------------------------- tone


def cmd_tone(args: argparse.Namespace) -> int:
    """Play a test tone on one headphone: the only check for what goes out."""
    from .tone import SIDE_LABELS, TonePlayer

    cfg, _ = load_or_recover(find_config(args.config))
    backend = PipeWireBackend()
    router = Router(cfg, backend)
    try:
        graph = router.refresh_devices()
    except PwError as exc:
        print(f"cannot read the PipeWire graph: {exc}", file=sys.stderr)
        return 1

    targets = router.output_targets(graph)
    if not args.alias:
        if not targets:
            print("No output device is connected right now.")
            return 1
        print("Test tone on which device?\n")
        for entry in targets:
            print(f"  {entry['alias']:16} {entry['label']}")
        print("\ntfcz-audio tone <alias> [--side left|right|both]")
        return 0

    try:
        node, label = router.tone_target(args.alias, graph)
    except (RouterError, UnknownRoute) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    player = TonePlayer()
    print(f"{label}: {SIDE_LABELS.get(args.side, args.side)} …")
    player.play(node, label, args.side, args.seconds)
    while player.state()["running"]:
        time.sleep(0.1)
    state = player.state()
    if state["error"]:
        print(f"  failed: {state['error']}", file=sys.stderr)
        if state["command"]:
            print(f"  command: {state['command']}", file=sys.stderr)
        return 1
    print("  played. Heard it on the expected side? Then this device is wired correctly.")
    return 0


# --------------------------------------------------------------------- fix


def cmd_fix(args: argparse.Namespace) -> int:
    """Show what is broken about the system and repair it.

    In a terminal this is the better place for the repairs that need root:
    sudo can ask for a password here, which it cannot do from a web page.
    """
    from . import repair

    router = None
    try:
        cfg, _ = load_or_recover(find_config(args.config))
        router = Router(cfg, PipeWireBackend())
        router.refresh_devices()
    except Exception:  # noqa: BLE001 - the plan works without a router too
        router = None

    actions = repair.detect(router)
    if not actions:
        print("Nichts zu reparieren: alles, was sich prüfen lässt, ist in Ordnung.")
        return 0

    wanted = set(getattr(args, "action", []) or [])
    if not wanted and not args.all:
        print("Gefunden:\n")
        for a in actions:
            print(f"  {a.id}")
            print(f"    {a.title}")
            print(f"    {a.why}")
            if a.command:
                print(f"    Befehl: {'sudo ' if a.needs_root and os.getuid() else ''}{' '.join(a.command)}")
            if a.note:
                print(f"    Hinweis: {a.note}")
            print()
        print("Ausführen: tfcz-audio fix <id> [<id> …]   oder   tfcz-audio fix --all")
        return 0

    problems = 0
    for a in actions:
        if wanted and a.id not in wanted:
            continue
        if a.manual and not wanted:
            continue  # --all never runs the deep ones
        if a.manual:
            print(f"[{a.id}] läuft nur von Hand:\n  {' '.join(a.command)}\n  {a.note}")
            continue
        print(f"=== {a.title}")
        problems += _apply_fix(a)
    return 1 if problems else 0


def _apply_fix(action: Any) -> int:
    """Run one repair with the terminal attached, so sudo can prompt."""
    from . import repair

    if action.python:
        runner = repair.Runner()
        rc = runner._python_fix(action)  # noqa: SLF001 - same module, deliberately shared
        print(runner.output.strip())
        return 0 if rc == 0 else 1
    cmd = list(action.command)
    if action.needs_root and os.getuid() != 0:
        cmd = ["sudo", *cmd]
    print("$ " + " ".join(cmd))
    try:
        rc = subprocess.call(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"liess sich nicht starten: {exc}")
        return 1
    if rc != 0:
        print(f"Befehl endete mit {rc}")
    return 0 if rc == 0 else 1


# ----------------------------------------------------------------- versions


def cmd_versions(args: argparse.Namespace) -> int:
    """Everything that has a version and can explain odd behaviour, in one
    block that can be pasted into a bug report."""
    from . import versions

    if getattr(args, "json", False):
        print(json.dumps(versions.collect(fresh=True), indent=2, ensure_ascii=False))
    else:
        print(versions.as_text(versions.collect(fresh=True)))
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
    from .router import resolve_devices

    missing = 0
    for alias, res in resolve_devices(cfg, graph).items():
        missing += not res.present
        print(f"  [{'ok' if res.present else 'MISSING'}] {alias:16} {res.node or cfg.devices[alias].match}")
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


STARTER = """# tfcz-audio configuration. Devices and connections are added by the
# setup wizard in the web UI (http://127.0.0.1:8787/), nothing to edit here.
# For a fully annotated example: tfcz-audio init-config --example PATH

[api]
listen = "127.0.0.1"
port = 8787
token = ""

[audio]
latency = "auto"
channels = 2
meters = true

[virtual]
obs_mic_name = "tfcz.obsmic"
obs_mic_description = "TFCZ OBS Mic"
"""


def _write_starter(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STARTER)


def cmd_init_config(args: argparse.Namespace) -> int:
    target = Path(args.path).expanduser() if args.path else default_config_paths()[0]
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    if args.example:
        target.parent.mkdir(parents=True, exist_ok=True)
        with resources.files("tfcz_audio").joinpath("example_config.toml").open("rb") as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        print(f"wrote example config to {target}\nnext: run 'tfcz-audio devices' and fill in the [devices] node names, or use the web UI Setup")
    else:
        _write_starter(target)
        print(f"wrote {target}\nnext: start the service and open http://127.0.0.1:8787/ -- the setup wizard connects your devices")
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

    s = sub.add_parser("selftest", help="record from every device and show what really arrives (use when it sounds wrong)")
    s.add_argument("--seconds", type=float, default=2.0, help="how long to record per device")
    s.add_argument("--fake", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_selftest)

    s = sub.add_parser("tone", help="play a test tone on one headphone (no alias: list them)")
    s.add_argument("alias", nargs="?")
    s.add_argument("--side", choices=("left", "right", "both"), default="both")
    s.add_argument("--seconds", type=float, default=1.2)
    s.set_defaults(func=cmd_tone)

    s = sub.add_parser("fix", help="show what is broken about the system and repair it")
    s.add_argument("action", nargs="*", help="ids to run; without any, the list is only shown")
    s.add_argument("--all", action="store_true", help="run everything that is safe to run")
    s.set_defaults(func=cmd_fix)

    s = sub.add_parser("versions", help="show the versions of every tool this router depends on")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_versions)

    s = sub.add_parser("devices", help="list PipeWire audio sources and sinks")
    s.add_argument("--json", action="store_true")
    s.add_argument("-p", "--props", dest="verbose_props", action="store_true", help="show ALSA card properties")
    s.set_defaults(func=cmd_devices)

    s = sub.add_parser("init-config", help="write a starter config (the web UI wizard fills it)")
    s.add_argument("path", nargs="?")
    s.add_argument("--force", action="store_true")
    s.add_argument("--example", action="store_true", help="write the fully annotated example instead")
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
