# tfcz-audio

Audio router for the TFCZ streaming PC. It wires two USB headsets and the
HDMI capture card together on top of PipeWire and exposes the result to OBS
and to Advanced Scene Switcher:

* headset A mic -> headset B ears, and the other way round (intercom)
* HDMI program audio -> both headsets
* both headset mics -> one virtual microphone ("TFCZ OBS Mic") for OBS
* a volume and mute per route, changeable at runtime over HTTP, CLI or a
  small web UI
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

The installer checks the environment first (desktop session, PipeWire as
the sound server, WirePlumber, tools), starts the service, verifies it is
running and ends with `tfcz-audio doctor`, which lists every remaining
problem with the command that fixes it. Then open the web UI and run the
setup wizard; nothing needs to be edited by hand.

```
tfcz-audio doctor           # tools, session, PipeWire, config, devices, service, linger
tfcz-audio selftest         # record from every device: what arrives, what is linked
tfcz-audio status           # routes and devices as the daemon sees them
journalctl --user -u tfcz-audio -f
```

The installer also makes the router **start at boot without a login**: it
enables lingering for your user (so your user services, including
PipeWire and this daemon, start with the system) and adds you to the
`audio` group (device access before anyone logs in) and the `pipewire`
group (realtime priority for the audio helpers when no desktop session
grants it). Group changes take effect after the next boot. Pass
`--no-boot` to skip that.

A first install writes a minimal starter config with no devices; the web
UI opens the setup wizard automatically. `tfcz-audio init-config
--example PATH` writes the fully annotated example instead.

## Web UI

Open <http://127.0.0.1:8787/> on the streaming PC (or from the LAN if you
change `listen` and set a `token`). The page follows the TFCZ brand guide
(<https://design.tfcz.ch/brandguide>): navy frosted panels on the club's
blue and gold, Nunito Sans, the blue-over-gold brand line, Lucide icons
and no emoji anywhere, custom dropdowns instead of native selects, hover
as an inset brand frame and selection in gold. It speaks German in the
Du-form, like the rest of the club's interfaces. It is written for people
who do not care about audio plumbing:

* **Status** at the top: "Everything is working", or a list of problems in
  plain language, each with *why*, *effect* and *what to do*. Problems the
  daemon can fix itself (a device muted in the system) get a **Fix** button.
* **How the sound flows**: a diagram with inputs on the left, outputs on the
  right and one arrow per connection. Arrow thickness is the volume, dashed
  means off, red means a device is missing. Live green bars show sound
  arriving; the OBS box tells you in words whether sound is reaching OBS.
* **Volume controls**: one card per arrow with a percent slider and an
  On/Off switch.
* **Set up devices**: a three-step wizard, opened from the header as a
  dialog over the page so nothing else can be changed halfway through
  (Esc or a click beside it closes). Pick person A's headset (speak into
  it, the right bar moves), person B's headset, and the game sound input,
  give them names, and press Connect. It builds the standard layout:
  A hears B and the game, B hears A and the game, OBS hears A and B.
* **Advanced** (collapsed), in four tabs:
  * **Einrichtung**: names, device assignment, connections, defaults.
  * **Klang**: buffer size and the dropout measurement.
  * **Signalweg**: the whole wiring of the audio system drawn as a graph,
    not only this router's connections. Sources on the left, targets on
    the right, one line per real link, colour-coded by owner; hovering a
    box highlights its lines. The hop a loopback makes internally is drawn
    dashed, otherwise every playback stream would look like a source.
    **Vollbild** opens it over the whole window with zoom controls; on a
    real setup the graph is far wider than the panel. Plus and minus zoom,
    `einpassen` fits it, and Escape closes.
  * **Diagnose**: the checks, **System reparieren**, the audio-system
    analysis (including what profile, how many channels and which sample
    rate each device really runs at), the by-hand checklist for bad sound,
    and the log.
  * **System**: API token, config path, which build is running, and an
    **Aktualisieren** button that runs `git pull` and `./install.sh` and
    shows their output. It only works from a git checkout, and it runs as
    its own systemd unit because the installer restarts the service.
    Anyone who can reach the API can trigger it, which with the default
    `listen = "127.0.0.1"` means local users only.

  Each tab is linkable: `/#einrichtung`, `/#klang`, `/#diagnose`,
  `/#system`, `/#api` open the page with that tab in front, which is handy
  when pointing someone at a specific control.

  **API** holds the token field, the addresses an external tool talks to
  and a link to `/api-docs`: the whole interface as a Swagger page, every
  call with a description and a **try it out** button that talks to this
  very daemon. Swagger itself is loaded from a CDN; without internet the
  page falls back to a plain list built from `/openapi.json`, which can be
  loaded into any other tool as well. Both are readable without a token,
  because documentation behind a token is no documentation and neither
  holds anything the page does not already show.

  Every output area — reports, log, update protocol, the version list as
  text — carries an **Alles kopieren** button in its top right corner.

  **System** also lists every tool the router builds on with its version:
  PipeWire and WirePlumber as they actually answer, the ALSA and kernel
  version, the `hws` capture driver, where each helper binary lives and
  which packages are installed. When something behaves oddly, that list is
  usually where the reason shows up, and it can be switched to plain text
  for pasting into a message. **API** holds the token field and the
  addresses an external tool talks to.

