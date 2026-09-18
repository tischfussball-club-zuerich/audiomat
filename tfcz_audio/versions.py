"""Which pieces of software this router depends on, and which version of each.

When something sounds wrong on the streaming PC, the answer is often "the
distribution shipped a different PipeWire" or "the capture driver did not
rebuild after a kernel update". Collecting all of that in one place turns a
long remote debugging session into a screenshot.

Everything here is read-only and best effort: a missing tool is a normal
answer, not an error. Nothing is started that could show up in the audio
graph -- ``pw-loopback``, ``pw-record`` and ``pw-top`` do real work when run,
so they are only located on disk, never executed.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__

# tools that must never be executed just to read a version: they would open
# devices, create nodes or sit in a terminal loop until the timeout kills them
NEVER_RUN = ("pw-loopback", "pw-record", "pw-cat", "pw-play", "pw-top", "parec", "obs")

TOOLS: tuple[tuple[str, str], ...] = (
    ("pw-loopback", "pipewire-bin"),
    ("pw-dump", "pipewire-bin"),
    ("pw-record", "pipewire-bin"),
    ("pw-top", "pipewire-bin"),
    ("pw-cli", "pipewire-bin"),
    ("pw-metadata", "pipewire-bin"),
    ("wpctl", "wireplumber"),
    ("pactl", "pulseaudio-utils"),
    ("parec", "pulseaudio-utils"),
    ("systemctl", "systemd"),
)

PACKAGES = ("pipewire", "pipewire-bin", "pipewire-audio", "pipewire-pulse", "wireplumber",
            "libpipewire-0.3-0", "libspa-0.2-modules", "pulseaudio-utils", "alsa-utils", "obs-studio")

CACHE_SECONDS = 20.0

_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None


def _run(cmd: list[str], timeout: float = 3.0) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                           timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, ""
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return r.returncode, (r.stdout + r.stderr).strip()


def _number(text: str) -> str:
    """First version-looking number in a tool's greeting line."""
    m = re.search(r"\d+\.\d+(?:\.\d+)*", text or "")
    return m.group(0) if m else ""


def _item(name: str, version: str = "", detail: str = "", level: str = "") -> dict[str, str]:
    """One row. ``level`` is empty when the row is merely informational."""
    if not level:
        level = "ok" if version else "missing"
    return {"name": name, "version": version or "nicht gefunden", "detail": detail, "level": level}


def _os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text(errors="replace").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def _packages() -> dict[str, str]:
    """Installed versions in one dpkg call; unknown packages are simply absent."""
    if not shutil.which("dpkg-query"):
        return {}
    rc, out = _run(["dpkg-query", "-W", "-f", "${Package} ${Version}\n", *PACKAGES], timeout=5.0)
    found: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in PACKAGES:
            found[parts[0]] = parts[1]
    return found


def _this_program() -> list[dict[str, str]]:
    from .api import ui_build

    # the install path itself is right above under "Fassung", no need to repeat it
    return [
        _item("tfcz-audio", __version__, f"Seite {ui_build()}"),
        _item("Python", sys.version.split()[0], sys.executable),
    ]


def _sound_system() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    if shutil.which("pw-cli"):
        rc, out = _run(["pw-cli", "--version"])
        first = next((l for l in out.splitlines() if "ompiled" in l), "")
        linked = next((l for l in out.splitlines() if "inked" in l), "")
        rows.append(_item("PipeWire", _number(linked) or _number(out),
                          f"gebaut gegen {_number(first)}" if _number(first) and _number(first) != _number(linked) else ""))
    else:
        rows.append(_item("PipeWire", "", "pw-cli fehlt (sudo apt install pipewire-bin)"))

    if shutil.which("wpctl"):
        rc, out = _run(["wpctl", "--version"])
        rows.append(_item("WirePlumber", _number(out), "" if rc == 0 else "antwortet nicht"))
    else:
        rows.append(_item("WirePlumber", "", "wpctl fehlt (sudo apt install wireplumber)"))

    if shutil.which("pactl"):
        rc, out = _run(["pactl", "info"])
        server = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("Server Name")), "")
        if rc == 0 and server:
            # the string says which server actually answers: PulseAudio instead of
            # PipeWire here is the single most common cause of a silent router
            rows.append(_item("Tonserver", server, "" if "PipeWire" in server else "kein PipeWire!",
                              level="ok" if "PipeWire" in server else "bad"))
        else:
            rows.append(_item("Tonserver", "", "pactl info antwortet nicht"))

    try:
        alsa = Path("/proc/asound/version").read_text(errors="replace").strip()
        rows.append(_item("ALSA", _number(alsa), alsa))
    except OSError:
        rows.append(_item("ALSA", "", "/proc/asound fehlt (kein Tonsystem im Kernel)"))

    rows.append(_item("Kernel", platform.release(), _os_name()))
    if shutil.which("systemctl"):
        rc, out = _run(["systemctl", "--version"])
        rows.append(_item("systemd", _number(out.splitlines()[0] if out else ""), ""))
    return rows


