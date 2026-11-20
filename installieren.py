#!/usr/bin/env python3
"""Richtet die Automation „Kaffeemaschine an, wenn die Alarmanlage morgens
unscharf geschaltet wird“ direkt in Home Assistant ein.

Das Skript braucht nur Python 3, keine Zusatzpakete. Es spricht mit Home
Assistant über dessen REST-API, sucht Alarmanlage und Kaffeemaschine
(Home Connect) heraus, legt die Automation an und prüft, dass sie aktiv ist.
Ein zweiter Aufruf aktualisiert die Automation, statt sie doppelt anzulegen,
und übernimmt Alarmanlage, Kaffeemaschine und Zeitfenster aus der vorhandenen
Automation, soweit nichts anderes angegeben ist.

Die Alarmanlage kann eine Alarmzentrale (alarm_control_panel) sein oder eine
andere Entität, die ihren Zustand meldet – z. B. eine Systemvariable der
Homematic-CCU (OpenCCU). Dann fragt das Skript, welcher Zustand „unscharf“
bedeutet (oder nimmt --unscharf). Ist die Entität in Home Assistant noch
deaktiviert, wie Homematic(IP) Local Systemvariablen anlegt, aktiviert das
Skript sie auf Wunsch selbst.

Mit --licht <entity_id> legt es zusätzlich eine zweite Automation an: Ein Licht
oder eine Steckdose an der Maschine geht mit der Kaffeemaschine an und aus –
egal ob die Automation, Home Assistant oder jemand am Gerät sie einschaltet.
Mit --dunkel geht das Licht nur an, wenn ein Helligkeitssensor – z. B. der des
Bewegungsmelders in der Küche – Dunkelheit meldet (Standard: höchstens 20 Lux),
und aus, sobald es hell wird.

Mit --getraenke --personen Jens,Nicole richtet es statt der Automation ein
Dashboard für die Kaffeemaschine ein: Getränk wählen und anpassen (Stärke,
Menge, Temperatur, Milchanteil, Intensität, doppelt), Lieblingsgetränke je
Person speichern und per Tipp zubereiten, dazu alles, was die Maschine sonst
bedienen lässt (Ein/Aus, Kindersicherung, Programm, Abbrechen, Fortschritt,
Wartungshinweise, Zähler). Dafür legt es Helfer, drei Skripte und das
Dashboard „Konrad“ in der Seitenleiste an.

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
import datetime
import getpass
import hashlib
import json
import math
import os
import re
import socket
import ssl
import struct
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator, Optional

AUTOMATION_ID = "siemens_kaffee_bei_unscharf"
ALIAS = "Kaffeemaschine an, wenn die Alarmanlage morgens unscharf geschaltet wird"
# Die zweite Automation für --licht: Licht an der Maschine folgt der Kaffeemaschine.
LICHT_AUTOMATION_ID = "siemens_kaffee_licht"
LICHT_ALIAS = "Licht an der Kaffeemaschine folgt der Kaffeemaschine"
LICHT_DOMAINS = ("light", "switch")
LICHT_AUTO = "auto"  # --licht ohne Wert: Licht suchen, neu angelegte Geräte zuerst
NEU_TAGE = 7  # so lange gilt ein Gerät in der Lichtsuche als „neu angelegt“
LICHT_WOERTER = re.compile(
    r"led|licht|light|lampe|leuchte|zwischenschalter|steckdose|plug|strip", re.IGNORECASE
)
LICHT_HINWEIS = (
    "Gesucht wird ein Licht oder Schalter mit „Kaffee“ im Namen, der nicht zur Kaffeemaschine "
    "selbst gehört; neu angelegte Geräte stehen oben. Sonst eines mit LED, Licht oder Steckdose im Namen."
)
# Mit --dunkel geht das Licht nur, wenn ein Helligkeitssensor (z. B. der des
# Bewegungsmelders in der Küche) höchstens STANDARD_DUNKEL_UNTER Lux meldet.
STANDARD_DUNKEL_UNTER = 20
DUNKEL_DOMAINS = ("sensor",)
DUNKEL_AUTO = "auto"  # --dunkel ohne Wert: Sensor suchen
DUNKEL_AUS = "aus"  # --dunkel aus: Bedingung wieder entfernen
LUX_EINHEITEN = ("lx", "lux")
KUECHE = re.compile(r"k(?:ü|ue|u)che|kitchen", re.IGNORECASE)  # „kuche“: so schreibt HA Küche in IDs
# Homematic-Bewegungsmelder liefern neben der Helligkeit auch Mittel-/Minimal-/Maximalwerte.
NEBENWERTE = re.compile(r"average|lowest|highest|current|mittel|minim|maxim", re.IGNORECASE)
# Woran man einen Helligkeitssensor am Namen erkennt – nötig für deaktivierte
# Entitäten (das Register kennt weder Geräteklasse noch Einheit) und für ältere
# Homematic-Melder, deren Helligkeit (0–255) keine Einheit hat.
HELLIGKEIT_WOERTER = re.compile(
    r"beleuchtungsst|illuminat|helligkeit|brightness|light_?level|(?<![a-z])lux(?![a-z])", re.IGNORECASE
)
HELLIGKEIT_HINWEIS = (
    "Gesucht wird ein Sensor mit Geräteklasse „illuminance“ oder Einheit lx – z. B. die Helligkeit "
    "des Bewegungsmelders in der Küche (Homematic IP: „Beleuchtungsstärke“) – oder ein Sensor, der "
    "„Helligkeit“, „Beleuchtungsstärke“ oder „Illumination“ heißt und eine Zahl meldet."
)
HOME_CONNECT_ANLEITUNG = "https://github.com/JensReinke/siemens-Kaffee#home-connect-einrichten"
STANDARD_URL = "http://homeassistant.local:8123"
STANDARD_VON = "05:00"
STANDARD_BIS = "09:00"
ZEITLIMIT = 30  # Sekunden pro Anfrage
AUSSCHALT_SEKUNDEN = 30  # so lange wartet der Probelauf, bis die Maschine wieder aus ist
VERALTET_MINUTEN = 60  # ab diesem Alter gilt ein Helligkeitswert bei --pruefen als alt
LAEUFE = 5  # so viele Läufe je Automation zeigt --pruefen (mehr merkt sich Home Assistant nicht)
PROBELAUF_SCHON_AN = "Die Kaffeemaschine ist schon an – zum Ausprobieren bitte erst ausschalten."
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


# Woran man den „Einschalter“ (englisch „Power“) eines Home-Connect-Geräts
# und eine Kaffeemaschine erkennt – an Entitäts-IDs, Namen und Modell.
EINSCHALTER = re.compile(r"(^|[_ ])(power|power_?state|einschalter)$", re.IGNORECASE)
# Die Wörter müssen am Wortanfang stehen (in Entitäts-IDs trennen Unterstriche),
# sonst passt „Festplatte“ zu „latte“ oder die Seriennummer „UEQ19…“ zur EQ-Serie.
KAFFEEMASCHINE = re.compile(
    r"(?<![a-z])(kaffee|coffee|espresso|cappuccino|latte(?![a-z])|beans?(?![a-z])|bohne"
    r"|milk(?![a-z])|milch|drip_tray|tropfschale|hot_water|heisswasser|heißwasser|vollautomat"
    r"|eq[._ ]?\d)",
    re.IGNORECASE,
)

# Integrationen, über die Hausgeräte von Siemens/Bosch in Home Assistant kommen.
HOME_CONNECT_DOMAINS = ("home_connect", "home_connect_alt")
# Ab so vielen Entitäten ist ein Gerät eine Zentrale (CCU, Bridge, Hub), keine Kaffeemaschine.
GROSSES_GERAET = 40
# Mehr Schalter ohne „Einschalter“ im Namen bietet das Skript nicht blind an …
MAX_SCHALTER = 8
# … und mehr deaktivierte Entitäten nicht zum Aktivieren.
MAX_ANGEBOT = 40
HAUSGERAETE = re.compile(r"siemens|bosch|bsh|neff|gaggenau|home ?connect", re.IGNORECASE)
EINTRAGSZUSTAENDE = {
    "loaded": "geladen",
    "not_loaded": "nicht geladen",
    "setup_error": "Fehler beim Einrichten",
    "setup_retry": "Einrichten fehlgeschlagen, wird wiederholt",
    "setup_in_progress": "wird gerade eingerichtet",
    "migration_error": "Fehler bei der Migration",
    "failed_unload": "Entladen fehlgeschlagen",
}


class Abbruch(Exception):
    """Ein Fehler, der dem Benutzer erklärt wird – ohne Python-Fehlermeldung."""


class WebSocketFehler(Abbruch):
    """Eine Fehlermeldung der WebSocket-API, samt ihrem Code (z. B. config_not_found)."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code = code


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


