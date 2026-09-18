"""Live signal level meters.

One ``pw-record`` per metered node streams raw 16-bit audio to us; a reader
thread computes the peak of every ~100 ms chunk. A meter that dies is
restarted with backoff, and a meter that produces no data is reported as
inactive. Nothing here can affect the routing: meters are read-only
observers and every failure is contained.
"""

from __future__ import annotations

import logging
import math
import random
import re
import shlex
import subprocess
import threading
import time
from array import array
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import OBS_MIC, Config

log = logging.getLogger("tfcz.meters")

RATE = 48000
CHANNELS = 2
CHUNK_FRAMES = RATE // 10  # 100 ms
CHUNK_BYTES = CHUNK_FRAMES * CHANNELS * 2
STALE_AFTER = 1.5  # seconds without data -> "no signal / not connected"
SILENCE_DB = -60.0
OBS_KEY = "obs"


@dataclass
class Level:
    peak: float = 0.0  # 0..1 linear
    db: float = SILENCE_DB
    updated: float = 0.0  # monotonic timestamp of last chunk
    running: bool = False
    error: str = ""

    def to_dict(self, now: float) -> dict[str, Any]:
        fresh = self.updated > 0 and now - self.updated < STALE_AFTER
        return {
            "peak": round(self.peak, 4),
            "db": round(self.db, 1),
            "active": fresh,  # receiving audio data at all
            "signal": fresh and self.db > -50.0,  # audible signal present
            "running": self.running,
            "error": self.error,
        }


@dataclass
class MeterSpec:
    key: str
    node: str
    capture_sink: bool = False

    def command(self, raw: bool = True, props: bool = True, tool: str = "pw-record") -> list[str]:
        if tool == "parec":
            # PulseAudio client shipped with pipewire-pulse; writes raw PCM to
            # stdout with no options that differ between versions
            device = f"{self.node}.monitor" if self.capture_sink else self.node
            return [
                "parec", "--format=s16le", f"--rate={RATE}", f"--channels={CHANNELS}",
                f"--device={device}", "--latency-msec=100",
                f"--client-name=tfcz.meter.{re.sub(r'[^A-Za-z0-9_.-]', '_', self.key)}",
            ]
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.key)
        stream_props = {
            "node.name": f"tfcz.meter.{safe}",
            "node.description": f"TFCZ meter {safe}",
            "node.dont-fallback": "true",
            "node.passive": "false",
            # unique restore-stream keys: nothing WirePlumber remembers for one meter reaches another
            "media.role": f"tfcz.meter.{safe}",
            "application.id": f"tfcz.meter.{safe}",
            "application.name": f"tfcz.meter.{safe}",
        }
        if self.capture_sink:
            stream_props["stream.capture.sink"] = "true"
        # target both ways: the command line flag and the stream property. Which
        # of the two a given pw-record honours has changed between versions.
        stream_props["target.object"] = self.node
        spa = "{ " + " ".join(f'{k} = "{_spa_escape(v)}"' for k, v in stream_props.items()) + " }"
        cmd = ["pw-record"]
        if raw:
            # without --raw the stream carries a WAV header, which the reader strips
            cmd.append("--raw")
        cmd += ["--format", "s16", "--rate", str(RATE), "--channels", str(CHANNELS)]
        # no --latency: the default (100 ms) matches the window we read, and the
        # flag's accepted syntax differs between versions
        cmd += ["--target", self.node]
        if props:
            cmd += ["-P", spa]
        cmd.append("-")
        return cmd


def _spa_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def peak_of(chunk: bytes) -> float:
    """Peak absolute sample of interleaved s16 little-endian data, 0..1."""
    if len(chunk) < 2:
        return 0.0
    samples = array("h")
    samples.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
    hi = max(samples)
    lo = min(samples)
    peak = max(hi, -lo)
    return min(1.0, peak / 32768.0)


