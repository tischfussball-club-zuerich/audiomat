"""tfcz-audio: a thin control daemon that builds an audio routing matrix on PipeWire.

Every route is one ``pw-loopback`` process. The daemon owns those processes,
applies per-route volume/mute through ``wpctl`` and exposes an HTTP API so
tools like Advanced Scene Switcher (OBS) can change gains at runtime.
"""

__version__ = "0.1.0"
