"""Config edits requested over the API: validate through parse(), write the
file, hot-reload the router. Every function returns the new status."""

from __future__ import annotations

import copy
import threading
from typing import Any, Callable

from .config import OBS_MIC, Config, ConfigError, human, parse, save, to_dict
from .router import Router, UnknownPreset, UnknownRoute


class EditError(ConfigError):
    pass


# Read the config, change it, write it back. Two edits arriving at the same
# time (the page saves names while a preset renames a route) would otherwise
# both start from the old file and the second would drop the first.
_commit_lock = threading.Lock()


def _commit(router: Router, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _commit_lock:
        data = to_dict(router.cfg)
        mutate(data)
        new_cfg = parse(copy.deepcopy(data))
        new_cfg.path = router.cfg.path
        if new_cfg.path is not None:
            save(new_cfg)
        return router.reload(new_cfg)


def set_devices(router: Router, devices: dict[str, Any]) -> dict[str, Any]:
    """devices: alias -> node.name string, or {"match": {...}}, or
    {"node": name, "identity": "auto"} to let the router pick serial/port matching."""
    clean: dict[str, Any] = {}
    for alias, value in devices.items():
        if not isinstance(alias, str) or not alias.strip():
            raise EditError("Ein Gerätename darf nicht leer sein")
        alias = alias.strip()
        if isinstance(value, str) and value.strip():
            clean[alias] = value.strip()
        elif isinstance(value, dict) and isinstance(value.get("match"), dict) and value["match"]:
            clean[alias] = {"match": {str(k): str(v) for k, v in value["match"].items()}}
        elif isinstance(value, dict) and value.get("node"):
            clean[alias] = _auto_identity(router, str(value["node"]))
        else:
            raise EditError(f"device '{alias}': expected a device name or a match table")

    def mutate(data: dict[str, Any]) -> None:
        used = {r["from"] for r in data["routes"].values()} | {r["to"] for r in data["routes"].values()}
        removed = set(data["devices"]) - set(clean)
        for alias in removed & used:
            raise EditError(f"device '{alias}' is still used by a route")
        data["devices"] = clean

    return _commit(router, mutate)


def upsert_route(router: Router, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    if "from" not in spec or "to" not in spec:
        raise EditError("Eine Verbindung braucht «von» und «zu»")
    entry: dict[str, Any] = {"from": str(spec["from"]), "to": str(spec["to"])}
    if spec.get("description"):
        entry["description"] = str(spec["description"])
    if "volume" in spec:
        try:
            entry["volume"] = float(spec["volume"])
        except (TypeError, ValueError):
            raise EditError("Die Lautstärke muss eine Zahl sein") from None
    if spec.get("mute"):
        entry["mute"] = True
    if spec.get("capture_sink"):
        entry["capture_sink"] = True

    def mutate(data: dict[str, Any]) -> None:
        old = data["routes"].get(name, {})
        if "volume" not in entry and "volume" in old:
            entry["volume"] = old["volume"]
        data["routes"][name] = entry

    return _commit(router, mutate)


def delete_route(router: Router, name: str) -> dict[str, Any]:
    if name not in router.cfg.routes:
        raise UnknownRoute(name)

    def mutate(data: dict[str, Any]) -> None:
        data["routes"].pop(name, None)
        for preset in data["presets"].values():
            preset.pop(name, None)
        data["presets"] = {p: e for p, e in data["presets"].items() if e}

    return _commit(router, mutate)


def upsert_preset(router: Router, name: str, entries: dict[str, Any]) -> dict[str, Any]:
    if not entries:
        raise EditError("Eine Voreinstellung braucht mindestens eine Verbindung")

    def mutate(data: dict[str, Any]) -> None:
        data["presets"][name] = entries

    return _commit(router, mutate)


def delete_preset(router: Router, name: str) -> dict[str, Any]:
    if name not in router.cfg.presets:
        raise UnknownPreset(name)

    def mutate(data: dict[str, Any]) -> None:
        data["presets"].pop(name, None)

    return _commit(router, mutate)


def save_current_as_defaults(router: Router) -> dict[str, Any]:
    snapshot = {n: (s.volume, s.mute) for n, s in router.desired.items()}

    def mutate(data: dict[str, Any]) -> None:
        for name, (volume, mute) in snapshot.items():
            if name in data["routes"]:
                data["routes"][name]["volume"] = volume
                if mute:
                    data["routes"][name]["mute"] = True
                else:
                    data["routes"][name].pop("mute", None)

    return _commit(router, mutate)


def public_config(cfg: Config) -> dict[str, Any]:
    data = to_dict(cfg)
    data["api"] = {"listen": cfg.api.listen, "port": cfg.api.port, "token_set": bool(cfg.api.token)}
    data["path"] = str(cfg.path) if cfg.path else None
    data["obs_mic_target"] = OBS_MIC
    data["obs_mic_targets"] = sorted(cfg.virtual.outputs)
    data["labels"] = dict(cfg.labels)
    data["names"] = ({alias: human(cfg, alias) for alias in cfg.devices}
                     | {key: human(cfg, key) for key in cfg.virtual.outputs})
    return data


def _auto_identity(router: Router, node_name: str) -> Any:
    """Config value for a node: a matcher on serial or USB port when that is
    more stable than the node name, otherwise the node name itself."""
    ident = router.identity_of_node(node_name)
    if ident["strategy"] in ("serial", "port") and ident["match"]:
        value: dict[str, Any] = {"match": ident["match"]}
        if ident.get("prefer"):
            value["prefer"] = ident["prefer"]
        return value
    return node_name


# --------------------------------------------------------------------------- #
# Guided setup: two headsets (+ optional game sound) -> complete config
# --------------------------------------------------------------------------- #

GAME_ALIAS = "game_sound"


def _pair(spec: Any, label: str) -> tuple[str, str, str]:
    if not isinstance(spec, dict) or not spec.get("mic") or not spec.get("out"):
        raise EditError(f"{label}: wähl ein Headset mit Mikrofon und Kopfhörer")
    name = str(spec.get("label") or "").strip()
    return str(spec["mic"]), str(spec["out"]), name


OBS_A, OBS_B = "obs_mic_a", "obs_mic_b"


def obs_outputs(separate: bool, name_a: str, name_b: str) -> dict[str, Any]:
    """The microphones OBS sees: one shared, or one per person.

    Separate ones are what lets OBS filter each voice on its own -- a gate or
    expander can only work on a channel it can see by itself.
    """
    if not separate:
        return {}
    return {
        OBS_A: {"description": f"TFCZ {name_a}"},
        OBS_B: {"description": f"TFCZ {name_b}"},
    }


def setup(router: Router, body: dict[str, Any]) -> dict[str, Any]:
    """Replace devices, routes and presets with the standard two-headset layout.
    body = {headset_a: {mic, out}, headset_b: {mic, out}, game: node|null,
            game_volume: 0.6, separate_obs: false}"""
    a_mic, a_out, a_label = _pair(body.get("headset_a"), "Headset A")
    b_mic, b_out, b_label = _pair(body.get("headset_b"), "Headset B")
    if {a_mic, a_out} & {b_mic, b_out}:
        raise EditError("Headset A und Headset B müssen zwei verschiedene Geräte sein")
    if a_label and b_label and a_label.lower() == b_label.lower():
        raise EditError("Gib den beiden Headsets verschiedene Namen")
    game = body.get("game") or None
    game_label = str(body.get("game_label") or "").strip()
    # identity: serial number when unique, else USB port, else fixed name
    ids = {n: _auto_identity(router, n) for n in (a_mic, a_out, b_mic, b_out)}
    if game:
        ids[str(game)] = _auto_identity(router, str(game))
    try:
        game_volume = float(body.get("game_volume", 0.6))
    except (TypeError, ValueError):
        raise EditError("Die Lautstärke des Spieltons muss eine Zahl sein") from None
    if game and game in (a_mic, b_mic, a_out, b_out):
        raise EditError("Der Spielton kann nicht von einem Headset kommen; wähl den HDMI-Aufnahmeeingang")
    separate = bool(body.get("separate_obs", router.cfg.virtual.separate))

    def mutate(data: dict[str, Any]) -> None:
        devices = {"headset_a_mic": ids[a_mic], "headset_a_out": ids[a_out], "headset_b_mic": ids[b_mic], "headset_b_out": ids[b_out]}
        na, nb, ng = a_label or "Headset A", b_label or "Headset B", game_label or "Game sound"
        labels = {"headset_a": na, "headset_b": nb}
        obs_a, obs_b = (OBS_A, OBS_B) if separate else (OBS_MIC, OBS_MIC)
        routes: dict[str, Any] = {
            "a_to_b": {"description": f"{na} spricht zu {nb}", "from": "headset_a_mic", "to": "headset_b_out", "volume": 1.0},
            "b_to_a": {"description": f"{nb} spricht zu {na}", "from": "headset_b_mic", "to": "headset_a_out", "volume": 1.0},
            "a_to_obs": {"description": f"{na} auf dem Stream", "from": "headset_a_mic", "to": obs_a, "volume": 1.0},
            "b_to_obs": {"description": f"{nb} auf dem Stream", "from": "headset_b_mic", "to": obs_b, "volume": 1.0},
        }
        presets: dict[str, Any] = {
            "everything_on": {"a_to_b": 1.0, "b_to_a": 1.0, "a_to_obs": 1.0, "b_to_obs": 1.0},
            "mics_off_air": {"a_to_obs": {"mute": True}, "b_to_obs": {"mute": True}},
            "mics_on_air": {"a_to_obs": {"mute": False}, "b_to_obs": {"mute": False}},
        }
        if game:
            devices[GAME_ALIAS] = ids[str(game)]
            labels[GAME_ALIAS] = ng
            routes["game_to_a"] = {"description": f"{ng} für {na}", "from": GAME_ALIAS, "to": "headset_a_out", "volume": game_volume}
            routes["game_to_b"] = {"description": f"{ng} für {nb}", "from": GAME_ALIAS, "to": "headset_b_out", "volume": game_volume}
            presets["everything_on"].update({"game_to_a": game_volume, "game_to_b": game_volume})
            presets["game_quiet"] = {"game_to_a": round(game_volume / 2, 2), "game_to_b": round(game_volume / 2, 2)}
            presets["game_off"] = {"game_to_a": {"mute": True}, "game_to_b": {"mute": True}}
            presets["game_on"] = {"game_to_a": {"mute": False}, "game_to_b": {"mute": False}}
        # keep the user's old volumes for routes that keep their name
        for name, entry in routes.items():
            old = data["routes"].get(name)
            if old and old.get("from") == entry["from"] and old.get("to") == entry["to"]:
                entry["volume"] = old.get("volume", entry["volume"])
        data["devices"] = devices
        data["labels"] = labels
        data["routes"] = routes
        data["presets"] = presets
        virtual = data.setdefault("virtual", {})
        outputs = obs_outputs(separate, na, nb)
        if outputs:
            virtual["outputs"] = outputs
        else:
            virtual.pop("outputs", None)

    return _commit(router, mutate)


def _person_of(cfg: Config, source_ref: str) -> str:
    """Whose voice a route to OBS carries, in the words the page uses."""
    base = source_ref[:-4] if source_ref.endswith("_mic") else source_ref
    return human(cfg, base)


def _obs_assignment(cfg: Config, routes: dict[str, Any]) -> list[tuple[str, str]]:
    """(route, person) for every route to OBS, headset A first.

    Sorting by route name would be enough only as long as the names happen to
    be in that order. They are not: a route called "alpha" from headset B and
    one called "zebra" from headset A would put B's voice on the microphone
    named after A, and nothing would say so until it is on the stream.
    """
    entries = [(name, spec) for name, spec in routes.items() if str(spec.get("to", "")).startswith(OBS_MIC)]

    def order(entry: tuple[str, Any]) -> tuple[int, str]:
        source = str(entry[1].get("from", ""))
        rank = 0 if source.startswith("headset_a") else 1 if source.startswith("headset_b") else 2
        return (rank, entry[0])

    return [(name, _person_of(cfg, str(spec.get("from", "")))) for name, spec in sorted(entries, key=order)]


def set_obs_mode(router: Router, separate: bool) -> dict[str, Any]:
    """Switch between one microphone for OBS and one per person, without
    touching anything else. The routes to OBS follow along, so the change is
    complete: a route pointing at a microphone that no longer exists would
    refuse to load."""
    cfg = router.cfg

    def mutate(data: dict[str, Any]) -> None:
        assignment = _obs_assignment(cfg, data["routes"])
        keys = [OBS_A, OBS_B]
        if separate and len(assignment) != len(keys):
            raise EditError(f"Getrennte OBS-Mikrofone brauchen genau zwei Verbindungen zum Stream, "
                            f"gefunden: {len(assignment)}. Richte die Geräte neu ein.")
        virtual = data.setdefault("virtual", {})
        if separate:
            names = [person for _, person in assignment]
            if names[0] == names[1]:  # both routes from the same person: keep them apart anyway
                names = [f"{names[0]} ({route})" for route, _ in assignment]
            virtual["outputs"] = {key: {"description": f"TFCZ {name}"} for key, name in zip(keys, names, strict=True)}
        else:
            virtual.pop("outputs", None)
        # every route has to land on a microphone that exists afterwards: going
        # back to one means all of them point at it, not just the first
        for index, (route_name, _) in enumerate(assignment):
            data["routes"][route_name]["to"] = keys[index] if separate else OBS_MIC

    return _commit(router, mutate)


def set_labels(router: Router, labels: dict[str, Any]) -> dict[str, Any]:
    clean = {str(k): str(v).strip() for k, v in labels.items() if str(v).strip()}

    def mutate(data: dict[str, Any]) -> None:
        data["labels"] = clean
        # A renamed person should be renamed in OBS too. Only the description
        # follows: the node names stay as they are, because that is what OBS
        # stores in the scene and what must never move under it.
        outputs = data.get("virtual", {}).get("outputs") or {}
        for key, alias in ((OBS_A, "headset_a"), (OBS_B, "headset_b")):
            if key in outputs and clean.get(alias):
                outputs[key]["description"] = f"TFCZ {clean[alias]}"

    return _commit(router, mutate)


def set_audio(router: Router, values: dict[str, Any]) -> dict[str, Any]:
    """Change the [audio] section: the buffer this router asks for, and whether
    level meters run at all."""
    latency = values.get("latency")
    meters = values.get("meters")

    def mutate(data: dict[str, Any]) -> None:
        audio = data.setdefault("audio", {})
        if latency is not None:
            audio["latency"] = str(latency)
        if meters is not None:
            audio["meters"] = bool(meters) if isinstance(meters, bool) else str(meters).lower() in ("1", "true", "yes", "on")

    return _commit(router, mutate)
