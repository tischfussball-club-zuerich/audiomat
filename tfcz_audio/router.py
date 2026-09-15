"""The routing matrix: owns one pw-loopback per route plus the virtual OBS mic,
resolves configured devices to live PipeWire nodes, keeps desired
volume/mute per route and re-applies it whenever a loopback (re)appears."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .config import MAX_VOLUME, OBS_MIC, Config, DeviceSpec, RouteConfig, human
from .pw import (
    Backend,
    Graph,
    LoopbackSpec,
    Node,
    Process,
    PwError,
    cubic_to_db,
    describe_node,
    identity_for,
    physical_devices,
    resolve_match,
)

log = logging.getLogger("tfcz.router")


def stop_all(procs, deadline: float = 5.0) -> None:
    """SIGTERM every process, then wait for all of them within ONE shared
    deadline; whatever is still alive gets SIGKILL."""
    procs = list(procs)
    for proc in procs:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    end = time.monotonic() + deadline
    stubborn = []
    for proc in procs:
        remaining = max(0.05, end - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except Exception:  # noqa: BLE001
            stubborn.append(proc)
    for proc in stubborn:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    for proc in stubborn:
        try:
            proc.wait(timeout=0.05)
        except Exception:  # noqa: BLE001
            pass

VIRTUAL = "__virtual__"
UNRESOLVED_PREFIX = "tfcz.unresolved."


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


@dataclass
class Resolved:
    node: str | None  # node.name to target, None if nothing matches right now
    present: bool
    ambiguous: bool = False
    candidates: int = 0


def resolve_devices(cfg: Config, graph: Graph) -> dict[str, Resolved]:
    out: dict[str, Resolved] = {}
    for alias, spec in cfg.devices.items():
        if spec.is_static:
            out[alias] = Resolved(spec.node, graph.by_name(spec.node) is not None)
            continue
        nodes = resolve_match(spec.match, graph)
        if nodes:
            out[alias] = Resolved(nodes[0].name, True, ambiguous=len(nodes) > 1, candidates=len(nodes))
        else:
            out[alias] = Resolved(None, False)
    return out


def _target(cfg: Config, ref: str, resolved: dict[str, Resolved]) -> str:
    if ref == OBS_MIC:
        return cfg.virtual.obs_mix_name
    if ref in cfg.devices:
        node = resolved.get(ref, Resolved(None, False)).node
        return node or f"{UNRESOLVED_PREFIX}{ref}"
    return ref  # raw node name


def route_spec(cfg: Config, route: RouteConfig, resolved: dict[str, Resolved]) -> LoopbackSpec:
    common = {
        "node.latency": cfg.audio.latency,
        "node.dont-fallback": True,
        "node.dont-reconnect": False,
    }

    def identity(node_name: str) -> dict[str, Any]:
        # WirePlumber's restore-stream keys saved volumes by media.role /
        # application.id / application.name. Every stream gets its own key so a
        # volume set on one route can never be restored onto another stream.
        return {"media.role": node_name, "application.id": node_name, "application.name": node_name}

    capture = {
        "node.name": route.in_node,
        "node.description": f"TFCZ {route.name} (capture)",
        "target.object": _target(cfg, route.source_ref, resolved),
        **identity(route.in_node),
        **common,
    }
    if route.capture_sink:
        capture["stream.capture.sink"] = True
    playback = {
        "node.name": route.out_node,
        "node.description": f"TFCZ {route.name} (playback)",
        "target.object": _target(cfg, route.sink_ref, resolved),
        **identity(route.out_node),
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
        "media.role": cfg.virtual.obs_mix_name,
        "application.id": cfg.virtual.obs_mix_name,
        "application.name": cfg.virtual.obs_mix_name,
    }
    playback = {
        "media.class": "Audio/Source/Virtual",
        "node.name": cfg.virtual.obs_mic_name,
        "node.description": cfg.virtual.obs_mic_description,
        "audio.position": position,
        "node.latency": cfg.audio.latency,
        "media.role": cfg.virtual.obs_mic_name,
        "application.id": cfg.virtual.obs_mic_name,
        "application.name": cfg.virtual.obs_mic_name,
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
        self.desired: dict[str, RouteState] = {name: RouteState(r.volume, r.mute) for name, r in cfg.routes.items()}
        self.procs: dict[str, Process] = {}
        self.resolved: dict[str, Resolved] = {}
        self._spec_used: dict[str, LoopbackSpec] = {}
        self._applied: dict[str, tuple[int, float, bool]] = {}
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._spawned_at: dict[str, float] = {}
        self._started = False
        self.last_error: str = ""
        self.config_error: str = ""  # set by the CLI when the config could not be loaded cleanly
        self._graph_cache: tuple[float, Graph] | None = None
        self._graph_fetch_lock = threading.Lock()  # concurrent cache misses share one pw-dump
        self._apply_retry_at: dict[str, float] = {}  # per-route backoff after a failed wpctl call
        self.apply_budget = 4  # max wpctl-affected routes per supervisor pass; keeps a pass short
        self._state_timer: threading.Timer | None = None
        self._state_last_saved = 0.0
        self.pw_recovered_count = 0  # bumped whenever PipeWire comes back; meters reset their backoff on it
        self.graph_cache_ttl = 0.0  # seconds; the CLI enables caching for the real backend
        self._pw_down_logged = False
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
        """Coalesce bursts (slider drags) into at most ~2 writes per second."""
        if not self.cfg.state_file:
            return
        now = time.monotonic()
        if now - self._state_last_saved >= 0.5:
            self._write_state()
            return
        if self._state_timer is None or not self._state_timer.is_alive():
            self._state_timer = threading.Timer(0.5, self._write_state)
            self._state_timer.daemon = True
            self._state_timer.start()

    def _write_state(self) -> None:
        self._state_last_saved = time.monotonic()
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

    def _fetch_graph(self) -> Graph:
        """Graph with a short cache so UI polling does not multiply pw-dump calls."""
        now = self._clock()
        cached = self._graph_cache
        if cached is not None and now - cached[0] < self.graph_cache_ttl:
            return cached[1]
        with self._graph_fetch_lock:
            cached = self._graph_cache
            if cached is not None and self._clock() - cached[0] < self.graph_cache_ttl:
                return cached[1]
            graph = self.backend.graph()
            self._graph_cache = (self._clock(), graph)
            return graph

    def _invalidate_graph(self) -> None:
        self._graph_cache = None

    def _graph_or_empty(self) -> Graph:
        try:
            graph = self._fetch_graph()
        except PwError as exc:
            if not self._pw_down_logged:
                log.warning("pw-dump failed: %s (further failures are not logged until it recovers)", exc)
                self._pw_down_logged = True
            self.last_error = f"cannot talk to PipeWire: {exc}"
            return Graph()
        if self._pw_down_logged:
            self._pipewire_recovered()
        return graph

    def _refresh_resolution(self, graph: Graph) -> None:
        new = resolve_devices(self.cfg, graph)
        for alias, res in new.items():
            old = self.resolved.get(alias)
            if old is None or old.node != res.node:
                if res.node:
                    log.info("device %s -> %s", alias, res.node)
                elif old is not None and old.node:
                    log.warning("device %s (%s) is gone", alias, old.node)
        self.resolved = new

    def start(self) -> None:
        with self._lock:
            self._started = True
            self._refresh_resolution(self._graph_or_empty())
            self._spawn(VIRTUAL)
            self._wait_for_node(self.cfg.virtual.obs_mix_name)
            for name in self.cfg.routes:
                self._spawn(name)
            self.reconcile()

    def stop(self, deadline: float = 5.0) -> None:
        """Terminate all helpers at once and wait for them with one shared
        deadline, so shutdown never takes N x timeout."""
        with self._lock:
            self._started = False
            procs = dict(self.procs)
            self.procs.clear()
            self._spec_used.clear()
            self._applied.clear()
            if self._state_timer is not None and self._state_timer.is_alive():
                self._state_timer.cancel()
                self._write_state()
        stop_all(procs.values(), deadline)
        for name in procs:
            log.info("stopped loopback %s", name)

    def _pipewire_recovered(self) -> None:
        log.info("PipeWire reachable again; retrying all helpers now")
        self._pw_down_logged = False
        self._retry_at.clear()
        self._failures.clear()
        self._apply_retry_at.clear()
        self.pw_recovered_count += 1

    def _terminate(self, name: str) -> None:
        proc = self.procs.pop(name, None)
        self._spec_used.pop(name, None)
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:  # noqa: BLE001
                pass
        self._applied.pop(name, None)
        self._invalidate_graph()
        log.info("stopped loopback %s", name)

    def _spec(self, name: str) -> LoopbackSpec:
        if name == VIRTUAL:
            return virtual_spec(self.cfg)
        return route_spec(self.cfg, self.cfg.routes[name], self.resolved)

    def _spawn(self, name: str) -> bool:
        spec = self._spec(name)
        try:
            self.procs[name] = self.backend.spawn_loopback(spec)
        except PwError as exc:
            self._failures[name] = self._failures.get(name, 0) + 1
            delay = min(30.0, 2.0 ** self._failures[name])
            self._retry_at[name] = self._clock() + delay
            log.error("spawn %s failed (%s); retry in %.0fs", name, exc, delay)
            return False
        self._spec_used[name] = spec
        self._retry_at.pop(name, None)
        self._applied.pop(name, None)
        self._spawned_at[name] = self._clock()
        self._invalidate_graph()
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
        """One supervisor pass: re-resolve devices, respawn dead or outdated
        loopbacks, apply pending volumes. Never raises."""
        with self._lock:
            if not self._started:
                return
            now = self._clock()
            self._invalidate_graph()  # the supervisor always looks at a fresh graph
            try:
                graph = self._fetch_graph()
                self.last_error = ""
                if self._pw_down_logged:
                    self._pipewire_recovered()
            except PwError as exc:
                if not self._pw_down_logged:
                    log.warning("pw-dump failed: %s (further failures are not logged until it recovers)", exc)
                    self._pw_down_logged = True
                self.last_error = f"cannot talk to PipeWire: {exc}"
                graph = None
            if graph is not None:
                self._refresh_resolution(graph)

            for name in [VIRTUAL, *self.cfg.routes]:
                proc = self.procs.get(name)
                if proc is not None and proc.poll() is None:
                    if self._failures.get(name) and now - self._spawned_at.get(name, now) > 30.0:
                        self._failures.pop(name, None)  # healthy for a while: forget crash history
                    # device resolved to a different node (replug, other port, first appearance)?
                    if graph is not None and name != VIRTUAL and self._spec_used.get(name) != self._spec(name):
                        log.info("route %s: target changed, restarting loopback", name)
                        self._terminate(name)
                        self._spawn(name)
                    continue
                if proc is not None:
                    err = str(getattr(proc, "stderr_tail", "") or "")[-300:]
                    log.warning("loopback %s exited with %s %s", name, proc.poll(), err)
                    self.procs.pop(name, None)
                    self._spec_used.pop(name, None)
                    if graph is None:
                        # PipeWire itself is gone: not the helper's fault, retry as soon as it is back
                        self._retry_at[name] = now + 1.0
                    else:
                        self._failures[name] = self._failures.get(name, 0) + 1
                        self._retry_at[name] = now + min(30.0, 2.0 ** self._failures[name])
                    self.last_error = f"{name} stopped unexpectedly, restarting"
                    continue
                if self._retry_at.get(name, 0.0) <= now:
                    self._spawn(name)
            if graph is None:
                return
            self._apply_all(graph)

    def _apply_all(self, graph: Graph) -> None:
        """Apply desired volumes with a per-pass budget of wpctl work, and give
        freshly spawned streams a short settle window so they are never audible
        at the wrong volume for a whole tick."""
        budget = self.apply_budget
        pending = [n for n in self.cfg.routes if not self._is_applied(n, graph)]
        fresh = [n for n in pending if self._clock() - self._spawned_at.get(n, -1e9) < 2.0]
        if fresh:
            # poll (bounded) for the new nodes, then apply right away
            deadline = self._clock() + 0.6
            while self._clock() < deadline:
                self._invalidate_graph()
                try:
                    graph = self._fetch_graph()
                except PwError:
                    return
                if all(graph.by_name(self.cfg.routes[n].out_node) is not None for n in fresh):
                    break
                self._sleep(0.1)
        for name in fresh + [n for n in pending if n not in fresh]:
            if budget <= 0:
                return
            if self._apply(name, graph):
                budget -= 1

    def _is_applied(self, name: str, graph: Graph) -> bool:
        node = graph.by_name(self.cfg.routes[name].out_node)
        if node is None:
            return True  # nothing to do until it exists
        want = self.desired[name]
        return self._applied.get(name) == (node.id, want.volume, want.mute)

    def run_forever(
        self,
        stop: threading.Event,
        interval: float = 1.0,
        on_tick: list[Callable[[], None]] | None = None,
    ) -> None:
        """Supervisor loop. Every callback runs in its own try/except so a
        failure in one subsystem (meters, watchdog) never stops the others."""
        while not stop.is_set():
            try:
                self.reconcile()
            except Exception:  # noqa: BLE001
                log.exception("supervisor pass failed")
            for cb in on_tick or []:
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    log.exception("tick callback %s failed", getattr(cb, "__name__", cb))
            stop.wait(interval)

    def reload(self, new_cfg: Config) -> dict[str, Any]:
        """Switch to a new config at runtime. Only loopbacks whose spec
        actually changed are restarted; runtime volumes of unchanged routes
        are kept unless their config default changed."""
        with self._lock:
            graph = self._graph_or_empty()
            new_resolved = resolve_devices(new_cfg, graph)
            new_specs = {n: route_spec(new_cfg, r, new_resolved) for n, r in new_cfg.routes.items()}
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
                if name not in new_specs or self._spec_used.get(name) != new_specs[name]:
                    self._terminate(name)

            new_cfg.path = new_cfg.path or self.cfg.path
            self.cfg = new_cfg
            self.desired = desired
            self.resolved = new_resolved
            for stale in set(self._failures) - set(new_cfg.routes) - {VIRTUAL}:
                self._failures.pop(stale, None)
                self._retry_at.pop(stale, None)
            self._save_state()

            if self._started:
                if VIRTUAL not in self.procs:
                    self._spawn(VIRTUAL)  # routes to it link as soon as it exists; no blocking wait here
                for name in self.cfg.routes:
                    if name not in self.procs:
                        self._spawn(name)
                self.reconcile()
            log.info("config reloaded: %d routes, %d presets", len(self.cfg.routes), len(self.cfg.presets))
            return self.status()

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
        now = self._clock()
        if self._apply_retry_at.get(name, 0.0) > now:
            return False  # wpctl failed recently; do not stall the supervisor on it again
        try:
            self.backend.set_volume(node.id, want.volume)
            self.backend.set_mute(node.id, want.mute)
            cap = graph.by_name(route.in_node)
            if cap is not None and ((cap.volume is not None and abs(cap.volume - 1.0) > 0.01) or cap.mute):
                # the capture side must always be neutral; only the playback side carries the route gain
                self.backend.set_volume(cap.id, 1.0)
                self.backend.set_mute(cap.id, False)
        except PwError as exc:
            log.error("apply %s: %s (retry in 5s)", name, exc)
            self._apply_retry_at[name] = now + 5.0
            return False
        self._apply_retry_at.pop(name, None)
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

    def fix_device(self, alias: str) -> dict[str, Any]:
        """Unmute a device and raise its system volume if it is at zero."""
        if alias not in self.cfg.devices:
            raise UnknownRoute(alias)
        graph = self._graph_or_empty()
        node = self._device_node(alias, graph)
        if node is None:
            raise RouterError(f"{human(self.cfg, alias)} is not connected")
        if node.mute:
            self.backend.set_mute(node.id, False)
        if node.volume is not None and node.volume < 0.05:
            self.backend.set_volume(node.id, 1.0)
        return self.status()

    # ----------------------------------------------------------------- status

    def _device_node(self, alias: str, graph: Graph) -> Node | None:
        res = self.resolved.get(alias)
        if res is None or res.node is None:
            return None
        return graph.by_name(res.node)

    def _ref_node(self, ref: str, graph: Graph) -> Node | None:
        if ref == OBS_MIC:
            return graph.by_name(self.cfg.virtual.obs_mix_name)
        if ref in self.cfg.devices:
            return self._device_node(ref, graph)
        return graph.by_name(ref)

    def resolved_nodes(self) -> dict[str, str | None]:
        return {alias: res.node for alias, res in self.resolved.items()}

    def _label(self, alias: str) -> str:
        return human(self.cfg, alias)

    def _route_label(self, route: RouteConfig) -> str:
        return f"{self._label(route.source_ref)} → {self._label(route.sink_ref)}"

    def route_status(self, name: str, graph: Graph | None = None) -> dict[str, Any]:
        route = self._route(name)
        graph = graph or self._graph_or_empty()
        want = self.desired.get(name) or RouteState(route.volume, route.mute)
        src = self._ref_node(route.source_ref, graph)
        dst = self._ref_node(route.sink_ref, graph)
        cap = graph.by_name(route.in_node)
        play = graph.by_name(route.out_node)
        proc = self.procs.get(name)
        return {
            "name": name,
            "description": route.description,
            "label": self._route_label(route),
            "from": route.source_ref,
            "to": route.sink_ref,
            "from_node": src.name if src else None,
            "to_node": dst.name if dst else None,
            "volume": want.volume,
            "volume_db": cubic_to_db(want.volume),
            "mute": want.mute,
            "actual": {"volume": play.volume if play else None, "mute": play.mute if play else None},
            "running": proc is not None and proc.poll() is None,
            "source_present": src is not None,
            "sink_present": dst is not None,
            "connected": bool(cap and play and graph.has_input_link(cap.id) and graph.has_output_link(play.id)),
        }

    def device_status(self, alias: str, graph: Graph) -> dict[str, Any]:
        spec: DeviceSpec = self.cfg.devices[alias]
        res = self.resolved.get(alias, Resolved(None, False))
        node = graph.by_name(res.node) if res.node else None
        info = describe_node(node, graph) if node else None
        if spec.is_static:
            how = {"strategy": "name", "text": "Recognised by its fixed name in the audio system.", "port": info["port"] if info else ""}
        else:
            key = "device.serial" if "device.serial" in spec.match else "device.bus-path" if "device.bus-path" in spec.match else "match"
            how = {
                "strategy": {"device.serial": "serial", "device.bus-path": "port"}.get(key, "match"),
                "port": (info["port"] if info else "") or (self._port_from_match(spec.match)),
                "text": (
                    "Recognised by its serial number. Any USB port works."
                    if key == "device.serial"
                    else f"Recognised by the USB port it is plugged into ({self._port_from_match(spec.match)}). It must stay in that port."
                    if key == "device.bus-path"
                    else "Recognised by matching hardware properties."
                ),
            }
        usage = info["usage"] if info else {"exclusive": False, "owner": ""}
        return {
            "label": self._label(alias),
            "node": res.node,
            "present": res.present,
            "taken_by": usage["owner"] if usage["exclusive"] else None,
            "error": (info or {}).get("error") or None,
            "ambiguous": res.ambiguous,
            "friendly": info["friendly"] if info else None,
            "bus": info["bus"] if info else None,
            "identity": how,
            "match": spec.match,
        }

    @staticmethod
    def _port_from_match(match: dict[str, str]) -> str:
        from .pw import port_label

        return port_label(match.get("device.bus-path", ""))

    def status(self, graph: Graph | None = None) -> dict[str, Any]:
        graph = graph or self._graph_or_empty()
        vproc = self.procs.get(VIRTUAL)
        problems = self.problems(graph)
        return {
            "ok": not any(p["level"] == "error" for p in problems),
            "problems": problems,
            "virtual_mic": {
                "node": self.cfg.virtual.obs_mic_name,
                "description": self.cfg.virtual.obs_mic_description,
                "running": vproc is not None and vproc.poll() is None,
                "present": graph.by_name(self.cfg.virtual.obs_mic_name) is not None,
            },
            "devices": {alias: self.device_status(alias, graph) for alias in self.cfg.devices},
            "labels": dict(self.cfg.labels),
            "routes": {name: self.route_status(name, graph) for name in self.cfg.routes},
            "presets": sorted(self.cfg.presets),
        }

    # --------------------------------------------------------------- problems

    def problems(self, graph: Graph | None = None) -> list[dict[str, Any]]:
        """Plain-language list of what is wrong or risky right now.
        Each entry: level (error|warning|info), code, what, title, why, effect, fix."""
        graph = graph or self._graph_or_empty()
        out: list[dict[str, Any]] = []
        cfg = self.cfg

        def add(level: str, code: str, what: str, title: str, why: str = "", effect: str = "", fix: str = "", **extra: Any) -> None:
            out.append({"level": level, "code": code, "what": what, "title": title, "why": why, "effect": effect, "fix": fix, **extra})

        if self.config_error:
            add("error", "config_invalid", "config", "The settings file could not be read",
                self.config_error,
                "The router runs with whatever it could recover; some or all connections may be missing.",
                f"Run Setup in this page to write a fresh settings file, or fix {cfg.path} by hand and restart the service.")

        if not graph.nodes:
            add("error", "no_audio_system", "daemon", "The computer's audio system is not reachable",
                self.last_error or "PipeWire did not answer.",
                "Nothing can be routed until it is back.",
                "Log out and in again, or run: systemctl --user restart pipewire wireplumber")
            return out

        if graph.by_name(cfg.virtual.obs_mic_name) is None:
            add("error", "obs_mic_missing", "obs_mic", "The OBS microphone does not exist right now",
                "The virtual microphone is created by this router and it is currently being recreated.",
                "OBS records silence until it is back (a few seconds).",
                "Nothing to do unless it stays like this for a minute; then restart: systemctl --user restart tfcz-audio")

        if not cfg.routes and not self.config_error:
            add("warning", "no_routes", "routes", "No connections set up yet", "", "No sound goes anywhere.",
                "Use Setup to connect your headsets.")

        physical = physical_devices(graph)
        used_aliases = {a for r in cfg.routes.values() for a in (r.source_ref, r.sink_ref) if a in cfg.devices}
        for alias in sorted(used_aliases):
            spec = cfg.devices[alias]
            res = self.resolved.get(alias, Resolved(None, False))
            node = graph.by_name(res.node) if res.node else None
            routes_using = [self._route_label(r) for r in cfg.routes.values() if alias in (r.source_ref, r.sink_ref)]
            label = self._label(alias)
            if node is None:
                why = "The computer does not see this device: it may be unplugged, switched off, or it was replaced by another model."
                fix = "Plug it in or switch it on; it reconnects by itself. If it is a new device, assign it under Setup."
                if "device.bus-path" in spec.match:
                    port = self._port_from_match(spec.match)
                    twin = self._same_model_elsewhere(spec, physical)
                    if twin:
                        why = f"{label} is recognised by its USB port ({port}), and a device of that kind is now in {twin} instead."
                        fix = f"Move it back to {port}, or run Setup again to accept the new port."
                    else:
                        why = f"{label} is recognised by its USB port ({port}) and nothing is plugged in there."
                        fix = f"Plug it into {port}. Identical headsets without serial numbers can only be told apart by the port."
                add("error", "device_missing", alias, f"{label} is not connected", why,
                    "These connections are silent: " + ", ".join(routes_using), fix, routes=routes_using)
                continue
            info = describe_node(node, graph)
            usage = info["usage"]
            if usage["exclusive"]:
                owner = usage["owner"] or "another program"
                pretty = {"obs": "OBS", "obs64": "OBS"}.get(owner.lower(), owner)
                add("error", "device_taken", alias, f"{label} is taken over by {pretty}",
                    f"{pretty} opened the device directly, bypassing the computer's audio system. Only one program can do that, and it locks everyone else out.",
                    "Silent for all connections using it, and for the level bar. " + ("OBS still hears it, nobody else does." if pretty == "OBS" else ""),
                    f"In {pretty}, use a source that goes through the audio system: in OBS pick 'Audio Input Capture (PipeWire)' instead of 'ALSA Input Capture' for this device. Or close {pretty}.",
                    owner=owner)
            elif node.state == "error" or info["error"]:
                add("error", "device_error", alias, f"The audio system cannot use {label}",
                    f"The device reports an error: {info['error'] or 'unknown'}. Usually another program holds it, or the driver is stuck.",
                    "Silent for all connections using it.",
                    "Close other audio programs; unplug and replug the device; if it persists, restart the audio system: systemctl --user restart pipewire wireplumber")
            if res.ambiguous:
                add("warning", "device_ambiguous", alias, f"More than one device matches {label}",
                    f"{res.candidates} connected devices look the same to the computer.",
                    "The router picked one of them; it may be the wrong one.",
                    "Run Setup again while both devices are plugged in so they get told apart by USB port.")
            if node.mute:
                add("warning", "device_muted", alias, f"{label} is muted by the system",
                    "The device itself is muted in the computer's sound settings; this is separate from the switches on this page.",
                    "Everything from or to it is silent even though the arrows look fine.",
                    "Click Fix, or unmute it in the sound settings.", fixable=True)
            elif node.volume is not None and node.volume < 0.05:
                add("warning", "device_silent", alias, f"{label} is turned all the way down by the system",
                    "The device's own volume in the computer's sound settings is at 0.",
                    "Everything from or to it is nearly silent.",
                    "Click Fix to set it to 100 %, or raise it in the sound settings.", fixable=True)

        # routes to OBS
        obs_routes = [(n, r) for n, r in cfg.routes.items() if r.sink_ref == OBS_MIC]
        live_obs = [n for n, _ in obs_routes if n in self.desired and not self.desired[n].mute and self.desired[n].volume > 0]
        if cfg.routes and not obs_routes:
            add("warning", "obs_unconnected", "obs_mic", "Nothing is connected to the OBS stream",
                "No arrow points to the OBS stream.", "Your viewers hear no microphones.",
                "Add a connection from each headset microphone to the OBS stream, or run Setup.")
        elif obs_routes and not live_obs:
            add("warning", "obs_all_off", "obs_mic", "All connections to the OBS stream are switched off",
                "Every arrow to OBS is off or at 0 %.", "Your viewers hear no microphones.",
                "Switch on at least one microphone → OBS stream connection.")

        # risky combinations
        dev_of_node = {n["name"]: g for g in physical for n in g["inputs"] + g["outputs"]}
        for name, route in cfg.routes.items():
            proc = self.procs.get(name)
            if proc is None or proc.poll() is not None:
                add("warning", "route_restarting", name, f"Connection {self._route_label(route)} is restarting",
                    "Its helper process stopped and is being started again automatically.",
                    "A short interruption on this path.", "Nothing to do; if it repeats, check the log.")
            if route.sink_ref == OBS_MIC:
                continue
            src_node = self._ref_node(route.source_ref, graph)
            dst_node = self._ref_node(route.sink_ref, graph)
            if dst_node is not None:
                info = describe_node(dst_node, graph)
                if info["speakers"] and src_node is not None and not describe_node(src_node, graph)["hdmi_capture"]:
                    add("warning", "feedback_risk", name, f"{self._route_label(route)} sends a microphone to loudspeakers",
                        "The output looks like loudspeakers (TV/monitor/built-in), not headphones. The microphone can pick the sound up again.",
                        "Echo or a loud howling feedback tone is likely.",
                        "Use headphones as the output, or switch this connection off.")
                if info["bus"] == "Bluetooth":
                    add("info", "bluetooth_delay", name, f"{self._label(route.sink_ref)} is a Bluetooth device",
                        "Bluetooth audio arrives about 0.15 to 0.3 seconds late.",
                        "Fine for talking to each other; distracting if someone hears their own voice through it.",
                        "Prefer a USB headset for anyone who needs to hear themselves.")
            if src_node is not None and dst_node is not None:
                g1, g2 = dev_of_node.get(src_node.name), dev_of_node.get(dst_node.name)
                if g1 is not None and g1 is g2:
                    add("info", "sidetone", name, f"{self._route_label(route)}: this person hears their own voice",
                        "Microphone and headphones belong to the same headset.", "Some people like the sidetone, others find it distracting.",
                        "Switch it off or turn it down if it bothers them.")

        # identity hints: port-bound devices are worth knowing about (once per headset)
        seen_ports: dict[tuple[str, str], str] = {}
        for alias in sorted(used_aliases):
            spec = cfg.devices[alias]
            if "device.bus-path" in spec.match and self.resolved.get(alias, Resolved(None, False)).present:
                base, _, kind = alias.rpartition("_")
                group = base if kind in ("mic", "out") else alias
                key = (group, self._port_from_match(spec.match))
                if key in seen_ports:
                    continue
                seen_ports[key] = alias
                name = human(cfg, group) if kind in ("mic", "out") else self._label(alias)
                what = f"{name}'s headset" if kind in ("mic", "out") else name
                add("info", "port_bound", alias, f"{what} must stay in {key[1]}",
                    "It is recognised by its USB port because identical devices report no serial number.",
                    "If it is moved to another port it counts as missing.", "Label the plug and the port.")

        if self.last_error and not any(p["level"] == "error" for p in out):
            add("warning", "daemon", "daemon", self.last_error)
        order = {"error": 0, "warning": 1, "info": 2}
        out.sort(key=lambda p: order.get(p["level"], 9))
        return out

    def _same_model_elsewhere(self, spec: DeviceSpec, physical: list[dict[str, Any]]) -> str:
        """If a port-bound device is missing, look for the same kind of hardware
        in another port (the user probably moved the plug). Returns port label."""
        want_kind = spec.match.get("kind")
        for g in physical:
            nodes = g["inputs"] if want_kind == "input" else g["outputs"] if want_kind == "output" else g["inputs"] + g["outputs"]
            if not nodes or not g.get("port"):
                continue
            if g["identity"].get("strategy") == "port" and g["port"] != self._port_from_match(spec.match) and not self._port_in_use(g["port"]):
                return g["port"]
        return ""

    def _port_in_use(self, port: str) -> bool:
        from .pw import port_label

        return any(port_label(s.match.get("device.bus-path", "")) == port for s in self.cfg.devices.values())

    # ----------------------------------------------------------------- lists

    def hardware(self) -> list[dict[str, Any]]:
        graph = self._graph_or_empty()
        groups = physical_devices(graph)
        assigned = {res.node: alias for alias, res in self.resolved.items() if res.node}
        for g in groups:
            for n in g["inputs"] + g["outputs"]:
                n["assigned_to"] = assigned.get(n["name"])
                n["assigned_label"] = self._label(assigned[n["name"]]) if n["name"] in assigned else None
        return groups

    def list_presets(self) -> dict[str, Any]:
        return {
            name: {r: {k: v for k, v in asdict(e).items() if v is not None} for r, e in entries.items()}
            for name, entries in self.cfg.presets.items()
        }

    def devices(self) -> list[dict[str, Any]]:
        graph = self._graph_or_empty()
        return [describe_node(n, graph) for n in graph.audio_devices()]

    def identity_of_node(self, node_name: str) -> dict[str, Any]:
        graph = self._graph_or_empty()
        node = graph.by_name(node_name)
        if node is None:
            raise RouterError(f"{node_name} is not connected")
        return identity_for(node, graph)
