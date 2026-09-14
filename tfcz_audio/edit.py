"""Config edits requested over the API: validate through parse(), write the
file, hot-reload the router. Every function returns the new status."""

from __future__ import annotations

import copy
from typing import Any, Callable

from .config import OBS_MIC, Config, ConfigError, parse, save, to_dict
from .router import Router, UnknownPreset, UnknownRoute


class EditError(ConfigError):
    pass


def _commit(router: Router, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    data = to_dict(router.cfg)
    mutate(data)
    new_cfg = parse(copy.deepcopy(data))
    new_cfg.path = router.cfg.path
    if new_cfg.path is not None:
        save(new_cfg)
    return router.reload(new_cfg)


def set_devices(router: Router, devices: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, str] = {}
    for alias, node in devices.items():
        if not isinstance(alias, str) or not alias.strip():
            raise EditError("device alias must be a non-empty string")
        if not isinstance(node, str) or not node.strip():
            raise EditError(f"device '{alias}': node name must be a non-empty string")
        clean[alias.strip()] = node.strip()

    def mutate(data: dict[str, Any]) -> None:
        used = {r["from"] for r in data["routes"].values()} | {r["to"] for r in data["routes"].values()}
        removed = set(data["devices"]) - set(clean)
        for alias in removed & used:
            raise EditError(f"device '{alias}' is still used by a route")
        data["devices"] = clean

    return _commit(router, mutate)


def upsert_route(router: Router, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    if "from" not in spec or "to" not in spec:
        raise EditError("route needs 'from' and 'to'")
    entry: dict[str, Any] = {"from": str(spec["from"]), "to": str(spec["to"])}
    if spec.get("description"):
        entry["description"] = str(spec["description"])
    if "volume" in spec:
        try:
            entry["volume"] = float(spec["volume"])
        except (TypeError, ValueError):
            raise EditError("volume must be a number") from None
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
        raise EditError("preset needs at least one route")

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
    return data
