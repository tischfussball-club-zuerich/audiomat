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

from . import __version__
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


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], router: Router, token: str = ""):
        self.router = router
        self.token = token
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: ApiServer  # type: ignore[assignment]
    server_version = f"tfcz-audio/{__version__}"

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

    def _read_params(self) -> dict[str, Any]:
        parts = urlsplit(self.path)
        params: dict[str, Any] = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
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

    def _dispatch(self) -> None:
        try:
            params = self._read_params()
            if not self._authorized(params):
                self._error(HTTPStatus.UNAUTHORIZED, "invalid or missing token")
                return
            path = urlsplit(self.path).path
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
        except Exception:  # noqa: BLE001
            log.exception("unhandled error for %s %s", self.command, self.path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    def _route(self, method: str, seg: list[str], params: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]]:
        router = self.server.router
        read = method in ("GET", "HEAD")
        write = method in ("POST", "PUT", "PATCH")
        ok = HTTPStatus.OK

        if not seg or seg == ["health"]:
            return ok, {"ok": True, "version": __version__}
        if seg == ["status"] and read:
            return ok, router.status()
        if seg == ["devices"] and read:
            return ok, {"ok": True, "devices": router.devices()}
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

        if read or write:
            return HTTPStatus.NOT_FOUND, {"ok": False, "error": f"no such endpoint: {method} /{'/'.join(seg)}"}
        return HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "method not allowed"}


def serve(router: Router, listen: str, port: int, token: str = "") -> ApiServer:
    server = ApiServer((listen, port), router, token)
    log.info("API listening on http://%s:%d/", *server.server_address[:2])
    return server
