"""PipeWire backend: graph inspection via ``pw-dump``, volume via ``wpctl``,
routes via ``pw-loopback`` child processes.

Volumes use the same cubic scale as ``wpctl``/pavucontrol: 1.0 is 0 dB,
0.5 is roughly -18 dB. ``channelVolumes`` in the graph are linear
amplitudes, i.e. the cube of that value.
"""

from __future__ import annotations

import collections
import json
import logging
import math
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
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
    state: str = ""  # suspended / idle / running / error
    error: str = ""

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
class Device:
    id: int
    name: str = ""
    description: str = ""
    bus: str = ""
    form_factor: str = ""
    api: str = ""
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class Graph:
    nodes: dict[int, Node] = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)
    devices: dict[int, Device] = field(default_factory=dict)
    defaults: dict[str, str] = field(default_factory=dict)  # default.audio.sink / default.audio.source -> node.name
    clients: set[str] = field(default_factory=set)  # application.name of connected clients
    has_default_metadata: bool = False
    settings: dict[str, str] = field(default_factory=dict)  # PipeWire's own clock settings

    def linked(self, output_node: int, input_node: int) -> bool:
        return any(l.output_node == output_node and l.input_node == input_node for l in self.links)

    def peers_of_input(self, node_id: int) -> set[int]:
        return {l.output_node for l in self.links if l.input_node == node_id}

    def peers_of_output(self, node_id: int) -> set[int]:
        return {l.input_node for l in self.links if l.output_node == node_id}

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


_STRIP_WORDS = (
    "Analog Stereo", "Analog Mono", "Analog Surround 5.1", "Analog Surround 7.1", "Digital Stereo (IEC958)",
    "Digital Stereo (HDMI)", "Digital Stereo", "Stereo", "Mono", "Multichannel", "Pro", "Duplex", "Audio",
)


def _clean_description(text: str) -> str:
    out = text.strip()
    changed = True
    while changed and out:
        changed = False
        for word in _STRIP_WORDS:
            if out.endswith(word):
                out = out[: -len(word)].strip(" -_,")
                changed = True
    return out or text.strip()


def bus_of(node: Node, device: Device | None) -> str:
    """'USB', 'Bluetooth', 'PCI card', 'built-in' or ''."""
    api = str(node.props.get("device.api", "")) or (device.api if device else "")
    if api == "bluez5":
        return "Bluetooth"
    name = node.name
    bus = device.bus if device else ""
    if bus == "usb" or ".usb-" in name or name.startswith("alsa_input.usb") or name.startswith("alsa_output.usb"):
        return "USB"
    if bus == "pci" or ".pci-" in name:
        ff = (device.form_factor if device else "") or str(node.props.get("device.form-factor", ""))
        if ff == "internal":
            return "eingebaut"
        return "PCI-Karte"
    return ""


def is_hdmi_capture(node: Node, device: Device | None) -> bool:
    text = " ".join(
        str(x)
        for x in (
            node.props.get("alsa.card_name"), node.props.get("alsa.long_card_name"), node.props.get("alsa.driver_name"),
            device.props.get("alsa.card_name") if device else "", device.name if device else "", node.name,
        )
        if x
    ).lower()
    return node.media_class.startswith("Audio/Source") and ("hws" in text or "capture" in text or "hdmi" in text)


# profile names that mean "this device is in a telephone mode": mono, heavily
# compressed, often 8 or 16 kHz. Speech through them is dull and hissy.
COMMS_PROFILE_HINTS = ("headset", "headset_head_unit", "hfp", "hsp", "handsfree", "chat", "communication", "voice")

TOP_FORMAT = re.compile(r"(?P<format>[A-Za-z][A-Za-z0-9_]*)\s+(?P<channels>\d+)\s+(?P<rate>\d+)")


def parse_top_format(text: str) -> dict[str, Any]:
    """pw-top prints the negotiated format as e.g. ``S16LE 2 48000``."""
    m = TOP_FORMAT.search(text or "")
    if not m:
        return {}
    return {"sample_format": m.group("format"), "channels": int(m.group("channels")), "rate": int(m.group("rate"))}


