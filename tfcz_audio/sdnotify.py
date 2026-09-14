"""Tiny sd_notify client (no dependency on libsystemd).

When started by systemd with ``Type=notify`` and ``WatchdogSec=``, the
daemon reports READY once the routes are up and pings the watchdog from
its supervisor loop. If the loop ever hangs, systemd restarts the service.
Outside systemd (no NOTIFY_SOCKET) every call is a no-op.
"""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger("tfcz.sdnotify")


class Notifier:
    def __init__(self, env: dict[str, str] | None = None):
        env = os.environ if env is None else env
        self.address = env.get("NOTIFY_SOCKET", "")
        self.watchdog_usec = int(env.get("WATCHDOG_USEC", "0") or 0)
        self._sock: socket.socket | None = None
        if self.address:
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            except OSError as exc:
                log.warning("sd_notify disabled: %s", exc)
                self._sock = None

    @property
    def enabled(self) -> bool:
        return self._sock is not None

    @property
    def watchdog_interval(self) -> float | None:
        """Recommended ping interval in seconds (half the systemd timeout)."""
        if not self.enabled or self.watchdog_usec <= 0:
            return None
        return self.watchdog_usec / 1_000_000 / 2

    def _send(self, message: str) -> None:
        if self._sock is None:
            return
        addr = self.address
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        try:
            self._sock.sendto(message.encode(), addr)
        except OSError as exc:
            log.debug("sd_notify send failed: %s", exc)

    def ready(self, status: str = "") -> None:
        self._send("READY=1" + (f"\nSTATUS={status}" if status else ""))

    def watchdog(self) -> None:
        self._send("WATCHDOG=1")

    def status(self, text: str) -> None:
        self._send(f"STATUS={text}")

    def stopping(self) -> None:
        self._send("STOPPING=1")
