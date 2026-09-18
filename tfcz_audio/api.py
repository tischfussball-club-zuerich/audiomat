"""Minimal JSON-over-HTTP control API (stdlib http.server, threaded).

Designed so that clients which cannot send a body (some automation tools)
can still do everything with a bare POST and query parameters or path
segments, e.g. ``POST /routes/hdmi_to_a/volume/0.3``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from importlib import resources
from pathlib import Path

from . import __version__, edit
from .config import ConfigError
from .pw import db_to_cubic
from .router import Router, RouterError, UnknownPreset, UnknownRoute

log = logging.getLogger("tfcz.api")

TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"0", "false", "no", "off"}


class BadRequest(Exception):
    pass


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    raise BadRequest(f"not a boolean: {value!r}")


def parse_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a number") from None


def extract_route_params(params: dict[str, Any]) -> tuple[float | None, bool | None]:
    volume: float | None = None
    mute: bool | None = None
    if "volume" in params:
        volume = parse_float(params["volume"], "volume")
    elif "volume_db" in params:
        volume = round(db_to_cubic(parse_float(params["volume_db"], "volume_db")), 4)
    if "mute" in params:
        mute = parse_bool(params["mute"])
    if volume is None and mute is None:
        raise BadRequest("provide volume, volume_db and/or mute")
    return volume, mute


class Diagnostics:
    """Runs `tfcz-audio doctor` / `selftest` in the background and keeps the
    output, so the web UI can offer them without a terminal.

    They shell out to PipeWire tools and can take a while, which is why the
    HTTP request only starts the job and the page polls for the result.
    """

    KINDS = ("doctor", "selftest")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.kind = ""
        self.output = ""
        self.running = False
        self.started = 0.0
        self.finished = 0.0
        self.rc: int | None = None

    def start(self, kind: str, router: Router) -> dict[str, Any]:
        if kind not in self.KINDS:
            raise BadRequest(f"unknown check '{kind}'")
        with self._lock:
            if self.running:
                return self.state()
            self.kind, self.output, self.running = kind, "", True
            self.started, self.finished, self.rc = time.time(), 0.0, None
        threading.Thread(target=self._run, name=f"diag-{kind}", args=(kind, router), daemon=True).start()
        return self.state()

    def _run(self, kind: str, router: Router) -> None:
        import argparse
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from .cli import cmd_doctor, cmd_selftest
        from .pw import FakeBackend

        buf = io.StringIO()
        rc: int | None = None
        try:
            args = argparse.Namespace(
                config=str(router.cfg.path) if router.cfg.path else None,
                cfg=router.cfg,  # the running configuration, not whatever is on disk
                seconds=2.0,
                fake=isinstance(router.backend, FakeBackend),
                verbose=False,
            )
            with redirect_stdout(buf), redirect_stderr(buf):
                rc = cmd_doctor(args) if kind == "doctor" else cmd_selftest(args)
        except Exception as exc:  # noqa: BLE001 - the report must never take the daemon down
            log.exception("%s failed", kind)
            buf.write(f"\nthe check itself failed: {exc}\n")
            rc = 2
        with self._lock:
            self.output = buf.getvalue()
            self.rc = rc
            self.running = False
            self.finished = time.time()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "kind": self.kind,
                "running": self.running,
                "output": self.output,
                "rc": self.rc,
                "age": round(time.time() - self.finished, 1) if self.finished else None,
                "seconds": round((time.time() if self.running else self.finished) - self.started, 1) if self.started else 0,
            }


def _journal(level: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    """Full history across restarts, when systemd is the launcher."""
    import shutil
    import subprocess

    if not shutil.which("journalctl"):
        return [], "journalctl is not available on this system"
    priority = {"ERROR": "3", "CRITICAL": "2", "WARNING": "4"}.get(level.upper(), "7")
    cmd = ["journalctl", "--user", "-u", "tfcz-audio", "-n", str(max(1, min(limit, 2000))),
           "--no-pager", "-o", "short-iso", "-p", priority]
    try:
        proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"journalctl failed: {exc}"
    if proc.returncode != 0:
        return [], (proc.stderr or "journalctl returned an error").strip()[:200]
    entries = []
    for line in proc.stdout.splitlines():
        if not line.strip() or line.startswith("-- "):
            continue
        parts = line.split(" ", 3)
        stamp = parts[0].split("T")[-1][:8] if parts else ""
        message = parts[3] if len(parts) > 3 else line
        text = message.split(": ", 1)[-1] if ": " in message[:60] else message
        upper = text.upper()
        lvl = "ERROR" if "ERROR" in upper[:40] else "WARNING" if "WARNING" in upper[:40] else "INFO"
        entries.append({"time": stamp, "level": lvl, "logger": "", "message": text})
    return entries, "full history from the system journal"


_UI_CACHE: tuple[float, bytes] | None = None


def load_ui() -> bytes:
    """The page, re-read when the file changes.

    Caching it forever means an edited or replaced ui.html keeps serving the
    old bytes until the daemon restarts, which is confusing during development
    and indistinguishable from a failed install.
    """
    global _UI_CACHE  # noqa: PLW0603
    path = resources.files("tfcz_audio").joinpath("ui.html")
    try:
        stamp = Path(str(path)).stat().st_mtime
    except OSError:
        stamp = 0.0
    if _UI_CACHE is None or _UI_CACHE[0] != stamp:
        _UI_CACHE = (stamp, path.read_bytes())
    return _UI_CACHE[1]


_LOGO_CACHE: bytes | None = None


def load_logo() -> bytes:
    """The club's own header logo, shipped with the package so the page needs
    no network. The file is used as provided; the brand guide forbids redrawing
    or altering the mark."""
    global _LOGO_CACHE  # noqa: PLW0603
    if _LOGO_CACHE is None:
        _LOGO_CACHE = resources.files("tfcz_audio").joinpath("logo.png").read_bytes()
    return _LOGO_CACHE


def ui_build() -> str:
    """Short fingerprint of the page being served, so an outdated install is
    visible instead of leaving people looking for a section that is not there."""
    import hashlib

    return hashlib.sha256(load_ui()).hexdigest()[:8]


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], router: Router, token: str = "", meters: Any = None):
        self.router = router
        self.token = token
        self.meters = meters
        self.listen_host = address[0]
        self.diagnostics = Diagnostics()
        super().__init__(address, Handler)


MAX_BODY = 1_000_000  # bytes; nothing legitimate is anywhere near this


class Handler(BaseHTTPRequestHandler):
    server: ApiServer  # type: ignore[assignment]
    server_version = f"tfcz-audio/{__version__}"
    timeout = 15  # a stalled client releases its thread instead of holding it forever
    protocol_version = "HTTP/1.0"

    # silence default stderr logging; use logging module instead
    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s " + fmt, self.address_string(), *args)

    # ------------------------------------------------------------- plumbing

    def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send(status, {"ok": False, "error": message})

    def _send_asset(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_html(self, body: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_params(self) -> dict[str, Any]:
        parts = urlsplit(self.path)
        params: dict[str, Any] = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("invalid Content-Length") from None
        if length < 0 or length > MAX_BODY:
            raise BadRequest("invalid request body length")
        if length:
            raw = self.rfile.read(length)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                return params
            if ctype == "application/x-www-form-urlencoded" and not text.startswith("{"):
                params.update({k: v[-1] for k, v in parse_qs(text).items()})
            else:
                try:
                    body = json.loads(text)
                except ValueError:
                    raise BadRequest("body must be JSON") from None
                if not isinstance(body, dict):
                    raise BadRequest("JSON body must be an object")
                params.update(body)
        return params

    def _host_allowed(self) -> bool:
        """Pin the Host header so DNS rebinding cannot impersonate this daemon.
        When listening on all interfaces (LAN mode) the token is the guard."""
        listen = (self.server.listen_host or "").lower()
        if listen not in ("127.0.0.1", "localhost", "::1"):
            return True  # LAN listener: clients may use any hostname; the token is the guard
        host = (self.headers.get("Host") or "").lower()
        if host.startswith("["):
            hostname = host.split("]")[0].lstrip("[")
        elif host.count(":") == 1:
            hostname = host.rsplit(":", 1)[0]
        else:
            hostname = host
        return hostname in {"127.0.0.1", "localhost", "::1", listen}

    def _same_origin(self) -> bool:
        """Browsers add Origin / Sec-Fetch-Site to cross-site requests. Reject
        those so a random web page cannot change volumes on localhost."""
        if not self._host_allowed():
            return False
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True  # curl, Advanced Scene Switcher
        if origin == "null":
            return False  # sandboxed iframe / file:// page: not us
        host = (self.headers.get("Host") or "").lower()
        o = urlsplit(origin)
        return bool(host) and (o.netloc.lower() == host)

    def _authorized(self, params: dict[str, Any]) -> bool:
        token = self.server.token
        if not token:
            return True
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer ") and header[7:].strip() == token:
            return True
        return str(params.get("token", "")) == token

    # ------------------------------------------------------------- dispatch

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            path = urlsplit(self.path).path
            if self.command in ("GET", "HEAD") and path in ("/", "/ui", "/ui/", "/index.html"):
                self._send_html(load_ui())
                return
            if self.command in ("GET", "HEAD") and path == "/logo.png":
                self._send_asset(load_logo(), "image/png")
                return
            if self.command not in ("GET", "HEAD") and not self._same_origin():
                self._error(HTTPStatus.FORBIDDEN, "cross-site request rejected")
                return
            params = self._read_params()
            if not self._authorized(params):
                self._error(HTTPStatus.UNAUTHORIZED, "invalid or missing token")
                return
            segments = [unquote(s) for s in path.strip("/").split("/") if s]
            status, payload = self._route(self.command, segments, params)
            self._send(status, payload)
        except BadRequest as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except UnknownRoute as exc:
            self._error(HTTPStatus.NOT_FOUND, f"unknown route '{exc}'")
        except UnknownPreset as exc:
            self._error(HTTPStatus.NOT_FOUND, f"unknown preset '{exc}'")
        except RouterError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except ConfigError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:  # noqa: BLE001
            log.exception("unhandled error for %s %s", self.command, self.path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    def _route(self, method: str, seg: list[str], params: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]]:
        router = self.server.router
        read = method in ("GET", "HEAD")
        write = method in ("POST", "PUT", "PATCH")
        ok = HTTPStatus.OK

        if not seg or seg == ["health"]:
            # cheap liveness probe: no pw-dump, no /proc reads
            return ok, {
                "ok": not router.last_error,
                "version": __version__,
                "build": ui_build(),
                # which copy of the package this process actually loaded: a restart
                # re-runs the installed copy, it does not pick up a git checkout
                "module": str(Path(__file__).resolve().parent),
                "routes": len(router.cfg.routes),
                "error": router.last_error,
            }
        if seg == ["status"] and read:
            payload = router.status()
            meters = self.server.meters
            if meters is not None and hasattr(meters, "problem"):
                entry = meters.problem()
                if entry is not None:
                    payload["problems"] = [*payload["problems"], entry]
            return ok, payload
        if seg == ["devices"] and read:
            return ok, {"ok": True, "devices": router.devices()}
        if seg == ["logs"] and read:
            from . import logbuf

            level = str(params.get("level", "INFO")).upper()
            try:
                limit = int(params.get("limit", 200))
            except (TypeError, ValueError):
                limit = 200
            if str(params.get("source", "")) == "journal":
                entries, note = _journal(level, limit)
                return ok, {"ok": True, "source": "journal", "note": note, "entries": entries}
            ring = logbuf.ring()
            entries = ring.records(level, limit) if ring is not None else []
            return ok, {"ok": True, "source": "memory", "note": "since the service last started", "entries": entries}
        if seg == ["update"] and read:
            from . import update

            return ok, {"ok": True, **update.status()}
        if seg == ["update"] and write:
            from . import update

            result = update.start()
            return (ok if result["started"] else HTTPStatus.CONFLICT), {"ok": result["started"], **result}
        if seg == ["versions"] and read:
            from . import versions

            fresh = str(params.get("fresh", "")).lower() in TRUE_WORDS
            return ok, {"ok": True, **versions.collect(fresh)}
        if seg == ["diagnostics"] and read:
            return ok, {"ok": True, **self.server.diagnostics.state()}
        if seg == ["diagnostics"] and write:
            kind = str(params.get("kind", "doctor"))
            return ok, {"ok": True, **self.server.diagnostics.start(kind, router)}
        if seg == ["audio"] and read:
            return ok, {"ok": True, **router.audio_settings()}
        if seg == ["audio"] and method in ("PUT", "POST"):
            try:
                frames = int(params.get("quantum"))
            except (TypeError, ValueError):
                raise BadRequest("quantum must be a number of frames (0 = automatic)") from None
            persist = params.get("persist") in (True, "true", "1")
            return ok, {"ok": True, **router.set_audio_buffer(frames, persist)}
        if seg == ["graph"] and read:
            return ok, {"ok": True, **router.signal_graph()}
        if seg == ["analysis"] and write:
            try:
                seconds = float(params.get("seconds", 3.0))
            except (TypeError, ValueError):
                seconds = 3.0
            return ok, {"ok": True, **router.analyse(min(max(seconds, 1.0), 10.0))}
        if seg == ["audio", "dropouts"] and write:
            try:
                seconds = float(params.get("seconds", 2.0))
            except (TypeError, ValueError):
                seconds = 2.0
            return ok, {"ok": True, **router.dropout_check(min(max(seconds, 1.0), 10.0))}
        if seg == ["hardware"] and read:
            return ok, {"ok": True, "devices": router.hardware()}
        if len(seg) == 2 and seg[0] == "identity" and read:
            return ok, {"ok": True, **router.identity_of_node(seg[1])}
        if seg == ["setup"] and write:
            return ok, edit.setup(router, {k: v for k, v in params.items() if k != "token"})
        if seg == ["meters", "watch"] and write:
            meters = self.server.meters
            nodes = params.get("nodes") or []
            if not isinstance(nodes, list):
                raise BadRequest("nodes must be a list of {name, kind}")
            clear = params.get("clear") in (True, "true", "1")
            watched = []
            if meters is not None and hasattr(meters, "watch"):
                watched = meters.watch([], seconds=0) if clear else meters.watch([n for n in nodes if isinstance(n, dict)])
            return ok, {"ok": True, "watching": watched, "available": meters is not None}
        if len(seg) == 3 and seg[0] == "fix" and seg[1] == "device" and write:
            return ok, router.fix_device(seg[2])
        if len(seg) == 3 and seg[0] == "demo" and seg[1] == "take" and write:
            # only available against the fake backend: simulate another program grabbing a device
            from .pw import FakeBackend

            if not isinstance(router.backend, FakeBackend):
                return HTTPStatus.NOT_FOUND, {"ok": False, "error": "demo endpoints exist only in --fake mode"}
            alias = seg[2]
            res = router.resolved.get(alias)
            node = router.backend.graph().by_name(res.node) if res and res.node else None
            if node is None:
                raise UnknownRoute(alias)
            owner = str(params.get("by", "obs"))
            if params.get("release") in (True, "true", "1"):
                node.props.pop("tfcz.fake.owner", None)
            else:
                node.props["tfcz.fake.owner"] = owner
            return ok, router.status()
        if seg == ["levels"] and read:
            meters = self.server.meters
            enabled = meters is not None and getattr(meters, "enabled", True)
            levels = meters.levels() if enabled else {}
            obs = levels.get("obs", {})
            return ok, {
                "ok": True,
                "available": enabled,
                "reason": "" if enabled else (getattr(meters, "disabled_reason", "") or "level meters are switched off"),
                "shape": getattr(meters, "shape_label", lambda: "")() if enabled else "",
                "levels": levels,
                "obs_signal": bool(obs.get("signal")),
                "obs_active": bool(obs.get("active")),
            }
        if seg == ["reset"] and write:
            return ok, router.reset_to_config()

        if seg[0] == "routes":
            if len(seg) == 1 and read:
                return ok, {"ok": True, "routes": router.status()["routes"]}
            if len(seg) == 2:
                name = seg[1]
                if read:
                    return ok, {"ok": True, "route": router.route_status(name)}
                if write:
                    volume, mute = extract_route_params(params)
                    return ok, {"ok": True, "route": router.set_route(name, volume=volume, mute=mute)}
            if len(seg) == 3 and write:
                name, action = seg[1], seg[2]
                if action == "mute":
                    return ok, {"ok": True, "route": router.set_route(name, mute=True)}
                if action == "unmute":
                    return ok, {"ok": True, "route": router.set_route(name, mute=False)}
                if action == "toggle":
                    return ok, {"ok": True, "route": router.toggle_mute(name)}
            if len(seg) == 4 and write and seg[2] in ("volume", "volume_db"):
                name = seg[1]
                volume, _ = extract_route_params({seg[2]: seg[3]})
                return ok, {"ok": True, "route": router.set_route(name, volume=volume)}

        if seg[0] == "presets":
            if len(seg) == 1 and read:
                return ok, {"ok": True, "presets": router.list_presets()}
            if len(seg) == 2 and write:
                return ok, {"ok": True, **router.apply_preset(seg[1])}

        if seg[0] == "config":
            body = {k: v for k, v in params.items() if k != "token"}
            if len(seg) == 1 and read:
                return ok, {"ok": True, **edit.public_config(router.cfg)}
            if seg[1:] == ["devices"] and method == "PUT":
                return ok, edit.set_devices(router, body)
            if seg[1:] == ["audio"] and method == "PUT":
                return ok, edit.set_audio(router, body)
            if seg[1:] == ["labels"] and method == "PUT":
                return ok, edit.set_labels(router, body)
            if seg[1:] == ["save-defaults"] and write:
                return ok, edit.save_current_as_defaults(router)
            if len(seg) == 3 and seg[1] == "routes":
                if method == "PUT":
                    return ok, edit.upsert_route(router, seg[2], body)
                if method == "DELETE":
                    return ok, edit.delete_route(router, seg[2])
            if len(seg) == 3 and seg[1] == "presets":
                if method == "PUT":
                    return ok, edit.upsert_preset(router, seg[2], body)
                if method == "DELETE":
                    return ok, edit.delete_preset(router, seg[2])

        if read or write or method == "DELETE":
            return HTTPStatus.NOT_FOUND, {"ok": False, "error": f"no such endpoint: {method} /{'/'.join(seg)}"}
        return HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "method not allowed"}


def serve(router: Router, listen: str, port: int, token: str = "", meters: Any = None) -> ApiServer:
    server = ApiServer((listen, port), router, token, meters)
    log.info("API listening on http://%s:%d/", *server.server_address[:2])
    return server
