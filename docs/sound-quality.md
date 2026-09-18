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

### What the signal itself says

**Tonweg testen** records a moment from every device and measures four
things: how loud it is (peak, rms), how far the peaks rise above the
average (crest), how often the wave changes sign (zero crossings), and
how many samples sit at the end of the scale.

That is enough to name the fault instead of the symptom:

* **Not sound at all, but noise.** A console or PC set to "Bitstream"
  or "Automatic" sends Dolby or DTS over HDMI, and the capture card
  records that stream as what it is. Compressed data is close to random:
  it crosses zero on about half of all samples and stays loud, which no
  microphone and no game does. *Fix*: set the source's audio output to
  PCM / stereo.
* **Clipping**, counted rather than guessed: the share of samples pinned
  to the end of the scale.
* **Digital silence** — the device delivers, but delivers nothing.
* **One dead channel**, which is normal for a microphone and a fault for
  a headphone or the game sound.
* **A DC offset**, which sounds dull and can click.

### The same sound twice, or in a circle

**Tonsystem analysieren** also reads the shape of the graph. When a
headphone receives the same microphone over two different paths, it
plays it twice with a small offset: hollow and metallic. And when sound
comes back to where it started, that is feedback, which can get loud
enough to hurt. Both are reported with the path that causes them.

### The other person's voice in this microphone

Two headsets side by side: each microphone picks up the other person
through the air. In the stream that voice arrives twice, about a
millisecond apart, and the sum is hollow and metallic — the acoustic
version of the doubled path above.

Nothing can subtract it after the fact. What works, in order of effect:

1. **Microphone position.** A boom 2 cm from the mouth against a voice
   from 50 cm is 25–30 dB. No filter comes close, and it costs no
   latency and no CPU. Headphones quieter helps too: what leaks out of
   them ends up in the microphone as well.
2. **An expander per person in OBS**, which needs one source per person:
   see "One microphone for OBS, or one per person" in the README. While
   only one of them speaks — the normal case — the other channel is
   closed and the doubling is gone. While both speak it is back; that is
   inherent.
3. An automixer would share the gain between the two instead of opening
   and closing each channel on its own. PipeWire has none built in.

Adaptive cancellation using the other microphone as a reference is
possible in principle and not worth it here: it only holds while nobody
moves, reaches maybe 6–12 dB, costs latency, and puts exactly the kind of
foreign filter chain into the path that this page spends its length
warning about.

Noise suppression does not help at all. It is trained to keep speech and
remove everything else — the person next to you is speech.

## Checked by hand

### Is this even the right headphone?

**Erweitert → Einrichtung → Testton** plays a short tone on one
headphone, left, right or both. It answers what no measurement can: that
the headphone on this person's head is the one the page means, that both
ears work, that the sides are not swapped, and that the device is not
muted in the system. A device stuck on one channel by its profile gives
itself away here too -- "left" is heard on both ears.

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
