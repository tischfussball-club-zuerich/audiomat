# It sounds bad, but nothing is being lost

The dropout measurement in **Erweitert → Diagnose** answers exactly one
question: does the machine keep up? When it reports nothing lost, timing
is ruled out — and everything on this page is still possible, because
none of it loses a single packet. It just sounds wrong.

Two of these are checked automatically, the rest are in the checklist
under **Diagnose → Von Hand prüfen**, with a tick box each so a run
through them can be interrupted.

## Checked automatically

### Device profile

A USB headset usually offers more than one profile. Besides the full
one there is a telephony profile (`headset-head-unit`, HFP/HSP,
sometimes called "chat"): one channel, heavily compressed, 8 or 16 kHz.
WirePlumber picks it whenever something asks for a microphone and a
headphone at the same time on a device that cannot do both in the full
profile, and it never says so.

*Sounds like*: dull, hissy, speech hard to follow, as if through a
phone. Music through it is unmistakable.

The analysis names the device, its profile and its rate, and warns when
a profile looks like a telephony one, when an output runs on a single
channel, or when a device runs below 44100 Hz.

*Fix*: Ubuntu's sound settings, or `pactl list cards` to see the
profiles and `pactl set-card-profile <card> <profile>` to change one.
Pick the profile without "head unit" in its name.

### Sample rates that do not match

The capture card runs at 48000 Hz. If a headset runs at 44100, PipeWire
resamples silently. That usually works, but it costs quality, and a
device whose clock drifts produces a regular tick that never appears as
a lost packet.

The analysis lists the rate per device and warns when they differ, or
when the PipeWire clock itself runs at a different rate than the
hardware.

*Fix*: put everything on 48000. For PipeWire:

```
# ~/.config/pipewire/pipewire.conf.d/10-rate.conf
context.properties = {
    default.clock.rate = 48000
    default.clock.allowed-rates = [ 48000 ]
}
```

then `systemctl --user restart pipewire wireplumber`.

## Checked by hand

### The game source sends a compressed stream

A console or PC set to "Bitstream" or "Automatic" sends Dolby or DTS
over HDMI. The capture card records that stream as what it is: noise.

*Sounds like*: loud hiss or rattling instead of game sound, from the
first second, independent of the volume.

*Fix*: set the source's audio output to PCM / stereo.

### A microphone clips

USB headsets often have a microphone boost, and the system volume can
be above 100 %.

*Sounds like*: harsh, scratchy speech that gets louder rather than
clearer. **Tonweg testen** shows a peak close to 0 dB.

*Fix*: capture level to about 70 %, and `alsamixer -c <card>` to turn
the boost off.

### A filter chain sits in the path

A noise-suppression or sidetone chain in `~/.config/pipewire/` runs
before this router.

*Sounds like*: tinny, watery voice, syllables dropping out.

*Fix*: disable the chain and listen again. The analysis lists such
nodes; names containing `clean` or `sidetone` give them away.

### The same sound arrives twice

A route of this router plus a monitor output or OBS monitoring make two
paths to the same destination.

*Sounds like*: hollow, metallic, a slight echo, at worst feedback.

*Fix*: the **Signalweg** view in full screen shows two arrows ending at
the same box. In OBS, check "Monitor and Output" per source.

### USB

Two headsets and the capture card on one hub share bandwidth and power.

*Sounds like*: sporadic crackling that comes and goes.

*Fix*: plug the headsets into separate ports of the machine directly —
the device identification needs that anyway.

### The headset does its own processing

Some headsets have noise suppression, sidetone or a game mode in the
hardware, independent of the computer.

*Sounds like*: the effect stays even when everything on the computer is
neutral.

*Fix*: turn off every effect on the headset or in its own software.

### The room

Open microphones pick up the table, the hall and the other headphone.

*Sounds like*: reverberant, a lot of background, the other person
hearing themselves faintly.

*Fix*: microphone arm closer to the mouth, headphones quieter, and mute
the microphones during breaks.
