"""A short test tone into one headphone.

Everything else in this program measures what comes *in*. This is the only
check for what goes *out*, and it answers the questions a level bar cannot:
is this the headphone I think it is, do both ears work, are left and right
the right way round, is the device muted in the system, is it stuck on one
channel because of its profile.

The tone is deliberately mild -- a quarter of full scale, a pair of notes,
faded in and out -- because it is played into headphones someone is wearing.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any

log = logging.getLogger("tfcz.tone")

RATE = 48000
CHANNELS = 2
AMPLITUDE = 0.25          # a quarter of full scale: audible, never startling
NOTES = (523.25, 659.25)  # C5 and E5, a friendly third
MAX_SECONDS = 4.0
SIDES = ("left", "right", "both")
SIDE_LABELS = {"left": "links", "right": "rechts", "both": "beide Seiten"}


def make_wav(side: str = "both", seconds: float = 1.2) -> bytes:
    """A stereo WAV with the tone on the chosen side and silence on the other.

    Silence on one side is the whole point: it turns "I hear something" into
    "I hear it on the left", which is what tells a swapped pair of headphones
    from a correct one.
    """
    if side not in SIDES:
        raise ValueError(f"side must be one of {', '.join(SIDES)}")
    seconds = min(max(float(seconds), 0.2), MAX_SECONDS)
    frames = int(RATE * seconds)
    fade = max(1, int(RATE * 0.02))  # 20 ms, so it does not click
    left_on = side in ("left", "both")
    right_on = side in ("right", "both")

    samples = bytearray()
    for i in range(frames):
        note = NOTES[0] if i < frames // 2 else NOTES[1]
        envelope = min(1.0, i / fade, (frames - i) / fade)
        value = int(32767 * AMPLITUDE * envelope * math.sin(2 * math.pi * note * i / RATE))
        samples += struct.pack("<hh", value if left_on else 0, value if right_on else 0)

    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(CHANNELS)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(bytes(samples))
    return buffer.getvalue()


def commands(node: str, path: str) -> list[list[str]]:
    """How to play the file, best first.

    pw-play targets a node by name, which is what we want: the tone has to go
    to this headphone and not to whatever is the system default. paplay is the
    fallback for installations where only the PulseAudio tools are present.
    """
    shapes = []
    if shutil.which("pw-play"):
        shapes.append(["pw-play", "--target", node, path])
    if shutil.which("pw-cat"):
        shapes.append(["pw-cat", "-p", "--target", node, path])
    if shutil.which("paplay"):
        shapes.append(["paplay", f"--device={node}", path])
    return shapes


class TonePlayer:
    """Plays one tone at a time and remembers how it went."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.node = ""
        self.label = ""
        self.side = ""
        self.started = 0.0
        self.finished = 0.0
        self.error = ""
        self.command = ""

    def _snapshot(self) -> dict[str, Any]:
        """The caller holds the lock; never takes it itself."""
        return {
            "running": self.running,
            "node": self.node,
            "label": self.label,
            "side": self.side,
            "error": self.error,
            "command": self.command,
            "seconds": round((self.finished or time.time()) - self.started, 1) if self.started else 0,
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def play(self, node: str, label: str, side: str = "both", seconds: float = 1.2) -> dict[str, Any]:
        if side not in SIDES:
            raise ValueError(f"side must be one of {', '.join(SIDES)}")
        with self._lock:
            if self.running:
                return {"started": False, **self._snapshot(),
                        "problem": "Es läuft bereits ein Testton. Warte kurz."}
            self.running, self.node, self.label, self.side = True, node, label, side
            self.started, self.finished, self.error, self.command = time.time(), 0.0, "", ""
        threading.Thread(target=self._run, name="tone", args=(node, side, seconds), daemon=True).start()
        return {"started": True, **self.state(), "problem": ""}

    def _run(self, node: str, side: str, seconds: float) -> None:
        error, used = "", ""
        path = ""
        try:
            data = make_wav(side, seconds)
            base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
            with tempfile.NamedTemporaryFile(prefix="tfcz-tone-", suffix=".wav", dir=base, delete=False) as handle:
                handle.write(data)
                path = handle.name
            shapes = commands(node, path)
            if not shapes:
                error = ("Zum Abspielen fehlt ein Werkzeug (pw-play aus pipewire-bin oder paplay "
                         "aus pulseaudio-utils).")
            for cmd in shapes:
                used = " ".join(cmd)
                try:
                    proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                                          # never longer than the tone plus a moment
                                          timeout=seconds + 8.0, check=False, stdin=subprocess.DEVNULL)
                except subprocess.TimeoutExpired:
                    error = "Das Abspielen hat zu lange gedauert und wurde abgebrochen."
                    continue
                except (OSError, subprocess.SubprocessError) as exc:
                    error = f"{cmd[0]} liess sich nicht starten: {exc}"
                    continue
                if proc.returncode == 0:
                    error = ""
                    break
                error = (proc.stderr or proc.stdout or f"{cmd[0]} endete mit {proc.returncode}").strip()[:200]
        except Exception as exc:  # noqa: BLE001 - a test tone must never take the daemon down
            log.exception("test tone failed")
            error = str(exc)[:200]
        finally:
            if path:
                try:
                    Path(path).unlink()
                except OSError:
                    pass
            with self._lock:
                self.running, self.error, self.command, self.finished = False, error, used, time.time()
        if error:
            log.warning("test tone on %s failed: %s", node, error)
