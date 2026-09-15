# AVMatrix VC42 on Linux

The VC42 is a 4-input HDMI PCIe capture card. Its out-of-tree driver
exposes each input as a V4L2 video device **and** as its own ALSA sound
card carrying the embedded HDMI audio (48 kHz, 16-bit stereo). PipeWire
picks up those ALSA cards, and tfcz-audio routes them like any other
source.

## Driver

Maintained fork with DKMS support:
<https://github.com/GloriousEggroll/AVMATRIX-VC41-VC42-CAPTURE>

```
sudo apt install dkms build-essential linux-headers-$(uname -r)
git clone https://github.com/GloriousEggroll/AVMATRIX-VC41-VC42-CAPTURE
cd AVMATRIX-VC41-VC42-CAPTURE
./dkms-install.sh
sudo reboot
```

Then verify:

```
arecord -l          # four capture cards from the hws driver
v4l2-ctl --list-devices
tfcz-audio devices -p
```

Caveats:

* The driver is not in the mainline kernel. Kernel updates trigger a DKMS
  rebuild that can fail on a new major kernel. Hold the kernel on the
  production machine (`sudo apt-mark hold linux-image-generic
  linux-headers-generic`) or test each update first.
* An upstream `hws` driver is under review on the kernel mailing list;
  as of September 2026 its audio part is a separate, unmerged series.
  Once it lands, the DKMS module becomes unnecessary.

## Pin the card order (important with four identical inputs)

The four HDMI inputs are four ALSA cards created by the same driver. Card
numbers are handed out in the order the kernel finds the hardware, and USB
devices are probed asynchronously, so the numbers **can differ between
boots**. Everything that identifies an input by name or number then points
at a different HDMI port after a reboot: the game sound would come from
the wrong console and nothing in the UI could notice, because the device
exists and is linked.

Pin the order once, in `/etc/modprobe.d/tfcz-audio-slots.conf`:

```
# built-in audio first, then the four capture inputs, in a fixed order
options snd slots=snd_hda_intel,hws,hws,hws,hws
```

Adapt the first entry to the module your onboard audio uses (`lsmod | grep
snd_` and `cat /proc/asound/modules` show it). Reboot, then check that
`cat /proc/asound/cards` lists the same order twice in a row across
reboots. `tfcz-audio doctor` reports how many capture inputs it sees.

## Keep the capture card from driving the audio graph

All connected devices end up in one PipeWire graph, and one node drives
its clock. PipeWire prefers PCI devices over USB, so the capture card can
become the driver. If the HDMI source is switched off and the card stops
delivering samples, the headset intercom can stall with it.

Check once with routes running:

```
pw-top        # the non-indented rows are the drivers
```

If an `hws` node is the driver, take it out of the running with a
WirePlumber rule (`~/.config/wireplumber/wireplumber.conf.d/52-tfcz-hdmi-priority.conf`
on 0.5, or the Lua equivalent from the next section on 0.4):

```
monitor.alsa.rules = [
  {
    matches = [ { alsa.card_name = "~.*HWS.*" } ]
    actions = { update-props = { priority.driver = 100 priority.session = 100 } }
  }
]
```

Lower numbers lose the election; the USB headsets (driver priority around
1000 for sinks) then drive the graph. Restart WirePlumber and check
`pw-top` again.

## Stable node names for the four inputs

The four inputs are four ALSA cards on the same PCI device, so PipeWire
may give them similar or numbered `node.name` values, and the numbering
can depend on probe order. Pin them with a WirePlumber rule so
`config.toml` can refer to `hdmi_in_1` etc.

First look at what distinguishes them:

```
tfcz-audio devices -p
```

Typical distinguishing properties are `alsa.card_name`,
`alsa.long_card_name` or `api.alsa.card` (the ALSA card index).

### WirePlumber 0.5 and newer (Ubuntu 24.10+)

`~/.config/wireplumber/wireplumber.conf.d/51-tfcz-hdmi.conf`:

```
monitor.alsa.rules = [
  {
    matches = [ { alsa.long_card_name = "~.*HWS.*" api.alsa.card = "1" } ]
    actions = { update-props = { node.name = "hdmi_in_1" node.description = "HDMI In 1" } }
  }
  {
    matches = [ { alsa.long_card_name = "~.*HWS.*" api.alsa.card = "2" } ]
    actions = { update-props = { node.name = "hdmi_in_2" node.description = "HDMI In 2" } }
  }
]
```

### WirePlumber 0.4 (Ubuntu 24.04)

`~/.config/wireplumber/main.lua.d/51-tfcz-hdmi.lua`:

```lua
for i = 1, 4 do
  table.insert(alsa_monitor.rules, {
    matches = {
      {
        { "alsa.long_card_name", "matches", "*HWS*" },
        { "api.alsa.card", "equals", tostring(i) },
        { "media.class", "equals", "Audio/Source" },
      },
    },
    apply_properties = {
      ["node.name"] = "hdmi_in_" .. i,
      ["node.description"] = "HDMI In " .. i,
    },
  })
end
```

Adjust the match values to what `tfcz-audio devices -p` shows, then:

```
systemctl --user restart wireplumber
tfcz-audio devices
```

and use `hdmi_1 = "hdmi_in_1"` in `[devices]`.

## If the card audio is not usable

If the ALSA devices do not appear or carry no audio, the fallback is to
let OBS capture the audio from its own VC42 video source and monitor that
source to a PipeWire sink: OBS -> Advanced Audio Properties -> *Monitor
and Output* for the capture source, monitoring device = a null sink you
create for that purpose. Then add a route with `capture_sink = true` from
that sink. The daemon is agnostic about where the HDMI audio comes from.
