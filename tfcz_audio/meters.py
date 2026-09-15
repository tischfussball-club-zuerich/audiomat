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

    def command(self) -> list[str]:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.key)
        props = {
            "node.name": f"tfcz.meter.{safe}",
            "node.description": f"TFCZ meter {self.key}",
            "node.dont-fallback": "true",
            "media.role": "Production",
        }
        if self.capture_sink:
            props["stream.capture.sink"] = "true"
        spa = "{ " + " ".join(f'{k} = "{v}"' for k, v in props.items()) + " }"
        return [
            "pw-record",
            "--raw",
            "--format", "s16",
            "--rate", str(RATE),
            "--channels", str(CHANNELS),
            "--latency", f"{CHUNK_FRAMES}",
            "--target", self.node,
            "-P", spa,
            "-",
        ]


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


def to_db(peak: float) -> float:
    if peak <= 0:
        return SILENCE_DB
    return max(SILENCE_DB, 20.0 * math.log10(peak))


class Meter:
    def __init__(self, spec: MeterSpec, spawn: Callable[[list[str]], Any]):
        self.spec = spec
        self._spawn = spawn
        self.level = Level()
        self.proc: Any = None
        self._thread: threading.Thread | None = None
        self.failures = 0
        self.retry_at = 0.0
        self.started_at = 0.0

    def start(self) -> bool:
        cmd = self.spec.command()
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
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                chunk = proc.stdout.read(CHUNK_BYTES)
                if not chunk:
                    break
                peak = peak_of(chunk)
                self.level.peak = peak
                self.level.db = to_db(peak)
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
            except Exception:  # noqa: BLE001
                pass
        self.level.running = False

    def reconcile(self, now: float) -> None:
        if self.alive():
            if self.failures and now - self.started_at > 30.0:
                self.failures = 0  # lived long enough: forget the crash history
            return
        if self.proc is not None:
            err = getattr(self.proc, "stderr_tail", "")[-200:]
            log.warning("meter %s exited (%s) %s", self.spec.key, self.proc.poll(), err)
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
        self._tail: list[str] = []
        threading.Thread(target=self._drain, name=f"meter-stderr-{proc.pid}", daemon=True).start()

    def _drain(self) -> None:
        if self._proc.stderr is None:
            return
        try:
            for raw in iter(self._proc.stderr.readline, b""):
                self._tail.append(raw.decode("utf-8", "replace").rstrip())
                del self._tail[:-10]
        except (OSError, ValueError):
            pass

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._tail)

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


_RAW_SUPPORT: bool | None = None


def pw_record_supports_raw() -> bool:
    """pw-record --raw exists since PipeWire 0.3.6x; older versions would
    write a WAV header into our sample stream. Probe once."""
    global _RAW_SUPPORT  # noqa: PLW0603
    if _RAW_SUPPORT is None:
        try:
            out = subprocess.run(["pw-record", "--help"], capture_output=True, text=True, timeout=5, check=False)
            _RAW_SUPPORT = "--raw" in (out.stdout + out.stderr)
        except (OSError, subprocess.SubprocessError):
            _RAW_SUPPORT = False
    return _RAW_SUPPORT


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
    specs = [MeterSpec(a, resolved[a], capture_sink=False) for a in sorted(sources) if resolved.get(a)]
    specs += [MeterSpec(a, resolved[a], capture_sink=True) for a in sorted(sinks - sources) if resolved.get(a)]
    specs.append(MeterSpec(OBS_KEY, cfg.virtual.obs_mic_name, capture_sink=False))
    return specs


class MeterManager:
    """Owns all meters, follows config reloads, never raises out of reconcile()."""

    WATCH_SECONDS = 120.0

    def __init__(
        self,
        cfg_getter: Callable[[], Config],
        spawn: Callable[[list[str]], subprocess.Popen] = _default_spawn,
        resolved_getter: Callable[[], dict[str, str | None]] | None = None,
    ):
        self._cfg = cfg_getter
        self._resolved = resolved_getter
        self._spawn = spawn
        self._lock = threading.Lock()
        self.meters: dict[str, Meter] = {}
        self._watch: dict[str, tuple[float, bool]] = {}  # node -> (expires, capture_sink)
        self.enabled = True
        self.disabled_reason = ""
        if spawn is _default_spawn and not pw_record_supports_raw():
            self.enabled = False
            self.disabled_reason = "pw-record does not support --raw (PipeWire too old); level bars are off, routing is unaffected"
            log.warning(self.disabled_reason)

    def watch(self, nodes: list[dict[str, Any]], seconds: float | None = None) -> list[str]:
        """Temporarily meter arbitrary nodes (used by the setup wizard so the
        user can identify a headset by speaking into it). Keys = node names."""
        expires = time.monotonic() + (seconds or self.WATCH_SECONDS)
        added: list[str] = []
        with self._lock:
            for item in nodes[:16]:
                name = str(item.get("name", "")).strip()
                if not name or name.startswith("tfcz."):
                    continue
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
            wanted = {s.key: s for s in meter_specs(self._cfg(), self._resolved() if self._resolved else None)}
            for spec in self._watched_specs(now):
                wanted.setdefault(spec.key, spec)
            for key in list(self.meters):
                if key not in wanted or self.meters[key].spec != wanted[key]:
                    self.meters.pop(key).stop()
            for key, spec in wanted.items():
                if key not in self.meters:
                    self.meters[key] = Meter(spec, self._spawn)
            for meter in self.meters.values():
                meter.reconcile(now)

    def levels(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            return {key: m.level.to_dict(now) for key, m in self.meters.items()}

    def stop(self) -> None:
        with self._lock:
            for meter in self.meters.values():
                meter.stop()
            self.meters.clear()


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
        expires = time.monotonic() + (seconds or 120.0)
        names = [str(n.get("name", "")) for n in nodes if n.get("name")]
        for n in names:
            self._watch[n] = expires
        return names

    def reconcile(self) -> None:
        pass

    def stop(self) -> None:
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