def _channel_stats(samples: "array[int]") -> dict[str, float]:
    count = len(samples)
    if not count:
        return {"peak": 0.0, "rms": 0.0, "zcr": 0.0, "dc": 0.0, "clipped": 0.0}
    total = 0.0
    squares = 0.0
    crossings = 0
    clipped = 0
    peak = 0
    previous = samples[0]
    for value in samples:
        total += value
        squares += float(value) * value
        if (value >= 0) != (previous >= 0):
            crossings += 1
        previous = value
        magnitude = -value if value < 0 else value
        if magnitude > peak:
            peak = magnitude
        if magnitude >= 32700:
            clipped += 1
    return {
        "peak": min(1.0, peak / 32768.0),
        "rms": min(1.0, math.sqrt(squares / count) / 32768.0),
        "zcr": crossings / count,
        "dc": total / count / 32768.0,
        "clipped": clipped / count,
    }


def signal_stats(chunk: bytes, channels: int = CHANNELS) -> dict[str, Any]:
    """Describe a piece of s16 audio well enough to tell what it is.

    Everything is measured per channel and then combined -- interleaved samples
    would fake a zero crossing on every second value as soon as the two
    channels differ, which is exactly the case this is meant to judge.

    * ``peak`` and ``rms``: how loud,
    * ``crest`` (peak over rms): speech and music peak far above their average,
      uniform noise barely does,
    * ``zcr``: the share of samples where the wave changes sign. Random data
      sits near 0.5, anything with a pitch far below,
    * ``clipped``: samples pinned to the end of the scale.

    Loud with a high ``zcr`` is what an HDMI input carrying a compressed stream
    looks like: structureless noise, which no microphone and no game produces.
    """
    channels = max(1, channels)
    empty = {"frames": 0, "peak": 0.0, "rms": 0.0, "crest": 0.0, "zcr": 0.0, "clipped": 0.0,
             "dc": 0.0, "channels": [0.0] * channels, "silent": True}
    if len(chunk) < 2 * channels:
        return empty
    samples = array("h")
    usable = len(chunk) - (len(chunk) % (2 * channels))
    samples.frombytes(chunk[:usable])
    if not len(samples):
        return empty

    per_channel = [_channel_stats(samples[c::channels]) for c in range(channels)]
    peak = max(c["peak"] for c in per_channel)
    rms = max(c["rms"] for c in per_channel)
    return {
        "frames": len(samples) // channels,
        "peak": round(peak, 5),
        "rms": round(rms, 5),
        "crest": round(peak / rms, 2) if rms > 0 else 0.0,
        "zcr": round(sum(c["zcr"] for c in per_channel) / channels, 4),
        "clipped": round(max(c["clipped"] for c in per_channel), 5),
        "dc": round(max(per_channel, key=lambda c: abs(c["dc"]))["dc"], 5),
        "channels": [round(c["peak"], 5) for c in per_channel],
        "silent": peak < 0.0005,
    }