def licht_config(
    kaffeemaschine: str,
    licht: str,
    dunkel: Optional[str] = None,
    dunkel_unter: "int | float" = STANDARD_DUNKEL_UNTER,
) -> dict[str, Any]:
    """Dieselbe Automation wie beispiele/licht_folgt_kaffeemaschine.yaml – mit
    ``dunkel`` wie beispiele/licht_folgt_kaffeemaschine_wenn_dunkel.yaml.

    Das Licht (oder die Steckdose) an der Maschine geht mit dem Einschalter der
    Kaffeemaschine an und aus – auch wenn jemand die Maschine am Gerät oder in
    Home Assistant schaltet. ``homeassistant.turn_on`` schaltet Lichter und
    Schalter gleichermaßen.

    Mit ``dunkel`` (ein Helligkeitssensor) geht das Licht nur an, wenn der Sensor
    höchstens ``dunkel_unter`` Lux meldet, und aus, sobald es heller wird. Ohne
    brauchbaren Wert (unknown, unavailable, keine Zahl, Sensor gelöscht) gilt es
    als dunkel: ``numeric_state`` ist dann falsch oder löst einen Fehler aus, und
    ``choose`` übergeht die Option – übrig bleibt „Maschine an → Licht an“.
    """
    an = {"action": "homeassistant.turn_on", "target": {"entity_id": licht}}
    aus = {"action": "homeassistant.turn_off", "target": {"entity_id": licht}}
    if dunkel is None:
        return {
            "alias": LICHT_ALIAS,
            "description": "Schaltet das Licht an der Kaffeemaschine zusammen mit der Maschine ein und aus.",
            "mode": "restart",
            "triggers": [
                {"trigger": "state", "entity_id": kaffeemaschine, "to": "on", "id": "an"},
                {"trigger": "state", "entity_id": kaffeemaschine, "to": "off", "id": "aus"},
            ],
            "actions": [{"if": [{"condition": "trigger", "id": "an"}], "then": [an], "else": [aus]}],
        }
    maschine_an = {"condition": "state", "entity_id": kaffeemaschine, "state": "on"}
    return {
        "alias": LICHT_ALIAS,
        "description": (
            "Schaltet das Licht an der Kaffeemaschine mit der Maschine aus – und ein, wenn es dabei "
            f"dunkel ist ({dunkel} höchstens {dunkel_unter:g} lx)."
        ),
        "mode": "restart",
        "triggers": [
            {"trigger": "state", "entity_id": kaffeemaschine, "to": "on", "id": "an"},
            {"trigger": "state", "entity_id": kaffeemaschine, "to": "off", "id": "aus"},
            {"trigger": "numeric_state", "entity_id": dunkel, "above": dunkel_unter, "id": "hell"},
            {"trigger": "numeric_state", "entity_id": dunkel, "below": dunkel_unter, "id": "dunkel"},
        ],
        "actions": [
            {
                "choose": [
                    # Maschine aus oder hell geworden: Licht aus.
                    {"conditions": [{"condition": "trigger", "id": ["aus", "hell"]}], "sequence": [aus]},
                    # Maschine an, aber zu hell: Licht bleibt aus (steht so in der Ablaufverfolgung).
                    {
                        "conditions": [
                            maschine_an,
                            {"condition": "numeric_state", "entity_id": dunkel, "above": dunkel_unter},
                        ],
                        "sequence": [{"stop": "Es ist hell – das Licht bleibt aus."}],
                    },
                    # Maschine an und dunkel (oder kein Messwert): Licht an.
                    {"conditions": [maschine_an], "sequence": [an]},
                ]
            }
        ],
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


def lux(text: str) -> "int | float":
    """„20“, „20,5“ oder „20 lx“ → 20 bzw. 20.5 – die Schwelle für --dunkel-unter."""
    roh = text.strip().lower()
    for einheit in LUX_EINHEITEN:
        roh = roh.removesuffix(einheit).strip()
    try:
        wert = float(roh.replace(",", "."))
    except ValueError:
        raise argparse.ArgumentTypeError(f"„{text}“ ist kein Helligkeitswert in Lux wie 20") from None
    if not (0 <= wert < float("inf")):  # auch NaN fällt hier durch
        raise argparse.ArgumentTypeError(f"„{text}“ ist kein Helligkeitswert in Lux wie 20")
    return int(wert) if wert.is_integer() else wert


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
        self._register: Optional[Register] = None

    def register(self) -> "Register":
        """Die Register von Home Assistant – werden beim ersten Zugriff geholt."""
        if self._register is None:
            self._register = Register(self)
        return self._register

    def register_aktualisieren(self) -> "Register":
        """Holt die Register neu – nach dem Aktivieren oder Anlegen von Entitäten."""
        self._register = Register(self)
        return self._register

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


def ist_einschalter(entity_id: str, name: str = "") -> bool:
    objekt_id = entity_id.split(".", 1)[1]
    return bool(EINSCHALTER.search(objekt_id) or EINSCHALTER.search(name))


def kandidaten_kaffeemaschine(
    register: "Register", zustaende: dict[str, dict[str, Any]]
) -> tuple[list[tuple[str, str]], list[str], str]:
    """Mögliche Einschalter der Kaffeemaschine.

    Gibt zurück: die auswählbaren Einschalter als (entity_id, Beschreibung),
    deaktivierte Einschalter und einen Hinweis für den Fall, dass nichts
    auswählbar ist.
    """
    geraete = [g for g in register.geraete.values() if register.ist_kaffeemaschine(g)]
    if not geraete:
        # Keine Kaffeemaschine erkannt – dann alle Home-Connect-Geräte anbieten.
        geraete = [g for g in register.geraete.values() if register.ist_home_connect(g)]
    if not geraete:
        # Gar kein Hausgerät: Nach Schaltern suchen, die wie der Einschalter
        # einer Kaffeemaschine heißen.
        kandidaten = [
            (entity_id, f"„{name_von(zustand)}“")
            for entity_id, zustand in sorted(zustaende.items())
            if entity_id.startswith("switch.")
            and ist_einschalter(entity_id, name_von(zustand))
            and (KAFFEEMASCHINE.search(entity_id) or KAFFEEMASCHINE.search(name_von(zustand)))
        ]
        return kandidaten, [], diagnose(register, zustaende)

    kandidaten: list[tuple[str, str]] = []
    echte_einschalter: list[tuple[str, str]] = []
    deaktiviert: list[str] = []
    for geraet in geraete:
        hersteller_modell = " ".join(
            str(w) for w in (geraet.get("manufacturer"), geraet.get("model")) if w
        ) or register.geraetename(geraet)
        schalter = [e for e in register.entitaeten_von(geraet) if e["entity_id"].startswith("switch.")]
        einschalter = [
            e for e in schalter if ist_einschalter(e["entity_id"], register.entitaetsname(e))
        ]
        echt = bool(einschalter)
        # Heißt kein Schalter „Einschalter“, kommen die wenigen Schalter des Geräts infrage.
        if not einschalter and len(schalter) <= MAX_SCHALTER:
            einschalter = schalter
        for entitaet in einschalter:
            entity_id = entitaet["entity_id"]
            if verfuegbar(entity_id, zustaende):
                kandidat = (entity_id, f"„{name_von(zustaende[entity_id])}“, {hersteller_modell}")
                kandidaten.append(kandidat)
                if echt:
                    echte_einschalter.append(kandidat)
            elif entitaet.get("disabled_by"):
                deaktiviert.append(entity_id)
            # Sonst: aktiv, aber nicht verfügbar (Integration nicht geladen, Gerät
            # offline) – steht in der Diagnose.
    # Gibt es einen echten „Einschalter“, fallen bloße Schalter anderer Geräte weg –
    # sonst stünde die Steckdose „LED Kaffee“ als zweite Kaffeemaschine neben dem Vollautomaten.
    if echte_einschalter:
        kandidaten = echte_einschalter
    return kandidaten, sorted(deaktiviert), diagnose(register, zustaende)


def licht_beschreibung(
    zustand: dict[str, Any], entitaet: Optional[dict[str, Any]], geraet: Optional[dict[str, Any]]
) -> str:
    """„Zwischenschalter Kaffee“, eQ-3 HmIP-FSM, aus, angelegt 14.11.2026"""
    teile = [f"„{name_von(zustand)}“"]
    if geraet:
        hersteller_modell = " ".join(str(w) for w in (geraet.get("manufacturer"), geraet.get("model")) if w)
        if hersteller_modell:
            teile.append(hersteller_modell)
    state = str(zustand.get("state"))
    teile.append({"on": "an", "off": "aus", "unavailable": "nicht verfügbar"}.get(state, state))
    if entitaet and entitaet.get("created_at"):
        angelegt = datetime.datetime.fromtimestamp(float(entitaet["created_at"]))
        neu = datetime.datetime.now() - angelegt <= datetime.timedelta(days=NEU_TAGE)
        teile.append(f"{'neu angelegt am' if neu else 'angelegt'} {angelegt:%d.%m.%Y}")
    return ", ".join(teile)


def kandidaten_licht(
    register: "Register", zustaende: dict[str, dict[str, Any]], kaffeemaschine: str = ""
) -> list[tuple[str, str]]:
    """Mögliche Lichter an der Maschine für --licht ohne Wert.

    Lichter und Schalter, die nicht zur Kaffeemaschine (Home Connect) und nicht
    zu einer Zentrale gehören. Zuerst alles mit Kaffee-Bezug, sonst alles, was
    nach Licht klingt, sonst alle – verfügbare zuerst, neu angelegte oben.
    """
    treffer = []
    for entity_id, zustand in zustaende.items():
        if entity_id.split(".", 1)[0] not in LICHT_DOMAINS or entity_id == kaffeemaschine:
            continue
        entitaet = register.entitaeten.get(entity_id)
        geraet = register.geraete.get((entitaet or {}).get("device_id") or "")
        if geraet and (register.ist_home_connect(geraet) or register.ist_gross(geraet)):
            continue
        text = f"{entity_id} {name_von(zustand)}"
        stufe = 0 if KAFFEEMASCHINE.search(text) else 1 if LICHT_WOERTER.search(text) else 2
        treffer.append((stufe, entity_id, zustand, entitaet, geraet))
    if not treffer:
        return []
    beste = min(stufe for stufe, *_ in treffer)
    auswahl = sorted(
        (t for t in treffer if t[0] == beste),
        key=lambda t: (
            not verfuegbar(t[1], zustaende),
            -float((t[3] or {}).get("created_at") or 0),
            t[1],
        ),
    )
    return [(entity_id, licht_beschreibung(zustand, entitaet, geraet)) for _, entity_id, zustand, entitaet, geraet in auswahl]


def licht_suchen(
    register: "Register",
    zustaende: dict[str, dict[str, Any]],
    kaffeemaschine: str,
    interaktiv: bool,
    hinweis: str = LICHT_HINWEIS,
) -> tuple[str, str]:
    """Sucht das Licht an der Maschine – automatisch, per Rückfrage oder mit klarer Meldung.

    Gibt die Entitäts-ID und ihre Beschreibung (Name, Modell, Zustand, Anlegedatum) zurück.
    """
    kandidaten = kandidaten_licht(register, zustaende, kaffeemaschine)
    if len(kandidaten) > MAX_ANGEBOT:
        raise Abbruch(
            f"{len(kandidaten)} Lichter und Schalter kämen infrage – bitte mit --licht <entity_id> angeben, "
            "welches an der Kaffeemaschine hängt. Tipp: Dem Gerät in der Zentrale „Kaffee“ in den Namen "
            "schreiben, dann findet das Skript es von selbst."
        )
    [licht] = auswaehlen(
        kandidaten, "Kein Licht an der Maschine", "das Licht an der Maschine", "--licht", interaktiv, hinweis
    )
    return licht, dict(kandidaten)[licht]


def verfuegbar(entity_id: str, zustaende: dict[str, dict[str, Any]]) -> bool:
    """Hat die Entität einen Zustand, und ist er nicht „unavailable“?"""
    return zustaende.get(entity_id, {}).get("state", "unavailable") != "unavailable"


def diagnose(register: "Register", zustaende: dict[str, dict[str, Any]]) -> str:
    """Was Home Assistant über Home Connect und Hausgeräte weiß – für die Fehlersuche."""
    zeilen = []
    eintraege = [e for e in register.eintraege.values() if e.get("domain") in HOME_CONNECT_DOMAINS]
    if not eintraege:
        zeilen.append(
            "Die Integration „Home Connect“ ist in Home Assistant nicht eingerichtet – darüber "
            "kommt eine Siemens-Kaffeemaschine normalerweise nach Home Assistant. Anleitung: "
            + HOME_CONNECT_ANLEITUNG
        )
    for eintrag in eintraege:
        zustand = str(eintrag.get("state"))
        zeile = (
            f"Integration „{eintrag.get('title')}“ ({eintrag.get('domain')}): "
            f"{'deaktiviert' if eintrag.get('disabled_by') else EINTRAGSZUSTAENDE.get(zustand, zustand)}"
        )
        if eintrag.get("reason"):
            zeile += f" – {eintrag['reason']}"
        if zustand != "loaded":
            zeile += (
                ". → In Home Assistant unter Einstellungen → Geräte & Dienste die Integration "
                "neu laden bzw. der Aufforderung zur erneuten Anmeldung folgen."
            )
        zeilen.append(zeile)
    for geraet in register.geraete.values():
        if not (
            register.ist_kaffeemaschine(geraet)
            or HAUSGERAETE.search(f"{geraet.get('manufacturer')} {geraet.get('model')}")
        ):
            continue
        entitaeten = register.entitaeten_von(geraet)
        deaktiviert = [e for e in entitaeten if e.get("disabled_by")]
        nicht_verfuegbar = [
            e for e in entitaeten
            if not e.get("disabled_by") and not verfuegbar(e["entity_id"], zustaende)
        ]
        # Bei einer Zentrale nur die Schalter mit Kaffee-Bezug, sonst alle.
        gezeigt = register.mit_kaffeebezug(geraet) if register.ist_gross(geraet) else entitaeten
        schalter = [
            e["entity_id"]
            + (" (deaktiviert)" if e in deaktiviert else " (nicht verfügbar)" if e in nicht_verfuegbar else "")
            for e in gezeigt
            if e["entity_id"].startswith("switch.")
        ]
        integrationen = ", ".join(sorted({str(e.get("domain")) for e in register.integrationen_von(geraet)}))
        zeilen.append(
            f"Gerät „{register.geraetename(geraet)}“ ({geraet.get('manufacturer')} {geraet.get('model')}, "
            f"Integration: {integrationen or '?'}): {len(entitaeten)} Entitäten, "
            f"{len(deaktiviert)} deaktiviert, {len(nicht_verfuegbar)} nicht verfügbar; "
            f"Schalter{' mit Kaffee-Bezug' if register.ist_gross(geraet) else ''}: {', '.join(schalter) or 'keine'}"
        )
    # Egal an welchem Gerät: aktive Schalter, die nach Kaffee klingen – z. B. eine
    # Steckdose, über die die Maschine schon geschaltet wird.
    kaffee_schalter = [
        f"{entity_id} („{name_von(zustand)}“, {zustand['state']})"
        for entity_id, zustand in sorted(zustaende.items())
        if entity_id.startswith("switch.")
        and (KAFFEEMASCHINE.search(entity_id) or KAFFEEMASCHINE.search(name_von(zustand)))
    ]
    zeilen.append("Aktive Schalter mit Kaffee-Bezug: " + (", ".join(kaffee_schalter) or "keine"))
    return "\n  ".join(zeilen)


def diagnose_ausfuehrlich(register: "Register", zustaende: dict[str, dict[str, Any]]) -> str:
    """Die Diagnose plus alle Entitäten der Hausgeräte – zum Weitergeben bei Problemen."""
    zeilen = ["Diagnose:", "  " + diagnose(register, zustaende)]
    for geraet in register.geraete.values():
        if not (
            register.ist_kaffeemaschine(geraet)
            or HAUSGERAETE.search(f"{geraet.get('manufacturer')} {geraet.get('model')}")
        ):
            continue
        gross = register.ist_gross(geraet)
        zeilen.append(
            f"\nEntitäten von „{register.geraetename(geraet)}“"
            + (" (nur die mit Kaffee-Bezug):" if gross else ":")
        )
        for entitaet in register.mit_kaffeebezug(geraet) if gross else register.entitaeten_von(geraet):
            entity_id = entitaet["entity_id"]
            if entitaet.get("disabled_by"):
                status = f"deaktiviert ({entitaet['disabled_by']})"
            elif entity_id in zustaende:
                status = f"Zustand: {zustaende[entity_id]['state']}"
            else:
                status = "nicht verfügbar (kein Zustand)"
            zeilen.append(
                f"  {entity_id}  „{register.entitaetsname(entitaet)}“  [{entitaet.get('platform')}]  {status}"
            )
    return "\n".join(zeilen)


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


def ist_lux_sensor(entity_id: str, zustand: dict[str, Any]) -> bool:
    """Ein Sensor mit Geräteklasse „illuminance“ oder Einheit Lux."""
    attribute = zustand.get("attributes", {})
    return entity_id.startswith("sensor.") and (
        attribute.get("device_class") == "illuminance"
        or str(attribute.get("unit_of_measurement", "")).lower() in LUX_EINHEITEN
    )


def ist_helligkeitssensor(entity_id: str, zustand: dict[str, Any]) -> bool:
    """Ein Lux-Sensor – oder ein Sensor, der nach Helligkeit heißt und eine Zahl meldet
    (ältere Homematic-Melder melden ihre Helligkeit 0–255 ohne Einheit)."""
    return ist_lux_sensor(entity_id, zustand) or (
        entity_id.startswith("sensor.")
        and bool(HELLIGKEIT_WOERTER.search(f"{entity_id} {name_von(zustand)}"))
        and helligkeit(zustand) is not None
    )


def einheit_von(zustand: dict[str, Any]) -> str:
    return str(zustand.get("attributes", {}).get("unit_of_measurement") or "")


def mit_einheit(wert: "int | float", zustand: dict[str, Any]) -> str:
    """„20 lx“ – oder nur „20“, wenn der Sensor keine Einheit hat."""
    return f"{wert:g} {einheit_von(zustand)}".strip()


def helligkeit(zustand: dict[str, Any]) -> Optional[float]:
    """Der Messwert als Zahl – oder None, wenn der Sensor gerade keinen liefert."""
    try:
        wert = float(zustand["state"])
    except (KeyError, TypeError, ValueError):
        return None
    return wert if wert == wert else None  # NaN


def helligkeit_text(zustand: dict[str, Any]) -> str:
    wert = helligkeit(zustand)
    if wert is None:
        return "zurzeit kein Wert"
    return mit_einheit(wert, zustand)


def ist_kueche(entity_id: str, name: str = "") -> bool:
    return bool(KUECHE.search(f"{entity_id} {name}"))


def kandidaten_helligkeit(zustaende: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """Helligkeitssensoren für --dunkel: die aus der Küche zuerst, Nebenwerte zuletzt."""
    treffer = [
        (entity_id, zustand)
        for entity_id, zustand in zustaende.items()
        if ist_helligkeitssensor(entity_id, zustand)
    ]
    treffer.sort(
        key=lambda paar: (
            not ist_kueche(paar[0], name_von(paar[1])),
            bool(NEBENWERTE.search(f"{paar[0]} {name_von(paar[1])}")),
            paar[0],
        )
    )
    return [
        (entity_id, f"„{name_von(zustand)}“, aktuell {helligkeit_text(zustand)}")
        for entity_id, zustand in treffer
    ]


def deaktivierte_helligkeitssensoren(register: "Register") -> list[str]:
    """Deaktivierte Sensoren, die nach Helligkeit heißen – Küche zuerst, Nebenwerte zuletzt.

    Homematic(IP) Local legt die Helligkeit eines Bewegungsmelders oft so an.
    Das Register kennt weder Geräteklasse noch Einheit, also zählt nur der Name.
    """
    treffer = [
        (entitaet["entity_id"], register.entitaetsname(entitaet))
        for entitaet in register.entitaeten.values()
        if entitaet.get("disabled_by")
        and entitaet["entity_id"].startswith("sensor.")
        and HELLIGKEIT_WOERTER.search(f"{entitaet['entity_id']} {register.entitaetsname(entitaet)}")
    ]
    treffer.sort(
        key=lambda paar: (
            not ist_kueche(*paar),
            bool(NEBENWERTE.search(f"{paar[0]} {paar[1]}")),
            paar[0],
        )
    )
    return [entity_id for entity_id, _ in treffer]


def deaktivierte_alarm_entitaeten(register: "Register") -> list[str]:
    """Deaktivierte Entitäten, die nach Alarmanlage klingen.

    Homematic(IP) Local legt Systemvariablen der CCU standardmäßig so an.
    Auswählen kann man sie erst, wenn sie aktiv sind.
    """
    return sorted(
        entitaet["entity_id"]
        for entitaet in register.entitaeten.values()
        if entitaet.get("disabled_by")
        and alarm_treffer(entitaet["entity_id"], register.entitaetsname(entitaet))
    )


def hinweis_deaktiviert(entity_ids: list[str], option: str = "--alarmanlage") -> str:
    liste = "\n".join(f"    {entity_id}" for entity_id in entity_ids[:15])
    if len(entity_ids) > 15:
        liste += f"\n    … und {len(entity_ids) - 15} weitere"
    return (
        "Diese Entitäten gibt es in Home Assistant, sie sind aber deaktiviert und "
        f"deshalb nicht auswählbar:\n{liste}\n"
        f"  Mit {option} <entity_id> angegeben, aktiviert dieses Skript sie selbst. "
        "Von Hand: Einstellungen → Geräte & Dienste → Entitäten → Filter "
        "„Deaktivierte Entitäten anzeigen“ → Entität öffnen → Zahnrad → „Aktiviert“ "
        "einschalten. Bei Homematic(IP) Local geht es auch mit „hahm“ in der "
        "Beschreibung der Systemvariable in der CCU."
    )


class WebSocket:
    """Ein kleiner WebSocket-Client (RFC 6455) für die WebSocket-API von Home Assistant.

    Nur für das nötig, was die REST-API nicht kann: Entitäten aktivieren, die Register
    lesen, Helfer und Dashboards anlegen.
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
                    raise WebSocketFehler(
                        str(fehler.get("code", "")), f"Home Assistant meldet: {fehler.get('message', fehler)}"
                    )
                return antwort.get("result")

    def schliessen(self) -> None:
        try:
            self._rahmen_senden(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        finally:
            self.sock.close()


class Register:
    """Integrationen, Geräte und Entitäten aus den Registern von Home Assistant.

    Kommt über die WebSocket-API, weil die REST-API weder deaktivierte Entitäten
    noch Geräte noch den Zustand der Integrationen kennt.
    """

    def __init__(self, ha: HomeAssistant) -> None:
        ws = WebSocket(ha.url, ha.token)
        try:
            self.eintraege: dict[str, dict[str, Any]] = {
                e["entry_id"]: e for e in ws.befehl(type="config_entries/get")
            }
            self.geraete: dict[str, dict[str, Any]] = {
                g["id"]: g for g in ws.befehl(type="config/device_registry/list")
            }
            self.entitaeten: dict[str, dict[str, Any]] = {
                e["entity_id"]: e for e in ws.befehl(type="config/entity_registry/list")
            }
        finally:
            ws.schliessen()
        self._nach_geraet: dict[str, list[dict[str, Any]]] = {}
        for entitaet in sorted(self.entitaeten.values(), key=lambda e: e["entity_id"]):
            if entitaet.get("device_id"):
                self._nach_geraet.setdefault(entitaet["device_id"], []).append(entitaet)

    @staticmethod
    def geraetename(geraet: dict[str, Any]) -> str:
        return str(geraet.get("name_by_user") or geraet.get("name") or geraet["id"])

    @staticmethod
    def entitaetsname(entitaet: dict[str, Any]) -> str:
        return str(entitaet.get("name") or entitaet.get("original_name") or "")

    def entitaeten_von(self, geraet: dict[str, Any]) -> list[dict[str, Any]]:
        return self._nach_geraet.get(geraet["id"], [])

    def integrationen_von(self, geraet: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            self.eintraege[eintrag_id]
            for eintrag_id in geraet.get("config_entries") or []
            if eintrag_id in self.eintraege
        ]

    def ist_home_connect(self, geraet: dict[str, Any]) -> bool:
        return any(e.get("domain") in HOME_CONNECT_DOMAINS for e in self.integrationen_von(geraet))

    def ist_gross(self, geraet: dict[str, Any]) -> bool:
        """Eine Zentrale wie die CCU mit hunderten Programmen und Variablen.

        Hausgeräte von Home Connect sind nie eine Zentrale, auch wenn ein
        Kaffeevollautomat viele Entitäten hat.
        """
        return not self.ist_home_connect(geraet) and len(self.entitaeten_von(geraet)) > GROSSES_GERAET

    def ist_kaffeemaschine(self, geraet: dict[str, Any]) -> bool:
        """Erkennt eine Kaffeemaschine an Name, Modell oder ihren Entitäten (Bohnen, Kaffeezähler …).

        Bei einer Zentrale zählen nur Name und Modell – sonst würde ein einzelnes
        Programm namens „Kaffee“ die ganze CCU zur Kaffeemaschine machen.
        """
        if KAFFEEMASCHINE.search(
            f"{geraet.get('name') or ''} {self.geraetename(geraet)} {geraet.get('model') or ''}"
        ):
            return True
        if self.ist_gross(geraet):
            return False
        return any(
            KAFFEEMASCHINE.search(f"{e['entity_id']} {self.entitaetsname(e)}")
            for e in self.entitaeten_von(geraet)
        )

    def mit_kaffeebezug(self, geraet: dict[str, Any]) -> list[dict[str, Any]]:
        """Die Entitäten eines Geräts, die nach Kaffee oder Einschalter klingen."""
        return [
            e
            for e in self.entitaeten_von(geraet)
            if KAFFEEMASCHINE.search(f"{e['entity_id']} {self.entitaetsname(e)}")
            or (e["entity_id"].startswith("switch.") and ist_einschalter(e["entity_id"], self.entitaetsname(e)))
        ]


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


def entitaet_bereitstellen(
    ha: HomeAssistant,
    entity_id: str,
    zustaende: dict[str, dict[str, Any]],
    domains: tuple[str, ...],
    option: str,
) -> str:
    """Prüft eine per Option angegebene Entität; ist sie nur deaktiviert, wird sie aktiviert."""
    domain_pruefen(entity_id, domains, option)
    if entity_id not in zustaende:
        eintrag = ha.register().entitaeten.get(entity_id)
        if eintrag and eintrag.get("disabled_by"):
            print(f"  {entity_id} ist deaktiviert – wird aktiviert.")
            aktivieren_und_warten(ha, [entity_id], zustaende)
        elif eintrag:
            raise Abbruch(
                f"{entity_id} ist in Home Assistant registriert, meldet aber keinen Zustand – "
                f"die Integration „{eintrag.get('platform')}“ ist wohl gerade nicht geladen. "
                "Unter Einstellungen → Geräte & Dienste nachsehen."
            )
    return entitaet_pruefen(entity_id, domains, zustaende, option)


def deaktivierte_anbieten(
    ha: HomeAssistant,
    deaktiviert: list[str],
    zustaende: dict[str, dict[str, Any]],
    verwendung: str = "als Alarmanlage",
    mehrere: bool = True,
) -> list[str]:
    """Bietet im Terminal an, deaktivierte Entitäten zu aktivieren und zu verwenden."""
    gewaehlt = menue(
        "Diese Entitäten gibt es in Home Assistant, sie sind aber deaktiviert. Soll ich eine "
        f"davon aktivieren und {verwendung} verwenden?"
        + (" (mehrere möglich)" if mehrere else ""),
        deaktiviert,
        mehrere=mehrere,
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


def automation_entitaet(
    ha: HomeAssistant, automation_id: str = AUTOMATION_ID
) -> Optional[dict[str, Any]]:
    """Die Automation mit dieser ID – oder None, wenn sie (noch) nicht geladen ist."""
    for zustand in ha.get_json("/api/states"):
        if zustand["entity_id"].startswith("automation.") and (
            zustand.get("attributes", {}).get("id") == automation_id
        ):
            return zustand
    return None


def vorhandene_automation(
    ha: HomeAssistant, automation_id: str = AUTOMATION_ID
) -> Optional[dict[str, Any]]:
    """Die gespeicherte Konfiguration einer unserer Automationen – oder None, wenn es sie noch nicht gibt."""
    status, text = ha.anfrage("GET", f"/api/config/automation/config/{automation_id}")
    if status != 200:
        return None
    try:
        config = json.loads(text)
    except ValueError:
        return None
    return config if isinstance(config, dict) else None


def uebernehmen(config: dict[str, Any]) -> dict[str, Any]:
    """Liest Alarmanlage, Zustand „unscharf“, Kaffeemaschine und Zeitfenster aus einer
    gespeicherten Automation – nachsichtig, falls sie in Home Assistant bearbeitet wurde."""
    werte: dict[str, Any] = {}
    triggers = config.get("triggers") or []
    if triggers and isinstance(triggers[0], dict):
        anlagen = triggers[0].get("entity_id")
        if isinstance(anlagen, str):
            werte["alarmanlagen"] = [anlagen]
        elif isinstance(anlagen, list) and anlagen and all(isinstance(a, str) for a in anlagen):
            werte["alarmanlagen"] = anlagen
        if isinstance(triggers[0].get("to"), str):
            werte["unscharf"] = triggers[0]["to"]
    for aktion in config.get("actions") or []:
        if isinstance(aktion, dict) and aktion.get("action") == "switch.turn_on":
            ziel = (aktion.get("target") or {}).get("entity_id")
            if isinstance(ziel, str):
                werte["kaffeemaschine"] = ziel
                break
    for bedingung in config.get("conditions") or []:
        if isinstance(bedingung, dict) and bedingung.get("condition") == "time":
            if isinstance(bedingung.get("after"), str) and isinstance(bedingung.get("before"), str):
                try:
                    werte["von"], werte["bis"] = uhrzeit(bedingung["after"]), uhrzeit(bedingung["before"])
                except argparse.ArgumentTypeError:
                    pass  # z. B. ein Template statt einer Uhrzeit – dann gilt der Standard
    return werte


def _alle_dicts(objekt: Any) -> Iterator[dict[str, Any]]:
    """Alle Dictionaries einer verschachtelten Struktur, in Lesereihenfolge."""
    if isinstance(objekt, dict):
        yield objekt
        for wert in objekt.values():
            yield from _alle_dicts(wert)
    elif isinstance(objekt, list):
        for eintrag in objekt:
            yield from _alle_dicts(eintrag)


def licht_uebernehmen(config: dict[str, Any]) -> dict[str, Any]:
    """Liest Licht, Dunkel-Sensor und Schwelle aus der gespeicherten Licht-Automation –
    nachsichtig, falls sie in Home Assistant bearbeitet wurde."""
    werte: dict[str, Any] = {}
    for eintrag in _alle_dicts(config.get("actions")):
        if "licht" not in werte and eintrag.get("action") == "homeassistant.turn_on":
            ziel = eintrag.get("target")
            if isinstance(ziel, dict) and isinstance(ziel.get("entity_id"), str):
                werte["licht"] = ziel["entity_id"]
        if "dunkel" not in werte and eintrag.get("condition") == "numeric_state":
            sensor, schwelle = eintrag.get("entity_id"), eintrag.get("above")
            if isinstance(sensor, str) and isinstance(schwelle, (int, float)) and not isinstance(schwelle, bool):
                werte["dunkel"], werte["dunkel_unter"] = sensor, schwelle
    return werte


def installieren(
    ha: HomeAssistant, config: dict[str, Any], automation_id: str = AUTOMATION_ID
) -> tuple[str, dict[str, Any]]:
    """Legt die Automation an bzw. aktualisiert sie und wartet, bis sie aktiv ist."""
    pfad = f"/api/config/automation/config/{automation_id}"
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
        zustand = automation_entitaet(ha, automation_id)
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
        return PROBELAUF_SCHON_AN
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


def zeit_lesen(text: Any) -> Optional[datetime.datetime]:
    """Ein Zeitstempel von Home Assistant (ISO 8601, UTC) als Ortszeit – auch mit Python 3.9."""
    if not isinstance(text, str):
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", text.strip().replace("Z", "+00:00"))
    try:
        zeit = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if zeit.tzinfo is None:
        zeit = zeit.replace(tzinfo=datetime.timezone.utc)
    return zeit.astimezone()


def alter_text(minuten: float) -> str:
    """„vor 5 Min.“, „vor 1 Std. 30 Min.“, „vor 2 Tagen“"""
    minuten = int(minuten)
    if minuten < 60:
        return f"vor {minuten} Min."
    if minuten < 24 * 60:
        stunden, rest = divmod(minuten, 60)
        return f"vor {stunden} Std." + (f" {rest} Min." if rest else "")
    tage = minuten // (24 * 60)
    return f"vor {tage} Tag" + ("" if tage == 1 else "en")


def an_aus(zustand: Optional[dict[str, Any]]) -> str:
    if zustand is None:
        return "unbekannt (keine solche Entität)"
    state = str(zustand.get("state"))
    return {"on": "an", "off": "aus", "unavailable": "nicht verfügbar", "unknown": "unbekannt"}.get(state, state)


def laeufe(ha: HomeAssistant, automation_id: str, anzahl: int = LAEUFE) -> list[dict[str, Any]]:
    """Die letzten Läufe einer Automation aus der Ablaufverfolgung von Home Assistant, neueste zuerst."""
    ws = WebSocket(ha.url, ha.token)
    try:
        kurz = ws.befehl(type="trace/list", domain="automation", item_id=automation_id) or []
        kurz = sorted(
            (k for k in kurz if isinstance(k, dict)),
            key=lambda k: str((k.get("timestamp") or {}).get("start") or ""),
            reverse=True,
        )[:anzahl]
        ergebnis = []
        for eintrag in kurz:
            try:
                voll = ws.befehl(
                    type="trace/get", domain="automation", item_id=automation_id, run_id=eintrag["run_id"]
                )
            except Abbruch:
                voll = {}
            ergebnis.append({**eintrag, "trace": (voll or {}).get("trace") or {}})
        return ergebnis
    finally:
        ws.schliessen()


def _ergebnis(trace: dict[str, Any], pfad: str) -> dict[str, Any]:
    schritte = trace.get(pfad) or []
    return (schritte[-1].get("result") or {}) if schritte and isinstance(schritte[-1], dict) else {}


def lauf_erklaeren(lauf: dict[str, Any], art: str, grenze: Optional[float] = None) -> str:
    """Ein Lauf aus der Ablaufverfolgung in einem Satz: Uhrzeit, Auslöser, Ergebnis.

    ``art`` ist „licht“ für die Licht-Automation oder „kaffee“ für die Hauptautomation;
    ``grenze`` die Dunkel-Schwelle, falls die Ablaufverfolgung sie nicht selbst nennt.
    """
    trace = lauf.get("trace") or {}
    zeit = zeit_lesen((lauf.get("timestamp") or {}).get("start"))
    wann = f"{zeit:%d.%m. %H:%M:%S}" if zeit else "unbekannte Zeit"

    ausloeser: dict[str, Any] = {}
    for pfad, schritte in trace.items():
        if pfad.startswith("trigger/") and schritte and isinstance(schritte[0], dict):
            ausloeser = (schritte[0].get("changed_variables") or {}).get("trigger") or {}
            break
    zu = ausloeser.get("to_state") or {}
    zustand = str(zu.get("state")) if isinstance(zu, dict) and "state" in zu else None
    kennung = ausloeser.get("id")
    if art == "licht":
        wer = {
            "an": "Maschine an",
            "aus": "Maschine aus",
            "hell": f"Helligkeit {zustand} (hell)" if zustand else "Helligkeit gestiegen",
            "dunkel": f"Helligkeit {zustand} (dunkel)" if zustand else "Helligkeit gesunken",
        }.get(str(kennung), lauf.get("trigger") or "unbekannter Auslöser")
    else:
        wer = f"Alarmanlage → {zustand}" if zustand else (lauf.get("trigger") or "unbekannter Auslöser")

    was: str
    if art == "licht":
        wahl = _ergebnis(trace, "action/0").get("choice")
        if wahl == 0 or wahl == "else":
            was = "Licht ausgeschaltet"
        elif wahl == 1:
            # Home Assistant notiert den Messwert; die Schwelle nur, wenn die Bedingung nicht zutraf.
            messwert = _ergebnis(trace, "action/0/choose/1/conditions/1/entity_id/0")
            wert = messwert.get("state")
            schwelle = messwert.get("wanted_state_above", grenze)
            was = "Es ist hell – das Licht bleibt aus"
            if isinstance(wert, (int, float)) and isinstance(schwelle, (int, float)):
                was += f" (Helligkeit {wert:g} > {schwelle:g})"
        elif wahl == 2 or wahl == "then":
            was = "Licht eingeschaltet"
        else:
            was = "nichts zu tun (Maschine aus)"
    else:
        if _ergebnis(trace, "condition/0").get("result") is False:
            was = "außerhalb des Zeitfensters"
        elif _ergebnis(trace, "condition/1").get("result") is False:
            was = "Kaffeemaschine war schon an"
        elif "action/0" in trace:
            was = "Kaffeemaschine eingeschaltet"
        else:
            was = "Bedingung nicht erfüllt"
    fehler = lauf.get("error")
    ausfuehrung = lauf.get("script_execution")
    # „failed_conditions“ heißt nur: eine Bedingung traf nicht zu – das steht schon im Ergebnis.
    if fehler or (ausfuehrung and ausfuehrung not in ("finished", "failed_conditions")):
        was += f" – Fehler: {fehler or ausfuehrung}"
    return f"  {wann}  {wer} → {was}."


def pruefung(ha: HomeAssistant, zustaende: dict[str, dict[str, Any]]) -> str:
    """Erklärt in Klartext, warum das Licht an oder aus ist – mit den letzten Läufen beider Automationen."""
    haupt = uebernehmen(vorhandene_automation(ha) or {})
    if not haupt.get("kaffeemaschine"):
        return (
            f"Die Automation „{ALIAS}“ ist noch nicht eingerichtet – bitte das Skript erst einmal ohne "
            "--pruefen ausführen."
        )
    licht_einst = licht_uebernehmen(vorhandene_automation(ha, LICHT_AUTOMATION_ID) or {})
    jetzt = datetime.datetime.now().astimezone()

    def seit(zustand: Optional[dict[str, Any]]) -> str:
        zeit = zeit_lesen((zustand or {}).get("last_changed"))
        return f" (seit {zeit:%H:%M})" if zeit else ""

    kaffeemaschine = haupt["kaffeemaschine"]
    maschine = zustaende.get(kaffeemaschine)
    zeilen = ["Prüfung – Stand jetzt:", f"  Kaffeemaschine {kaffeemaschine}: {an_aus(maschine)}{seit(maschine)}"]

    licht = licht_einst.get("licht")
    dunkel = licht_einst.get("dunkel")
    grenze = licht_einst.get("dunkel_unter", STANDARD_DUNKEL_UNTER)
    licht_zustand = zustaende.get(licht) if licht else None
    sensor = zustaende.get(dunkel) if dunkel else None
    wert = helligkeit(sensor) if sensor else None
    alter: Optional[float] = None
    if licht:
        name = f" („{name_von(licht_zustand)}“)" if licht_zustand else ""
        zeilen.append(f"  Licht {licht}{name}: {an_aus(licht_zustand)}{seit(licht_zustand)}")
    if dunkel:
        gemeldet = zeit_lesen((sensor or {}).get("last_updated"))
        wann = ""
        if gemeldet:
            alter = (jetzt - gemeldet).total_seconds() / 60
            wann = f" – zuletzt gemeldet {gemeldet:%H:%M} ({alter_text(alter)})"
        messung = helligkeit_text(sensor) if sensor else "keine solche Entität"
        zeilen.append(f"  Dunkel-Sensor {dunkel}: {messung}, Grenze {mit_einheit(grenze, sensor or {})}{wann}")

    maschine_an = (maschine or {}).get("state") == "on"
    if not licht:
        urteil = "Es ist kein Licht an der Maschine eingerichtet (--licht)."
    elif licht_zustand is None:
        urteil = f"{licht} gibt es in Home Assistant nicht mehr – ein neues Licht sucht --licht ohne Wert."
    elif not verfuegbar(licht, zustaende):
        urteil = (
            f"{licht} ist nicht verfügbar – Home Assistant erreicht den Schalter nicht. Ist er eingesteckt "
            "und in der Zentrale angelernt?"
        )
    elif not maschine_an:
        urteil = "Die Kaffeemaschine ist aus, deshalb ist das Licht aus. Zum Testen die Maschine einschalten oder --probelauf."
    elif dunkel and wert is not None and wert > grenze:
        urteil = f"Zu hell für das Licht ({wert:g} > {grenze:g})."
        if alter is not None and alter >= VERALTET_MINUTEN:
            urteil += (
                f" Der Wert ist {alter_text(alter)[4:]} alt – der Melder meldet die Helligkeit nur bei Bewegung. "
                "Einmal durch die Küche gehen, dann meldet er neu."
            )
        urteil += " Bleibt es über der Grenze, ist das Küchenlicht zu hell: Grenze mit --dunkel-unter anheben oder --dunkel aus."
    elif (licht_zustand or {}).get("state") == "on":
        urteil = "Alles wie gewünscht: Maschine an, Licht an."
    else:
        urteil = (
            f"Das Licht müsste an sein, ist aber aus. Prüfe in Home Assistant, ob sich {licht} von Hand "
            "schalten lässt – reagiert der Schalter nicht, liegt es am Gerät, nicht an der Automation."
        )
    zeilen.append(f"Urteil: {urteil}")

    for automation_id, alias, art in (
        (LICHT_AUTOMATION_ID, LICHT_ALIAS, "licht"),
        (AUTOMATION_ID, ALIAS, "kaffee"),
    ):
        if art == "licht" and not licht:
            continue
        try:
            runs = laeufe(ha, automation_id)
        except Abbruch as fehler:
            zeilen.append(f"Letzte Läufe „{alias}“: nicht abrufbar ({fehler})")
            continue
        zeilen.append(f"Letzte Läufe „{alias}“:")
        zeilen += [lauf_erklaeren(lauf, art, grenze if art == "licht" else None) for lauf in runs] or [
            "  (noch keiner)"
        ]
    return "\n".join(zeilen)


def wieder_ausschalten(ha: HomeAssistant, kaffeemaschine: str, sekunden: Optional[int] = None) -> str:
    """Nach dem Probelauf: Die Maschine wieder ausschalten – sie war vorher aus."""
    sekunden = AUSSCHALT_SEKUNDEN if sekunden is None else sekunden
    status, _ = ha.anfrage("POST", "/api/services/switch/turn_off", json_daten={"entity_id": kaffeemaschine})
    for _ in range(sekunden if status < 400 else 0):
        if ha.get_json(f"/api/states/{kaffeemaschine}")["state"] == "off":
            return "✓ Die Kaffeemaschine ist wieder aus – sie war vor dem Probelauf aus."
        time.sleep(1)
    return (
        f"⚠ {kaffeemaschine} ließ sich nicht wieder ausschalten – bitte von Hand ausschalten. "
        "Home Connect lehnt das Ausschalten manchmal ab, solange die Maschine noch hochfährt."
    )


def licht_aus_pruefen(ha: HomeAssistant, licht: str, sekunden: int = 10) -> str:
    """Nach dem Ausschalten: Ist das Licht an der Maschine wieder ausgegangen?"""
    for _ in range(sekunden):
        if ha.get_json(f"/api/states/{licht}")["state"] == "off":
            return "✓ Das Licht an der Maschine ist wieder aus."
        time.sleep(1)
    return f"⚠ {licht} ist nicht wieder ausgegangen – bitte die Licht-Automation in Home Assistant prüfen."


def hell_hinweis(ha: HomeAssistant, dunkel: str, dunkel_unter: "int | float") -> Optional[str]:
    """Vor dem Probelauf: Ist es gerade so hell, dass das Licht aus bleiben soll?"""
    zustand = ha.get_json(f"/api/states/{dunkel}")
    wert = helligkeit(zustand)
    if wert is None or wert <= dunkel_unter:
        return None
    return (
        f"ℹ Es ist hell ({dunkel}: {helligkeit_text(zustand)}, Licht nur bis "
        f"{mit_einheit(dunkel_unter, zustand)}) – das Licht bleibt aus, so soll es sein."
    )


def licht_pruefen(
    ha: HomeAssistant, licht: str, hell: Optional[str] = None, sekunden: int = 10
) -> str:
    """Nach dem Probelauf: Ist das Licht an der Maschine mit angegangen? Bei Helligkeit soll es aus bleiben."""
    if hell:
        return hell
    for _ in range(sekunden):
        zustand = ha.get_json(f"/api/states/{licht}")["state"]
        if zustand == "on":
            return "✓ Das Licht an der Maschine ist angegangen."
        if zustand == "unavailable":
            break
        time.sleep(1)
    return (
        f"⚠ {licht} ist nicht mit angegangen. Ist die Steckdose bzw. das Licht eingesteckt "
        "und in Home Assistant erreichbar? Die Automation ist trotzdem eingerichtet."
    )


# ---------------------------------------------------------------------------
# Dashboard für die Kaffeemaschine (--getraenke): Getränke, Optionen,
# Lieblingsgetränke, Bedienung, Wartung und Zähler
# ---------------------------------------------------------------------------

DASHBOARD_URL = "kaffee-konrad"  # Dashboards brauchen einen Bindestrich im Pfad
REITER_PFAD = "kaffee-konrad"  # Pfad der Konrad-Ansicht als Reiter in anderen Dashboards
REITER_KENNZEICHEN = "kaffee_dashboard"  # so erkennt das Skript seinen eigenen Reiter wieder
WEITERE_DASHBOARDS = "weitere_dashboards"  # Schlüssel im Konrad-Dashboard: wo der Reiter sonst noch liegt
STANDARD_DASHBOARD = "lovelace"  # so heißt das Standard-Dashboard „Übersicht“ in der API
AUCH_AUTO = "auto"  # --auch-in ohne Wert: Dashboards zur Auswahl anbieten
AUCH_AUS = "aus"  # --auch-in aus: Reiter wieder entfernen
SKRIPT_ZUBEREITEN = "kaffee_zubereiten"
SKRIPT_SPEICHERN = "lieblingsgetraenk_speichern"
SKRIPT_LIEBLING = "lieblingsgetraenk_zubereiten"
HELFER_PRAEFIX = "Kaffee"  # Helfer heißen „Kaffee: Getränk“, „Kaffee: Stärke“ …
WIE_MASCHINE = "wie an der Maschine"
PLATZ_FREI = "frei – gedrückt halten, um die Zusammenstellung zu speichern"
MAX_PERSONEN = 4
MAX_PLAETZE = 5
STANDARD_PLAETZE = 3
PROGRAMM_PRAEFIX = "consumer_products_coffee_maker_program_"
ENUM_PRAEFIX = "consumer_products_coffee_maker_enum_type_"
VORBEREITUNG_SEKUNDEN = 90  # so lange wartet das Skript, bis die Maschine an und bereit ist
VORBEREITUNG_PAUSE = 3  # Sekunden, bis Home Assistant die Optionen des gewählten Getränks kennt
OPTIONEN_PAUSE = 1  # Sekunden im Skript zwischen Getränkewahl und Optionen
AUSWAHL_SEKUNDEN = 10  # so lange wartet das Skript, bis die Maschine das Getränk übernommen hat
SKRIPT_SEKUNDEN = 10  # so lange wartet das Skript, bis ein gespeichertes Skript in Home Assistant auftaucht
# Deutsche Namen der Getränke, wie Home Connect sie schlüsselt (ohne Programm-Präfix).
GETRAENKE_NAMEN = {
    "beverage_espresso": "Espresso",
    "beverage_espresso_doppio": "Espresso Doppio",
    "beverage_coffee": "Caffè Crema",
    "beverage_caffe_grande": "Caffè Grande",
    "beverage_x_l_coffee": "Caffè XL",
    "beverage_cappuccino": "Cappuccino",
    "beverage_latte_macchiato": "Latte Macchiato",
    "beverage_caffe_latte": "Milchkaffee",
    "beverage_milk_froth": "Milchschaum",
    "beverage_warm_milk": "Warme Milch",
    "beverage_hot_water": "Heißwasser",
    "beverage_ristretto": "Ristretto",
    "beverage_espresso_macchiato": "Espresso Macchiato",
    "beverage_coffee_pot": "Kaffeekanne",
    "coffee_world_verlaengerter": "Verlängerter",
    "coffee_world_kleiner_brauner": "Kleiner Brauner",
    "coffee_world_wiener_melange": "Wiener Melange",
    "coffee_world_flat_white": "Flat White",
    "coffee_world_cafe_con_leche": "Café con Leche",
    "coffee_world_cafe_au_lait": "Café au Lait",
    "coffee_world_koffie_verkeerd": "Koffie Verkeerd",
    "coffee_world_americano": "Americano",
    "coffee_world_grosser_brauner": "Großer Brauner",
    "coffee_world_verlaengerter_braun": "Verlängerter Braun",
    "coffee_world_cortado": "Cortado",
    "coffee_world_doppio": "Doppio",
    "coffee_world_cafe_cortado": "Café Cortado",
    "coffee_world_kaapi": "Kaapi",
    "coffee_world_galao": "Galão",
    "coffee_world_garoto": "Garoto",
    "coffee_world_red_eye": "Red Eye",
    "coffee_world_black_eye": "Black Eye",
    "coffee_world_dead_eye": "Dead Eye",
    "coffee_world_cold_brew": "Cold Brew",
    "coffee_world_cold_brew_macchiato": "Cold Brew Macchiato",
}
# Frühere Namen – gespeicherte Lieblingsgetränke und Sprachbefehle mit ihnen gelten weiter.
ALTE_NAMEN = {"Kaffee": "beverage_coffee", "XL-Kaffee": "beverage_x_l_coffee", "Caffè Latte": "beverage_caffe_latte"}
# Auswahlwerte: (Anzeige, Endung des Home-Connect-Schlüssels)
STAERKEN = [
    ("sehr mild", "very_mild"), ("mild", "mild"), ("mild+", "mild_plus"), ("normal", "normal"),
    ("normal+", "normal_plus"), ("stark", "strong"), ("stark+", "strong_plus"),
    ("sehr stark", "very_strong"), ("sehr stark+", "very_strong_plus"), ("extra stark", "extra_strong"),
    ("Double Shot", "double_shot"), ("Double Shot+", "double_shot_plus"),
    ("Double Shot++", "double_shot_plus_plus"), ("Triple Shot", "triple_shot"),
    ("Triple Shot+", "triple_shot_plus"), ("Pulverkaffee", "coffee_ground"),
]
TEMPERATUREN = [(f"{grad} °C", f"{grad}_c") for grad in (88, 90, 92, 94, 95, 96)]
MILCHANTEILE = [
    (f"{prozent} %", f"{prozent}_percent")
    for prozent in (10, 20, 25, 30, 40, 50, 55, 60, 65, 67, 70, 75, 80, 85, 90)
]
INTENSITAETEN = [("Normal", "normal"), ("Intensiv", "intense"), ("Intensiv+", "intense_plus")]
BOHNENBEHAELTER = [("links", "left"), ("rechts", "right")]
HEISSWASSER = (
    [("Weißer Tee", "white_tea"), ("Grüner Tee", "green_tea"), ("Schwarzer Tee", "black_tea")]
    + [(f"{grad} °C", f"{grad}_c") for grad in (50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 97)]
    + [("Maximal", "max")]
)
# Welche Entitäten der Kaffeemaschine das Dashboard nutzt: translation_key → Kurzname.
MASCHINEN_SCHLUESSEL = {
    "active_program": "aktiv",
    "selected_program": "gewaehlt",
    "bean_amount": "staerke",
    "fill_quantity": "menge",
    "coffee_temperature": "temperatur",
    "coffee_milk_ratio": "milch",
    "flow_rate": "intensitaet",
    "bean_container": "bohnen",
    "multiple_beverages": "doppelt",
    "hot_water_temperature": "heisswasser",
    "power": "einschalter",
    "child_lock": "kindersicherung",
    "cup_warmer": "tassenwaermer",
    "stop_program": "stopp",
    "operation_state": "zustand",
    "program_progress": "fortschritt",
    "program_finish_time": "endzeit",
    "remote_start": "fernstart",
}
# Kurzname, Anzeigename, Werte, Teil des Enum-Schlüssels, Symbol, Farbe
OPTIONEN = (
    ("staerke", "Stärke", STAERKEN, "bean_amount_", "mdi:seed", "brown"),
    ("temperatur", "Temperatur", TEMPERATUREN, "coffee_temperature_", "mdi:thermometer", "red"),
    ("milch", "Milchanteil", MILCHANTEILE, "coffee_milk_ratio_", "mdi:cup", "blue-grey"),
    ("intensitaet", "Intensität", INTENSITAETEN, "flow_rate_", "mdi:speedometer", "orange"),
    ("bohnen", "Bohnenbehälter", BOHNENBEHAELTER, "bean_container_selection_", "mdi:silo", "brown"),
    ("heisswasser", "Heißwasser-Temperatur", HEISSWASSER, "hot_water_temperature_", "mdi:kettle-steam", "teal"),
)
NUR_WENN_VORHANDEN = ("bohnen", "heisswasser")  # Helfer nur, wenn die Maschine die Option hat
WARTUNG = re.compile(r"descal|calc_n_clean|water_tank|bean_container_empty|drip_tray|milk_tank|clean|rinse")
WARTUNG_IMMER = ("water_tank_empty", "bean_container_empty", "drip_tray_full")  # immer zu sehen, der Rest nur, wenn er anliegt
ZAEHLER = re.compile(r"_counter$")
WARTUNG_SYMBOLE = (  # Teil des Schlüssels → Symbol, Farbe
    ("water_tank", "mdi:water-alert", "blue"),
    ("bean_container", "mdi:seed-off", "brown"),
    ("drip_tray", "mdi:tray-alert", "grey"),
    ("milk", "mdi:cup-off", "blue-grey"),
    ("descal", "mdi:water-off", "deep-orange"),
    ("calc_n_clean", "mdi:spray-bottle", "teal"),
    ("clean", "mdi:broom", "teal"),
    ("rinse", "mdi:water-sync", "cyan"),
)
LAUFEND = ["run", "pause", "delayedstart", "actionrequired", "aborting"]
TASSE = {"text": "Steht eine Tasse unter dem Auslauf? Dann geht es los."}


def slug(text: str) -> str:
    """Wie Home Assistant aus einem Namen eine Kennung macht: „Lieblingsgetränk Jens 1“ → lieblingsgetrank_jens_1."""
    text = unicodedata.normalize("NFKD", text.replace("ß", "ss"))
    text = "".join(zeichen for zeichen in text if not unicodedata.combining(zeichen))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def anzahl_plaetze(text: str) -> int:
    try:
        zahl = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"„{text}“ ist keine Zahl") from None
    if not 1 <= zahl <= MAX_PLAETZE:
        raise argparse.ArgumentTypeError(f"1 bis {MAX_PLAETZE} Plätze je Person")
    return zahl


def personen_lesen(text: Optional[str]) -> list[str]:
    personen = [p.strip() for p in (text or "").split(",") if p.strip()]
    if not personen:
        raise Abbruch("Für wen sollen Lieblingsgetränke angelegt werden? Zum Beispiel: --personen Jens,Nicole")
    if len(personen) > MAX_PERSONEN:
        raise Abbruch(f"Höchstens {MAX_PERSONEN} Personen – sonst wird das Dashboard unübersichtlich.")
    kennungen = [slug(p) for p in personen]
    if not all(kennungen) or len(set(kennungen)) != len(kennungen):
        raise Abbruch("Die Namen in --personen müssen aus Buchstaben bestehen und sich unterscheiden.")
    return personen


def getraenk_name(schluessel: str) -> str:
    kurz = schluessel[len(PROGRAMM_PRAEFIX):] if schluessel.startswith(PROGRAMM_PRAEFIX) else schluessel
    if kurz in GETRAENKE_NAMEN:
        return GETRAENKE_NAMEN[kurz]
    return re.sub(r"^(beverage|coffee_world)_", "", kurz).replace("_", " ").title()


def getraenke_liste(zustaende: dict[str, dict[str, Any]], aktiv: Optional[str]) -> list[tuple[str, str]]:
    """(Anzeige, Programmschlüssel) der Getränke, die die Maschine meldet – ohne Reinigungsprogramme.

    Meldet sie gerade keine (ausgeschaltet, noch nie an mit Home Assistant), gilt die Liste
    aller bekannten Getränke.
    """
    optionen = ((zustaende.get(aktiv or "") or {}).get("attributes") or {}).get("options") or []
    schluessel = [
        o for o in optionen if isinstance(o, str) and o.startswith(PROGRAMM_PRAEFIX) and "cleaning" not in o
    ]
    if not schluessel:
        schluessel = [PROGRAMM_PRAEFIX + kurz for kurz in GETRAENKE_NAMEN]
    liste: list[tuple[str, str]] = []
    for s in schluessel:
        name = getraenk_name(s)
        if name not in {n for n, _ in liste}:
            liste.append((name, s))
    return liste


def kaffee_geraet(register: "Register", kaffeemaschine: str) -> Optional[dict[str, Any]]:
    eintrag = register.entitaeten.get(kaffeemaschine) or {}
    return register.geraete.get(eintrag.get("device_id") or "")


def kaffee_entitaeten(register: "Register", kaffeemaschine: str) -> dict[str, dict[str, Any]]:
    """translation_key → Registereintrag aller Entitäten am Gerät der Kaffeemaschine."""
    geraet = kaffee_geraet(register, kaffeemaschine)
    if not geraet:
        return {}
    ergebnis: dict[str, dict[str, Any]] = {}
    for entitaet in register.entitaeten_von(geraet):
        schluessel = entitaet.get("translation_key")
        if schluessel and schluessel not in ergebnis:
            ergebnis[schluessel] = entitaet
    return ergebnis


def bedienbare(eintraege: dict[str, dict[str, Any]], kaffeemaschine: str) -> dict[str, str]:
    """Kurzname → Entitäts-ID der aktiven Entitäten, die das Dashboard bedient."""
    maschine = {
        kurz: e["entity_id"]
        for schluessel, kurz in MASCHINEN_SCHLUESSEL.items()
        if (e := eintraege.get(schluessel)) and not e.get("disabled_by")
    }
    maschine["einschalter"] = kaffeemaschine
    return maschine


def helfer_anlegen(ws: "WebSocket", domain: str, name: str, **felder: Any) -> str:
    """Legt einen Helfer (input_select, input_number, …) an oder aktualisiert ihn; gibt die Entitäts-ID zurück.

    Der Startwert (``initial``) bleibt dem Anlegen vorbehalten – ein zweiter Lauf
    überschreibt keine gespeicherten Werte.
    """
    vorhandene = ws.befehl(type=f"{domain}/list") or []
    treffer = next((h for h in vorhandene if isinstance(h, dict) and h.get("name") == name), None)
    if treffer:
        felder.pop("initial", None)
        ws.befehl(type=f"{domain}/update", **{f"{domain}_id": treffer["id"]}, name=name, **felder)
        return f"{domain}.{treffer['id']}"
    neu = ws.befehl(type=f"{domain}/create", name=name, **felder)
    return f"{domain}.{neu['id']}"


def skript_anlegen(ha: HomeAssistant, kennung: str, config: dict[str, Any]) -> str:
    """Speichert ein Skript über den Skript-Editor und wartet, bis es in Home Assistant auftaucht."""
    pfad = f"/api/config/script/config/{kennung}"
    status, text = ha.anfrage("POST", pfad, json_daten=config)
    if status == 404:
        raise Abbruch(
            "Home Assistant hat keinen Skript-Editor (Integration „config“). "
            "Sie gehört zu default_config und muss in der configuration.yaml aktiv sein."
        )
    ha._json(status, text, "POST", pfad)
    entity_id = f"script.{kennung}"
    for _ in range(SKRIPT_SEKUNDEN * 2):
        status, _ = ha.anfrage("GET", f"/api/states/{entity_id}")
        if status == 200:
            return entity_id
        time.sleep(0.5)
    raise Abbruch(
        f"Das Skript „{config.get('alias', kennung)}“ wurde gespeichert, taucht aber nicht in Home Assistant "
        "auf. Steht in der configuration.yaml die Zeile „script: !include scripts.yaml“?"
    )


def dashboard_anlegen(ws: "WebSocket", url_path: str, titel: str, config: dict[str, Any]) -> bool:
    """Legt das Dashboard an (falls neu) und speichert seine Konfiguration. True, wenn es neu war."""
    vorhandene = ws.befehl(type="lovelace/dashboards/list") or []
    neu = not any(isinstance(d, dict) and d.get("url_path") == url_path for d in vorhandene)
    if neu:
        ws.befehl(
            type="lovelace/dashboards/create",
            url_path=url_path,
            title=titel,
            icon="mdi:coffee",
            show_in_sidebar=True,
            require_admin=False,
        )
    ws.befehl(type="lovelace/config/save", url_path=url_path, config=config)
    return neu


def dashboards_auflisten(ws: "WebSocket") -> list[tuple[str, str]]:
    """(Pfad, Beschreibung) der Dashboards, in die sich ein Reiter legen lässt – ohne das Konrad-Dashboard.

    YAML-Dashboards fehlen (sie lassen sich nicht per Skript ändern); die „Übersicht“ steht vorn.
    """
    liste = [(STANDARD_DASHBOARD, "„Übersicht“ (Standard-Dashboard)")]
    for dashboard in ws.befehl(type="lovelace/dashboards/list") or []:
        if not isinstance(dashboard, dict) or dashboard.get("url_path") in (DASHBOARD_URL, None):
            continue
        if dashboard.get("mode", "storage") != "storage":
            continue
        liste.append((dashboard["url_path"], f"„{dashboard.get('title') or dashboard['url_path']}“"))
    return liste


def _dashboard_konfiguration_lesen(ws: "WebSocket", url_path: str) -> dict[str, Any]:
    """Die gespeicherte Konfiguration eines Dashboards – mit Erklärung, wenn es keine gibt."""
    ziel = None if url_path == STANDARD_DASHBOARD else url_path
    try:
        config = ws.befehl(type="lovelace/config", url_path=ziel)
    except WebSocketFehler as fehler:
        if fehler.code == "config_not_found":
            raise Abbruch(
                f"Das Dashboard „{url_path}“ wird von Home Assistant automatisch erzeugt und lässt sich so nicht "
                "ergänzen. Erst im Dashboard oben rechts auf den Stift tippen und „Steuerung übernehmen“ wählen, "
                "dann das Skript erneut ausführen."
            ) from fehler
        raise
    if not isinstance(config, dict) or "strategy" in config or not isinstance(config.get("views"), list):
        raise Abbruch(
            f"Das Dashboard „{url_path}“ wird automatisch zusammengestellt (Strategie) – ein Reiter lässt sich "
            "nur in ein von Hand gestaltetes Dashboard legen."
        )
    return config


def reiter_einfuegen(ws: "WebSocket", url_path: str, ansicht: dict[str, Any]) -> str:
    """Legt die Konrad-Ansicht als Reiter in ein anderes Dashboard – oder erneuert den eigenen Reiter dort."""
    config = _dashboard_konfiguration_lesen(ws, url_path)
    reiter = {**ansicht, "path": REITER_PFAD, REITER_KENNZEICHEN: True}
    ansichten = [a for a in config["views"] if isinstance(a, dict)]
    eigene = [a for a in ansichten if a.get(REITER_KENNZEICHEN)]
    if any(a.get("path") == REITER_PFAD for a in ansichten if not a.get(REITER_KENNZEICHEN)):
        raise Abbruch(
            f"Im Dashboard „{url_path}“ gibt es schon einen Reiter mit dem Pfad „{REITER_PFAD}“, der nicht vom "
            "Skript stammt – bitte dort umbenennen."
        )
    config["views"] = [a for a in ansichten if not a.get(REITER_KENNZEICHEN)] + [reiter]
    ws.befehl(type="lovelace/config/save", url_path=None if url_path == STANDARD_DASHBOARD else url_path, config=config)
    return "aktualisiert" if eigene else "angelegt"


def reiter_entfernen(ws: "WebSocket", url_path: str) -> bool:
    """Nimmt den Konrad-Reiter aus einem Dashboard heraus; True, wenn es ihn gab."""
    try:
        config = _dashboard_konfiguration_lesen(ws, url_path)
    except Abbruch:
        return False
    ansichten = [a for a in config["views"] if isinstance(a, dict)]
    rest = [a for a in ansichten if not a.get(REITER_KENNZEICHEN)]
    if len(rest) == len(ansichten):
        return False
    config["views"] = rest
    ws.befehl(type="lovelace/config/save", url_path=None if url_path == STANDARD_DASHBOARD else url_path, config=config)
    return True


def gemerkte_dashboards(ws: "WebSocket") -> list[str]:
    """Die Dashboards, in denen der Konrad-Reiter beim letzten Lauf lag (steht im Konrad-Dashboard)."""
    try:
        config = ws.befehl(type="lovelace/config", url_path=DASHBOARD_URL)
    except Abbruch:
        return []
    gemerkt = (config or {}).get(WEITERE_DASHBOARDS) if isinstance(config, dict) else None
    return [d for d in gemerkt if isinstance(d, str)] if isinstance(gemerkt, list) else []


def auch_in_bestimmen(ws: "WebSocket", wunsch: Optional[str], gemerkt: list[str], interaktiv: bool) -> list[str]:
    """In welche weiteren Dashboards der Konrad-Reiter soll: aus --auch-in, zur Auswahl oder wie gemerkt."""
    if wunsch is None:
        return gemerkt
    if wunsch == AUCH_AUS:
        return []
    moeglich = dashboards_auflisten(ws)
    if wunsch == AUCH_AUTO:
        if not interaktiv:
            raise Abbruch(
                "--auch-in braucht ohne Terminal die Dashboards als Pfade, durch Komma getrennt, z. B. "
                f"--auch-in {','.join(p for p, _ in moeglich[:2])}. Möglich sind: "
                + ", ".join(f"{p} ({b})" for p, b in moeglich)
            )
        gewaehlt = menue(
            "In welche weiteren Dashboards soll die Kaffeemaschine als Reiter? (mehrere möglich)",
            [f"{beschreibung}  –  {pfad}" for pfad, beschreibung in moeglich],
            mehrere=True,
            optional=True,
        )
        return [moeglich[i][0] for i in gewaehlt]
    ziele = [teil.strip().strip("/") for teil in wunsch.split(",") if teil.strip()]
    unbekannt = [z for z in ziele if z not in {p for p, _ in moeglich}]
    if unbekannt:
        raise Abbruch(
            f"Dashboard nicht gefunden: {', '.join(unbekannt)}. Möglich sind (Pfad aus der Adresszeile): "
            + ", ".join(f"{p} ({b})" for p, b in moeglich)
        )
    return list(dict.fromkeys(ziele))


def hinweis_aktion(text: str) -> dict[str, Any]:
    return {
        "action": "persistent_notification.create",
        "data": {"title": "Kaffeemaschine", "message": text, "notification_id": "kaffee_dashboard"},
    }


def wertetabelle(werte: list[tuple[str, str]], enum_teil: str) -> dict[str, str]:
    return {anzeige: f"{ENUM_PRAEFIX}{enum_teil}{endung}" for anzeige, endung in werte}


def skript_konfigurationen(
    maschine: dict[str, str],
    helfer: dict[str, str],
    plaetze: dict[str, dict[str, str]],
    geraete_id: str,
    getraenke: list[tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    """Die drei Skripte: Getränk zubereiten, Lieblingsgetränk speichern, Lieblingsgetränk zubereiten.

    ``maschine`` und ``helfer`` ordnen Kurznamen Entitäts-IDs zu (was fehlt, lässt das
    Skript weg), ``plaetze`` ordnet Platzkennungen Anzeigename und die beiden Textfelder zu.
    """
    optionen = {kurz: (name, werte, enum) for kurz, name, werte, enum, _, _ in OPTIONEN}
    auswahl = {anzeige: schluessel for anzeige, schluessel in getraenke}
    auswahl.update(
        {alt: PROGRAMM_PRAEFIX + kurz for alt, kurz in ALTE_NAMEN.items() if PROGRAMM_PRAEFIX + kurz in auswahl.values()}
    )

    def vorgabe(kurz: str) -> str:
        """Der Wert aus dem Dashboard, wenn das Skript ohne diese Angabe aufgerufen wird."""
        helfer_id = helfer.get(kurz)
        if helfer_id:
            return f"{{{{ {kurz} | default(states('{helfer_id}'), true) }}}}"
        return f"{{{{ {kurz} | default('', true) }}}}"

    felder: dict[str, Any] = {
        "getraenk": {
            "name": "Getränk",
            "description": "Name wie auf dem Dashboard, z. B. Cappuccino. Ohne Angabe gilt die Auswahl im Dashboard.",
            "selector": {"select": {"options": [n for n, _ in getraenke], "custom_value": True}},
        },
        "menge": {
            "name": "Menge in ml",
            "description": "0 = wie an der Maschine; wird auf den Bereich des Getränks begrenzt.",
            "selector": {"number": {"min": 0, "max": 500, "step": 5, "unit_of_measurement": "ml"}},
        },
        "doppelt": {"name": "Doppelt (zwei Tassen)", "selector": {"boolean": {}}},
    }
    variablen: dict[str, Any] = {
        "getraenke": auswahl,
        "wunsch_getraenk": vorgabe("getraenk"),
        "programm": "{{ getraenke.get(wunsch_getraenk, wunsch_getraenk) }}",
        "wunsch_menge": (
            f"{{{{ menge | default(states('{helfer['menge']}')) | float(0) | int }}}}"
            if helfer.get("menge")
            else "{{ menge | default(0) | float(0) | int }}"
        ),
        "wunsch_doppelt": (
            f"{{{{ doppelt | default(is_state('{helfer['doppelt']}', 'on')) | bool(false) }}}}"
            if helfer.get("doppelt")
            else "{{ doppelt | default(false) | bool(false) }}"
        ),
    }
    for kurz, (name, werte, enum) in optionen.items():
        felder[kurz] = {
            "name": name,
            "description": f"„{WIE_MASCHINE}“ lässt die Einstellung der Maschine unangetastet.",
            "selector": {"select": {"options": [WIE_MASCHINE] + [a for a, _ in werte]}},
        }
        variablen[kurz + "_werte"] = wertetabelle(werte, enum)
        variablen["wunsch_" + kurz] = vorgabe(kurz)

    ablauf: list[dict[str, Any]] = []
    if maschine.get("fernstart"):
        ablauf.append(
            {
                "if": [{"condition": "state", "entity_id": maschine["fernstart"], "state": "off"}],
                "then": [
                    hinweis_aktion(
                        "Fernstart ist an der Maschine nicht erlaubt – im Menü der Maschine „Fernstart“ einschalten."
                    ),
                    {"stop": "Fernstart nicht erlaubt"},
                ],
            }
        )
    warten: dict[str, Any] = (
        {
            "wait_template": f"{{{{ is_state('{maschine['zustand']}', 'ready') }}}}",
            "timeout": {"seconds": VORBEREITUNG_SEKUNDEN},
            "continue_on_timeout": True,
        }
        if maschine.get("zustand")
        else {"delay": {"seconds": 20}}
    )
    ablauf.append(
        {
            "if": [{"condition": "state", "entity_id": maschine["einschalter"], "state": "off"}],
            "then": [{"action": "switch.turn_on", "target": {"entity_id": maschine["einschalter"]}}, warten],
        }
    )
    ablauf.append(
        {
            "action": "home_connect.set_program_and_options",
            "data": {"device_id": geraete_id, "affects_to": "selected_program", "program": "{{ programm }}"},
        }
    )
    if maschine.get("gewaehlt"):
        ablauf.append(
            {
                "wait_template": f"{{{{ is_state('{maschine['gewaehlt']}', programm) }}}}",
                "timeout": {"seconds": AUSWAHL_SEKUNDEN},
                "continue_on_timeout": True,
            }
        )
    ablauf.append({"delay": {"seconds": OPTIONEN_PAUSE}})
    for kurz in optionen:
        entity_id = maschine.get(kurz)
        if not entity_id:
            continue
        ablauf.append(
            {
                "if": [
                    {
                        "condition": "template",
                        "value_template": (
                            f"{{{{ wunsch_{kurz} in {kurz}_werte and {kurz}_werte[wunsch_{kurz}] in "
                            f"(state_attr('{entity_id}', 'options') or []) }}}}"
                        ),
                    }
                ],
                "then": [
                    {
                        "action": "select.select_option",
                        "target": {"entity_id": entity_id},
                        "data": {"option": f"{{{{ {kurz}_werte[wunsch_{kurz}] }}}}"},
                    }
                ],
            }
        )
    if maschine.get("menge"):
        menge = maschine["menge"]
        ablauf.append(
            {
                "if": [
                    {
                        "condition": "template",
                        "value_template": (
                            f"{{{{ wunsch_menge > 0 and states('{menge}') not in ['unknown', 'unavailable'] }}}}"
                        ),
                    }
                ],
                "then": [
                    {
                        "action": "number.set_value",
                        "target": {"entity_id": menge},
                        "data": {
                            "value": (
                                f"{{{{ [[wunsch_menge, state_attr('{menge}', 'min') | float(0)] | max, "
                                f"state_attr('{menge}', 'max') | float(500)] | min }}}}"
                            )
                        },
                    }
                ],
            }
        )
    if maschine.get("doppelt"):
        doppelt = maschine["doppelt"]
        ablauf.append(
            {
                "if": [
                    {
                        "condition": "template",
                        "value_template": f"{{{{ states('{doppelt}') not in ['unknown', 'unavailable'] }}}}",
                    }
                ],
                "then": [
                    {
                        "if": [{"condition": "template", "value_template": "{{ wunsch_doppelt }}"}],
                        "then": [{"action": "switch.turn_on", "target": {"entity_id": doppelt}}],
                        "else": [{"action": "switch.turn_off", "target": {"entity_id": doppelt}}],
                    }
                ],
            }
        )
    ablauf.append({"action": "home_connect.start_selected_program", "data": {"device_id": geraete_id}})

    zubereiten = {
        "alias": "Kaffee zubereiten",
        "icon": "mdi:coffee",
        "mode": "single",
        "description": (
            "Wählt das Getränk an der Kaffeemaschine, setzt die gewünschten Optionen und startet. "
            "Ohne Angaben gelten die Werte aus „Getränk zusammenstellen“ auf dem Dashboard."
        ),
        "fields": felder,
        "variables": variablen,
        "sequence": ablauf,
    }

    platz_feld = {
        "platz": {
            "name": "Platz",
            "required": True,
            "selector": {
                "select": {
                    "options": [{"label": daten["label"], "value": kennung} for kennung, daten in plaetze.items()]
                }
            },
        }
    }
    platz_tabelle = {kennung: {"name": daten["name"], "daten": daten["daten"]} for kennung, daten in plaetze.items()}
    werte: dict[str, Any] = {
        "g": f"{{{{ states('{helfer['getraenk']}') }}}}" if helfer.get("getraenk") else "",
        "m": f"{{{{ states('{helfer['menge']}') | float(0) | int }}}}" if helfer.get("menge") else 0,
        "d": f"{{{{ is_state('{helfer['doppelt']}', 'on') }}}}" if helfer.get("doppelt") else False,
    }
    kuerzel = {"staerke": "s", "temperatur": "t", "milch": "mi", "intensitaet": "i", "bohnen": "b", "heisswasser": "h"}
    for kurz, kurzform in kuerzel.items():
        werte[kurzform] = f"{{{{ states('{helfer[kurz]}') }}}}" if helfer.get(kurz) else WIE_MASCHINE
    beschreibung = (
        "{% set ns = namespace(teile=[daten.g]) %}"
        "{% if daten.s != wie %}{% set ns.teile = ns.teile + [daten.s] %}{% endif %}"
        "{% if daten.m | int(0) > 0 %}{% set ns.teile = ns.teile + [daten.m ~ ' ml'] %}{% endif %}"
        "{% if daten.t != wie %}{% set ns.teile = ns.teile + [daten.t] %}{% endif %}"
        "{% if daten.mi != wie %}{% set ns.teile = ns.teile + ['Milch ' ~ daten.mi] %}{% endif %}"
        "{% if daten.i != wie %}{% set ns.teile = ns.teile + [daten.i] %}{% endif %}"
        "{% if daten.b != wie %}{% set ns.teile = ns.teile + ['Bohnen ' ~ daten.b] %}{% endif %}"
        "{% if daten.h != wie %}{% set ns.teile = ns.teile + ['Wasser ' ~ daten.h] %}{% endif %}"
        "{% if daten.d %}{% set ns.teile = ns.teile + ['doppelt'] %}{% endif %}"
        "{{ ns.teile | join(' · ') }}"
    )
    speichern = {
        "alias": "Lieblingsgetränk speichern",
        "icon": "mdi:content-save",
        "mode": "parallel",
        "description": "Legt die aktuelle Zusammenstellung vom Dashboard auf einem Lieblingsgetränk-Platz ab.",
        "fields": platz_feld,
        "variables": {"plaetze": platz_tabelle, "wie": WIE_MASCHINE, "daten": werte},
        "sequence": [
            {"variables": {"beschreibung": beschreibung}},
            {
                "action": "input_text.set_value",
                "target": {"entity_id": "{{ plaetze[platz].daten }}"},
                "data": {"value": "{{ daten | to_json }}"},
            },
            {
                "action": "input_text.set_value",
                "target": {"entity_id": "{{ plaetze[platz].name }}"},
                "data": {"value": "{{ beschreibung }}"},
            },
        ],
    }
    liebling = {
        "alias": "Lieblingsgetränk zubereiten",
        "icon": "mdi:coffee-to-go",
        "mode": "single",
        "description": "Bereitet das auf einem Platz gespeicherte Lieblingsgetränk zu.",
        "fields": platz_feld,
        "variables": {"plaetze": platz_tabelle, "roh": "{{ states(plaetze[platz].daten) }}"},
        "sequence": [
            {
                "if": [{"condition": "template", "value_template": "{{ roh in ['', 'unknown', 'unavailable'] }}"}],
                "then": [
                    hinweis_aktion(
                        "Auf diesem Platz ist noch kein Getränk gespeichert – erst „Getränk zusammenstellen“, "
                        "dann die Taste gedrückt halten."
                    ),
                    {"stop": "Platz leer"},
                ],
            },
            {"variables": {"f": "{{ roh | from_json }}"}},
            {
                "action": f"script.{SKRIPT_ZUBEREITEN}",
                "data": {
                    "getraenk": "{{ f.g }}",
                    "staerke": "{{ f.s }}",
                    "menge": "{{ f.m }}",
                    "temperatur": "{{ f.t }}",
                    "milch": "{{ f.mi }}",
                    "intensitaet": "{{ f.i }}",
                    "bohnen": "{{ f.b }}",
                    "heisswasser": "{{ f.h }}",
                    "doppelt": "{{ f.d }}",
                },
            },
        ],
    }
    return {SKRIPT_ZUBEREITEN: zubereiten, SKRIPT_SPEICHERN: speichern, SKRIPT_LIEBLING: liebling}


# ---------------------------------------------------------------------------
# Die Zeichnung der Kaffeemaschine für den Kopf des Dashboards
# ---------------------------------------------------------------------------

BILD_BREITE, BILD_HOEHE = 1200, 500  # Seitenverhältnis 12:5
SCHRIFT = "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
FUELLDAUER = "22s"  # so lange „braucht“ das gezeichnete Glas, bis es voll ist (Schleife)
BILD_VARIANTEN = {  # Variante → (Statuszeile, Untertitel, Farbe der Statuszeile)
    "aus": ("Aus", "Zum Einschalten auf die Taste tippen", "#9e9e9e"),
    "bereit": ("Bereit", "Tasse hinstellen und Getränk wählen", "#43a047"),
    "brueht": ("Brüht …", "Der Kaffee läuft", "#fb8c00"),
    "fertig": ("Fertig", "Getränk bitte entnehmen", "#1e88e5"),
    "achtung": ("Achtung", "Bitte an der Maschine nachsehen", "#f9a825"),
    "fehler": ("Störung", "Hinweis an der Maschine lesen", "#e53935"),
}
ZUSTAND_BILDER = {  # Betriebszustand von Home Connect → Variante
    "inactive": "aus",
    "ready": "bereit",
    "delayedstart": "bereit",
    "aborting": "bereit",
    "run": "brueht",
    "pause": "brueht",
    "finished": "fertig",
    "actionrequired": "achtung",
    "error": "fehler",
}
UNBEKANNT_FILTER = "grayscale(100%) opacity(45%)"
# Stellen in der Zeichnung (Koordinaten der 1200×500-Fläche)
MARKIERUNGEN = (  # translation_key → Symbol und Stelle, wenn der Hinweis anliegt
    ("water_tank_empty", "mdi:water-alert", (66, 245)),  # Griff des Wassertanks links
    ("bean_container_empty", "mdi:seed-off", (330, 24)),  # Oberkante der Glasfront (Bohnen sitzen oben)
    ("drip_tray_full", "mdi:tray-alert", (330, 458)),  # Tropfgitter
    ("keep_milk_tank_cool", "mdi:cup-off", (398, 300)),  # rechts am Auslauf
    ("device_should_be_descaled", "mdi:water-off", (484, 50)),  # Bildschirm oben rechts
    ("device_descaling_overdue", "mdi:water-off", (484, 50)),
    ("device_descaling_blockage", "mdi:water-off", (484, 50)),
    ("device_should_be_calc_n_cleaned", "mdi:spray-bottle", (176, 50)),  # Bildschirm oben links
    ("device_calc_n_clean_overdue", "mdi:spray-bottle", (176, 50)),
    ("device_calc_n_clean_blockage", "mdi:spray-bottle", (176, 50)),
    ("device_should_be_cleaned", "mdi:broom", (176, 192)),  # Bildschirm unten links
    ("device_cleaning_overdue", "mdi:broom", (176, 192)),
)
EINSCHALTER_STELLE = (150, 220)  # die Ein/Aus-Taste der Tastenleiste
TASSE_STELLE = (330, 392)  # das Glas unter dem Auslauf
ANZEIGE_STELLE = (330, 64)  # Kopfzeile des Bildschirms: dort steht das Programm wie an der Maschine
SYMBOLGROESSE = "clamp(14px, 2vw, 32px)"
SCHRIFTGROESSE = "clamp(11px, 1.8vw, 28px)"
ANZEIGE_SCHRIFT = "clamp(7px, 1.1vw, 15px)"
# Leeres Grundbild der Karte (gibt nur das Seitenverhältnis vor – sonst schiene es unter dem Zustandsbild durch)
LEERES_BILD = "data:image/svg+xml;base64," + base64.b64encode(
    b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1200 500' width='1200' height='500'/>"
).decode()
TIPPZIEL = "data:image/svg+xml;base64," + base64.b64encode(
    b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10' width='10' height='10'/>"
).decode()


def bild_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode()


def _stern(cx: float, cy: float, aussen: float, innen: float) -> str:
    punkte = []
    for k in range(10):
        r = aussen if k % 2 == 0 else innen
        winkel = math.radians(-90 + k * 36)
        punkte.append(f"{cx + r * math.cos(winkel):.1f},{cy + r * math.sin(winkel):.1f}")
    return " ".join(punkte)


def _tastenleiste(an: bool) -> str:
    """Die Tastenleiste unter dem Bildschirm, wie an der Maschine: Ein/Aus, Barista/Comfort, Tassen, Stern, Pflege, Einstellungen."""
    farbe = "#cfd8dc" if an else "#4a5057"
    linie = f'fill="none" stroke="{farbe}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"'
    zacken = "".join(
        f'<path d="M{530 + 6.5 * math.cos(math.radians(w)):.1f} {220 + 6.5 * math.sin(math.radians(w)):.1f} '
        f'L{530 + 9 * math.cos(math.radians(w)):.1f} {220 + 9 * math.sin(math.radians(w)):.1f}" {linie}/>'
        for w in range(0, 360, 45)
    )
    return (
        # Ein/Aus (das Dashboard legt die tippbare Taste darüber)
        f'<path d="M144.3 214.3 a8 8 0 1 0 11.4 0" {linie}/><path d="M150 211 v8" {linie}/>'
        # Barista / Comfort
        f'<text x="235" y="217" font-size="8" fill="{farbe}" font-family="{SCHRIFT}" text-anchor="middle">Barista</text>'
        f'<path d="M223 219.5 h24" stroke="{farbe}" stroke-width=".8"/>'
        f'<text x="235" y="228" font-size="8" fill="{farbe}" font-family="{SCHRIFT}" text-anchor="middle">Comfort</text>'
        # zwei Tassen
        f'<path d="M311 214 h9 v8 a4.5 4.5 0 0 1 -9 0 z M320 216 h3 a2.5 2.5 0 0 1 0 5 h-3" {linie}/>'
        f'<path d="M323 212 h9 v8 a4.5 4.5 0 0 1 -9 0 z M332 214 h3 a2.5 2.5 0 0 1 0 5 h-3" {linie}/>'
        # Stern (Lieblingsgetränke)
        f'<polygon points="{_stern(395, 220, 7.5, 3.2)}" {linie}/>'
        # Pflege (Tropfen mit Funkeln)
        f'<path d="M459 211 q6 8 6 11.5 a6 6 0 0 1 -12 0 q0 -3.5 6 -11.5 z" {linie}/>'
        f'<path d="M469 210 l1.3 2.7 l2.7 1.3 l-2.7 1.3 l-1.3 2.7 l-1.3 -2.7 l-2.7 -1.3 l2.7 -1.3 z" fill="{farbe}"/>'
        # Einstellungen (Zahnrad)
        f'<circle cx="530" cy="220" r="6.5" {linie}/><circle cx="530" cy="220" r="2" fill="{farbe}"/>{zacken}'
    )


def _display(variante: str) -> str:
    """Der Bildschirm (x 180–480, y 48–193) im Stil der Maschine: blauer Kopf, Tasse auf heller Theke, Start-Taste.

    Die Kopfzeile bleibt frei – dort legt das Dashboard das Programm als Beschriftung hinein.
    """
    text = f'font-family="{SCHRIFT}" text-anchor="middle" font-weight="600"'
    if variante == "aus":
        return (
            '<rect x="180" y="48" width="300" height="145" rx="4" fill="#06080c"/>'
            '<path d="M190 56 h70 l-44 129 h-26 z" fill="#ffffff" opacity=".03"/>'
        )
    kopf = {"achtung": "url(#ui_amber)", "fehler": "url(#ui_rot)"}.get(variante, "url(#ui_blau)")
    knopf, knopf_farbe = {
        "brueht": ("Stopp", "#c62828"),
        "achtung": ("OK", "#1a6fd0"),
        "fehler": ("OK", "#c62828"),
    }.get(variante, ("Start", "#1a6fd0"))
    teile = [
        f'<rect x="180" y="48" width="300" height="88" fill="{kopf}"/>',
        '<rect x="180" y="136" width="300" height="57" fill="url(#ui_theke)"/>',
        '<rect x="438" y="55" width="34" height="11" rx="2" fill="#e3f2fd" opacity=".92"/>',
        f'<text x="455" y="63.5" font-size="7.5" fill="#1c4fb1" {text}>Barista</text>',
    ]
    if variante in ("bereit", "brueht", "fertig"):
        teile.append(
            '<ellipse cx="300" cy="150" rx="36" ry="8" fill="#f7f7f7" stroke="#cfd8dc" stroke-width="1"/>'
            '<path d="M274 112 h52 v24 a26 14 0 0 1 -52 0 z" fill="#fafafa" stroke="#cfd8dc" stroke-width="1"/>'
            '<path d="M326 118 q16 0 16 10 q0 10 -16 10" fill="none" stroke="#fafafa" stroke-width="5"/>'
            '<path d="M326 118 q16 0 16 10 q0 10 -16 10" fill="none" stroke="#cfd8dc" stroke-width="1"/>'
            '<ellipse cx="300" cy="112" rx="26" ry="7" fill="#ffffff" stroke="#cfd8dc" stroke-width="1"/>'
            '<ellipse cx="300" cy="112" rx="21" ry="5" fill="#c58a4a"/>'
        )
    if variante == "brueht":
        teile.append('<rect x="186" y="168" width="288" height="5" rx="2.5" fill="#c9d3de"/>')
        teile.append(
            '<rect x="186" y="168" width="0" height="5" rx="2.5" fill="#1a6fd0">'
            f'<animate attributeName="width" values="0;288" dur="{FUELLDAUER}" repeatCount="indefinite"/></rect>'
        )
    if variante == "fertig":
        teile.append(
            '<circle cx="432" cy="104" r="16" fill="#2e7d32"/>'
            '<path d="M423 104 l6 6 l12 -13" fill="none" stroke="#ffffff" stroke-width="3.5" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
        )
    if variante == "achtung":
        teile.append(
            '<g><path d="M300 70 L330 122 L270 122 z" fill="#ffd54f" stroke="#6d4c00" stroke-width="3" '
            'stroke-linejoin="round"/>'
            f'<text x="300" y="115" font-size="30" fill="#6d4c00" {text}>!</text>'
            '<animate attributeName="opacity" values="1;.25;1" dur="1.4s" repeatCount="indefinite"/></g>'
        )
    if variante == "fehler":
        teile.append(
            '<path d="M284 78 l32 32 M316 78 l-32 32" stroke="#ffcdd2" stroke-width="6" stroke-linecap="round"/>'
        )
    teile.append('<rect x="180" y="176" width="300" height="17" fill="#e3e6ea"/>')
    teile.append(f'<text x="214" y="188" font-size="6.5" fill="#6b7682" font-family="{SCHRIFT}">Im Profil speichern</text>')
    teile.append('<path d="M248 178 v13 M290 178 v13" stroke="#cfd5db" stroke-width="1"/>')
    puls = (
        '<animate attributeName="opacity" values="1;.3;1" dur="2.4s" repeatCount="indefinite"/>'
        if variante == "bereit"
        else ""
    )
    teile.append(f'<rect x="400" y="176" width="80" height="17" fill="{knopf_farbe}">{puls}</rect>')
    teile.append(f'<text x="440" y="188" font-size="8" fill="#ffffff" {text}>{knopf}</text>')
    return "".join(teile)


def konrad_svg(variante: str, name: str = "Konrad") -> str:
    """Die Front des Vollautomaten als SVG, nach Fotos eines EQ900 plus gezeichnet – animiert je nach Zustand.

    Links die Maschine: schwarze Glasfront mit Bildschirm im Stil der Maschine, Tastenleiste
    und Schriftzug (dem Namen der Maschine), darunter der Edelstahlkorpus mit Auslauf,
    V-Blende, Glas und Tropfgitter, links der Griff des Wassertanks. Rechts Name,
    Statuszeile und Untertitel. Die Animationen (SMIL) laufen auch als <img>.
    """
    status, untertitel, farbe = BILD_VARIANTEN[variante]
    an = variante != "aus"
    brueht = variante == "brueht"
    dampf = variante in ("brueht", "fertig")
    teile = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {BILD_BREITE} {BILD_HOEHE}" '
        f'width="{BILD_BREITE}" height="{BILD_HOEHE}" role="img" aria-label="{name}: {status}">',
        "<defs>",
        '<linearGradient id="glas" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#2a2d33"/><stop offset=".45" stop-color="#0f1114"/>'
        '<stop offset="1" stop-color="#1a1d22"/></linearGradient>',
        '<linearGradient id="stahl_korpus" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#8d969e"/><stop offset=".15" stop-color="#c3cad0"/>'
        '<stop offset=".5" stop-color="#9aa3ab"/><stop offset=".85" stop-color="#c3cad0"/>'
        '<stop offset="1" stop-color="#8d969e"/></linearGradient>',
        '<linearGradient id="stahl_blende" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#e3e8ec"/><stop offset="1" stop-color="#aab3bb"/></linearGradient>',
        '<linearGradient id="stahl_schale" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#d3dadf"/><stop offset=".6" stop-color="#9ea7ae"/>'
        '<stop offset="1" stop-color="#7f888f"/></linearGradient>',
        '<linearGradient id="auslauf" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#2b2f35"/><stop offset="1" stop-color="#0d0f12"/></linearGradient>',
        '<radialGradient id="duese" cx=".4" cy=".35" r=".7">'
        '<stop offset="0" stop-color="#f1f4f6"/><stop offset=".6" stop-color="#9aa4ac"/>'
        '<stop offset="1" stop-color="#5c656c"/></radialGradient>',
        '<linearGradient id="ui_blau" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#1b4fb3"/><stop offset="1" stop-color="#2f86e3"/></linearGradient>',
        '<linearGradient id="ui_amber" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#a65f00"/><stop offset="1" stop-color="#e0a200"/></linearGradient>',
        '<linearGradient id="ui_rot" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#8e1b1b"/><stop offset="1" stop-color="#d32f2f"/></linearGradient>',
        '<linearGradient id="ui_theke" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#f4f6f8"/><stop offset="1" stop-color="#d6dbe0"/></linearGradient>',
        '<linearGradient id="chrom" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#f5f7f9"/><stop offset=".5" stop-color="#b8c0c7"/>'
        '<stop offset="1" stop-color="#e3e7ea"/></linearGradient>',
        '<clipPath id="glasrand"><path d="M296 352 h68 l-6 60 h-56 z"/></clipPath>',
        "</defs>",
        # Schatten, Griff des Wassertanks, Edelstahlkorpus mit V-Blende
        '<ellipse cx="330" cy="484" rx="250" ry="11" fill="#000" opacity=".22"/>',
        '<rect x="54" y="150" width="24" height="190" rx="8" fill="#23272d" stroke="#4a5057" stroke-width="1"/>',
        '<rect x="118" y="250" width="424" height="226" rx="12" fill="url(#stahl_korpus)" stroke="#6f777e" stroke-width="1"/>',
        '<path d="M268 318 h124 l94 152 h-312 z" fill="url(#stahl_blende)" opacity=".85"/>',
        # Tropfschale mit Gitter
        '<rect x="104" y="436" width="452" height="42" rx="10" fill="url(#stahl_schale)" stroke="#6f777e" stroke-width="1"/>',
    ]
    for i in range(7):
        breite = 300 + i * 10
        teile.append(
            f'<rect x="{330 - breite / 2:.0f}" y="{441 + i * 4.6:.1f}" width="{breite}" height="2.4" rx="1.2" fill="#1b1e22"/>'
        )
    # Glasfront mit Bildschirm, Tastenleiste und Schriftzug
    teile.append('<rect x="70" y="22" width="520" height="240" rx="10" fill="url(#glas)" stroke="#4a5057" stroke-width="2"/>')
    teile.append('<path d="M80 30 h200 l-120 224 h-80 z" fill="#ffffff" opacity=".035"/>')
    teile.append('<path d="M82 26 h496" stroke="#ffffff" stroke-width="1.2" opacity=".18"/>')
    teile.append(_display(variante))
    teile.append('<rect x="180" y="48" width="300" height="145" rx="4" fill="none" stroke="#2a2e34" stroke-width="1.5"/>')
    teile.append(_tastenleiste(an))
    teile.append(
        f'<text x="330" y="252" font-size="19" font-weight="700" letter-spacing="4" fill="url(#chrom)" '
        f'font-family="{SCHRIFT}" text-anchor="middle">{name.upper()}</text>'
    )
    # Auslauf mit zwei Düsen
    teile.append('<rect x="270" y="262" width="120" height="54" rx="6" fill="url(#auslauf)" stroke="#07080a"/>')
    teile.append('<rect x="278" y="268" width="104" height="4" rx="2" fill="#3a3f46" opacity=".8"/>')
    for x in (307, 353):
        teile.append(f'<circle cx="{x}" cy="322" r="10" fill="url(#duese)"/><circle cx="{x}" cy="322" r="4" fill="#1b1e22"/>')
    # Glas: Inhalt (geclippt), dann das Glas selbst
    if brueht:
        teile.append(
            '<g clip-path="url(#glasrand)"><rect x="290" y="412" width="80" height="0" fill="#5d3a1a">'
            f'<animate attributeName="y" values="412;358" dur="{FUELLDAUER}" repeatCount="indefinite"/>'
            f'<animate attributeName="height" values="0;54" dur="{FUELLDAUER}" repeatCount="indefinite"/>'
            "</rect></g>"
        )
    elif variante == "fertig":
        teile.append(
            '<g clip-path="url(#glasrand)"><rect x="290" y="358" width="80" height="54" fill="#5d3a1a"/>'
            '<rect x="290" y="358" width="80" height="9" fill="#c8a27a"/></g>'
        )
    teile.append(
        '<path d="M296 352 h68 l-6 60 h-56 z" fill="#ffffff" fill-opacity=".16" stroke="#eceff1" '
        'stroke-width="3" stroke-linejoin="round"/>'
    )
    teile.append(
        '<path d="M363 366 q22 2 20 18 q-2 14 -22 14" fill="none" stroke="#eceff1" stroke-width="6" stroke-linecap="round"/>'
    )
    # Kaffeestrahlen
    if brueht:
        for x in (307, 353):
            teile.append(
                f'<line x1="{x}" y1="334" x2="{x}" y2="360" stroke="#5d3a1a" stroke-width="5" stroke-linecap="round" '
                'stroke-dasharray="7 5"><animate attributeName="stroke-dashoffset" from="0" to="-24" dur=".45s" '
                'repeatCount="indefinite"/></line>'
            )
    # Dampf
    if dampf:
        for i, x in enumerate((318, 330, 342)):
            teile.append(
                f'<path d="M{x} 346 c-7 -9 7 -15 0 -26" fill="none" stroke="#cfd8dc" stroke-width="4" '
                f'stroke-linecap="round" opacity="0">'
                f'<animateTransform attributeName="transform" type="translate" values="0 0;0 -30" dur="2.6s" '
                f'begin="{i * 0.9:.1f}s" repeatCount="indefinite"/>'
                f'<animate attributeName="opacity" values="0;.85;0" dur="2.6s" begin="{i * 0.9:.1f}s" '
                'repeatCount="indefinite"/></path>'
            )
    # Rechts: Name, Status, Untertitel
    teile.append(
        f'<text x="640" y="160" font-size="42" font-weight="600" fill="#8d6e63" font-family="{SCHRIFT}">{name}</text>'
    )
    teile.append(
        f'<text x="636" y="248" font-size="78" font-weight="700" fill="{farbe}" font-family="{SCHRIFT}">{status}</text>'
    )
    teile.append(f'<text x="640" y="302" font-size="32" fill="#90a4ae" font-family="{SCHRIFT}">{untertitel}</text>')
    teile.append("</svg>")
    return "\n".join(teile) + "\n"


def stelle(x: float, y: float) -> dict[str, str]:
    """Die Position eines Elements in Prozent der Zeichnung."""
    return {"left": f"{x / BILD_BREITE * 100:.1f}%", "top": f"{y / BILD_HOEHE * 100:.1f}%"}


def nicht_aus(entity_id: str) -> list[dict[str, str]]:
    """Bedingung: Der Hinweis liegt an (nicht „off“, nicht unbekannt)."""
    return [{"entity": entity_id, "state_not": zustand} for zustand in ("off", "unknown", "unavailable")]


def kopfkarte(
    maschine_name: str, maschine: dict[str, str], weitere: dict[str, tuple[str, str]]
) -> Optional[dict[str, Any]]:
    """Die Bildkarte für den Kopf des Dashboards: Zeichnung je Betriebszustand mit Tippzielen und Hinweisen.

    Ohne Betriebszustand gibt es keine Zeichnung (None). Die Karte gehört in einen eigenen
    Abschnitt über alle Spalten – als ``header`` der Ansicht bekäme sie nur eine Spalte.
    """
    zustand = maschine.get("zustand")
    if not zustand:
        return None
    bilder = {variante: bild_uri(konrad_svg(variante, maschine_name)) for variante in BILD_VARIANTEN}
    elemente: list[dict[str, Any]] = [
        {
            "type": "image",
            "entity": zustand,
            "image": bilder["aus"],
            "state_image": {z: bilder[v] for z, v in ZUSTAND_BILDER.items()},
            "filter": "none",
            "state_filter": {"unavailable": UNBEKANNT_FILTER, "unknown": UNBEKANNT_FILTER},
            "aspect_ratio": "12x5",
            "style": {"left": "50%", "top": "50%", "width": "100%"},
        },
        {
            "type": "icon",
            "icon": "mdi:power",
            "entity": maschine["einschalter"],
            "title": "Ein / Aus",
            "tap_action": {"action": "toggle"},
            "style": {**stelle(*EINSCHALTER_STELLE), "color": "#eceff1", "--mdc-icon-size": SYMBOLGROESSE},
        },
        {
            "type": "image",
            "image": TIPPZIEL,
            "title": "Jetzt zubereiten",
            "tap_action": {"action": "perform-action", "perform_action": f"script.{SKRIPT_ZUBEREITEN}", "confirmation": TASSE},
            "style": {**stelle(*TASSE_STELLE), "width": "8%"},
        },
    ]
    laeuft = [{"entity": zustand, "state": "run"}]
    anzeige = {  # Beschriftung in der Kopfzeile des Bildschirms
        **stelle(*ANZEIGE_STELLE),
        "transform": "translate(-50%, -50%)",
        "color": "#ffffff",
        "font-size": ANZEIGE_SCHRIFT,
        "font-weight": "600",
        "max-width": "17%",
        "white-space": "nowrap",
        "overflow": "hidden",
        "text-overflow": "ellipsis",
    }
    # Wie an der Maschine steht das Programm im Bildschirmkopf: das laufende, sonst das ausgewählte.
    if maschine.get("aktiv"):
        elemente.append(
            {
                "type": "conditional",
                "conditions": laeuft,
                "elements": [{"type": "state-label", "entity": maschine["aktiv"], "style": anzeige}],
            }
        )
    if maschine.get("gewaehlt"):
        elemente.append(
            {
                "type": "conditional",
                "conditions": [
                    {"entity": zustand, "state_not": z} for z in ("run", "inactive", "unknown", "unavailable")
                ]
                + [{"entity": maschine["gewaehlt"], "state_not": z} for z in ("unknown", "unavailable")],
                "elements": [{"type": "state-label", "entity": maschine["gewaehlt"], "style": anzeige}],
            }
        )
    if maschine.get("fortschritt"):
        elemente.append(
            {
                "type": "conditional",
                "conditions": laeuft,
                "elements": [
                    {
                        "type": "state-label",
                        "entity": maschine["fortschritt"],
                        "prefix": "Fortschritt ",
                        "style": {
                            **stelle(640, 372),
                            "transform": "translate(0, -50%)",
                            "color": "#90a4ae",
                            "font-size": SCHRIFTGROESSE,
                        },
                    }
                ],
            }
        )
    for schluessel, symbol, (x, y) in MARKIERUNGEN:
        if schluessel not in weitere:
            continue
        entity_id, name = weitere[schluessel]
        elemente.append(
            {
                "type": "conditional",
                "conditions": nicht_aus(entity_id),
                "elements": [
                    {
                        "type": "icon",
                        "icon": symbol,
                        "entity": entity_id,
                        "title": name,
                        "tap_action": {"action": "more-info"},
                        "style": {
                            **stelle(x, y),
                            "color": "#ffffff",
                            "background": "#d32f2f",
                            "border-radius": "50%",
                            "padding": "3px",
                            "--mdc-icon-size": SYMBOLGROESSE,
                        },
                    }
                ],
            }
        )
    return {
        "type": "picture-elements",
        "image": LEERES_BILD,
        "elements": elemente,
        "grid_options": {"columns": "full", "rows": "auto"},
    }


def ohne_bilder(objekt: Any) -> Any:
    """Für die Ausgabe: eingebettete Bilder auf ihre Länge kürzen."""
    if isinstance(objekt, dict):
        return {k: ohne_bilder(v) for k, v in objekt.items()}
    if isinstance(objekt, list):
        return [ohne_bilder(v) for v in objekt]
    if isinstance(objekt, str) and objekt.startswith("data:image/svg+xml;base64,"):
        return f"data:image/svg+xml;base64,… ({len(objekt)} Zeichen)"
    return objekt


def kachel(entity_id: str, name: str, icon: str, farbe: str, **mehr: Any) -> dict[str, Any]:
    return {"type": "tile", "entity": entity_id, "name": name, "icon": icon, "color": farbe, **mehr}


def wenn_verfuegbar(entity_id: str) -> list[dict[str, Any]]:
    """Sichtbarkeit: nur, wenn die Entität nicht „nicht verfügbar“ meldet (Optionen, die das Getränk nicht hat)."""
    return [{"condition": "state", "entity": entity_id, "state_not": "unavailable"}]


def wenn_anliegend(entity_id: str) -> list[dict[str, Any]]:
    """Sichtbarkeit: nur, wenn ein Hinweis anliegt (nicht aus, nicht unbekannt)."""
    return [{"condition": "state", "entity": entity_id, "state_not": ["off", "unknown", "unavailable"]}]


def skript_taste(skript: str, platz: str, rueckfrage: dict[str, str]) -> dict[str, Any]:
    return {
        "action": "perform-action",
        "perform_action": f"script.{skript}",
        "data": {"platz": platz},
        "confirmation": rueckfrage,
    }


def dashboard_konfiguration(
    maschine_name: str,
    maschine: dict[str, str],
    helfer: dict[str, str],
    plaetze_je_person: dict[str, dict[str, dict[str, str]]],
    weitere: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    """Das Dashboard: Lieblingsgetränke und Getränk zusammenstellen, die Maschine direkt, Wartung und Zähler.

    Ganz oben, über alle Spalten, die Zeichnung der Maschine, animiert je Betriebszustand (siehe ``kopfkarte``).
    Darunter drei etwa gleich hohe Abschnitte, damit auf breiten Bildschirmen keine Leerflächen
    entstehen (die Sections-Ansicht legt Abschnitte in Reihen, jede so hoch wie ihr längster Abschnitt).
    ``weitere`` ordnet translation_keys weiterer Entitäten der Maschine (Wartungshinweise,
    Zähler) ihre Entitäts-ID und ihren Namen zu.
    """
    abschnitte: list[dict[str, Any]] = []

    karten: list[dict[str, Any]] = []
    if plaetze_je_person:
        karten.append({"type": "heading", "heading": "Lieblingsgetränke", "heading_style": "title", "icon": "mdi:star"})
    for person, plaetze in plaetze_je_person.items():
        karten.append({"type": "heading", "heading": person, "heading_style": "subtitle", "icon": "mdi:account-circle"})
        for kennung, daten in plaetze.items():
            karten.append(
                kachel(
                    daten["name"],
                    daten["label"],
                    "mdi:coffee",
                    "amber",
                    vertical=True,
                    state_content="state",
                    tap_action=skript_taste(SKRIPT_LIEBLING, kennung, TASSE),
                    icon_tap_action=skript_taste(SKRIPT_LIEBLING, kennung, TASSE),
                    hold_action=skript_taste(
                        SKRIPT_SPEICHERN,
                        kennung,
                        {"text": f"Die aktuelle Zusammenstellung auf „{daten['label']}“ speichern?"},
                    ),
                    grid_options={"columns": 4, "rows": 2},
                )
            )
    if plaetze_je_person:
        karten.append(
            {
                "type": "markdown",
                "content": (
                    "Tippen bereitet das Getränk zu. Gedrückt halten speichert die aktuelle "
                    "Zusammenstellung auf dem Platz."
                ),
                "grid_options": {"columns": "full"},
            }
        )

    zusammenstellen = karten  # im selben Abschnitt weiter: Getränk zusammenstellen
    zusammenstellen.append(
        {
            "type": "heading",
            "heading": "Getränk zusammenstellen",
            "heading_style": "title",
            "icon": "mdi:coffee-maker",
        }
    )
    if helfer.get("getraenk"):
        zusammenstellen.append(
            kachel(
                helfer["getraenk"],
                "Getränk",
                "mdi:coffee",
                "brown",
                features=[{"type": "select-options"}],
                grid_options={"columns": "full"},
            )
        )
    for kurz, name, _, _, symbol, farbe in OPTIONEN:
        if helfer.get(kurz):
            zusammenstellen.append(kachel(helfer[kurz], name, symbol, farbe, features=[{"type": "select-options"}]))
    if helfer.get("menge"):
        zusammenstellen.append(
            kachel(
                helfer["menge"],
                "Menge",
                "mdi:cup-water",
                "blue",
                features=[{"type": "numeric-input", "style": "slider"}],
                grid_options={"columns": "full"},
            )
        )
    if helfer.get("doppelt"):
        zusammenstellen.append(
            kachel(
                helfer["doppelt"],
                "Doppelt (zwei Tassen)",
                "mdi:numeric-2-box-multiple",
                "amber",
                features=[{"type": "toggle"}],
            )
        )
    zusammenstellen.append(
        {
            "type": "button",
            "name": "Jetzt zubereiten",
            "icon": "mdi:coffee",
            "icon_height": "56px",
            "show_state": False,
            "tap_action": {
                "action": "perform-action",
                "perform_action": f"script.{SKRIPT_ZUBEREITEN}",
                "confirmation": TASSE,
            },
            "grid_options": {"columns": "full", "rows": 2},
        }
    )
    zusammenstellen.append(
        {
            "type": "markdown",
            "content": (
                f"„{WIE_MASCHINE}“ und Menge 0 lassen die Einstellung der Maschine unangetastet. "
                "Was ein Getränk nicht anbietet, wird übersprungen."
            ),
            "grid_options": {"columns": "full"},
        }
    )
    abschnitte.append({"type": "grid", "cards": zusammenstellen})

    direkt: list[dict[str, Any]] = [
        {"type": "heading", "heading": maschine_name, "heading_style": "title", "icon": "mdi:coffee-maker-outline"}
    ]
    direkt.append(kachel(maschine["einschalter"], "Ein / Aus", "mdi:power", "green", features=[{"type": "toggle"}]))
    if maschine.get("kindersicherung"):
        direkt.append(
            kachel(maschine["kindersicherung"], "Kindersicherung", "mdi:lock", "grey", features=[{"type": "toggle"}])
        )
    if maschine.get("tassenwaermer"):
        direkt.append(
            kachel(
                maschine["tassenwaermer"],
                "Tassenwärmer",
                "mdi:heat-wave",
                "orange",
                features=[{"type": "toggle"}],
                visibility=wenn_verfuegbar(maschine["tassenwaermer"]),
            )
        )
    if maschine.get("zustand"):
        direkt.append(kachel(maschine["zustand"], "Zustand", "mdi:state-machine", "brown"))
    if maschine.get("fernstart"):
        direkt.append(kachel(maschine["fernstart"], "Fernstart erlaubt", "mdi:remote", "teal"))
    laufend = (
        [{"condition": "state", "entity": maschine["zustand"], "state": LAUFEND}] if maschine.get("zustand") else []
    )
    if maschine.get("fortschritt"):
        direkt.append(
            {
                "type": "gauge",
                "entity": maschine["fortschritt"],
                "name": "Fortschritt",
                "min": 0,
                "max": 100,
                "severity": {"green": 0, "yellow": 50, "red": 90},
                "visibility": laufend,
                "grid_options": {"columns": 6, "rows": 3},
            }
        )
    if maschine.get("endzeit"):
        direkt.append(kachel(maschine["endzeit"], "Fertig um", "mdi:clock-end", "brown", visibility=laufend))
    if maschine.get("stopp"):
        direkt.append(
            {
                "type": "button",
                "name": "Abbrechen",
                "icon": "mdi:stop-circle",
                "show_state": False,
                "tap_action": {
                    "action": "perform-action",
                    "perform_action": "button.press",
                    "target": {"entity_id": maschine["stopp"]},
                },
                "visibility": laufend,
            }
        )
    if maschine.get("aktiv"):
        direkt.append(
            {
                "type": "markdown",
                "content": (
                    "**Direkt an der Maschine.** Ein Getränk unter „Sofort starten“ startet ohne Rückfrage – "
                    "Tasse hinstellen! Die Optionen darunter gehören zum ausgewählten Programm."
                ),
                "grid_options": {"columns": "full"},
            }
        )
        direkt.append(
            kachel(
                maschine["aktiv"],
                "Sofort starten",
                "mdi:play-circle",
                "red",
                features=[{"type": "select-options"}],
                grid_options={"columns": "full"},
            )
        )
    if maschine.get("gewaehlt"):
        direkt.append(
            kachel(
                maschine["gewaehlt"],
                "Ausgewähltes Programm",
                "mdi:format-list-bulleted",
                "brown",
                features=[{"type": "select-options"}],
                grid_options={"columns": "full"},
            )
        )
    # Optionen gibt es nur für das gewählte Getränk – was die Maschine nicht anbietet, bleibt unsichtbar.
    for kurz, name, _, _, symbol, farbe in OPTIONEN:
        if maschine.get(kurz):
            direkt.append(
                kachel(
                    maschine[kurz],
                    name,
                    symbol,
                    farbe,
                    features=[{"type": "select-options"}],
                    visibility=wenn_verfuegbar(maschine[kurz]),
                )
            )
    if maschine.get("menge"):
        direkt.append(
            kachel(
                maschine["menge"],
                "Füllmenge",
                "mdi:cup-water",
                "blue",
                features=[{"type": "numeric-input", "style": "slider"}],
                grid_options={"columns": "full"},
                visibility=wenn_verfuegbar(maschine["menge"]),
            )
        )
    if maschine.get("doppelt"):
        direkt.append(
            kachel(
                maschine["doppelt"],
                "Mehrere Getränke",
                "mdi:numeric-2-box-multiple",
                "amber",
                features=[{"type": "toggle"}],
                visibility=wenn_verfuegbar(maschine["doppelt"]),
            )
        )
    abschnitte.append({"type": "grid", "cards": direkt})

    # Wartung und Zähler in einem Abschnitt; Hinweise bis auf Wassertank, Bohnen und Tropfschale nur, wenn sie anliegen.
    wartung: list[dict[str, Any]] = []
    zaehler: list[dict[str, Any]] = []
    for schluessel, (entity_id, name) in sorted(weitere.items()):
        if ZAEHLER.search(schluessel):
            zaehler.append(kachel(entity_id, name, "mdi:counter", "brown"))
        elif WARTUNG.search(schluessel):
            symbol, farbe = next(
                ((s, f) for teil, s, f in WARTUNG_SYMBOLE if teil in schluessel), ("mdi:alert-circle", "orange")
            )
            if schluessel in WARTUNG_IMMER:
                wartung.append(kachel(entity_id, name, symbol, farbe))
            else:
                wartung.append(kachel(entity_id, name, symbol, farbe, visibility=wenn_anliegend(entity_id)))
    pflege: list[dict[str, Any]] = []
    if wartung:
        pflege.append({"type": "heading", "heading": "Wartung", "heading_style": "title", "icon": "mdi:wrench"})
        pflege.extend(wartung)
    if zaehler:
        pflege.append({"type": "heading", "heading": "Zähler", "heading_style": "title", "icon": "mdi:counter"})
        pflege.extend(zaehler)
    if pflege:
        abschnitte.append({"type": "grid", "cards": pflege})

    badges: list[dict[str, Any]] = []
    if maschine.get("zustand"):
        badges.append(
            {
                "type": "entity",
                "entity": maschine["zustand"],
                "name": maschine_name,
                "show_name": True,
                "show_state": True,
                "show_icon": True,
                "color": "brown",
            }
        )
    badges.append(
        {
            "type": "entity",
            "entity": maschine["einschalter"],
            "name": "Strom",
            "show_name": True,
            "show_state": True,
            "show_icon": True,
            "color": "green",
        }
    )
    for schluessel, (entity_id, name) in sorted(weitere.items()):
        if WARTUNG.search(schluessel) and not ZAEHLER.search(schluessel):
            badges.append(
                {
                    "type": "entity",
                    "entity": entity_id,
                    "name": name,
                    "show_name": True,
                    "show_state": False,
                    "show_icon": True,
                    "color": "red",
                    "visibility": [
                        {"condition": "state", "entity": entity_id, "state_not": ["off", "unknown", "unavailable"]}
                    ],
                }
            )

    ansicht: dict[str, Any] = {
        "title": maschine_name,
        "path": "kaffee",
        "icon": "mdi:coffee",
        "type": "sections",
        "max_columns": 3,
        "badges": badges,
        "sections": abschnitte,
    }
    kopf = kopfkarte(maschine_name, maschine, weitere)
    if kopf:
        abschnitte.insert(0, {"type": "grid", "column_span": 3, "cards": [kopf]})
    return {"title": maschine_name, "views": [ansicht]}


def zustaende_laden(ha: HomeAssistant, zustaende: dict[str, dict[str, Any]]) -> None:
    zustaende.clear()
    zustaende.update({z["entity_id"]: z for z in ha.get_json("/api/states")})


def maschine_vorbereiten(
    ha: HomeAssistant, maschine: dict[str, str], zustaende: dict[str, dict[str, Any]], geraete_id: str
) -> bool:
    """Schaltet die Maschine ein und wählt ein Milchgetränk aus (ohne zu starten).

    Erst dann kennt Home Assistant die Getränkeliste und alle Optionsentitäten.
    Gibt True zurück, wenn die Maschine dafür eingeschaltet wurde.
    """
    eingeschaltet = False
    einschalter = maschine["einschalter"]
    aktiv = maschine["aktiv"]
    if (zustaende.get(einschalter) or {}).get("state") != "on":
        print("  Die Kaffeemaschine wird eingeschaltet …")
        ha.post_json("/api/services/switch/turn_on", {"entity_id": einschalter})
        eingeschaltet = True
    optionen: list[str] = []
    for _ in range(VORBEREITUNG_SEKUNDEN):
        zustand = ha.get_json(f"/api/states/{aktiv}")
        optionen = [o for o in (zustand.get("attributes") or {}).get("options") or [] if isinstance(o, str)]
        bereit = (
            not maschine.get("zustand")
            or ha.get_json(f"/api/states/{maschine['zustand']}").get("state") == "ready"
        )
        if optionen and bereit:
            break
        time.sleep(1)
    else:
        raise Abbruch(
            f"Die Kaffeemaschine meldet nach {VORBEREITUNG_SEKUNDEN} Sekunden keine Getränkeliste – "
            "ist sie am Strom, im WLAN und in Home Connect verbunden?"
        )
    milch = next((o for o in optionen if o.endswith("cappuccino")), optionen[0])
    print(f"  „{getraenk_name(milch)}“ wird ausgewählt (nicht gestartet), damit alle Optionen sichtbar werden …")
    ha.post_json(
        "/api/services/home_connect/set_program_and_options",
        {"device_id": geraete_id, "affects_to": "selected_program", "program": milch},
    )
    time.sleep(VORBEREITUNG_PAUSE)
    return eingeschaltet


def wartung_aktivieren(ha: HomeAssistant, entity_ids: list[str], zustaende: dict[str, dict[str, Any]]) -> None:
    """Aktiviert deaktivierte Wartungshinweise und Zähler; ein Hinweis ohne Wert ist kein Fehler."""
    for entity_id in entity_ids:
        entitaet_aktivieren(ha, entity_id)
    print(f"✓ {len(entity_ids)} Wartungshinweise/Zähler aktiviert: " + ", ".join(entity_ids))
    try:
        auf_zustand_warten(ha, entity_ids[0])
    except Abbruch as fehler:
        print(f"⚠ {fehler}")
    zustaende_laden(ha, zustaende)


def getraenke_einrichten(
    ha: HomeAssistant,
    args: argparse.Namespace,
    zustaende: dict[str, dict[str, Any]],
    kaffeemaschine: str,
    interaktiv: bool,
) -> int:
    """Legt Helfer, Skripte und das Dashboard für die Kaffeemaschine an."""
    if not args.personen and interaktiv:
        args.personen = input("Für wen sollen Lieblingsgetränke angelegt werden (Namen durch Komma getrennt)? ")
    personen = personen_lesen(args.personen)
    register = ha.register()
    geraet = kaffee_geraet(register, kaffeemaschine)
    if not geraet:
        raise Abbruch(
            f"{kaffeemaschine} gehört zu keinem Gerät – ist die Kaffeemaschine über Home Connect eingebunden?"
        )
    maschine_name = register.geraetename(geraet)
    eintraege = kaffee_entitaeten(register, kaffeemaschine)
    maschine = bedienbare(eintraege, kaffeemaschine)
    if not maschine.get("aktiv"):
        raise Abbruch(
            f"Am Gerät „{maschine_name}“ fehlt die Entität „Aktives Programm“ – das Dashboard braucht die "
            f"Home-Connect-Integration von Home Assistant ({HOME_CONNECT_ANLEITUNG})."
        )
    print(f"✓ Kaffeemaschine „{maschine_name}“: {len(eintraege)} Entitäten, davon {len(maschine)} fürs Dashboard")

    # Wartungshinweise (Wassertank leer, Entkalken …) sind ab Werk deaktiviert.
    deaktiviert = sorted(
        e["entity_id"]
        for schluessel, e in eintraege.items()
        if e.get("disabled_by") and (WARTUNG.search(schluessel) or ZAEHLER.search(schluessel))
    )
    if deaktiviert and not args.nur_anzeigen:
        wartung_aktivieren(ha, deaktiviert, zustaende)
        eintraege = kaffee_entitaeten(ha.register_aktualisieren(), kaffeemaschine)
        maschine = bedienbare(eintraege, kaffeemaschine)

    # Ohne eingeschaltete Maschine kennt Home Assistant weder Getränkeliste noch Optionen.
    optionen_da = all(maschine.get(k) for k in ("staerke", "menge"))
    getraenke_da = bool(((zustaende.get(maschine["aktiv"]) or {}).get("attributes") or {}).get("options"))
    eingeschaltet = False
    if not (optionen_da and getraenke_da) and not args.nur_anzeigen:
        frage = (
            "Home Assistant kennt noch nicht alle Getränke und Optionen der Maschine. Dafür muss sie kurz an "
            "sein und ein Getränk ausgewählt haben (gebrüht wird nichts)."
        )
        if not interaktiv:
            raise Abbruch(frage + " Bitte die Maschine einschalten und das Skript im Terminal erneut ausführen.")
        print(f"\n{frage}")
        if input("Kaffeemaschine jetzt einschalten und vorbereiten? [J/n]: ").strip().lower() in ("", "j", "ja", "y"):
            eingeschaltet = maschine_vorbereiten(ha, maschine, zustaende, geraet["id"])
            zustaende_laden(ha, zustaende)
            eintraege = kaffee_entitaeten(ha.register_aktualisieren(), kaffeemaschine)
            maschine = bedienbare(eintraege, kaffeemaschine)
            fehlt = [name for kurz, name, *_ in OPTIONEN if kurz not in NUR_WENN_VORHANDEN and not maschine.get(kurz)]
            if fehlt:
                print(f"⚠ Diese Optionen meldet die Maschine nicht: {', '.join(fehlt)} – das Dashboard lässt sie weg.")
        else:
            print("  Dann gilt vorerst die Standardliste der Getränke; Optionen, die fehlen, lässt das Dashboard weg.")
    getraenke = getraenke_liste(zustaende, maschine["aktiv"])
    weitere = {
        schluessel: (e["entity_id"], register.entitaetsname(e) or schluessel.replace("_", " ").capitalize())
        for schluessel, e in eintraege.items()
        if not e.get("disabled_by")
        and schluessel not in MASCHINEN_SCHLUESSEL
        and (WARTUNG.search(schluessel) or ZAEHLER.search(schluessel))
    }
    print(f"✓ {len(getraenke)} Getränke, {len(weitere)} Wartungshinweise und Zähler")

    # Helfer: die Namen stehen fest, die Entitäts-IDs vergibt Home Assistant.
    helfer_plan: list[tuple[str, str, str, dict[str, Any]]] = [
        (
            "getraenk",
            "input_select",
            f"{HELFER_PRAEFIX}: Getränk",
            {"options": [n for n, _ in getraenke], "icon": "mdi:coffee"},
        )
    ]
    for kurz, name, werte, _, symbol, _ in OPTIONEN:
        if kurz in NUR_WENN_VORHANDEN and not maschine.get(kurz):
            continue
        helfer_plan.append(
            (
                kurz,
                "input_select",
                f"{HELFER_PRAEFIX}: {name}",
                {"options": [WIE_MASCHINE] + [a for a, _ in werte], "icon": symbol},
            )
        )
    helfer_plan.append(
        (
            "menge",
            "input_number",
            f"{HELFER_PRAEFIX}: Menge",
            {"min": 0, "max": 500, "step": 5, "unit_of_measurement": "ml", "mode": "slider", "icon": "mdi:cup-water"},
        )
    )
    helfer_plan.append(("doppelt", "input_boolean", f"{HELFER_PRAEFIX}: Doppelt", {"icon": "mdi:numeric-2-box-multiple"}))
    plaetze_je_person: dict[str, dict[str, dict[str, str]]] = {}
    platz_plan: list[tuple[str, str, str, str, dict[str, Any]]] = []  # Kennung, Art, Domain, Name, Felder
    for person in personen:
        plaetze_je_person[person] = {}
        for nummer in range(1, args.plaetze + 1):
            kennung = f"{slug(person)}_{nummer}"
            plaetze_je_person[person][kennung] = {"label": f"{person} {nummer}"}
            name = f"Lieblingsgetränk {person} {nummer}"
            platz_plan.append((kennung, "name", "input_text", name, {"max": 255, "icon": "mdi:coffee", "initial": PLATZ_FREI}))
            platz_plan.append((kennung, "daten", "input_text", f"{name} Daten", {"max": 255, "icon": "mdi:code-json"}))

    def platz_eintragen(kennung: str, art: str, entity_id: str) -> None:
        for plaetze in plaetze_je_person.values():
            if kennung in plaetze:
                plaetze[kennung][art] = entity_id

    if args.nur_anzeigen:
        helfer = {kurz: f"{domain}.{slug(name)}" for kurz, domain, name, _ in helfer_plan}
        for kennung, art, domain, name, _ in platz_plan:
            platz_eintragen(kennung, art, f"{domain}.{slug(name)}")
    else:
        ws = WebSocket(ha.url, ha.token)
        try:
            helfer = {kurz: helfer_anlegen(ws, domain, name, **felder) for kurz, domain, name, felder in helfer_plan}
            for kennung, art, domain, name, felder in platz_plan:
                platz_eintragen(kennung, art, helfer_anlegen(ws, domain, name, **felder))
        finally:
            ws.schliessen()
        print(f"✓ {len(helfer_plan) + len(platz_plan)} Helfer angelegt bzw. aktualisiert")

    alle_plaetze = {kennung: daten for plaetze in plaetze_je_person.values() for kennung, daten in plaetze.items()}
    skripte = skript_konfigurationen(maschine, helfer, alle_plaetze, geraet["id"], getraenke)
    dashboard = dashboard_konfiguration(maschine_name, maschine, helfer, plaetze_je_person, weitere)

    if args.nur_anzeigen:
        print("\nDiese Skripte würden angelegt (nichts geändert):")
        print(json.dumps(skripte, indent=2, ensure_ascii=False))
        print("\nUnd dieses Dashboard (eingebettete Bilder gekürzt):")
        print(json.dumps(ohne_bilder(dashboard), indent=2, ensure_ascii=False))
        if args.auch_in and args.auch_in not in (AUCH_AUTO, AUCH_AUS):
            print(f"\nDie Ansicht käme außerdem als Reiter in: {args.auch_in}")
        return 0

    for kennung, config in skripte.items():
        skript_anlegen(ha, kennung, config)
    print("✓ 3 Skripte angelegt bzw. aktualisiert: " + ", ".join(f"script.{k}" for k in skripte))
    ws = WebSocket(ha.url, ha.token)
    try:
        gemerkt = gemerkte_dashboards(ws)
        ziele = auch_in_bestimmen(ws, args.auch_in, gemerkt, interaktiv)
        gelungen: list[str] = []
        for ziel in ziele:
            try:
                ergebnis = reiter_einfuegen(ws, ziel, dashboard["views"][0])
            except Abbruch as fehler:
                print(f"⚠ Kein Reiter im Dashboard „{ziel}“: {fehler}")
                continue
            gelungen.append(ziel)
            print(f"✓ Reiter „{maschine_name}“ im Dashboard „{ziel}“ {ergebnis}")
        for ziel in gemerkt:
            if ziel not in gelungen and reiter_entfernen(ws, ziel):
                print(f"✓ Reiter „{maschine_name}“ aus dem Dashboard „{ziel}“ entfernt")
        dashboard[WEITERE_DASHBOARDS] = gelungen  # beim nächsten Lauf werden diese Reiter mit erneuert
        neu = dashboard_anlegen(ws, DASHBOARD_URL, maschine_name, dashboard)
        print(f"✓ Dashboard „{maschine_name}“ {'angelegt' if neu else 'aktualisiert'}: {ha.url}/{DASHBOARD_URL}")
    finally:
        ws.schliessen()

    if eingeschaltet:
        ha.post_json("/api/services/switch/turn_off", {"entity_id": maschine["einschalter"]})
        print("✓ Die Kaffeemaschine ist wieder ausgeschaltet.")
    print(
        f"\nFertig. In der Seitenleiste von Home Assistant steht jetzt „{maschine_name}“ – mit "
        + ", ".join(f"{len(p)} Plätzen für {name}" for name, p in plaetze_je_person.items())
        + ".\n  Tippen auf einen Platz bereitet das Getränk zu, gedrückt halten speichert die Zusammenstellung."
        "\n  Tipp: Die drei Skripte unter Einstellungen → Sprachassistenten → Freigeben für Assist freigeben, "
        "dann reicht „Mach Jens’ Lieblingskaffee 1“."
    )
    return 0


def beispiel_konfigurationen() -> dict[str, Any]:
    """Skripte und Dashboard mit Beispiel-IDs – für beispiele/ und die Tests."""
    maschine = {
        kurz: f"{domain}.konrad_{slug(name)}"
        for kurz, domain, name in (
            ("aktiv", "select", "Aktives Programm"),
            ("gewaehlt", "select", "Ausgewähltes Programm"),
            ("staerke", "select", "Bohnenmenge"),
            ("menge", "number", "Füllmenge"),
            ("temperatur", "select", "Kaffee-Temperatur"),
            ("milch", "select", "Kaffee-Milch-Verhältnis"),
            ("intensitaet", "select", "Durchflussrate"),
            ("doppelt", "switch", "Mehrere Getränke"),
            ("einschalter", "switch", "Einschalter"),
            ("kindersicherung", "switch", "Kindersicherung"),
            ("stopp", "button", "Programm stoppen"),
            ("zustand", "sensor", "Betriebszustand"),
            ("fortschritt", "sensor", "Programm-Fortschritt"),
            ("endzeit", "sensor", "Programm-Endzeit"),
            ("fernstart", "binary_sensor", "Fernstart"),
        )
    }
    helfer = {
        "getraenk": "input_select.kaffee_getrank",
        "staerke": "input_select.kaffee_starke",
        "temperatur": "input_select.kaffee_temperatur",
        "milch": "input_select.kaffee_milchanteil",
        "intensitaet": "input_select.kaffee_intensitat",
        "menge": "input_number.kaffee_menge",
        "doppelt": "input_boolean.kaffee_doppelt",
    }
    plaetze_je_person = {
        person: {
            f"{slug(person)}_{n}": {
                "label": f"{person} {n}",
                "name": f"input_text.lieblingsgetrank_{slug(person)}_{n}",
                "daten": f"input_text.lieblingsgetrank_{slug(person)}_{n}_daten",
            }
            for n in range(1, STANDARD_PLAETZE + 1)
        }
        for person in ("Jens", "Nicole")
    }
    weitere = {
        "water_tank_empty": ("sensor.konrad_wassertank_leer", "Wassertank leer"),
        "bean_container_empty": ("sensor.konrad_bohnenbehalter_leer", "Bohnenbehälter leer"),
        "drip_tray_full": ("sensor.konrad_tropfschale_voll", "Tropfschale voll"),
        "device_should_be_descaled": ("sensor.konrad_entkalken_des_gerats_empfohlen", "Entkalken des Geräts empfohlen"),
        "coffee_counter": ("sensor.konrad_kaffees", "Kaffees"),
        "coffee_and_milk_counter": ("sensor.konrad_kaffee_und_milchtassen", "Kaffee- und Milchtassen"),
    }
    getraenke = [(GETRAENKE_NAMEN[k], PROGRAMM_PRAEFIX + k) for k in GETRAENKE_NAMEN]
    alle_plaetze = {k: v for p in plaetze_je_person.values() for k, v in p.items()}
    return {
        "skripte": skript_konfigurationen(maschine, helfer, alle_plaetze, "GERAETE_ID_VON_KONRAD", getraenke),
        "dashboard": dashboard_konfiguration("Konrad", maschine, helfer, plaetze_je_person, weitere),
        "bilder": {variante: konrad_svg(variante, "Konrad") for variante in BILD_VARIANTEN},
    }


def argumente(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="installieren.py",
        description=(
            "Richtet die Automation „Kaffeemaschine an, wenn die Alarmanlage morgens "
            "unscharf geschaltet wird“ in Home Assistant ein – oder mit --getraenke ein "
            "Dashboard für die Kaffeemaschine."
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
        "--licht",
        nargs="?",
        const=LICHT_AUTO,
        metavar="ENTITY_ID",
        help=(
            "Licht oder Steckdose an der Kaffeemaschine (light.… oder switch.…): geht in einer "
            "zweiten Automation mit der Maschine an und aus; ohne Wert wird es gesucht, neu angelegte "
            "Geräte zuerst. Eine deaktivierte Entität wird aktiviert"
        ),
    )
    parser.add_argument(
        "--dunkel",
        nargs="?",
        const=DUNKEL_AUTO,
        metavar="ENTITY_ID",
        help=(
            "Licht nur, wenn es dunkel ist: Helligkeitssensor (sensor.…, Lux), z. B. der des "
            "Bewegungsmelders in der Küche; ohne Angabe wird er gesucht. „aus“ entfernt die Bedingung wieder"
        ),
    )
    parser.add_argument(
        "--dunkel-unter",
        type=lux,
        metavar="LUX",
        help=(
            f"Bis zu welcher Helligkeit es „dunkel“ ist (Standard {STANDARD_DUNKEL_UNTER} lx; bei einer "
            "vorhandenen Licht-Automation deren Wert)"
        ),
    )
    parser.add_argument(
        "--von",
        type=uhrzeit,
        help=f"Anfang des Zeitfensters (Standard {STANDARD_VON}; bei einer vorhandenen Automation deren Wert)",
    )
    parser.add_argument(
        "--bis",
        type=uhrzeit,
        help=(
            f"Ende des Zeitfensters, ausschließlich (Standard {STANDARD_BIS}; bei einer vorhandenen "
            "Automation deren Wert)"
        ),
    )
    parser.add_argument(
        "--getraenke",
        action="store_true",
        help=(
            "Statt der Automation ein Dashboard für die Kaffeemaschine einrichten: Getränke wählen und "
            "anpassen, Lieblingsgetränke je Person, Bedienung, Wartung und Zähler (mit --personen)"
        ),
    )
    parser.add_argument(
        "--personen",
        metavar="NAMEN",
        help=(
            "Für --getraenke: wer Lieblingsgetränke bekommt, durch Komma getrennt, z. B. Jens,Nicole "
            f"(höchstens {MAX_PERSONEN})"
        ),
    )
    parser.add_argument(
        "--plaetze",
        type=anzahl_plaetze,
        default=STANDARD_PLAETZE,
        metavar="ANZAHL",
        help=(
            f"Für --getraenke: Plätze für Lieblingsgetränke je Person (Standard {STANDARD_PLAETZE}, "
            f"höchstens {MAX_PLAETZE})"
        ),
    )
    parser.add_argument(
        "--auch-in",
        nargs="?",
        const=AUCH_AUTO,
        metavar="DASHBOARDS",
        help=(
            "Für --getraenke: die Kaffeemaschine zusätzlich als Reiter in andere Dashboards legen, z. B. "
            "haus,wand-wohnzimmer (der Pfad aus der Adresszeile; „lovelace“ ist die Übersicht). Ohne Wert zur "
            "Auswahl. Die Wahl merkt sich das Skript; „aus“ entfernt die Reiter wieder"
        ),
    )
    parser.add_argument(
        "--nur-anzeigen",
        action="store_true",
        help="Nur zeigen, was eingerichtet würde – nichts ändern",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Nur ausgeben, was Home Assistant über Home Connect und Hausgeräte weiß – zur Fehlersuche",
    )
    parser.add_argument(
        "--pruefen",
        action="store_true",
        help=(
            "Nur prüfen, ob die Automationen tun, was sie sollen: aktuelle Zustände, Alter des "
            "Helligkeitswerts und die letzten Läufe in Klartext – nichts ändern"
        ),
    )
    parser.add_argument(
        "--probelauf",
        action="store_true",
        help=(
            "Nach dem Einrichten die Automation sofort auslösen – die Kaffeemaschine geht dann wirklich "
            "an und danach wieder aus"
        ),
    )
    parser.add_argument(
        "--anlassen",
        action="store_true",
        help="Nach dem Probelauf die Kaffeemaschine anlassen (sonst wird sie wieder ausgeschaltet)",
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


def dunkel_bestimmen(
    ha: HomeAssistant,
    args: argparse.Namespace,
    zustaende: dict[str, dict[str, Any]],
    vorhanden: dict[str, Any],
    interaktiv: bool,
) -> tuple[Optional[str], "int | float"]:
    """Welcher Helligkeitssensor als Dunkel-Sensor dient und bis wohin es dunkel ist.

    Vorrang: Option > gespeicherte Licht-Automation > Standard. ``--dunkel aus``
    entfernt den Sensor, ``--dunkel`` ohne Wert sucht ihn, ``--dunkel-unter``
    allein löst die Suche ebenfalls aus.
    """
    schwelle = (
        args.dunkel_unter
        if args.dunkel_unter is not None
        else vorhanden.get("dunkel_unter", STANDARD_DUNKEL_UNTER)
    )
    if args.dunkel == DUNKEL_AUS:
        if args.dunkel_unter is not None:
            raise Abbruch("--dunkel aus und --dunkel-unter passen nicht zusammen.")
        return None, schwelle
    suchen = args.dunkel == DUNKEL_AUTO
    dunkel: Optional[str] = None
    if args.dunkel and not suchen:
        if re.fullmatch(r"[\d.,]+", args.dunkel):
            raise Abbruch(
                f"Die Schwelle gehört zu --dunkel-unter: --dunkel-unter {args.dunkel} – "
                "--dunkel erwartet den Helligkeitssensor (sensor.…)."
            )
        dunkel = entitaet_bereitstellen(ha, args.dunkel, zustaende, DUNKEL_DOMAINS, "--dunkel")
        if not ist_helligkeitssensor(dunkel, zustaende[dunkel]):
            print(f"⚠ {dunkel} hat weder Geräteklasse „illuminance“ noch Einheit lx – ist das ein Helligkeitssensor?")
    elif not suchen:
        gespeichert = vorhanden.get("dunkel")
        if gespeichert in zustaende:
            dunkel = gespeichert
        elif gespeichert:
            print(f"⚠ Den gespeicherten Dunkel-Sensor {gespeichert} gibt es nicht mehr – ich suche neu.")
            suchen = True
        elif args.dunkel_unter is not None:
            suchen = True
    if suchen:
        kandidaten = kandidaten_helligkeit(zustaende)
        kueche = [k for k in kandidaten if ist_kueche(k[0], name_von(zustaende[k[0]]))]
        if not kueche:
            # Kein aktiver Sensor aus der Küche – vielleicht ist er nur deaktiviert.
            deaktiviert = deaktivierte_helligkeitssensoren(ha.register())
            if deaktiviert and interaktiv and len(deaktiviert) <= MAX_ANGEBOT:
                gewaehlt = deaktivierte_anbieten(ha, deaktiviert, zustaende, "als Dunkel-Sensor", mehrere=False)
                if gewaehlt:
                    [dunkel] = gewaehlt
            elif deaktiviert:
                aktive = ", ".join(entity_id for entity_id, _ in kandidaten) or "keine"
                raise Abbruch(
                    hinweis_deaktiviert(deaktiviert, "--dunkel")
                    + f"\n  Aktive Helligkeitssensoren: {aktive}.\n  {HELLIGKEIT_HINWEIS}"
                )
        if dunkel is None:
            if len(kueche) == 1:
                kandidaten = kueche
            [dunkel] = auswaehlen(
                kandidaten,
                "Kein Helligkeitssensor",
                "den Helligkeitssensor",
                "--dunkel",
                interaktiv,
                HELLIGKEIT_HINWEIS,
            )
    if dunkel is None:
        return None, schwelle
    zustand = zustaende[dunkel]
    if not verfuegbar(dunkel, zustaende):
        print(f"⚠ {dunkel} ist zurzeit nicht verfügbar – ohne Wert geht das Licht wie bisher immer an.")
    elif helligkeit(zustand) is None and str(zustand.get("state")) != "unknown":
        print(f"⚠ {dunkel} meldet gerade „{zustand.get('state')}“ statt einer Zahl – ist das ein Helligkeitssensor?")
    elif not ist_lux_sensor(dunkel, zustand):
        print(
            f"⚠ {dunkel} meldet keine Lux, sondern eine eigene Skala (ältere Homematic-Melder: 0–255). "
            "Die Schwelle --dunkel-unter gilt in dieser Skala."
        )
    print(
        f"✓ Dunkel-Sensor: {dunkel} („{name_von(zustand)}“), aktuell {helligkeit_text(zustand)} – "
        f"heller als {mit_einheit(schwelle, zustand)} bleibt das Licht aus"
    )
    return dunkel, schwelle


def kaffeemaschine_bestimmen(
    ha: HomeAssistant,
    args: argparse.Namespace,
    zustaende: dict[str, dict[str, Any]],
    vorhanden: dict[str, Any],
    interaktiv: bool,
) -> str:
    """Der Einschalter der Kaffeemaschine: angegeben, aus der Automation übernommen oder gesucht."""
    if args.kaffeemaschine:
        kaffeemaschine = entitaet_bereitstellen(
            ha, args.kaffeemaschine, zustaende, ("switch",), "--kaffeemaschine"
        )
    elif vorhanden.get("kaffeemaschine") in zustaende:
        kaffeemaschine = vorhanden["kaffeemaschine"]
    else:
        kandidaten, deaktiviert, hinweis = kandidaten_kaffeemaschine(ha.register(), zustaende)
        gewaehlt: list[str] = []
        # Deaktivierte Schalter nur anbieten, wenn kein aktiver Einschalter da ist –
        # sonst steht eine alte Steckdose neben der echten Kaffeemaschine.
        if deaktiviert and not kandidaten:
            hinweis = hinweis_deaktiviert(deaktiviert, "--kaffeemaschine") + "\n  " + hinweis
            if interaktiv and len(deaktiviert) <= MAX_ANGEBOT:
                gewaehlt = deaktivierte_anbieten(
                    ha, deaktiviert, zustaende, "als Einschalter der Kaffeemaschine", mehrere=False
                )
        if not gewaehlt:
            gewaehlt = auswaehlen(
                kandidaten,
                "Kein Einschalter der Kaffeemaschine",
                "den Einschalter der Kaffeemaschine",
                "--kaffeemaschine",
                interaktiv,
                hinweis,
            )
        [kaffeemaschine] = gewaehlt
    print(f"✓ Kaffeemaschine: {kaffeemaschine} („{name_von(zustaende[kaffeemaschine])}“)")
    return kaffeemaschine


def _einrichten(ha: HomeAssistant, args: argparse.Namespace, interaktiv: bool) -> int:
    info = ha.get_json("/api/config")
    print(f"✓ Home Assistant {info.get('version')} („{info.get('location_name')}“)")

    zustaende = {z["entity_id"]: z for z in ha.get_json("/api/states")}
    if args.diagnose:
        print(diagnose_ausfuehrlich(ha.register(), zustaende))
        return 0
    if args.pruefen:
        print(pruefung(ha, zustaende))
        return 0

    # Eine vorhandene Automation gibt die Einstellungen vor – ein zweiter Aufruf
    # (z. B. nur mit --licht) soll nicht alles neu erfragen.
    vorhanden = uebernehmen(vorhandene_automation(ha) or {})
    if args.getraenke:
        kaffeemaschine = kaffeemaschine_bestimmen(ha, args, zustaende, vorhanden, interaktiv)
        return getraenke_einrichten(ha, args, zustaende, kaffeemaschine, interaktiv)
    if vorhanden:
        print(
            f"✓ Die Automation „{ALIAS}“ gibt es schon – Alarmanlage, Kaffeemaschine und Zeitfenster "
            "werden übernommen, soweit nichts anderes angegeben ist."
        )
    alte_anlagen = vorhanden.get("alarmanlagen", [])
    uebernommen = False
    if args.alarmanlage:
        alarmanlagen = [
            entitaet_bereitstellen(ha, anlage, zustaende, ALARM_DOMAINS, "--alarmanlage")
            for anlage in args.alarmanlage
        ]
    elif alte_anlagen and all(anlage in zustaende for anlage in alte_anlagen):
        alarmanlagen = alte_anlagen
        uebernommen = True
    else:
        kandidaten = kandidaten_alarmanlage(zustaende)
        hinweis = (
            "Ist die Alarmanlage in Home Assistant eingebunden – als Alarmzentrale "
            "(alarm_control_panel) oder z. B. als Systemvariable der CCU (Sensor, Auswahl, Schalter)?"
        )
        alarmanlagen: list[str] = []
        if not any(entity_id.startswith("alarm_control_panel.") for entity_id, _ in kandidaten):
            deaktiviert = deaktivierte_alarm_entitaeten(ha.register())
            if deaktiviert:
                hinweis = hinweis_deaktiviert(deaktiviert)
                if interaktiv and len(deaktiviert) <= MAX_ANGEBOT:
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
    unscharf = unscharf_bestimmen(
        alarmanlagen,
        zustaende,
        args.unscharf or (vorhanden.get("unscharf") if uebernommen else None),
        interaktiv,
    )
    print(
        "✓ Alarmanlage: "
        + ", ".join(f"{anlage} („{name_von(zustaende[anlage])}“)" for anlage in alarmanlagen)
        + f", unscharf = „{unscharf}“"
    )

    kaffeemaschine = kaffeemaschine_bestimmen(ha, args, zustaende, vorhanden, interaktiv)
    if not verfuegbar(kaffeemaschine, zustaende):
        print(
            f"⚠ {kaffeemaschine} ist zurzeit nicht verfügbar – Home Assistant erreicht die Maschine "
            "gerade nicht (Integration geladen? Maschine am Strom und im WLAN?). Die Automation "
            "wird trotzdem angelegt."
        )

    # Auch die Licht-Automation gibt ihre Einstellungen vor, damit z. B.
    # „--dunkel“ allein reicht, um das Licht an die Helligkeit zu koppeln.
    vorhandenes_licht = licht_uebernehmen(vorhandene_automation(ha, LICHT_AUTOMATION_ID) or {})
    if vorhandenes_licht:
        print(
            "✓ Die Licht-Automation gibt es schon – Licht, Dunkel-Sensor und Schwelle werden "
            "übernommen, soweit nichts anderes angegeben ist."
        )
    licht = None
    gespeichert = vorhandenes_licht.get("licht")
    if args.licht == LICHT_AUTO:
        licht, beschreibung = licht_suchen(ha.register(), zustaende, kaffeemaschine, interaktiv)
        print(
            f"✓ Licht gesucht: {licht} ({beschreibung})"
            + (" – wie bisher." if licht == gespeichert else ".")
        )
        if gespeichert and gespeichert != licht:
            print(f"✓ Das Licht an der Maschine ist jetzt {licht} (vorher {gespeichert}).")
    elif args.licht:
        licht = entitaet_bereitstellen(ha, args.licht, zustaende, LICHT_DOMAINS, "--licht")
    elif gespeichert in zustaende:
        licht = gespeichert
    elif gespeichert:
        # Das alte Licht ist weg – z. B. eine Steckdose gegen einen Zwischenschalter getauscht.
        kandidaten = kandidaten_licht(ha.register(), zustaende, kaffeemaschine)
        if len(kandidaten) == 1:
            [(licht, beschreibung)] = kandidaten
            print(f"✓ Das gespeicherte Licht {gespeichert} gibt es nicht mehr – ersetzt durch {licht} ({beschreibung}).")
        elif kandidaten and interaktiv and len(kandidaten) <= MAX_ANGEBOT:
            licht, beschreibung = licht_suchen(
                ha.register(),
                zustaende,
                kaffeemaschine,
                interaktiv,
                f"Das gespeicherte Licht {gespeichert} gibt es nicht mehr – welches ist das neue?",
            )
            print(f"✓ Licht gesucht: {licht} ({beschreibung}).")
            print(f"✓ Das Licht an der Maschine ist jetzt {licht} (vorher {gespeichert}).")
        else:
            liste = "".join(f"\n    {entity_id}  {beschreibung}" for entity_id, beschreibung in kandidaten[:15])
            print(
                f"⚠ Das gespeicherte Licht {gespeichert} gibt es nicht mehr – die Licht-Automation bleibt "
                "unverändert. Ein neues Licht sucht --licht ohne Wert, oder direkt --licht <entity_id>."
                + (f"\n  Infrage kommen:{liste}" if liste else "")
            )
    if licht is None and (args.dunkel or args.dunkel_unter is not None):
        raise Abbruch("--dunkel braucht ein Licht an der Maschine: bitte --licht <entity_id> mit angeben.")
    dunkel, dunkel_unter = None, STANDARD_DUNKEL_UNTER
    if licht:
        if licht == kaffeemaschine:
            raise Abbruch(f"--licht {licht} ist der Einschalter der Kaffeemaschine selbst.")
        print(f"✓ Licht an der Maschine: {licht} („{name_von(zustaende[licht])}“)")
        if not verfuegbar(licht, zustaende):
            print(
                f"⚠ {licht} ist zurzeit nicht verfügbar – ist die Steckdose bzw. das Licht eingesteckt "
                "und in der Zentrale erreichbar? Die Automation wird trotzdem angelegt. Ein neues Licht "
                "sucht --licht ohne Wert."
            )
        dunkel, dunkel_unter = dunkel_bestimmen(ha, args, zustaende, vorhandenes_licht, interaktiv)

    von = args.von or vorhanden.get("von") or uhrzeit(STANDARD_VON)
    bis = args.bis or vorhanden.get("bis") or uhrzeit(STANDARD_BIS)
    config = automation_config(alarmanlagen, kaffeemaschine, von, bis, unscharf)
    licht_konfig = licht_config(kaffeemaschine, licht, dunkel, dunkel_unter) if licht else None
    if args.nur_anzeigen:
        print("\nDiese Automation würde eingerichtet (nichts geändert):")
        print(json.dumps(config, indent=2, ensure_ascii=False))
        if licht_konfig:
            print("\nUnd diese zweite Automation für das Licht an der Maschine:")
            print(json.dumps(licht_konfig, indent=2, ensure_ascii=False))
        return 0

    ergebnis, automation = installieren(ha, config)
    aktiv = automation["state"] == "on"
    print(
        f"✓ Automation {ergebnis}: „{ALIAS}“\n"
        f"  {automation['entity_id']}, {'aktiv' if aktiv else 'NICHT aktiv: ' + automation['state']}, "
        f"Zeitfenster {von[:5]}–{bis[:5]} Uhr"
    )
    if not aktiv:
        raise Abbruch("Die Automation ist nicht aktiv. Details zeigt ihre Ablaufverfolgung in Home Assistant.")

    if licht_konfig:
        ergebnis, licht_automation = installieren(ha, licht_konfig, LICHT_AUTOMATION_ID)
        licht_aktiv = licht_automation["state"] == "on"
        print(
            f"✓ Automation {ergebnis}: „{LICHT_ALIAS}“\n"
            f"  {licht_automation['entity_id']}, "
            f"{'aktiv' if licht_aktiv else 'NICHT aktiv: ' + licht_automation['state']}"
        )
        if not licht_aktiv:
            raise Abbruch(
                "Die Licht-Automation ist nicht aktiv. Details zeigt ihre Ablaufverfolgung in Home Assistant."
            )

    if args.probelauf:
        print("Probelauf: Automation wird ausgelöst …")
        hell = hell_hinweis(ha, dunkel, dunkel_unter) if dunkel else None
        ergebnis = probelauf(ha, automation["entity_id"], kaffeemaschine)
        print(f"✓ {ergebnis}")
        licht_war_an = False
        if licht:
            meldung = licht_pruefen(ha, licht, hell)
            print(meldung)
            licht_war_an = meldung.startswith("✓")
        # Die Maschine war vor dem Probelauf aus – so soll sie auch bleiben.
        if ergebnis != PROBELAUF_SCHON_AN and not args.anlassen:
            print(wieder_ausschalten(ha, kaffeemaschine))
            if licht_war_an:
                print(licht_aus_pruefen(ha, licht))

    print(
        "\nFertig. Wird die Alarmanlage im Zeitfenster unscharf geschaltet, geht die "
        "Kaffeemaschine an."
        + (
            (
                " Das Licht an der Maschine geht mit ihr aus – und an, wenn es dunkel ist."
                if dunkel
                else " Das Licht an der Maschine geht mit ihr an und aus."
            )
            if licht
            else ""
        )
        + "\nZum Ändern: Einstellungen → Automationen & Szenen → „"
        + ALIAS
        + ("“ bzw. „" + LICHT_ALIAS if licht else "")
        + "“."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
