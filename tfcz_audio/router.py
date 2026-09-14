"""The routing matrix: owns one pw-loopback per route plus the virtual OBS mic,
keeps desired volume/mute per route and re-applies it whenever a loopback
(re)appears in the graph."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .config import MAX_VOLUME, OBS_MIC, Config, RouteConfig
from .pw import Backend, Graph, LoopbackSpec, Process, PwError, cubic_to_db

log = logging.getLogger("tfcz.router")

VIRTUAL = "__virtual__"


class RouterError(Exception):
    pass


class UnknownRoute(RouterError):
    pass


class UnknownPreset(RouterError):
    pass


@dataclass
class RouteState:
    volume: float
    mute: bool


def route_spec(cfg: Config, route: RouteConfig) -> LoopbackSpec:
    sink = cfg.virtual.obs_mix_name if route.sink == OBS_MIC else route.sink
    common = {
        "node.latency": cfg.audio.latency,
        "node.dont-fallback": True,
        "node.dont-reconnect": False,
    }
    capture = {
        "node.name": route.in_node,
        "node.description": f"TFCZ {route.name} (capture)",
        "target.object": route.source,
        **common,
    }
    if route.capture_sink:
        capture["stream.capture.sink"] = True
    playback = {
        "node.name": route.out_node,
        "node.description": f"TFCZ {route.name} (playback)",
        "target.object": sink,
        **common,
    }
    return LoopbackSpec(name=f"tfcz.{route.name}", capture_props=capture, playback_props=playback, channels=cfg.audio.channels)


def virtual_spec(cfg: Config) -> LoopbackSpec:
    position = ["FL", "FR"] if cfg.audio.channels == 2 else ["MONO"]
    capture = {
        "media.class": "Audio/Sink",
        "node.name": cfg.virtual.obs_mix_name,
        "node.description": f"{cfg.virtual.obs_mic_description} (mix bus)",
        "audio.position": position,
        "node.latency": cfg.audio.latency,
    }
    playback = {
        "media.class": "Audio/Source/Virtual",
        "node.name": cfg.virtual.obs_mic_name,
        "node.description": cfg.virtual.obs_mic_description,
        "audio.position": position,
        "node.latency": cfg.audio.latency,
    }
    return LoopbackSpec(name="tfcz.virtual", capture_props=capture, playback_props=playback, channels=cfg.audio.channels)


class Router:
    def __init__(
        self,
        cfg: Config,
        backend: Backend,
        *,
        node_wait: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg
        self.backend = backend
        self.node_wait = node_wait
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.RLock()
        self.desired: dict[str, RouteState] = {
            name: RouteState(r.volume, r.mute) for name, r in cfg.routes.items()
        }
        self.procs: dict[str, Process] = {}
        self._applied: dict[str, tuple[int, float, bool]] = {}
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._started = False
        self._load_state()

    # ------------------------------------------------------------------ state

    def _load_state(self) -> None:
        path = self.cfg.state_file
        if not path or not path.is_file():
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable state file %s: %s", path, exc)
            return
        for name, st in (data.get("routes") or {}).items():
            if name in self.desired and isinstance(st, dict):
                try:
                    vol = float(st.get("volume", self.desired[name].volume))
                    mute = bool(st.get("mute", self.desired[name].mute))
                except (TypeError, ValueError):
                    continue
                self.desired[name] = RouteState(min(max(vol, 0.0), MAX_VOLUME), mute)
        log.info("restored route state from %s", path)

    def _save_state(self) -> None:
        path = self.cfg.state_file
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"routes": {n: asdict(s) for n, s in self.desired.items()}}, indent=2))
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("could not save state to %s: %s", path, exc)

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        with self._lock:
            self._started = True
            self._spawn(VIRTUAL)
            self._wait_for_node(self.cfg.virtual.obs_mix_name)
            for name in self.cfg.routes:
                self._spawn(name)
            self.reconcile()

    def stop(self) -> None:
        with self._lock:
            self._started = False
            for name in list(self.procs):
                self._terminate(name)
            self._applied.clear()

    def _terminate(self, name: str) -> None:
        proc = self.procs.pop(name, None)
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
        self._applied.pop(name, None)
        log.info("stopped loopback %s", name)

    def reload(self, new_cfg: Config) -> dict[str, Any]:
        """Switch to a new config at runtime. Only loopbacks whose spec
        actually changed are restarted; runtime volumes of unchanged routes
        are kept unless their config default changed."""
        with self._lock:
            old_specs = {n: route_spec(self.cfg, r) for n, r in self.cfg.routes.items()}
            new_specs = {n: route_spec(new_cfg, r) for n, r in new_cfg.routes.items()}
            virtual_changed = virtual_spec(self.cfg) != virtual_spec(new_cfg)

            desired: dict[str, RouteState] = {}
            for name, route in new_cfg.routes.items():
                old = self.cfg.routes.get(name)
                if old is not None and name in self.desired and (old.volume, old.mute) == (route.volume, route.mute):
                    desired[name] = self.desired[name]
                else:
                    desired[name] = RouteState(route.volume, route.mute)

            for name in list(self.procs):
                if name == VIRTUAL:
                    if virtual_changed:
                        self._terminate(name)
                    continue
                if name not in new_specs or old_specs.get(name) != new_specs[name]:
                    self._terminate(name)

            new_cfg.path = new_cfg.path or self.cfg.path
            self.cfg = new_cfg
            self.desired = desired
            for stale in set(self._failures) - set(new_cfg.routes) - {VIRTUAL}:
                self._failures.pop(stale, None)
                self._retry_at.pop(stale, None)
            self._save_state()

            if self._started:
                if VIRTUAL not in self.procs:
                    self._spawn(VIRTUAL)
                    self._wait_for_node(self.cfg.virtual.obs_mix_name)
                for name in self.cfg.routes:
                    if name not in self.procs:
                        self._spawn(name)
                self.reconcile()
            log.info("config reloaded: %d routes, %d presets", len(self.cfg.routes), len(self.cfg.presets))
            return self.status()

    def _spec(self, name: str) -> LoopbackSpec:
        if name == VIRTUAL:
            return virtual_spec(self.cfg)
        return route_spec(self.cfg, self.cfg.routes[name])

    def _spawn(self, name: str) -> bool:
        try:
            self.procs[name] = self.backend.spawn_loopback(self._spec(name))
        except PwError as exc:
            self._failures[name] = self._failures.get(name, 0) + 1
            delay = min(30.0, 2.0 ** self._failures[name])
            self._retry_at[name] = self._clock() + delay
            log.error("spawn %s failed (%s); retry in %.0fs", name, exc, delay)
            return False
        self._failures.pop(name, None)
        self._retry_at.pop(name, None)
        self._applied.pop(name, None)
        return True

    def _wait_for_node(self, node_name: str) -> bool:
        deadline = self._clock() + self.node_wait
        while True:
            try:
                if self.backend.graph().by_name(node_name):
                    return True
            except PwError as exc:
                log.warning("pw-dump failed while waiting for %s: %s", node_name, exc)
            if self._clock() >= deadline:
                log.warning("node %s did not appear within %.1fs", node_name, self.node_wait)
                return False
            self._sleep(0.25)

    def reconcile(self) -> None:
        """One supervisor pass: respawn dead loopbacks, apply pending volumes."""
        with self._lock:
            if not self._started:
                return
            now = self._clock()
            for name in [VIRTUAL, *self.cfg.routes]:
                proc = self.procs.get(name)
                if proc is not None and proc.poll() is None:
                    continue
                if proc is not None:
                    err = ""
                    stream = getattr(proc, "stderr", None)
                    if stream is not None:
                        try:
                            err = (stream.read() or "").strip()
                        except Exception:  # noqa: BLE001
                            err = ""
                    log.warning("loopback %s exited with %s %s", name, proc.poll(), err)
                    self.procs.pop(name, None)
                    self._failures[name] = self._failures.get(name, 0) + 1
                    self._retry_at[name] = now + min(30.0, 2.0 ** self._failures[name])
                    continue
                if self._retry_at.get(name, 0.0) <= now:
                    self._spawn(name)
            try:
                graph = self.backend.graph()
            except PwError as exc:
                log.warning("pw-dump failed: %s", exc)
                return
            for name in self.cfg.routes:
                self._apply(name, graph)

    def run_forever(self, stop: threading.Event, interval: float = 1.0) -> None:
        while not stop.is_set():
            try:
                self.reconcile()
            except Exception:  # noqa: BLE001
                log.exception("supervisor pass failed")
            stop.wait(interval)

    # ---------------------------------------------------------------- control

    def _apply(self, name: str, graph: Graph) -> bool:
        route = self.cfg.routes[name]
        node = graph.by_name(route.out_node)
        if node is None:
            return False
        want = self.desired[name]
        key = (node.id, want.volume, want.mute)
        if self._applied.get(name) == key:
            return True
        try:
            self.backend.set_volume(node.id, want.volume)
            self.backend.set_mute(node.id, want.mute)
        except PwError as exc:
            log.error("apply %s: %s", name, exc)
            return False
        self._applied[name] = key
        log.info("route %s: volume=%.3f mute=%s", name, want.volume, want.mute)
        return True

    def _route(self, name: str) -> RouteConfig:
        try:
            return self.cfg.routes[name]
        except KeyError:
            raise UnknownRoute(name) from None

    def set_route(self, name: str, *, volume: float | None = None, mute: bool | None = None) -> dict[str, Any]:
        self._route(name)
        with self._lock:
            state = self.desired[name]
            if volume is not None:
                if not 0.0 <= volume <= MAX_VOLUME:
                    raise RouterError(f"volume must be between 0 and {MAX_VOLUME}")
                state.volume = round(float(volume), 4)
            if mute is not None:
                state.mute = bool(mute)
            self._save_state()
            graph = self._graph_or_empty()
            self._apply(name, graph)
            return self.route_status(name, graph)

    def toggle_mute(self, name: str) -> dict[str, Any]:
        self._route(name)
        with self._lock:
            return self.set_route(name, mute=not self.desired[name].mute)

    def apply_preset(self, name: str) -> dict[str, Any]:
        try:
            preset = self.cfg.presets[name]
        except KeyError:
            raise UnknownPreset(name) from None
        with self._lock:
            for rname, entry in preset.items():
                state = self.desired[rname]
                if entry.volume is not None:
                    state.volume = entry.volume
                if entry.mute is not None:
                    state.mute = entry.mute
            self._save_state()
            graph = self._graph_or_empty()
            for rname in preset:
                self._apply(rname, graph)
            return {"preset": name, "routes": {r: self.route_status(r, graph) for r in preset}}

    def reset_to_config(self) -> dict[str, Any]:
        with self._lock:
            for name, route in self.cfg.routes.items():
                self.desired[name] = RouteState(route.volume, route.mute)
            self._save_state()
            graph = self._graph_or_empty()
            for name in self.cfg.routes:
                self._apply(name, graph)
            return self.status(graph)

    # ----------------------------------------------------------------- status

    def _graph_or_empty(self) -> Graph:
        try:
            return self.backend.graph()
        except PwError as exc:
            log.warning("pw-dump failed: %s", exc)
            return Graph()

    def route_status(self, name: str, graph: Graph | None = None) -> dict[str, Any]:
        route = self._route(name)
        graph = graph or self._graph_or_empty()
        want = self.desired[name]
        sink_name = self.cfg.virtual.obs_mix_name if route.sink == OBS_MIC else route.sink
        src = graph.by_name(route.source)
        dst = graph.by_name(sink_name)
        cap = graph.by_name(route.in_node)
        play = graph.by_name(route.out_node)
        proc = self.procs.get(name)
        return {
            "name": name,
            "description": route.description,
            "from": route.source,
            "to": route.sink,
            "volume": want.volume,
            "volume_db": cubic_to_db(want.volume),
            "mute": want.mute,
            "actual": {
                "volume": play.volume if play else None,
                "mute": play.mute if play else None,
            },
            "running": proc is not None and proc.poll() is None,
            "source_present": src is not None,
            "sink_present": dst is not None,
            "connected": bool(
                cap and play and graph.has_input_link(cap.id) and graph.has_output_link(play.id)
            ),
        }

    def status(self, graph: Graph | None = None) -> dict[str, Any]:
        graph = graph or self._graph_or_empty()
        vproc = self.procs.get(VIRTUAL)
        devices = {}
        for key, node_name in self.cfg.devices.items():
            devices[key] = {"node": node_name, "present": graph.by_name(node_name) is not None}
        return {
            "ok": True,
            "virtual_mic": {
                "node": self.cfg.virtual.obs_mic_name,
                "description": self.cfg.virtual.obs_mic_description,
                "running": vproc is not None and vproc.poll() is None,
                "present": graph.by_name(self.cfg.virtual.obs_mic_name) is not None,
            },
            "devices": devices,
            "routes": {name: self.route_status(name, graph) for name in self.cfg.routes},
            "presets": sorted(self.cfg.presets),
        }

    def list_presets(self) -> dict[str, Any]:
        return {
            name: {r: {k: v for k, v in asdict(e).items() if v is not None} for r, e in entries.items()}
            for name, entries in self.cfg.presets.items()
        }

    def devices(self) -> list[dict[str, Any]]:
        return [n.to_dict() for n in self._graph_or_empty().audio_devices()]