def node_format(node: Node, device: Device | None = None) -> dict[str, Any]:
    """Channels, rate and card profile of a device node, as far as PipeWire
    tells us. Every field is optional: which properties a node carries depends
    on the driver and on the WirePlumber version."""
    props = node.props
    out: dict[str, Any] = {"channels": 0, "rate": 0, "position": "", "profile": "", "profile_name": ""}
    try:
        out["channels"] = int(props.get("audio.channels") or 0)
    except (TypeError, ValueError):
        pass
    for key in ("audio.rate", "node.rate", "api.alsa.rate"):
        value = props.get(key)
        if value is None:
            continue
        # node.rate is a fraction like "1/48000"
        text = str(value)
        digits = text.rsplit("/", 1)[-1]
        try:
            out["rate"] = int(float(digits))
        except (TypeError, ValueError):
            continue
        if out["rate"]:
            break
    out["position"] = str(props.get("audio.position") or "")
    for source in (props, device.props if device else {}):
        out["profile"] = out["profile"] or str(source.get("device.profile.description") or "")
        out["profile_name"] = out["profile_name"] or str(source.get("device.profile.name") or "")
    return out


def is_comms_profile(fmt: dict[str, Any]) -> bool:
    """A profile meant for telephony rather than for listening."""
    text = f"{fmt.get('profile_name', '')} {fmt.get('profile', '')}".lower()
    return any(hint in text for hint in COMMS_PROFILE_HINTS)


def looks_like_speakers(node: Node, device: Device | None) -> bool:
    """Output that is probably a loudspeaker rather than a headset: HDMI/monitor
    audio or the built-in card. Routing a microphone there risks echo."""
    if not node.media_class.startswith("Audio/Sink"):
        return False
    ff = (device.form_factor if device else "") or str(node.props.get("device.form-factor", ""))
    if ff in ("headset", "headphone", "hands-free"):
        return False
    if ff in ("speaker", "internal", "tv"):
        return True
    text = (node.name + " " + node.description).lower()
    return "hdmi" in text or "displayport" in text or (bus_of(node, device) == "built-in")


def friendly_name(node: Node, device: Device | None) -> str:
    """Plain-language label: 'Jabra EVOLVE 20 · microphone', 'HDMI capture input 2'."""
    if is_hdmi_capture(node, device):
        idx = node.props.get("api.alsa.card") or node.props.get("alsa.card") or ""
        card = str(node.props.get("alsa.card_name") or (device.props.get("alsa.card_name") if device else "") or "")
        label = "HDMI-Aufnahmeeingang"
        if idx != "":
            label += f" {idx}"
        return f"{label} ({card})" if card else label
    base = _clean_description(node.description) if node.description else ""
    if device and device.description:
        dev_base = _clean_description(device.description)
        if dev_base and (not base or len(dev_base) <= len(base)):
            base = dev_base
    if not base:
        base = node.name
    if node.media_class.startswith("Audio/Source"):
        return f"{base} · Mikrofon"
    if node.media_class.startswith("Audio/Sink"):
        ff = (device.form_factor if device else "") or str(node.props.get("device.form-factor", ""))
        what = "Kopfhörer" if ff in ("headset", "headphone", "hands-free") or bus_of(node, device) in ("USB", "Bluetooth") else "Lautsprecher"
        return f"{base} · {what}"
    return base


def describe_node(node: Node, graph: Graph) -> dict[str, Any]:
    device = None
    dev_id = node.props.get("device.id")
    if dev_id is not None:
        try:
            device = graph.devices.get(int(dev_id))
        except (TypeError, ValueError):
            device = None
    kind = "input" if node.media_class.startswith("Audio/Source") else "output" if node.media_class.startswith("Audio/Sink") else "other"
    return {
        **node.to_dict(),
        "friendly": friendly_name(node, device),
        "kind": kind,
        "bus": bus_of(node, device),
        "device_id": device.id if device else None,
        "device_name": _clean_description(device.description) if device and device.description else "",
        "hdmi_capture": is_hdmi_capture(node, device),
        "speakers": looks_like_speakers(node, device),
        "system_mute": bool(node.mute) if node.mute is not None else False,
        "system_volume": node.volume,
        "virtual": node.media_class == "Audio/Source/Virtual" or node.name.startswith("tfcz."),
        "state": node.state,
        "error": node.error,
        "usage": alsa_usage(node),
        "serial": str((device.props.get("device.serial") if device else None) or node.props.get("device.serial") or ""),
        "bus_path": str((device.props.get("device.bus-path") if device else None) or node.props.get("device.bus-path") or ""),
        "port": port_label(str((device.props.get("device.bus-path") if device else None) or node.props.get("device.bus-path") or "")),
    }


