# Driving tfcz-audio from Advanced Scene Switcher

Advanced Scene Switcher (ASS) runs macros inside OBS: *if* some condition
holds, *then* run actions. tfcz-audio's HTTP API is the target of those
actions. Because every write works with a bare `POST` and no body, the
simplest ASS actions are enough.

## Option A: HTTP action (preferred)

1. OBS -> Tools -> Advanced Scene Switcher -> **Macros** tab -> `+`.
2. Add a condition, for example **Scene** -> *Current scene is* -> `Talk`.
3. Add an action **HTTP**:
   * Method: `POST`
   * URL: `http://127.0.0.1:8787/presets/hdmi_quiet`
   * Body: leave empty
4. Add a second macro with the inverse condition and
   `http://127.0.0.1:8787/presets/default`.

The complete list with descriptions is on the router itself:
`http://127.0.0.1:8787/api-docs`. Every call can be tried out from there,
and `/openapi.json` loads into Postman, Bruno or Insomnia.

Useful URLs (all `POST`, no body needed):

| Purpose | URL |
|---|---|
| Apply preset | `/presets/<name>` |
| Set one route | `/routes/game_to_a/volume/0.3` (route names as shown under Advanced, e.g. `hdmi_to_a` in the example config) |
| Set in dB | `/routes/hdmi_to_a/volume_db/-12` |
| Mute / unmute / toggle | `/routes/a_to_obs/mute`, `/unmute`, `/toggle` |
| Set several fields | `/routes/hdmi_to_a?volume=0.3&mute=false` |
| Back to config | `/reset` |

If you set `token` in the config, either add a request header
`Authorization: Bearer <token>` in the HTTP action or append
`?token=<token>` to the URL.

If your ASS build has no HTTP action (older than 1.24), use option B.

## Option B: Run action

Action **Run**, program `/usr/bin/curl`, arguments:

```
-s -X POST http://127.0.0.1:8787/presets/hdmi_quiet
```

or use the CLI (the path is what `install.sh` created):

```
/home/<user>/.local/bin/tfcz-audio preset hdmi_quiet
```

## Typical macros

* **Talk segment**: scene `Talk` active -> `POST /presets/hdmi_quiet`;
  scene left -> `POST /presets/default`.
* **Commentators off air**: condition *Hotkey* (bind e.g. F9) -> action
  `POST /routes/a_to_obs/toggle` and a second action
  `POST /routes/b_to_obs/toggle`. Or define a preset pair
  `mics_off_air` / `mics_on_air` and call those.
* **Replay / intermission**: scene `Break` -> `POST /presets/hdmi_off`.
* **Stream started**: condition *Streaming* is active -> `POST /reset` so
  every show starts from the config values.
* **Studio mode safety**: condition *Studio Mode* preview scene changed ->
  do nothing to audio (intercom is independent of what is on air).

Make sure the macro runs only on the edge (ASS macros have a *perform
actions only on condition change* checkbox) so the API is not hit every
interval.

## Reading state back

`GET /status` returns JSON. ASS can store the result of an HTTP action in
a variable in recent versions; the interesting fields are
`routes.<name>.mute` and `routes.<name>.volume`. For most setups it is
simpler to keep ASS as the single source of truth and always *set* state
rather than reading it back.

## Checking that it works

From a terminal on the streaming PC, while OBS is running:

```
tfcz-audio status
journalctl --user -u tfcz-audio -f
```

Every accepted request is logged at debug level (`tfcz-audio -v run`),
and every volume change is logged at info level, so you can see the ASS
macro firing.
