"""Configuration loading and validation (TOML, stdlib only)."""

from __future__ import annotations

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
    latency: str = "256/48000"
    channels: int = 2


@dataclass
class VirtualConfig:
    obs_mix_name: str = "tfcz.obsmix"
    obs_mic_name: str = "tfcz.obsmic"
    obs_mic_description: str = "TFCZ OBS Mic"


@dataclass
class RouteConfig:
    name: str
    source: str  # PipeWire node.name of the capture device
    sink: str  # PipeWire node.name of the playback device, or OBS_MIC
    volume: float = 1.0
    mute: bool = False
    description: str = ""
    capture_sink: bool = False  # capture a sink's monitor instead of a source

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
    devices: dict[str, str] = field(default_factory=dict)
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
    cfg = parse(data)
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
    )
    if not re.fullmatch(r"\d+/\d+", cfg.audio.latency):
        raise ConfigError("[audio] latency must look like '256/48000'")

    virt = _section(data, "virtual")
    cfg.virtual = VirtualConfig(
        obs_mix_name=str(virt.get("obs_mix_name", cfg.virtual.obs_mix_name)),
        obs_mic_name=str(virt.get("obs_mic_name", cfg.virtual.obs_mic_name)),
        obs_mic_description=str(virt.get("obs_mic_description", cfg.virtual.obs_mic_description)),
    )

    devices = _section(data, "devices")
    for key, value in devices.items():
        if not isinstance(value, str) or not value:
            raise ConfigError(f"[devices] {key}: expected a PipeWire node.name string")
        if key == OBS_MIC:
            raise ConfigError(f"[devices] '{OBS_MIC}' is reserved for the virtual OBS microphone")
        cfg.devices[key] = value

    routes = _section(data, "routes")
    if not routes:
        raise ConfigError("no [routes.*] defined")
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
        source = cfg.devices.get(src, src)
        sink = OBS_MIC if dst == OBS_MIC else cfg.devices.get(dst, dst)
        cfg.routes[name] = RouteConfig(
            name=name,
            source=source,
            sink=sink,
            volume=_volume(spec.get("volume", 1.0), where),
            mute=_bool(spec.get("mute", False), where),
            description=str(spec.get("description", "")),
            capture_sink=_bool(spec.get("capture_sink", False), where),
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

    state = data.get("state_file")
    if state is None:
        cfg.state_file = default_state_file()
    elif state is False or state == "":
        cfg.state_file = None
    else:
        cfg.state_file = Path(str(state)).expanduser()

    return cfg