def physical_devices(graph: Graph) -> list[dict[str, Any]]:
    """Group audio nodes by the hardware they belong to. A USB headset becomes
    one entry with an input (microphone) and an output (headphones)."""
    groups: dict[str, dict[str, Any]] = {}
    for node in graph.audio_devices():
        if node.name.startswith("tfcz."):
            continue
        info = describe_node(node, graph)
        key = f"dev:{info['device_id']}" if info["device_id"] is not None else f"node:{node.name}"
        g = groups.setdefault(
            key,
            {
                "id": key,
                "name": info["device_name"] or _clean_description(node.description) or node.name,
                "bus": info["bus"],
                "inputs": [],
                "outputs": [],
                "hdmi_capture": False,
                "speakers": False,
            },
        )
        g["hdmi_capture"] = g["hdmi_capture"] or info["hdmi_capture"]
        if info["hdmi_capture"]:
            g["name"] = info["friendly"].split(" (")[0]
        g["speakers"] = g["speakers"] or info["speakers"]
        g["port"] = info["port"]
        g["serial"] = info["serial"]
        (g["inputs"] if info["kind"] == "input" else g["outputs"] if info["kind"] == "output" else []).append(info)
    out = list(groups.values())
    for g in out:
        g["headset"] = bool(g["inputs"] and g["outputs"] and g["bus"] in ("USB", "Bluetooth") and not g["hdmi_capture"])
        first = (g["inputs"] or g["outputs"])[0]
        node = graph.by_name(first["name"])
        g["identity"] = identity_for(node, graph) if node else {"strategy": "name", "text": "", "port": "", "match": {}}
    out.sort(key=lambda g: (not g["headset"], not g["hdmi_capture"], g["name"]))
    return out


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
                    state=str(info.get("state", "")),
                    error=str(info.get("error", "") or ""),
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
            elif otype == "PipeWire:Interface:Device":
                props = info.get("props") or {}
                graph.devices[int(obj["id"])] = Device(
                    id=int(obj["id"]),
                    name=str(props.get("device.name", "")),
                    description=str(props.get("device.description") or props.get("device.nick") or props.get("device.product.name") or ""),
                    bus=str(props.get("device.bus", "")),
                    form_factor=str(props.get("device.form-factor", "")),
                    api=str(props.get("device.api", "")),
                    props=props,
                )
            elif otype == "PipeWire:Interface:Client":
                name = str((info.get("props") or {}).get("application.name", ""))
                if name:
                    graph.clients.add(name)
            elif otype == "PipeWire:Interface:Metadata":
                meta_name = str((obj.get("props") or info.get("props") or {}).get("metadata.name", ""))
                if meta_name == "settings":
                    for entry in obj.get("metadata") or []:
                        if isinstance(entry, dict) and entry.get("key"):
                            value = entry.get("value")
                            graph.settings[str(entry["key"])] = str(value if not isinstance(value, dict) else value.get("name", ""))
                if meta_name == "default":
                    graph.has_default_metadata = True
                    for entry in obj.get("metadata") or []:
                        if not isinstance(entry, dict):
                            continue
                        key = str(entry.get("key", ""))
                        if key in ("default.audio.sink", "default.audio.source", "default.configured.audio.sink", "default.configured.audio.source"):
                            value = entry.get("value")
                            name = value.get("name") if isinstance(value, dict) else None
                            if name:
                                graph.defaults[key] = str(name)
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
            # explicit UTF-8: the systemd user environment may run with a C/POSIX locale, and device
            # descriptions ("Kopfhörer") would otherwise raise UnicodeDecodeError
            proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise PwError(f"{cmd[0]} not found; install pipewire-bin/wireplumber") from exc
        except subprocess.TimeoutExpired as exc:
            raise PwError(f"{cmd[0]} timed out") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise PwError(f"cannot run {cmd[0]}: {exc}") from exc
        if proc.returncode != 0:
            raise PwError(f"{shlex.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout

    def graph(self) -> Graph:
        if self.dry_run:
            return Graph()
        return parse_dump(self._run(["pw-dump"], timeout=4.0))

    def set_volume(self, node_id: int, volume: float) -> None:
        cmd = ["wpctl", "set-volume", str(node_id), f"{volume:.4f}"]
        if self.dry_run:
            log.info("dry-run: %s", shlex.join(cmd))
            return
        self._run(cmd, timeout=2.0)

    def set_mute(self, node_id: int, mute: bool) -> None:
        cmd = ["wpctl", "set-mute", str(node_id), "1" if mute else "0"]
        if self.dry_run:
            log.info("dry-run: %s", shlex.join(cmd))
            return
        self._run(cmd, timeout=2.0)

    def set_force_quantum(self, frames: int) -> None:
        """Change the buffer size of the whole audio system, immediately.

        0 hands the decision back to PipeWire. This is the value everything in
        the graph runs at, which is why it belongs here and not on our streams.
        """
        cmd = ["pw-metadata", "-n", "settings", "0", "clock.force-quantum", str(int(frames))]
        if self.dry_run:
            log.info("dry-run: %s", shlex.join(cmd))
            return
        self._run(cmd, timeout=3.0)

    def dropouts(self, seconds: float = 2.0) -> dict[str, Any]:
        """Ask pw-top how many periods were missed. Several samples are taken so
        the difference between them shows what is happening right now, not what
        has accumulated since each node started."""
        samples = max(2, min(8, int(seconds) + 1))
        out = self._run(["pw-top", "-b", "-n", str(samples)], timeout=max(10.0, samples + 8))
        result = parse_pw_top(out)
        result["window"] = max(1, samples - 1)
        return result

    def set_default(self, node_id: int) -> None:
        cmd = ["wpctl", "set-default", str(node_id)]
        if self.dry_run:
            log.info("dry-run: %s", shlex.join(cmd))
            return
        self._run(cmd, timeout=2.0)

    def clear_stream_target(self, node_id: int) -> None:
        """Forget a target the session manager remembered for this stream.

        WirePlumber's restore-stream writes the target a user chose in a mixer
        app into the 'default' metadata for that node; the metadata wins over
        our target.object property, so a single accidental drag would otherwise
        stick forever, across restarts."""
        for key in ("target.object", "target.node"):
            cmd = ["pw-metadata", "-d", str(node_id), key]
            if self.dry_run:
                log.info("dry-run: %s", shlex.join(cmd))
                continue
            try:
                self._run(cmd, timeout=2.0)
            except PwError as exc:
                log.debug("clearing %s on node %d: %s", key, node_id, exc)

    def spawn_loopback(self, spec: LoopbackSpec) -> Process:
        cmd = spec.command()
        if self.dry_run:
            log.info("dry-run: %s", shlex.join(cmd))
            return _DryProcess()
        log.info("spawn: %s", shlex.join(cmd))
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise PwError("pw-loopback not found; install pipewire-bin") from exc
        except OSError as exc:
            raise PwError(f"cannot start pw-loopback: {exc}") from exc
        return DrainedProcess(proc)


class DrainedProcess:
    """Popen wrapper whose stderr is drained by a background thread into a
    bounded buffer. A child that logs a lot must never block on a full pipe:
    for pw-loopback that would stall the audio it carries."""

    def __init__(self, proc: subprocess.Popen, keep_lines: int = 20):
        self._proc = proc
        self.pid = proc.pid
        # A usage error puts the message on the FIRST line and then prints the
        # whole help text, so keeping only the tail loses the cause.
        self._head: list[str] = []
        self._lines: collections.deque[str] = collections.deque(maxlen=keep_lines)
        self._thread = threading.Thread(target=self._drain, name=f"stderr-{proc.pid}", daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        stream = self._proc.stderr
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if len(self._head) < 5:
                    self._head.append(line)
                else:
                    self._lines.append(line)
        except (OSError, ValueError):
            pass

    @property
    def stderr_head(self) -> str:
        return "\n".join(self._head)

    @property
    def stderr_tail(self) -> str:
        return "\n".join([*self._head, *self._lines])

    def poll(self) -> int | None:
        return self._proc.poll()

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()

    def wait(self, timeout: float | None = None) -> int:
        return self._proc.wait(timeout=timeout)


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
        # a healthy system: session manager connected, default metadata present
        self._graph.clients.add("WirePlumber")
        self._graph.has_default_metadata = True
        self._next_id = 30
        self.calls: list[tuple] = []
        self.processes: dict[int, tuple[FakeProcess, LoopbackSpec]] = {}
        for name, media_class in devices or []:
            self.add_device(name, media_class)

    def add_device(self, name: str, media_class: str, description: str = "", props: dict[str, Any] | None = None) -> Node:
        node = Node(id=self._next_id, name=name, media_class=media_class, description=description or name, props=dict(props or {}))
        node.props.setdefault("node.name", name)
        node.props.setdefault("media.class", media_class)
        self._next_id += 1
        self._graph.nodes[node.id] = node
        return node

    def add_physical(self, description: str, bus: str, mic: str | None, out: str | None, form_factor: str = "", extra: dict[str, Any] | None = None) -> Device:
        """Add a hardware device with optional microphone and output nodes."""
        extra = dict(extra or {})
        dev = Device(id=self._next_id, name=f"alsa_card.{description}", description=description, bus=bus, form_factor=form_factor, api="alsa")
        dev.props = {
            "device.description": description, "device.bus": bus, "device.form-factor": form_factor,
            "device.serial": extra.pop("device.serial", description.replace(" ", "_")),
            "device.bus-path": extra.pop("device.bus-path", f"pci-0000:00:14.0-usb-0:{dev.id % 9 + 1}:1.0" if bus == "usb" else f"pci-0000:03:0{dev.id % 9}.0"),
            "device.vendor.id": extra.pop("device.vendor.id", str(abs(hash(description.split(' (')[0])) % 9999)),
            "device.product.id": extra.pop("device.product.id", "0001"),
            **{k: v for k, v in extra.items() if k.startswith("alsa.") or k.startswith("device.")},
        }
        self._next_id += 1
        self._graph.devices[dev.id] = dev
        props = {"device.id": dev.id, "device.api": "alsa", **extra}
        # a believable format, so the demo shows what the real machine shows:
        # USB headset microphones are mono, everything else stereo, all at 48 kHz
        fmt = {"node.rate": "1/48000", "device.profile.name": "analog-stereo",
               "device.profile.description": "Analog Stereo"}
        if mic:
            n = self.add_device(mic, "Audio/Source", f"{description} Mono",
                                {**props, **fmt, "audio.channels": 1 if bus == "usb" else 2})
            n.volume, n.mute = 1.0, False
        if out:
            n = self.add_device(out, "Audio/Sink", f"{description} Analog Stereo",
                                {**props, **fmt, "audio.channels": 2})
            n.volume, n.mute = 1.0, False
        return dev

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

    def set_force_quantum(self, frames: int) -> None:
        self.calls.append(("force_quantum", frames))
        self._graph.settings["clock.force-quantum"] = str(frames)
        if frames:
            self._graph.settings["clock.quantum"] = str(frames)

    def dropouts(self, seconds: float = 2.0) -> dict[str, Any]:
        return {"available": True, "errors": 0, "nodes": [], "drivers": []}

    def set_default(self, node_id: int) -> None:
        self.calls.append(("set_default", node_id))
        node = self._graph.nodes[node_id]
        key = "default.audio.sink" if node.media_class.startswith("Audio/Sink") else "default.audio.source"
        self._graph.defaults[key] = node.name

    def clear_stream_target(self, node_id: int) -> None:
        self.calls.append(("clear_target", node_id))

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


# --------------------------------------------------------------------------- #
# Orphan cleanup
# --------------------------------------------------------------------------- #

OWNED_EXECUTABLES = ("pw-loopback", "pw-record")
OWNED_MARKERS = ("tfcz.",)


def is_owned_helper(argv: list[str]) -> bool:
    """True only for a pw-loopback/pw-record process that this daemon spawned.

    Matches the exact shapes we produce (-n tfcz.… / node.name = "tfcz.…") so a
    user's own experiment that merely mentions one of our nodes, for example
    `pw-record --target tfcz.obsmic test.wav`, is never killed."""
    if not argv:
        return False
    exe = os.path.basename(argv[0])
    if exe not in OWNED_EXECUTABLES:
        return False
    for i, arg in enumerate(argv[1:], start=1):
        if arg == "-n" and i + 1 < len(argv) and argv[i + 1].startswith("tfcz."):
            return True
        if arg.startswith("-n") and arg[2:].startswith("tfcz."):
            return True
        if 'node.name = "tfcz.' in arg:
            return True
    return False


def find_stale_helpers(proc_root: str = "/proc", exclude_pids: set[int] | None = None) -> list[int]:
    """PIDs of leftover helper processes from a previous daemon instance."""
    exclude = exclude_pids or set()
    uid = os.getuid()
    found: list[int] = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in exclude or pid == os.getpid():
            continue
        try:
            if os.stat(f"{proc_root}/{entry}").st_uid != uid:
                continue
            with open(f"{proc_root}/{entry}/cmdline", "rb") as fh:
                argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        if is_owned_helper(argv):
            found.append(pid)
    return found


def kill_stale_helpers(exclude_pids: set[int] | None = None, settle: float = 0.5) -> list[int]:
    killed: list[int] = []
    for pid in find_stale_helpers(exclude_pids=exclude_pids):
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except OSError:
            continue
    if killed:
        log.warning("terminated %d leftover helper process(es) from a previous run: %s", len(killed), killed)
        # give the old nodes time to disappear so our own wait-for-node does not
        # latch onto a dying one
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline:
            if not any(os.path.exists(f"/proc/{pid}") for pid in killed):
                break
            time.sleep(0.05)
    return killed


# --------------------------------------------------------------------------- #
# Device identity: resolve config matchers to live nodes, explain how a
# device is recognised, and label USB ports for humans.
# --------------------------------------------------------------------------- #


def node_identity_props(node: Node, graph: Graph) -> dict[str, str]:
    """Node props merged with the props of the hardware device it belongs to."""
    merged: dict[str, str] = {}
    dev_id = node.props.get("device.id")
    if dev_id is not None:
        try:
            device = graph.devices.get(int(dev_id))
        except (TypeError, ValueError):
            device = None
        if device is not None:
            merged.update({k: str(v) for k, v in device.props.items()})
    merged.update({k: str(v) for k, v in node.props.items()})
    merged["kind"] = "input" if node.media_class.startswith("Audio/Source") else "output" if node.media_class.startswith("Audio/Sink") else "other"
    return merged


def resolve_match(match: dict[str, str], graph: Graph) -> list[Node]:
    """All audio nodes whose (node + device) properties equal every match entry."""
    found: list[Node] = []
    for node in graph.audio_devices():
        if node.name.startswith("tfcz."):
            continue
        props = node_identity_props(node, graph)
        if all(props.get(k) == str(v) for k, v in match.items()):
            found.append(node)
    found.sort(key=lambda n: n.name)
    return found


def port_label(bus_path: str) -> str:
    """'pci-0000:00:14.0-usb-0:3.2:1.0' -> 'USB port 3.2'."""
    if not bus_path:
        return ""
    if "-usb-" in bus_path:
        tail = bus_path.split("-usb-", 1)[1]  # 0:3.2:1.0
        parts = tail.split(":")
        if len(parts) >= 2 and parts[1]:
            return f"USB-Anschluss {parts[1]}"
        return "USB-Anschluss"
    if bus_path.startswith("pci-"):
        return "interner Steckplatz " + bus_path[4:]
    return bus_path


def identity_for(node: Node, graph: Graph) -> dict[str, Any]:
    """Decide how to recognise this node again later.

    * 'serial': the device reports a serial number no other visible device of
      the same model shares -> works in any USB port.
    * 'port':   identical devices without usable serial -> only the physical
      USB port tells them apart.
    * 'name':   fall back to the PipeWire node name (built-in / PCI cards).
    Returns match dict, strategy and a plain-language explanation.
    """
    props = node_identity_props(node, graph)
    kind = props["kind"]
    serial = props.get("device.serial", "")
    bus_path = props.get("device.bus-path", "")
    vendor, product = props.get("device.vendor.id", ""), props.get("device.product.id", "")
    bus = props.get("device.bus", "")
    same_model: list[Node] = []
    for other in graph.audio_devices():
        if other.name.startswith("tfcz.") or other.media_class != node.media_class:
            continue
        op = node_identity_props(other, graph)
        if (vendor and product and (op.get("device.vendor.id"), op.get("device.product.id")) == (vendor, product)) or (
            not (vendor and product) and serial and op.get("device.serial") == serial
        ):
            same_model.append(other)
    twins = [o for o in same_model if o.id != node.id]
    serial_shared = any(node_identity_props(o, graph).get("device.serial", "") == serial for o in twins)
    # A serial that is just vendor_product (no real serial part) repeats for identical devices.
    serial_ok = bool(serial) and bus in ("usb", "bluetooth", "bluez5", "") and not serial_shared and (bus_path or bus)

    if serial_ok and (twins or bus in ("usb", "bluetooth", "bluez5")):
        return {
            "match": {"device.serial": serial, "kind": kind},
            # if another device of the same model shows up later and shares the
            # serial, this is what tells them apart
            "prefer": {"device.bus-path": bus_path} if bus_path else {},
            "strategy": "serial",
            "port": port_label(bus_path),
            "text": "Wird an der Seriennummer erkannt. Jeder USB-Anschluss funktioniert."
            + ("" if twins else " Hinweis: es ist nur ein Gerät dieses Modells angesteckt, die Seriennummer muss also nicht eindeutig sein. Kommt später ein zweites gleiches dazu, richte die Geräte nochmals ein."),
        }
    if bus_path and bus in ("usb", "bluetooth", "bluez5", "") and "usb" in bus_path:
        why = (
            "Die zwei gleichen Geräte melden keine unterscheidbare Seriennummer, der Computer kann sie also nur am USB-Anschluss auseinanderhalten."
            if twins
            else "Dieses Gerät meldet keine brauchbare Seriennummer und wird deshalb am USB-Anschluss erkannt, in dem es steckt."
        )
        return {
            "match": {"device.bus-path": bus_path, "kind": kind},
            "strategy": "port",
            "port": port_label(bus_path),
            "text": f"{why} Lass es in {port_label(bus_path)}; beschrifte Stecker und Anschluss.",
        }
    own_device = None
    own_id = node.props.get("device.id")
    if own_id is not None:
        try:
            own_device = graph.devices.get(int(own_id))
        except (TypeError, ValueError):
            own_device = None
    if is_hdmi_capture(node, own_device):
        return {
            "match": {},
            "prefer": {},
            "strategy": "name",
            "port": port_label(bus_path),
            "text": (
                "Wird am Namen im Tonsystem erkannt. Aufnahmekarten werden in der Reihenfolge nummeriert, in der "
                "das System sie findet; die Eingänge können nach einem Neustart also tauschen, solange die "
                "Reihenfolge nicht festgenagelt ist (siehe docs/hdmi-capture.md)."
            ),
        }
    return {
        "match": {},
        "prefer": {},
        "strategy": "name",
        "port": port_label(bus_path),
        "text": "Wird am festen Namen im Tonsystem erkannt (eingebaute oder PCI-Hardware).",
    }


# --------------------------------------------------------------------------- #
# Exclusive use detection: who has the ALSA device open?
# --------------------------------------------------------------------------- #

PIPEWIRE_COMMS = {"pipewire", "pipewire-pulse", "wireplumber"}


def _comm(pid: int, proc_root: str) -> str:
    try:
        with open(f"{proc_root}/{pid}/comm", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def alsa_usage(node: Node, proc_root: str = "/proc") -> dict[str, Any]:
    """Read /proc/asound/cardX/pcmYc|p/subZ/status for the node's ALSA device.

    Returns {open, owner_pid, owner, exclusive}. ``exclusive`` is True when a
    process other than PipeWire holds the device, which means PipeWire (and
    therefore this router and every other program) cannot use it.
    """
    fake = node.props.get("tfcz.fake.owner")
    if fake:
        return {"open": True, "owner_pid": 0, "owner": str(fake), "exclusive": True}
    card = node.props.get("alsa.card") or node.props.get("api.alsa.pcm.card")
    if card is None or node.props.get("device.api", "alsa") != "alsa":
        return {"open": False, "owner_pid": None, "owner": "", "exclusive": False}
    device = node.props.get("alsa.device", "0")
    sub = node.props.get("alsa.subdevice", "0")
    stream = node.props.get("api.alsa.pcm.stream") or ("capture" if node.media_class.startswith("Audio/Source") else "playback")
    path = f"{proc_root}/asound/card{card}/pcm{device}{'c' if stream == 'capture' else 'p'}/sub{sub}/status"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {"open": False, "owner_pid": None, "owner": "", "exclusive": False}
    if text.strip().startswith("closed"):
        return {"open": False, "owner_pid": None, "owner": "", "exclusive": False}
    pid = None
    for line in text.splitlines():
        if line.startswith("owner_pid"):
            try:
                pid = int(line.split(":", 1)[1].strip())
            except ValueError:
                pid = None
    owner = _comm(pid, proc_root) if pid else ""
    return {"open": True, "owner_pid": pid, "owner": owner, "exclusive": bool(owner) and owner not in PIPEWIRE_COMMS}


# --------------------------------------------------------------------------- #
# pw-top parsing (dropout counters) and the system buffer size
# --------------------------------------------------------------------------- #

QUANTUM_CHOICES = (0, 128, 256, 512, 1024, 2048)


PW_TOP_ROW = re.compile(
    r"^(?P<lead>\s*)(?P<state>[SRIEsrie*!]+)\s+(?P<id>\d+)\s+(?P<quantum>\d+)\s+(?P<rate>\d+)"
    r"\s+(?P<wait>\S+)\s+(?P<busy>\S+)\s+(?P<wq>\S+)\s+(?P<bq>\S+)\s+(?P<err>\d+)\s*(?P<rest>.*)$"
)


def _pw_top_tables(text: str) -> list[list[dict[str, Any]]]:
    """Split pw-top's batch output into its successive tables."""
    tables: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        tokens = line.split()
        if "ERR" in tokens and "ID" in tokens:
            current = []
            tables.append(current)
            continue
        if current is None:
            continue
        m = PW_TOP_ROW.match(line)
        if not m:
            continue
        rest = m.group("rest").split()
        if not rest:
            continue
        current.append({
            "id": int(m.group("id")),
            "quantum": int(m.group("quantum")),
            "rate": int(m.group("rate")),
            "errors": int(m.group("err")),
            "name": rest[-1],
            "format": " ".join(t for t in rest[:-1] if t != "+"),
            "driver": "+" not in rest,
            "active": m.group("state").upper().startswith("R"),
        })
    return [t for t in tables if t]


def parse_pw_top(text: str) -> dict[str, Any]:
    """Read pw-top's batch output.

    Two numbers matter and they mean different things. ``errors`` is what the
    node has lost since it started, so a node that has been up since boot looks
    far worse than one restarted a minute ago. ``delta`` is what it lost between
    the first and the last table, which is the only fair comparison.
    """
    tables = _pw_top_tables(text)
    if not tables:
        return {"available": False, "errors": 0, "delta": 0, "nodes": [], "drivers": [], "rows": [], "samples": 0}
    # with a single table there is nothing to compare against, so the total is
    # the only figure available
    first = {r["name"]: r for r in tables[0]} if len(tables) > 1 else {}
    rows = []
    for row in tables[-1]:
        before = first.get(row["name"])
        row = dict(row)
        row["delta"] = max(0, row["errors"] - before["errors"]) if before else row["errors"]
        rows.append(row)
    return {
        "available": True,
        "errors": sum(r["errors"] for r in rows),
        "delta": sum(r["delta"] for r in rows),
        "samples": len(tables),
        "rows": rows,
        "nodes": sorted((r for r in rows if r["delta"] or r["errors"]), key=lambda r: (-r["delta"], -r["errors"])),
        "drivers": [r["name"] for r in rows if r["driver"]],
    }


ROUTER_PREFIX = "tfcz."


def classify_node(name: str, node: Node | None, graph: Graph) -> str:
    """Who owns this node: this router, real hardware, a filter chain someone
    configured, or an ordinary application."""
    if name.startswith(ROUTER_PREFIX):
        return "router"
    if name in ("Dummy-Driver", "Freewheel-Driver", "Midi-Bridge"):
        return "system"
    by_name = _classify_by_name(name)
    if by_name in ("filter", "device"):
        # capture.* / playback.* / *-clean belong to a filter chain, alsa_* and
        # bluez_* to hardware; both are clear from the name alone
        return by_name
    if node is None:
        return by_name
    props = node.props
    if props.get("device.id") is not None or str(props.get("device.api", "")) in ("alsa", "bluez5", "v4l2"):
        return "device"
    media = node.media_class
    if media.startswith("Stream/"):
        return "app"
    if media in ("Audio/Sink", "Audio/Source", "Audio/Source/Virtual", "Audio/Duplex"):
        # a virtual sink/source with no hardware behind it: filter-chain, loopback,
        # echo-cancel and similar, set up outside this router
        return "filter"
    if "filter" in str(props.get("node.name", "")) or str(props.get("media.name", "")).startswith("filter"):
        return "filter"
    return by_name


FILTER_HINTS = ("filter-chain", "-clean", "-sidetone", "echo-cancel", "noise", "rnnoise")


def _classify_by_name(name: str) -> str:
    """Fallback when the node is not in the graph dump, which happens when it
    appeared or vanished between the two measurements."""
    low = name.lower()
    if low.startswith(("alsa_input.", "alsa_output.", "bluez_input.", "bluez_output.", "v4l2_")):
        return "device"
    if low.startswith(("capture.", "playback.", "input.", "output.", "effect_")) or any(h in low for h in FILTER_HINTS):
        return "filter"
    if name and name[0].isupper():
        return "app"  # applications register under their own name: OBS, Firefox, TeamViewer
    return "other"


def quantum_state(graph: Graph) -> dict[str, Any]:
    """What buffer size the audio system runs at, in frames and milliseconds."""

    def num(key: str, default: int = 0) -> int:
        try:
            return int(float(graph.settings.get(key, default)))
        except (TypeError, ValueError):
            return default

    rate = num("clock.force-rate") or num("clock.rate", 48000) or 48000
    forced = num("clock.force-quantum")
    quantum = forced or num("clock.quantum", 1024) or 1024
    return {
        "quantum": quantum,
        "rate": rate,
        "ms": round(quantum * 1000 / rate, 1),
        "forced": bool(forced),
        "min": num("clock.min-quantum", 32),
        "max": num("clock.max-quantum", 2048),
        "known": bool(graph.settings),
        "choices": [
            {"frames": c, "ms": round(c * 1000 / rate, 1) if c else None}
            for c in QUANTUM_CHOICES
        ],
    }


def quantum_drop_in() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pipewire" / "pipewire.conf.d" / "10-tfcz-quantum.conf"


def persist_quantum(frames: int) -> Path | None:
    """Write (or remove) a PipeWire drop-in so the buffer size survives a restart."""
    path = quantum_drop_in()
    if not frames:
        try:
            path.unlink()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PwError(f"cannot remove {path}: {exc}") from exc
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# written by tfcz-audio; delete this file to go back to the system default\n"
            "context.properties = {\n"
            f"    default.clock.quantum = {int(frames)}\n"
            "}\n"
        )
    except OSError as exc:
        raise PwError(f"cannot write {path}: {exc}") from exc
    return path
