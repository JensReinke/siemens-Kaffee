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
from typing import Any, Callable, Iterator, Optional

AUTOMATION_ID = "siemens_kaffee_bei_unscharf"
ALIAS = "Kaffeemaschine an, wenn die Alarmanlage morgens unscharf geschaltet wird"
# Die zweite Automation für --licht: Licht an der Maschine folgt der Kaffeemaschine.
LICHT_AUTOMATION_ID = "siemens_kaffee_licht"
LICHT_ALIAS = "Licht an der Kaffeemaschine folgt der Kaffeemaschine"
LICHT_DOMAINS = ("light", "switch")
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
        "--licht",
        metavar="ENTITY_ID",
        help=(
            "Licht oder Steckdose an der Kaffeemaschine (light.… oder switch.…): geht in einer "
            "zweiten Automation mit der Maschine an und aus. Eine deaktivierte Entität wird aktiviert"
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


def _einrichten(ha: HomeAssistant, args: argparse.Namespace, interaktiv: bool) -> int:
    info = ha.get_json("/api/config")
    print(f"✓ Home Assistant {info.get('version')} („{info.get('location_name')}“)")

    zustaende = {z["entity_id"]: z for z in ha.get_json("/api/states")}
    if args.diagnose:
        print(diagnose_ausfuehrlich(ha.register(), zustaende))
        return 0

    # Eine vorhandene Automation gibt die Einstellungen vor – ein zweiter Aufruf
    # (z. B. nur mit --licht) soll nicht alles neu erfragen.
    vorhanden = uebernehmen(vorhandene_automation(ha) or {})
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
    if args.licht:
        licht = entitaet_bereitstellen(ha, args.licht, zustaende, LICHT_DOMAINS, "--licht")
    elif vorhandenes_licht.get("licht") in zustaende:
        licht = vorhandenes_licht["licht"]
    elif vorhandenes_licht.get("licht"):
        print(
            f"⚠ Das gespeicherte Licht {vorhandenes_licht['licht']} gibt es nicht mehr – die "
            "Licht-Automation bleibt unverändert. Neu angeben mit --licht <entity_id>."
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
                "und in der Zentrale erreichbar? Die Automation wird trotzdem angelegt."
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
        print(f"✓ {probelauf(ha, automation['entity_id'], kaffeemaschine)}")
        if licht:
            print(licht_pruefen(ha, licht, hell))

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
