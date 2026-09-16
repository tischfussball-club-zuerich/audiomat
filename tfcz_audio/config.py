"""Configuration loading and validation (TOML, stdlib only)."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Special sink name a route may target: the virtual microphone consumed by OBS.
OBS_MIC = "obs_mic"

ROUTE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
MAX_VOLUME = 1.5


class ConfigError(Exception):
    pass


@dataclass
class ApiConfig:
    listen: str = "127.0.0.1"
    port: int = 8787
    token: str = ""


@dataclass
class AudioConfig:
    # One buffer per hop. 1024 frames is about 21 ms, which is inaudible for an
    # intercom and survives several USB devices running on different clocks.
    # Lower it only if the delay is noticeable and `pw-top` shows no xruns.
    latency: str = "1024/48000"
    channels: int = 2
    meters: bool = True  # level bars; they cost one pw-record per device


@dataclass
class VirtualConfig:
    obs_mix_name: str = "tfcz.obsmix"
    obs_mic_name: str = "tfcz.obsmic"
    obs_mic_description: str = "TFCZ OBS Mic"


@dataclass
class DeviceSpec:
    """How to find a device. Either a fixed PipeWire node.name, or a property
    matcher (e.g. device.serial or device.bus-path) resolved at runtime so the
    device keeps its identity across reboots and USB ports."""

    alias: str
    node: str = ""
    match: dict[str, str] = field(default_factory=dict)
    # not required to match; used only to pick between several candidates that
    # all match (two identical headsets sharing a model-wide "serial")
    prefer: dict[str, str] = field(default_factory=dict)

    @property
    def is_static(self) -> bool:
        return bool(self.node)

    def to_value(self) -> Any:
        if self.is_static:
            return self.node
        value: dict[str, Any] = {"match": dict(self.match)}
        if self.prefer:
            value["prefer"] = dict(self.prefer)
        return value


@dataclass
class RouteConfig:
    name: str
    source: str  # PipeWire node.name of the capture device
    sink: str  # PipeWire node.name of the playback device, or OBS_MIC
    volume: float = 1.0
    mute: bool = False
    description: str = ""
    capture_sink: bool = False  # capture a sink's monitor instead of a source
    source_ref: str = ""  # 'from' as written in the config (alias or node name)
    sink_ref: str = ""  # 'to' as written in the config

    @property
    def in_node(self) -> str:
        return f"tfcz.{self.name}.in"

    @property
    def out_node(self) -> str:
        return f"tfcz.{self.name}.out"


@dataclass
class PresetEntry:
    volume: float | None = None
    mute: bool | None = None


@dataclass
class Config:
    api: ApiConfig = field(default_factory=ApiConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    virtual: VirtualConfig = field(default_factory=VirtualConfig)
    devices: dict[str, DeviceSpec] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)  # alias or alias prefix -> friendly name
    routes: dict[str, RouteConfig] = field(default_factory=dict)
    presets: dict[str, dict[str, PresetEntry]] = field(default_factory=dict)
    state_file: Path | None = None
    path: Path | None = None


def default_config_paths() -> list[Path]:
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return [Path(xdg) / "tfcz-audio" / "config.toml", Path("/etc/tfcz-audio/config.toml")]


def default_state_file() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg) / "tfcz-audio" / "state.json"


def find_config(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p
    for p in default_config_paths():
        if p.is_file():
            return p
    raise ConfigError(
        "no config file found; looked in "
        + ", ".join(str(p) for p in default_config_paths())
        + ". Run 'tfcz-audio init-config' to create one."
    )


def load(path: Path) -> Config:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc.strerror or exc}") from exc
    try:
        cfg = parse(data)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: invalid value: {exc}") from exc
    cfg.path = path
    return cfg


def _volume(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: volume must be a number, got {value!r}")
    v = float(value)
    if not 0.0 <= v <= MAX_VOLUME:
        raise ConfigError(f"{where}: volume must be between 0 and {MAX_VOLUME}, got {v}")
    return v


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true/false, got {value!r}")
    return value


def _section(data: dict, key: str) -> dict:
    sec = data.get(key, {})
    if not isinstance(sec, dict):
        raise ConfigError(f"[{key}] must be a table")
    return sec


def parse(data: dict) -> Config:
    cfg = Config()

    api = _section(data, "api")
    cfg.api = ApiConfig(
        listen=str(api.get("listen", cfg.api.listen)),
        port=int(api.get("port", cfg.api.port)),
        token=str(api.get("token", "")),
    )

    audio = _section(data, "audio")
    cfg.audio = AudioConfig(
        latency=str(audio.get("latency", cfg.audio.latency)),
        channels=int(audio.get("channels", cfg.audio.channels)),
        meters=_bool(audio.get("meters", True), "[audio] meters"),
    )
    if not re.fullmatch(r"\d+/\d+", cfg.audio.latency):
        raise ConfigError("[audio] latency must look like '256/48000'")

    virt = _section(data, "virtual")
    cfg.virtual = VirtualConfig(
        obs_mix_name=str(virt.get("obs_mix_name", cfg.virtual.obs_mix_name)),
        obs_mic_name=str(virt.get("obs_mic_name", cfg.virtual.obs_mic_name)),
        obs_mic_description=str(virt.get("obs_mic_description", cfg.virtual.obs_mic_description)),
    )
    # The 'tfcz.' prefix is how the daemon tells its own nodes from hardware
    # everywhere (cleanup of leftovers, device lists, loop detection).
    for key in ("obs_mic_name", "obs_mix_name"):
        value = getattr(cfg.virtual, key)
        if not value.startswith("tfcz."):
            raise ConfigError(f"[virtual] {key} must start with 'tfcz.' (got {value!r})")
    if cfg.virtual.obs_mic_name == cfg.virtual.obs_mix_name:
        raise ConfigError("[virtual] obs_mic_name and obs_mix_name must differ")

    devices = _section(data, "devices")
    for key, value in devices.items():
        if key == OBS_MIC:
            raise ConfigError(f"[devices] '{OBS_MIC}' is reserved for the virtual OBS microphone")
        if not ROUTE_NAME_RE.match(key):
            raise ConfigError(f"[devices] '{key}': names must match {ROUTE_NAME_RE.pattern}")
        if isinstance(value, str) and value:
            cfg.devices[key] = DeviceSpec(alias=key, node=value)
        elif isinstance(value, dict) and isinstance(value.get("match"), dict) and value["match"]:
            match = {}
            for mk, mv in value["match"].items():
                if not isinstance(mk, str) or not isinstance(mv, (str, int, float, bool)):
                    raise ConfigError(f"[devices] {key}: match values must be strings")
                match[mk] = str(mv)
            if match.get("kind") not in (None, "input", "output"):
                raise ConfigError(f"[devices] {key}: match.kind must be 'input' or 'output'")
            prefer = {}
            raw_prefer = value.get("prefer")
            if raw_prefer is not None:
                if not isinstance(raw_prefer, dict):
                    raise ConfigError(f"[devices] {key}: prefer must be a table")
                for pk, pv in raw_prefer.items():
                    if not isinstance(pk, str) or not isinstance(pv, (str, int, float, bool)):
                        raise ConfigError(f"[devices] {key}: prefer values must be strings")
                    prefer[pk] = str(pv)
            cfg.devices[key] = DeviceSpec(alias=key, match=match, prefer=prefer)
        else:
            raise ConfigError(f"[devices] {key}: expected a node.name string or {{ match = {{ ... }} }}")

    labels = _section(data, "labels")
    for key, value in labels.items():
        if not isinstance(value, str):
            raise ConfigError(f"[labels] {key}: expected a string")
        cfg.labels[str(key)] = value

    routes = _section(data, "routes")
    for name, spec in routes.items():
        where = f"[routes.{name}]"
        if not ROUTE_NAME_RE.match(name):
            raise ConfigError(f"{where}: route names must match {ROUTE_NAME_RE.pattern}")
        if not isinstance(spec, dict):
            raise ConfigError(f"{where}: must be a table")
        try:
            src, dst = spec["from"], spec["to"]
        except KeyError as exc:
            raise ConfigError(f"{where}: missing key {exc}") from None
        if src == OBS_MIC:
            raise ConfigError(f"{where}: '{OBS_MIC}' can only be used as 'to'")
        if str(src).startswith("tfcz.") or (dst != OBS_MIC and str(dst).startswith("tfcz.")):
            raise ConfigError(f"{where}: the router's own nodes (tfcz.*) cannot be routed; use '{OBS_MIC}' as the target for the OBS microphone")
        # static node names are known now; matcher-based devices resolve at runtime
        source = cfg.devices[src].node if src in cfg.devices else src
        sink = OBS_MIC if dst == OBS_MIC else (cfg.devices[dst].node if dst in cfg.devices else dst)
        cfg.routes[name] = RouteConfig(
            name=name,
            source=source,
            sink=sink,
            volume=_volume(spec.get("volume", 1.0), where),
            mute=_bool(spec.get("mute", False), where),
            description=str(spec.get("description", "")),
            capture_sink=_bool(spec.get("capture_sink", False), where),
            source_ref=str(src),
            sink_ref=str(dst),
        )

    presets = _section(data, "presets")
    for pname, entries in presets.items():
        where = f"[presets.{pname}]"
        if not ROUTE_NAME_RE.match(pname):
            raise ConfigError(f"{where}: preset names must match {ROUTE_NAME_RE.pattern}")
        if not isinstance(entries, dict):
            raise ConfigError(f"{where}: must be a table of route = volume | {{ volume, mute }}")
        preset: dict[str, PresetEntry] = {}
        for rname, value in entries.items():
            if rname not in cfg.routes:
                raise ConfigError(f"{where}: unknown route '{rname}'")
            if isinstance(value, dict):
                entry = PresetEntry()
                if "volume" in value:
                    entry.volume = _volume(value["volume"], f"{where}.{rname}")
                if "mute" in value:
                    entry.mute = _bool(value["mute"], f"{where}.{rname}")
                if entry.volume is None and entry.mute is None:
                    raise ConfigError(f"{where}.{rname}: needs volume and/or mute")
            else:
                entry = PresetEntry(volume=_volume(value, f"{where}.{rname}"))
            preset[rname] = entry
        cfg.presets[pname] = preset

    _reject_cycles(cfg)

    state = data.get("state_file")
    if state is None:
        cfg.state_file = default_state_file()
    elif state is False or state == "":
        cfg.state_file = None
    else:
        cfg.state_file = Path(str(state)).expanduser()

    return cfg


# --------------------------------------------------------------------------- #
# Writing the config back (used by the web UI / config API)
# --------------------------------------------------------------------------- #


def to_dict(cfg: Config) -> dict[str, Any]:
    """Inverse of parse(): a plain dict that parse() accepts again."""
    data: dict[str, Any] = {}
    if cfg.state_file is None:
        data["state_file"] = False
    elif cfg.state_file != default_state_file():
        data["state_file"] = str(cfg.state_file)
    data["api"] = {"listen": cfg.api.listen, "port": cfg.api.port, "token": cfg.api.token}
    data["audio"] = {"latency": cfg.audio.latency, "channels": cfg.audio.channels, "meters": cfg.audio.meters}
    data["virtual"] = {
        "obs_mix_name": cfg.virtual.obs_mix_name,
        "obs_mic_name": cfg.virtual.obs_mic_name,
        "obs_mic_description": cfg.virtual.obs_mic_description,
    }
    data["devices"] = {alias: spec.to_value() for alias, spec in cfg.devices.items()}
    if cfg.labels:
        data["labels"] = dict(cfg.labels)
    routes: dict[str, Any] = {}
    for name, r in cfg.routes.items():
        entry: dict[str, Any] = {}
        if r.description:
            entry["description"] = r.description
        entry["from"] = r.source_ref or r.source
        entry["to"] = r.sink_ref or r.sink
        entry["volume"] = r.volume
        if r.mute:
            entry["mute"] = True
        if r.capture_sink:
            entry["capture_sink"] = True
        routes[name] = entry
    data["routes"] = routes
    presets: dict[str, Any] = {}
    for pname, entries in cfg.presets.items():
        preset: dict[str, Any] = {}
        for rname, e in entries.items():
            if e.mute is None and e.volume is not None:
                preset[rname] = e.volume
            else:
                item: dict[str, Any] = {}
                if e.volume is not None:
                    item["volume"] = e.volume
                if e.mute is not None:
                    item["mute"] = e.mute
                preset[rname] = item
        presets[pname] = preset
    data["presets"] = presets
    return data


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()) + " }"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot serialise {type(value).__name__} to TOML")


HEADER = """# tfcz-audio configuration (generated; comments are not preserved).
# Volumes: 1.0 = 0 dB, 0.5 ~ -18 dB, max 1.5. Node names: `tfcz-audio devices`.
"""


def dumps(data: dict[str, Any]) -> str:
    """Serialise the dict shape produced by to_dict() as TOML."""
    out = [HEADER]
    for key, value in data.items():
        if not isinstance(value, dict):
            out.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for section in ("api", "audio", "virtual", "devices", "labels"):
        table = data.get(section)
        if not isinstance(table, dict):
            continue
        out.append(f"\n[{section}]")
        for k, v in table.items():
            out.append(f"{_toml_key(k)} = {_toml_value(v)}")
    for section in ("routes", "presets"):
        tables = data.get(section) or {}
        for name, table in tables.items():
            out.append(f"\n[{section}.{_toml_key(name)}]")
            for k, v in table.items():
                out.append(f"{_toml_key(k)} = {_toml_value(v)}")
    return "\n".join(out) + "\n"


def save(cfg: Config, path: Path | None = None) -> Path:
    """Validate (via a parse round-trip) and atomically write the config."""
    path = path or cfg.path
    if path is None:
        raise ConfigError("config has no path to save to")
    data = to_dict(cfg)
    text = dumps(data)
    parse(tomllib.loads(text))  # never write something we cannot read back
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            import shutil

            try:
                shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
            except OSError as exc:
                log_backup_failure(path, exc)
        os.replace(tmp, path)  # the live file is only ever replaced atomically, never removed
    except PermissionError as exc:
        raise ConfigError(
            f"cannot write {path}: permission denied. Copy it to ~/.config/tfcz-audio/config.toml (writable) and restart."
        ) from exc
    except OSError as exc:
        raise ConfigError(f"cannot write {path}: {exc.strerror or exc}. Check free disk space and permissions.") from exc
    return path


def log_backup_failure(path: Path, exc: OSError) -> None:
    import logging

    logging.getLogger("tfcz.config").warning("could not write backup of %s: %s", path, exc)


def load_or_recover(path: Path) -> tuple[Config, str]:
    """Load the config; on failure fall back to the .bak copy, then to an
    empty config so the daemon (and its UI) can still start and the user can
    repair or re-run Setup. Returns (config, error message or '')."""
    try:
        return load(path), ""
    except ConfigError as exc:
        primary = str(exc)
    backup = path.with_suffix(path.suffix + ".bak")
    if backup.is_file():
        try:
            cfg = load(backup)
            cfg.path = path
            return cfg, f"{primary}. Started with the previous config from {backup.name}; saving from the UI writes a fresh {path.name}."
        except ConfigError as exc2:
            primary += f" (backup also unusable: {exc2})"
    cfg = Config()
    cfg.path = path
    cfg.state_file = default_state_file()
    return cfg, f"{primary}. Started with NO connections. Fix the file or run Setup in the web UI, which rewrites it."


def _reject_cycles(cfg: Config) -> None:
    """Refuse a configuration in which audio could flow in a circle.

    Only reachable with `capture_sink` routes (capturing what a sink plays and
    sending it on), but such a loop feeds itself and gets louder every pass."""
    edges: dict[str, set[str]] = {}
    for route in cfg.routes.values():
        edges.setdefault(route.source_ref, set()).add(route.sink_ref)
    state: dict[str, int] = {}

    def walk(node: str, path: list[str]) -> None:
        state[node] = 1
        for nxt in sorted(edges.get(node, ())):
            if state.get(nxt) == 1:
                cycle = " -> ".join([*path, node, nxt])
                raise ConfigError(f"[routes] these connections would feed each other in a circle: {cycle}")
            if state.get(nxt) is None:
                walk(nxt, [*path, node])
        state[node] = 2

    for start in sorted(edges):
        if state.get(start) is None:
            walk(start, [])


def human(cfg: Config | None, alias: str) -> str:
    """Friendly name for an alias: user label first, then a readable fallback."""
    if alias == OBS_MIC:
        return "OBS stream"
    labels = cfg.labels if cfg else {}
    if alias in labels:
        return labels[alias]
    base, _, kind = alias.rpartition("_")
    suffix = {"mic": "microphone", "out": "headphones"}.get(kind)
    if base and suffix and base in labels:
        return f"{labels[base]} {suffix}"
    words = alias.replace("-", "_").split("_")
    acr = {"hdmi": "HDMI", "obs": "OBS", "usb": "USB", "pc": "PC", "tv": "TV", "a": "A", "b": "B", "c": "C", "d": "D"}
    out = " ".join(acr.get(w, w) for w in words)
    out = out.replace(" mic", " microphone").replace(" out", " headphones")
    return out[:1].upper() + out[1:]
