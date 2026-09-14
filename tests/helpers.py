from __future__ import annotations

import tomllib
from pathlib import Path

from tfcz_audio.config import Config, parse
from tfcz_audio.pw import FakeBackend

EXAMPLE = Path(__file__).resolve().parents[1] / "tfcz_audio" / "example_config.toml"

MINIMAL = """
[devices]
a_mic = "alsa_input.a"
a_out = "alsa_output.a"
b_mic = "alsa_input.b"
b_out = "alsa_output.b"
hdmi = "alsa_input.hdmi"

[routes.a_to_b]
from = "a_mic"
to = "b_out"
volume = 1.0

[routes.b_to_a]
from = "b_mic"
to = "a_out"

[routes.hdmi_to_a]
from = "hdmi"
to = "a_out"
volume = 0.6

[routes.a_to_obs]
from = "a_mic"
to = "obs_mic"

[presets.quiet]
hdmi_to_a = 0.2
[presets.hdmi_off]
hdmi_to_a = { mute = true }
"""


def minimal_config(extra: str = "", state_file: Path | None = None) -> Config:
    cfg = parse(tomllib.loads(MINIMAL + extra))
    cfg.state_file = state_file
    return cfg


def example_config() -> Config:
    with open(EXAMPLE, "rb") as fh:
        return parse(tomllib.load(fh))


def fake_backend(with_devices: bool = True) -> FakeBackend:
    devices = []
    if with_devices:
        devices = [
            ("alsa_input.a", "Audio/Source"),
            ("alsa_output.a", "Audio/Sink"),
            ("alsa_input.b", "Audio/Source"),
            ("alsa_output.b", "Audio/Sink"),
            ("alsa_input.hdmi", "Audio/Source"),
        ]
    return FakeBackend(devices)
