#!/usr/bin/env python3
"""Richtet die Automation „Kaffeemaschine an, wenn die Alarmanlage morgens
unscharf geschaltet wird“ direkt in Home Assistant ein.

Das Skript braucht nur Python 3, keine Zusatzpakete. Es spricht mit Home
Assistant über dessen REST-API, sucht Alarmanlage und Kaffeemaschine
(Home Connect) heraus, legt die Automation an und prüft, dass sie aktiv ist.
Ein zweiter Aufruf aktualisiert die Automation, statt sie doppelt anzulegen.

Die Alarmanlage kann eine Alarmzentrale (alarm_control_panel) sein oder eine
andere Entität, die ihren Zustand meldet – z. B. eine Systemvariable der
Homematic-CCU (OpenCCU). Dann fragt das Skript, welcher Zustand „unscharf“
bedeutet (oder nimmt --unscharf). Ist die Entität in Home Assistant noch
deaktiviert, wie Homematic(IP) Local Systemvariablen anlegt, aktiviert das
Skript sie auf Wunsch selbst.

Aufruf im Heimnetz, z. B. auf einem Mac oder PC:

    python3 installieren.py --url http://homeassistant.local:8123

Es fragt dann nach Benutzername und Passwort von Home Assistant (ein Benutzer
mit Administratorrechten). Statt Benutzername und Passwort geht auch ein
langlebiger Zugangstoken, als Umgebungsvariable HA_TOKEN oder mit --token.
Alle Optionen zeigt: python3 installieren.py --hilfe
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

AUTOMATION_ID = "siemens_kaffee_bei_unscharf"
ALIAS = "Kaffeemaschine an, wenn die Alarmanlage morgens unscharf geschaltet wird"
STANDARD_URL = "http://homeassistant.local:8123"
STANDARD_VON = "05:00"
STANDARD_BIS = "09:00"
ZEITLIMIT = 30  # Sekunden pro Anfrage
STANDARD_UNSCHARF = "disarmed"  # Zustand „unscharf“ einer Alarmzentrale

# Entitäten, die den Zustand der Alarmanlage melden können – eine Alarmzentrale
# oder z. B. eine Systemvariable der Homematic-CCU als Sensor, Auswahl oder Schalter.
ALARM_DOMAINS = (
    "alarm_control_panel", "sensor", "binary_sensor", "select", "input_select",
    "input_boolean", "switch",
)
ALARM_WOERTER = re.compile(
    r"alarm|scharf|sicherheit|security|secur|h[üu]llschutz|vollschutz|einbruch|intrusion",
    re.IGNORECASE,
)
# … aber Rauch-/Wassermelder, Batteriewarnungen und Meldungszähler melden nie,
# ob die Anlage scharf ist.
ALARM_RAUSCHEN = re.compile(r"rauch|smoke|batter|wasser|water|leck|leak|meldungen|messages", re.IGNORECASE)
# Besonders wahrscheinliche Treffer stehen in der Auswahl oben.
ALARM_STARK = re.compile(
    r"scharf|h[üu]llschutz|vollschutz|alarmanlage|alarmzentrale|alarmmodus|alarm_mode|security_system",
    re.IGNORECASE,
)

# Alle Entitäten aus dem Register von Home Assistant – auch deaktivierte, die
# keinen Zustand haben. Homematic(IP) Local legt Systemvariablen der CCU so an.
# Der Weg führt über die Titel der Konfigurationseinträge, weil
# integration_entities() nur dafür auch deaktivierte Entitäten liefert.
REGISTER_TEMPLATE = """
{%- set ns = namespace(eintraege=[]) -%}
{%- for zustand in states -%}
  {%- set geraet = device_id(zustand.entity_id) -%}
  {%- if geraet -%}
    {%- for eintrag in device_attr(geraet, 'config_entries') or [] -%}
      {%- set ns.eintraege = ns.eintraege + [eintrag] -%}
    {%- endfor -%}
  {%- endif -%}
{%- endfor -%}
{%- set ns2 = namespace(ids=[]) -%}
{%- for eintrag in ns.eintraege | unique -%}
  {%- set ns2.ids = ns2.ids + (integration_entities(config_entry_attr(eintrag, 'title')) | list) -%}
{%- endfor -%}
{{ ns2.ids | unique | list | to_json }}
"""

# Woran man den „Einschalter“ (englisch „Power“) eines Home-Connect-Geräts
# und eine Kaffeemaschine erkennt – an Entitäts-IDs, Namen und Modell.
EINSCHALTER = re.compile(r"(^|[_ ])(power|einschalter)$", re.IGNORECASE)
KAFFEEMASCHINE = re.compile(
    r"kaffee|coffee|espresso|cappuccino|latte|bean|bohne|milk|milch|drip_tray"
    r"|tropfschale|hot_water|heisswasser|heißwasser|vollautomat|\beq\b|eq[._ ]?\d",
    re.IGNORECASE,
)

# Liefert alle Home-Connect-Geräte mit Namen, Hersteller, Modell und ihren
# Entitäten – als JSON, gerendert von Home Assistant selbst.
GERAETE_TEMPLATE = """
{%- set ns = namespace(geraete=[]) -%}
{%- for geraet in integration_entities('home_connect')
      | map('device_id') | reject('none') | unique -%}
  {%- set ns.geraete = ns.geraete + [{
        'name': device_attr(geraet, 'name_by_user') or device_attr(geraet, 'name'),
        'hersteller': device_attr(geraet, 'manufacturer'),
        'modell': device_attr(geraet, 'model'),
        'entitaeten': device_entities(geraet),
      }] -%}
{%- endfor -%}
{{ ns.geraete | to_json }}
"""


class Abbruch(Exception):
    """Ein Fehler, der dem Benutzer erklärt wird – ohne Python-Fehlermeldung."""


def automation_config(
    alarmanlage: "str | list[str]",
    kaffeemaschine: str,
    von: str,
    bis: str,
    unscharf: str = STANDARD_UNSCHARF,
) -> dict[str, Any]:
    """Dieselbe Automation wie beispiele/automation_ohne_blueprint.yaml.

    ``alarmanlage`` darf auch eine Liste sein, z. B. je eine Systemvariable für
    Hüllschutz und Vollschutz – das Unscharfschalten jeder davon zählt.
    """
    anlagen = [alarmanlage] if isinstance(alarmanlage, str) else list(alarmanlage)
    # Nicht reagieren, wenn die Alarmanlage nach einem Neustart wieder erreichbar
    # wird oder (bei einer Alarmzentrale) ein Scharfschalten abgebrochen wurde.
    nicht_von = ["unavailable", "unknown"]
    if any(anlage.startswith("alarm_control_panel.") for anlage in anlagen):
        nicht_von.append("arming")
    return {
        "alias": ALIAS,
        "description": (
            "Schaltet den Siemens-Kaffeevollautomaten ein, wenn die Alarmanlage "
            f"zwischen {von[:5]} und {bis[:5]} Uhr unscharf geschaltet wird."
        ),
        "mode": "single",
        "max_exceeded": "silent",
        "triggers": [
            {
                "trigger": "state",
                "entity_id": anlagen[0] if len(anlagen) == 1 else anlagen,
                "to": unscharf,
                "not_from": nicht_von,
            }
        ],
        "conditions": [
            {"condition": "time", "after": von, "before": bis},
            {
                "condition": "not",
                "conditions": [
                    {"condition": "state", "entity_id": kaffeemaschine, "state": "on"}
                ],
            },
        ],
        "actions": [{"action": "switch.turn_on", "target": {"entity_id": kaffeemaschine}}],
    }


def uhrzeit(text: str) -> str:
    """„5:00“, „05:00“ oder „05:00:00“ → „05:00:00“."""
    treffer = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text.strip())
    if not treffer:
        raise argparse.ArgumentTypeError(f"„{text}“ ist keine Uhrzeit wie 05:00")
    stunde, minute, sekunde = (int(teil or 0) for teil in treffer.groups())
    if stunde > 23 or minute > 59 or sekunde > 59:
        raise argparse.ArgumentTypeError(f"„{text}“ ist keine gültige Uhrzeit")
    return f"{stunde:02d}:{minute:02d}:{sekunde:02d}"


def url_bereinigen(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    if not urllib.parse.urlparse(url).netloc:
        raise Abbruch(f"„{url}“ ist keine gültige Adresse.")
    return url


class HomeAssistant:
    """Ein kleiner Client für die REST-API von Home Assistant."""

    def __init__(self, url: str, token: Optional[str] = None) -> None:
        self.url = url
        self.token = token

    def anfrage(
        self,
        methode: str,
        pfad: str,
        *,
        json_daten: Any = None,
        formular: Optional[dict[str, str]] = None,
        mit_token: bool = True,
    ) -> tuple[int, str]:
        """Schickt eine Anfrage und gibt (Statuscode, Antworttext) zurück."""
        kopfzeilen = {"Accept": "application/json, text/plain"}
        daten: Optional[bytes] = None
        if json_daten is not None:
            daten = json.dumps(json_daten).encode()
            kopfzeilen["Content-Type"] = "application/json"
        elif formular is not None:
            daten = urllib.parse.urlencode(formular).encode()
            kopfzeilen["Content-Type"] = "application/x-www-form-urlencoded"
        if mit_token and self.token:
            kopfzeilen["Authorization"] = f"Bearer {self.token}"
        anfrage = urllib.request.Request(
            self.url + pfad, data=daten, headers=kopfzeilen, method=methode
        )
        try:
            with urllib.request.urlopen(anfrage, timeout=ZEITLIMIT) as antwort:
                return antwort.status, antwort.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as fehler:
            return fehler.code, fehler.read().decode("utf-8", "replace")
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as fehler:
            grund = getattr(fehler, "reason", fehler)
            raise Abbruch(
                f"Home Assistant ist unter {self.url} nicht erreichbar ({grund}).\n"
                "  Läuft dieses Skript im selben Netz wie Home Assistant? Stimmt die "
                "Adresse (--url)? Für den Zugriff von außerhalb braucht es die "
                "Fernzugriffs-Adresse, z. B. https://….ui.nabu.casa."
            ) from fehler

    def get_json(self, pfad: str) -> Any:
        status, text = self.anfrage("GET", pfad)
        return self._json(status, text, "GET", pfad)

    def post_json(self, pfad: str, daten: Any) -> Any:
        status, text = self.anfrage("POST", pfad, json_daten=daten)
        return self._json(status, text, "POST", pfad)

    def _json(self, status: int, text: str, methode: str, pfad: str) -> Any:
        if status == 401:
            raise Abbruch(
                "Home Assistant lehnt die Anmeldung ab (401). Ist der Token noch "
                "gültig und gehört er einem Benutzer mit Administratorrechten?"
            )
        if status >= 400:
            raise Abbruch(f"{methode} {pfad} fehlgeschlagen ({status}): {fehlermeldung(text)}")
        try:
            return json.loads(text) if text else None
        except json.JSONDecodeError as fehler:
            raise Abbruch(f"Unerwartete Antwort auf {methode} {pfad}: {text[:200]}") from fehler


def fehlermeldung(text: str) -> str:
    try:
        return str(json.loads(text).get("message", text))
    except (json.JSONDecodeError, AttributeError):
        return text.strip()[:300]


def anmelden(
    ha: HomeAssistant,
    benutzer: str,
    passwort: str,
    code_abfragen: Optional[Callable[[], str]] = None,
) -> str:
    """Meldet sich mit Benutzername und Passwort an, wie es die Oberfläche tut.

    Setzt ``ha.token`` auf einen kurzlebigen Zugangstoken und gibt den
    Refresh-Token zurück, der am Ende wieder widerrufen wird.
    """
    client_id = ha.url + "/"
    status, text = ha.anfrage("GET", "/auth/providers", mit_token=False)
    anbieter = ha._json(status, text, "GET", "/auth/providers")
    if isinstance(anbieter, dict):  # seit 2024 ein Objekt, davor eine Liste
        anbieter = anbieter.get("providers", [])
    passende = [a for a in anbieter if a.get("type") == "homeassistant"] or anbieter
    if not passende:
        raise Abbruch("Home Assistant bietet keine Anmeldung mit Benutzername und Passwort an.")
    anbieter = passende[0]

    status, text = ha.anfrage(
        "POST",
        "/auth/login_flow",
        json_daten={
            "client_id": client_id,
            "handler": [anbieter["type"], anbieter.get("id")],
            "redirect_uri": client_id + "?auth_callback=1",
        },
        mit_token=False,
    )
    schritt = ha._json(status, text, "POST", "/auth/login_flow")
    eingaben: dict[str, Any] = {"username": benutzer, "password": passwort}
    while schritt.get("type") == "form":
        if schritt.get("errors"):
            fehler = schritt["errors"].get("base", schritt["errors"])
            if fehler == "invalid_auth":
                raise Abbruch("Benutzername oder Passwort ist falsch.")
            raise Abbruch(f"Anmeldung fehlgeschlagen: {fehler}")
        if schritt.get("step_id") == "mfa":
            if code_abfragen is None:
                raise Abbruch(
                    "Dieser Benutzer hat die Zwei-Faktor-Anmeldung aktiviert. Bitte das "
                    "Skript direkt im Terminal ausführen oder einen Token (HA_TOKEN) nutzen."
                )
            eingaben = {"code": code_abfragen()}
        status, text = ha.anfrage(
            "POST",
            f"/auth/login_flow/{schritt['flow_id']}",
            json_daten={"client_id": client_id, **eingaben},
            mit_token=False,
        )
        schritt = ha._json(status, text, "POST", "/auth/login_flow/…")
    if schritt.get("type") != "create_entry":
        raise Abbruch(f"Unerwarteter Schritt bei der Anmeldung: {schritt}")

    status, text = ha.anfrage(
        "POST",
        "/auth/token",
        formular={
            "grant_type": "authorization_code",
            "code": schritt["result"],
            "client_id": client_id,
        },
        mit_token=False,
    )
    token = ha._json(status, text, "POST", "/auth/token")
    ha.token = token["access_token"]
    return token["refresh_token"]


def abmelden(ha: HomeAssistant, refresh_token: str) -> None:
    """Widerruft den bei der Anmeldung erhaltenen Refresh-Token."""
    ha.anfrage(
        "POST", "/auth/token", formular={"action": "revoke", "token": refresh_token}
    )


def name_von(zustand: dict[str, Any]) -> str:
    return str(zustand.get("attributes", {}).get("friendly_name") or zustand["entity_id"])


def ist_einschalter(entity_id: str, zustaende: dict[str, dict[str, Any]]) -> bool:
    objekt_id = entity_id.split(".", 1)[1]
    name = name_von(zustaende[entity_id]) if entity_id in zustaende else ""
    return bool(EINSCHALTER.search(objekt_id) or EINSCHALTER.search(name))


def ist_kaffeemaschine(geraet: dict[str, Any]) -> bool:
    merkmale = " ".join(
        str(wert or "") for wert in (geraet["name"], geraet["modell"])
    ) + " " + " ".join(geraet["entitaeten"])
    return bool(KAFFEEMASCHINE.search(merkmale))


def home_connect_geraete(ha: HomeAssistant) -> list[dict[str, Any]]:
    status, text = ha.anfrage("POST", "/api/template", json_daten={"template": GERAETE_TEMPLATE})
    if status >= 400:
        raise Abbruch(f"Home Assistant konnte die Geräteliste nicht liefern: {fehlermeldung(text)}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as fehler:
        raise Abbruch(f"Unerwartete Geräteliste: {text[:200]}") from fehler


def kandidaten_kaffeemaschine(
    ha: HomeAssistant, zustaende: dict[str, dict[str, Any]]
) -> list[tuple[str, str]]:
    """Mögliche Einschalter der Kaffeemaschine: (entity_id, Beschreibung)."""
    geraete = home_connect_geraete(ha)
    kaffeemaschinen = [g for g in geraete if ist_kaffeemaschine(g)] or geraete
    kandidaten: list[tuple[str, str]] = []
    for geraet in kaffeemaschinen:
        # Nur Schalter, die es auch als Zustand gibt – also nicht deaktiviert sind.
        schalter = [e for e in geraet["entitaeten"] if e.startswith("switch.") and e in zustaende]
        einschalter = [e for e in schalter if ist_einschalter(e, zustaende)] or schalter
        for entity_id in einschalter:
            zustand = zustaende.get(entity_id, {"entity_id": entity_id})
            hersteller_modell = " ".join(
                str(w) for w in (geraet["hersteller"], geraet["modell"]) if w
            )
            kandidaten.append(
                (entity_id, f"„{name_von(zustand)}“, {hersteller_modell or geraet['name']}")
            )
    if kandidaten:
        return kandidaten
    # Kein Home Connect: Nach Schaltern suchen, die wie ein Einschalter einer
    # Kaffeemaschine heißen.
    for entity_id, zustand in sorted(zustaende.items()):
        if not entity_id.startswith("switch.") or not ist_einschalter(entity_id, zustaende):
            continue
        if KAFFEEMASCHINE.search(entity_id) or KAFFEEMASCHINE.search(name_von(zustand)):
            kandidaten.append((entity_id, f"„{name_von(zustand)}“"))
    return kandidaten


def alarm_treffer(entity_id: str, name: str = "") -> int:
    """0 = kommt nicht infrage, 1 = klingt nach Alarm, 2 = klingt nach Scharf-/Unscharf-Zustand."""
    text = f"{entity_id} {name}"
    if (
        entity_id.split(".", 1)[0] not in ALARM_DOMAINS
        or not ALARM_WOERTER.search(text)
        or ALARM_RAUSCHEN.search(text)
    ):
        return 0
    return 2 if ALARM_STARK.search(text) else 1


def kandidaten_alarmanlage(zustaende: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """Alarmzentralen – oder, wenn es keine gibt, Entitäten, die nach Alarmanlage klingen."""
    zentralen = [
        (entity_id, f"„{name_von(zustand)}“")
        for entity_id, zustand in sorted(zustaende.items())
        if entity_id.startswith("alarm_control_panel.")
    ]
    if zentralen:
        return zentralen
    treffer = sorted(
        zustaende.items(),
        key=lambda paar: (-alarm_treffer(paar[0], name_von(paar[1])), paar[0]),
    )
    return [
        (entity_id, f"„{name_von(zustand)}“, Zustand: {zustand['state']}")
        for entity_id, zustand in treffer
        if alarm_treffer(entity_id, name_von(zustand))
    ]


def register_entitaeten(ha: HomeAssistant) -> list[str]:
    """Alle Entitäten laut Register von Home Assistant – auch deaktivierte ohne Zustand."""
    status, text = ha.anfrage("POST", "/api/template", json_daten={"template": REGISTER_TEMPLATE})
    if status >= 400:
        return []  # z. B. ältere Home-Assistant-Version ohne config_entry_attr
    try:
        return list(json.loads(text))
    except json.JSONDecodeError:
        return []


def deaktivierte_alarm_entitaeten(
    ha: HomeAssistant, zustaende: dict[str, dict[str, Any]]
) -> list[str]:
    """Entitäten, die nach Alarmanlage klingen, aber keinen Zustand haben.

    Das sind deaktivierte Entitäten – Homematic(IP) Local legt Systemvariablen
    der CCU standardmäßig so an – oder solche einer gerade nicht geladenen
    Integration. Auswählen kann man sie erst, wenn sie aktiv sind.
    """
    return sorted(
        entity_id
        for entity_id in set(register_entitaeten(ha))
        if entity_id not in zustaende and alarm_treffer(entity_id)
    )


def hinweis_deaktiviert(entity_ids: list[str]) -> str:
    liste = "\n".join(f"    {entity_id}" for entity_id in entity_ids)
    return (
        "Diese Entitäten gibt es in Home Assistant, sie sind aber deaktiviert und "
        f"deshalb nicht auswählbar:\n{liste}\n"
        "  Mit --alarmanlage <entity_id> angegeben, aktiviert dieses Skript sie selbst. "
        "Von Hand: Einstellungen → Geräte & Dienste → Entitäten → Filter "
        "„Deaktivierte Entitäten anzeigen“ → Entität öffnen → Zahnrad → „Aktiviert“ "
        "einschalten. Bei Homematic(IP) Local geht es auch mit „hahm“ in der "
        "Beschreibung der Systemvariable in der CCU."
    )


class WebSocket:
    """Ein kleiner WebSocket-Client (RFC 6455) für die WebSocket-API von Home Assistant.

    Nur für das nötig, was die REST-API nicht kann: eine Entität aktivieren.
    """

    def __init__(self, url: str, token: Optional[str]) -> None:
        teile = urllib.parse.urlparse(url)
        sicher = teile.scheme == "https"
        host = teile.hostname or ""
        port = teile.port or (443 if sicher else 80)
        try:
            sock: socket.socket = socket.create_connection((host, port), timeout=ZEITLIMIT)
            if sicher:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        except OSError as fehler:
            raise Abbruch(f"Keine WebSocket-Verbindung zu {url} ({fehler}).") from fehler
        self.sock = sock
        self.puffer = b""
        self.naechste_id = 0
        self._verbinden(teile)
        if self.empfangen().get("type") != "auth_required":
            raise Abbruch("Unerwartete Begrüßung der WebSocket-API von Home Assistant.")
        self.senden({"type": "auth", "access_token": token})
        antwort = self.empfangen()
        if antwort.get("type") != "auth_ok":
            raise Abbruch(f"WebSocket-Anmeldung fehlgeschlagen: {antwort.get('message', antwort)}")

    def _verbinden(self, teile: urllib.parse.ParseResult) -> None:
        schluessel = base64.b64encode(os.urandom(16)).decode()
        pfad = teile.path.rstrip("/") + "/api/websocket"
        self.sock.sendall(
            (
                f"GET {pfad} HTTP/1.1\r\nHost: {teile.netloc}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {schluessel}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        antwort = b""
        while b"\r\n\r\n" not in antwort:
            teil = self.sock.recv(4096)
            if not teil:
                raise Abbruch("Home Assistant hat die WebSocket-Verbindung nicht angenommen.")
            antwort += teil
        kopf, _, self.puffer = antwort.partition(b"\r\n\r\n")
        zeilen = kopf.decode("latin-1").split("\r\n")
        if " 101 " not in zeilen[0]:
            raise Abbruch(f"Home Assistant lehnt die WebSocket-Verbindung ab: {zeilen[0]}")
        erwartet = base64.b64encode(
            hashlib.sha1((schluessel + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        for zeile in zeilen[1:]:
            name, _, wert = zeile.partition(":")
            if name.strip().lower() == "sec-websocket-accept" and wert.strip() != erwartet:
                raise Abbruch("Ungültige WebSocket-Antwort von Home Assistant.")

    def _lesen(self, anzahl: int) -> bytes:
        while len(self.puffer) < anzahl:
            teil = self.sock.recv(65536)
            if not teil:
                raise Abbruch("Die WebSocket-Verbindung zu Home Assistant wurde unterbrochen.")
            self.puffer += teil
        daten, self.puffer = self.puffer[:anzahl], self.puffer[anzahl:]
        return daten

    def _rahmen_senden(self, opcode: int, daten: bytes) -> None:
        kopf = bytearray([0x80 | opcode])
        if len(daten) < 126:
            kopf.append(0x80 | len(daten))
        elif len(daten) < 65536:
            kopf.append(0x80 | 126)
            kopf += struct.pack("!H", len(daten))
        else:
            kopf.append(0x80 | 127)
            kopf += struct.pack("!Q", len(daten))
        maske = os.urandom(4)  # Clients müssen ihre Daten maskieren
        kopf += maske
        self.sock.sendall(bytes(kopf) + bytes(b ^ maske[i % 4] for i, b in enumerate(daten)))

    def senden(self, nachricht: dict[str, Any]) -> None:
        self._rahmen_senden(0x1, json.dumps(nachricht).encode())

    def empfangen(self) -> dict[str, Any]:
        nutzlast = b""
        while True:
            erstes, zweites = self._lesen(2)
            fin, opcode = erstes & 0x80, erstes & 0x0F
            laenge = zweites & 0x7F
            if laenge == 126:
                laenge = struct.unpack("!H", self._lesen(2))[0]
            elif laenge == 127:
                laenge = struct.unpack("!Q", self._lesen(8))[0]
            maske = self._lesen(4) if zweites & 0x80 else b""
            daten = self._lesen(laenge)
            if maske:
                daten = bytes(b ^ maske[i % 4] for i, b in enumerate(daten))
            if opcode == 0x8:
                raise Abbruch("Home Assistant hat die WebSocket-Verbindung geschlossen.")
            if opcode == 0x9:
                self._rahmen_senden(0xA, daten)  # Ping → Pong
                continue
            if opcode == 0xA:
                continue
            nutzlast += daten
            if fin:
                return json.loads(nutzlast.decode())

    def befehl(self, **nachricht: Any) -> Any:
        """Schickt einen Befehl und gibt dessen Ergebnis zurück."""
        self.naechste_id += 1
        self.senden({"id": self.naechste_id, **nachricht})
        while True:
            antwort = self.empfangen()
            if antwort.get("id") == self.naechste_id and antwort.get("type") == "result":
                if not antwort.get("success"):
                    fehler = antwort.get("error") or {}
                    raise Abbruch(f"Home Assistant meldet: {fehler.get('message', fehler)}")
                return antwort.get("result")

    def schliessen(self) -> None:
        try:
            self._rahmen_senden(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        finally:
            self.sock.close()


def entitaet_aktivieren(ha: HomeAssistant, entity_id: str) -> None:
    """Aktiviert eine deaktivierte Entität – wie der Schalter „Aktiviert“ in ihren Einstellungen."""
    ws = WebSocket(ha.url, ha.token)
    try:
        ergebnis = ws.befehl(
            type="config/entity_registry/update", entity_id=entity_id, disabled_by=None
        )
    finally:
        ws.schliessen()
    if (ergebnis or {}).get("require_restart"):
        raise Abbruch(
            f"{entity_id} ist jetzt aktiviert, aber Home Assistant muss dafür neu gestartet "
            "werden (Einstellungen → System → Neu starten). Danach dieses Skript erneut ausführen."
        )


def auf_zustand_warten(ha: HomeAssistant, entity_id: str, sekunden: int = 150) -> dict[str, Any]:
    """Wartet, bis die Entität einen Zustand meldet.

    Nach dem Aktivieren lädt Home Assistant die Integration erst nach etwa
    30 Sekunden neu, und der erste Wert braucht noch einen Moment.
    """
    print(
        f"  Warte auf {entity_id} – Home Assistant lädt die Integration nach etwa "
        "30 Sekunden neu …",
        end="",
        flush=True,
    )
    for versuch in range(sekunden):
        status, text = ha.anfrage("GET", f"/api/states/{entity_id}")
        if status == 200:
            zustand = json.loads(text)
            if zustand.get("state") not in ("unknown", "unavailable"):
                print(" da.")
                return zustand
        if versuch % 5 == 4:
            print(".", end="", flush=True)
        time.sleep(1)
    print()
    raise Abbruch(
        f"{entity_id} meldet auch nach {sekunden} Sekunden keinen Zustand. Ist die Integration "
        "verbunden? Unter Einstellungen → Geräte & Dienste nachsehen und das Skript erneut ausführen."
    )


def aktivieren_und_warten(
    ha: HomeAssistant, entity_ids: list[str], zustaende: dict[str, dict[str, Any]]
) -> None:
    for entity_id in entity_ids:
        entitaet_aktivieren(ha, entity_id)
        print(f"✓ {entity_id} aktiviert")
    for entity_id in entity_ids:
        zustaende[entity_id] = auf_zustand_warten(ha, entity_id)


def alarm_entitaet_bereitstellen(
    ha: HomeAssistant, entity_id: str, zustaende: dict[str, dict[str, Any]]
) -> str:
    """Prüft eine mit --alarmanlage angegebene Entität; ist sie nur deaktiviert, wird sie aktiviert."""
    domain_pruefen(entity_id, ALARM_DOMAINS, "--alarmanlage")
    if entity_id not in zustaende and entity_id in register_entitaeten(ha):
        print(f"  {entity_id} ist deaktiviert – wird aktiviert.")
        aktivieren_und_warten(ha, [entity_id], zustaende)
    return entitaet_pruefen(entity_id, ALARM_DOMAINS, zustaende, "--alarmanlage")


def deaktivierte_anbieten(
    ha: HomeAssistant, deaktiviert: list[str], zustaende: dict[str, dict[str, Any]]
) -> list[str]:
    """Bietet im Terminal an, deaktivierte Entitäten zu aktivieren und als Alarmanlage zu nehmen."""
    gewaehlt = menue(
        "Diese Entitäten gibt es in Home Assistant, sie sind aber deaktiviert. Soll ich eine "
        "davon aktivieren und als Alarmanlage verwenden? (mehrere möglich)",
        deaktiviert,
        mehrere=True,
        optional=True,
    )
    entity_ids = [deaktiviert[index] for index in gewaehlt]
    if entity_ids:
        aktivieren_und_warten(ha, entity_ids, zustaende)
    return entity_ids


def unscharf_bestimmen(
    alarmanlagen: list[str],
    zustaende: dict[str, dict[str, Any]],
    vorgabe: Optional[str],
    interaktiv: bool,
) -> str:
    """Welcher Zustand der Alarmanlage „unscharf“ bedeutet.

    Bei einer Alarmzentrale ist das ``disarmed``; bei allem anderen (z. B. einer
    Systemvariable der CCU) muss es angegeben oder erfragt werden. Bei mehreren
    Entitäten gilt derselbe Zustand für alle.
    """
    if vorgabe:
        return vorgabe
    andere = [a for a in alarmanlagen if not a.startswith("alarm_control_panel.")]
    if not andere:
        return STANDARD_UNSCHARF
    alarmanlage = andere[0]
    zustand = zustaende[alarmanlage]
    aktuell = str(zustand["state"])
    optionen = zustand.get("attributes", {}).get("options")
    if alarmanlage.split(".", 1)[0] in ("binary_sensor", "switch", "input_boolean"):
        optionen = ["off", "on"]  # „off“ = aus, „on“ = an
    frage = (
        f"„{name_von(zustand)}“ ist keine Alarmzentrale. Welcher Zustand bedeutet "
        f"„unscharf“? Aktuell: „{aktuell}“"
    )
    if not interaktiv:
        moeglich = f", möglich: {', '.join(map(str, optionen))}" if optionen else ""
        raise Abbruch(f"{frage}{moeglich}.\n  Bitte mit --unscharf <Zustand> angeben.")
    if optionen:
        [gewaehlt] = menue(frage, [str(o) for o in optionen])
        return str(optionen[gewaehlt])
    print(f"\n{frage}")
    antwort = input(f"Zustand für „unscharf“ [{aktuell}]: ").strip()
    return antwort or aktuell


def menue(
    frage: str, eintraege: list[str], mehrere: bool = False, optional: bool = False
) -> list[int]:
    """Lässt den Benutzer Einträge wählen und gibt deren Indizes zurück.

    Mit ``mehrere`` dürfen es mehrere Nummern sein, durch Komma getrennt; mit
    ``optional`` darf die Antwort leer bleiben (dann kommt eine leere Liste).
    """
    print(f"\n{frage}")
    for nr, eintrag in enumerate(eintraege, 1):
        print(f"  {nr}) {eintrag}")
    aufforderung = (
        f"Nummer{'n, durch Komma getrennt,' if mehrere else ''} [1-{len(eintraege)}]"
        + (" oder Enter zum Überspringen" if optional else "")
        + ": "
    )
    while True:
        eingabe = input(aufforderung).strip()
        if optional and not eingabe:
            return []
        nummern = [teil.strip() for teil in eingabe.replace(";", ",").split(",")]
        if (
            nummern
            and all(nummer.isdigit() and 1 <= int(nummer) <= len(eintraege) for nummer in nummern)
            and (mehrere or len(nummern) == 1)
        ):
            return sorted({int(nummer) - 1 for nummer in nummern})


def auswaehlen(
    kandidaten: list[tuple[str, str]],
    fehlt: str,
    fuer: str,
    option: str,
    interaktiv: bool,
    hinweis: str = "",
    mehrere: bool = False,
) -> list[str]:
    """Wählt Kandidaten – automatisch, per Rückfrage oder gar nicht.

    ``fehlt`` („Keine Alarmanlage“) und ``fuer`` („die Alarmanlage“) sind die
    Formen für die Meldungen „… in Home Assistant gefunden“ bzw.
    „Mehrere Möglichkeiten für …“. Mit ``mehrere`` dürfen mehrere gewählt werden.
    """
    mehrfach = " (mehrfach möglich)" if mehrere else ""
    if not kandidaten:
        raise Abbruch(
            f"{fehlt} in Home Assistant gefunden. {hinweis}".strip()
            + f"\n  Die Entitäts-ID lässt sich auch direkt angeben: {option} <entity_id>{mehrfach}"
        )
    if len(kandidaten) == 1:
        return [kandidaten[0][0]]
    liste = "\n".join(
        f"  {nr}) {entity_id}  {beschreibung}"
        for nr, (entity_id, beschreibung) in enumerate(kandidaten, 1)
    )
    if not interaktiv:
        raise Abbruch(
            f"Mehrere Möglichkeiten für {fuer} gefunden:\n{liste}\n"
            f"  Bitte mit {option} <entity_id> angeben, welche gemeint ist{mehrfach}."
            + (f"\n  {hinweis}" if hinweis else "")
        )
    if hinweis:
        print(f"\n{hinweis}")
    gewaehlt = menue(
        f"Mehrere Möglichkeiten für {fuer} gefunden – welche "
        + ("sind gemeint? (mehrere möglich)" if mehrere else "ist gemeint?"),
        [f"{entity_id}  {beschreibung}" for entity_id, beschreibung in kandidaten],
        mehrere,
    )
    return [kandidaten[index][0] for index in gewaehlt]


def domain_pruefen(entity_id: str, domains: tuple[str, ...], option: str) -> None:
    if entity_id.split(".", 1)[0] not in domains:
        bereiche = ", ".join(f"„{domain}“" for domain in domains)
        raise Abbruch(f"{option} erwartet eine Entität aus dem Bereich {bereiche}, nicht „{entity_id}“.")


def entitaet_pruefen(
    entity_id: str,
    domains: tuple[str, ...],
    zustaende: dict[str, dict[str, Any]],
    option: str,
) -> str:
    domain_pruefen(entity_id, domains, option)
    if entity_id not in zustaende:
        raise Abbruch(f"Die Entität „{entity_id}“ gibt es in Home Assistant nicht.")
    return entity_id


def automation_entitaet(ha: HomeAssistant) -> Optional[dict[str, Any]]:
    """Die Automation mit unserer ID – oder None, wenn sie (noch) nicht geladen ist."""
    for zustand in ha.get_json("/api/states"):
        if zustand["entity_id"].startswith("automation.") and (
            zustand.get("attributes", {}).get("id") == AUTOMATION_ID
        ):
            return zustand
    return None


def installieren(ha: HomeAssistant, config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Legt die Automation an bzw. aktualisiert sie und wartet, bis sie aktiv ist."""
    pfad = f"/api/config/automation/config/{AUTOMATION_ID}"
    status, _ = ha.anfrage("GET", pfad)
    vorhanden = status == 200
    status, text = ha.anfrage("POST", pfad, json_daten=config)
    if status == 404:
        raise Abbruch(
            "Home Assistant hat keinen Automations-Editor (Integration „config“). "
            "Sie gehört zu default_config und muss in der configuration.yaml aktiv sein."
        )
    ha._json(status, text, "POST", pfad)
    for _ in range(20):
        zustand = automation_entitaet(ha)
        if zustand is not None:
            return ("aktualisiert" if vorhanden else "angelegt"), zustand
        time.sleep(0.5)
    raise Abbruch(
        "Die Automation wurde gespeichert, taucht aber nicht in Home Assistant auf. "
        "Steht in der configuration.yaml die Zeile „automation: !include automations.yaml“?"
    )


