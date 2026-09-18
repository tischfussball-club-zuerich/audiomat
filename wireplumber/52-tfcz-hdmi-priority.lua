-- Installed by tfcz-audio's install.sh into ~/.config/wireplumber/main.lua.d/
-- (WirePlumber 0.4; 0.5 and newer use 52-tfcz-hdmi-priority.conf instead).
-- Removed again by uninstall.sh.
--
-- All devices share one PipeWire graph and one node drives its clock. PipeWire
-- prefers PCI devices over USB, so an HDMI capture input can become that
-- driver. When its HDMI source is switched off or unplugged the card stops
-- delivering samples, and everything that follows it stalls: the headsets, the
-- intercom and the OBS microphones all go silent at once. Giving the capture
-- inputs a low driver priority lets the USB headsets (2000) drive the graph
-- instead, so a missing HDMI source only silences that one input.
--
-- The cards are called "HAudio 1..4" with the AVMatrix VC42 driver and "HWS"
-- in the upstream hws driver; either one matches.
table.insert(alsa_monitor.rules, {
  matches = {
    { { "api.alsa.card.name", "matches", "HAudio*" } },
    { { "alsa.card_name", "matches", "*HWS*" } },
  },
  apply_properties = {
    ["priority.driver"] = 100,
    ["priority.session"] = 100,
  },
})