def signal_verdict(stats: dict[str, Any], kind: str = "input") -> list[dict[str, str]]:
    """Plain sentences about what the numbers mean. Empty means: looks fine."""
    out: list[dict[str, str]] = []
    if not stats.get("frames"):
        return [{"level": "error", "title": "Es kam gar nichts an",
                 "fix": "Gerät prüfen: steckt es, ist es im System stummgeschaltet, hält es ein anderes Programm?"}]
    if stats["silent"]:
        return [{"level": "warning", "title": "Digitale Stille",
                 "fix": "Sprich hinein bzw. starte den Ton an der Quelle und miss nochmals. Bleibt es exakt still, "
                        "liefert das Gerät nichts."}]
    # loud, structureless, almost no silence between samples: that is not sound
    if stats["zcr"] > 0.35 and stats["rms"] > 0.15 and stats["crest"] < 2.6:
        out.append({"level": "error", "title": "Das sieht nicht nach Ton aus, sondern nach Rauschen",
                    "why": f"Laut und ohne Form: Nulldurchgänge bei {int(stats['zcr'] * 100)} % der Werte "
                           f"(Sprache liegt unter 10 %), Scheitelfaktor {stats['crest']}.",
                    "fix": "Typisch für eine HDMI-Quelle, die Dolby/DTS statt PCM sendet. Stell die Tonausgabe der "
                           "Quelle auf PCM / Stereo. Sonst: Kabel oder Eingang prüfen."})
    if stats["clipped"] > 0.001:
        out.append({"level": "warning", "title": f"Übersteuert ({stats['clipped'] * 100:.1f} % der Werte am Anschlag)",
                    "why": "Werte am Ende der Skala werden abgeschnitten.",
                    "fix": "Aufnahmepegel des Geräts senken (etwa 70 %) und den Mikrofon-Boost ausschalten."})
    elif stats["peak"] > 0.98:
        out.append({"level": "info", "title": "Sehr nahe an der Grenze",
                    "fix": "Etwas leiser stellen, sonst verzerrt es bei lauten Stellen."})
    channels = stats.get("channels") or []
    if len(channels) == 2 and max(channels) > 0.02 and min(channels) < max(channels) * 0.02:
        side = "links" if channels[0] > channels[1] else "rechts"
        out.append({"level": "warning", "title": f"Nur ein Kanal hat Ton ({side})",
                    "why": f"links {channels[0]:.3f}, rechts {channels[1]:.3f}.",
                    "fix": "Bei einem Mikrofon ist das normal. Bei Spielton oder Kopfhörern deutet es auf ein "
                           "falsches Profil, ein defektes Kabel oder einen stummen Kanal hin."})
    if abs(stats["dc"]) > 0.02:
        out.append({"level": "warning", "title": "Gleichspannungsanteil im Signal",
                    "why": f"Mittelwert {stats['dc']:+.3f} statt 0.",
                    "fix": "Meist ein Treiber- oder Kabelproblem; es klingt dumpf und kann knacken."})
    if kind == "input" and not out and stats["peak"] < 0.02:
        out.append({"level": "info", "title": "Sehr leise",
                    "fix": "Falls jemand hineingesprochen hat: Aufnahmepegel des Geräts erhöhen."})
    return out


def to_db(peak: float) -> float:
    if peak <= 0:
        return SILENCE_DB
    return max(SILENCE_DB, 20.0 * math.log10(peak))


UNSUPPORTED_MARKERS = ("unrecognized option", "unrecognised option", "unknown option", "invalid option", "unknown or invalid")


def wav_data_offset(buf: bytes) -> int | None:
    """Offset of the sample data in a WAV stream, or None if not seen yet."""
    if len(buf) < 12 or buf[:4] != b"RIFF":
        return 0 if len(buf) >= 4 else None  # not a WAV: samples start immediately
    at = 12
    while at + 8 <= len(buf):
        chunk = buf[at : at + 4]
        try:
            size = int.from_bytes(buf[at + 4 : at + 8], "little")
        except ValueError:
            return None
        if chunk == b"data":
            return at + 8
        at += 8 + size + (size & 1)
    return None


SHAPES: tuple[dict[str, Any], ...] = (
    {"tool": "pw-record", "raw": True, "props": True},
    {"tool": "pw-record", "raw": False, "props": True},
    {"tool": "pw-record", "raw": False, "props": False},
    {"tool": "parec", "raw": True, "props": False},
)


def _shape_label(shape: dict[str, Any]) -> str:
    if shape["tool"] != "pw-record":
        return shape["tool"]
    if shape["raw"]:
        return "pw-record"
    return "pw-record ohne --raw" if shape["props"] else "pw-record ohne --raw und ohne -P"