Everything the command line offers is reachable from that page:
**Check the system** runs `doctor`, **Test the sound path** runs
`selftest`, and the log viewer shows what the router has been doing,
either since the last start or the full history from the system journal.
Output is plain text you can copy into a message.

The club's own header logo (`logo-horizontal-white.png` from the brand
guide) ships with the package and is served from the daemon, used exactly
as supplied at the documented height of 38.8 px, 35.2 px on a phone. The
guide forbids redrawing or altering the mark, so nothing here reproduces
it by other means.

Fonts are not fetched from the internet: the page asks for Nunito Sans and
falls back to the system's geometric sans if it is not installed locally.
The background swirl module from the brand guide is a website component
and is deliberately not duplicated here.

Every UI change rewrites `config.toml` (comments in the file are not kept)
and hot-reloads the daemon. Hand edits to the file still work; restart the
service afterwards.

## Buffer size

The buffer size is a property of the whole audio system, not of this
router. PipeWire runs its entire graph at the **smallest** buffer any
participant asks for, so a small request from one program slows down
nothing and can starve everything. That is why `latency` defaults to
`"auto"` here: the router asks for nothing and runs at whatever the
system uses (1024 frames, about 21 ms, on a stock Ubuntu).

The easiest way to change it is **Advanced -> Sound quality** in the web
UI: pick a size, press "Try it now" and listen. Nothing restarts. "Keep
after restart" writes the PipeWire drop-in below for you, and "Check for
dropouts" measures whether anything is being lost at the current setting.

By hand, the same thing lives in
`~/.config/pipewire/pipewire.conf.d/10-quantum.conf`:

```
context.properties = {
    default.clock.quantum     = 1024
    default.clock.min-quantum = 256
    default.clock.max-quantum = 2048
}
```

Then `systemctl --user restart pipewire wireplumber tfcz-audio`.

To try a value without changing anything, force it at runtime and listen:

```
pw-metadata -n settings 0 clock.force-quantum 2048   # bigger buffer, safer
pw-metadata -n settings 0 clock.force-quantum 256    # smaller, tighter
pw-metadata -n settings 0 clock.force-quantum 0      # back to automatic
```

Smaller means less delay and more risk of dropouts, which sound like
crackle or, when constant, like noise. `pw-top` shows dropouts in its ERR
column. Only set `latency` in `[audio]` if this router alone must run
tighter than the rest of the system.

## Sharing devices with OBS

PipeWire shares every device, so the router and OBS can both read the HDMI
audio at the same time. In OBS use **Audio Input Capture (PipeWire)** (or
the PulseAudio variant) for the game sound. Never use the old **ALSA Input
Capture** source: it opens the sound card directly and locks PipeWire out.

The daemon detects that case. It reads the device owner from
`/proc/asound` and PipeWire's node state, and reports "Game sound is taken
over by OBS" with the fix. The diagram marks the device and its arrows as
blocked. In `--fake` mode, `POST /demo/take/<alias>` simulates it.

## Telling identical headsets apart

Two headsets of the same model are the classic trap: after a reboot the
audio system may list them in a different order and A becomes B. The
setup wizard therefore stores *how to recognise* each device, not its
position in a list:

* **Serial number** if the headset reports one that no other visible device
  shares. It is then recognised in any USB port.
* **USB port** if the headsets are identical and report no serial. No
  software can tell such devices apart in any other way, so the wizard says
  which port each headset must stay in. Label the plugs and ports.
* **Fixed name** for PCI and built-in hardware, which never moves.

