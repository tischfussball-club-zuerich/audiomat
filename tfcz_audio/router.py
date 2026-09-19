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
    node_identity_props,
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


class GraphBusy(PwError):
    """Somebody else is reading the graph and no recent picture is at hand.

    A separate error because it means "try again in a moment", not "the audio
    system is broken" -- the difference between a quiet pass and a red card on
    the page.
    """


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
        candidates = len(nodes)
        if len(nodes) > 1 and spec.prefer:
            # several devices look the same (identical model, shared "serial"):
            # fall back on what was recorded at setup time, e.g. the USB port
            preferred = [n for n in nodes if all(node_identity_props(n, graph).get(k) == v for k, v in spec.prefer.items())]
            if len(preferred) == 1:
                nodes = preferred
        if nodes:
            out[alias] = Resolved(nodes[0].name, True, ambiguous=len(nodes) > 1, candidates=candidates)
        else:
            out[alias] = Resolved(None, False)
    return out


def _target(cfg: Config, ref: str, resolved: dict[str, Resolved]) -> str:
    if ref in cfg.virtual.outputs:
        return cfg.virtual.outputs[ref].mix_name
    if ref == OBS_MIC:
        return cfg.virtual.primary().mix_name
    if ref in cfg.devices:
        node = resolved.get(ref, Resolved(None, False)).node
        return node or f"{UNRESOLVED_PREFIX}{ref}"
    return ref  # raw node name


def route_spec(cfg: Config, route: RouteConfig, resolved: dict[str, Resolved]) -> LoopbackSpec:
    common: dict[str, Any] = {
        "node.dont-fallback": True,
        "node.dont-reconnect": False,
    }
    if cfg.audio.latency != "auto":
        # a request here lowers the buffer size for the entire graph, not just
        # for this stream; "auto" leaves that decision to PipeWire
        common["node.latency"] = cfg.audio.latency

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


def virtual_spec(cfg: Config, key: str = OBS_MIC) -> LoopbackSpec:
    """The loopback behind one microphone for OBS: a sink to play into and a
    source for OBS to record from."""
    out = cfg.virtual.outputs.get(key) or cfg.virtual.primary()
    position = ["FL", "FR"] if cfg.audio.channels == 2 else ["MONO"]
    capture = {
        "media.class": "Audio/Sink",
        "node.name": out.mix_name,
        "node.description": f"{out.description} (mix bus)",
        "audio.position": position,
        "media.role": out.mix_name,
        "application.id": out.mix_name,
        "application.name": out.mix_name,
    }
    playback = {
        "media.class": "Audio/Source",
        "node.name": out.mic_name,
        "node.description": out.description,
        "audio.position": position,
        "media.role": out.mic_name,
        "application.id": out.mic_name,
        "application.name": out.mic_name,
    }
    if cfg.audio.latency != "auto":
        capture["node.latency"] = cfg.audio.latency
        playback["node.latency"] = cfg.audio.latency
    name = "tfcz.virtual" if out.key == OBS_MIC else f"tfcz.virtual.{out.key}"
    return LoopbackSpec(name=name, capture_props=capture, playback_props=playback, channels=cfg.audio.channels)


def virtual_names(cfg: Config) -> list[str]:
    """Supervisor names of the virtual microphones, one per output.

    The first one keeps the historical name so nothing that remembers it -- a
    saved state file, a log -- has to be migrated.
    """
    return [VIRTUAL if key == OBS_MIC else f"{VIRTUAL}:{key}" for key in cfg.virtual.outputs]


def virtual_key(name: str) -> str:
    """The config key behind a supervisor name, or "" if it is a route."""
    if name == VIRTUAL:
        return OBS_MIC
    return name.split(":", 1)[1] if name.startswith(f"{VIRTUAL}:") else ""


def is_virtual(name: str) -> bool:
    return name == VIRTUAL or name.startswith(f"{VIRTUAL}:")


