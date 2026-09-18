# The studio setup

`studio/` holds the setup of the commentary studio's own machine, so a
reinstall (or a new PC) ends up exactly where the studio is known to sound
right:

```
./install.sh --studio
```

Everything the plain installer does, plus:

* `studio/config.toml` becomes `~/.config/tfcz-audio/config.toml`: the two
  headsets identified by USB port, the HDMI game sound, the six connections,
  the presets and the two OBS microphones (one per person). A different
  existing config is kept next to it as `config.toml.bak-studio-<time>`.
* `studio/wireplumber/*.lua` go to `~/.config/wireplumber/main.lua.d/`: they
  give the headsets `alsa_input.headset-left/right` and the HDMI inputs
  `alsa_input.avmatrix-*` their stable names, which the config refers to.
  Changed files are kept as `*.bak-studio-<time>` and WirePlumber is
  restarted once.

Without `--studio` nothing in `studio/` is touched. The plain installer never
overwrites an existing config.

## What the sound depends on

These are facts about the hardware, found by measurement, that no installer
can create:

* **The two headsets must not share a USB hub.** Both MMX 150 are full-speed
  devices. Behind one single-TT hub (the VIA Labs hub on the VL805 card) they
  share one transaction translator, and the game sound and the other
  person's voice came out with noise that grew with the signal. With one
  headset on a motherboard port, on its own controller, the noise was gone.
  Today: left headset on the VL805 hub, port 1.2 (`pci-0000:09:00.0`); right
  headset on the motherboard, port 2 (`pci-0000:0f:00.3`).
* **The buffer stays at PipeWire's default of 1024.** 2048 was the worst of
  everything measured (hundreds of underruns per 30 s); 512 made the
  headphones resync several times a second. **Erweitert → Buffer** should say
  "automatic".
* **HDMI capture inputs never drive the graph.** The rule in
  `wireplumber/` (installed by every `install.sh`) makes that so. If the HDMI
  source is switched off, its own input goes silent and nothing else does.
* **The OBS microphones are `Audio/Source`.** Not `Audio/Source/Virtual`:
  on PipeWire 1.0.5 that class never delivers audio once something plays
  into the mix bus.

## Moving a headset to another USB port

The headsets are identified by their port, because two identical devices
report no serial number. After moving one:

1. Read its new port: `pactl list sinks | grep long_card_name` shows e.g.
   `usb-0000:0f:00.3-2` for the sink of the moved headset.
2. Add that string to the headset's two blocks in
   `~/.config/wireplumber/main.lua.d/52-headset-stable-names.lua` and restart
   WirePlumber.
3. In the config, set that headset's `device.bus-path` in `[devices]` to the
   matching `pci-0000:0f:00.3-usb-0:2:1.0`, or use **Erweitert → Einrichtung**.
4. Copy both files back into `studio/` so the next `--studio` install knows.