class Meter:
    def __init__(self, spec: MeterSpec, spawn: Callable[[list[str]], Any], raw: bool = True, props: bool = True, tool: str = "pw-record"):
        self.spec = spec
        self.raw = raw
        self.props = props
        self.tool = tool
        self.unsupported = ""
        self._spawn = spawn
        self.level = Level()
        self.proc: Any = None
        self._thread: threading.Thread | None = None
        self.failures = 0
        self.retry_at = 0.0
        self.started_at = 0.0

    def start(self) -> bool:
        cmd = self.spec.command(raw=self.raw, props=self.props, tool=self.tool)
        try:
            self.proc = self._spawn(cmd)
        except (OSError, subprocess.SubprocessError) as exc:
            self.failures += 1
            self.retry_at = time.monotonic() + min(60.0, 2.0 ** self.failures)
            self.level.running = False
            self.level.error = str(exc)
            log.warning("meter %s: cannot start (%s)", self.spec.key, exc)
            return False
        self.started_at = time.monotonic()
        self.level.running = True
        self.level.error = ""
        self._thread = threading.Thread(target=self._read, name=f"meter-{self.spec.key}", daemon=True)
        self._thread.start()
        log.debug("meter %s: %s", self.spec.key, shlex.join(cmd))
        return True

    def _read(self) -> None:
        proc = self.proc
        if proc is None or getattr(proc, "stdout", None) is None:
            return
        try:
            buf = bytearray()
            header = bytearray()
            skipping = not self.raw and self.tool == "pw-record"  # a WAV header precedes the samples
            while True:
                # one pipe read returns a single quantum (~5 ms); collect a whole
                # window so the level is a real peak and not a random slice
                chunk = proc.stdout.read(CHUNK_BYTES - len(buf))
                if not chunk:
                    break
                if skipping:
                    header += chunk
                    at = wav_data_offset(bytes(header))
                    if at is None:
                        continue
                    buf += header[at:]
                    skipping = False
                else:
                    buf += chunk
                if len(buf) < CHUNK_BYTES:
                    continue
                peak = peak_of(bytes(buf))
                buf.clear()
                # short peak hold: a level that only falls between windows reads
                # much more calmly than one that jumps back to zero
                self.level.peak = max(peak, self.level.peak * 0.6)
                self.level.db = to_db(self.level.peak)
                self.level.updated = time.monotonic()
        except (OSError, ValueError):
            pass
        self.level.running = False

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
                proc.wait(timeout=0.5)  # reap it; a killed child would linger as a zombie
            except Exception:  # noqa: BLE001
                pass
        self.level.running = False

    def reconcile(self, now: float) -> None:
        if self.alive():
            if self.failures and now - self.started_at > 30.0:
                self.failures = 0  # lived long enough: forget the crash history
            return
        if self.proc is not None:
            err = getattr(self.proc, "stderr_tail", "")[:400]
            log.warning("meter %s exited (%s) %s", self.spec.key, self.proc.poll(), err)
            if any(marker in err.lower() for marker in UNSUPPORTED_MARKERS):
                self.unsupported = err.strip()[:200]
            self.proc = None
            self.level.running = False
            self.level.error = err or "exited"
            self.failures += 1
            self.retry_at = now + min(60.0, 2.0 ** self.failures)
            return
        if now >= self.retry_at:
            self.start()


