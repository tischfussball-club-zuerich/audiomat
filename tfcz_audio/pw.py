"""PipeWire backend: graph inspection via ``pw-dump``, volume via ``wpctl``,
routes via ``pw-loopback`` child processes.

Volumes use the same cubic scale as ``wpctl``/pavucontrol: 1.0 is 0 dB,
0.5 is roughly -18 dB. ``channelVolumes`` in the graph are linear
amplitudes, i.e. the cube of that value.
"""

from __future__ import annotations

import json
import logging
import math
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("tfcz.pw")

AUDIO_CLASSES = ("Audio/Source", "Audio/Sink", "Audio/Source/Virtual", "Audio/Duplex")


class PwError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Volume helpers
# --------------------------------------------------------------------------- #


def cubic_to_linear(volume: float) -> float:
    return max(0.0, volume) ** 3


def linear_to_cubic(amplitude: float) -> float:
    return max(0.0, amplitude) ** (1.0 / 3.0)


def cubic_to_db(volume: float) -> float | None:
    if volume <= 0:
        return None
    return round(60.0 * math.log10(volume), 2)  # 20*log10(v^3)


def db_to_cubic(db: float) -> float:
    return 10 ** (db / 60.0)


# --------------------------------------------------------------------------- #
# Graph model
# --------------------------------------------------------------------------- #


@dataclass
class Node:
    id: int
    name: str
    description: str = ""
    media_class: str = ""
    volume: float | None = None  # cubic scale
    mute: bool | None = None
    props: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "media_class": self.media_class,
        }


@dataclass
class Link:
    id: int
    output_node: int
    input_node: int


@dataclass
class Graph:
    nodes: dict[int, Node] = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)

    def by_name(self, name: str) -> Node | None:
        for node in self.nodes.values():
            if node.name == name:
                return node
        return None

    def has_input_link(self, node_id: int) -> bool:
        return any(l.input_node == node_id for l in self.links)

    def has_output_link(self, node_id: int) -> bool:
        return any(l.output_node == node_id for l in self.links)

    def audio_devices(self) -> list[Node]:
        return sorted(
            (n for n in self.nodes.values() if n.media_class in AUDIO_CLASSES),
            key=lambda n: (n.media_class, n.name),
        )


def _iter_json_documents(text: str):
    """pw-dump normally emits one JSON array; be tolerant of several."""
    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)
    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break
        obj, end = decoder.raw_decode(text, idx)
        yield obj
        idx = end