ORIGIN_VISITS = 20000  # nodes the "where does this come from" walk may look at per analysis


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
        self._graph_error: tuple[float, str] | None = None  # negative cache: do not re-run pw-dump for every caller while it fails
        self._graph_fetch_lock = threading.Lock()  # concurrent cache misses share one pw-dump
        self._last_graph: tuple[float, Graph] | None = None  # the most recent picture, whatever the cache says
        self.graph_wait_limit = 1.5  # how long a supervisor pass queues behind someone else's read
        self.stale_graph_limit = 5.0  # older than this, no picture is better than a wrong one
        self._apply_retry_at: dict[str, float] = {}  # per-route backoff after a failed wpctl call
        self._drift_retry_at: dict[str, float] = {}  # per-route pause after correcting an external volume change
        self._safety_muted: set[str] = set()  # routes muted because their stream is linked to the wrong device
        self._virtual_fix_at = 0.0
        self._drift_count: dict[str, int] = {}  # consecutive external volume changes per route ("virtual" for the OBS nodes)
        self._unlinked_since: dict[str, float] = {}
        self._relink_at: dict[str, float] = {}
        self._relink_attempts: dict[str, int] = {}
        # Hard bounds so one supervisor pass can never approach the systemd
        # watchdog, no matter how slow PipeWire answers.
        self.max_pass_seconds = 6.0  # wall clock for volume/level work in one pass
        self.max_restarts_per_pass = 3  # recycles (terminate+spawn) per pass; terminating is the slow part
        self.relink_after = 8.0  # seconds a present-but-unlinked route may wait before its loopback is recycled
        self.drift_alert_after = 5  # consecutive external volume changes before we report a conflict
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
        # valid JSON of the wrong shape is as likely as invalid JSON, and it
        # must not keep the daemon from starting: without sound, nobody can
        # even reach the page that would explain it
        if not isinstance(data, dict):
            log.warning("ignoring state file %s: expected an object, found %s", path, type(data).__name__)
            return
        routes = data.get("routes")
        if not isinstance(routes, dict):
            if routes is not None:
                log.warning("ignoring the routes in %s: expected an object", path)
            return
        for name, st in routes.items():
            if name in self.desired and isinstance(st, dict):
                try:
                    vol = float(st.get("volume", self.desired[name].volume))
                    mute = bool(st.get("mute", self.desired[name].mute))
                except (TypeError, ValueError, OverflowError):
                    continue
                if vol != vol or vol in (float("inf"), float("-inf")):  # NaN and infinity
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

    def _fetch_graph(self, wait: float | None = None) -> Graph:
        """Graph with a short cache so UI polling does not multiply pw-dump calls.

        ``wait`` bounds how long to queue behind a read someone else started.
        The supervisor uses it: on a busy machine pw-dump takes a second or
        two, and a page that asks for the whole graph must never be the reason
        a dead loopback is restarted later than it could have been. Past the
        bound it works from the last picture, which is at most
        ``stale_graph_limit`` seconds old, or reports PipeWire as unreachable
        -- both of which it already handles.
        """
        now = self._clock()
        cached = self._graph_cache
        if cached is not None and 0.0 <= now - cached[0] < self.graph_cache_ttl:
            return cached[1]
        failed = self._graph_error
        if failed is not None and 0.0 <= now - failed[0] < 1.0:
            # a failing pw-dump costs a full timeout; do not let every HTTP poll
            # start its own while PipeWire is wedged
            raise PwError(failed[1])
        if wait is None:
            self._graph_fetch_lock.acquire()
        elif not self._graph_fetch_lock.acquire(timeout=wait):
            # _last_graph, not _graph_cache: the supervisor clears the cache
            # before every pass, so the cache is exactly what it does not have
            last = self._last_graph
            age = self._clock() - last[0] if last is not None else None
            if last is not None and 0.0 <= age < self.stale_graph_limit:
                log.debug("another pw-dump is still running; working from a picture %.1fs old", age)
                return last[1]
            raise GraphBusy("another read of the audio graph is still running")
        try:
            cached = self._graph_cache
            if cached is not None and 0.0 <= self._clock() - cached[0] < self.graph_cache_ttl:
                return cached[1]
            try:
                graph = self.backend.graph()
            except PwError as exc:
                self._graph_error = (self._clock(), str(exc))
                raise
            except Exception as exc:  # noqa: BLE001 - a parse problem must behave like an unreachable PipeWire
                self._graph_error = (self._clock(), f"cannot read the PipeWire graph: {exc}")
                raise PwError(self._graph_error[1]) from exc
            self._graph_error = None
            self._graph_cache = (self._clock(), graph)
            self._last_graph = self._graph_cache  # survives _invalidate_graph()
            return graph
        finally:
            self._graph_fetch_lock.release()

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

    def refresh_devices(self, graph: Graph | None = None) -> Graph:
        """Resolve the configured devices against the current graph and nothing
        else. Read-only on purpose: one-off callers like the command line want
        to know what is connected without starting a single stream."""
        graph = graph if graph is not None else self._graph_or_empty()
        self._refresh_resolution(graph)
        return graph

    def start(self) -> None:
        with self._lock:
            self._started = True
            self._refresh_resolution(self._graph_or_empty())
            for name in virtual_names(self.cfg):
                self._spawn(name)
            for out in self.cfg.virtual.outputs.values():
                self._wait_for_node(out.mix_name)
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
        self._drift_retry_at.clear()
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

    PER_NAME_STATE = ("_spec_used", "_applied", "_failures", "_retry_at", "_spawned_at", "_apply_retry_at",
                      "_drift_retry_at", "_drift_count", "_unlinked_since", "_relink_at", "_relink_attempts")

    def _forget(self, keep: set[str]) -> None:
        """Drop everything remembered about names that no longer exist."""
        keep = keep | {"virtual"}  # the shared drift counter of the OBS nodes
        for attribute in self.PER_NAME_STATE:
            store = getattr(self, attribute)
            for stale in [k for k in store if k not in keep]:
                store.pop(stale, None)

    def _spec(self, name: str) -> LoopbackSpec:
        if is_virtual(name):
            return virtual_spec(self.cfg, virtual_key(name))
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
                # bounded: a slow read started by the page must not hold up a
                # restart while the sound is out
                graph = self._fetch_graph(wait=self.graph_wait_limit)
                self.last_error = ""
                if self._pw_down_logged:
                    self._pipewire_recovered()
            except GraphBusy:
                # somebody else is reading and there is no recent picture. That
                # is a busy moment, not a broken audio system: skip the pass
                # quietly instead of putting an error on the page.
                log.debug("skipping this pass: the audio graph is being read elsewhere")
                return
            except PwError as exc:
                if not self._pw_down_logged:
                    log.warning("pw-dump failed: %s (further failures are not logged until it recovers)", exc)
                    self._pw_down_logged = True
                self.last_error = f"cannot talk to PipeWire: {exc}"
                graph = None
            if graph is not None:
                self._refresh_resolution(graph)

            restarts = self.max_restarts_per_pass
            for name in [*virtual_names(self.cfg), *self.cfg.routes]:
                proc = self.procs.get(name)
                if proc is not None and proc.poll() is None:
                    if self._failures.get(name) and now - self._spawned_at.get(name, now) > 30.0:
                        self._failures.pop(name, None)  # healthy for a while: forget crash history
                    # device resolved to a different node (replug, other port, first appearance)?
                    if graph is not None and not is_virtual(name) and self._spec_used.get(name) != self._spec(name):
                        if restarts <= 0:
                            continue  # next pass (1 s later) takes the rest; keeps this pass short
                        restarts -= 1
                        log.info("route %s: target changed, restarting loopback", name)
                        self._terminate(name)
                        self._spawn(name)
                    continue
                if proc is not None:
                    err = str(getattr(proc, "stderr_tail", "") or "")[-300:]
                    # while PipeWire is unreachable every helper exits immediately; say it once
                    (log.debug if graph is None else log.warning)("loopback %s exited with %s %s", name, proc.poll(), err)
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
                if graph is None:
                    continue  # no point starting helpers that cannot connect; _pipewire_recovered() retries at once
                if self._retry_at.get(name, 0.0) <= now:
                    self._spawn(name)  # plain spawn is cheap (fork+exec); only recycling is budgeted
            if graph is None:
                return
            pass_end = now + self.max_pass_seconds
            self._update_safety(graph, now)
            self._relink_watchdog(graph, now, restarts)
            self._fix_default_sink(graph)
            self._enforce_virtual_levels(graph, pass_end)
            self._apply_all(graph, pass_end)

    def _relink_watchdog(self, graph: Graph, now: float, restarts: int) -> None:
        """A route whose devices and streams all exist but which is not linked
        (or linked to the wrong node) is normally waiting for the session
        manager. If that has not resolved after `relink_after` seconds, recycle
        the loopback: a fresh stream is linked from scratch. Attempts are
        limited so a genuinely wrong setup is reported instead of restarted
        forever."""
        for name in self.cfg.routes:
            proc = self.procs.get(name)
            route = self.cfg.routes[name]
            src = self._ref_node(route.source_ref, graph)
            dst = self._ref_node(route.sink_ref, graph)
            cap = graph.by_name(route.in_node)
            play = graph.by_name(route.out_node)
            if proc is None or proc.poll() is not None or src is None or dst is None or cap is None or play is None:
                self._unlinked_since.pop(name, None)  # nothing to heal while something is missing
                continue
            if graph.linked(src.id, cap.id) and graph.linked(play.id, dst.id) and not self._misrouted(name, graph):
                self._unlinked_since.pop(name, None)
                self._relink_attempts.pop(name, None)
                self._relink_at.pop(name, None)
                continue
            since = self._unlinked_since.setdefault(name, now)
            if now - since < self.relink_after or now < self._relink_at.get(name, 0.0) or restarts <= 0:
                continue
            attempts = self._relink_attempts.get(name, 0)
            if attempts >= 3:
                continue  # reported as a problem instead; restarting clearly does not help
            self._relink_attempts[name] = attempts + 1
            self._relink_at[name] = now + 30.0
            self._unlinked_since[name] = now
            restarts -= 1
            log.warning("route %s has not been linked to its devices for %.0fs; recycling its loopback (attempt %d/3)", name, now - since, attempts + 1)
            if self._misrouted(name, graph):
                # a target the session manager remembered for this stream beats our
                # own property, and would survive the recycle; forget it first
                for node in (cap, play):
                    try:
                        self.backend.clear_stream_target(node.id)
                    except Exception:  # noqa: BLE001 - best effort, never fatal
                        log.debug("could not clear the remembered target of %s", node.name)
            self._terminate(name)
            self._spawn(name)

    def _misrouted(self, name: str, graph: Graph) -> list[str]:
        """Names of nodes a route's streams are linked to although they are not
        the configured devices. Covers WirePlumber falling back to a default
        device (dont-fallback ignored), remembered manual moves, and any
        accidental loop through our own virtual nodes."""
        route = self.cfg.routes[name]
        src = self._ref_node(route.source_ref, graph)
        dst = self._ref_node(route.sink_ref, graph)
        cap = graph.by_name(route.in_node)
        play = graph.by_name(route.out_node)
        wrong: list[str] = []
        if cap is not None:
            for peer in graph.peers_of_input(cap.id) - ({src.id} if src else set()):
                n = graph.nodes.get(peer)
                if n is not None:
                    wrong.append(n.description or n.name)
        if play is not None:
            for peer in graph.peers_of_output(play.id) - ({dst.id} if dst else set()):
                n = graph.nodes.get(peer)
                if n is not None:
                    wrong.append(n.description or n.name)
        return wrong

    def _update_safety(self, graph: Graph, now: float = 0.0) -> None:
        """Mute (at the stream) any route whose audio would go to or come
        from the wrong device. Wrong routing is worse than silence: it can
        leak a microphone or build a feedback loop through the OBS mic."""
        for name in self.cfg.routes:
            if self._spawned_at.get(name, -1e9) >= now:
                continue  # just (re)started in this pass: the graph we hold predates it
            wrong = self._misrouted(name, graph)
            if wrong and name not in self._safety_muted:
                log.error("route %s is linked to the wrong device(s) %s; muting it for safety", name, wrong)
                self._safety_muted.add(name)
                self._applied.pop(name, None)
            elif not wrong and name in self._safety_muted:
                log.info("route %s is linked correctly again; safety mute lifted", name)
                self._safety_muted.discard(name)
                self._applied.pop(name, None)
        for stale in list(self._safety_muted - set(self.cfg.routes)):
            self._safety_muted.discard(stale)

    def _fix_default_sink(self, graph: Graph) -> None:
        """If the computer's default output became our mix bus (typically because
        no headset was connected at the time), put it back on a real device.
        Otherwise every notification sound and any app monitoring the default
        output lands on the stream, and OBS monitoring can loop."""
        if not graph.defaults.get("default.audio.sink", "").startswith("tfcz."):
            return
        real = [
            n for n in graph.nodes.values()
            if n.media_class == "Audio/Sink" and not n.name.startswith("tfcz.")
        ]
        if not real:
            return  # nothing better exists yet; the problem list explains it
        target = sorted(real, key=lambda n: n.name)[0]
        log.warning("default output was %s; setting it back to %s", graph.defaults["default.audio.sink"], target.name)
        try:
            self.backend.set_default(target.id)
            graph.defaults["default.audio.sink"] = target.name
        except PwError as exc:
            log.error("cannot change the default output: %s", exc)

    def _enforce_virtual_levels(self, graph: Graph, pass_end: float = float("inf")) -> None:
        """The OBS mic and its mix bus belong to the daemon: keep them at
        unity and unmuted no matter what a mixer app or OBS did to them."""
        now = self._clock()
        if now < self._virtual_fix_at:
            return
        node_names = [n for out in self.cfg.virtual.outputs.values() for n in (out.mic_name, out.mix_name)]
        last_mic = node_names[0] if node_names else ""
        for node_name in node_names:
            if self._clock() >= pass_end:
                return
            node = graph.by_name(node_name)
            if node is None:
                continue
            if node.mute or (node.volume is not None and abs(node.volume - 1.0) > 0.01):
                count = self._drift_count.get("virtual", 0) + 1
                self._drift_count["virtual"] = count
                (log.warning if count == 1 else log.debug)(
                    "%s was changed externally (volume %.2f, mute %s); restoring unity (%d)", node_name, node.volume or 0, node.mute, count
                )
                try:
                    self.backend.set_volume(node.id, 1.0)
                    self.backend.set_mute(node.id, False)
                except PwError as exc:
                    log.error("cannot restore %s: %s", node_name, exc)
                self._virtual_fix_at = now + (30.0 if count >= self.drift_alert_after else 5.0)
            elif node_name == last_mic:
                self._drift_count.pop("virtual", None)

    def _apply_all(self, graph: Graph, pass_end: float = float("inf")) -> None:
        """Apply desired volumes with a per-pass budget of wpctl work, and give
        freshly spawned streams a short settle window so they are never audible
        at the wrong volume for a whole tick."""
        budget = self.apply_budget
        now = self._clock()
        # streams spawned in the last 2 s whose volume has not been applied yet:
        # their nodes may not have appeared; poll briefly so they are never
        # linked at the wrong volume for a whole tick
        fresh = [
            n for n in self.cfg.routes
            if now - self._spawned_at.get(n, -1e9) < 2.0 and n in self.procs and self._applied.get(n) is None
        ]
        if fresh:
            deadline = min(now + 0.6, pass_end)
            while True:
                if all(graph.by_name(self.cfg.routes[n].out_node) is not None for n in fresh):
                    break
                if self._clock() >= deadline:
                    break
                self._sleep(0.1)
                self._invalidate_graph()
                try:
                    graph = self._fetch_graph()
                except PwError:
                    return
        pending = [n for n in self.cfg.routes if not self._is_applied(n, graph)]
        # a fresh stream must never stay at the default level for a whole tick,
        # so the budget always covers all of them
        budget = max(budget, len(fresh))
        for name in fresh + [n for n in pending if n not in fresh]:
            if budget <= 0 or self._clock() >= pass_end:
                return
            if self._apply(name, graph, pass_end):
                budget -= 1

    def _is_applied(self, name: str, graph: Graph) -> bool:
        node = graph.by_name(self.cfg.routes[name].out_node)
        if node is None:
            return True  # nothing to do until it exists
        want = self.desired.get(name)
        if want is None:
            return True
        mute = want.mute or name in self._safety_muted
        if self._applied.get(name) != (node.id, want.volume, mute):
            return False
        # applied by us, but do the nodes still agree? (a mixer app or a restored
        # value can change either side; the capture side must stay neutral)
        cap = graph.by_name(self.cfg.routes[name].in_node)
        drift = (node.volume is not None and abs(node.volume - want.volume) > 0.005) or (node.mute is not None and bool(node.mute) != mute)
        if cap is not None and ((cap.volume is not None and abs(cap.volume - 1.0) > 0.01) or cap.mute):
            drift = True
        if not drift:
            self._drift_count.pop(name, None)
            return True
        if self._clock() < self._drift_retry_at.get(name, 0.0):
            return True  # correction is pending, do not spin on it
        count = self._drift_count.get(name, 0) + 1
        self._drift_count[name] = count
        # Something outside keeps changing this stream. Correct it, but back off
        # and report it instead of trading writes with the other program forever.
        (log.info if count == 1 else log.debug)(
            "route %s: volume drifted (node %.3f/%s, want %.3f/%s); re-applying (%d)", name, node.volume or 0, node.mute, want.volume, want.mute, count
        )
        if count == self.drift_alert_after:
            log.warning("route %s: something keeps changing this volume; correcting less often now", name)
        self._applied.pop(name, None)
        self._drift_retry_at[name] = self._clock() + (30.0 if count >= self.drift_alert_after else 5.0)
        return False

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
            # one microphone per output, so each is compared on its own: adding
            # a second one must not restart the first and cut its sound
            new_virtual = {name: virtual_spec(new_cfg, virtual_key(name)) for name in virtual_names(new_cfg)}

            desired: dict[str, RouteState] = {}
            for name, route in new_cfg.routes.items():
                old = self.cfg.routes.get(name)
                if old is not None and name in self.desired and (old.volume, old.mute) == (route.volume, route.mute):
                    desired[name] = self.desired[name]
                else:
                    desired[name] = RouteState(route.volume, route.mute)

            for name in list(self.procs):
                if is_virtual(name):
                    if name not in new_virtual or self._spec_used.get(name) != new_virtual[name]:
                        self._terminate(name)
                    continue
                if name not in new_specs or self._spec_used.get(name) != new_specs[name]:
                    self._terminate(name)

            new_cfg.path = new_cfg.path or self.cfg.path
            self.cfg = new_cfg
            self.desired = desired
            self.resolved = new_resolved
            # Every per-route note has to go when the route does. Two reasons:
            # a daemon that runs for weeks while an automation creates and
            # deletes routes would grow these dicts forever, and a route
            # created again under an old name would inherit the old entries --
            # including "its volume is already applied", which would leave the
            # fresh stream at whatever it started with.
            self._forget(set(new_cfg.routes) | set(new_virtual))
            self._save_state()

            if self._started:
                for name in virtual_names(self.cfg):
                    if name not in self.procs:
                        self._spawn(name)  # routes to it link as soon as it exists; no blocking wait here
                for name in self.cfg.routes:
                    if name not in self.procs:
                        self._spawn(name)
                self.reconcile()
            log.info("config reloaded: %d routes, %d presets", len(self.cfg.routes), len(self.cfg.presets))
            return self.status()

    # ---------------------------------------------------------------- control

    def _apply(self, name: str, graph: Graph, pass_end: float = float("inf")) -> bool:
        route = self.cfg.routes[name]
        proc = self.procs.get(name)
        if proc is None or proc.poll() is not None:
            return False  # its nodes are about to disappear; ids could be reused by something else
        node = graph.by_name(route.out_node)
        if node is None:
            return False
        want = self.desired[name]
        mute = want.mute or name in self._safety_muted
        key = (node.id, want.volume, mute)
        if self._applied.get(name) == key:
            return True
        now = self._clock()
        if self._apply_retry_at.get(name, 0.0) > now:
            return False  # wpctl failed recently; do not stall the supervisor on it again
        try:
            self.backend.set_volume(node.id, want.volume)
            if self._clock() >= pass_end:
                return False  # budget spent mid-route; the next pass finishes it
            self.backend.set_mute(node.id, mute)
            cap = graph.by_name(route.in_node)
            if cap is not None and self._clock() < pass_end and ((cap.volume is not None and abs(cap.volume - 1.0) > 0.01) or cap.mute):
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
                    raise RouterError(f"Die Lautstärke muss zwischen 0 und {MAX_VOLUME} liegen")
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
            raise RouterError(f"{human(self.cfg, alias)} ist nicht angeschlossen")
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
        if ref in self.cfg.virtual.outputs:
            return graph.by_name(self.cfg.virtual.outputs[ref].mix_name)
        if ref == OBS_MIC:
            return graph.by_name(self.cfg.virtual.primary().mix_name)
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
        # linked to the RIGHT devices? (a manual move in pavucontrol is remembered by WirePlumber)
        in_ok = bool(cap and src and graph.linked(src.id, cap.id))
        out_ok = bool(play and dst and graph.linked(play.id, dst.id))
        wrong = self._misrouted(name, graph)
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
            "connected": in_ok and out_ok and not wrong,
            "misrouted_to": wrong,
            "safety_muted": name in self._safety_muted,
        }

    def device_status(self, alias: str, graph: Graph) -> dict[str, Any]:
        spec: DeviceSpec = self.cfg.devices[alias]
        res = self.resolved.get(alias, Resolved(None, False))
        node = graph.by_name(res.node) if res.node else None
        info = describe_node(node, graph) if node else None
        if spec.is_static:
            how = {"strategy": "name", "text": "Wird am festen Namen im Tonsystem erkannt.", "port": info["port"] if info else ""}
        else:
            key = "device.serial" if "device.serial" in spec.match else "device.bus-path" if "device.bus-path" in spec.match else "match"
            how = {
                "strategy": {"device.serial": "serial", "device.bus-path": "port"}.get(key, "match"),
                "port": (info["port"] if info else "") or (self._port_from_match(spec.match)),
                "text": (
                    "Wird an der Seriennummer erkannt. Jeder USB-Anschluss funktioniert."
                    if key == "device.serial"
                    else f"Wird am USB-Anschluss erkannt, in dem es steckt ({self._port_from_match(spec.match)}). Es muss dort stecken bleiben."
                    if key == "device.bus-path"
                    else "Wird über passende Hardware-Eigenschaften erkannt."
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

    def virtual_mics(self, graph: Graph) -> list[dict[str, Any]]:
        from .meters import obs_meter_key

        mics = []
        for name in virtual_names(self.cfg):
            out = self.cfg.virtual.outputs[virtual_key(name)]
            proc = self.procs.get(name)
            mics.append({
                "key": out.key,
                "meter": obs_meter_key(out.key),
                "node": out.mic_name,
                "description": out.description,
                "running": proc is not None and proc.poll() is None,
                "present": graph.by_name(out.mic_name) is not None,
                "feeders": sorted(r.name for r in self.cfg.routes.values() if r.sink_ref == out.key),
            })
        return mics

    def status(self, graph: Graph | None = None) -> dict[str, Any]:
        graph = graph or self._graph_or_empty()
        problems = self.problems(graph)
        mics = self.virtual_mics(graph)
        return {
            "ok": not any(p["level"] == "error" for p in problems),
            "problems": problems,
            # the first one keeps its old shape: an automation may read it
            "virtual_mic": {k: v for k, v in mics[0].items() if k != "feeders"} if mics else {},
            "virtual_mics": mics,
            "separate_obs": self.cfg.virtual.separate,
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
            add("error", "config_invalid", "config", "Die Einstellungsdatei konnte nicht gelesen werden",
                self.config_error,
                "Der Router läuft mit dem, was zu retten war; einzelne oder alle Verbindungen können fehlen.",
                f"Richte die Geräte auf dieser Seite neu ein, das schreibt eine frische Einstellungsdatei. Oder repariere {cfg.path} von Hand und starte den Dienst neu.")

        if not graph.nodes:
            add("error", "no_audio_system", "daemon", "Das Tonsystem des Computers antwortet nicht",
                self.last_error or "PipeWire hat nicht geantwortet.",
                "Solange es weg ist, kann nichts geleitet werden.",
                "Ab- und wieder anmelden, oder: systemctl --user restart pipewire wireplumber")
            return out

        if graph.nodes and not graph.has_default_metadata and "WirePlumber" not in graph.clients:
            add("error", "no_session_manager", "daemon", "Die Tonverwaltung läuft nicht",
                "PipeWire antwortet, aber WirePlumber fehlt. Das ist der Teil, der Ströme mit Geräten verbindet. Ohne ihn wird nichts verbunden, egal was dieser Router tut.",
                "Alle Verbindungen bleiben stumm.",
                "systemctl --user restart wireplumber   (danach: systemctl --user restart tfcz-audio). Stirbt er immer wieder, zeigt 'journalctl --user -u wireplumber' den Fehler, meist in einer Konfigurationsregel.")

        missing_mics = [mic for mic in cfg.virtual.outputs.values() if graph.by_name(mic.mic_name) is None]
        if missing_mics:
            which = "Das OBS-Mikrofon" if len(cfg.virtual.outputs) == 1 else \
                "«" + "», «".join(mic.description for mic in missing_mics) + "»"
            add("error", "obs_mic_missing", OBS_MIC, f"{which} gibt es gerade nicht",
                "Dieses virtuelle Mikrofon wird vom Router erzeugt und im Moment neu angelegt.",
                "OBS nimmt Stille auf, bis es wieder da ist (ein paar Sekunden).",
                "Nichts zu tun, ausser es bleibt eine Minute lang so; dann: systemctl --user restart tfcz-audio")

        if not cfg.routes and not self.config_error:
            add("warning", "no_routes", "routes", "Noch keine Verbindungen eingerichtet", "", "Es geht kein Ton irgendwohin.",
                "Nutze «Geräte einrichten», um die Headsets zu verbinden.")

        physical = physical_devices(graph)
        used_aliases = {a for r in cfg.routes.values() for a in (r.source_ref, r.sink_ref) if a in cfg.devices}
        for alias in sorted(used_aliases):
            spec = cfg.devices[alias]
            res = self.resolved.get(alias, Resolved(None, False))
            node = graph.by_name(res.node) if res.node else None
            routes_using = [self._route_label(r) for r in cfg.routes.values() if alias in (r.source_ref, r.sink_ref)]
            label = self._label(alias)
            if node is None:
                why = "Der Computer sieht dieses Gerät nicht: vielleicht ausgesteckt, ausgeschaltet oder durch ein anderes Modell ersetzt."
                fix = "Einstecken oder einschalten; es verbindet sich von selbst wieder. Ist es ein neues Gerät, richte es unter «Geräte einrichten» zu."
                if "device.bus-path" in spec.match:
                    port = self._port_from_match(spec.match)
                    twin = self._same_model_elsewhere(spec, physical)
                    if twin:
                        why = f"{label} wird an seinem USB-Anschluss erkannt ({port}), und ein solches Gerät steckt jetzt stattdessen in {twin}."
                        fix = f"Zurück in {port} stecken, oder die Geräte neu einrichten, damit der neue Anschluss übernommen wird."
                    else:
                        why = f"{label} wird an seinem USB-Anschluss erkannt ({port}), und dort steckt nichts."
                        fix = f"In {port} einstecken. Gleiche Headsets ohne Seriennummer lassen sich nur am Anschluss unterscheiden."
                add("error", "device_missing", alias, f"{label} ist nicht angeschlossen", why,
                    "Diese Verbindungen sind stumm: " + ", ".join(routes_using), fix, routes=routes_using)
                continue
            info = describe_node(node, graph)
            usage = info["usage"]
            if usage["exclusive"]:
                owner = usage["owner"] or "ein anderes Programm"
                pretty = {"obs": "OBS", "obs64": "OBS"}.get(owner.lower(), owner)
                add("error", "device_taken", alias, f"{label} ist von {pretty} übernommen",
                    f"{pretty} hat das Gerät direkt geöffnet und dabei das Tonsystem umgangen. Das kann nur ein Programm gleichzeitig, und es sperrt alle anderen aus.",
                    "Stumm für alle Verbindungen, die es nutzen, und für den Pegelbalken. " + ("OBS hört es weiterhin, sonst niemand." if pretty == "OBS" else ""),
                    f"Nutze in {pretty} eine Quelle, die über das Tonsystem geht: in OBS «Audio Input Capture (PipeWire)» statt «ALSA Input Capture» für dieses Gerät. Oder schliesse {pretty}.",
                    owner=owner)
            elif node.state == "error" or info["error"]:
                add("error", "device_error", alias, f"Das Tonsystem kann {label} nicht benutzen",
                    f"Das Gerät meldet einen Fehler: {info['error'] or 'unbekannt'}. Meist hält es ein anderes Programm fest, oder der Treiber hängt.",
                    "Stumm für alle Verbindungen, die es nutzen.",
                    "Andere Tonprogramme schliessen; Gerät aus- und wieder einstecken; hilft das nicht: systemctl --user restart pipewire wireplumber")
            if res.ambiguous:
                add("warning", "device_ambiguous", alias, f"Mehr als ein Gerät passt auf {label}",
                    f"{res.candidates} angeschlossene Geräte sehen für den Computer gleich aus.",
                    "Der Router hat eines davon genommen; es kann das falsche sein.",
                    "Richte die Geräte neu ein, solange beide eingesteckt sind, dann werden sie am USB-Anschluss unterschieden.")
            if node.mute:
                add("warning", "device_muted", alias, f"{label} ist im System stummgeschaltet",
                    "Das Gerät selbst ist in den Toneinstellungen des Computers stumm; das ist unabhängig von den Schaltern auf dieser Seite.",
                    "Alles von und zu ihm ist still, obwohl die Pfeile in Ordnung aussehen.",
                    "Auf «Beheben» klicken, oder es in den Toneinstellungen laut schalten.", fixable=True)
            elif node.volume is not None and node.volume < 0.05:
                add("warning", "device_silent", alias, f"{label} ist im System ganz heruntergedreht",
                    "Die eigene Lautstärke des Geräts steht in den Toneinstellungen des Computers auf 0.",
                    "Alles von und zu ihm ist fast still.",
                    "Auf «Beheben» klicken für 100 %, oder in den Toneinstellungen hochdrehen.", fixable=True)

        # system default output/input pointing at our virtual nodes
        for key, label in (("default.audio.sink", "output"), ("default.audio.source", "input")):
            target = graph.defaults.get(key, "")
            if target.startswith("tfcz."):
                if label == "output":
                    add("error", "default_into_obs", "obs_mic", "Systemtöne landen im OBS-Mikrofon",
                        "Der Standard-Ausgang des Computers ist die Mischspur des Routers, vermutlich weil gerade kein anderer Ausgang angeschlossen ist. Hinweistöne, Browserton und so weiter gehen damit auf den Stream.",
                        "Deine Zuschauer hören die Töne des Computers über den Mikrofonkanal.",
                        "Headsets einstecken, oder in den Toneinstellungen einen anderen Ausgang wählen (wpctl set-default <id>).")
                else:
                    add("warning", "default_source_is_obs", "obs_mic", "Das Standard-Mikrofon des Computers ist das OBS-Mikrofon",
                        "Programme, die einfach «das Standard-Mikrofon» nehmen, nehmen jetzt die Mischung des Routers auf.",
                        "Meist harmlos; OBS sollte ohnehin «TFCZ OBS Mic» ausdrücklich auswählen.",
                        "Wähle in den Toneinstellungen ein echtes Mikrofon als Standard, falls ein anderes Programm es braucht.")

        # streams linked to the wrong device (remembered manual moves)
        for name, route in cfg.routes.items():
            st = self.route_status(name, graph)
            unlinked_for = self._clock() - self._unlinked_since.get(name, self._clock())
            if (
                self._relink_attempts.get(name, 0) < 3
                and not st["misrouted_to"]
                and st["source_present"]
                and st["sink_present"]
                and not st["connected"]
                and unlinked_for >= self.relink_after
            ):
                add("warning", "still_connecting", name, f"Verbindung {self._route_label(route)} ist noch nicht verbunden",
                    "Beide Geräte sind da, aber das Tonsystem hat den Strom des Routers noch nicht mit ihnen verbunden.",
                    "Diese Verbindung ist solange stumm.",
                    "Der Router versucht es von selbst weiter. Bleibt es so: systemctl --user restart wireplumber tfcz-audio")
            if self._relink_attempts.get(name, 0) >= 3 and not st["misrouted_to"] and st["source_present"] and st["sink_present"]:
                add("error", "not_linking", name, f"Verbindung {self._route_label(route)} kommt nicht zustande",
                    "Beide Geräte sind da, aber das Tonsystem verbindet den Strom des Routers auch nach mehreren Versuchen nicht mit ihnen.",
                    "Diese Verbindung ist stumm.",
                    "Tonsystem neu starten: systemctl --user restart pipewire wireplumber tfcz-audio. Hilft das nicht, zeigen 'journalctl --user -u tfcz-audio' und 'pw-link -l' mehr.")
            if st["misrouted_to"]:
                add("error", "misrouted", name, f"Verbindung {self._route_label(route)} hängt am falschen Gerät",
                    "Das Tonsystem hat diesen Strom mit " + ", ".join(st["misrouted_to"]) + " verbunden statt mit dem gewählten Gerät. Meist eine von Hand gemerkte Umstellung aus einem Mischpult-Programm, oder ein Ausweichen, weil das Gerät fehlte.",
                    "Der Router hat diese Verbindung zur Sicherheit stummgeschaltet: falsches Leiten kann ein Mikrofon irgendwohin tragen oder das OBS-Mikrofon in sich selbst zurückführen.",
                    "Der Router vergisst das gemerkte Ziel und baut die Verbindung selbst neu auf. Bleibt es falsch: systemctl --user restart wireplumber, und zur Not die gemerkten Zuordnungen löschen mit 'rm ~/.local/state/wireplumber/restore-stream', danach 'systemctl --user restart wireplumber tfcz-audio'.")

        if self._drift_count.get("virtual", 0) >= self.drift_alert_after:
            add("warning", "virtual_fought", "obs_mic", "Ein anderes Programm verstellt dauernd den Pegel des OBS-Mikrofons",
                "Etwas ausserhalb dieses Routers schaltet das OBS-Mikrofon immer wieder stumm oder dreht es herunter. Oft ein Mischpult-Programm, oder OBS selbst mit «Monitor and Output» auf einer Quelle.",
                "Der Pegel springt; der Router stellt ihn zurück, aber man hört es womöglich auf dem Stream.",
                "Finde das Programm, das es tut (meist ein Toneinstellungs- oder Mischpultfenster), und lass «TFCZ OBS Mic» in Ruhe; der Router hält es auf 100 %.")
        for name in sorted(n for n, c in self._drift_count.items() if n != "virtual" and c >= self.drift_alert_after):
            if name in cfg.routes:
                add("warning", "volume_fought", name, f"Ein anderes Programm verstellt dauernd die Lautstärke von {self._route_label(cfg.routes[name])}",
                    "Etwas ausserhalb dieses Routers ändert die Lautstärke dieser Verbindung immer wieder.",
                    "Was du hier einstellst, bleibt nicht stehen.",
                    "Schliesse Mischpult-Programme, die «TFCZ»-Ströme anfassen. Der Router korrigiert weiter, aber seltener.")

        # routes to OBS, checked per microphone: with one per person, a single
        # dead one is exactly what nobody notices until the stream is out
        obs_routes = [(n, r) for n, r in cfg.routes.items() if r.sink_ref in cfg.virtual.outputs]
        live_obs = [n for n, _ in obs_routes if n in self.desired and not self.desired[n].mute and self.desired[n].volume > 0]
        if cfg.routes and not obs_routes:
            add("warning", "obs_unconnected", OBS_MIC, "Nichts ist mit dem OBS-Stream verbunden",
                "Kein Pfeil zeigt auf den OBS-Stream.", "Deine Zuschauer hören kein Mikrofon.",
                "Lege von jedem Headset-Mikrofon eine Verbindung zum OBS-Stream an, oder richte die Geräte neu ein.")
        elif obs_routes and not live_obs:
            add("warning", "obs_all_off", OBS_MIC, "Alle Verbindungen zum OBS-Stream sind ausgeschaltet",
                "Jeder Pfeil zu OBS ist aus oder auf 0 %.", "Deine Zuschauer hören kein Mikrofon.",
                "Schalte mindestens eine Verbindung Mikrofon zum OBS-Stream ein.")
        elif cfg.virtual.separate:
            # `out` is the problem list in this method; the microphones need
            # their own name here
            for mic in cfg.virtual.outputs.values():
                feeders = [n for n, r in obs_routes if r.sink_ref == mic.key]
                live = [n for n in feeders if n in self.desired and not self.desired[n].mute and self.desired[n].volume > 0]
                if not feeders:
                    add("warning", f"obs_empty_{mic.key}", mic.key, f"«{mic.description}» bekommt nichts",
                        "Dieses Mikrofon für OBS hat keine Verbindung.",
                        "In OBS bleibt diese Spur stumm.",
                        "Lege eine Verbindung von einem Headset-Mikrofon zu diesem Mikrofon an.")
                elif not live:
                    add("warning", f"obs_off_{mic.key}", mic.key, f"«{mic.description}» ist ausgeschaltet",
                        "Alle Verbindungen zu diesem Mikrofon sind aus oder auf 0 %.",
                        "In OBS bleibt diese Spur stumm.", "Schalte die Verbindung wieder ein.")

        # risky combinations
        dev_of_node = {n["name"]: g for g in physical for n in g["inputs"] + g["outputs"]}
        for name, route in cfg.routes.items():
            proc = self.procs.get(name)
            if proc is None or proc.poll() is not None:
                add("warning", "route_restarting", name, f"Verbindung {self._route_label(route)} startet neu",
                    "Ihr Hilfsprozess hat gestoppt und wird automatisch wieder gestartet.",
                    "Eine kurze Unterbrechung auf diesem Weg.", "Nichts zu tun; wiederholt es sich, schau ins Protokoll.")
            if route.sink_ref in cfg.virtual.outputs:
                continue
            src_node = self._ref_node(route.source_ref, graph)
            dst_node = self._ref_node(route.sink_ref, graph)
            if dst_node is not None:
                info = describe_node(dst_node, graph)
                if info["speakers"] and src_node is not None and not describe_node(src_node, graph)["hdmi_capture"]:
                    add("warning", "feedback_risk", name, f"{self._route_label(route)} schickt ein Mikrofon auf Lautsprecher",
                        "Der Ausgang sieht nach Lautsprechern aus (Fernseher, Bildschirm, eingebaut), nicht nach Kopfhörern. Das Mikrofon kann den Ton wieder aufnehmen.",
                        "Echo oder lautes Pfeifen ist wahrscheinlich.",
                        "Nimm Kopfhörer als Ausgang, oder schalte diese Verbindung aus.")
                if info["bus"] == "Bluetooth":
                    add("info", "bluetooth_delay", name, f"{self._label(route.sink_ref)} ist ein Bluetooth-Gerät",
                        "Bluetooth-Ton kommt ungefähr 0,15 bis 0,3 Sekunden zu spät an.",
                        "Zum Miteinanderreden in Ordnung; störend, wenn jemand die eigene Stimme darüber hört.",
                        "Für alle, die sich selbst hören müssen, lieber ein USB-Headset.")
            if src_node is not None and dst_node is not None:
                g1, g2 = dev_of_node.get(src_node.name), dev_of_node.get(dst_node.name)
                if g1 is not None and g1 is g2:
                    add("info", "sidetone", name, f"{self._route_label(route)}: diese Person hört die eigene Stimme",
                        "Mikrofon und Kopfhörer gehören zum selben Headset.", "Manche mögen das, andere finden es störend.",
                        "Ausschalten oder leiser stellen, wenn es stört.")

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
                what = f"Headset von {name}" if kind in ("mic", "out") else name
                add("info", "port_bound", alias, f"{what} muss in {key[1]} bleiben",
                    "Es wird am USB-Anschluss erkannt, weil gleiche Geräte keine Seriennummer melden.",
                    "In einem anderen Anschluss gilt es als fehlend.", "Beschrifte Stecker und Anschluss.")

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

    def audio_settings(self) -> dict[str, Any]:
        from .pw import QUANTUM_CHOICES, quantum_drop_in, quantum_state

        state = quantum_state(self._graph_or_empty())
        path = quantum_drop_in()
        saved = None
        try:
            if path.is_file():
                text = path.read_text()
                for line in text.splitlines():
                    if "default.clock.quantum" in line:
                        saved = int(line.split("=")[-1].strip())
        except (OSError, ValueError):
            saved = None
        state["saved"] = saved
        state["saved_path"] = str(path)
        state["router_request"] = self.cfg.audio.latency
        state["choices"] = [c for c in state["choices"] if c["frames"] in QUANTUM_CHOICES]
        return state

    def set_audio_buffer(self, frames: int, persist: bool = False) -> dict[str, Any]:
        """Change the buffer size of the whole audio system (0 = automatic)."""
        from .pw import QUANTUM_CHOICES, persist_quantum

        if frames not in QUANTUM_CHOICES:
            raise RouterError(f"Die Puffergrösse muss eine davon sein: {', '.join(str(c) for c in QUANTUM_CHOICES)}")
        self.backend.set_force_quantum(frames)
        if persist:
            persist_quantum(frames)
        self._invalidate_graph()
        log.info("audio buffer set to %s%s", frames or "automatic", " (saved for next start)" if persist else "")
        return self.audio_settings()

    def dropout_check(self, seconds: float = 2.0) -> dict[str, Any]:
        try:
            return self.backend.dropouts(seconds)
        except PwError as exc:
            return {"available": False, "errors": 0, "nodes": [], "drivers": [], "error": str(exc)}

    def signal_graph(self) -> dict[str, Any]:
        """Every audio node and every link between them, labelled by owner.

        This is the whole wiring, not just our routes: it shows what else is
        attached to a device and where a path really ends up.
        """
        from .pw import classify_node, describe_node

        graph = self._graph_or_empty()
        route_of = {}
        for name, route in self.cfg.routes.items():
            route_of[route.in_node] = (name, "in")
            route_of[route.out_node] = (name, "out")
        mix_names = {out.mix_name: out for out in self.cfg.virtual.outputs.values()}
        mic_names = {out.mic_name: out for out in self.cfg.virtual.outputs.values()}

        nodes: dict[int, dict[str, Any]] = {}
        for node in graph.nodes.values():
            media = node.media_class
            if not (media.startswith("Audio/") or media.startswith("Stream/")):
                continue
            if media.startswith("Stream/") and "Audio" not in media and media != "Stream/Input" and media != "Stream/Output":
                continue
            info = describe_node(node, graph)
            kind = "sink" if media.startswith("Audio/Sink") else "source" if media.startswith("Audio/Source") else "stream"
            route_hit = route_of.get(node.name)
            if route_hit:
                route_name, side = route_hit
                friendly = self._route_label(self.cfg.routes[route_name])
                detail = "nimmt auf" if side == "in" else "gibt aus"
            elif node.name in mix_names:
                target = "OBS" if len(mix_names) == 1 else f"«{mix_names[node.name].description}»"
                friendly, detail = f"Mischspur für {target}", "sammelt die Mikrofone"
            elif node.name in mic_names:
                friendly, detail = mic_names[node.name].description, "was OBS aufnimmt"
            else:
                friendly, detail = info["friendly"] or node.name, info.get("device_name") or ""
            nodes[node.id] = {
                "id": node.id,
                "name": node.name,
                "friendly": friendly,
                "detail": detail,
                "category": classify_node(node.name, node, graph),
                "media_class": media,
                "kind": kind,
                "route": route_hit[0] if route_hit else None,
                "device": info.get("device_name") or "",
                "state": node.state,
                "muted": bool(node.mute),
                "default": any(node.name == v for v in graph.defaults.values()),
            }

        seen: dict[tuple[int, int], dict[str, Any]] = {}
        for link in graph.links:
            if link.output_node not in nodes or link.input_node not in nodes:
                continue
            key = (link.output_node, link.input_node)
            entry = seen.setdefault(key, {"from": link.output_node, "to": link.input_node, "channels": 0})
            entry["channels"] += 1

        # A loopback's two streams carry the audio between them inside the
        # module, with no PipeWire link. Without that hop the picture puts every
        # playback stream in the first column, which reads backwards.
        by_name = {n["name"]: n["id"] for n in nodes.values()}
        internal = [(self.cfg.routes[r].in_node, self.cfg.routes[r].out_node) for r in self.cfg.routes]
        internal += [(out.mix_name, out.mic_name) for out in self.cfg.virtual.outputs.values()]
        for src_name, dst_name in internal:
            a, b = by_name.get(src_name), by_name.get(dst_name)
            if a is not None and b is not None:
                seen.setdefault((a, b), {"from": a, "to": b, "channels": 1, "internal": True})

        for entry in seen.values():
            entry.setdefault("internal", False)
        linked = {i for pair in seen for i in pair}
        for node_id, node in nodes.items():
            node["connected"] = node_id in linked
        return {
            "nodes": sorted(nodes.values(), key=lambda n: (n["kind"], n["name"])),
            "links": list(seen.values()),
            "defaults": dict(graph.defaults),
        }

    def analyse(self, seconds: float = 3.0) -> dict[str, Any]:
        """A full picture of the audio system: who drives it, at what buffer
        size, who drops samples, and which nodes belong to this router, to real
        hardware, to a filter chain someone set up, or to an application."""
        from .pw import classify_node, describe_node, quantum_state

        graph = self._graph_or_empty()
        try:
            top = self.backend.dropouts(seconds)
        except PwError as exc:
            top = {"available": False, "rows": [], "errors": 0, "drivers": [], "error": str(exc)}

        by_name = {n.name: n for n in graph.nodes.values()}
        rows = []
        for row in top.get("rows", []):
            node = by_name.get(row["name"])
            info = describe_node(node, graph) if node else None
            rows.append({
                **row,
                "category": classify_node(row["name"], node, graph),
                "friendly": (info or {}).get("friendly") or row["name"],
                "media_class": node.media_class if node else "",
            })
        rows.sort(key=lambda r: (-r.get("delta", 0), -r["errors"], r["name"]))

        totals: dict[str, int] = {}
        for r in rows:
            totals[r["category"]] = totals.get(r["category"], 0) + r.get("delta", 0)
        drivers = [r for r in rows if r["driver"] and r["active"] and r["quantum"]]
        quanta = sorted({r["quantum"] for r in rows if r["quantum"]})

        findings: list[dict[str, str]] = []

        def add(level: str, title: str, why: str = "", effect: str = "", fix: str = "") -> None:
            findings.append({"level": level, "title": title, "why": why, "effect": effect, "fix": fix})

        if not top.get("available"):
            add("warning", "Keine Messung möglich",
                top.get("error") or "pw-top hat keine Tabelle geliefert (Teil von pipewire-bin).",
                "Ohne Messung lässt sich nicht sagen, ob Ton verloren geht.",
                "sudo apt install pipewire-bin")
            return {"available": False, "rows": rows, "findings": findings, "totals": totals,
                    "drivers": [], "quanta": quanta, "buffer": quantum_state(graph)}

        window = top.get("window", 1) if top.get("samples", 0) > 1 else 0
        span = f"in {window} s" if window else "seit dem Start der Knoten"
        live = top.get("delta", 0)
        historic = top.get("errors", 0)
        if not live:
            add("info", "Gerade gehen keine Tonpakete verloren",
                f"Während der Messung ({window} s) ging nichts verloren." if window else "Es wurde kein Verlust gemeldet."
                + (f" Insgesamt seit dem Start der einzelnen Knoten: {historic}, das ist Vergangenheit." if historic else ""),
                "Klingt der Ton trotzdem schlecht, liegt es nicht am Timing.", "")
        else:
            worst = rows[0]
            add("error" if live > 50 else "warning",
                f"{live} verlorene Tonpakete {span}",
                "Am meisten bei: " + ", ".join(f"{r['name']} ({r['delta']})" for r in rows[:3] if r["delta"]),
                "Verlorene Pakete sind Löcher im Ton. Viele davon klingen wie Knacken oder machen Sprache unverständlich.",
                "Puffergrösse oben erhöhen und nochmals messen. Bleibt es, liegt es an dem Knoten, der oben in der Liste steht.")
            if worst["category"] == "filter":
                add("warning", "Die meisten Aussetzer kommen von einer Filterkette",
                    f"«{worst['name']}» gehört nicht zu diesem Router. Solche Knoten entstehen durch eine eigene "
                    "Filterkette in der PipeWire-Konfiguration, zum Beispiel Rauschunterdrückung oder Mithören.",
                    "Der Ton wird schon kaputt, bevor dieser Router ihn überhaupt anfasst.",
                    "Filterkette vorübergehend deaktivieren und nochmals hören. Klingt es dann sauber, braucht die "
                    "Kette eine grössere Puffergrösse oder zu viel Rechenzeit.")
            elif totals.get("router", 0) * 2 < live:
                add("info", "Die Aussetzer entstehen nicht in diesem Router",
                    f"Von {live} verlorenen Paketen entfallen {totals.get('router', 0)} auf Knoten dieses Routers.",
                    "", "Schau zuerst auf die Knoten oben in der Liste.")

        for d in drivers:
            node = by_name.get(d["name"])
            info = describe_node(node, graph) if node else None
            if info and info.get("hdmi_capture"):
                add("warning", f"Die Aufnahmekarte gibt den Takt vor ({d['name']})",
                    f"Dieser Knoten ist Taktgeber der Gruppe und läuft mit {d['quantum']} Werten.",
                    "Liefert die Karte keinen Ton mehr, etwa weil die Quelle aus ist, kann die ganze Gruppe stehen bleiben.",
                    "Siehe docs/hdmi-capture.md: der Karte eine tiefere Taktpriorität geben.")
            elif d["quantum"] and d["quantum"] < 256:
                add("warning", f"Sehr kleine Puffergrösse bei {d['name']}",
                    f"Diese Taktgruppe läuft mit {d['quantum']} Werten ({round(d['quantum'] * 1000 / (d['rate'] or 48000), 1)} ms).",
                    "Je kleiner der Puffer, desto eher geht etwas verloren.",
                    "Puffergrösse oben erhöhen.")

        filters = sorted({r["name"] for r in rows if r["category"] == "filter"})
        if filters:
            filter_errors = totals.get("filter", 0)  # in the measured window, not since boot
            add("warning" if filter_errors else "info",
                f"{len(filters)} Knoten einer fremden Filterkette" + (f", zusammen {filter_errors} Aussetzer" if filter_errors else ""),
                ", ".join(filters[:8]) + (" …" if len(filters) > 8 else ""),
                "Diese Knoten liegen im Tonweg, gehören aber nicht zu diesem Router. Namen mit «clean» deuten auf "
                "Rauschunterdrückung, «sidetone» auf Mithören der eigenen Stimme.",
                "Schalte die Kette zum Test ab und hör nochmals: klingt es dann sauber, liegt es an ihr. "
                "Gesucht wird sie in ~/.config/pipewire/ (filter-chain). Sollen die Mikrofone über die gereinigte "
                "Variante laufen, wähle unter «Geräte» die Knoten mit «-clean».")

        if self.cfg.audio.latency != "auto":
            add("warning", f"Dieser Router verlangt eine feste Puffergrösse ({self.cfg.audio.latency})",
                "PipeWire läuft mit der kleinsten Puffergrösse, die irgendjemand verlangt. Eine Forderung hier zieht "
                "das ganze Tonsystem mit herunter.",
                "Kleinere Puffer heissen mehr Aussetzer, auch bei Geräten und Programmen, die nichts damit zu tun haben.",
                "Auf «Automatisch» stellen: der Knopf steht oben bei der Puffergrösse.")

        if len(quanta) > 2:
            add("info", "Mehrere Taktgruppen mit verschiedenen Puffergrössen",
                "Gefunden: " + ", ".join(str(q) for q in quanta),
                "Das ist normal, wenn Geräte auf eigenen Uhren laufen; PipeWire rechnet dazwischen um.", "")

        formats = self.device_formats(graph, rows)
        for entry in formats:
            for finding in entry["findings"]:
                add(**finding)
        for finding in self.rate_findings(graph, rows, formats):
            add(**finding)
        for finding in self.path_findings(graph):
            add(**finding)

        return {
            "available": True,
            "window": window,
            "delta": live,
            "historic": historic,
            "rows": rows,
            "findings": findings,
            "devices": formats,
            "totals": totals,
            "drivers": [{"name": d["name"], "quantum": d["quantum"], "rate": d["rate"], "errors": d["errors"]} for d in drivers],
            "quanta": quanta,
            "buffer": quantum_state(graph),
        }

    # ------------------------------------------------------ Format und Rate

    def device_formats(self, graph: Any = None, rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """What each configured device actually runs at, and what is wrong with
        it. A headset in its telephone profile or a stereo device stuck on one
        channel sounds broken without losing a single packet, so none of this
        shows up in the dropout measurement."""
        from .pw import describe_node, is_comms_profile, node_format, parse_top_format

        graph = graph if graph is not None else self._graph_or_empty()
        by_name = {r["name"]: r for r in (rows or [])}
        out: list[dict[str, Any]] = []
        for alias, res in sorted(self.resolved.items()):
            if not res.present or not res.node:
                continue
            node = graph.by_name(res.node)
            if node is None:
                continue
            device = None
            try:
                device = graph.devices.get(int(node.props.get("device.id")))
            except (TypeError, ValueError):
                device = None
            fmt = node_format(node, device)
            # pw-top prints the format that was really negotiated, which beats
            # the properties whenever both are there
            measured = parse_top_format(by_name.get(res.node, {}).get("format", ""))
            channels = measured.get("channels") or fmt["channels"]
            rate = measured.get("rate") or fmt["rate"]
            info = describe_node(node, graph)
            label = info.get("friendly") or res.node
            findings: list[dict[str, str]] = []

            if is_comms_profile(fmt):
                findings.append({
                    "level": "warning",
                    "title": f"«{label}» läuft im Sprechprofil",
                    "why": f"Das Gerät steht auf dem Profil «{fmt['profile'] or fmt['profile_name']}». "
                           "Solche Profile sind fürs Telefonieren gedacht: ein Kanal, stark komprimiert, "
                           "oft 8 oder 16 kHz.",
                    "effect": "Der Ton klingt dumpf und zischelt, Sprache wird schwer verständlich.",
                    "fix": "Auf das volle Profil umstellen: wpctl status zeigt die Karte, "
                           "pactl list cards die Profile, und in den Ton-Einstellungen von Ubuntu "
                           "lässt es sich am schnellsten wechseln (nicht «Headset», sondern «Analog Stereo» "
                           "bzw. das Profil ohne «head unit»).",
                })
            elif channels == 1 and info.get("kind") == "output":
                findings.append({
                    "level": "warning",
                    "title": f"«{label}» läuft mit einem Kanal",
                    "why": f"Der Knoten meldet {channels} Kanal. Kopfhörer sind stereo; ein Kanal deutet auf ein "
                           "eingeschränktes Profil hin.",
                    "effect": "Der Ton kommt nur auf einem Ohr an oder wird zusammengemischt.",
                    "fix": "Profil der Karte auf Stereo stellen (siehe Ton-Einstellungen von Ubuntu).",
                })

            if rate and rate < 44100:
                findings.append({
                    "level": "warning",
                    "title": f"«{label}» läuft mit {rate} Hz",
                    "why": "Unter 44100 Hz ist Telefonqualität. Das kommt vom Profil des Geräts, nicht vom Router.",
                    "effect": "Alles über etwa 8 kHz fehlt: der Ton klingt dumpf, S-Laute verschwinden.",
                    "fix": "Profil des Geräts wechseln (siehe oben). Danach hier nochmals messen.",
                })

            out.append({
                "alias": alias,
                "node": res.node,
                "label": label,
                "kind": info.get("kind", ""),
                "channels": channels,
                "rate": rate,
                "position": fmt["position"],
                "profile": fmt["profile"] or fmt["profile_name"],
                "sample_format": measured.get("sample_format", ""),
                "findings": findings,
            })
        return out

    def path_findings(self, graph: Any = None) -> list[dict[str, str]]:
        """Two problems that are only visible in the shape of the graph.

        *The same sound arriving twice*: a headphone that receives the same
        microphone over two different paths plays it slightly offset, which
        sounds hollow and metallic.

        *A loop*: sound that comes back to where it started. That is feedback,
        and it can get loud enough to hurt.
        """
        from .pw import classify_node, describe_node

        graph = graph if graph is not None else self._graph_or_empty()
        findings: list[dict[str, str]] = []
        if not graph.nodes:
            return findings

        # a graph where everything is linked to everything is broken in its own
        # right; the walk gets a budget so a diagnosis can never become the
        # bigger problem
        budget = [ORIGIN_VISITS]
        labels: dict[int, str] = {}
        mine = {out.mix_name: out for out in self.cfg.virtual.outputs.values()}

        def label(node_id: int) -> str:
            if node_id not in labels:
                node = graph.nodes.get(node_id)
                if node is None:
                    labels[node_id] = f"Knoten {node_id}"
                elif node.name in mine:
                    # our own mix bus is not "speakers", whatever it looks like
                    labels[node_id] = f"Mikrofon für OBS «{mine[node.name].description}»"
                else:
                    labels[node_id] = describe_node(node, graph).get("friendly") or node.name
            return labels[node_id]

        # --- the same source reaching one sink over more than one path.
        # The incoming edges are indexed once: walking them through
        # graph.peers_of_input() would rescan every link for every node.
        sources: dict[int, set[int]] = {}
        for link in graph.links:
            sources.setdefault(link.input_node, set()).add(link.output_node)
        for sink_id, feeders in sources.items():
            node = graph.nodes.get(sink_id)
            if node is None or not node.media_class.startswith("Audio/Sink"):
                continue
            # keyed by node name, never by the friendly one: two identical
            # headsets carry the same label, and counting those as one origin
            # turned the normal setup into a warning
            origins: dict[str, list[str]] = {}
            for feeder in feeders:
                for origin in self._origins_of(feeder, graph, incoming=sources, seen=set(), budget=budget):
                    origins.setdefault(origin, []).append(label(feeder))
            for origin, paths in origins.items():
                if len(paths) > 1:
                    node = graph.by_name(origin)
                    friendly = label(node.id) if node is not None else origin
                    findings.append({
                        "level": "warning",
                        "title": f"«{label(sink_id)}» bekommt «{friendly}» über {len(paths)} Wege",
                        "why": "Über: " + ", ".join(sorted(set(paths))) + ".",
                        "effect": "Zweimal derselbe Ton, leicht versetzt: das klingt hohl und metallisch.",
                        "fix": "Einen der Wege abschalten. Meist ist es ein Mithören in OBS oder ein zusätzlicher "
                               "Monitor-Ausgang neben der Verbindung dieses Routers.",
                    })

        # --- a cycle: sound that comes back to where it started
        for cycle in self._link_cycles(graph):
            findings.append({
                "level": "error",
                "title": "Der Ton läuft im Kreis",
                "why": " → ".join(label(n) for n in cycle) + f" → {label(cycle[0])}.",
                "effect": "Rückkopplung. Das schaukelt sich auf und kann laut genug werden, um weh zu tun.",
                "fix": "Eine Verbindung in diesem Kreis sofort abschalten. Meist entsteht er, wenn ein Ausgang "
                       "wieder auf einen Eingang gelegt wird, der schon dorthin spielt.",
            })
        return findings

    def _internal_hop(self, node: Any, graph: Any) -> Any:
        """A loopback is one process: its playback side is fed by its capture
        side, but PipeWire shows no link between the two. The same holds for the
        virtual microphone, which is fed by its mix sink."""
        name = node.name
        if name.startswith("tfcz.") and name.endswith(".out"):
            return graph.by_name(name[: -len(".out")] + ".in")
        for out in self.cfg.virtual.outputs.values():
            if name == out.mic_name:
                return graph.by_name(out.mix_name)
        return None

    def _origins_of(self, node_id: int, graph: Any, incoming: dict[int, set[int]] | None = None,
                    depth: int = 6, seen: set[int] | None = None, budget: list[int] | None = None) -> set[str]:
        """Which real capture devices feed this node, looking through our own
        loopbacks and through filter chains.

        `seen` is not optional in spirit: without it a densely linked graph
        would be walked along every possible path instead of every node.
        """
        from .pw import describe_node

        seen = seen if seen is not None else set()
        budget = budget if budget is not None else [ORIGIN_VISITS]
        budget[0] -= 1
        if budget[0] <= 0:
            return set()
        if incoming is None:
            incoming = {}
            for link in graph.links:
                incoming.setdefault(link.input_node, set()).add(link.output_node)
        if node_id in seen or depth <= 0:
            return set()
        seen.add(node_id)
        node = graph.nodes.get(node_id)
        if node is None:
            return set()
        if node.media_class.startswith("Audio/Source") and not node.name.startswith("tfcz."):
            return {node.name}
        found: set[str] = set()
        hop = self._internal_hop(node, graph)
        if hop is not None:
            found |= self._origins_of(hop.id, graph, incoming, depth - 1, seen, budget)
        for feeder in incoming.get(node_id, ()):  # noqa: PLR1702
            found |= self._origins_of(feeder, graph, incoming, depth - 1, seen, budget)
        return found

    def _link_cycles(self, graph: Any, limit: int = 3) -> list[list[int]]:
        """Cycles in the link graph, at most `limit` of them: one is enough to
        act on, and a broken graph must not turn this into a long walk."""
        colour: dict[int, int] = {}
        stack: list[int] = []
        cycles: list[list[int]] = []
        edges: dict[int, set[int]] = {}
        for link in graph.links:
            edges.setdefault(link.output_node, set()).add(link.input_node)

        def walk(node_id: int) -> None:
            if len(cycles) >= limit:
                return
            colour[node_id] = 1
            stack.append(node_id)
            for nxt in sorted(edges.get(node_id, ())):
                if colour.get(nxt) == 1:  # back edge: everything from nxt on the stack is the cycle
                    cycles.append(stack[stack.index(nxt):])
                    if len(cycles) >= limit:
                        break
                elif colour.get(nxt, 0) == 0:
                    walk(nxt)
            stack.pop()
            colour[node_id] = 2

        for node_id in sorted(edges):
            if colour.get(node_id, 0) == 0:
                walk(node_id)
        return cycles

    def rate_findings(self, graph: Any = None, rows: list[dict[str, Any]] | None = None,
                      formats: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
        """Sample rates that do not match. PipeWire resamples silently, so this
        never shows up as a lost packet -- it is only audible."""
        from .pw import quantum_state

        graph = graph if graph is not None else self._graph_or_empty()
        clock = quantum_state(graph)
        findings: list[dict[str, str]] = []

        device_rates: dict[int, list[str]] = {}
        for entry in (formats if formats is not None else self.device_formats(graph, rows)):
            if entry["rate"]:
                device_rates.setdefault(entry["rate"], []).append(entry["label"])

        if len(device_rates) > 1:
            listed = "; ".join(f"{rate} Hz: {', '.join(names)}" for rate, names in sorted(device_rates.items()))
            findings.append({
                "level": "warning",
                "title": "Die Geräte laufen mit verschiedenen Abtastraten",
                "why": listed,
                "effect": "PipeWire rechnet dazwischen um. Das geht meistens gut, kostet aber Qualität, und wenn ein "
                          "Gerät seine Rate nicht sauber hält, knackt es regelmässig.",
                "fix": "Alles auf 48000 Hz bringen: die Aufnahmekarte kann nur das, also die Headsets ebenfalls darauf "
                       "festlegen.",
            })

        clock_rate = clock.get("rate") or 0
        off = sorted(r for r in device_rates if clock_rate and r != clock_rate)
        if off:
            findings.append({
                "level": "warning",
                "title": f"Das Tonsystem läuft auf {clock_rate} Hz, Geräte auf {', '.join(str(r) for r in off)} Hz",
                "why": "Die Uhr von PipeWire und die Geräte sind sich nicht einig, also wird für jedes Gerät "
                       "umgerechnet.",
                "effect": "Dauerhaftes Umrechnen klingt je nach Gerät leicht rau und kann bei Drift knacken.",
                "fix": "In ~/.config/pipewire/pipewire.conf.d/10-rate.conf eintragen: "
                       "context.properties = { default.clock.rate = 48000, default.clock.allowed-rates = [ 48000 ] } "
                       "und danach systemctl --user restart pipewire wireplumber.",
            })
        return findings

    def output_targets(self, graph: Graph | None = None) -> list[dict[str, Any]]:
        """Devices a test tone can be sent to: everything that plays sound.

        Sinks only. Sending a tone at a microphone would do nothing and only
        cost trust in the check.
        """
        graph = graph if graph is not None else self._graph_or_empty()
        out = []
        for alias in sorted(self.cfg.devices):
            res = self.resolved.get(alias)
            node = graph.by_name(res.node) if res and res.node else None
            if node is None or not node.media_class.startswith("Audio/Sink"):
                continue
            out.append({"alias": alias, "node": node.name, "label": human(self.cfg, alias),
                        "present": True})
        return out

    def tone_target(self, alias: str, graph: Graph | None = None) -> tuple[str, str]:
        """(node, label) for a test tone, or a clear refusal."""
        graph = graph if graph is not None else self._graph_or_empty()
        if alias not in self.cfg.devices:
            # the setup wizard plays a tone before anything is configured, so a
            # plain node name is allowed too -- as long as it really plays sound
            from .pw import describe_node

            raw = graph.by_name(alias)
            if raw is not None:
                if raw.name.startswith("tfcz."):
                    # our own mix bus is a sink like any other, and a tone into
                    # it would go out on the stream and into both headsets
                    raise RouterError("Der Testton geht nur an echte Geräte, nicht an die Kanäle dieses Routers")
                if not raw.media_class.startswith("Audio/Sink"):
                    raise RouterError(f"{describe_node(raw, graph).get('friendly') or alias} ist kein Ausgabegerät; "
                                      "ein Testton geht nur an Kopfhörer oder Lautsprecher")
                return raw.name, describe_node(raw, graph).get("friendly") or raw.name
            known = ", ".join(sorted(self.cfg.devices)) or "keine"
            raise RouterError(f"Das Gerät «{alias}» ist nicht eingerichtet (bekannt: {known})")
        res = self.resolved.get(alias)
        node = graph.by_name(res.node) if res and res.node else None
        label = human(self.cfg, alias)
        if node is None:
            raise RouterError(f"{label} ist nicht angeschlossen")
        if not node.media_class.startswith("Audio/Sink"):
            raise RouterError(f"{label} ist kein Ausgabegerät; ein Testton geht nur an Kopfhörer oder Lautsprecher")
        return node.name, label

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
            raise RouterError(f"{node_name} ist nicht angeschlossen")
        return identity_for(node, graph)
