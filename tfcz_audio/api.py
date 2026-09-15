"""Minimal JSON-over-HTTP control API (stdlib http.server, threaded).

Designed so that clients which cannot send a body (some automation tools)
can still do everything with a bare POST and query parameters or path
segments, e.g. ``POST /routes/hdmi_to_a/volume/0.3``.
"""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from importlib import resources

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


_UI_CACHE: bytes | None = None


def load_ui() -> bytes:
    global _UI_CACHE  # noqa: PLW0603
    if _UI_CACHE is None:
        _UI_CACHE = resources.files("tfcz_audio").joinpath("ui.html").read_bytes()
    return _UI_CACHE


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], router: Router, token: str = "", meters: Any = None):
        self.router = router
        self.token = token
        self.meters = meters
        self.listen_host = address[0]
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
        if listen in ("0.0.0.0", "::", ""):
            return True
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
            return ok, {"ok": not router.last_error, "version": __version__, "routes": len(router.cfg.routes), "error": router.last_error}
        if seg == ["status"] and read:
            return ok, router.status()
        if seg == ["devices"] and read:
            return ok, {"ok": True, "devices": router.devices()}
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