def parse_dump(text: str) -> Graph:
    graph = Graph()
    for doc in _iter_json_documents(text):
        if not isinstance(doc, list):
            continue
        for obj in doc:
            if not isinstance(obj, dict):
                continue
            otype = obj.get("type", "")
            info = obj.get("info") or {}
            if otype == "PipeWire:Interface:Node":
                props = info.get("props") or {}
                node = Node(
                    id=int(obj["id"]),
                    name=str(props.get("node.name", "")),
                    description=str(props.get("node.description") or props.get("node.nick") or ""),
                    media_class=str(props.get("media.class", "")),
                    props=props,
                )
                for p in (info.get("params") or {}).get("Props") or []:
                    if not isinstance(p, dict):
                        continue
                    if "mute" in p:
                        node.mute = bool(p["mute"])
                    chans = p.get("channelVolumes")
                    if isinstance(chans, list) and chans:
                        node.volume = round(linear_to_cubic(sum(chans) / len(chans)), 4)
                    elif "volume" in p and node.volume is None:
                        node.volume = round(float(p["volume"]), 4)
                graph.nodes[node.id] = node
            elif otype == "PipeWire:Interface:Link":
                try:
                    graph.links.append(
                        Link(
                            id=int(obj["id"]),
                            output_node=int(info["output-node-id"]),
                            input_node=int(info["input-node-id"]),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
    return graph


# --------------------------------------------------------------------------- #
# Loopback specification
# --------------------------------------------------------------------------- #


def spa_json(props: dict[str, Any]) -> str:
    """Serialise a flat dict to SPA-JSON as accepted by pw-loopback --*-props."""
    parts = []
    for key, value in props.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        elif isinstance(value, (list, tuple)):
            rendered = "[ " + " ".join(str(v) for v in value) + " ]"
        else:
            rendered = json.dumps(str(value))
        parts.append(f"{key} = {rendered}")
    return "{ " + " ".join(parts) + " }"


@dataclass
class LoopbackSpec:
    name: str
    capture_props: dict[str, Any]
    playback_props: dict[str, Any]
    channels: int = 2

    @property
    def capture_node(self) -> str:
        return str(self.capture_props["node.name"])

    @property
    def playback_node(self) -> str:
        return str(self.playback_props["node.name"])

    def command(self) -> list[str]:
        channel_map = {1: ["MONO"], 2: ["FL", "FR"]}.get(self.channels)
        cmd = ["pw-loopback", "-n", self.name, "-c", str(self.channels)]
        if channel_map:
            cmd += ["-m", "[ " + " ".join(channel_map) + " ]"]
        cmd += [
            "--capture-props=" + spa_json(self.capture_props),
            "--playback-props=" + spa_json(self.playback_props),
        ]
        return cmd


class Process(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


class Backend(Protocol):
    def graph(self) -> Graph: ...
    def set_volume(self, node_id: int, volume: float) -> None: ...
    def set_mute(self, node_id: int, mute: bool) -> None: ...
    def spawn_loopback(self, spec: LoopbackSpec) -> Process: ...


# --------------------------------------------------------------------------- #
# Real backend
# --------------------------------------------------------------------------- #


class PipeWireBackend:
    """Talks to the user's PipeWire session through the standard CLI tools."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def _run(self, cmd: list[str], timeout: float = 5.0) -> str:
        log.debug("exec: %s", shlex.join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise PwError(f"{cmd[0]} not found; install pipewire-bin/wireplumber") from exc
        except subprocess.TimeoutExpired as exc:
            raise PwError(f"{cmd[0]} timed out") from exc
        if proc.returncode != 0:
            raise PwError(f"{shlex.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout

    def graph(self) -> Graph:
        if self.dry_run:
            return Graph()
        return parse_dump(self._run(["pw-dump"]))

    def set_volume(self, node_id: int, volume: float) -> None:
        cmd = ["wpctl", "set-volume", str(node_id), f"{volume:.4f}"]
        if self.dry_run:
            log.info("dry-run: %s", shlex.join(cmd))
            return
        self._run(cmd)

    def set_mute(self, node_id: int, mute: bool) -> None:
        cmd = ["wpctl", "set-mute", str(node_id), "1" if mute else "0"]
        if self.dry_run:
            log.info("dry-run: %s", shlex.join(cmd))
            return
        self._run(cmd)

    def spawn_loopback(self, spec: LoopbackSpec) -> Process:
        cmd = spec.command()
        if self.dry_run:
            log.info("dry-run: %s", shlex.join(cmd))
            return _DryProcess()
        log.info("spawn: %s", shlex.join(cmd))
        try:
            return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise PwError("pw-loopback not found; install pipewire-bin") from exc


class _DryProcess:
    pid = 0

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        return 0


# --------------------------------------------------------------------------- #
# Fake backend for tests and offline development
# --------------------------------------------------------------------------- #


class FakeProcess:
    _next_pid = 1000

    def __init__(self, on_exit=None):
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.returncode: int | None = None
        self.terminated = False
        self._on_exit = on_exit

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        if self._on_exit:
            self._on_exit(self)

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode if self.returncode is not None else 0

    def crash(self) -> None:
        self.returncode = 1
        if self._on_exit:
            self._on_exit(self)


class FakeBackend:
    """In-memory PipeWire. Spawning a loopback creates its two stream nodes."""

    def __init__(self, devices: list[tuple[str, str]] | None = None):
        self._graph = Graph()
        self._next_id = 30
        self.calls: list[tuple] = []
        self.processes: dict[int, tuple[FakeProcess, LoopbackSpec]] = {}
        for name, media_class in devices or []:
            self.add_device(name, media_class)

    def add_device(self, name: str, media_class: str, description: str = "") -> Node:
        node = Node(id=self._next_id, name=name, media_class=media_class, description=description or name)
        self._next_id += 1
        self._graph.nodes[node.id] = node
        return node

    def remove_node(self, name: str) -> None:
        node = self._graph.by_name(name)
        if node:
            del self._graph.nodes[node.id]
            self._graph.links = [l for l in self._graph.links if node.id not in (l.output_node, l.input_node)]

    def link(self, output_name: str, input_name: str) -> None:
        out = self._graph.by_name(output_name)
        inp = self._graph.by_name(input_name)
        if out and inp:
            self._graph.links.append(Link(self._next_id, out.id, inp.id))
            self._next_id += 1

    def graph(self) -> Graph:
        return self._graph

    def set_volume(self, node_id: int, volume: float) -> None:
        self.calls.append(("set_volume", node_id, round(volume, 4)))
        self._graph.nodes[node_id].volume = round(volume, 4)

    def set_mute(self, node_id: int, mute: bool) -> None:
        self.calls.append(("set_mute", node_id, mute))
        self._graph.nodes[node_id].mute = mute

    def spawn_loopback(self, spec: LoopbackSpec) -> FakeProcess:
        self.calls.append(("spawn", spec.name))
        proc = FakeProcess(on_exit=self._on_exit)
        self.processes[proc.pid] = (proc, spec)
        cap = self.add_device(spec.capture_node, str(spec.capture_props.get("media.class", "Stream/Input/Audio")))
        cap.volume, cap.mute = 1.0, False
        play = self.add_device(spec.playback_node, str(spec.playback_props.get("media.class", "Stream/Output/Audio")))
        play.volume, play.mute = 1.0, False
        # auto-link to targets when present, like WirePlumber would
        src = spec.capture_props.get("target.object")
        dst = spec.playback_props.get("target.object")
        if src:
            self.link(str(src), spec.capture_node)
        if dst:
            self.link(spec.playback_node, str(dst))
        return proc

    def _on_exit(self, proc: FakeProcess) -> None:
        entry = self.processes.pop(proc.pid, None)
        if entry:
            _, spec = entry
            self.remove_node(spec.capture_node)
            self.remove_node(spec.playback_node)
