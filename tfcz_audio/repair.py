"""Find what is broken around the router, and fix what can be fixed safely.

Three kinds of repair live here:

* things this process may simply do (write a PipeWire drop-in, clean up
  leftover helper processes),
* things the user's own systemd may do (enable and start the audio session,
  enable this service),
* things that need root (install packages, switch on lingering).

For the last kind nothing is escalated quietly: a passwordless sudo is used
when it exists, otherwise pkexec asks on the desktop, and when neither is
available the exact command is handed over to be run in a terminal. Every
action is a fixed entry in the table below -- nothing the caller sends ever
becomes part of a command line.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("tfcz.repair")

TIMEOUT = 600.0  # apt over a slow line is allowed to take its time
USER_NAME = re.compile(r"^[a-z_][a-z0-9_-]*\$?$")


@dataclass(frozen=True)
class Action:
    id: str
    title: str
    why: str
    effect: str
    command: tuple[str, ...] = ()
    needs_root: bool = False
    manual: bool = False  # shown with its command, never run from here
    note: str = ""
    python: str = ""  # name of a fix done in this process instead of a command
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, root: dict[str, Any]) -> dict[str, Any]:
        runnable = bool(self.python) or (bool(self.command) and (not self.needs_root or root["available"]))
        return {
            "id": self.id,
            "title": self.title,
            "why": self.why,
            "effect": self.effect,
            "command": " ".join(self.command),
            "needs_root": self.needs_root,
            "manual": self.manual,
            "runnable": runnable and not self.manual,
            "note": self.note,
            **self.detail,
        }


# --------------------------------------------------------------------- root


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                           timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "Zeitüberschreitung"
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return r.returncode, (r.stdout + r.stderr).strip()


def root_mode() -> dict[str, Any]:
    """How this daemon could become root, if at all."""
    if os.getuid() == 0:
        return {"available": True, "how": "root", "why": "läuft bereits als root"}
    if shutil.which("sudo") and _run(["sudo", "-n", "true"], timeout=4)[0] == 0:
        return {"available": True, "how": "sudo", "why": "sudo ohne Passwort ist erlaubt"}
    if shutil.which("pkexec") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return {"available": True, "how": "pkexec", "why": "pkexec fragt am Bildschirm nach dem Passwort"}
    return {
        "available": False,
        "how": "",
        "why": "Dieser Dienst darf nicht Administrator werden. Führe den Befehl in einem Terminal aus, "
               "dort fragt sudo nach deinem Passwort.",
    }


def _elevate(command: tuple[str, ...], root: dict[str, Any]) -> list[str]:
    if root["how"] == "sudo":
        return ["sudo", "-n", *command]
    if root["how"] == "pkexec":
        return ["pkexec", *command]
    return list(command)


# ------------------------------------------------------------- Feststellen


def _user_name() -> str:
    try:
        import pwd

        name = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, ImportError, OSError):
        name = os.environ.get("USER", "")
    return name if USER_NAME.match(name or "") else ""


def _missing_packages() -> list[str]:
    from .versions import TOOLS

    missing = []
    for tool, package in TOOLS:
        if not shutil.which(tool) and package not in missing and package != "systemd":
            missing.append(package)
    return missing


def _unit_active(unit: str) -> bool:
    return _run(["systemctl", "--user", "is-active", unit], timeout=4)[1].strip() == "active"


def _rate_problem(router: Any) -> dict[str, Any]:
    """Devices that disagree about the sample rate, and whether a drop-in of
    ours is already in place."""
    try:
        findings = router.rate_findings()
    except Exception:  # noqa: BLE001 - a broken graph must not break the plan
        return {}
    if not findings:
        return {}
    return {"titles": [f["title"] for f in findings], "path": str(rate_drop_in())}


def rate_drop_in() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pipewire" / "pipewire.conf.d" / "10-tfcz-rate.conf"


def detect(router: Any = None) -> list[Action]:
    """Everything that is wrong and could be repaired, most important first."""
    actions: list[Action] = []

    missing = _missing_packages()
    if missing:
        actions.append(Action(
            id="install-packages",
            title=f"{len(missing)} fehlende(s) Paket(e) installieren",
            why="Ohne diese Programme kann der Router weder Verbindungen bauen noch Pegel messen: "
                + ", ".join(missing),
            effect="Einzelne Funktionen fehlen ganz oder still: keine Pegelanzeige, keine Messung, kein Ton.",
            command=("apt-get", "install", "-y", *missing),
            needs_root=True,
        ))

    if shutil.which("systemctl"):
        dead = [u for u in ("pipewire", "wireplumber") if not _unit_active(u)]
        if dead:
            actions.append(Action(
                id="start-sound",
                title="Tonsystem starten",
                why=f"Nicht aktiv: {', '.join(dead)}.",
                effect="Ohne laufendes PipeWire und WirePlumber gibt es überhaupt keinen Ton.",
                command=("systemctl", "--user", "enable", "--now", "pipewire", "pipewire-pulse", "wireplumber"),
            ))
        enabled = _run(["systemctl", "--user", "is-enabled", "tfcz-audio"], timeout=4)[1].strip()
        if enabled not in ("enabled", "static", "linked"):
            actions.append(Action(
                id="enable-service",
                title="Router beim Anmelden starten",
                why=f"Der Dienst tfcz-audio ist nicht aktiviert (Zustand: {enabled or 'unbekannt'}).",
                effect="Nach einem Neustart des Rechners läuft der Router erst, wenn ihn jemand von Hand startet.",
                command=("systemctl", "--user", "enable", "tfcz-audio"),
            ))

    user = _user_name()
    if user and shutil.which("loginctl"):
        rc, out = _run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger"], timeout=4)
        if rc == 0 and "Linger=yes" not in out:
            actions.append(Action(
                id="enable-linger",
                title="Ohne Anmeldung starten",
                why="Lingering ist aus, der Dienst startet deshalb erst, wenn sich jemand anmeldet.",
                effect="Nach einem Stromausfall bleibt der Streaming-Rechner stumm, bis sich jemand anmeldet.",
                command=("loginctl", "enable-linger", user),
                needs_root=True,
            ))

    if shutil.which("pactl"):
        info = _run(["pactl", "info"], timeout=5)[1]
        if "Server Name" in info and "PipeWire" not in info:
            actions.append(Action(
                id="pulseaudio",
                title="PulseAudio läuft statt PipeWire",
                why="Auf diesem Rechner antwortet PulseAudio. Dieser Router baut auf PipeWire auf.",
                effect="Nichts von alldem hier funktioniert, solange PulseAudio den Ton hält.",
                command=("sudo", "apt", "install", "pipewire-audio", "wireplumber", "&&",
                         "systemctl", "--user", "--now", "disable", "pulseaudio.service", "pulseaudio.socket", "&&",
                         "systemctl", "--user", "--now", "enable", "pipewire", "pipewire-pulse", "wireplumber"),
                needs_root=True,
                manual=True,
                note="Das tauscht das Tonsystem des ganzen Rechners aus. Führe es von Hand aus, wenn du sicher bist.",
            ))

    rate = _rate_problem(router) if router is not None else {}
    if rate:
        actions.append(Action(
            id="rate-dropin",
            title="Alles auf 48000 Hz festlegen",
            why="; ".join(rate["titles"]),
            effect="Ständiges Umrechnen kostet Qualität, und driftende Uhren knacken.",
            python="rate_dropin",
            note=f"Schreibt {rate['path']}. Danach muss das Tonsystem einmal neu starten "
                 "(systemctl --user restart pipewire wireplumber) — dabei setzt der Ton kurz aus.",
        ))

    try:
        from .pw import find_stale_helpers

        stale = find_stale_helpers()
    except Exception:  # noqa: BLE001
        stale = []
    if stale:
        actions.append(Action(
            id="stale-helpers",
            title=f"{len(stale)} übrig gebliebene Hilfsprozess(e) beenden",
            why="Aus einem früheren Lauf hängen noch pw-loopback-Prozesse herum.",
            effect="Sie halten Geräte belegt und erzeugen doppelte Verbindungen im Signalweg.",
            python="stale_helpers",
        ))

    return actions


def plan(router: Any = None) -> dict[str, Any]:
    root = root_mode()
    actions = detect(router)
    return {
        "root": root,
        "actions": [a.to_dict(root) for a in actions],
        "healthy": not actions,
    }


def action_by_id(action_id: str, router: Any = None) -> Action | None:
    """The command that runs always comes from a fresh detection, never from
    the caller: the id only picks one of our own entries."""
    return next((a for a in detect(router) if a.id == action_id), None)


# ------------------------------------------------------------------ Ausführen


class Runner:
    """One repair at a time, with its output kept for the page to read."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.action = ""
        self.title = ""
        self.output = ""
        self.running = False
        self.started = 0.0
        self.finished = 0.0
        self.rc: int | None = None

    def _snapshot(self) -> dict[str, Any]:
        """The caller holds the lock; never takes it itself."""
        return {
            "action": self.action, "title": self.title, "output": self.output[-20000:],
            "running": self.running, "rc": self.rc,
            "seconds": round((self.finished or time.time()) - self.started, 1) if self.started else 0,
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def start(self, action: Action, root: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.running:
                return {"started": False, **self._snapshot(), "problem": "Es läuft bereits eine Reparatur."}
            self.action, self.title, self.output, self.running = action.id, action.title, "", True
            self.started, self.finished, self.rc = time.time(), 0.0, None
        threading.Thread(target=self._run, name=f"repair-{action.id}", args=(action, root), daemon=True).start()
        return {"started": True, **self.state(), "problem": ""}

    def _write(self, text: str) -> None:
        with self._lock:
            self.output += text

    def _run(self, action: Action, root: dict[str, Any]) -> None:
        rc = 1
        try:
            if action.python:
                rc = self._python_fix(action)
            else:
                cmd = _elevate(action.command, root) if action.needs_root else list(action.command)
                self._write("$ " + " ".join(cmd) + "\n\n")
                try:
                    proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                                          timeout=TIMEOUT, check=False, stdin=subprocess.DEVNULL)
                    self._write((proc.stdout or "") + (proc.stderr or ""))
                    rc = proc.returncode
                except subprocess.TimeoutExpired:
                    self._write(f"\nAbgebrochen: der Befehl lief länger als {int(TIMEOUT)} Sekunden.\n")
                    rc = 124
                except (OSError, subprocess.SubprocessError) as exc:
                    self._write(f"\nDer Befehl liess sich nicht starten: {exc}\n")
                    rc = 127
        except Exception as exc:  # noqa: BLE001 - a failed repair must not take the daemon with it
            log.exception("repair %s failed", action.id)
            self._write(f"\nUnerwarteter Fehler: {exc}\n")
            rc = 1
        self._write(f"\n=== fertig (rc={rc})\n")
        with self._lock:
            self.running, self.rc, self.finished = False, rc, time.time()

    def _python_fix(self, action: Action) -> int:
        if action.python == "rate_dropin":
            path = rate_drop_in()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "# von tfcz-audio geschrieben: alle Geräte auf 48000 Hz\n"
                "context.properties = {\n"
                "    default.clock.rate = 48000\n"
                "    default.clock.allowed-rates = [ 48000 ]\n"
                "}\n"
            )
            self._write(f"geschrieben: {path}\n\n{path.read_text()}\n")
            self._write("Damit es greift: systemctl --user restart pipewire wireplumber\n")
            return 0
        if action.python == "stale_helpers":
            from .pw import kill_stale_helpers

            killed = kill_stale_helpers()
            self._write(f"beendet: {len(killed)} Prozess(e) {killed}\n" if killed
                        else "Es war nichts mehr übrig.\n")
            return 0
        self._write(f"Unbekannte Reparatur: {action.python}\n")
        return 1
