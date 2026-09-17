"""Self-update: pull the source checkout and re-run the installer.

The installer restarts the service, and systemd kills the whole control group
when it does. An update started as a child of the daemon would therefore kill
itself halfway through, so it runs as its own transient unit and writes to a
log file that the page reads afterwards.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("tfcz.update")

DONE_MARKER = "=== fertig (rc="
STALE_AFTER = 300.0  # seconds without an end marker before a run counts as abandoned
UNIT = "tfcz-audio-update"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "tfcz-audio"


def log_path() -> Path:
    return state_dir() / "update.log"


def source_dir() -> Path | None:
    """Where the checkout lives. install.sh records it next to the installed
    package; a daemon started straight from a checkout finds it itself."""
    pkg = Path(__file__).resolve().parent
    recorded = pkg.parent / "source-path"
    try:
        if recorded.is_file():
            candidate = Path(recorded.read_text().strip()).expanduser()
            if candidate.is_dir():
                return candidate
    except OSError:
        pass
    if (pkg.parent / ".git").is_dir():
        return pkg.parent
    return None


def check() -> tuple[Path | None, str]:
    """(directory, reason it cannot be used)."""
    src = source_dir()
    if src is None:
        return None, ("Der Ort der Quelldateien ist nicht bekannt. Führe ./install.sh einmal von Hand aus, "
                      "danach merkt sich der Router den Ordner.")
    if not (src / ".git").is_dir():
        return None, (f"{src} ist kein Git-Ordner, sondern vermutlich ein entpacktes ZIP. "
                      "Einmal klonen: git clone <repo>, dann von dort ./install.sh ausführen.")
    if not (src / "install.sh").is_file():
        return None, f"In {src} fehlt install.sh."
    if not shutil.which("systemd-run"):
        return None, ("systemd-run fehlt; ohne das würde die Aktualisierung sich selbst abschiessen, "
                      "sobald der Dienst neu startet. Führe die Aktualisierung von Hand aus.")
    return src, ""


def status() -> dict[str, Any]:
    path = log_path()
    try:
        text = path.read_text(errors="replace")
        age = time.time() - path.stat().st_mtime
    except OSError:
        src, problem = check()
        return {"running": False, "log": "", "rc": None, "state": "idle", "problem": problem,
                "source": str(src) if src else ""}
    rc: int | None = None
    running = True
    state = "running"
    if DONE_MARKER in text:
        running = False
        state = "done"
        try:
            rc = int(text.rsplit(DONE_MARKER, 1)[1].split(")")[0])
        except (IndexError, ValueError):
            rc = None
    elif age > STALE_AFTER:
        running = False
        state = "stale"
    src, problem = check()
    return {"running": running, "log": text[-20000:], "rc": rc, "state": state,
            "problem": problem, "source": str(src) if src else "", "age": round(age, 1)}


def start() -> dict[str, Any]:
    # a run in flight is the more relevant answer than anything about the setup
    current = status()
    if current["running"]:
        return {"started": False, **current, "problem": "Eine Aktualisierung läuft bereits."}
    src, problem = check()
    if src is None:
        return {"started": False, **current, "problem": problem}

    state_dir().mkdir(parents=True, exist_ok=True)
    script = state_dir() / "update.sh"
    script.write_text(
        "#!/bin/bash\n"
        f'cd {shell_quote(str(src))} || exit 1\n'
        "export GIT_TERMINAL_PROMPT=0\n"  # fail instead of waiting for a password nobody can type
        'echo "== git pull =="\n'
        "git pull --ff-only 2>&1\n"
        "rc=$?\n"
        'if [ $rc -ne 0 ]; then echo; echo "' + DONE_MARKER + '$rc)"; exit $rc; fi\n'
        'echo; echo "== ./install.sh =="\n'
        "./install.sh 2>&1\n"
        "rc=$?\n"
        'echo; echo "' + DONE_MARKER + '$rc)"\n'
    )
    script.chmod(0o755)
    try:
        log_path().write_text(f"Aktualisierung gestartet in {src}\n")
    except OSError as exc:
        return {"started": False, "problem": f"Protokolldatei nicht schreibbar: {exc}", **current}

    cmd = [
        "systemd-run", "--user", "--collect", "--quiet",
        f"--unit={UNIT}-{int(time.time())}",
        "--setenv=PATH=" + os.environ.get("PATH", "/usr/bin:/bin"),
        "/bin/bash", "-c", f"{shell_quote(str(script))} >> {shell_quote(str(log_path()))} 2>&1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"started": False, "problem": f"systemd-run liess sich nicht starten: {exc}", **status()}
    if proc.returncode != 0:
        return {"started": False, "problem": (proc.stderr or proc.stdout or "systemd-run meldete einen Fehler").strip()[:300], **status()}
    log.warning("Aktualisierung gestartet in %s; der Dienst startet dabei neu", src)
    return {"started": True, "problem": "", **status()}


def shell_quote(text: str) -> str:
    import shlex

    return shlex.quote(text)
