#!/usr/bin/env python3
"""Richtet die Automation „Kaffeemaschine an, wenn die Alarmanlage morgens
unscharf geschaltet wird“ direkt in Home Assistant ein.

Das Skript braucht nur Python 3, keine Zusatzpakete. Es spricht mit Home
Assistant über dessen REST-API, sucht Alarmanlage und Kaffeemaschine
(Home Connect) heraus, legt die Automation an und prüft, dass sie aktiv ist.
Ein zweiter Aufruf aktualisiert die Automation, statt sie doppelt anzulegen.

Aufruf im Heimnetz, z. B. auf einem Mac oder PC:

    python3 installieren.py --url http://homeassistant.local:8123

Es fragt dann nach Benutzername und Passwort von Home Assistant (ein Benutzer
mit Administratorrechten). Statt Benutzername und Passwort geht auch ein
langlebiger Zugangstoken, als Umgebungsvariable HA_TOKEN oder mit --token.
Alle Optionen zeigt: python3 installieren.py --hilfe
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import socket
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
    alarmanlage: str, kaffeemaschine: str, von: str, bis: str
) -> dict[str, Any]:
    """Dieselbe Automation wie beispiele/automation_ohne_blueprint.yaml."""
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
                "entity_id": alarmanlage,
                "to": "disarmed",
                # Nicht reagieren, wenn die Alarmanlage nach einem Neustart
                # wieder erreichbar wird oder ein Scharfschalten abgebrochen wurde.
                "not_from": ["unavailable", "unknown", "arming"],
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


def auswaehlen(
    kandidaten: list[tuple[str, str]],
    fehlt: str,
    fuer: str,
    option: str,
    interaktiv: bool,
    hinweis: str = "",
) -> str:
    """Wählt einen Kandidaten – automatisch, per Rückfrage oder gar nicht.

    ``fehlt`` („Keine Alarmanlage“) und ``fuer`` („die Alarmanlage“) sind die
    Formen für die Meldungen „… in Home Assistant gefunden“ bzw.
    „Mehrere Möglichkeiten für …“.
    """
    if not kandidaten:
        raise Abbruch(
            f"{fehlt} in Home Assistant gefunden. {hinweis}".strip()
            + f"\n  Die Entitäts-ID lässt sich auch direkt angeben: {option} <entity_id>"
        )
    if len(kandidaten) == 1:
        return kandidaten[0][0]
    liste = "\n".join(
        f"  {nr}) {entity_id}  {beschreibung}"
        for nr, (entity_id, beschreibung) in enumerate(kandidaten, 1)
    )
    if not interaktiv:
        raise Abbruch(
            f"Mehrere Möglichkeiten für {fuer} gefunden:\n{liste}\n"
            f"  Bitte mit {option} <entity_id> angeben, welche gemeint ist."
        )
    print(f"\nMehrere Möglichkeiten für {fuer} gefunden:\n{liste}")
    while True:
        antwort = input(f"Welche ist gemeint? [1-{len(kandidaten)}] ").strip()
        if antwort.isdigit() and 1 <= int(antwort) <= len(kandidaten):
            return kandidaten[int(antwort) - 1][0]


def entitaet_pruefen(
    entity_id: str, domain: str, zustaende: dict[str, dict[str, Any]], option: str
) -> str:
    if not entity_id.startswith(domain + "."):
        raise Abbruch(f"{option} erwartet eine Entität aus dem Bereich „{domain}“, nicht „{entity_id}“.")
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
        help="Entitäts-ID der Alarmanlage, falls sie nicht automatisch gefunden wird",
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
        alarmanlage = entitaet_pruefen(args.alarmanlage, "alarm_control_panel", zustaende, "--alarmanlage")
    else:
        alarmanlage = auswaehlen(
            [
                (entity_id, f"„{name_von(zustand)}“")
                for entity_id, zustand in sorted(zustaende.items())
                if entity_id.startswith("alarm_control_panel.")
            ],
            "Keine Alarmanlage",
            "die Alarmanlage",
            "--alarmanlage",
            interaktiv,
            "Ist die Alarmanlage als Alarmzentrale (alarm_control_panel) eingebunden?",
        )
    print(f"✓ Alarmanlage: {alarmanlage} („{name_von(zustaende[alarmanlage])}“)")

    if args.kaffeemaschine:
        kaffeemaschine = entitaet_pruefen(args.kaffeemaschine, "switch", zustaende, "--kaffeemaschine")
    else:
        kaffeemaschine = auswaehlen(
            kandidaten_kaffeemaschine(ha, zustaende),
            "Kein Einschalter der Kaffeemaschine",
            "den Einschalter der Kaffeemaschine",
            "--kaffeemaschine",
            interaktiv,
            "Ist die Maschine über Home Connect eingebunden?",
        )
    print(f"✓ Kaffeemaschine: {kaffeemaschine} („{name_von(zustaende[kaffeemaschine])}“)")

    config = automation_config(alarmanlage, kaffeemaschine, args.von, args.bis)
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
