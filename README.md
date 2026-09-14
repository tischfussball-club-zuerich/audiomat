# tfcz-audio

Audio router for the TFCZ streaming PC. It wires two USB headsets and the
HDMI capture card together on top of PipeWire and exposes the result to OBS
and to Advanced Scene Switcher:

* headset A mic -> headset B ears, and the other way round (intercom)
* HDMI program audio -> both headsets
* both headset mics -> one virtual microphone ("TFCZ OBS Mic") for OBS
* a volume and mute per route, changeable at runtime over HTTP or CLI
* presets to switch several routes at once (e.g. "hdmi_off" during a talk)

There is no audio code in this project. Every route is a `pw-loopback`
process owned by the daemon, so PipeWire does mixing, resampling and
reconnecting after a USB replug. The daemon is the control plane only.

```
 headset A mic ─┬─► [a_to_b] ─────────────► headset B out
                └─► [a_to_obs] ─┐
 headset B mic ─┬─► [b_to_a] ───┼─────────► headset A out
                └─► [b_to_obs] ─┤
 HDMI in 1 ─────┬─► [hdmi_to_a] ┼─────────► headset A out
                └─► [hdmi_to_b] ┼─────────► headset B out
                                └──► tfcz.obsmix ─► "TFCZ OBS Mic" ─► OBS
   [route] = pw-loopback with its own volume/mute, controlled by the daemon
```

## Requirements

* Ubuntu 22.10 or newer (PipeWire + WirePlumber as the session audio
  server; Ubuntu 24.04 LTS is the tested target). Packages: `pipewire-bin`,
  `wireplumber`, `python3` (3.11+), `python3-venv`.
* The AVMatrix VC42 DKMS driver so the HDMI inputs show up as ALSA capture
  devices. See [docs/hdmi-capture.md](docs/hdmi-capture.md).
* OBS Studio with the Advanced Scene Switcher plugin (optional, for
  automation). See [docs/advanced-scene-switcher.md](docs/advanced-scene-switcher.md).

## Install

As the desktop user (not root), in this directory:

```
./install.sh
```

This copies the package to `~/.local/share/tfcz-audio` (no pip, no
network needed), writes the `~/.local/bin/tfcz-audio` wrapper, installs the
systemd **user** unit and writes an example config to
`~/.config/tfcz-audio/config.toml` if none exists. Re-run it to upgrade;
`./uninstall.sh` removes everything except config and state, `./uninstall.sh
--purge` removes those too. `pip install .` works too if you prefer a venv.

Then:

```
tfcz-audio devices          # list PipeWire sources/sinks and their node names
$EDITOR ~/.config/tfcz-audio/config.toml    # fill in [devices]
tfcz-audio check            # validates config, flags missing devices
systemctl --user start tfcz-audio
tfcz-audio status
journalctl --user -u tfcz-audio -f
```

If the machine should route audio without anyone logging in, enable
lingering so the user session (and PipeWire) starts at boot:

```
loginctl enable-linger $USER
```

## Configuration

`~/.config/tfcz-audio/config.toml`, see the annotated
[example](config/tfcz-audio.example.toml).

