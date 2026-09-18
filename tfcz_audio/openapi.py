"""The HTTP API described as OpenAPI, built from one table.

The description lives next to the server instead of in a checked-in file so
it cannot quietly drift away from the code: a test walks every path in here
and asks the running server whether it exists.
"""

from __future__ import annotations

from typing import Any

from . import __version__

JSON = {"application/json": {"schema": {"type": "object"}}}


def _op(summary: str, description: str = "", *, tag: str = "Steuerung",
        params: list[dict[str, Any]] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    op: dict[str, Any] = {
        "tags": [tag],
        "summary": summary,
        "responses": {
            "200": {"description": "ok", "content": JSON},
            "401": {"description": "Token fehlt oder stimmt nicht", "content": JSON},
            "404": {"description": "Route, Voreinstellung oder Pfad gibt es nicht", "content": JSON},
        },
    }
    if description:
        op["description"] = description
    if params:
        op["parameters"] = params
    if body:
        op["requestBody"] = {"content": {"application/json": {"schema": body}}}
    return op


def _path(name: str, what: str) -> dict[str, Any]:
    return {"name": name, "in": "path", "required": True, "schema": {"type": "string"}, "description": what}


def _query(name: str, what: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "in": "query", "required": False, "schema": schema or {"type": "string"}, "description": what}


ROUTE = _path("route", "Name der Verbindung, z. B. hdmi_to_a")
PRESET = _path("preset", "Name der Voreinstellung")

VOLUME_BODY = {
    "type": "object",
    "properties": {
        "volume": {"type": "number", "minimum": 0, "maximum": 1.5, "description": "1.0 = normale Lautstärke"},
        "volume_db": {"type": "number", "description": "statt volume, in Dezibel"},
        "mute": {"type": "boolean"},
    },
}

PATHS: dict[str, dict[str, Any]] = {
    "/health": {"get": _op("Läuft der Dienst?", "Antwortet ohne PipeWire zu befragen; als Liveness-Probe gedacht.", tag="Zustand")},
    "/status": {"get": _op("Alles auf einen Blick", "Geräte, virtuelles Mikrofon, alle Verbindungen, erkannte Probleme.", tag="Zustand")},
    "/devices": {"get": _op("Geräte in PipeWire", tag="Zustand")},
    "/hardware": {"get": _op("Angestecktes Gerät für Gerät", "Mit der Erkennungsstrategie je Gerät (Seriennummer oder USB-Anschluss).", tag="Zustand")},
    "/levels": {"get": _op("Aktuelle Pegel", "Pro Gerät und für das OBS-Mikrofon.", tag="Zustand")},
    "/graph": {"get": _op("Alle Knoten und Verbindungen", "Der vollständige Signalweg, wie ihn die Grafik zeichnet.", tag="Zustand")},
    "/versions": {"get": _op("Versionen aller beteiligten Werkzeuge", tag="Diagnose",
                             params=[_query("fresh", "1 = Zwischenspeicher übergehen")])},
    "/routes": {"get": _op("Alle Verbindungen")},
    "/routes/{route}": {
        "get": _op("Eine Verbindung: Soll- und Istwert", params=[ROUTE]),
        "put": _op("Lautstärke und/oder Stummschaltung setzen", params=[ROUTE], body=VOLUME_BODY),
        "post": _op("Wie PUT, für Clients ohne PUT", params=[ROUTE], body=VOLUME_BODY),
    },
    "/routes/{route}/volume/{value}": {"post": _op(
        "Lautstärke ohne Datenkörper setzen",
        "Für Automationen, die nur eine URL abschicken können, etwa den Advanced Scene Switcher in OBS.",
        params=[ROUTE, _path("value", "0.0 bis 1.5, 1.0 = normal")])},
    "/routes/{route}/volume_db/{value}": {"post": _op("Lautstärke in Dezibel setzen", params=[ROUTE, _path("value", "z. B. -6")])},
    "/routes/{route}/mute": {"post": _op("Verbindung stummschalten", params=[ROUTE])},
    "/routes/{route}/unmute": {"post": _op("Stummschaltung aufheben", params=[ROUTE])},
    "/routes/{route}/toggle": {"post": _op("Stummschaltung umschalten", params=[ROUTE])},
    "/presets": {"get": _op("Voreinstellungen auflisten")},
    "/presets/{preset}": {"post": _op("Voreinstellung anwenden", params=[PRESET])},
    "/reset": {"post": _op("Alle Verbindungen auf die Werte aus der Einstellungsdatei zurücksetzen")},
    "/setup": {"post": _op(
        "Standardaufbau erzeugen", "Was der Einrichtungsassistent macht: beide Headsets, Spielton und OBS-Mikrofon verdrahten.",
        tag="Einrichtung",
        body={"type": "object", "properties": {
            "headset_a": {"type": "object", "description": "{mic, out, label}"},
            "headset_b": {"type": "object", "description": "{mic, out, label}"},
            "game": {"type": "string", "description": "Knotenname des HDMI-Eingangs"},
            "game_label": {"type": "string"}}})},
    "/fix/device/{alias}": {"post": _op("Gerät entstummen und Systemlautstärke anheben", tag="Einrichtung",
                                        params=[_path("alias", "Gerätekürzel, z. B. headset_a_mic")])},
    "/config": {"get": _op("Einstellungen als JSON", "Das Token wird nicht mitgeschickt.", tag="Einstellungen")},
    "/config/devices": {"put": _op("Zuordnung Kürzel zu Gerät ersetzen", tag="Einstellungen", body={"type": "object"})},
    "/config/labels": {"put": _op("Namen der Geräte ersetzen", tag="Einstellungen", body={"type": "object"})},
    "/config/audio": {"put": _op("Tonparameter ändern", tag="Einstellungen", body={"type": "object"})},
    "/config/save-defaults": {"post": _op("Aktuelle Werte als Standard speichern", tag="Einstellungen")},
    "/config/routes/{route}": {
        "put": _op("Verbindung anlegen oder ändern", tag="Einstellungen", params=[ROUTE],
                   body={"type": "object", "properties": {
                       "from": {"type": "string"}, "to": {"type": "string"},
                       "volume": {"type": "number"}, "mute": {"type": "boolean"},
                       "description": {"type": "string"}}}),
        "delete": _op("Verbindung löschen", tag="Einstellungen", params=[ROUTE])},
    "/config/presets/{preset}": {
        "put": _op("Voreinstellung anlegen oder ändern", tag="Einstellungen", params=[PRESET], body={"type": "object"}),
        "delete": _op("Voreinstellung löschen", tag="Einstellungen", params=[PRESET])},
    "/audio": {
        "get": _op("Puffergrösse des Tonsystems lesen", tag="Diagnose"),
        "put": _op("Puffergrösse setzen", "quantum in Bildern, 0 = automatisch. persist behält sie über den Neustart.",
                   tag="Diagnose", body={"type": "object", "properties": {
                       "quantum": {"type": "integer"}, "persist": {"type": "boolean"}}})},
    "/audio/dropouts": {"post": _op("Aussetzer messen", "Misst mit pw-top mit.", tag="Diagnose",
                                    params=[_query("seconds", "Messdauer", {"type": "number"})])},
    "/analysis": {"post": _op("Tonsystem analysieren", "Wer gibt den Takt vor, wo geht Ton verloren, wer hängt sonst noch im Graph.",
                              tag="Diagnose", params=[_query("seconds", "Messdauer", {"type": "number"})])},
    "/diagnostics": {
        "get": _op("Letzten Prüfbericht lesen", tag="Diagnose"),
        "post": _op("Prüfung starten", tag="Diagnose",
                    params=[_query("kind", "doctor oder selftest")])},
    "/logs": {"get": _op("Protokoll lesen", tag="Diagnose", params=[
        _query("level", "ab welchem Rang"), _query("limit", "wie viele Zeilen", {"type": "integer"}),
        _query("source", "memory oder journal")])},
    "/meters/watch": {"post": _op("Weitere Geräte zwei Minuten lang messen", tag="Diagnose",
                                  body={"type": "object", "properties": {"nodes": {"type": "array", "items": {"type": "object"}}}})},
    "/update": {
        "get": _op("Stand der letzten Aktualisierung", tag="Wartung"),
        "post": _op("Aktualisieren", "git pull und ./install.sh; der Dienst startet dabei neu.", tag="Wartung")},
}

TAGS = [
    {"name": "Zustand", "description": "Lesen, was gerade läuft."},
    {"name": "Steuerung", "description": "Lautstärken, Stummschaltung, Voreinstellungen — das, was eine Automation wie der Advanced Scene Switcher braucht."},
    {"name": "Einrichtung", "description": "Geräte verdrahten und Probleme beheben."},
    {"name": "Einstellungen", "description": "Die Einstellungsdatei ändern."},
    {"name": "Diagnose", "description": "Messen und nachsehen, wenn etwas nicht klingt."},
    {"name": "Wartung", "description": "Aktualisieren."},
]

DESCRIPTION = """Der TFCZ-Audiorouter steuert die Tonwege des Streaming-Rechners.

Alles lässt sich mit einer blossen URL auslösen, damit auch Automationen ohne
Datenkörper damit umgehen können, etwa der Advanced Scene Switcher in OBS:
`POST /routes/hdmi_to_a/volume/0.3`.

Ist in der Einstellungsdatei ein Token gesetzt, gehört es entweder in den
Kopf `Authorization: Bearer <token>` oder als `?token=<token>` an die URL.
"""


def document(base_url: str = "") -> dict[str, Any]:
    """The complete OpenAPI document. ``base_url`` makes «try it out» in the
    documentation page talk to this very daemon."""
    doc: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": "TFCZ Audio",
            "version": __version__,
            "description": DESCRIPTION,
        },
        "tags": TAGS,
        "paths": {path: dict(ops) for path, ops in PATHS.items()},
        "components": {
            "securitySchemes": {
                "bearer": {"type": "http", "scheme": "bearer"},
                "token": {"type": "apiKey", "in": "query", "name": "token"},
            },
        },
        "security": [{"bearer": []}, {"token": []}],
    }
    if base_url:
        doc["servers"] = [{"url": base_url.rstrip("/"), "description": "dieser Router"}]
    return doc