def probelauf(ha: HomeAssistant, automation: str, kaffeemaschine: str) -> str:
    """Löst die Automation ohne Bedingungen aus und wartet, bis die Maschine an ist."""
    if ha.get_json(f"/api/states/{kaffeemaschine}")["state"] == "on":
        return "Die Kaffeemaschine ist schon an – zum Ausprobieren bitte erst ausschalten."
    ha.post_json(
        "/api/services/automation/trigger",
        {"entity_id": automation, "skip_condition": True},
    )
    for _ in range(30):
        if ha.get_json(f"/api/states/{kaffeemaschine}")["state"] == "on":
            return "Die Kaffeemaschine ist angegangen."
        time.sleep(1)
    raise Abbruch(
        f"Die Automation wurde ausgelöst, aber {kaffeemaschine} meldet nach 30 s "
        "noch nicht „an“. Ist die Maschine im Standby mit dem WLAN verbunden und "
        "die Fernsteuerung in Home Connect erlaubt?"
    )


def argumente(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="installieren.py",
        description=(
            "Richtet die Automation „Kaffeemaschine an, wenn die Alarmanlage morgens "
            "unscharf geschaltet wird“ in Home Assistant ein."
        ),
        add_help=False,
    )
    parser.add_argument("-h", "--help", "--hilfe", action="help", help="Diese Hilfe anzeigen")
    parser.add_argument(
        "--url",
        default=os.environ.get("HA_URL", STANDARD_URL),
        help=f"Adresse von Home Assistant (Standard: {STANDARD_URL} bzw. HA_URL)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("HA_TOKEN"),
        help="Langlebiger Zugangstoken (oder HA_TOKEN); sonst Anmeldung mit Benutzername/Passwort",
    )
    parser.add_argument(
        "--benutzer",
        default=os.environ.get("HA_BENUTZER"),
        help=(
            "Benutzername in Home Assistant (oder HA_BENUTZER); das Passwort wird "
            "abgefragt oder aus HA_PASSWORT gelesen"
        ),
    )
    parser.add_argument(
        "--alarmanlage",
        action="append",
        metavar="ENTITY_ID",
        help=(
            "Entitäts-ID der Alarmanlage, falls sie nicht automatisch gefunden wird; "
            "mehrfach möglich, z. B. je eine Systemvariable für Hüllschutz und Vollschutz. "
            "Eine deaktivierte Entität wird dabei aktiviert"
        ),
    )
    parser.add_argument(
        "--unscharf",
        help=(
            "Zustand der Alarmanlage, der „unscharf“ bedeutet – bei einer Alarmzentrale "
            f"„{STANDARD_UNSCHARF}“ (Standard), bei einer Systemvariable z. B. „Unscharf“ "
            "oder „off“; ohne Angabe wird gefragt"
        ),
    )
    parser.add_argument("--kaffeemaschine", help="Entitäts-ID des Einschalters der Kaffeemaschine")
    parser.add_argument(
        "--von",
        type=uhrzeit,
        default=STANDARD_VON,
        help=f"Anfang des Zeitfensters (Standard {STANDARD_VON})",
    )
    parser.add_argument(
        "--bis",
        type=uhrzeit,
        default=STANDARD_BIS,
        help=f"Ende des Zeitfensters, ausschließlich (Standard {STANDARD_BIS})",
    )
    parser.add_argument(
        "--nur-anzeigen",
        action="store_true",
        help="Nur zeigen, was eingerichtet würde – nichts ändern",
    )
    parser.add_argument(
        "--probelauf",
        action="store_true",
        help="Nach dem Einrichten die Automation sofort auslösen – die Kaffeemaschine geht dann wirklich an",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = argumente(argv)
    interaktiv = sys.stdin.isatty()
    try:
        ha = HomeAssistant(url_bereinigen(args.url), args.token)
        refresh_token = None
        print(f"Verbinde mit {ha.url} …")
        if not ha.token:
            benutzer = args.benutzer
            passwort = os.environ.get("HA_PASSWORT")
            if not interaktiv and not (benutzer and passwort):
                raise Abbruch(
                    "Keine Zugangsdaten: bitte HA_TOKEN setzen oder HA_BENUTZER und "
                    "HA_PASSWORT – oder das Skript direkt im Terminal ausführen."
                )
            benutzer = benutzer or input("Benutzername in Home Assistant: ").strip()
            passwort = passwort or getpass.getpass("Passwort: ")
            refresh_token = anmelden(
                ha,
                benutzer,
                passwort,
                (lambda: input("Zwei-Faktor-Code: ").strip()) if interaktiv else None,
            )
        try:
            return _einrichten(ha, args, interaktiv)
        finally:
            if refresh_token:
                abmelden(ha, refresh_token)
    except Abbruch as fehler:
        print(f"\n✗ {fehler}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return 130


def _einrichten(ha: HomeAssistant, args: argparse.Namespace, interaktiv: bool) -> int:
    info = ha.get_json("/api/config")
    print(f"✓ Home Assistant {info.get('version')} („{info.get('location_name')}“)")

    zustaende = {z["entity_id"]: z for z in ha.get_json("/api/states")}
    if args.alarmanlage:
        alarmanlagen = [
            alarm_entitaet_bereitstellen(ha, anlage, zustaende) for anlage in args.alarmanlage
        ]
    else:
        kandidaten = kandidaten_alarmanlage(zustaende)
        hinweis = (
            "Ist die Alarmanlage in Home Assistant eingebunden – als Alarmzentrale "
            "(alarm_control_panel) oder z. B. als Systemvariable der CCU (Sensor, Auswahl, Schalter)?"
        )
        alarmanlagen: list[str] = []
        if not any(entity_id.startswith("alarm_control_panel.") for entity_id, _ in kandidaten):
            deaktiviert = deaktivierte_alarm_entitaeten(ha, zustaende)
            if deaktiviert:
                hinweis = hinweis_deaktiviert(deaktiviert)
                if interaktiv:
                    alarmanlagen = deaktivierte_anbieten(ha, deaktiviert, zustaende)
        if not alarmanlagen:
            alarmanlagen = auswaehlen(
                kandidaten,
                "Keine Alarmanlage",
                "die Alarmanlage",
                "--alarmanlage",
                interaktiv,
                hinweis,
                mehrere=True,
            )
    unscharf = unscharf_bestimmen(alarmanlagen, zustaende, args.unscharf, interaktiv)
    print(
        "✓ Alarmanlage: "
        + ", ".join(f"{anlage} („{name_von(zustaende[anlage])}“)" for anlage in alarmanlagen)
        + f", unscharf = „{unscharf}“"
    )

    if args.kaffeemaschine:
        kaffeemaschine = entitaet_pruefen(args.kaffeemaschine, ("switch",), zustaende, "--kaffeemaschine")
    else:
        [kaffeemaschine] = auswaehlen(
            kandidaten_kaffeemaschine(ha, zustaende),
            "Kein Einschalter der Kaffeemaschine",
            "den Einschalter der Kaffeemaschine",
            "--kaffeemaschine",
            interaktiv,
            "Ist die Maschine über Home Connect eingebunden?",
        )
    print(f"✓ Kaffeemaschine: {kaffeemaschine} („{name_von(zustaende[kaffeemaschine])}“)")

    config = automation_config(alarmanlagen, kaffeemaschine, args.von, args.bis, unscharf)
    if args.nur_anzeigen:
        print("\nDiese Automation würde eingerichtet (nichts geändert):")
        print(json.dumps(config, indent=2, ensure_ascii=False))
        return 0

    ergebnis, automation = installieren(ha, config)
    aktiv = automation["state"] == "on"
    print(
        f"✓ Automation {ergebnis}: „{ALIAS}“\n"
        f"  {automation['entity_id']}, {'aktiv' if aktiv else 'NICHT aktiv: ' + automation['state']}, "
        f"Zeitfenster {args.von[:5]}–{args.bis[:5]} Uhr"
    )
    if not aktiv:
        raise Abbruch("Die Automation ist nicht aktiv. Details zeigt ihre Ablaufverfolgung in Home Assistant.")

    if args.probelauf:
        print("Probelauf: Automation wird ausgelöst …")
        print(f"✓ {probelauf(ha, automation['entity_id'], kaffeemaschine)}")

    print(
        "\nFertig. Wird die Alarmanlage im Zeitfenster unscharf geschaltet, geht die "
        "Kaffeemaschine an.\nZum Ändern: Einstellungen → Automationen & Szenen → „"
        + ALIAS
        + "“."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