The daemon re-resolves devices every second. Unplugging and replugging,
or a reboot, restores the same assignment. A port-bound headset that is
plugged into another port is reported as missing together with the port
it was found in and the port it belongs to.

In the config this looks like:

```toml
[devices]
headset_a_mic = { match = { "device.bus-path" = "pci-0000:00:14.0-usb-0:1:1.0", kind = "input" } }
headset_b_out = { match = { "device.serial" = "Jabra_Speak_510_A1B2C3", kind = "output" } }
game_sound    = "alsa_input.pci-0000_03_00.0.analog-stereo"   # plain node name

[labels]
headset_a  = "Anna"
headset_b  = "Ben"
game_sound = "PlayStation"
```

## Configuration
## Configuration

`~/.config/tfcz-audio/config.toml`, see the annotated
[example](config/tfcz-audio.example.toml).

* `[devices]` maps aliases to devices: a PipeWire `node.name`, or
  `{ match = { ... } }` with hardware properties (see above). An optional
  `prefer = { ... }` picks between several devices that all match, which
  is how two identical headsets that report the same serial keep their
  identity.
* `[labels]` gives aliases or alias prefixes human names for the UI.
* `[routes.<name>]` has `from`, `to`, `volume` (default 1.0), `mute`,
  `description`. `to = "obs_mic"` targets the virtual OBS microphone.
  `capture_sink = true` captures a sink's monitor instead of a source
  (e.g. to route OBS's own monitor output somewhere).
* `[presets.<name>]` lists routes with a volume, or `{ volume = , mute = }`.
* `[api]` `listen`, `port`, optional `token`. Keep it on 127.0.0.1 unless
  you set a token.
* `[audio]` `latency = "auto"` leaves the buffer size to PipeWire, which is
  what you want (see below). `meters = false` switches the level bars off if
  `pw-record` misbehaves; routing is unaffected.

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
4. In OBS's Advanced Audio Properties, set the **monitoring device to a
   specific headset, never "Default"**. The default output can briefly
   become the router's own mix bus (for example while both headsets are
   unplugged), and monitoring into it would feed the OBS microphone back
   into itself. The daemon puts the default output back on a real device
   when that happens, but choosing a fixed monitoring device avoids the
   race entirely.

## CLI

```
tfcz-audio status                       routes, devices, virtual mic
tfcz-audio set hdmi_to_a --volume 0.3   or --db -10, --mute, --unmute, --toggle
tfcz-audio preset hdmi_off              apply a preset; no name lists them
tfcz-audio reset                        back to config values
tfcz-audio devices [-p]                 PipeWire audio nodes (+ ALSA card props)
tfcz-audio check                        validate config, report missing devices
tfcz-audio selftest [--seconds N]       record from every device and show what arrives
tfcz-audio versions [--json]            versions of every tool this router depends on
tfcz-audio fix [id ...] [--all]         what is broken about the system, and repair it
tfcz-audio run [--dry-run]              run the daemon in the foreground
```

### Repairing the system

**Erweitert → Diagnose → System reparieren** looks at the machine around
the router: missing packages, an audio session that is not running, a
service that will not come back after a reboot, lingering, PulseAudio
answering instead of PipeWire, sample rates that do not match, leftover
helper processes. Each finding says what it breaks and carries the exact
command.

What can be done without being root is done at the press of a button:
starting and enabling the audio session, enabling this service, writing a
PipeWire drop-in, cleaning up leftover helpers. For the rest nothing is
escalated quietly — a passwordless `sudo` is used when the system already
allows it, otherwise `pkexec` asks on the desktop, and when neither is
possible the command is shown to be pasted into a terminal. Swapping
PulseAudio for PipeWire is never offered as a button: it changes the whole
machine, so it is shown as a command only.

The ids never become part of a command line. A request names one of the
router's own findings, and the command that runs is rebuilt from a fresh
check, so a repair cannot be replayed once the problem is gone.