class _MeterProcess:
    """pw-record with stdout read by the meter and stderr drained in the
    background (bounded), so the child can never block on a full pipe."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self.pid = proc.pid
        self.stdout = proc.stdout
        self._head: list[str] = []
        threading.Thread(target=self._drain, name=f"meter-stderr-{proc.pid}", daemon=True).start()

    def _drain(self) -> None:
        if self._proc.stderr is None:
            return
        try:
            for raw in iter(self._proc.stderr.readline, b""):
                # the first lines carry the reason; the rest is usually a help dump
                if len(self._head) < 8:
                    self._head.append(raw.decode("utf-8", "replace").rstrip())
        except (OSError, ValueError):
            pass

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._head)

    def poll(self) -> int | None:
        return self._proc.poll()

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()

    def wait(self, timeout: float | None = None) -> int:
        return self._proc.wait(timeout=timeout)


def _default_spawn(cmd: list[str]) -> Any:
    proc = subprocess.Popen(  # noqa: S603 - fixed argv
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
    )
    return _MeterProcess(proc)




def meter_specs(cfg: Config, resolved: dict[str, str | None] | None = None) -> list[MeterSpec]:
    """Meter every device alias used by a route (that currently resolves to a
    node), plus the OBS microphone."""
    resolved = resolved if resolved is not None else {a: s.node or None for a, s in cfg.devices.items()}
    sources: set[str] = set()
    sinks: set[str] = set()
    for route in cfg.routes.values():
        if route.source_ref in cfg.devices:
            sources.add(route.source_ref)
        if route.sink_ref in cfg.devices:
            sinks.add(route.sink_ref)
    # a source that is really a sink monitor (capture_sink route) must be metered the same way
    monitor_sources = {r.source_ref for r in cfg.routes.values() if r.capture_sink}
    specs = [MeterSpec(a, resolved[a], capture_sink=a in monitor_sources) for a in sorted(sources) if resolved.get(a)]
    specs += [MeterSpec(a, resolved[a], capture_sink=True) for a in sorted(sinks - sources) if resolved.get(a)]
    specs.append(MeterSpec(OBS_KEY, cfg.virtual.obs_mic_name, capture_sink=False))
    return specs


class MeterManager:
    """Owns all meters, follows config reloads, never raises out of reconcile()."""

    WATCH_SECONDS = 120.0
    MAX_WATCH = 32

    def __init__(
        self,
        cfg_getter: Callable[[], Config],
        spawn: Callable[[list[str]], Any] = _default_spawn,
        resolved_getter: Callable[[], dict[str, str | None]] | None = None,
        known_nodes: Callable[[], set[str]] | None = None,
        pw_recovered: Callable[[], int] | None = None,
    ):
        self._cfg = cfg_getter
        self._resolved = resolved_getter
        self._known_nodes = known_nodes
        self._pw_recovered = pw_recovered
        self._pw_recovered_seen = pw_recovered() if pw_recovered else 0
        self._spawn = spawn
        self._lock = threading.Lock()
        self.meters: dict[str, Meter] = {}
        self._watch: dict[str, tuple[float, bool]] = {}  # node -> (expires, capture_sink)
        self.enabled = True
        self.disabled_reason = ""
        # Which command shape this pw-record accepts is found out by trying,
        # not by parsing --help: the help text differs between versions.
        self.shape = 0
        self._announced = False

    def watch(self, nodes: list[dict[str, Any]], seconds: float | None = None) -> list[str]:
        """Temporarily meter arbitrary nodes (used by the setup wizard so the
        user can identify a headset by speaking into it). Keys = node names."""
        if seconds is not None and seconds <= 0:
            with self._lock:
                self._watch.clear()
            self.reconcile()
            return []
        expires = time.monotonic() + (seconds or self.WATCH_SECONDS)
        known = self._known_nodes() if self._known_nodes else None
        added: list[str] = []
        with self._lock:
            for item in nodes[:16]:
                name = str(item.get("name", "")).strip()
                if not name or name.startswith("tfcz.") or len(name) > 200:
                    continue
                if known is not None and name not in known:
                    continue  # only real, currently present devices get a pw-record
                if name not in self._watch and len(self._watch) >= self.MAX_WATCH:
                    break
                self._watch[name] = (expires, str(item.get("kind", "input")) == "output")
                added.append(name)
        self.reconcile()
        return added

    def _watched_specs(self, now: float) -> list[MeterSpec]:
        self._watch = {n: v for n, v in self._watch.items() if v[0] > now}
        return [MeterSpec(name, name, capture_sink=sink) for name, (_, sink) in self._watch.items()]

    def reconcile(self) -> None:
        try:
            self._reconcile()
        except Exception:  # noqa: BLE001
            log.exception("meter reconcile failed (meters are optional; routing unaffected)")

    def _reconcile(self) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        with self._lock:
            if self._pw_recovered and self._pw_recovered() != self._pw_recovered_seen:
                self._pw_recovered_seen = self._pw_recovered()
                for m in self.meters.values():
                    m.failures, m.retry_at = 0, 0.0  # PipeWire is back: retry immediately
            wanted = {s.key: s for s in meter_specs(self._cfg(), self._resolved() if self._resolved else None)}
            for spec in self._watched_specs(now):
                wanted.setdefault(spec.key, spec)
            for key in list(self.meters):
                if key not in wanted or self.meters[key].spec != wanted[key]:
                    self.meters.pop(key).stop()
            for key, spec in wanted.items():
                if key not in self.meters:
                    shape = SHAPES[self.shape]
                    self.meters[key] = Meter(spec, self._spawn, **shape)
            for meter in self.meters.values():
                meter.reconcile(now)
            if not self._announced and any(m.level.updated for m in self.meters.values()):
                self._announced = True
                log.info("Pegelmessung liefert Daten (%s)", self.shape_label())
            self._degrade_if_needed()

    def _degrade_if_needed(self) -> None:
        """The recorder refused to run: try the next command shape rather than
        leaving the user with empty bars and a guess about the cause."""
        import shutil

        broken = [m for m in self.meters.values() if m.unsupported or m.failures >= 3]
        if not broken or not self.meters:
            return
        complaint = next((m.unsupported for m in broken if m.unsupported), "") or "der Aufnahmebefehl startet nicht"
        complaint = complaint.splitlines()[0][:160]
        nxt = self.shape + 1
        # only skip shapes whose tool is really missing; with an injected spawn
        # (tests, fake mode) every shape is reachable
        while nxt < len(SHAPES) and self._spawn is _default_spawn and not shutil.which(SHAPES[nxt]["tool"]):
            nxt += 1
        if nxt >= len(SHAPES):
            self.enabled = False
            self.disabled_reason = f"kein Aufnahmebefehl funktioniert auf diesem System: {complaint}"
            log.error("level meters off: %s", complaint)
            for meter in self.meters.values():
                meter.stop()
            self.meters.clear()
            return
        log.warning("Pegelmessung: %s; nächster Versuch mit %s", complaint, _shape_label(SHAPES[nxt]))
        self.shape = nxt
        self._announced = False
        for meter in self.meters.values():
            meter.stop()
        self.meters.clear()

    def levels(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            return {key: m.level.to_dict(now) for key, m in self.meters.items()}

    def shape_label(self) -> str:
        return _shape_label(SHAPES[self.shape])

    def problem(self) -> dict[str, Any] | None:
        """A UI problem entry when the level bars cannot work. Meters are
        optional, so this is never an error: routing is unaffected."""
        if not self.enabled:
            return {
                "level": "info", "code": "meters_off", "what": "meters",
                "title": "Die Pegelbalken sind ausgeschaltet",
                "why": self.disabled_reason or "Die Pegelmessung ist deaktiviert.",
                "effect": "Keine bewegten Balken; alles andere läuft normal.",
                "fix": "",
            }
        now = time.monotonic()
        with self._lock:
            meters = list(self.meters.values())
            if not meters:
                return None
            failing = [m for m in meters if not m.alive() or m.failures]
            errors = [m.level.error for m in meters if m.level.error]
            live = [m for m in meters if m.level.updated and now - m.level.updated < STALE_AFTER]
        if live:
            return None
        if failing:
            detail = (errors[0].splitlines()[0][:160] if errors else "das Hilfsprogramm beendet sich sofort")
            return {
                "level": "warning", "code": "meters_broken", "what": "meters",
                "title": "Die Pegelbalken funktionieren nicht",
                "why": f"Das Hilfsprogramm für die Pegel (pw-record) läuft auf diesem System nicht: {detail}",
                "effect": "Die Balken bleiben leer. Das Leiten des Tons und das OBS-Mikrofon sind nicht betroffen.",
                "fix": "Führe unter «Erweitert» den Tonweg-Test aus, dort stehen der genaue Befehl und sein Fehler. Oder setze in der Einstellungsdatei [audio] meters = false.",
            }
        return {
            "level": "info", "code": "meters_silent", "what": "meters",
            "title": "Die Pegelbalken zeigen keinen Ton",
            "why": "Die Messung läuft, aber von den Geräten kommen keine Tondaten an.",
            "effect": "Die Balken bleiben leer, auch wenn jemand spricht.",
            "fix": "Führe unter «Erweitert» den Tonweg-Test aus: er nimmt von jedem Gerät auf und zeigt, was ankommt.",
        }

    def stop(self, deadline: float = 3.0) -> None:
        from .router import stop_all

        with self._lock:
            procs = [m.proc for m in self.meters.values() if m.proc is not None]
            for m in self.meters.values():
                m.proc = None
                m.level.running = False
            self.meters.clear()
        stop_all(procs, deadline)


class FakeMeterManager:
    """Synthetic levels for --fake mode: inputs 'talk', outputs follow the
    unmuted routes, so the demo UI behaves like the real thing."""

    def __init__(
        self,
        cfg_getter: Callable[[], Config],
        desired_getter: Callable[[], dict[str, Any]],
        resolved_getter: Callable[[], dict[str, str | None]] | None = None,
    ):
        self._cfg = cfg_getter
        self._desired = desired_getter
        self._resolved = resolved_getter
        self._phase: dict[str, float] = {}
        self._watch: dict[str, float] = {}
        self.enabled = True

    def watch(self, nodes: list[dict[str, Any]], seconds: float | None = None) -> list[str]:
        if seconds is not None and seconds <= 0:
            self._watch.clear()
            return []
        expires = time.monotonic() + (seconds or 120.0)
        names = [str(n.get("name", "")) for n in nodes if n.get("name")][:32]
        for n in names:
            self._watch[n] = expires
        return names

    def reconcile(self) -> None:
        pass

    def problem(self) -> dict[str, Any] | None:
        return None

    def stop(self, deadline: float = 0.0) -> None:
        pass

    def _input_level(self, key: str, now: float) -> float:
        ph = self._phase.setdefault(key, random.random() * 10)
        talking = math.sin(now * 0.35 + ph) > -0.2  # talks ~60 % of the time
        if not talking:
            return 0.002
        return max(0.0, min(1.0, 0.25 + 0.2 * math.sin(now * 7 + ph) + random.uniform(-0.08, 0.12)))

    def levels(self) -> dict[str, Any]:
        cfg = self._cfg()
        desired = self._desired()
        resolved = self._resolved() if self._resolved else None
        specs = meter_specs(cfg, resolved)
        now = time.monotonic()
        out: dict[str, Any] = {}
        inputs: dict[str, float] = {}
        for spec in specs:
            if spec.key == OBS_KEY or spec.capture_sink:
                continue
            inputs[spec.key] = self._input_level(spec.key, now)
        outputs: dict[str, float] = {OBS_KEY: 0.0}
        for name, route in cfg.routes.items():
            st = desired.get(name)
            if st is None or st.mute or route.source_ref not in inputs:
                continue
            target = OBS_KEY if route.sink == OBS_MIC else route.sink_ref
            outputs[target] = max(outputs.get(target, 0.0), inputs[route.source_ref] * (st.volume ** 3))
        for spec in specs:
            peak = inputs.get(spec.key, outputs.get(spec.key, 0.0))
            lvl = Level(peak=peak, db=to_db(peak), updated=now, running=True)
            out[spec.key] = lvl.to_dict(now)
        node_of = {s.key: s.node for s in specs}
        by_node = {node_of[k]: v for k, v in out.items() if k in node_of}
        for name, exp in list(self._watch.items()):
            if exp < now:
                del self._watch[name]
                continue
            if name in by_node:
                out[name] = by_node[name]
            else:
                # unassigned hardware in the demo "talks" too, but more rarely
                peak = self._input_level("w:" + name, now) if math.sin(now * 0.2 + hash(name) % 7) > 0.3 else 0.001
                out[name] = Level(peak=peak, db=to_db(peak), updated=now, running=True).to_dict(now)
        return out