def _capture_card() -> list[dict[str, str]]:
    """The AVMatrix card runs on an out-of-tree module that can quietly fail to
    rebuild after a kernel update."""
    rows: list[dict[str, str]] = []
    version = ""
    for path in ("/sys/module/hws/version", "/sys/module/hws/srcversion"):
        try:
            version = Path(path).read_text(errors="replace").strip()
        except OSError:
            continue
        if version:
            break
    loaded = Path("/sys/module/hws").exists()
    cards = ""
    try:
        cards = Path("/proc/asound/cards").read_text(errors="replace")
    except OSError:
        pass
    inputs = cards.lower().count("hws")
    if loaded or inputs:
        rows.append(_item("hws (HDMI-Aufnahme)", version or "geladen",
                          f"{inputs} Eingang/Eingänge" if inputs else "Treiber geladen, keine Karte gemeldet"))
    else:
        rows.append(_item("hws (HDMI-Aufnahme)", "", "Treiber nicht geladen — siehe docs/hdmi-capture.md"))
    if shutil.which("dkms"):
        rc, out = _run(["dkms", "status"])
        line = next((l for l in out.splitlines() if "hws" in l.lower()), "")
        if line:
            rows.append(_item("DKMS", _number(line) or "eingetragen", line.strip()))
    return rows


def _tools(packages: dict[str, str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for tool, package in TOOLS:
        where = shutil.which(tool)
        if not where:
            rows.append(_item(tool, "", f"fehlt — sudo apt install {package}"))
            continue
        version = packages.get(package, "")
        if not version and tool not in NEVER_RUN:
            rc, out = _run([tool, "--version"])
            version = _number(out) if rc == 0 else ""
        rows.append(_item(tool, version or "vorhanden", where))
    return rows


def _packages_group(packages: dict[str, str]) -> list[dict[str, str]]:
    if not shutil.which("dpkg-query"):
        return [_item("dpkg", "", "kein dpkg auf diesem System — keine Paketliste", level="info")]
    rows = [_item(name, packages[name], "") for name in PACKAGES if name in packages]
    missing = [name for name in PACKAGES if name not in packages]
    if missing:
        rows.append(_item("nicht installiert", f"{len(missing)} Pakete", ", ".join(missing), level="info"))
    return rows


def collect(fresh: bool = False) -> dict[str, Any]:
    """All groups at once. Cached briefly so that a page which polls does not
    keep spawning processes."""
    global _cache

    with _lock:
        if _cache and not fresh and time.time() - _cache[0] < CACHE_SECONDS:
            return _cache[1]

    packages = _packages()
    report = {
        "collected": time.time(),
        "host": platform.node(),
        "groups": [
            {"title": "Dieses Programm", "items": _this_program()},
            {"title": "Tonsystem", "items": _sound_system()},
            {"title": "Aufnahmekarte", "items": _capture_card()},
            {"title": "Werkzeuge", "items": _tools(packages)},
            {"title": "Pakete", "items": _packages_group(packages)},
        ],
    }
    with _lock:
        _cache = (time.time(), report)
    return report


def as_text(report: dict[str, Any] | None = None) -> str:
    """The same list as plain text, for the terminal and for pasting into a chat."""
    report = report or collect()
    out: list[str] = [f"tfcz-audio Versionen ({report.get('host', '')})"]
    for group in report["groups"]:
        out.append("")
        out.append(f"=== {group['title']}")
        width = max((len(i["name"]) for i in group["items"]), default=0)
        for item in group["items"]:
            line = f"  {item['name'].ljust(width)}  {item['version']}"
            if item["detail"]:
                line += f"   ({item['detail']})"
            out.append(line)
    return "\n".join(out)