* `[devices]` maps friendly aliases to PipeWire `node.name` values.
* `[routes.<name>]` has `from`, `to`, `volume` (default 1.0), `mute`,
  `description`. `to = "obs_mic"` targets the virtual OBS microphone.
  `capture_sink = true` captures a sink's monitor instead of a source
  (e.g. to route OBS's own monitor output somewhere).
* `[presets.<name>]` lists routes with a volume, or `{ volume = , mute = }`.
* `[api]` `listen`, `port`, optional `token`. Keep it on 127.0.0.1 unless
  you set a token.
* `[audio]` `latency = "256/48000"` per hop (~5 ms). Lower to `128/48000`
  if the intercom feels laggy and the USB headsets keep up.

Volumes use the wpctl / pavucontrol scale: 1.0 is unity, 0.5 is about
-18 dB, maximum 1.5. The API also accepts `volume_db`.

Changed volumes are saved to `~/.local/state/tfcz-audio/state.json` and
restored on restart. `POST /reset` or `tfcz-audio reset` goes back to the
config values. Set `state_file = false` at the top of the config to disable.

Route names become PipeWire node names `tfcz.<route>.in` / `.out`, so you can
see and tweak every route in `pavucontrol`, `qpwgraph` or `wpctl status` too.

## OBS

1. Sources -> Audio Input Capture (PipeWire) -> device **TFCZ OBS Mic**.
2. Do not also add the headsets as separate OBS inputs, or they will be
   heard twice.
3. Video from the VC42 stays a normal "Video Capture Device (V4L2)"
   source with its audio disabled; the audio comes through PipeWire.

## CLI

```
tfcz-audio status                       routes, devices, virtual mic
tfcz-audio set hdmi_to_a --volume 0.3   or --db -10, --mute, --unmute, --toggle
tfcz-audio preset hdmi_off              apply a preset; no name lists them
tfcz-audio reset                        back to config values
tfcz-audio devices [-p]                 PipeWire audio nodes (+ ALSA card props)
tfcz-audio check                        validate config, report missing devices
tfcz-audio run [--dry-run]              run the daemon in the foreground
```

## HTTP API

Base URL `http://127.0.0.1:8787`. All responses are JSON. Writes accept a
JSON body, form data, query parameters or path segments, so any HTTP
client can drive it.

| Method | Path | Effect |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/status` | devices present, virtual mic, all routes, presets |
| GET | `/routes` | all routes |
| GET | `/routes/{r}` | one route: desired and actual volume/mute, `connected` |
| PUT/POST | `/routes/{r}` | body `{"volume": 0.3}` and/or `{"mute": true}` or `{"volume_db": -6}` |
| POST | `/routes/{r}/volume/{v}` | set volume without a body |
| POST | `/routes/{r}/volume_db/{db}` | set volume in dB without a body |
| POST | `/routes/{r}/mute` `/unmute` `/toggle` | mute control |
| GET | `/presets` | list presets |
| POST | `/presets/{p}` | apply a preset |
| POST | `/reset` | all routes back to config values |
| GET | `/devices` | audio sources/sinks currently in PipeWire |

Examples:

```
curl -X PUT localhost:8787/routes/hdmi_to_a -d '{"volume": 0.3}'
curl -X POST localhost:8787/routes/a_to_obs/mute
curl -X POST localhost:8787/presets/hdmi_quiet
curl -X POST 'localhost:8787/routes/hdmi_to_b?volume=0.5&mute=false'
```

With `token` set, send `Authorization: Bearer <token>` or `?token=<token>`.

## How it works

* On start the daemon spawns one `pw-loopback` for the virtual mic (an
  `Audio/Sink` mix bus feeding an `Audio/Source/Virtual` node) and one per
  route, each with `target.object` set to the configured node names and
  `node.dont-fallback = true` so a missing headset leaves the route silent
  instead of falling back to the default device.
* Volume and mute are applied to the route's playback stream with `wpctl`;
  the graph is read with `pw-dump`.
* A supervisor pass runs every second: restarts loopbacks that died (with
  backoff), and re-applies desired volumes once a node exists.
* WirePlumber relinks the streams itself when a USB device disappears and
  comes back.

## Troubleshooting

* `tfcz-audio status` shows `linked no` for a route: one of its devices is
  missing (`[MISSING]` in the device list) or the node name changed. Compare
  with `tfcz-audio devices`.
* Headset mic is quiet or clipping: set the *device* input level with
  `wpctl set-volume <id> 1.0` or pavucontrol; route volumes sit on top of it.
* The mix bus "TFCZ OBS Mic (mix bus)" became the default output: pick your
  real default with `wpctl set-default <id>`, or in the sound settings.
* Four identical HDMI inputs: pin their names with a WirePlumber rule, see
  [docs/hdmi-capture.md](docs/hdmi-capture.md).
* Live graph: `pw-top` (loads/xruns), `pw-link -l` (links), `qpwgraph` (GUI).
* Logs: `journalctl --user -u tfcz-audio -f`. Use
  `tfcz-audio -v run` for command-level debug output.

## Development

Stdlib only, no third-party dependencies.

```
python3 -m unittest discover -s tests -t .
python3 -m tfcz_audio -c config/tfcz-audio.example.toml run --dry-run
```

`--dry-run` logs the exact `pw-loopback` and `wpctl` commands instead of
executing them, which also works on machines without PipeWire.
