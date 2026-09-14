"""Command line: run the daemon, inspect devices, or control a running daemon."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import signal
import sys
import threading
import urllib.error
import urllib.request
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from .api import serve
from .config import Config, ConfigError, default_config_paths, find_config, load
from .pw import PipeWireBackend, PwError, cubic_to_db
from .router import Router

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


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    if args.no_state:
        cfg.state_file = None
    if args.fake:
        from .pw import FakeBackend

        backend = FakeBackend()
        for alias, node in cfg.devices.items():
            backend.add_device(node, "Audio/Sink" if "output" in node or alias.endswith("_out") else "Audio/Source", alias)
        log.warning("running against a FAKE in-memory PipeWire (demo/UI development only)")
    else:
        backend = PipeWireBackend(dry_run=args.dry_run)
    router = Router(cfg, backend, node_wait=args.node_wait)
    stop = threading.Event()

    def _signal(signum, _frame):  # noqa: ANN001
        log.info("received signal %d, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    log.info("tfcz-audio %s starting with %s", __version__, cfg.path)
    router.start()
    server = serve(router, cfg.api.listen, cfg.api.port, cfg.api.token)
    http_thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    http_thread.start()
    try:
        router.run_forever(stop, interval=args.interval)
    finally:
        server.shutdown()
        server.server_close()
        router.stop()
        log.info("stopped")
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
    s.add_argument("--no-state", action="store_true", help="do not persist or restore volumes")
    s.add_argument("--node-wait", type=float, default=5.0, help="seconds to wait for spawned nodes")
    s.add_argument("--interval", type=float, default=1.0, help="supervisor poll interval in seconds")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("check", help="validate config and report missing devices")
    s.set_defaults(func=cmd_check)

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
