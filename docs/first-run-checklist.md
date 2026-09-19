# First run on the streaming PC

Everything in this project was developed and tested against a simulated
PipeWire. The daemon's logic is covered by tests, but the real PipeWire and
WirePlumber behaviour can only be confirmed on the Ubuntu box. This is the
list of things to verify once, in order. Each step says what "good" looks
like and what to do otherwise.

## 0. Where the headsets are plugged in

Do this before anything else, because it is the one thing software cannot
fix and the one that cost the studio weeks of hunting.

**The two headsets must not hang off the same USB hub.** USB headsets are
full-speed devices; behind one hub they share its transaction translator,
and the result is noise on the game sound and on the other person's voice
that grows with the signal — not silence, not crackling, noise that
follows the audio. In the studio one headset sits on a hub and the other
directly on a motherboard port, on a different controller.

The page says so on its own: two audio devices on one hub appear in the
problem list by name, and the setup wizard warns while the second headset
is being chosen. `tfcz-audio doctor` prints it too.

## 1. Install

```
./install.sh
```

Good: ends with `tfcz-audio doctor` showing only `[ok]` lines and
"Everything needed is in place." Then open <http://127.0.0.1:8787/>.

Otherwise: fix the `[FAIL]` lines as printed, re-run `tfcz-audio doctor`.
Most of them can be done from the page: **Erweitert → Diagnose → System
reparieren** installs missing packages, starts the audio session, enables
the service and writes the WirePlumber rule for the capture card.
**Erweitert → System** lists the version of everything involved, which is
what to paste into a message when asking for help.

## 2. Devices are seen

Web UI → Set up devices. Good: both headsets appear as "microphone +
headphones", the VC42 inputs appear as "HDMI capture input N". Speak into
a headset: its bar moves. Press **Testton** on the same row and confirm
the tone comes out of the headphone on that person's head — the bar proves
the microphone, the tone proves the headphone, and a swapped pair sounds
perfectly normal to everyone except the two people wearing them.

Afterwards, in **Erweitert → Einrichtung → Testton**, play *links* and
*rechts* on each headset once. Heard on the wrong side, or on both: the
device is on a profile that collapses to one channel, see
[sound-quality.md](sound-quality.md).

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
OBS right now".

With the two commentators sitting next to each other, switch on
**Erweitert → Einrichtung → Mikrofone für OBS → Für jede Person ein
eigenes Mikrofon** instead. OBS then sees `TFCZ <name A>` and
`TFCZ <name B>` as two sources, which is what lets an expander per person
keep the other voice out of the stream. Both have to be added once after
switching; the names never change by themselves afterwards.

Add the game sound as its own "Audio Input Capture (PipeWire)" source
pointing at the HDMI input. Never use "ALSA Input Capture": the UI would
report the device as taken over by OBS.

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

## 9. Things that can only be seen on real hardware

These are the assumptions the daemon makes about PipeWire that no test on
a development machine can confirm. Each one takes a minute.

**Who drives the audio clock.** `install.sh` writes a WirePlumber rule that
keeps the capture inputs from driving it, so this should already be right:
switch the HDMI source off and check that the headsets still hear each
other. If everything goes silent at once, the rule is missing or in the
format the other WirePlumber version reads — **Erweitert → Diagnose →
System reparieren** offers to write it. `pw-top` shows the drivers as the
rows that are not indented.

**Realtime priority after a boot without login.**

```
ps -eLo pid,rtprio,comm | grep -E 'pw-loopback|pipewire'
```

The data threads must show a number, not `-`. If they do not, run
`tfcz-audio doctor` and apply what it says about the `pipewire` group and
`pam_limits`; otherwise expect crackles while OBS encodes.

**A remembered manual move.** Open pavucontrol, move "TFCZ a_to_b
(playback)" to another output once, then watch the web UI: it must report
"linked to the wrong device", mute that connection, and repair itself
within about ten seconds. Confirm with `pw-link -l | grep tfcz.a_to_b`.

**Card order across reboots.** With both headsets plugged in, reboot three
times and compare `tfcz-audio devices -p` for the capture inputs each
time. If the numbering moves, pin it as described in
[hdmi-capture.md](hdmi-capture.md).

**No session manager.** `systemctl --user stop wireplumber` with the UI
open: it must show "The audio session manager is not running" with the
restart command. Then `systemctl --user start wireplumber` and everything
must come back by itself.

**Suspend and resume.** `systemctl suspend`, wake the machine: all arrows
green within a few seconds, and no "wrong device" lines in
`journalctl --user -u tfcz-audio`.

**No fallback to the wrong microphone.** Unplug headset A and check that
`pw-link -l` shows no link into `tfcz.a_to_b.in`. A link from another
microphone would mean this PipeWire ignores `node.dont-fallback`; the
daemon then mutes the route and says so, which is the safety net for
exactly this case.

## 10. Advanced Scene Switcher

Create a macro with an HTTP action `POST http://127.0.0.1:8787/presets/game_off`.
Good: the game arrows switch off in the UI when the macro fires.