`tfcz-audio fix` does the same from a terminal, and is the better place
for the repairs that need root, because `sudo` can ask for a password
there. Without arguments it lists what it found.

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
| GET | `/presets` | list presets (presets are for automation; the page itself does not show them) |
| POST | `/presets/{p}` | apply a preset |
| POST | `/reset` | all routes back to config values |
| GET | `/devices` | audio sources/sinks currently in PipeWire |
| GET | `/` | web UI (`/#setup` opens the wizard) |
| GET | `/api-docs` | this API as a Swagger page, «try it out» included (no token needed) |
| GET | `/openapi.json` | the OpenAPI document, with this daemon as its server |
| GET | `/levels` | live signal levels per device and for the OBS mic |
| GET | `/hardware` | plugged-in hardware grouped by device, with identity strategy |
| GET | `/graph` | every audio node and link, labelled by owner, for the wiring view |
| POST | `/setup` | `{headset_a: {mic, out, label}, headset_b: {...}, game, game_label}`: build the standard layout |
| POST | `/fix/device/{alias}` | unmute a device / raise its system volume |
| PUT | `/config/labels` | replace the names |
| POST | `/meters/watch` | `{nodes: [{name, kind}]}`: meter extra devices for two minutes |
| GET | `/config` | current config as JSON (token hidden) |
| GET / PUT | `/audio` | read or change the system-wide buffer size (`quantum` in frames, 0 = automatic, `persist` to keep it) |
| POST | `/audio/dropouts` | measure dropouts with pw-top |
| GET | `/repair` | what is broken about the system and what can be repaired from here |
| POST | `/repair/{action}` | run one of those repairs |
| GET | `/versions` | versions of PipeWire, WirePlumber, the tools, the capture driver and the packages (`fresh=1` skips the 20 s cache) |
| GET / POST | `/diagnostics` | read the last report, or start one (`kind`: `doctor` or `selftest`) |
| GET | `/logs` | recent log lines (`level`, `limit`, `source`: `memory` or `journal`) |
| PUT | `/config/devices` | replace the alias -> node mapping, save, hot-reload |
| PUT / DELETE | `/config/routes/{r}` | create or update (`from`, `to`, `volume`, `mute`, `description`) or delete a route |
| PUT / DELETE | `/config/presets/{p}` | create or update or delete a preset |
| POST | `/config/save-defaults` | write current volumes/mutes into the config |

Examples:

```
curl -X PUT localhost:8787/routes/hdmi_to_a -d '{"volume": 0.3}'
curl -X POST localhost:8787/routes/a_to_obs/mute
curl -X POST localhost:8787/presets/hdmi_quiet
curl -X POST 'localhost:8787/routes/hdmi_to_b?volume=0.5&mute=false'
```

With `token` set, send `Authorization: Bearer <token>` or `?token=<token>`.

## How it works

* On start the daemon terminates helper processes left over from a crashed
  previous instance, then spawns one `pw-loopback` for the virtual mic (an
  `Audio/Sink` mix bus feeding an `Audio/Source/Virtual` node) and one per
  route, each targeting the resolved node names with
  `node.dont-fallback = true` so a missing headset leaves the route silent
  instead of falling back to the default device.
* Volume and mute are applied to the route's playback stream with `wpctl`;
  the graph is read with `pw-dump`.
* A supervisor pass runs every second: re-resolves devices, restarts
  loopbacks that died (with backoff, forgetting crash history after 30 s
  of health), restarts a loopback whose device resolved to a different
  node, recycles one that stays unlinked, and re-applies desired volumes
  once a node exists. The pass is bounded: at most three loopback
  recycles and six seconds of volume work, so it can never approach the
  systemd watchdog even when every PipeWire call is slow.
* Level meters are separate `pw-record` processes per device plus one on
  the OBS mic. They are read-only observers; a failing meter is restarted
  and never affects routing.
* Under systemd the unit is `Type=notify` with a 30 s watchdog. A small
  thread pings it as long as the supervisor loop has made progress in the
  last 20 s, so a slow PipeWire cannot get the daemon killed, while a truly
  hung loop is restarted. `Restart=always` with `RestartSteps` backs off
  from 2 s to 60 s between failures and never gives up. If the API port is
  taken, routing still starts and the bind is retried every second.
* Every stream carries its own WirePlumber restore key, so a volume saved
  for one route can never be restored onto another stream; freshly
  spawned streams get their volume and mute within the same supervisor
  pass, and capture streams are forced to unity.
* WirePlumber relinks the streams itself when a USB device disappears and
  comes back; the daemon only steps in when a device resolves differently.

## What happens when things go wrong

The daemon is written so that no single failure takes the audio down, and
so that it explains itself:

| Situation | Behaviour |
|---|---|
| A `pw-loopback` dies | restarted within 1 s, with backoff up to 30 s if it keeps dying; the UI shows "restarting" |
| PipeWire restarts | all helpers die and are respawned; the OBS mic reappears; logged once per outage |
| Headset unplugged | route stays silent (no fallback to another mic), UI shows "not connected"; replug relinks within 1 s |
| Identical headset moved to another port | reported as missing with the port it was found in and the port it belongs to |
| Device grabbed by another program (OBS ALSA source) | reported as "taken over by OBS" with the fix; arrows show "blocked" |
| Device muted in the system | reported with a **Fix** button that unmutes it |
| Config file unreadable | previous `.bak` is used; if none, daemon starts with no routes and the UI says so; Setup rewrites the file |
| Config not writable / disk full | the UI shows the exact error and the path |
| API port already in use | routing runs anyway; bind retried every second |
| Second daemon started by hand | refuses with a hint (single-instance lock) |
| Daemon hangs | systemd watchdog (30 s) kills and restarts it; `Restart=always`, never gives up |
| Daemon crashes | systemd restarts it after 2 s; leftover helpers are cleaned up at start |
| Helper writes a lot to stderr | drained in the background; a child can never block on a full pipe |
### Level bars on an older pw-record

Some PipeWire builds ship a `pw-record` without `--raw`. The daemon finds
that out by trying and walks a list of command shapes until one delivers
audio:

| Shape | Notes |
|---|---|
| `pw-record --raw …` | preferred, raw samples on stdout |
| `pw-record …` without `--raw` | writes a WAV header first, which the reader skips |
| the same without `-P` | for builds that also reject stream properties |
| `parec …` | PulseAudio client, needs `sudo apt install pulseaudio-utils` |

Which one is in use appears in the log (`Pegelmessung liefert Daten (…)`)
and in `/levels`. Nothing needs fixing on `pw-record` itself; if every
shape fails, the page names the refusal and `[audio] meters = false`
switches metering off. Routing never depends on any of this.

| `pw-record` refuses an option | that option is dropped and metering starts again (without `--raw` the WAV header is skipped); only an undroppable error switches the bars off, with the real message |
| Browser page from another site calls the API | rejected (cross-site guard); use a token for LAN access |
| Stream linked to the wrong device (fallback, manual move) | muted for safety and reported; unmuted when the link is right again |
| Devices present but the session manager does not connect the stream | reported after 8 s; the loopback is recycled up to 3 times, then reported as an error |
| A manual move in a mixer app is remembered by WirePlumber | the remembered target is cleared and the connection rebuilt |
| WirePlumber not running at all | reported as "session manager is not running" with the restart command |
| Default output becomes the router's mix bus | put back on a real device automatically |
| Another program keeps changing a volume | corrected, then reported as a conflict instead of fighting every second |
| OBS mic or mix bus muted/turned down by another program | restored to unity within a second |
| Non-ASCII device names under a C locale | tool output decoded as UTF-8 regardless of locale |

## Troubleshooting

Start with `tfcz-audio doctor`. It checks everything the daemon needs and
prints the fix for each failing line.

* **It sounds noisy, distorted or unintelligible**, or the level bars stay
  empty: run `tfcz-audio selftest`. It records from every device, prints
  what actually arrives, shows the exact helper command with its error if
  one fails, and lists what each router stream is really linked to. Then:
  switch the game-sound connections off in the web UI for a moment. If the
  intercom becomes clean, the capture card is the source of the noise
  (often an input with no signal, see
  [docs/hdmi-capture.md](docs/hdmi-capture.md)). If it stays noisy, it is
  timing: try a bigger buffer live with
  `pw-metadata -n settings 0 clock.force-quantum 2048`, watch the ERR
  column in `pw-top`, and confirm realtime priority with
  `tfcz-audio doctor`. When nothing is being lost and it still sounds
  wrong, the cause is a profile, a sample rate, a compressed HDMI stream, a
  clipping microphone, a filter chain, a doubled path or the room:
  [docs/sound-quality.md](docs/sound-quality.md) and the checklist under
  **Diagnose → Von Hand prüfen** walk through all of them.
* **A connection shows "linked to the wrong device"**: the daemon muted it
  on purpose. The audio system attached the stream to something other
  than the chosen device, usually because the device was missing and the
  stream fell back to a default, or because it was moved by hand in a
  mixer app. Plug the device in; the mute lifts by itself when the link
  is right again.


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
executing them, which also works on machines without PipeWire. `--fake`
runs against an in-memory PipeWire populated with the config's devices,
which is handy for working on the web UI: open <http://127.0.0.1:8787/>.
