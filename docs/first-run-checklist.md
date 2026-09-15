# First run on the streaming PC

Everything in this project was developed and tested against a simulated
PipeWire. The daemon's logic is covered by tests, but the real PipeWire and
WirePlumber behaviour can only be confirmed on the Ubuntu box. This is the
list of things to verify once, in order. Each step says what "good" looks
like and what to do otherwise.

## 1. Install

```
./install.sh
```

Good: ends with `tfcz-audio doctor` showing only `[ok]` lines and
"Everything needed is in place." Then open <http://127.0.0.1:8787/>.

Otherwise: fix the `[FAIL]` lines as printed, re-run `tfcz-audio doctor`.

## 2. Devices are seen

Web UI → Set up devices. Good: both headsets appear as "microphone +
headphones", the VC42 inputs appear as "HDMI capture input N". Speak into
a headset: its bar moves.

Otherwise:
* Headset missing: `wpctl status` must list it under Sources and Sinks.
  If not, it is a system problem (USB, permissions), not the router.
* HDMI inputs missing: `arecord -l` must show the hws cards; see
  `docs/hdmi-capture.md`.
* Bars never move for any device: check `journalctl --user -u tfcz-audio`
  for `meter` warnings. If `pw-record` complains about `--raw`, the
  PipeWire is too old for meters; routing still works.

## 3. Identity of the two headsets

On the wizard's last page read how each headset is recognised.
* "serial number, any USB port works": good, nothing to do.
* "recognised by USB port N": the headsets are identical without serials.
  Put a sticker on each plug and its port now.

After Connect, unplug headset B and plug it back into the same port. Good:
within ~2 s the UI shows it present again and its arrows green. Then plug
it into another port: with the port strategy the UI must say it is missing
and name both ports; with the serial strategy it must simply come back.

## 4. Volumes survive a restart of a stream

Switch the "Person A → OBS stream" arrow off, then unplug and replug
headset A. Good: the arrow stays off; on the console

```
pw-dump | python3 -c "import json,sys; [print(n['info']['props']['node.name'], p.get('mute'), p.get('channelVolumes')) for n in json.load(sys.stdin) if n['type']=='PipeWire:Interface:Node' and n['info']['props'].get('node.name','').startswith('tfcz.') for p in n['info']['params'].get('Props',[])]"
```

shows every `tfcz.<route>.in` at volume 1.0 / unmuted and every
`tfcz.<route>.out` at the volume/mute you set. If an `.in` stream shows a
different volume, WirePlumber restored a stale value; report it, the daemon
corrects it on the next tick anyway.

## 5. Nothing falls back to the wrong device

Unplug headset A. Good: person B hears nothing from A (no other
microphone is routed instead) and the UI shows "not connected" with the
routes listed. `pw-link -l` must show no link from another microphone into
`tfcz.a_to_b.in`. If there is one, `node.dont-fallback` is not honoured by
this WirePlumber; report it.

## 6. OBS

Add "Audio Input Capture (PipeWire)" → "TFCZ OBS Mic". Good: the OBS
meter moves when someone talks, and the web UI says "Sound is reaching
OBS right now". Add the game sound as its own "Audio Input Capture
(PipeWire)" source pointing at the HDMI input. Never use "ALSA Input
Capture": the UI would report the device as taken over by OBS.

## 7. Restart behaviour

```
systemctl --user restart pipewire wireplumber
```

Good: within a few seconds all arrows are green again without touching
the UI, and `journalctl --user -u tfcz-audio -n 50` shows "PipeWire
reachable again" followed by respawns, no tracebacks.

```
systemctl --user kill -s SIGKILL tfcz-audio
```

Good: systemd restarts it within 2 s, leftover helpers are cleaned up
(log line "terminated N leftover helper process(es)"), routes come back.

## 8. Boot without login

Reboot, do not log in. From another machine or after logging in later:
`systemctl --user status tfcz-audio` shows active since boot, and the
web UI shows everything working. If the headsets are "not connected"
although plugged in, the user is not in the `audio` group yet (needs the
reboot after `install.sh`), or lingering is off: `tfcz-audio doctor`.

## 9. Advanced Scene Switcher

Create a macro with an HTTP action `POST http://127.0.0.1:8787/presets/game_off`.
Good: the game arrows switch off in the UI when the macro fires.
