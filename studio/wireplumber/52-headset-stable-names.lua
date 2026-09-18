-- Stable per-port naming for the two identical MMX 150 USB headset
-- interfaces (commentator headsets). They report identical
-- idVendor/idProduct/iSerial (2e50:0026, no serial string), so ALSA
-- disambiguates them only by probe order (card N / N+1, ALSA id
-- "M150"/"M150_1"), which is NOT stable across reboots or USB
-- re-enumeration timing.
--
-- The one thing that IS stable is the physical USB port each headset
-- is plugged into. `alsa.long_card_name` embeds the kernel's usb
-- bus-path (e.g. "... at usb-0000:09:00.0-1.1, full speed") and is
-- present on every node spawned from that card, so we match on that
-- instead of the card number/ALSA id.
--
-- Port 1.1 (usb-0000:09:00.0-1.1) or motherboard port usb-0000:0f:00.3-2
--   = Right commentator headset (moved to the motherboard controller 2026-09-18)
-- Port 1.2 = Left commentator headset (as wired today)
--
-- If a headset is ever moved to a different USB port, or the AVMatrix
-- root USB path changes (currently pci-0000:09:00.0 -- shared with
-- these headsets on the same onboard USB controller), update the glob
-- patterns below, mirroring 51-avmatrix-audio-names.lua's pattern.
--
-- NOTE: this must be a .lua file under main.lua.d/, not a .conf file
-- under wireplumber.conf.d/ -- confirmed on this box (WirePlumber
-- 0.4.17) that the newer monitor.alsa.rules .conf schema is silently
-- ignored; only the legacy Lua alsa_monitor.rules API is honored.
-- (This is why 50-mic-cleanup.conf/50-mmx150-names.conf/51-mmx150.conf/
-- 52-mmx-names.conf/60-mmx150-clean-names.conf in wireplumber.conf.d/
-- never actually did anything, regardless of which one's rules
-- "should" have won.)
--
-- Produces: alsa_input.headset-right / alsa_output.headset-right
--           alsa_input.headset-left  / alsa_output.headset-left

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "alsa.long_card_name", "matches", "*usb-0000:09:00.0-1.1,*" },
      { "media.class", "equals", "Audio/Source" },
    },
    {
      { "alsa.long_card_name", "matches", "*usb-0000:0f:00.3-2,*" },
      { "media.class", "equals", "Audio/Source" },
    },
  },
  apply_properties = {
    ["node.name"] = "alsa_input.headset-right",
    ["node.description"] = "Headset Right Mic",
    ["node.nick"] = "Headset Right Mic",
    ["audio.channels"] = 1,
    ["audio.rate"] = 48000,
    ["node.passive"] = false,
  },
})

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "alsa.long_card_name", "matches", "*usb-0000:09:00.0-1.1,*" },
      { "media.class", "equals", "Audio/Sink" },
    },
    {
      { "alsa.long_card_name", "matches", "*usb-0000:0f:00.3-2,*" },
      { "media.class", "equals", "Audio/Sink" },
    },
  },
  apply_properties = {
    ["node.name"] = "alsa_output.headset-right",
    ["node.description"] = "Headset Right Speaker",
    ["node.nick"] = "Headset Right Speaker",
  },
})

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "alsa.long_card_name", "matches", "*usb-0000:09:00.0-1.2,*" },
      { "media.class", "equals", "Audio/Source" },
    },
  },
  apply_properties = {
    ["node.name"] = "alsa_input.headset-left",
    ["node.description"] = "Headset Left Mic",
    ["node.nick"] = "Headset Left Mic",
    ["audio.channels"] = 1,
    ["audio.rate"] = 48000,
    ["node.passive"] = false,
  },
})

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "alsa.long_card_name", "matches", "*usb-0000:09:00.0-1.2,*" },
      { "media.class", "equals", "Audio/Sink" },
    },
  },
  apply_properties = {
    ["node.name"] = "alsa_output.headset-left",
    ["node.description"] = "Headset Left Speaker",
    ["node.nick"] = "Headset Left Speaker",
  },
})
