table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "api.alsa.card.name", "matches", "HAudio 1" },
    },
  },
  apply_properties = {
    ["node.name"] = "alsa_input.avmatrix-movingright",
    ["node.description"] = "MovingRight Audio",
  },
})

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "api.alsa.card.name", "matches", "HAudio 3" },
    },
  },
  apply_properties = {
    ["node.name"] = "alsa_input.avmatrix-movingleft",
    ["node.description"] = "MovingLeft Audio",
  },
})

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "api.alsa.card.name", "matches", "HAudio 4" },
    },
  },
  apply_properties = {
    ["node.name"] = "alsa_input.avmatrix-maincam",
    ["node.description"] = "MainCam Audio",
  },
})
