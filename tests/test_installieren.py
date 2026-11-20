"""Tests für installieren.py.

Die Tests starten ein echtes Home Assistant samt HTTP-Server, bilden eine
Kaffeemaschine aus der Home-Connect-Integration nach und lassen das Skript
dagegen laufen – mit Token und mit Benutzername/Passwort.
"""

import argparse
import base64
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import yaml
from aiohttp.test_utils import TestClient
from freezegun.api import FrozenDateTimeFactory
from homeassistant.auth import auth_provider_from_config
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.setup import async_setup_component
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    MockUser,
    mock_config_flow,
    mock_integration,
    mock_platform,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator

import installieren

REPO = Path(__file__).resolve().parents[1]
AUTOMATION_OHNE_BLUEPRINT = REPO / "beispiele" / "automation_ohne_blueprint.yaml"
LICHT_FOLGT_KAFFEEMASCHINE = REPO / "beispiele" / "licht_folgt_kaffeemaschine.yaml"
LICHT_WENN_DUNKEL = REPO / "beispiele" / "licht_folgt_kaffeemaschine_wenn_dunkel.yaml"

HOME_CONNECT = "home_connect"
ALARMANLAGE = "alarm_control_panel.alarmanlage"
KAFFEEMASCHINE = "switch.kaffeevollautomat_einschalter"
GESCHIRRSPUELER = "switch.geschirrspuler_einschalter"
HELLIGKEIT = "sensor.kueche_helligkeit"  # der Lux-Wert des Bewegungsmelders in der Küche
KUECHE_SENSOR = "sensor.bewegungsmelder_kuche_beleuchtungsstarke"  # wie Homematic(IP) Local ihn anlegt
BERLIN = ZoneInfo("Europe/Berlin")


def um(uhrzeit: str, tag: str = "2026-10-05") -> datetime:
    """Gibt eine Uhrzeit in deutscher Ortszeit zurück (Standard: ein Montag)."""
    return datetime.fromisoformat(f"{tag} {uhrzeit}").replace(tzinfo=BERLIN)


class Systemvariable(BinarySensorEntity):
    """Stellvertretend für eine Systemvariable der CCU, wie Homematic(IP) Local sie anlegt."""

    _attr_should_poll = False

    def __init__(self, name: str, an: bool) -> None:
        self._attr_name = f"RaspberryMatic {name}"
        self._attr_unique_id = f"sysvar-{name}"
        self._attr_is_on = an


class HomeConnectSchalter(SwitchEntity):
    """Ein Schalter, wie ihn die Home-Connect-Integration anlegt."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, geraet: str, modell: str, name: str) -> None:
        self._attr_name = name
        self._attr_unique_id = f"{geraet}-{name}"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(HOME_CONNECT, geraet)},
            name=geraet,
            manufacturer="SIEMENS",
            model=modell,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


Geraete = dict[str, tuple[str, list[str]]]
HomeConnectEinrichten = Callable[..., Awaitable[MockConfigEntry]]


@pytest.fixture
def home_connect(hass: HomeAssistant) -> Iterator[HomeConnectEinrichten]:
    """Eine Funktion, die Home Connect mit Geräten nachspielt (siehe home_connect_einrichten)."""

    class HomeConnectFlow(ConfigFlow):
        """Nur damit Home Assistant den Konfigurationseintrag lädt."""

    with mock_config_flow(HOME_CONNECT, HomeConnectFlow):
        yield partial(home_connect_einrichten, hass)


async def home_connect_einrichten(
    hass: HomeAssistant,
    geraete: Geraete,
    variablen: dict[str, bool] | None = None,
    deaktiviert: set[str] = frozenset(),
    deaktivierte_schalter: set[tuple[str, str]] = frozenset(),
    konrad: "Konrad | None" = None,
) -> MockConfigEntry:
    """Spielt Home Connect nach: Gerätename → (Modell, weitere Entitäten des Geräts).

    Jedes Gerät bekommt wie in echt die Schalter „Einschalter“ und
    „Kindersicherung“; die weiteren Entitäten (z. B. ein Kaffeezähler) stehen
    nur im Entitätsregister. ``variablen`` (Name → an?) legt stellvertretend für
    CCU-Systemvariablen Binärsensoren an, die in ``deaktiviert`` von Anfang an
    deaktiviert sind; ``deaktivierte_schalter`` (Gerät, Schaltername) sind
    Schalter, die von Anfang an deaktiviert sind. ``konrad`` ersetzt die Schalter des
    Kaffeevollautomaten durch die vollständige Nachbildung der Kaffeemaschine (siehe
    ``Konrad``) samt der Dienste home_connect.*. Gibt den Konfigurationseintrag zurück.
    """
    plattformen = ["switch", "binary_sensor"] + (["select", "number", "sensor", "button"] if konrad else [])

    def konrad_plattform(name: str) -> list[Entity]:
        # Bei jedem (Neu-)Laden frische Entitäten – Home Assistant lässt kein zweites Hinzufügen zu.
        return konrad_entitaeten(konrad)[name] if konrad else []

    async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        if konrad:
            konrad_dienste(hass, konrad)
        await hass.config_entries.async_forward_entry_setups(entry, plattformen)
        return True

    async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        return await hass.config_entries.async_unload_platforms(entry, plattformen)

    async def async_setup_switch(
        hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
    ) -> None:
        async_add_entities(
            [
                HomeConnectSchalter(geraet, modell, name)
                for geraet, (modell, _) in geraete.items()
                if not (konrad and geraet == "Kaffeevollautomat")
                for name in ("Einschalter", "Kindersicherung")
            ]
            + konrad_plattform("switch")
        )

    async def async_setup_binary_sensor(
        hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
    ) -> None:
        async_add_entities(
            [Systemvariable(name, an) for name, an in (variablen or {}).items()] + konrad_plattform("binary_sensor")
        )

    def weitere_plattform(name: str) -> Callable[..., Awaitable[None]]:
        async def async_setup(
            hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
        ) -> None:
            async_add_entities(konrad_plattform(name))

        return async_setup

    mock_integration(
        hass,
        MockModule(
            HOME_CONNECT,
            async_setup_entry=async_setup_entry,
            async_unload_entry=async_unload_entry,
        ),
    )
    mock_platform(hass, f"{HOME_CONNECT}.config_flow", None)
    mock_platform(hass, f"{HOME_CONNECT}.switch", MockPlatform(async_setup_entry=async_setup_switch))
    mock_platform(
        hass,
        f"{HOME_CONNECT}.binary_sensor",
        MockPlatform(async_setup_entry=async_setup_binary_sensor),
    )
    for name in ("select", "number", "sensor", "button"):
        mock_platform(hass, f"{HOME_CONNECT}.{name}", MockPlatform(async_setup_entry=weitere_plattform(name)))
    eintrag = MockConfigEntry(domain=HOME_CONNECT, title="Home Connect")
    eintrag.add_to_hass(hass)
    # Schon vor dem Laden im Register als deaktiviert eingetragen – so bekommt
    # die Entität keinen Zustand, genau wie eine frisch importierte Systemvariable.
    for name in deaktiviert:
        er.async_get(hass).async_get_or_create(
            "binary_sensor",
            HOME_CONNECT,
            f"sysvar-{name}",
            config_entry=eintrag,
            suggested_object_id=slugify(f"raspberrymatic {name}"),
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        )
    for geraet, name in deaktivierte_schalter:
        # Wie in echt hängt der (vom Benutzer deaktivierte) Schalter schon am Gerät.
        geraete_eintrag = dr.async_get(hass).async_get_or_create(
            config_entry_id=eintrag.entry_id,
            identifiers={(HOME_CONNECT, geraet)},
            name=geraet,
            manufacturer="SIEMENS",
            model=geraete[geraet][0],
        )
        er.async_get(hass).async_get_or_create(
            "switch",
            HOME_CONNECT,
            f"{geraet}-{name}",
            config_entry=eintrag,
            device_id=geraete_eintrag.id,
            suggested_object_id=slugify(f"{geraet} {name}"),
            disabled_by=er.RegistryEntryDisabler.USER,
        )
    assert await hass.config_entries.async_setup(eintrag.entry_id)
    await hass.async_block_till_done()

    for geraet, (_, entitaeten) in geraete.items():
        geraete_eintrag = dr.async_get(hass).async_get_device_by_identifier(
            (HOME_CONNECT, geraet), eintrag.entry_id
        )
        assert geraete_eintrag is not None
        for entity_id in entitaeten:
            domain, objekt_id = entity_id.split(".")
            er.async_get(hass).async_get_or_create(
                domain,
                HOME_CONNECT,
                f"{geraet}-{objekt_id}",
                config_entry=eintrag,
                device_id=geraete_eintrag.id,
                suggested_object_id=objekt_id,
            )
    return eintrag


KAFFEEVOLLAUTOMAT = {"Kaffeevollautomat": ("TQ903D03", ["sensor.kaffeevollautomat_coffee_counter"])}
GESCHIRRSPUELER_GERAET = {"Geschirrspüler": ("SN65ZX49CE", ["sensor.geschirrspuler_remaining_program_time"])}


@pytest.fixture(autouse=True)
async def deutsche_zeitzone(hass: HomeAssistant) -> None:
    """Lässt Home Assistant in deutscher Zeit laufen."""
    await hass.config.async_set_time_zone("Europe/Berlin")


@pytest.fixture
async def home_assistant(hass: HomeAssistant, tmp_path: Path, hass_storage: dict[str, Any]) -> HomeAssistant:
    """Ein fertig eingerichtetes Home Assistant mit REST-API, Automations- und Skript-Editor, Helfern und Dashboards."""
    hass.config.config_dir = str(tmp_path)
    # Die Ersteinrichtung ist abgeschlossen – sonst lehnt die Anmeldung mit Passwort ab, sobald Dashboards geladen sind.
    hass_storage["onboarding"] = {
        "version": 4,
        "minor_version": 1,
        "key": "onboarding",
        "data": {"done": ["user", "core_config", "analytics", "integration"]},
    }
    (tmp_path / "configuration.yaml").write_text("automation: !include automations.yaml\nscript: !include scripts.yaml\n")
    (tmp_path / "automations.yaml").write_text("[]\n")
    (tmp_path / "scripts.yaml").write_text("{}\n")
    for komponente, config in (
        ("homeassistant", {}),  # u. a. die Dienste homeassistant.turn_on/turn_off
        ("auth", {}),
        ("api", {}),
        ("websocket_api", {}),
        ("config", {}),
        ("automation", {"automation": []}),
        ("script", {"script": {}}),
        ("input_select", {}),
        ("input_number", {}),
        ("input_boolean", {}),
        ("input_text", {}),
        ("lovelace", {}),
        ("persistent_notification", {}),
    ):
        assert await async_setup_component(hass, komponente, config)
    await hass.async_block_till_done()
    hass.states.async_set(ALARMANLAGE, "armed_night", {"friendly_name": "Alarmanlage"})
    return hass


@pytest.fixture
async def kaffeemaschine(home_assistant: HomeAssistant, home_connect: HomeConnectEinrichten) -> None:
    """Eine Kaffeemaschine und ein Geschirrspüler aus Home Connect."""
    await home_connect({**KAFFEEVOLLAUTOMAT, **GESCHIRRSPUELER_GERAET})
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "off"
    assert home_assistant.states.get(GESCHIRRSPUELER).state == "off"


@pytest.fixture
async def client(home_assistant: HomeAssistant, hass_client: ClientSessionGenerator) -> TestClient:
    """Ein HTTP-Server für dieses Home Assistant (die Anfragen tragen einen Token)."""
    return await hass_client()


@pytest.fixture
async def benutzerkonto(home_assistant: HomeAssistant, hass_admin_user: MockUser) -> MockUser:
    """Anmeldung mit Benutzername und Passwort („jens“ / „kaffee-123“) für einen Administrator."""
    anbieter = await auth_provider_from_config(
        home_assistant, home_assistant.auth._store, {"type": "homeassistant"}
    )
    assert anbieter is not None
    await anbieter.async_initialize()
    anbieter.data.add_auth("jens", "kaffee-123")
    await anbieter.data.async_save()
    home_assistant.auth._providers[(anbieter.type, anbieter.id)] = anbieter
    zugangsdaten = await anbieter.async_get_or_create_credentials({"username": "jens"})
    await home_assistant.auth.async_link_user(hass_admin_user, zugangsdaten)
    return hass_admin_user


async def skript(
    hass: HomeAssistant,
    client: TestClient,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
    umgebung: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Führt installieren.py gegen den Testserver aus: (Rückgabewert, Ausgabe, Fehlerausgabe)."""
    url = str(client.make_url("")).rstrip("/")
    env = {name: wert for name, wert in os.environ.items() if not name.startswith("HA_")}
    env.update(umgebung or {})
    with patch.dict(os.environ, env, clear=True):
        code = await hass.async_add_executor_job(installieren.main, ["--url", url, *argv])
    await hass.async_block_till_done()
    ausgabe = capsys.readouterr()
    return code, ausgabe.out, ausgabe.err


def gespeicherte_automationen(hass: HomeAssistant) -> list[dict[str, Any]]:
    return yaml.safe_load(Path(hass.config.path("automations.yaml")).read_text())


# --- Automation anlegen ------------------------------------------------------


@pytest.mark.usefixtures("kaffeemaschine")
async def test_legt_automation_an_und_sie_funktioniert(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    freezer: FrozenDateTimeFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Findet Alarmanlage und Kaffeemaschine, legt die Automation an – und sie schaltet morgens ein."""
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert f"✓ Alarmanlage: {ALARMANLAGE}" in ausgabe
    assert f"✓ Kaffeemaschine: {KAFFEEMASCHINE}" in ausgabe
    assert "✓ Automation angelegt" in ausgabe
    assert "aktiv, Zeitfenster 05:00–09:00 Uhr" in ausgabe

    [automation] = gespeicherte_automationen(home_assistant)
    assert automation == {
        "id": installieren.AUTOMATION_ID,
        **installieren.automation_config(ALARMANLAGE, KAFFEEMASCHINE, "05:00:00", "09:00:00"),
    }
    [zustand] = home_assistant.states.async_all("automation")
    assert zustand.state == "on"

    # Die Automation ist in Home Assistant geladen: Unscharfschalten um 06:30
    # schaltet die (echte) Schalter-Entität ein.
    freezer.move_to(um("23:00:00", "2026-10-04"))
    home_assistant.states.async_set(ALARMANLAGE, "armed_night")
    freezer.move_to(um("06:30:00"))
    home_assistant.states.async_set(ALARMANLAGE, "disarmed")
    await home_assistant.async_block_till_done()
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "on"
    assert home_assistant.states.get(GESCHIRRSPUELER).state == "off"


@pytest.mark.usefixtures("kaffeemaschine")
async def test_zweiter_aufruf_aktualisiert_statt_zu_verdoppeln(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--von", "6:00", "--bis", "10:30"
    )
    assert code == 0, fehler
    assert "✓ Automation aktualisiert" in ausgabe
    assert "Zeitfenster 06:00–10:30 Uhr" in ausgabe

    [automation] = gespeicherte_automationen(home_assistant)
    assert automation["conditions"][0] == {"condition": "time", "after": "06:00:00", "before": "10:30:00"}
    assert "zwischen 06:00 und 10:30 Uhr" in automation["description"]
    assert len(home_assistant.states.async_all("automation")) == 1


@pytest.mark.usefixtures("kaffeemaschine")
async def test_zweiter_aufruf_uebernimmt_die_einstellungen(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Gibt es die Automation schon, fragt das Skript nichts erneut, sondern übernimmt ihre Einstellungen."""
    home_assistant.states.async_remove(ALARMANLAGE)
    home_assistant.states.async_set(
        SYSTEMVARIABLE, "Scharf", {"friendly_name": "OpenCCU Alarmanlage", "options": ["Scharf", "Unscharf"]}
    )
    home_assistant.states.async_set(LICHT, "off", {"friendly_name": "LED Kaffee"})
    code, _, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token,
        "--alarmanlage", SYSTEMVARIABLE, "--unscharf", "Unscharf", "--von", "6:00", "--bis", "10:00",
    )
    assert code == 0, fehler
    [vorher] = gespeicherte_automationen(home_assistant)

    # Zweiter Aufruf nur mit --licht: keine einzige Rückfrage, nichts an der Kaffee-Automation geändert.
    with (
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=AssertionError("Es darf keine Rückfrage geben")),
    ):
        code, ausgabe, fehler = await skript(
            home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT
        )
    assert code == 0, fehler
    assert f"✓ Die Automation „{installieren.ALIAS}“ gibt es schon" in ausgabe
    assert f"✓ Alarmanlage: {SYSTEMVARIABLE} („OpenCCU Alarmanlage“), unscharf = „Unscharf“" in ausgabe
    assert f"✓ Kaffeemaschine: {KAFFEEMASCHINE}" in ausgabe
    assert "✓ Automation aktualisiert" in ausgabe
    assert "Zeitfenster 06:00–10:00 Uhr" in ausgabe
    automationen = {automation["id"]: automation for automation in gespeicherte_automationen(home_assistant)}
    assert automationen[installieren.AUTOMATION_ID] == vorher
    assert installieren.LICHT_AUTOMATION_ID in automationen

    # Einzelne Angaben überschreiben nur den jeweiligen Wert.
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--von", "5:00")
    assert code == 0, fehler
    assert "Zeitfenster 05:00–10:00 Uhr" in ausgabe


@pytest.mark.usefixtures("kaffeemaschine")
async def test_uebernahme_nur_solange_es_die_entitaet_gibt(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ist die gespeicherte Alarmanlage verschwunden, sucht das Skript wieder selbst."""
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler

    home_assistant.states.async_remove(ALARMANLAGE)
    home_assistant.states.async_set("alarm_control_panel.neu", "armed_away", {"friendly_name": "Neue Zentrale"})
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert "✓ Alarmanlage: alarm_control_panel.neu („Neue Zentrale“)" in ausgabe
    [automation] = gespeicherte_automationen(home_assistant)
    assert automation["triggers"][0]["entity_id"] == "alarm_control_panel.neu"


def test_uebernehmen_ist_nachsichtig() -> None:
    """Auch eine in Home Assistant bearbeitete Automation liefert, was noch lesbar ist."""
    config = installieren.automation_config([ALARMANLAGE, "sensor.zweite"], KAFFEEMASCHINE, "06:00:00", "10:00:00", "Unscharf")
    assert installieren.uebernehmen(config) == {
        "alarmanlagen": [ALARMANLAGE, "sensor.zweite"],
        "unscharf": "Unscharf",
        "kaffeemaschine": KAFFEEMASCHINE,
        "von": "06:00:00",
        "bis": "10:00:00",
    }
    config["conditions"][0]["after"] = "{{ states('input_datetime.aufstehen') }}"
    config["actions"] = [{"action": "light.turn_on", "target": {"entity_id": "light.kuche"}}]
    assert installieren.uebernehmen(config) == {"alarmanlagen": [ALARMANLAGE, "sensor.zweite"], "unscharf": "Unscharf"}
    assert installieren.uebernehmen({"triggers": "kaputt", "actions": None}) == {}


@pytest.mark.usefixtures("kaffeemaschine")
async def test_nur_anzeigen_aendert_nichts(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--nur-anzeigen"
    )
    assert code == 0, fehler
    assert "nichts geändert" in ausgabe
    assert '"action": "switch.turn_on"' in ausgabe
    assert gespeicherte_automationen(home_assistant) == []
    assert home_assistant.states.async_all("automation") == []


@pytest.mark.usefixtures("kaffeemaschine")
async def test_probelauf_schaltet_die_kaffeemaschine_ein(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--probelauf"
    )
    assert code == 0, fehler
    assert "✓ Die Kaffeemaschine ist angegangen." in ausgabe
    # … und danach wieder aus, denn vor dem Probelauf war sie aus.
    assert "✓ Die Kaffeemaschine ist wieder aus – sie war vor dem Probelauf aus." in ausgabe
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "off"

    # Mit --anlassen bleibt sie an.
    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--probelauf", "--anlassen"
    )
    assert code == 0, fehler
    assert "✓ Die Kaffeemaschine ist angegangen." in ausgabe
    assert "wieder aus" not in ausgabe
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "on"

    # Läuft sie schon, gibt es nichts auszuprobieren – und nichts auszuschalten.
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--probelauf")
    assert code == 0, fehler
    assert "schon an – zum Ausprobieren bitte erst ausschalten" in ausgabe
    assert "wieder aus" not in ausgabe
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "on"


@pytest.mark.usefixtures("kaffeemaschine")
async def test_probelauf_wenn_sich_die_maschine_nicht_ausschalten_laesst(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nimmt die Maschine das Ausschalten nicht an, sagt das Skript Bescheid, statt abzubrechen."""

    async def ignorieren(self: HomeConnectSchalter, **kwargs: Any) -> None:
        pass

    with patch.object(HomeConnectSchalter, "async_turn_off", ignorieren), patch("installieren.AUSSCHALT_SEKUNDEN", 1):
        code, ausgabe, fehler = await skript(
            home_assistant, client, capsys, "--token", hass_access_token, "--probelauf"
        )
    assert code == 0, fehler
    assert "✓ Die Kaffeemaschine ist angegangen." in ausgabe
    assert f"⚠ {KAFFEEMASCHINE} ließ sich nicht wieder ausschalten – bitte von Hand ausschalten." in ausgabe
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "on"


async def test_anmeldung_mit_benutzername_und_passwort(
    home_assistant: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    home_connect: HomeConnectEinrichten,
    benutzerkonto: MockUser,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ohne Token meldet sich das Skript wie die Oberfläche an und räumt danach auf."""
    await home_connect(KAFFEEVOLLAUTOMAT)
    client = await hass_client_no_auth()
    code, ausgabe, fehler = await skript(
        home_assistant,
        client,
        capsys,
        umgebung={"HA_BENUTZER": "jens", "HA_PASSWORT": "kaffee-123"},
    )
    assert code == 0, fehler
    assert "✓ Automation angelegt" in ausgabe
    assert len(gespeicherte_automationen(home_assistant)) == 1
    # Der bei der Anmeldung erhaltene Refresh-Token wurde wieder widerrufen.
    assert benutzerkonto.refresh_tokens == {}


@pytest.mark.usefixtures("benutzerkonto")
async def test_falsches_passwort(
    home_assistant: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = await hass_client_no_auth()
    code, _, fehler = await skript(
        home_assistant,
        client,
        capsys,
        umgebung={"HA_BENUTZER": "jens", "HA_PASSWORT": "falsch"},
    )
    assert code == 1
    assert "Benutzername oder Passwort ist falsch" in fehler
    assert gespeicherte_automationen(home_assistant) == []


async def test_ohne_zugangsdaten_klare_meldung(
    home_assistant: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = await hass_client_no_auth()
    code, _, fehler = await skript(home_assistant, client, capsys)
    assert code == 1
    assert "HA_TOKEN" in fehler and "HA_BENUTZER" in fehler


async def test_ungueltiger_token(
    home_assistant: HomeAssistant,
    client: TestClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", "falsch")
    assert code == 1
    assert "lehnt die Anmeldung ab (401)" in fehler


async def test_home_assistant_nicht_erreichbar(
    home_assistant: HomeAssistant,
    client: TestClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = await home_assistant.async_add_executor_job(
        installieren.main, ["--url", "127.0.0.1:9", "--token", "egal"]
    )
    assert code == 1
    assert "unter http://127.0.0.1:9 nicht erreichbar" in capsys.readouterr().err


# --- Alarmanlage und Kaffeemaschine finden -----------------------------------


@pytest.mark.usefixtures("kaffeemaschine")
async def test_mehrere_alarmanlagen_brauchen_eine_angabe(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home_assistant.states.async_set("alarm_control_panel.garage", "disarmed", {"friendly_name": "Garage"})
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 1
    assert "Mehrere Möglichkeiten für die Alarmanlage" in fehler
    assert "alarm_control_panel.garage" in fehler and ALARMANLAGE in fehler
    assert "--alarmanlage <entity_id>" in fehler

    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--alarmanlage", "alarm_control_panel.garage"
    )
    assert code == 0, fehler
    [automation] = gespeicherte_automationen(home_assistant)
    assert automation["triggers"][0]["entity_id"] == "alarm_control_panel.garage"


async def test_keine_alarmanlage(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home_assistant.states.async_remove(ALARMANLAGE)
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 1
    assert "Keine die Alarmanlage" not in fehler  # Grammatik stimmt
    assert "Keine Alarmanlage in Home Assistant gefunden" in fehler
    assert "alarm_control_panel" in fehler


async def test_zwei_kaffeemaschinen_brauchen_eine_angabe(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await home_connect(
        {**KAFFEEVOLLAUTOMAT, "Büro": ("TI9573X1DE", ["binary_sensor.buro_bean_container_empty"])},
    )
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 1
    assert "Mehrere Möglichkeiten für den Einschalter der Kaffeemaschine" in fehler
    assert KAFFEEMASCHINE in fehler and "switch.buro_einschalter" in fehler
    assert "SIEMENS TI9573X1DE" in fehler

    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--kaffeemaschine", "switch.buro_einschalter"
    )
    assert code == 0, fehler
    [automation] = gespeicherte_automationen(home_assistant)
    assert automation["actions"][0]["target"]["entity_id"] == "switch.buro_einschalter"


async def test_nur_geschirrspueler_wird_trotzdem_angeboten(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ist kein Gerät als Kaffeemaschine erkennbar, kommen alle Home-Connect-Einschalter infrage."""
    await home_connect(GESCHIRRSPUELER_GERAET)
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert f"✓ Kaffeemaschine: {GESCHIRRSPUELER}" in ausgabe


async def test_ohne_home_connect_nach_namen_suchen(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home_assistant.states.async_set("switch.steckdose_flur", "off", {"friendly_name": "Steckdose Flur"})
    home_assistant.states.async_set("switch.kaffeemaschine_power", "off", {"friendly_name": "Kaffeemaschine Power"})
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert "✓ Kaffeemaschine: switch.kaffeemaschine_power" in ausgabe


async def test_keine_kaffeemaschine(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home_assistant.states.async_set("switch.steckdose_flur", "off", {"friendly_name": "Steckdose Flur"})
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 1
    assert "Keine den Einschalter" not in fehler  # Grammatik stimmt
    assert "Kein Einschalter der Kaffeemaschine in Home Assistant gefunden" in fehler
    assert "Die Integration „Home Connect“ ist in Home Assistant nicht eingerichtet" in fehler
    assert "normalerweise" in fehler and installieren.HOME_CONNECT_ANLEITUNG in fehler
    assert "--kaffeemaschine <entity_id>" in fehler


async def test_home_connect_nicht_geladen(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ist Home Connect eingerichtet, aber nicht geladen, sagt das Skript genau das."""
    eintrag = await home_connect(KAFFEEVOLLAUTOMAT)
    assert await home_assistant.config_entries.async_unload(eintrag.entry_id)
    await home_assistant.async_block_till_done()
    # Home Assistant behält für die Entitäten einen Zustand „unavailable“.
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "unavailable"

    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 1
    assert "Integration „Home Connect“ (home_connect): nicht geladen" in fehler
    assert "neu laden" in fehler
    assert "Gerät „Kaffeevollautomat“ (SIEMENS TQ903D03, Integration: home_connect)" in fehler
    assert "3 nicht verfügbar" in fehler and f"Schalter: {KAFFEEMASCHINE} (nicht verfügbar)" in fehler

    # Ausdrücklich angegeben wird der Schalter genommen – mit Warnung.
    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--kaffeemaschine", KAFFEEMASCHINE
    )
    assert code == 0, fehler
    assert f"⚠ {KAFFEEMASCHINE} ist zurzeit nicht verfügbar" in ausgabe
    assert len(gespeicherte_automationen(home_assistant)) == 1


async def ccu_einrichten(hass: HomeAssistant, programme: int = 60) -> None:
    """Eine Zentrale wie die CCU: viele deaktivierte Programm-Schalter, eines heißt „Kaffee“."""
    eintrag = MockConfigEntry(domain="homematicip_local", title="RaspberryMatic")
    eintrag.add_to_hass(hass)
    zentrale = dr.async_get(hass).async_get_or_create(
        config_entry_id=eintrag.entry_id,
        identifiers={("homematicip_local", "ccu")},
        name="RaspberryMatic",
        manufacturer="eQ-3",
        model="CCU",
    )
    namen = [f"p_programm_{nr}" for nr in range(programme)] + ["p_kaffee_am_morgen"]
    for name in namen:
        er.async_get(hass).async_get_or_create(
            "switch",
            "homematicip_local",
            name,
            config_entry=eintrag,
            device_id=zentrale.id,
            suggested_object_id=f"raspberrymatic_{name}",
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        )


@pytest.mark.usefixtures("kaffeemaschine")
async def test_zentrale_ist_keine_kaffeemaschine(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ein Programm namens „Kaffee“ macht die CCU nicht zur Kaffeemaschine – kein Riesenmenü."""
    await ccu_einrichten(home_assistant)
    with (
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=AssertionError("Es darf keine Rückfrage geben")),
    ):
        code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert f"✓ Kaffeemaschine: {KAFFEEMASCHINE}" in ausgabe
    assert "raspberrymatic" not in ausgabe


async def test_nur_zentrale_ohne_kaffeemaschine(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ohne Kaffeemaschine bleibt die Meldung kurz und nennt nur Passendes aus der CCU."""
    await ccu_einrichten(home_assistant)
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 1
    assert "Kein Einschalter der Kaffeemaschine in Home Assistant gefunden" in fehler
    assert "Die Integration „Home Connect“ ist in Home Assistant nicht eingerichtet" in fehler
    assert "Aktive Schalter mit Kaffee-Bezug: keine" in fehler
    assert "switch.raspberrymatic_p_programm_" not in fehler
    assert fehler.count("\n") < 15

    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--diagnose")
    assert code == 0, fehler
    assert "switch.raspberrymatic_p_kaffee_am_morgen" not in ausgabe  # kein Kaffee-Gerät, keine Zentrale gelistet
    assert ausgabe.count("\n") < 15


@pytest.mark.usefixtures("kaffeemaschine")
async def test_diagnose(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--diagnose")
    assert code == 0, fehler
    assert "Integration „Home Connect“ (home_connect): geladen" in ausgabe
    assert "Entitäten von „Kaffeevollautomat“:" in ausgabe
    assert f"  {KAFFEEMASCHINE}  „Einschalter“  [home_connect]  Zustand: off" in ausgabe
    assert "sensor.kaffeevollautomat_coffee_counter" in ausgabe and "nicht verfügbar (kein Zustand)" in ausgabe
    assert "Entitäten von „Geschirrspüler“:" in ausgabe
    assert gespeicherte_automationen(home_assistant) == []


async def test_deaktivierter_einschalter_wird_aktiviert(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ist der Einschalter der Kaffeemaschine deaktiviert, nennt das Skript ihn bzw. aktiviert ihn."""
    await home_connect(
        {**KAFFEEVOLLAUTOMAT, **GESCHIRRSPUELER_GERAET},
        deaktivierte_schalter={("Kaffeevollautomat", "Einschalter")},
    )
    assert home_assistant.states.get(KAFFEEMASCHINE) is None
    assert home_assistant.states.get("switch.kaffeevollautomat_kindersicherung").state == "off"

    # Ohne Terminal: Hinweis statt Rückfrage – die Kindersicherung wird nicht angeboten.
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 1, ausgabe + fehler
    assert "Kein Einschalter der Kaffeemaschine in Home Assistant gefunden" in fehler
    assert f"    {KAFFEEMASCHINE}\n" in fehler
    assert "--kaffeemaschine <entity_id> angegeben, aktiviert dieses Skript sie selbst" in fehler
    assert "1) switch.kaffeevollautomat_kindersicherung" not in fehler  # nicht als Kandidat angeboten

    # Im Terminal: Angebot, den Schalter zu aktivieren.
    with (
        sofort_neuladen(),
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=["1"]),
    ):
        code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert "aktivieren und als Einschalter der Kaffeemaschine verwenden?" in ausgabe
    assert f"✓ {KAFFEEMASCHINE} aktiviert" in ausgabe
    assert f"✓ Kaffeemaschine: {KAFFEEMASCHINE}" in ausgabe
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "off"
    [automation] = gespeicherte_automationen(home_assistant)
    assert automation["actions"][0]["target"]["entity_id"] == KAFFEEMASCHINE


async def test_alte_steckdose_wird_nicht_angeboten_wenn_kaffeemaschine_da_ist(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Gibt es einen aktiven Einschalter, werden deaktivierte Schalter anderer „Kaffee“-Geräte nicht angeboten."""
    await home_connect(KAFFEEVOLLAUTOMAT)
    # Eine alte Homematic-Steckdose „LED Kaffee“: Hauptschalter nicht verfügbar, Rest deaktiviert.
    eintrag = MockConfigEntry(domain="homematicip_local", title="RaspberryMatic")
    eintrag.add_to_hass(home_assistant)
    steckdose = dr.async_get(home_assistant).async_get_or_create(
        config_entry_id=eintrag.entry_id,
        identifiers={("homematicip_local", "psm")},
        name="LED Kaffee",
        manufacturer="eQ-3",
        model="HMIP-PSM",
    )
    for name, deaktiviert in (("", False), ("vch4", True), ("vch5", True)):
        er.async_get(home_assistant).async_get_or_create(
            "switch",
            "homematicip_local",
            f"psm-{name or 'main'}",
            config_entry=eintrag,
            device_id=steckdose.id,
            suggested_object_id=f"led_kaffee_{name}".rstrip("_"),
            disabled_by=er.RegistryEntryDisabler.INTEGRATION if deaktiviert else None,
        )
    home_assistant.states.async_set("switch.led_kaffee", "unavailable", {"friendly_name": "LED Kaffee"})

    with (
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=AssertionError("Es darf keine Rückfrage geben")),
    ):
        code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert f"✓ Kaffeemaschine: {KAFFEEMASCHINE}" in ausgabe
    assert "led_kaffee" not in ausgabe


async def test_grosse_home_connect_geraete_bleiben_kaffeemaschinen(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ein Kaffeevollautomat mit sehr vielen Entitäten gilt nicht als Zentrale."""
    viele = [f"sensor.konrad_zaehler_{nr}" for nr in range(45)] + ["sensor.konrad_coffee_counter"]
    await home_connect({"Konrad": ("TQ903DZ3", viele)})
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert "✓ Kaffeemaschine: switch.konrad_einschalter („Konrad Einschalter“)" in ausgabe


async def test_angegebener_deaktivierter_einschalter_wird_aktiviert(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await home_connect(KAFFEEVOLLAUTOMAT, deaktivierte_schalter={("Kaffeevollautomat", "Einschalter")})
    with sofort_neuladen():
        code, ausgabe, fehler = await skript(
            home_assistant, client, capsys, "--token", hass_access_token,
            "--kaffeemaschine", KAFFEEMASCHINE, "--probelauf",
        )
    assert code == 0, fehler
    assert f"{KAFFEEMASCHINE} ist deaktiviert – wird aktiviert." in ausgabe
    assert "✓ Die Kaffeemaschine ist angegangen." in ausgabe
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "off"  # nach dem Probelauf wieder aus


@pytest.mark.usefixtures("kaffeemaschine")
@pytest.mark.parametrize(
    ("option", "wert", "meldung"),
    [
        ("--kaffeemaschine", "switch.gibt_es_nicht", "gibt es in Home Assistant nicht"),
        ("--kaffeemaschine", "light.kuche", "aus dem Bereich „switch“"),
        ("--alarmanlage", "light.kuche", "aus dem Bereich „alarm_control_panel“, „sensor“"),
        ("--licht", "light.gibt_es_nicht", "gibt es in Home Assistant nicht"),
        ("--licht", "sensor.kuche", "aus dem Bereich „light“, „switch“"),
        ("--licht", KAFFEEMASCHINE, "ist der Einschalter der Kaffeemaschine selbst"),
        ("--dunkel", HELLIGKEIT, "--dunkel braucht ein Licht an der Maschine: bitte --licht"),
    ],
)
async def test_angegebene_entitaet_wird_geprueft(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
    option: str,
    wert: str,
    meldung: str,
) -> None:
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, option, wert)
    assert code == 1
    assert meldung in fehler
    assert gespeicherte_automationen(home_assistant) == []


# --- Alarmanlage ohne Alarmzentrale, z. B. Systemvariable der Homematic-CCU ---

SYSTEMVARIABLE = "sensor.openccu_alarmanlage"


@pytest.fixture
async def openccu(home_assistant: HomeAssistant) -> None:
    """Keine Alarmzentrale – eine CCU-Systemvariable meldet den Zustand der Anlage."""
    home_assistant.states.async_remove(ALARMANLAGE)
    home_assistant.states.async_set(SYSTEMVARIABLE, "Vollschutz", {"friendly_name": "OpenCCU Alarmanlage"})
    home_assistant.states.async_set("sensor.openccu_temperatur", "21.5", {"friendly_name": "OpenCCU Temperatur"})


@pytest.mark.usefixtures("kaffeemaschine", "openccu")
async def test_systemvariable_als_alarmanlage(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    freezer: FrozenDateTimeFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Das Skript findet die Systemvariable, braucht aber den Zustand „unscharf“ – dann klappt alles."""
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 1
    assert "„OpenCCU Alarmanlage“ ist keine Alarmzentrale" in fehler
    assert "Aktuell: „Vollschutz“" in fehler and "--unscharf <Zustand>" in fehler
    assert gespeicherte_automationen(home_assistant) == []

    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--unscharf", "Unscharf"
    )
    assert code == 0, fehler
    assert f"✓ Alarmanlage: {SYSTEMVARIABLE} („OpenCCU Alarmanlage“), unscharf = „Unscharf“" in ausgabe
    [automation] = gespeicherte_automationen(home_assistant)
    assert automation["triggers"] == [
        {
            "trigger": "state",
            "entity_id": SYSTEMVARIABLE,
            "to": "Unscharf",
            "not_from": ["unavailable", "unknown"],
        }
    ]

    # Vollschutz → Unscharf um 06:30 schaltet die Kaffeemaschine ein …
    freezer.move_to(um("23:00:00", "2026-10-04"))
    home_assistant.states.async_set(SYSTEMVARIABLE, "Vollschutz")
    freezer.move_to(um("06:30:00"))
    home_assistant.states.async_set(SYSTEMVARIABLE, "Unscharf")
    await home_assistant.async_block_till_done()
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "on"

    # … ein Neustart der CCU-Anbindung (nicht erreichbar → Unscharf) aber nicht.
    await home_assistant.services.async_call(
        "switch", "turn_off", {"entity_id": KAFFEEMASCHINE}, blocking=True
    )
    home_assistant.states.async_set(SYSTEMVARIABLE, "unavailable")
    await home_assistant.async_block_till_done()
    freezer.move_to(um("06:40:00"))
    home_assistant.states.async_set(SYSTEMVARIABLE, "Unscharf")
    await home_assistant.async_block_till_done()
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "off"


@pytest.mark.usefixtures("kaffeemaschine", "openccu")
async def test_rueckfragen_im_terminal(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Im Terminal fragt das Skript, welche Entität die Alarmanlage ist und welcher Zustand „unscharf“."""
    home_assistant.states.async_set(
        "binary_sensor.openccu_alarmzone_1", "off", {"friendly_name": "OpenCCU Alarmzone 1"}
    )
    home_assistant.states.async_set(SYSTEMVARIABLE, "Unscharf", {"friendly_name": "OpenCCU Alarmanlage"})
    # Antworten: Nr. 1 aus der Liste, dann Enter für den vorgeschlagenen aktuellen Zustand.
    with (
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=["1", ""]),
    ):
        code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    # „Alarmanlage“ klingt eher nach dem Scharf-Zustand als „Alarmzone“ und steht oben.
    assert f"1) {SYSTEMVARIABLE}" in ausgabe
    assert "2) binary_sensor.openccu_alarmzone_1" in ausgabe
    assert "Welcher Zustand bedeutet „unscharf“? Aktuell: „Unscharf“" in ausgabe
    assert f"✓ Alarmanlage: {SYSTEMVARIABLE} („OpenCCU Alarmanlage“), unscharf = „Unscharf“" in ausgabe
    [automation] = gespeicherte_automationen(home_assistant)
    assert automation["triggers"][0]["to"] == "Unscharf"


@pytest.mark.usefixtures("kaffeemaschine")
@pytest.mark.parametrize(
    ("entity_id", "zustand", "attribute", "moeglich"),
    [
        (
            "select.openccu_alarmmodus",
            "Vollschutz",
            {"options": ["Unscharf", "Hüllschutz", "Vollschutz"]},
            "Unscharf, Hüllschutz, Vollschutz",
        ),
        ("binary_sensor.openccu_alarm_scharf", "on", {}, "off, on"),
        ("switch.openccu_alarm_scharf", "on", {}, "off, on"),
    ],
)
async def test_moegliche_zustaende_werden_genannt(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
    entity_id: str,
    zustand: str,
    attribute: dict[str, Any],
    moeglich: str,
) -> None:
    home_assistant.states.async_remove(ALARMANLAGE)
    home_assistant.states.async_set(entity_id, zustand, {"friendly_name": "Alarm", **attribute})
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 1
    assert f"Aktuell: „{zustand}“, möglich: {moeglich}." in fehler
    assert "--unscharf" in fehler


# --- Homematic-CCU: Hüllschutz und Vollschutz als zwei Systemvariablen ----------

INTERN = "binary_sensor.raspberrymatic_alarm_intern_scharf"
EXTERN = "binary_sensor.raspberrymatic_alarm_extern_scharf"


@pytest.fixture
async def raspberrymatic(home_assistant: HomeAssistant) -> None:
    """Eine CCU mit zwei Scharf-Variablen und viel Beiwerk, das nach „Alarm“ klingt."""
    home_assistant.states.async_remove(ALARMANLAGE)
    for entity_id, zustand, name in (
        (INTERN, "off", "RaspberryMatic Alarm Intern scharf"),
        (EXTERN, "off", "RaspberryMatic Alarm Extern scharf"),
        ("binary_sensor.openccu_alarmzone_1", "off", "OpenCCU Alarmzone 1"),
        ("binary_sensor.wz_rauchmelder_einbruchalarm", "off", "WZ Rauchmelder Einbruchalarm"),
        ("binary_sensor.handsender_1_alarm_batterie", "off", "Handsender 1 Alarm Batterie"),
        ("binary_sensor.wassersensor_wm_alarmzustand", "off", "Wassersensor WM Alarmzustand"),
        ("sensor.raspberrymatic_alarmmeldungen", "0", "RaspberryMatic Alarmmeldungen"),
    ):
        home_assistant.states.async_set(entity_id, zustand, {"friendly_name": name})


@pytest.mark.usefixtures("kaffeemaschine", "raspberrymatic")
async def test_beiwerk_wird_ausgeblendet_und_treffer_sortiert(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rauchmelder, Batterie, Wasser und Meldungszähler fliegen raus; „scharf“ steht oben."""
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 1
    zeilen = [zeile.strip() for zeile in fehler.splitlines() if re.match(r"\s*\d+\) ", zeile)]
    assert zeilen[0].startswith(f"1) {EXTERN}")
    assert zeilen[1].startswith(f"2) {INTERN}")
    assert zeilen[2].startswith("3) binary_sensor.openccu_alarmzone_1")
    assert len(zeilen) == 3
    assert "rauchmelder" not in fehler and "batterie" not in fehler
    assert "wassersensor" not in fehler and "alarmmeldungen" not in fehler
    assert "--alarmanlage <entity_id> angeben, welche gemeint ist (mehrfach möglich)" in fehler


@pytest.mark.usefixtures("kaffeemaschine", "raspberrymatic")
async def test_zwei_systemvariablen_fuer_huell_und_vollschutz(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    freezer: FrozenDateTimeFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token,
        "--alarmanlage", INTERN, "--alarmanlage", EXTERN, "--unscharf", "off",
    )
    assert code == 0, fehler
    assert (
        f"✓ Alarmanlage: {INTERN} („RaspberryMatic Alarm Intern scharf“), "
        f"{EXTERN} („RaspberryMatic Alarm Extern scharf“), unscharf = „off“"
    ) in ausgabe
    [automation] = gespeicherte_automationen(home_assistant)
    assert automation["triggers"][0]["entity_id"] == [INTERN, EXTERN]
    assert automation["triggers"][0]["to"] == "off"

    # Nachts Hüllschutz (intern) scharf, morgens unscharf → Kaffee.
    freezer.move_to(um("23:00:00", "2026-10-04"))
    home_assistant.states.async_set(INTERN, "on")
    freezer.move_to(um("06:30:00"))
    home_assistant.states.async_set(INTERN, "off")
    await home_assistant.async_block_till_done()
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "on"


@pytest.mark.usefixtures("kaffeemaschine", "raspberrymatic")
async def test_mehrere_im_terminal_waehlen(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Im Terminal lassen sich mehrere Entitäten wählen („1,2“) und der Zustand per Nummer."""
    with (
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=["1, 2", "1"]),
    ):
        code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert "welche sind gemeint? (mehrere möglich)" in ausgabe
    assert "1) off" in ausgabe and "2) on" in ausgabe
    [automation] = gespeicherte_automationen(home_assistant)
    assert automation["triggers"][0]["entity_id"] == [EXTERN, INTERN]
    assert automation["triggers"][0]["to"] == "off"


async def test_deaktivierte_entitaeten_werden_genannt(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deaktivierte Entitäten (so legt Homematic(IP) Local Systemvariablen an) werden genannt."""
    home_assistant.states.async_remove(ALARMANLAGE)
    eintrag = await home_connect(KAFFEEVOLLAUTOMAT)
    # Stellvertretend für die CCU hängt die deaktivierte Variable am Home-Connect-Gerät.
    geraet = dr.async_get(home_assistant).async_get_device_by_identifier(
        (HOME_CONNECT, "Kaffeevollautomat"), eintrag.entry_id
    )
    er.async_get(home_assistant).async_get_or_create(
        "binary_sensor",
        HOME_CONNECT,
        "sysvar-alarm-intern",
        config_entry=eintrag,
        device_id=geraet.id,
        suggested_object_id="raspberrymatic_alarm_intern_scharf",
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 1
    assert "Keine Alarmanlage in Home Assistant gefunden" in fehler
    assert "sie sind aber deaktiviert" in fehler
    assert f"    {INTERN}\n" in fehler
    assert "--alarmanlage <entity_id> angegeben, aktiviert dieses Skript sie selbst" in fehler
    assert "„Deaktivierte Entitäten anzeigen“" in fehler and "„hahm“" in fehler
    assert "switch.kaffeevollautomat" not in fehler


# --- Deaktivierte Systemvariable selbst aktivieren -------------------------------

VARIABLE_NAME = "Sv Alarm Intern scharf"
VARIABLE = "binary_sensor.raspberrymatic_sv_alarm_intern_scharf"


def sofort_neuladen() -> Any:
    """Lässt Home Assistant die Integration nach dem Aktivieren sofort statt nach 30 s neu laden."""
    return patch("homeassistant.config_entries.RELOAD_AFTER_UPDATE_DELAY", 0)


async def test_aktiviert_angegebene_variable_selbst(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Eine mit --alarmanlage angegebene, deaktivierte Entität aktiviert das Skript und wartet auf sie."""
    home_assistant.states.async_remove(ALARMANLAGE)
    await home_connect(KAFFEEVOLLAUTOMAT, variablen={VARIABLE_NAME: False}, deaktiviert={VARIABLE_NAME})
    assert home_assistant.states.get(VARIABLE) is None

    with sofort_neuladen():
        code, ausgabe, fehler = await skript(
            home_assistant, client, capsys, "--token", hass_access_token,
            "--alarmanlage", VARIABLE, "--unscharf", "off",
        )
    assert code == 0, fehler
    assert er.async_get(home_assistant).async_get(VARIABLE).disabled_by is None
    assert f"{VARIABLE} ist deaktiviert – wird aktiviert." in ausgabe
    assert f"✓ {VARIABLE} aktiviert" in ausgabe
    assert "lädt die Integration nach etwa 30 Sekunden neu … da." in ausgabe
    assert home_assistant.states.get(VARIABLE).state == "off"
    [automation] = gespeicherte_automationen(home_assistant)
    assert automation["triggers"][0] == {
        "trigger": "state",
        "entity_id": VARIABLE,
        "to": "off",
        "not_from": ["unavailable", "unknown"],
    }


async def test_bietet_aktivierung_im_terminal_an(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ohne Angabe fragt das Skript, ob es die deaktivierte Variable aktivieren soll."""
    home_assistant.states.async_remove(ALARMANLAGE)
    await home_connect(KAFFEEVOLLAUTOMAT, variablen={VARIABLE_NAME: False}, deaktiviert={VARIABLE_NAME})

    # Antworten: Nr. 1 der deaktivierten Entitäten, dann „off“ (Nr. 1) als „unscharf“.
    with (
        sofort_neuladen(),
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=["1", "1"]),
    ):
        code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert "Soll ich eine davon aktivieren und als Alarmanlage verwenden?" in ausgabe
    assert f"1) {VARIABLE}" in ausgabe
    assert f"✓ {VARIABLE} aktiviert" in ausgabe
    assert f"✓ Alarmanlage: {VARIABLE} („RaspberryMatic {VARIABLE_NAME}“), unscharf = „off“" in ausgabe
    assert len(gespeicherte_automationen(home_assistant)) == 1


async def test_ueberspringen_der_aktivierung(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mit Enter wird nichts aktiviert; dann läuft die normale Auswahl weiter."""
    home_assistant.states.async_remove(ALARMANLAGE)
    await home_connect(KAFFEEVOLLAUTOMAT, variablen={VARIABLE_NAME: False}, deaktiviert={VARIABLE_NAME})
    home_assistant.states.async_set(SYSTEMVARIABLE, "Unscharf", {"friendly_name": "OpenCCU Alarmanlage"})

    with (
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=["", ""]),
    ):
        code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert "Soll ich eine davon aktivieren und als Alarmanlage verwenden?" in ausgabe
    assert er.async_get(home_assistant).async_get(VARIABLE).disabled_by is not None
    assert f"✓ Alarmanlage: {SYSTEMVARIABLE} („OpenCCU Alarmanlage“), unscharf = „Unscharf“" in ausgabe


# --- Licht an der Maschine (--licht) -----------------------------------------

HOMEMATIC = "homematicip_local"
LICHT = "switch.led_kaffee"
ZWISCHENSCHALTER = "switch.zwischenschalter_kaffee"  # der neue Zwischenschalter am LED Kaffee


class Steckdose(SwitchEntity):
    """Die Homematic-IP-Schaltsteckdose „LED Kaffee“ mit der LED-Leiste an der Maschine."""

    _attr_should_poll = False

    def __init__(self) -> None:
        self._attr_name = "LED Kaffee"
        self._attr_unique_id = "psm-main"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(HOMEMATIC, "psm")}, name="LED Kaffee", manufacturer="eQ-3", model="HMIP-PSM"
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


@pytest.fixture
def steckdose(hass: HomeAssistant) -> Iterator[Callable[..., Awaitable[None]]]:
    """Eine Funktion, die Homematic(IP) Local mit der Steckdose „LED Kaffee“ nachspielt."""

    class HomematicFlow(ConfigFlow):
        """Nur damit Home Assistant den Konfigurationseintrag lädt."""

    with mock_config_flow(HOMEMATIC, HomematicFlow):
        yield partial(steckdose_einrichten, hass)


class Zwischenschalter(SwitchEntity):
    """Der neue Homematic-IP-Zwischenschalter, der die Steckdose „LED Kaffee“ ersetzt."""

    _attr_should_poll = False

    def __init__(self) -> None:
        self._attr_name = "Zwischenschalter Kaffee"
        self._attr_unique_id = "fsm-main"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(HOMEMATIC, "fsm")}, name="Zwischenschalter Kaffee", manufacturer="eQ-3", model="HmIP-FSM"
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


class Helligkeitssensor(SensorEntity):
    """Die Beleuchtungsstärke eines Homematic-IP-Bewegungsmelders in der Küche."""

    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = "lx"

    def __init__(self, wert: float) -> None:
        self._attr_name = "Bewegungsmelder Küche Beleuchtungsstärke"
        self._attr_unique_id = "smi-kueche-illumination"
        self._attr_native_value = wert


async def steckdose_einrichten(
    hass: HomeAssistant,
    deaktiviert: bool = False,
    helligkeit: float | None = None,
    sensor_deaktiviert: bool = False,
    zwischenschalter: bool = False,
    ohne_alte: bool = False,
    freezer: FrozenDateTimeFactory | None = None,
) -> None:
    """Legt die Steckdose „LED Kaffee“ (switch.led_kaffee) an – auf Wunsch deaktiviert –
    und mit ``helligkeit`` den Helligkeitssensor des Küchen-Bewegungsmelders (KUECHE_SENSOR),
    der mit ``sensor_deaktiviert`` von Anfang an deaktiviert ist, wie Homematic(IP) Local es macht.
    Mit ``zwischenschalter`` kommt der neue Zwischenschalter (ZWISCHENSCHALTER) dazu, mit ``freezer``
    später angelegt als die Steckdose; ``ohne_alte`` lässt die Steckdose weg."""
    plattformen = ["switch"] + (["sensor"] if helligkeit is not None else [])

    async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        await hass.config_entries.async_forward_entry_setups(entry, plattformen)
        return True

    async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        return await hass.config_entries.async_unload_platforms(entry, plattformen)

    async def async_setup_switch(
        hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
    ) -> None:
        async_add_entities(([] if ohne_alte else [Steckdose()]) + ([Zwischenschalter()] if zwischenschalter else []))

    async def async_setup_sensor(
        hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
    ) -> None:
        assert helligkeit is not None
        async_add_entities([Helligkeitssensor(helligkeit)])

    mock_integration(
        hass,
        MockModule(HOMEMATIC, async_setup_entry=async_setup_entry, async_unload_entry=async_unload_entry),
    )
    mock_platform(hass, f"{HOMEMATIC}.config_flow", None)
    mock_platform(hass, f"{HOMEMATIC}.switch", MockPlatform(async_setup_entry=async_setup_switch))
    mock_platform(hass, f"{HOMEMATIC}.sensor", MockPlatform(async_setup_entry=async_setup_sensor))
    eintrag = MockConfigEntry(domain=HOMEMATIC, title="RaspberryMatic")
    eintrag.add_to_hass(hass)
    if deaktiviert:
        er.async_get(hass).async_get_or_create(
            "switch",
            HOMEMATIC,
            "psm-main",
            config_entry=eintrag,
            suggested_object_id="led_kaffee",
            disabled_by=er.RegistryEntryDisabler.USER,
        )
    if sensor_deaktiviert:
        er.async_get(hass).async_get_or_create(
            "sensor",
            HOMEMATIC,
            "smi-kueche-illumination",
            config_entry=eintrag,
            suggested_object_id="bewegungsmelder_kuche_beleuchtungsstarke",
            original_name="Bewegungsmelder Küche Beleuchtungsstärke",
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        )
    if freezer is not None and zwischenschalter and not ohne_alte and not deaktiviert:
        # Die Steckdose gibt es schon etwas länger als den Zwischenschalter (kurz genug, dass der Token gilt).
        er.async_get(hass).async_get_or_create(
            "switch", HOMEMATIC, "psm-main", config_entry=eintrag, suggested_object_id="led_kaffee"
        )
        freezer.tick(timedelta(minutes=5))
    assert await hass.config_entries.async_setup(eintrag.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("kaffeemaschine")
async def test_licht_folgt_der_kaffeemaschine(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    steckdose: Callable[..., Awaitable[None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mit --licht geht die LED-Steckdose an der Maschine mit der Kaffeemaschine an und aus."""
    await steckdose()
    assert home_assistant.states.get(LICHT).state == "off"

    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--probelauf"
    )
    assert code == 0, fehler
    # Die Steckdose heißt zwar „Kaffee“, ist aber keine zweite Kaffeemaschine – keine Rückfrage.
    assert f"✓ Kaffeemaschine: {KAFFEEMASCHINE}" in ausgabe
    assert f"✓ Licht an der Maschine: {LICHT} („LED Kaffee“)" in ausgabe
    assert f"✓ Automation angelegt: „{installieren.LICHT_ALIAS}“" in ausgabe
    assert "✓ Die Kaffeemaschine ist angegangen." in ausgabe
    assert "✓ Das Licht an der Maschine ist angegangen." in ausgabe
    assert "✓ Die Kaffeemaschine ist wieder aus – sie war vor dem Probelauf aus." in ausgabe
    assert "✓ Das Licht an der Maschine ist wieder aus." in ausgabe
    assert "Das Licht an der Maschine geht mit ihr an und aus." in ausgabe
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "off"
    assert home_assistant.states.get(LICHT).state == "off"

    automationen = {automation["id"]: automation for automation in gespeicherte_automationen(home_assistant)}
    assert set(automationen) == {installieren.AUTOMATION_ID, installieren.LICHT_AUTOMATION_ID}
    assert automationen[installieren.LICHT_AUTOMATION_ID] == {
        "id": installieren.LICHT_AUTOMATION_ID,
        **installieren.licht_config(KAFFEEMASCHINE, LICHT),
    }

    # Maschine aus → Licht aus; Maschine an, egal wodurch → Licht an.
    await home_assistant.services.async_call("switch", "turn_off", {"entity_id": KAFFEEMASCHINE}, blocking=True)
    await home_assistant.async_block_till_done()
    assert home_assistant.states.get(LICHT).state == "off"
    await home_assistant.services.async_call("switch", "turn_on", {"entity_id": KAFFEEMASCHINE}, blocking=True)
    await home_assistant.async_block_till_done()
    assert home_assistant.states.get(LICHT).state == "on"

    # Ein weiterer Aufruf ohne --licht lässt die Licht-Automation stehen.
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert len(gespeicherte_automationen(home_assistant)) == 2


@pytest.mark.usefixtures("kaffeemaschine")
async def test_licht_wird_gesucht(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    steckdose: Callable[..., Awaitable[None]],
    freezer: FrozenDateTimeFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--licht ohne Wert zeigt die Lichter mit Kaffee-Bezug, neu angelegte zuerst – Konrads Schalter nicht."""
    await steckdose(zwischenschalter=True, freezer=freezer)
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT)
    assert code == 0, fehler

    # Ohne Terminal: klare Meldung mit beiden Kandidaten, der neue zuerst.
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--licht")
    assert code == 1
    assert "Mehrere Möglichkeiten für das Licht an der Maschine gefunden" in fehler
    assert f"1) {ZWISCHENSCHALTER}  „Zwischenschalter Kaffee“, eQ-3 HmIP-FSM, aus, neu angelegt am " in fehler
    assert f"2) {LICHT}  „LED Kaffee“, eQ-3 HMIP-PSM, aus, neu angelegt am " in fehler
    assert "kaffeevollautomat" not in fehler  # Konrads eigene Schalter sind kein Licht
    assert "--licht <entity_id>" in fehler

    # Im Terminal: Nummer 1 – die Automation zeigt danach auf den Zwischenschalter.
    with (
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=["1"]),
    ):
        code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--licht")
    assert code == 0, fehler
    assert "neu angelegte Geräte stehen oben" in ausgabe
    assert f"✓ Licht gesucht: {ZWISCHENSCHALTER} („Zwischenschalter Kaffee“, eQ-3 HmIP-FSM, aus, neu angelegt am " in ausgabe
    assert f"✓ Das Licht an der Maschine ist jetzt {ZWISCHENSCHALTER} (vorher {LICHT})." in ausgabe
    assert f"✓ Licht an der Maschine: {ZWISCHENSCHALTER} („Zwischenschalter Kaffee“)" in ausgabe
    automationen = {automation["id"]: automation for automation in gespeicherte_automationen(home_assistant)}
    assert automationen[installieren.LICHT_AUTOMATION_ID] == {
        "id": installieren.LICHT_AUTOMATION_ID,
        **installieren.licht_config(KAFFEEMASCHINE, ZWISCHENSCHALTER),
    }

    # Und die neue Automation schaltet wirklich den Zwischenschalter.
    await maschine(home_assistant, True)
    assert home_assistant.states.get(ZWISCHENSCHALTER).state == "on"
    assert home_assistant.states.get(LICHT).state == "off"


@pytest.mark.usefixtures("kaffeemaschine")
async def test_verschwundenes_licht_wird_ersetzt(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    steckdose: Callable[..., Awaitable[None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ist das gespeicherte Licht weg und gibt es genau einen Kandidaten, übernimmt das Skript ihn."""
    await steckdose(zwischenschalter=True)
    helligkeit(home_assistant, "4")
    code, _, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel", HELLIGKEIT
    )
    assert code == 0, fehler

    # Die alte Steckdose verschwindet aus Home Assistant.
    er.async_get(home_assistant).async_remove(LICHT)
    await home_assistant.async_block_till_done()
    home_assistant.states.async_remove(LICHT)
    assert home_assistant.states.get(LICHT) is None

    with patch.object(sys.stdin, "isatty", return_value=True), keine_rueckfrage():
        code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert (
        f"✓ Das gespeicherte Licht {LICHT} gibt es nicht mehr – ersetzt durch {ZWISCHENSCHALTER} "
        "(„Zwischenschalter Kaffee“, eQ-3 HmIP-FSM, aus, neu angelegt am " in ausgabe
    )
    assert f"✓ Dunkel-Sensor: {HELLIGKEIT}" in ausgabe  # die Dunkel-Einstellungen bleiben
    automationen = {automation["id"]: automation for automation in gespeicherte_automationen(home_assistant)}
    assert automationen[installieren.LICHT_AUTOMATION_ID] == {
        "id": installieren.LICHT_AUTOMATION_ID,
        **installieren.licht_config(KAFFEEMASCHINE, ZWISCHENSCHALTER, HELLIGKEIT, 20),
    }


@pytest.mark.usefixtures("kaffeemaschine")
async def test_licht_suche_bestaetigt_das_bisherige(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    steckdose: Callable[..., Awaitable[None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Findet die Suche nur das gespeicherte Licht, sagt sie das – samt Modell und Anlegedatum."""
    await steckdose()
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT)
    assert code == 0, fehler
    [vorher] = [a for a in gespeicherte_automationen(home_assistant) if a["id"] == installieren.LICHT_AUTOMATION_ID]

    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--licht")
    assert code == 0, fehler
    assert f"✓ Licht gesucht: {LICHT} („LED Kaffee“, eQ-3 HMIP-PSM, aus, neu angelegt am " in ausgabe
    assert "– wie bisher." in ausgabe
    assert "ist jetzt" not in ausgabe
    assert [a for a in gespeicherte_automationen(home_assistant) if a["id"] == installieren.LICHT_AUTOMATION_ID] == [vorher]


@pytest.mark.usefixtures("kaffeemaschine")
async def test_verschwundenes_licht_ohne_ersatz(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ohne Kandidaten bleibt die Licht-Automation stehen, mit Hinweis auf die Suche."""
    home_assistant.states.async_set(LICHT, "off", {"friendly_name": "LED Kaffee"})
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT)
    assert code == 0, fehler
    home_assistant.states.async_remove(LICHT)
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert f"⚠ Das gespeicherte Licht {LICHT} gibt es nicht mehr – die Licht-Automation bleibt unverändert." in ausgabe
    assert "--licht ohne Wert" in ausgabe
    assert "Infrage kommen" not in ausgabe
    assert len(gespeicherte_automationen(home_assistant)) == 2


@pytest.mark.usefixtures("kaffeemaschine")
async def test_altes_licht_nicht_verfuegbar_nennt_die_suche(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home_assistant.states.async_set(LICHT, "unavailable", {"friendly_name": "LED Kaffee"})
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT)
    assert code == 0, fehler
    assert f"⚠ {LICHT} ist zurzeit nicht verfügbar" in ausgabe
    assert "Ein neues Licht sucht --licht ohne Wert." in ausgabe


@pytest.mark.usefixtures("kaffeemaschine")
async def test_kein_licht_gefunden(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Konrads eigene Schalter zählen nicht als Licht – ohne weitere Geräte gibt es eine klare Meldung."""
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--licht")
    assert code == 1
    assert "Kein Licht an der Maschine in Home Assistant gefunden" in fehler
    assert "--licht <entity_id>" in fehler
    assert gespeicherte_automationen(home_assistant) == []


@pytest.mark.usefixtures("kaffeemaschine")
async def test_licht_suche_ohne_kaffee_im_namen(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Gibt es nichts mit „Kaffee“, zählt, was nach Licht oder Zwischenschalter klingt."""
    home_assistant.states.async_set("switch.zwischenschalter_theke", "off", {"friendly_name": "Zwischenschalter Theke"})
    home_assistant.states.async_set("switch.pumpe_garten", "off", {"friendly_name": "Pumpe Garten"})
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--licht")
    assert code == 0, fehler
    assert "✓ Licht an der Maschine: switch.zwischenschalter_theke („Zwischenschalter Theke“)" in ausgabe


@pytest.mark.usefixtures("kaffeemaschine")
async def test_deaktivierte_steckdose_wird_fuer_das_licht_aktiviert(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    steckdose: Callable[..., Awaitable[None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    await steckdose(deaktiviert=True)
    assert home_assistant.states.get(LICHT) is None

    with sofort_neuladen():
        code, ausgabe, fehler = await skript(
            home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT
        )
    assert code == 0, fehler
    assert f"{LICHT} ist deaktiviert – wird aktiviert." in ausgabe
    assert f"✓ {LICHT} aktiviert" in ausgabe
    assert er.async_get(home_assistant).async_get(LICHT).disabled_by is None
    assert home_assistant.states.get(LICHT).state == "off"
    assert len(gespeicherte_automationen(home_assistant)) == 2


@pytest.mark.usefixtures("kaffeemaschine")
async def test_nicht_erreichbare_steckdose_wird_trotzdem_eingetragen(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ist die Steckdose gerade ausgesteckt, warnt das Skript, legt die Automation aber an."""
    home_assistant.states.async_set(LICHT, "unavailable", {"friendly_name": "LED Kaffee"})
    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--probelauf"
    )
    assert code == 0, fehler
    assert f"⚠ {LICHT} ist zurzeit nicht verfügbar" in ausgabe
    assert "✓ Die Kaffeemaschine ist angegangen." in ausgabe
    assert f"⚠ {LICHT} ist nicht mit angegangen" in ausgabe
    assert "✓ Die Kaffeemaschine ist wieder aus" in ausgabe
    assert "wieder ausgegangen" not in ausgabe  # das Licht war nie an, also gibt es da nichts zu prüfen
    assert len(gespeicherte_automationen(home_assistant)) == 2


@pytest.mark.usefixtures("kaffeemaschine")
async def test_nur_anzeigen_mit_licht(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home_assistant.states.async_set(LICHT, "off", {"friendly_name": "LED Kaffee"})
    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--nur-anzeigen"
    )
    assert code == 0, fehler
    assert "zweite Automation für das Licht" in ausgabe
    assert '"action": "homeassistant.turn_on"' in ausgabe
    assert gespeicherte_automationen(home_assistant) == []


def test_licht_automation_wie_die_yaml_variante() -> None:
    """Das Skript richtet exakt die Automation aus beispiele/licht_folgt_kaffeemaschine.yaml ein."""
    erwartet = yaml.safe_load(LICHT_FOLGT_KAFFEEMASCHINE.read_text(encoding="utf-8"))
    assert installieren.licht_config(KAFFEEMASCHINE, LICHT) == erwartet


# --- Licht nur bei Dunkelheit (--dunkel) -------------------------------------


def helligkeit(
    hass: HomeAssistant, wert: str, entity_id: str = HELLIGKEIT, name: str = "Helligkeit Küche"
) -> None:
    """Ein Helligkeitssensor wie der des Bewegungsmelders – der Zustand ist in Home Assistant Text."""
    hass.states.async_set(
        entity_id, wert, {"device_class": "illuminance", "unit_of_measurement": "lx", "friendly_name": name}
    )


async def maschine(hass: HomeAssistant, an: bool) -> None:
    """Schaltet die Kaffeemaschine wie von Hand und wartet, bis die Automationen durch sind."""
    await hass.services.async_call(
        "switch", "turn_on" if an else "turn_off", {"entity_id": KAFFEEMASCHINE}, blocking=True
    )
    await hass.async_block_till_done()


def keine_rueckfrage() -> Any:
    return patch("builtins.input", side_effect=AssertionError("Es darf keine Rückfrage geben"))


def test_licht_automation_bei_dunkelheit_wie_die_yaml_variante() -> None:
    """Mit --dunkel richtet das Skript exakt beispiele/licht_folgt_kaffeemaschine_wenn_dunkel.yaml ein."""
    erwartet = yaml.safe_load(LICHT_WENN_DUNKEL.read_text(encoding="utf-8"))
    assert installieren.licht_config(KAFFEEMASCHINE, LICHT, HELLIGKEIT, 20) == erwartet


@pytest.mark.usefixtures("kaffeemaschine")
async def test_licht_nur_bei_dunkelheit(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    steckdose: Callable[..., Awaitable[None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Das Licht geht nur an, wenn der Sensor höchstens 20 lx meldet, und aus, wenn es hell wird."""
    await steckdose()
    helligkeit(home_assistant, "250")
    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel", HELLIGKEIT
    )
    assert code == 0, fehler
    assert (
        f"✓ Dunkel-Sensor: {HELLIGKEIT} („Helligkeit Küche“), aktuell 250 lx – heller als 20 lx bleibt das Licht aus"
        in ausgabe
    )
    assert "Das Licht an der Maschine geht mit ihr aus – und an, wenn es dunkel ist." in ausgabe
    automationen = {automation["id"]: automation for automation in gespeicherte_automationen(home_assistant)}
    assert automationen[installieren.LICHT_AUTOMATION_ID] == {
        "id": installieren.LICHT_AUTOMATION_ID,
        **installieren.licht_config(KAFFEEMASCHINE, LICHT, HELLIGKEIT, 20),
    }

    # Hell: Die Maschine geht an, das Licht bleibt aus.
    await maschine(home_assistant, True)
    assert home_assistant.states.get(LICHT).state == "off"
    # Es wird dunkel: Licht an. Es wird hell: Licht aus. Wieder dunkel: wieder an.
    helligkeit(home_assistant, "5")
    await home_assistant.async_block_till_done()
    assert home_assistant.states.get(LICHT).state == "on"
    helligkeit(home_assistant, "300")
    await home_assistant.async_block_till_done()
    assert home_assistant.states.get(LICHT).state == "off"
    helligkeit(home_assistant, "3")
    await home_assistant.async_block_till_done()
    assert home_assistant.states.get(LICHT).state == "on"
    # Maschine aus: Licht aus, egal wie dunkel.
    await maschine(home_assistant, False)
    assert home_assistant.states.get(LICHT).state == "off"
    # Ohne brauchbaren Messwert gilt dunkel – das Licht geht wie bisher mit an.
    for wert in ("unknown", "unavailable", "N/A"):
        helligkeit(home_assistant, wert)
        await maschine(home_assistant, True)
        assert home_assistant.states.get(LICHT).state == "on", wert
        await maschine(home_assistant, False)
        assert home_assistant.states.get(LICHT).state == "off", wert
    home_assistant.states.async_remove(HELLIGKEIT)
    await maschine(home_assistant, True)
    assert home_assistant.states.get(LICHT).state == "on"


@pytest.mark.usefixtures("kaffeemaschine")
async def test_dunkel_sensor_wird_gefunden(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dunkel ohne Wert nimmt den einzigen Helligkeitssensor von selbst, ohne Rückfrage."""
    home_assistant.states.async_set(LICHT, "off", {"friendly_name": "LED Kaffee"})
    helligkeit(home_assistant, "12", "sensor.flur_illumination", "Flur Illumination")
    with patch.object(sys.stdin, "isatty", return_value=True), keine_rueckfrage():
        code, ausgabe, fehler = await skript(
            home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel"
        )
    assert code == 0, fehler
    assert "✓ Dunkel-Sensor: sensor.flur_illumination („Flur Illumination“), aktuell 12 lx" in ausgabe


@pytest.mark.usefixtures("kaffeemaschine")
async def test_dunkel_sensor_kueche_wird_bevorzugt(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Gibt es genau einen Helligkeitssensor aus der Küche, wird er ohne Rückfrage genommen."""
    home_assistant.states.async_set(LICHT, "off", {"friendly_name": "LED Kaffee"})
    helligkeit(home_assistant, "40", "sensor.flur_illumination", "Flur Illumination")
    helligkeit(home_assistant, "8", "sensor.bewegungsmelder_kuche_illumination", "Bewegungsmelder Küche Illumination")
    home_assistant.states.async_set(
        "sensor.garten_helligkeit", "900", {"unit_of_measurement": "lux", "friendly_name": "Garten Helligkeit"}
    )
    with patch.object(sys.stdin, "isatty", return_value=True), keine_rueckfrage():
        code, ausgabe, fehler = await skript(
            home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel"
        )
    assert code == 0, fehler
    assert (
        "✓ Dunkel-Sensor: sensor.bewegungsmelder_kuche_illumination („Bewegungsmelder Küche Illumination“), "
        "aktuell 8 lx" in ausgabe
    )


@pytest.mark.usefixtures("kaffeemaschine")
async def test_mehrere_dunkel_sensoren_im_terminal_waehlen(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Zwei Küchen-Sensoren: ohne Terminal eine klare Meldung, im Terminal ein Menü."""
    home_assistant.states.async_set(LICHT, "off", {"friendly_name": "LED Kaffee"})
    helligkeit(home_assistant, "9", "sensor.kueche_average_illumination", "Küche Average Illumination")
    helligkeit(home_assistant, "8", "sensor.kueche_illumination", "Küche Illumination")

    code, _, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel"
    )
    assert code == 1
    assert "Mehrere Möglichkeiten für den Helligkeitssensor gefunden" in fehler
    assert "1) sensor.kueche_illumination" in fehler  # der Hauptwert vor dem Mittelwert
    assert "2) sensor.kueche_average_illumination" in fehler
    assert "--dunkel <entity_id>" in fehler
    assert gespeicherte_automationen(home_assistant) == []

    with (
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=["2"]),
    ):
        code, ausgabe, fehler = await skript(
            home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel"
        )
    assert code == 0, fehler
    assert "✓ Dunkel-Sensor: sensor.kueche_average_illumination" in ausgabe


@pytest.mark.usefixtures("kaffeemaschine")
async def test_deaktivierter_kuechen_sensor_wird_angeboten(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    steckdose: Callable[..., Awaitable[None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ist die Helligkeit des Küchen-Melders deaktiviert, bietet das Skript sie zum Aktivieren an."""
    await steckdose(helligkeit=7, sensor_deaktiviert=True)
    assert home_assistant.states.get(KUECHE_SENSOR) is None
    helligkeit(home_assistant, "0.71", "sensor.flur_og_bwm_beleuchtungsstarke", "Flur OG BWM Beleuchtungsstärke")

    # Ohne Terminal greift das Skript nicht blind zum Flur-Sensor, sondern nennt den deaktivierten.
    code, _, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel"
    )
    assert code == 1
    assert f"    {KUECHE_SENSOR}\n" in fehler
    assert "--dunkel <entity_id> angegeben, aktiviert dieses Skript sie selbst" in fehler
    assert "Aktive Helligkeitssensoren: sensor.flur_og_bwm_beleuchtungsstarke." in fehler
    assert gespeicherte_automationen(home_assistant) == []

    # Im Terminal, Enter: nichts aktivieren, dann bleibt der Flur-Sensor als einziger aktiver.
    with (
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=[""]),
    ):
        code, ausgabe, fehler = await skript(
            home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel"
        )
    assert code == 0, fehler
    assert "aktivieren und als Dunkel-Sensor verwenden?" in ausgabe
    assert f"1) {KUECHE_SENSOR}" in ausgabe
    assert "✓ Dunkel-Sensor: sensor.flur_og_bwm_beleuchtungsstarke" in ausgabe

    # Im Terminal, Nummer 1: Der Küchen-Sensor wird aktiviert und verwendet.
    with (
        sofort_neuladen(),
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=["1"]),
    ):
        code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--dunkel")
    assert code == 0, fehler
    assert f"✓ {KUECHE_SENSOR} aktiviert" in ausgabe
    assert (
        f"✓ Dunkel-Sensor: {KUECHE_SENSOR} („Bewegungsmelder Küche Beleuchtungsstärke“), aktuell 7 lx – "
        "heller als 20 lx bleibt das Licht aus" in ausgabe
    )
    assert home_assistant.states.get(KUECHE_SENSOR).state == "7"
    automationen = {automation["id"]: automation for automation in gespeicherte_automationen(home_assistant)}
    assert automationen[installieren.LICHT_AUTOMATION_ID]["triggers"][2]["entity_id"] == KUECHE_SENSOR


@pytest.mark.usefixtures("kaffeemaschine")
async def test_angegebener_deaktivierter_dunkel_sensor_wird_aktiviert(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    steckdose: Callable[..., Awaitable[None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    await steckdose(helligkeit=3, sensor_deaktiviert=True)
    with sofort_neuladen():
        code, ausgabe, fehler = await skript(
            home_assistant, client, capsys, "--token", hass_access_token,
            "--licht", LICHT, "--dunkel", KUECHE_SENSOR,
        )
    assert code == 0, fehler
    assert f"{KUECHE_SENSOR} ist deaktiviert – wird aktiviert." in ausgabe
    assert f"✓ Dunkel-Sensor: {KUECHE_SENSOR} („Bewegungsmelder Küche Beleuchtungsstärke“), aktuell 3 lx" in ausgabe
    assert er.async_get(home_assistant).async_get(KUECHE_SENSOR).disabled_by is None


@pytest.mark.usefixtures("kaffeemaschine")
async def test_helligkeit_ohne_einheit_wird_nach_namen_gefunden(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ein älterer Homematic-Melder meldet seine Helligkeit (0–255) ohne Einheit – der Name zählt."""
    home_assistant.states.async_set(LICHT, "off", {"friendly_name": "LED Kaffee"})
    home_assistant.states.async_set(
        "sensor.bewegungsmelder_kuche_helligkeit", "120", {"friendly_name": "Bewegungsmelder Küche Helligkeit"}
    )
    home_assistant.states.async_set("sensor.kuche_temperatur", "21.5", {"friendly_name": "Küche Temperatur"})
    with patch.object(sys.stdin, "isatty", return_value=True), keine_rueckfrage():
        code, ausgabe, fehler = await skript(
            home_assistant, client, capsys, "--token", hass_access_token,
            "--licht", LICHT, "--dunkel", "--dunkel-unter", "30",
        )
    assert code == 0, fehler
    assert "⚠ sensor.bewegungsmelder_kuche_helligkeit meldet keine Lux, sondern eine eigene Skala" in ausgabe
    assert (
        "✓ Dunkel-Sensor: sensor.bewegungsmelder_kuche_helligkeit („Bewegungsmelder Küche Helligkeit“), "
        "aktuell 120 – heller als 30 bleibt das Licht aus" in ausgabe
    )
    automationen = {automation["id"]: automation for automation in gespeicherte_automationen(home_assistant)}
    assert automationen[installieren.LICHT_AUTOMATION_ID]["triggers"][2] == {
        "trigger": "numeric_state", "entity_id": "sensor.bewegungsmelder_kuche_helligkeit", "above": 30, "id": "hell",
    }


@pytest.mark.usefixtures("kaffeemaschine")
async def test_kein_dunkel_sensor(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home_assistant.states.async_set(LICHT, "off", {"friendly_name": "LED Kaffee"})
    code, _, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel"
    )
    assert code == 1
    assert "Kein Helligkeitssensor in Home Assistant gefunden" in fehler
    assert "Geräteklasse „illuminance“ oder Einheit lx" in fehler
    assert "--dunkel <entity_id>" in fehler
    assert gespeicherte_automationen(home_assistant) == []


@pytest.mark.usefixtures("kaffeemaschine")
@pytest.mark.parametrize(
    ("wert", "meldung"),
    [
        ("sensor.gibt_es_nicht", "gibt es in Home Assistant nicht"),
        (LICHT, "aus dem Bereich „sensor“"),
        ("20", "--dunkel-unter 20"),
    ],
)
async def test_angegebener_dunkel_sensor_wird_geprueft(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
    wert: str,
    meldung: str,
) -> None:
    home_assistant.states.async_set(LICHT, "off", {"friendly_name": "LED Kaffee"})
    code, _, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel", wert
    )
    assert code == 1
    assert meldung in fehler
    assert gespeicherte_automationen(home_assistant) == []


@pytest.mark.usefixtures("kaffeemaschine")
async def test_dunkel_schwelle(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dunkel-unter setzt die Schwelle, auch mit Komma; sie landet als Zahl in der Automation."""
    home_assistant.states.async_set(LICHT, "off", {"friendly_name": "LED Kaffee"})
    helligkeit(home_assistant, "6")
    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token,
        "--licht", LICHT, "--dunkel", HELLIGKEIT, "--dunkel-unter", "5",
    )
    assert code == 0, fehler
    assert "aktuell 6 lx – heller als 5 lx bleibt das Licht aus" in ausgabe
    automationen = {automation["id"]: automation for automation in gespeicherte_automationen(home_assistant)}
    assert automationen[installieren.LICHT_AUTOMATION_ID] == {
        "id": installieren.LICHT_AUTOMATION_ID,
        **installieren.licht_config(KAFFEEMASCHINE, LICHT, HELLIGKEIT, 5),
    }
    assert automationen[installieren.LICHT_AUTOMATION_ID]["triggers"][2]["above"] == 5

    # Zweiter Aufruf nur mit neuer Schwelle: Licht und Sensor werden übernommen.
    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--dunkel-unter", "7,5"
    )
    assert code == 0, fehler
    assert "heller als 7.5 lx bleibt das Licht aus" in ausgabe
    automationen = {automation["id"]: automation for automation in gespeicherte_automationen(home_assistant)}
    assert automationen[installieren.LICHT_AUTOMATION_ID]["triggers"][2]["above"] == 7.5


@pytest.mark.usefixtures("kaffeemaschine")
async def test_zweiter_aufruf_uebernimmt_licht_und_dunkel(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Licht, Dunkel-Sensor und Schwelle kommen beim nächsten Aufruf aus der Licht-Automation."""
    home_assistant.states.async_set(LICHT, "off", {"friendly_name": "LED Kaffee"})
    helligkeit(home_assistant, "4")

    def licht_automation() -> dict[str, Any]:
        [automation] = [
            a for a in gespeicherte_automationen(home_assistant) if a["id"] == installieren.LICHT_AUTOMATION_ID
        ]
        return automation

    code, _, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token,
        "--licht", LICHT, "--dunkel", "--dunkel-unter", "10",
    )
    assert code == 0, fehler
    vorher = licht_automation()
    assert vorher["triggers"][2] == {"trigger": "numeric_state", "entity_id": HELLIGKEIT, "above": 10, "id": "hell"}

    # Ohne Optionen: keine Rückfrage, nichts geändert.
    with patch.object(sys.stdin, "isatty", return_value=True), keine_rueckfrage():
        code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token)
    assert code == 0, fehler
    assert "✓ Die Licht-Automation gibt es schon – Licht, Dunkel-Sensor und Schwelle werden übernommen" in ausgabe
    assert f"✓ Licht an der Maschine: {LICHT}" in ausgabe
    assert f"✓ Dunkel-Sensor: {HELLIGKEIT}" in ausgabe
    assert "heller als 10 lx" in ausgabe
    assert licht_automation() == vorher

    # Nur die Schwelle ändern.
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--dunkel-unter", "30")
    assert code == 0, fehler
    assert licht_automation() == {
        "id": installieren.LICHT_AUTOMATION_ID,
        **installieren.licht_config(KAFFEEMASCHINE, LICHT, HELLIGKEIT, 30),
    }

    # Dunkel-Bedingung wieder entfernen.
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--dunkel", "aus")
    assert code == 0, fehler
    assert "Das Licht an der Maschine geht mit ihr an und aus." in ausgabe
    assert licht_automation() == {"id": installieren.LICHT_AUTOMATION_ID, **installieren.licht_config(KAFFEEMASCHINE, LICHT)}

    # Und wieder dazu, mit Standardschwelle.
    with patch.object(sys.stdin, "isatty", return_value=True), keine_rueckfrage():
        code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--dunkel")
    assert code == 0, fehler
    assert f"✓ Dunkel-Sensor: {HELLIGKEIT}" in ausgabe
    assert "heller als 20 lx" in ausgabe

    code, _, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--dunkel", "aus", "--dunkel-unter", "5"
    )
    assert code == 1
    assert "--dunkel aus und --dunkel-unter passen nicht zusammen" in fehler


@pytest.mark.usefixtures("kaffeemaschine")
async def test_probelauf_bei_helligkeit(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    steckdose: Callable[..., Awaitable[None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ist es beim Probelauf hell, bleibt das Licht aus – und das Skript sagt, dass das so gehört."""
    await steckdose()
    helligkeit(home_assistant, "250")
    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token,
        "--licht", LICHT, "--dunkel", HELLIGKEIT, "--probelauf",
    )
    assert code == 0, fehler
    assert "✓ Die Kaffeemaschine ist angegangen." in ausgabe
    assert (
        f"ℹ Es ist hell ({HELLIGKEIT}: 250 lx, Licht nur bis 20 lx) – das Licht bleibt aus, so soll es sein."
        in ausgabe
    )
    assert "⚠" not in ausgabe
    assert "✓ Die Kaffeemaschine ist wieder aus" in ausgabe
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "off"
    assert home_assistant.states.get(LICHT).state == "off"


@pytest.mark.usefixtures("kaffeemaschine")
async def test_nur_anzeigen_mit_dunkel(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home_assistant.states.async_set(LICHT, "off", {"friendly_name": "LED Kaffee"})
    helligkeit(home_assistant, "1")
    code, ausgabe, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel", "--nur-anzeigen"
    )
    assert code == 0, fehler
    assert '"above": 20' in ausgabe
    assert '"stop": "Es ist hell – das Licht bleibt aus."' in ausgabe
    assert gespeicherte_automationen(home_assistant) == []


# --- Prüfung (--pruefen): warum ist das Licht an oder aus? -------------------


def fenster(stunden_von: int, stunden_bis: int) -> list[str]:
    """Ein Zeitfenster relativ zu jetzt, als Optionen für das Skript."""
    jetzt = datetime.now(BERLIN)  # Home Assistant rechnet in seiner Zeitzone
    von = (jetzt + timedelta(hours=stunden_von)).strftime("%H:%M")
    bis = (jetzt + timedelta(hours=stunden_bis)).strftime("%H:%M")
    return ["--von", von, "--bis", bis]


@pytest.mark.usefixtures("kaffeemaschine")
async def test_pruefen_erklaert_zu_hell(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    steckdose: Callable[..., Awaitable[None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bleibt das Licht aus, weil es hell ist, sagt --pruefen das – samt Messwert und letztem Lauf."""
    await steckdose()
    helligkeit(home_assistant, "250")
    code, _, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel", HELLIGKEIT
    )
    assert code == 0, fehler
    await maschine(home_assistant, True)
    assert home_assistant.states.get(LICHT).state == "off"

    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--pruefen")
    assert code == 0, fehler
    assert "Prüfung – Stand jetzt:" in ausgabe
    assert f"  Kaffeemaschine {KAFFEEMASCHINE}: an (seit " in ausgabe
    assert f"  Licht {LICHT} („LED Kaffee“): aus" in ausgabe
    assert f"  Dunkel-Sensor {HELLIGKEIT}: 250 lx, Grenze 20 lx – zuletzt gemeldet " in ausgabe
    assert "Urteil: Zu hell für das Licht (250 > 20)." in ausgabe
    assert "nur bei Bewegung" not in ausgabe  # der Wert ist frisch
    assert f"Letzte Läufe „{installieren.LICHT_ALIAS}“:" in ausgabe
    assert "  Maschine an → Es ist hell – das Licht bleibt aus (Helligkeit 250 > 20)." in ausgabe
    assert f"Letzte Läufe „{installieren.ALIAS}“:" in ausgabe
    assert len(gespeicherte_automationen(home_assistant)) == 2  # nichts geändert


@pytest.mark.usefixtures("kaffeemaschine")
async def test_pruefen_alter_sensorwert(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    steckdose: Callable[..., Awaitable[None]],
    freezer: FrozenDateTimeFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ein alter Helligkeitswert bekommt den Hinweis, dass der Melder nur bei Bewegung meldet."""
    await steckdose()
    helligkeit(home_assistant, "53")
    code, _, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel", HELLIGKEIT
    )
    assert code == 0, fehler
    freezer.tick(timedelta(minutes=5))
    await maschine(home_assistant, True)
    with patch("installieren.VERALTET_MINUTEN", 1):
        code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--pruefen")
    assert code == 0, fehler
    assert "(vor 5 Min.)" in ausgabe
    assert "Urteil: Zu hell für das Licht (53 > 20). Der Wert ist 5 Min. alt – der Melder meldet die Helligkeit nur bei Bewegung." in ausgabe


@pytest.mark.usefixtures("kaffeemaschine")
async def test_pruefen_licht_muesste_an_sein(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    steckdose: Callable[..., Awaitable[None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ist es dunkel und das Licht trotzdem aus, zeigt --pruefen auf das Gerät."""
    await steckdose()
    helligkeit(home_assistant, "3")
    code, _, fehler = await skript(
        home_assistant, client, capsys, "--token", hass_access_token, "--licht", LICHT, "--dunkel", HELLIGKEIT
    )
    assert code == 0, fehler

    async def ignorieren(self: Steckdose, **kwargs: Any) -> None:
        pass

    with patch.object(Steckdose, "async_turn_on", ignorieren):
        await maschine(home_assistant, True)
    assert home_assistant.states.get(LICHT).state == "off"

    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--pruefen")
    assert code == 0, fehler
    assert f"Urteil: Das Licht müsste an sein, ist aber aus. Prüfe in Home Assistant, ob sich {LICHT} von Hand" in ausgabe
    assert "  Maschine an → Licht eingeschaltet." in ausgabe  # die Automation hat es versucht


@pytest.mark.usefixtures("kaffeemaschine")
async def test_pruefen_ohne_automation(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--pruefen")
    assert code == 0, fehler
    assert "ist noch nicht eingerichtet" in ausgabe
    assert gespeicherte_automationen(home_assistant) == []


@pytest.mark.usefixtures("kaffeemaschine")
async def test_pruefen_hauptautomation(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Auch die Kaffee-Automation wird erklärt: außerhalb des Zeitfensters, eingeschaltet, schon an."""
    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, *fenster(2, 3))
    assert code == 0, fehler
    home_assistant.states.async_set(ALARMANLAGE, "disarmed")
    await home_assistant.async_block_till_done()
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "off"

    code, _, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, *fenster(-1, 1))
    assert code == 0, fehler
    home_assistant.states.async_set(ALARMANLAGE, "armed_night")
    home_assistant.states.async_set(ALARMANLAGE, "disarmed")
    await home_assistant.async_block_till_done()
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "on"
    home_assistant.states.async_set(ALARMANLAGE, "armed_night")
    home_assistant.states.async_set(ALARMANLAGE, "disarmed")
    await home_assistant.async_block_till_done()

    code, ausgabe, fehler = await skript(home_assistant, client, capsys, "--token", hass_access_token, "--pruefen")
    assert code == 0, fehler
    assert "Urteil: Es ist kein Licht an der Maschine eingerichtet (--licht)." in ausgabe
    laeufe = [zeile for zeile in ausgabe.splitlines() if zeile.startswith("  ") and "Alarmanlage → disarmed" in zeile]
    assert [zeile.split(" → ", 1)[1] for zeile in laeufe] == [
        "disarmed → Kaffeemaschine war schon an.",
        "disarmed → Kaffeemaschine eingeschaltet.",
        "disarmed → außerhalb des Zeitfensters.",
    ]


def test_lauf_erklaeren() -> None:
    """Die Läufe aus der Ablaufverfolgung werden zu einem Satz – auch bei Fehlern und lückenhaften Daten."""
    def lauf(trace: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {"timestamp": {"start": "2026-11-14T16:02:11.123456+00:00"}, "trace": trace, **extra}

    def ausloeser(kennung: str, zustand: str) -> dict[str, Any]:
        return {"trigger/0": [{"changed_variables": {"trigger": {"id": kennung, "to_state": {"state": zustand}}}}]}

    hell = lauf({
        **ausloeser("an", "on"),
        "action/0": [{"result": {"choice": 1}}],
        "action/0/choose/1/conditions/1/entity_id/0": [{"result": {"result": True, "state": 250.0, "wanted_state_above": 20.0}}],
    })
    assert installieren.lauf_erklaeren(hell, "licht").endswith("  Maschine an → Es ist hell – das Licht bleibt aus (Helligkeit 250 > 20).")
    assert installieren.lauf_erklaeren(hell, "licht").startswith("  14.11. ")
    # Traf die Bedingung zu, nennt Home Assistant nur den Messwert – die Schwelle kommt dann von uns.
    hell_ohne_schwelle = lauf({
        **ausloeser("an", "on"),
        "action/0": [{"result": {"choice": 1}}],
        "action/0/choose/1/conditions/1/entity_id/0": [{"result": {"result": True, "state": 250.0}}],
    })
    assert installieren.lauf_erklaeren(hell_ohne_schwelle, "licht", 20).endswith("(Helligkeit 250 > 20).")
    assert installieren.lauf_erklaeren(hell_ohne_schwelle, "licht").endswith("das Licht bleibt aus.")
    dunkel = lauf({**ausloeser("dunkel", "3.0"), "action/0": [{"result": {"choice": 2}}]})
    assert installieren.lauf_erklaeren(dunkel, "licht").endswith("  Helligkeit 3.0 (dunkel) → Licht eingeschaltet.")
    aus = lauf({**ausloeser("aus", "off"), "action/0": [{"result": {"choice": 0}}]})
    assert installieren.lauf_erklaeren(aus, "licht").endswith("  Maschine aus → Licht ausgeschaltet.")
    einfach = lauf({**ausloeser("an", "on"), "action/0": [{"result": {"choice": "then"}}]})
    assert installieren.lauf_erklaeren(einfach, "licht").endswith("  Maschine an → Licht eingeschaltet.")
    nichts = lauf({**ausloeser("hell", "80")}, trigger="numeric state of sensor.x")
    assert installieren.lauf_erklaeren(nichts, "licht").endswith("  Helligkeit 80 (hell) → nichts zu tun (Maschine aus).")
    kaputt = lauf({**ausloeser("an", "on"), "action/0": [{"result": {"choice": 2}}]}, error="Dienst nicht gefunden", script_execution="error")
    assert installieren.lauf_erklaeren(kaputt, "licht").endswith("Licht eingeschaltet – Fehler: Dienst nicht gefunden.")
    leer = {"timestamp": {}, "trace": {}, "trigger": "state of sensor.alarm"}
    assert installieren.lauf_erklaeren(leer, "kaffee") == "  unbekannte Zeit  state of sensor.alarm → Bedingung nicht erfüllt."
    zeitfenster = lauf({**ausloeser(None, "disarmed"), "condition/0": [{"result": {"result": False}}]})
    assert installieren.lauf_erklaeren(zeitfenster, "kaffee").endswith("  Alarmanlage → disarmed → außerhalb des Zeitfensters.")


@pytest.mark.parametrize(
    ("minuten", "erwartet"),
    [(0, "vor 0 Min."), (5, "vor 5 Min."), (90, "vor 1 Std. 30 Min."), (120, "vor 2 Std."), (1440, "vor 1 Tag"), (3000, "vor 2 Tagen")],
)
def test_alter_text(minuten: int, erwartet: str) -> None:
    assert installieren.alter_text(minuten) == erwartet


def test_zeit_lesen() -> None:
    zeit = installieren.zeit_lesen("2026-11-14T16:02:11.1234567+00:00")  # 7 Nachkommastellen, wie sie vorkommen können
    assert zeit is not None and zeit.utcoffset() is not None
    assert installieren.zeit_lesen("2026-11-14T16:02:11Z").hour == installieren.zeit_lesen("2026-11-14T16:02:11+00:00").hour
    assert installieren.zeit_lesen("gestern") is None and installieren.zeit_lesen(None) is None


# --- Kleinkram ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "sensor.projekt_nas_volume_1_durchschnittliche_festplattentemperatur",  # „latte“
        "sensor.hm_wds30_ot2_sm_2_ueq1925063_temperatur_ch1",  # „eq19…“
        "HM-WDS100-C6-O_MEQ0655167",
        "switch.steckdose_flur",
        "sensor.milkyway_helligkeit",
    ],
)
def test_keine_kaffeemaschine_erkannt(text: str) -> None:
    """Zufällige Buchstabenfolgen wie „Festplatte“ oder Seriennummern sind keine Kaffeemaschine."""
    assert not installieren.KAFFEEMASCHINE.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "Kaffeevollautomat",
        "switch.kaffeemaschine_power",
        "sensor.kueche_coffee_counter",
        "binary_sensor.buro_bean_container_empty",
        "Bohnenbehälter leer",
        "EQ.9 plus",
        "switch.eq9_power",
        "sensor.latte_macchiato_counter",
        "Milchbehälter",
    ],
)
def test_kaffeemaschine_erkannt(text: str) -> None:
    assert installieren.KAFFEEMASCHINE.search(text)


def test_gleiche_automation_wie_die_yaml_variante() -> None:
    """Das Skript richtet exakt die Automation aus beispiele/automation_ohne_blueprint.yaml ein."""
    erwartet = yaml.safe_load(AUTOMATION_OHNE_BLUEPRINT.read_text(encoding="utf-8"))
    assert installieren.automation_config(ALARMANLAGE, KAFFEEMASCHINE, "05:00:00", "09:00:00") == erwartet


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [("5:00", "05:00:00"), ("05:00", "05:00:00"), ("09:00:00", "09:00:00"), (" 23:59 ", "23:59:00")],
)
def test_uhrzeit(eingabe: str, erwartet: str) -> None:
    assert installieren.uhrzeit(eingabe) == erwartet


@pytest.mark.parametrize("eingabe", ["24:00", "5", "5:60", "morgens"])
def test_ungueltige_uhrzeit(eingabe: str) -> None:
    with pytest.raises(Exception, match="Uhrzeit"):
        installieren.uhrzeit(eingabe)


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        ("homeassistant.local:8123", "http://homeassistant.local:8123"),
        ("https://xyz.ui.nabu.casa/", "https://xyz.ui.nabu.casa"),
        (" http://192.168.178.20:8123 ", "http://192.168.178.20:8123"),
    ],
)
def test_url_bereinigen(eingabe: str, erwartet: str) -> None:
    assert installieren.url_bereinigen(eingabe) == erwartet


def test_licht_uebernehmen_ist_nachsichtig() -> None:
    """Auch eine bearbeitete Licht-Automation liefert, was noch lesbar ist."""
    mit = installieren.licht_config(KAFFEEMASCHINE, LICHT, HELLIGKEIT, 12.5)
    assert installieren.licht_uebernehmen(mit) == {"licht": LICHT, "dunkel": HELLIGKEIT, "dunkel_unter": 12.5}
    assert installieren.licht_uebernehmen(installieren.licht_config(KAFFEEMASCHINE, LICHT)) == {"licht": LICHT}
    mit["actions"][0]["choose"][1]["conditions"][1]["above"] = "20"  # von Hand: Text statt Zahl
    assert installieren.licht_uebernehmen(mit) == {"licht": LICHT}
    assert installieren.licht_uebernehmen({"actions": "kaputt"}) == {}
    assert installieren.licht_uebernehmen({}) == {}


@pytest.mark.parametrize(
    "text",
    [
        "sensor.bewegungsmelder_kuche_beleuchtungsstarke",
        "Beleuchtungsstärke",
        "sensor.hmip_smo_illumination",
        "sensor.hue_motion_light_level",
        "sensor.flur_brightness",
        "Helligkeit Küche",
        "sensor.garten_lux",
    ],
)
def test_helligkeitswoerter(text: str) -> None:
    assert installieren.HELLIGKEIT_WOERTER.search(text)


@pytest.mark.parametrize("text", ["sensor.kuche_temperatur", "sensor.luxemburg_wetter", "switch.led_kaffee"])
def test_keine_helligkeitswoerter(text: str) -> None:
    assert not installieren.HELLIGKEIT_WOERTER.search(text)


def test_kandidaten_licht_sortierung() -> None:
    """Kaffee-Bezug zuerst, verfügbare vor nicht verfügbaren, neu vor alt – Konrad und die CCU nie."""
    from types import SimpleNamespace

    geraete = {"hc": {"id": "hc"}, "ccu": {"id": "ccu"}, "psm": {"id": "psm", "manufacturer": "eQ-3", "model": "HMIP-PSM"}}
    entitaeten = {
        "switch.konrad_einschalter": {"entity_id": "switch.konrad_einschalter", "device_id": "hc", "created_at": 900},
        "switch.raspberrymatic_p_kaffee": {"entity_id": "switch.raspberrymatic_p_kaffee", "device_id": "ccu", "created_at": 900},
        "switch.led_kaffee": {"entity_id": "switch.led_kaffee", "device_id": "psm", "created_at": 100},
        "switch.zwischenschalter_kaffee": {"entity_id": "switch.zwischenschalter_kaffee", "created_at": 200},
        "switch.kaffee_alt": {"entity_id": "switch.kaffee_alt", "created_at": 300},
    }
    register = SimpleNamespace(
        entitaeten=entitaeten,
        geraete=geraete,
        ist_home_connect=lambda geraet: geraet["id"] == "hc",
        ist_gross=lambda geraet: geraet["id"] == "ccu",
    )

    def zustand(entity_id: str, state: str, name: str) -> dict[str, Any]:
        return {"entity_id": entity_id, "state": state, "attributes": {"friendly_name": name}}

    zustaende = {
        "switch.konrad_einschalter": zustand("switch.konrad_einschalter", "off", "Konrad Einschalter"),
        "switch.raspberrymatic_p_kaffee": zustand("switch.raspberrymatic_p_kaffee", "off", "Kaffee am Morgen"),
        "switch.led_kaffee": zustand("switch.led_kaffee", "off", "LED Kaffee"),
        "switch.zwischenschalter_kaffee": zustand("switch.zwischenschalter_kaffee", "off", "Zwischenschalter Kaffee"),
        "switch.kaffee_alt": zustand("switch.kaffee_alt", "unavailable", "Kaffee alt"),
        "light.flur": zustand("light.flur", "on", "Flur"),
        "sensor.kaffee_zaehler": zustand("sensor.kaffee_zaehler", "5", "Kaffee Zähler"),
    }
    assert installieren.kandidaten_licht(register, zustaende, "switch.konrad_einschalter") == [
        ("switch.zwischenschalter_kaffee", "„Zwischenschalter Kaffee“, aus, angelegt 01.01.1970"),
        ("switch.led_kaffee", "„LED Kaffee“, eQ-3 HMIP-PSM, aus, angelegt 01.01.1970"),
        ("switch.kaffee_alt", "„Kaffee alt“, nicht verfügbar, angelegt 01.01.1970"),
    ]
    # Ohne Kaffee-Bezug: erst, was nach Licht klingt; sonst alle.
    for entity_id in ("switch.led_kaffee", "switch.zwischenschalter_kaffee", "switch.kaffee_alt"):
        del zustaende[entity_id]
    assert installieren.kandidaten_licht(register, zustaende, "switch.konrad_einschalter") == [
        ("light.flur", "„Flur“, an")
    ]
    zustaende["light.flur"]["attributes"]["friendly_name"] = "Flur"
    zustaende["switch.pumpe"] = zustand("switch.pumpe", "off", "Pumpe")
    assert [e for e, _ in installieren.kandidaten_licht(register, zustaende)] == ["light.flur"]
    del zustaende["light.flur"]
    assert [e for e, _ in installieren.kandidaten_licht(register, zustaende)] == ["switch.pumpe"]
    # Ein frisch angelegtes Gerät wird als „neu angelegt“ beschrieben.
    import time

    entitaeten["switch.pumpe"] = {"entity_id": "switch.pumpe", "created_at": time.time() - 3600}
    [(_, beschreibung)] = installieren.kandidaten_licht(register, zustaende)
    assert beschreibung.startswith("„Pumpe“, aus, neu angelegt am ")


def test_deaktivierte_helligkeitssensoren_kueche_zuerst() -> None:
    """Aus dem Register: nur deaktivierte Sensoren mit Helligkeits-Namen, Küche vor Flur, Nebenwerte zuletzt."""
    from types import SimpleNamespace

    def eintrag(entity_id: str, name: str = "", deaktiviert: bool = True) -> dict[str, Any]:
        return {"entity_id": entity_id, "original_name": name, "disabled_by": "integration" if deaktiviert else None}

    register = SimpleNamespace(
        entitaeten={
            e["entity_id"]: e
            for e in (
                eintrag("sensor.flur_og_bwm_beleuchtungsstarke", "Beleuchtungsstärke"),
                eintrag("sensor.bewegungsmelder_kuche_average_illumination", "Average Illumination"),
                eintrag("sensor.bewegungsmelder_kuche_beleuchtungsstarke", "Beleuchtungsstärke"),
                eintrag("sensor.kuche_temperatur", "Temperatur"),
                eintrag("switch.kuche_beleuchtung", "Beleuchtung"),
                eintrag("sensor.terrasse_beleuchtungsstarke", "Beleuchtungsstärke", deaktiviert=False),
            )
        },
        entitaetsname=installieren.Register.entitaetsname,
    )
    assert installieren.deaktivierte_helligkeitssensoren(register) == [
        "sensor.bewegungsmelder_kuche_beleuchtungsstarke",
        "sensor.bewegungsmelder_kuche_average_illumination",
        "sensor.flur_og_bwm_beleuchtungsstarke",
    ]


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [("20", 20), ("20.5", 20.5), ("7,5", 7.5), ("20 lx", 20), ("0", 0), (" 15 Lux ", 15)],
)
def test_lux(eingabe: str, erwartet: "int | float") -> None:
    assert installieren.lux(eingabe) == erwartet
    assert type(installieren.lux(eingabe)) is type(erwartet)  # 20, nicht 20.0 – sonst stünde 20.0 in der Automation


@pytest.mark.parametrize("eingabe", ["dunkel", "-5", "", "nan", "inf", "sensor.x"])
def test_ungueltiger_lux(eingabe: str) -> None:
    with pytest.raises(Exception, match="Lux"):
        installieren.lux(eingabe)


# --- Dashboard für die Kaffeemaschine (--getraenke) --------------------------

KAFFEE_DASHBOARD = REPO / "beispiele" / "kaffee_dashboard.yaml"
KAFFEE_SKRIPTE = REPO / "beispiele" / "kaffee_skripte.yaml"
AKTIV = "select.kaffeevollautomat_aktives_programm"
GEWAEHLT = "select.kaffeevollautomat_ausgewahltes_programm"
BOHNENMENGE = "select.kaffeevollautomat_bohnenmenge"
FUELLMENGE = "number.kaffeevollautomat_fullmenge"
MEHRERE_GETRAENKE = "switch.kaffeevollautomat_mehrere_getranke"
WASSERTANK = "sensor.kaffeevollautomat_wassertank_leer"
GETRAENK = "input_select.kaffee_getrank"
STAERKE = "input_select.kaffee_starke"
MENGE = "input_number.kaffee_menge"
DOPPELT = "input_boolean.kaffee_doppelt"
JENS_1 = "input_text.lieblingsgetrank_jens_1"
JENS_1_DATEN = "input_text.lieblingsgetrank_jens_1_daten"
CAPPUCCINO = installieren.PROGRAMM_PRAEFIX + "beverage_cappuccino"
ESPRESSO = installieren.PROGRAMM_PRAEFIX + "beverage_espresso"
STARK = installieren.ENUM_PRAEFIX + "bean_amount_strong"
# Konrad meldet 33 Getränke – ohne die beiden Cold-Brew-Programme neuerer Modelle.
KONRAD_PROGRAMME = [installieren.PROGRAMM_PRAEFIX + k for k in list(installieren.GETRAENKE_NAMEN)[:33]]
BOHNENMENGEN = [f"{installieren.ENUM_PRAEFIX}bean_amount_{endung}" for _, endung in installieren.STAERKEN]


class Konrad:
    """Der Zustand der Kaffeemaschine hinter ihren Home-Connect-Entitäten.

    ``fernstart``: ob die Maschine Fernstart erlaubt. ``programme_erst_wenn_an``:
    die Getränkeliste ist erst bekannt, wenn die Maschine an ist (so, als wäre sie
    mit Home Assistant noch nie an gewesen).
    """

    def __init__(self, fernstart: bool = True, programme_erst_wenn_an: bool = False) -> None:
        self.an = False
        self.laeuft = False
        self.gewaehlt: str | None = None
        self.aktiv: str | None = None
        self.fernstart = fernstart
        self.programme_erst_wenn_an = programme_erst_wenn_an
        self.aufrufe: list[tuple[str, dict[str, Any]]] = []  # Aufrufe der Dienste home_connect.*
        self.entitaeten: list[Entity] = []  # die gerade in Home Assistant geladenen

    @property
    def programme(self) -> list[str]:
        return [] if self.programme_erst_wenn_an and not self.an else KONRAD_PROGRAMME

    def aktualisieren(self) -> None:
        for entitaet in list(self.entitaeten):
            entitaet.async_write_ha_state()


class KonradEntitaet(Entity):
    """Gemeinsames aller Entitäten der nachgebildeten Kaffeemaschine: Gerät, Name, translation_key."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, konrad: Konrad, name: str, schluessel: str) -> None:
        self.konrad = konrad
        self.schluessel = schluessel
        self._attr_name = name
        self._attr_translation_key = schluessel
        self._attr_unique_id = f"Kaffeevollautomat-{schluessel}"
        self._attr_device_info = DeviceInfo(
            identifiers={(HOME_CONNECT, "Kaffeevollautomat")},
            name="Kaffeevollautomat",
            manufacturer="SIEMENS",
            model="TQ903D03",
        )

    async def async_added_to_hass(self) -> None:
        self.konrad.entitaeten.append(self)

    async def async_will_remove_from_hass(self) -> None:
        self.konrad.entitaeten.remove(self)


class KonradSchalter(KonradEntitaet, SwitchEntity):
    """Einschalter, Kindersicherung, Tassenwärmer und die Option „Mehrere Getränke“."""

    def __init__(self, konrad: Konrad, name: str, schluessel: str) -> None:
        super().__init__(konrad, name, schluessel)
        self._an = False

    @property
    def is_on(self) -> bool:
        return self.konrad.an if self.schluessel == "power" else self._an

    @property
    def available(self) -> bool:
        return self.konrad.an if self.schluessel == "multiple_beverages" else True

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.schluessel == "power":
            self.konrad.an = True
        else:
            self._an = True
        self.konrad.aktualisieren()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self.schluessel == "power":
            self.konrad.an = False
            self.konrad.laeuft = False
        else:
            self._an = False
        self.konrad.aktualisieren()


class KonradAuswahl(KonradEntitaet, SelectEntity):
    """Aktives/ausgewähltes Programm und die Optionen mit Auswahlwerten (Bohnenmenge …)."""

    def __init__(self, konrad: Konrad, name: str, schluessel: str, optionen: list[str] | None = None) -> None:
        super().__init__(konrad, name, schluessel)
        self._optionen = optionen
        self._gewaehlt: str | None = None

    @property
    def options(self) -> list[str]:
        return list(self._optionen) if self._optionen is not None else self.konrad.programme

    @property
    def available(self) -> bool:
        return True if self._optionen is None else self.konrad.an  # Optionen gibt es nur bei laufender Maschine

    @property
    def current_option(self) -> str | None:
        if self.schluessel == "active_program":
            return self.konrad.aktiv if self.konrad.laeuft else None
        if self.schluessel == "selected_program":
            return self.konrad.gewaehlt
        return self._gewaehlt

    async def async_select_option(self, option: str) -> None:
        if self.schluessel == "active_program":
            self.konrad.aktiv, self.konrad.laeuft = option, True
        elif self.schluessel == "selected_program":
            self.konrad.gewaehlt = option
        else:
            self._gewaehlt = option
        self.konrad.aktualisieren()


class KonradZahl(KonradEntitaet, NumberEntity):
    """Die Füllmenge – mit dem Bereich des ausgewählten Getränks (hier 35–300 ml)."""

    _attr_native_min_value = 35
    _attr_native_max_value = 300
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "ml"
    _attr_native_value = 120

    @property
    def available(self) -> bool:
        return self.konrad.an

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.konrad.aktualisieren()


class KonradSensor(KonradEntitaet, SensorEntity):
    """Betriebszustand, Fortschritt, Zähler und Wartungshinweise (letztere ab Werk deaktiviert)."""

    def __init__(
        self, konrad: Konrad, name: str, schluessel: str, wert: Any = None, deaktiviert: bool = False
    ) -> None:
        super().__init__(konrad, name, schluessel)
        self._wert = wert
        self._attr_entity_registry_enabled_default = not deaktiviert

    @property
    def native_value(self) -> Any:
        if self.schluessel == "operation_state":
            return "inactive" if not self.konrad.an else ("run" if self.konrad.laeuft else "ready")
        return self._wert


class KonradFernstart(KonradEntitaet, BinarySensorEntity):
    @property
    def is_on(self) -> bool:
        return self.konrad.fernstart


class KonradTaste(KonradEntitaet, ButtonEntity):
    async def async_press(self) -> None:
        self.konrad.laeuft = False
        self.konrad.aktualisieren()


def konrad_entitaeten(konrad: Konrad) -> dict[str, list[Entity]]:
    """Alle Entitäten der nachgebildeten Kaffeemaschine je Plattform – mit deutschen Namen wie in Home Assistant."""
    return {
        "switch": [
            KonradSchalter(konrad, "Einschalter", "power"),
            KonradSchalter(konrad, "Kindersicherung", "child_lock"),
            KonradSchalter(konrad, "Tassenwärmer", "cup_warmer"),
            KonradSchalter(konrad, "Mehrere Getränke", "multiple_beverages"),
        ],
        "select": [
            KonradAuswahl(konrad, "Aktives Programm", "active_program"),
            KonradAuswahl(konrad, "Ausgewähltes Programm", "selected_program"),
            KonradAuswahl(konrad, "Bohnenmenge", "bean_amount", BOHNENMENGEN),
            KonradAuswahl(
                konrad,
                "Kaffee-Temperatur",
                "coffee_temperature",
                [f"{installieren.ENUM_PRAEFIX}coffee_temperature_{e}" for _, e in installieren.TEMPERATUREN],
            ),
            KonradAuswahl(
                konrad,
                "Kaffee-Milch-Verhältnis",
                "coffee_milk_ratio",
                [f"{installieren.ENUM_PRAEFIX}coffee_milk_ratio_{e}" for _, e in installieren.MILCHANTEILE],
            ),
            KonradAuswahl(
                konrad,
                "Durchflussrate",
                "flow_rate",
                [f"{installieren.ENUM_PRAEFIX}flow_rate_{e}" for _, e in installieren.INTENSITAETEN],
            ),
        ],
        "number": [KonradZahl(konrad, "Füllmenge", "fill_quantity")],
        "sensor": [
            KonradSensor(konrad, "Betriebszustand", "operation_state"),
            KonradSensor(konrad, "Programm-Fortschritt", "program_progress", 0),
            KonradSensor(konrad, "Programm-Endzeit", "program_finish_time"),
            KonradSensor(konrad, "Kaffees", "coffee_counter", 1234),
            KonradSensor(konrad, "Wassertank leer", "water_tank_empty", "off", deaktiviert=True),
            KonradSensor(konrad, "Tropfschale voll", "drip_tray_full", "off", deaktiviert=True),
        ],
        "binary_sensor": [KonradFernstart(konrad, "Fernstart", "remote_start")],
        "button": [KonradTaste(konrad, "Programm stoppen", "stop_program")],
    }


def konrad_dienste(hass: HomeAssistant, konrad: Konrad) -> None:
    """Die Dienste home_connect.set_program_and_options und start_selected_program – sie merken sich ihre Aufrufe."""

    async def programm_setzen(call: ServiceCall) -> None:
        konrad.aufrufe.append(("set_program_and_options", dict(call.data)))
        if call.data.get("affects_to") == "selected_program":
            konrad.gewaehlt = call.data["program"]
        else:
            konrad.aktiv, konrad.laeuft = call.data["program"], True
        konrad.aktualisieren()

    async def starten(call: ServiceCall) -> None:
        konrad.aufrufe.append(("start_selected_program", dict(call.data)))
        konrad.aktiv, konrad.laeuft = konrad.gewaehlt, True
        konrad.aktualisieren()

    hass.services.async_register(HOME_CONNECT, "set_program_and_options", programm_setzen)
    hass.services.async_register(HOME_CONNECT, "start_selected_program", starten)


@pytest.fixture
async def konrad(home_assistant: HomeAssistant, home_connect: HomeConnectEinrichten) -> Konrad:
    """Die Kaffeemaschine mit allen Entitäten der Home-Connect-Integration, ausgeschaltet."""
    maschine = Konrad()
    await home_connect({**KAFFEEVOLLAUTOMAT, **GESCHIRRSPUELER_GERAET}, konrad=maschine)
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "off"
    assert home_assistant.states.get(AKTIV).attributes["options"] == KONRAD_PROGRAMME
    assert home_assistant.states.get(WASSERTANK) is None  # ab Werk deaktiviert
    return maschine


@contextmanager
def schnell() -> Iterator[None]:
    """Keine Wartepausen im Skript und im Ablauf – die Nachbildung antwortet sofort."""
    with (
        patch.object(installieren, "OPTIONEN_PAUSE", 0),
        patch.object(installieren, "VORBEREITUNG_PAUSE", 0),
        sofort_neuladen(),
    ):
        yield


async def dashboard_einrichten(
    hass: HomeAssistant, client: TestClient, capsys: pytest.CaptureFixture[str], token: str, *argv: str
) -> tuple[int, str, str]:
    """Führt installieren.py --getraenke aus."""
    with schnell():
        return await skript(hass, client, capsys, "--token", token, "--getraenke", *argv)


async def ws_befehl(hass_ws_client: WebSocketGenerator, hass: HomeAssistant, **nachricht: Any) -> Any:
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, **nachricht})
    antwort = await ws.receive_json()
    assert antwort["success"], antwort
    return antwort["result"]


def geraete_id(hass: HomeAssistant) -> str:
    """Die Geräte-ID der Kaffeemaschine, wie home_connect.set_program_and_options sie braucht."""
    eintrag = er.async_get(hass).async_get(KAFFEEMASCHINE)
    assert eintrag is not None and eintrag.device_id
    return eintrag.device_id


async def dienst(hass: HomeAssistant, domain: str, name: str, **daten: Any) -> None:
    await hass.services.async_call(domain, name, daten, blocking=True)
    await hass.async_block_till_done()


async def zusammenstellen(hass: HomeAssistant, getraenk: str, staerke: str, menge: float, doppelt: bool) -> None:
    """Wie auf dem Dashboard: Getränk, Stärke, Menge und Doppelt einstellen."""
    await dienst(hass, "input_select", "select_option", entity_id=GETRAENK, option=getraenk)
    await dienst(hass, "input_select", "select_option", entity_id=STAERKE, option=staerke)
    await dienst(hass, "input_number", "set_value", entity_id=MENGE, value=menge)
    await dienst(hass, "input_boolean", "turn_on" if doppelt else "turn_off", entity_id=DOPPELT)


async def test_dashboard_wird_angelegt_und_bereitet_zu(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    hass_ws_client: WebSocketGenerator,
    konrad: Konrad,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--getraenke legt Helfer, Skripte und das Dashboard an; „Jetzt zubereiten“ schaltet ein, wählt, stellt ein, startet."""
    code, ausgabe, fehler = await dashboard_einrichten(
        home_assistant, client, capsys, hass_access_token, "--personen", "Jens,Nicole"
    )
    assert code == 0, fehler
    assert f"✓ Kaffeemaschine: {KAFFEEMASCHINE}" in ausgabe
    assert "✓ Kaffeemaschine „Kaffeevollautomat“: 19 Entitäten, davon 16 fürs Dashboard" in ausgabe
    assert f"✓ 2 Wartungshinweise/Zähler aktiviert: {WASSERTANK.replace('wassertank_leer', 'tropfschale_voll')}, {WASSERTANK}" in ausgabe
    assert "✓ 33 Getränke, 3 Wartungshinweise und Zähler" in ausgabe
    assert "✓ 19 Helfer angelegt bzw. aktualisiert" in ausgabe
    assert "✓ 3 Skripte angelegt bzw. aktualisiert: script.kaffee_zubereiten, script.lieblingsgetraenk_speichern, script.lieblingsgetraenk_zubereiten" in ausgabe
    assert "✓ Dashboard „Kaffeevollautomat“ angelegt: http://" in ausgabe
    assert "/kaffee-konrad" in ausgabe
    assert "3 Plätzen für Jens, 3 Plätzen für Nicole" in ausgabe
    assert "ist wieder ausgeschaltet" not in ausgabe  # die Maschine war aus und blieb aus

    # Helfer
    getraenk = home_assistant.states.get(GETRAENK)
    assert getraenk.attributes["options"][:3] == ["Espresso", "Espresso Doppio", "Caffè Crema"]  # wie am Display
    assert len(getraenk.attributes["options"]) == 33
    assert home_assistant.states.get(STAERKE).attributes["options"][:2] == [installieren.WIE_MASCHINE, "sehr mild"]
    assert home_assistant.states.get("input_select.kaffee_temperatur").attributes["options"][1] == "88 °C"
    assert home_assistant.states.get("input_select.kaffee_milchanteil").attributes["options"][-1] == "90 %"
    assert home_assistant.states.get("input_select.kaffee_intensitat").attributes["options"] == [
        installieren.WIE_MASCHINE, "Normal", "Intensiv", "Intensiv+"
    ]
    assert home_assistant.states.get("input_select.kaffee_bohnenbehalter") is None  # hat Konrad nicht
    assert home_assistant.states.get(MENGE).attributes["max"] == 500
    assert home_assistant.states.get(DOPPELT).state == "off"
    assert home_assistant.states.get(JENS_1).state == installieren.PLATZ_FREI
    assert home_assistant.states.get(JENS_1_DATEN).state == "unknown"  # noch nichts gespeichert
    assert home_assistant.states.get("input_text.lieblingsgetrank_nicole_3").state == installieren.PLATZ_FREI
    assert home_assistant.states.get("input_text.lieblingsgetrank_nicole_4") is None
    # Wartungshinweise sind jetzt aktiv
    assert er.async_get(home_assistant).async_get(WASSERTANK).disabled_by is None
    assert home_assistant.states.get(WASSERTANK).state == "off"

    # Skripte
    for kennung in (installieren.SKRIPT_ZUBEREITEN, installieren.SKRIPT_SPEICHERN, installieren.SKRIPT_LIEBLING):
        assert home_assistant.states.get(f"script.{kennung}").state == "off", kennung
    gespeichert = yaml.safe_load(Path(home_assistant.config.path("scripts.yaml")).read_text(encoding="utf-8"))
    assert set(gespeichert) == {installieren.SKRIPT_ZUBEREITEN, installieren.SKRIPT_SPEICHERN, installieren.SKRIPT_LIEBLING}
    zubereiten = gespeichert[installieren.SKRIPT_ZUBEREITEN]
    assert zubereiten["fields"]["getraenk"]["selector"]["select"]["options"][5] == "Cappuccino"
    assert "platz" not in zubereiten["fields"]
    assert zubereiten["sequence"][-1] == {
        "action": "home_connect.start_selected_program",
        "data": {"device_id": geraete_id(home_assistant)},
    }

    # Dashboard
    [dashboard] = await ws_befehl(hass_ws_client, home_assistant, type="lovelace/dashboards/list")
    assert dashboard["url_path"] == installieren.DASHBOARD_URL
    assert dashboard["title"] == "Kaffeevollautomat"
    assert dashboard["icon"] == "mdi:coffee"
    assert dashboard["show_in_sidebar"] is True
    config = await ws_befehl(hass_ws_client, home_assistant, type="lovelace/config", url_path=installieren.DASHBOARD_URL)
    [ansicht] = config["views"]
    assert ansicht["type"] == "sections"
    # Ganz oben die Zeichnung der Maschine je Betriebszustand, mit dem Gerätenamen darin.
    assert ansicht["sections"][0]["column_span"] == 3
    [kopf] = ansicht["sections"][0]["cards"]
    assert kopf["type"] == "picture-elements"
    zeichnung = kopf["elements"][0]
    assert zeichnung["entity"] == "sensor.kaffeevollautomat_betriebszustand"
    svg = base64.b64decode(zeichnung["state_image"]["run"].split(",", 1)[1]).decode("utf-8")
    assert ">Kaffeevollautomat<" in svg and ">Brüht …<" in svg
    assert kopf["elements"][1]["entity"] == KAFFEEMASCHINE  # die Ein/Aus-Taste der Zeichnung schaltet
    assert {e["elements"][0].get("icon") for e in kopf["elements"] if e["type"] == "conditional"} == {
        None, "mdi:water-alert", "mdi:tray-alert"
    }
    ueberschriften = [abschnitt["cards"][0]["heading"] for abschnitt in ansicht["sections"][1:]]
    assert ueberschriften == ["Jens", "Nicole", "Getränk zusammenstellen", "Kaffeevollautomat", "Wartung", "Zähler"]
    jens = ansicht["sections"][1]["cards"]
    assert [karte["entity"] for karte in jens[1:4]] == [JENS_1, "input_text.lieblingsgetrank_jens_2", "input_text.lieblingsgetrank_jens_3"]
    assert jens[1]["tap_action"]["perform_action"] == "script.lieblingsgetraenk_zubereiten"
    assert jens[1]["tap_action"]["data"] == {"platz": "jens_1"}
    assert jens[1]["hold_action"]["perform_action"] == "script.lieblingsgetraenk_speichern"
    direkt = ansicht["sections"][4]["cards"]
    assert direkt[1]["entity"] == KAFFEEMASCHINE
    assert {karte.get("entity") for karte in direkt} >= {AKTIV, GEWAEHLT, BOHNENMENGE, FUELLMENGE, MEHRERE_GETRAENKE,
                                                          "switch.kaffeevollautomat_kindersicherung",
                                                          "switch.kaffeevollautomat_tassenwarmer",
                                                          "sensor.kaffeevollautomat_betriebszustand",
                                                          "sensor.kaffeevollautomat_programm_fortschritt"}
    wartung = ansicht["sections"][5]["cards"]
    assert [(karte["entity"], karte["name"]) for karte in wartung[1:]] == [
        (WASSERTANK.replace("wassertank_leer", "tropfschale_voll"), "Tropfschale voll"),
        (WASSERTANK, "Wassertank leer"),
    ]
    assert ansicht["sections"][6]["cards"][1] == {
        "type": "tile", "entity": "sensor.kaffeevollautomat_kaffees", "name": "Kaffees", "icon": "mdi:counter", "color": "brown",
    }
    assert [badge["entity"] for badge in ansicht["badges"]] == [
        "sensor.kaffeevollautomat_betriebszustand", KAFFEEMASCHINE,
        WASSERTANK.replace("wassertank_leer", "tropfschale_voll"), WASSERTANK,
    ]

    # „Jetzt zubereiten“: Cappuccino, stark, 500 ml (mehr als die Maschine zulässt), doppelt – Maschine ist aus.
    await zusammenstellen(home_assistant, "Cappuccino", "stark", 500, True)
    await dienst(home_assistant, "script", installieren.SKRIPT_ZUBEREITEN)
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "on"
    assert konrad.aufrufe[0][0] == "set_program_and_options"
    assert konrad.aufrufe[0][1]["affects_to"] == "selected_program"
    assert konrad.aufrufe[0][1]["program"] == CAPPUCCINO
    assert konrad.aufrufe[-1][0] == "start_selected_program"
    assert len(konrad.aufrufe) == 2
    assert home_assistant.states.get(GEWAEHLT).state == CAPPUCCINO
    assert home_assistant.states.get(BOHNENMENGE).state == STARK
    assert float(home_assistant.states.get(FUELLMENGE).state) == 300  # auf das Maximum des Getränks begrenzt
    assert home_assistant.states.get(MEHRERE_GETRAENKE).state == "on"
    assert home_assistant.states.get("select.kaffeevollautomat_kaffee_temperatur").state == "unknown"  # „wie an der Maschine“
    assert konrad.laeuft and konrad.aktiv == CAPPUCCINO
    assert home_assistant.states.get("sensor.kaffeevollautomat_betriebszustand").state == "run"


async def test_lieblingsgetraenk_speichern_und_zubereiten(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    hass_ws_client: WebSocketGenerator,
    konrad: Konrad,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Gedrückt halten speichert die Zusammenstellung auf einem Platz, Tippen bereitet genau sie zu."""
    code, _, fehler = await dashboard_einrichten(home_assistant, client, capsys, hass_access_token, "--personen", "Jens,Nicole")
    assert code == 0, fehler
    await zusammenstellen(home_assistant, "Cappuccino", "stark", 180, True)
    await dienst(home_assistant, "script", installieren.SKRIPT_SPEICHERN, platz="jens_1")
    assert home_assistant.states.get(JENS_1).state == "Cappuccino · stark · 180 ml · doppelt"
    daten = json.loads(home_assistant.states.get(JENS_1_DATEN).state)
    assert daten == {
        "g": "Cappuccino", "m": 180, "d": True, "s": "stark",
        "t": installieren.WIE_MASCHINE, "mi": installieren.WIE_MASCHINE, "i": installieren.WIE_MASCHINE,
        "b": installieren.WIE_MASCHINE, "h": installieren.WIE_MASCHINE,
    }
    assert len(home_assistant.states.get(JENS_1_DATEN).state) < 255

    # Das Dashboard steht inzwischen auf etwas anderem – der Platz gewinnt.
    await zusammenstellen(home_assistant, "Espresso", "mild", 0, False)
    await dienst(home_assistant, "script", installieren.SKRIPT_LIEBLING, platz="jens_1")
    assert [name for name, _ in konrad.aufrufe] == ["set_program_and_options", "start_selected_program"]
    assert konrad.aufrufe[0][1]["program"] == CAPPUCCINO
    assert home_assistant.states.get(BOHNENMENGE).state == STARK
    assert float(home_assistant.states.get(FUELLMENGE).state) == 180
    assert home_assistant.states.get(MEHRERE_GETRAENKE).state == "on"

    # Ein leerer Platz brüht nichts, sondern erklärt sich.
    konrad.aufrufe.clear()
    await dienst(home_assistant, "script", installieren.SKRIPT_LIEBLING, platz="nicole_2")
    assert konrad.aufrufe == []
    [hinweis] = await ws_befehl(hass_ws_client, home_assistant, type="persistent_notification/get")
    assert hinweis["title"] == "Kaffeemaschine"
    assert "noch kein Getränk gespeichert" in hinweis["message"]

    # Ohne Doppelt wird „Mehrere Getränke“ wieder ausgeschaltet, 0 ml lässt die Füllmenge in Ruhe.
    await zusammenstellen(home_assistant, "Espresso", "mild", 0, False)
    await dienst(home_assistant, "script", installieren.SKRIPT_ZUBEREITEN)
    assert konrad.aufrufe[0][1]["program"] == ESPRESSO
    assert home_assistant.states.get(MEHRERE_GETRAENKE).state == "off"
    assert float(home_assistant.states.get(FUELLMENGE).state) == 180
    assert home_assistant.states.get(BOHNENMENGE).state == installieren.ENUM_PRAEFIX + "bean_amount_mild"


async def test_zweiter_aufruf_laesst_lieblingsgetraenke_stehen(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    hass_ws_client: WebSocketGenerator,
    konrad: Konrad,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ein zweiter Lauf aktualisiert Helfer, Skripte und Dashboard, ohne gespeicherte Plätze oder Doppeltes anzulegen."""
    code, _, fehler = await dashboard_einrichten(home_assistant, client, capsys, hass_access_token, "--personen", "Jens,Nicole")
    assert code == 0, fehler
    await dienst(home_assistant, "input_text", "set_value", entity_id=JENS_1, value="Mein Kaffee")
    await dienst(home_assistant, "input_text", "set_value", entity_id=JENS_1_DATEN, value='{"g": "Kaffee"}')

    code, ausgabe, fehler = await dashboard_einrichten(home_assistant, client, capsys, hass_access_token, "--personen", "Jens,Nicole")
    assert code == 0, fehler
    assert "✓ Dashboard „Kaffeevollautomat“ aktualisiert" in ausgabe
    assert "Wartungshinweise/Zähler aktiviert" not in ausgabe  # die sind es schon
    assert home_assistant.states.get(JENS_1).state == "Mein Kaffee"
    assert home_assistant.states.get(JENS_1_DATEN).state == '{"g": "Kaffee"}'
    assert len(await ws_befehl(hass_ws_client, home_assistant, type="input_select/list")) == 5
    assert len(await ws_befehl(hass_ws_client, home_assistant, type="input_text/list")) == 12
    assert len(await ws_befehl(hass_ws_client, home_assistant, type="lovelace/dashboards/list")) == 1


async def test_fernstart_nicht_erlaubt(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    hass_ws_client: WebSocketGenerator,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verbietet die Maschine den Fernstart, erklärt das Skript das, statt ins Leere zu laufen."""
    konrad = Konrad(fernstart=False)
    await home_connect({**KAFFEEVOLLAUTOMAT, **GESCHIRRSPUELER_GERAET}, konrad=konrad)
    code, _, fehler = await dashboard_einrichten(home_assistant, client, capsys, hass_access_token, "--personen", "Jens")
    assert code == 0, fehler
    await dienst(home_assistant, "script", installieren.SKRIPT_ZUBEREITEN)
    assert konrad.aufrufe == []
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "off"
    [hinweis] = await ws_befehl(hass_ws_client, home_assistant, type="persistent_notification/get")
    assert "Fernstart ist an der Maschine nicht erlaubt" in hinweis["message"]


async def test_maschine_aus_wird_vorbereitet(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Kennt Home Assistant die Getränke noch nicht, schaltet das Skript die Maschine kurz ein und wählt Cappuccino."""
    konrad = Konrad(programme_erst_wenn_an=True)
    await home_connect({**KAFFEEVOLLAUTOMAT, **GESCHIRRSPUELER_GERAET}, konrad=konrad)
    assert home_assistant.states.get(AKTIV).attributes["options"] == []
    with patch.object(sys.stdin, "isatty", return_value=True), patch("builtins.input", side_effect=[""]):
        code, ausgabe, fehler = await dashboard_einrichten(
            home_assistant, client, capsys, hass_access_token, "--personen", "Jens,Nicole"
        )
    assert code == 0, fehler
    assert "Home Assistant kennt noch nicht alle Getränke und Optionen der Maschine." in ausgabe
    assert "Die Kaffeemaschine wird eingeschaltet …" in ausgabe
    assert "„Cappuccino“ wird ausgewählt (nicht gestartet)" in ausgabe
    assert "✓ 33 Getränke" in ausgabe
    assert "✓ Die Kaffeemaschine ist wieder ausgeschaltet." in ausgabe
    assert konrad.aufrufe == [
        ("set_program_and_options", {"device_id": geraete_id(home_assistant), "affects_to": "selected_program", "program": CAPPUCCINO})
    ]
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "off"
    assert len(home_assistant.states.get(GETRAENK).attributes["options"]) == 33


async def test_getraenke_ohne_terminal_und_ohne_getraenkeliste(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    home_connect: HomeConnectEinrichten,
    capsys: pytest.CaptureFixture[str],
) -> None:
    konrad = Konrad(programme_erst_wenn_an=True)
    await home_connect({**KAFFEEVOLLAUTOMAT, **GESCHIRRSPUELER_GERAET}, konrad=konrad)
    code, _, fehler = await dashboard_einrichten(home_assistant, client, capsys, hass_access_token, "--personen", "Jens")
    assert code == 1
    assert "Bitte die Maschine einschalten und das Skript im Terminal erneut ausführen." in fehler
    assert home_assistant.states.get(GETRAENK) is None


@pytest.mark.usefixtures("konrad")
@pytest.mark.parametrize(
    ("personen", "meldung"),
    [
        ([], "Für wen sollen Lieblingsgetränke angelegt werden? Zum Beispiel: --personen Jens,Nicole"),
        (["--personen", "Jens,Nicole,Anna,Ben,Carla"], "Höchstens 4 Personen"),
        (["--personen", "Jens,jens"], "müssen aus Buchstaben bestehen und sich unterscheiden"),
        (["--personen", "???"], "müssen aus Buchstaben bestehen und sich unterscheiden"),
    ],
)
async def test_personen_werden_geprueft(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
    personen: list[str],
    meldung: str,
) -> None:
    code, _, fehler = await dashboard_einrichten(home_assistant, client, capsys, hass_access_token, *personen)
    assert code == 1
    assert meldung in fehler
    assert home_assistant.states.get(GETRAENK) is None


@pytest.mark.usefixtures("konrad")
async def test_personen_werden_im_terminal_erfragt(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch.object(sys.stdin, "isatty", return_value=True), patch("builtins.input", side_effect=["Jens, Nicole"]):
        code, ausgabe, fehler = await dashboard_einrichten(home_assistant, client, capsys, hass_access_token, "--plaetze", "1")
    assert code == 0, fehler
    assert "1 Plätzen für Jens, 1 Plätzen für Nicole" in ausgabe
    assert home_assistant.states.get("input_text.lieblingsgetrank_nicole_1") is not None
    assert home_assistant.states.get("input_text.lieblingsgetrank_nicole_2") is None


@pytest.mark.parametrize("eingabe", ["0", "6", "drei"])
def test_ungueltige_plaetze(eingabe: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        installieren.anzahl_plaetze(eingabe)


@pytest.mark.usefixtures("konrad")
async def test_nur_anzeigen_mit_getraenke(
    home_assistant: HomeAssistant,
    client: TestClient,
    hass_access_token: str,
    hass_ws_client: WebSocketGenerator,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, ausgabe, fehler = await dashboard_einrichten(
        home_assistant, client, capsys, hass_access_token, "--personen", "Jens,Nicole", "--nur-anzeigen"
    )
    assert code == 0, fehler
    assert "Diese Skripte würden angelegt (nichts geändert):" in ausgabe
    assert '"alias": "Kaffee zubereiten"' in ausgabe
    assert "Und dieses Dashboard (eingebettete Bilder gekürzt):" in ausgabe
    assert '"image": "data:image/svg+xml;base64,… (' in ausgabe
    assert not re.search(r"[A-Za-z0-9+/]{120}", ausgabe)  # keine Base64-Blöcke in der Ausgabe
    assert f'"entity": "{JENS_1}"' in ausgabe
    assert home_assistant.states.get(GETRAENK) is None
    assert home_assistant.states.get(WASSERTANK) is None
    assert home_assistant.states.get(f"script.{installieren.SKRIPT_ZUBEREITEN}") is None
    assert await ws_befehl(hass_ws_client, home_assistant, type="lovelace/dashboards/list") == []


def test_dashboard_und_skripte_wie_die_yaml_varianten() -> None:
    """beispiele/kaffee_dashboard.yaml und kaffee_skripte.yaml sind genau das, was das Skript anlegt."""
    beispiel = installieren.beispiel_konfigurationen()
    assert yaml.safe_load(KAFFEE_DASHBOARD.read_text(encoding="utf-8")) == beispiel["dashboard"]
    assert yaml.safe_load(KAFFEE_SKRIPTE.read_text(encoding="utf-8")) == beispiel["skripte"]


@pytest.mark.parametrize(
    "name",
    [
        "Kaffee: Getränk", "Kaffee: Stärke", "Kaffee: Heißwasser-Temperatur", "Kaffee: Intensität",
        "Lieblingsgetränk Jens 1 Daten", "Lieblingsgetränk Nicole 3", "Café au Lait", "Galão", "  Jens  ",
    ],
)
def test_slug_wie_home_assistant(name: str) -> None:
    assert installieren.slug(name) == slugify(name)


def test_alte_getraenkenamen_gelten_weiter() -> None:
    """„Kaffee“ heißt jetzt wie am Display „Caffè Crema“ – ein damit gespeichertes Lieblingsgetränk läuft weiter."""
    getraenke = installieren.beispiel_konfigurationen()["skripte"][installieren.SKRIPT_ZUBEREITEN]["variables"]["getraenke"]
    kaffee = installieren.PROGRAMM_PRAEFIX + "beverage_coffee"
    assert getraenke["Caffè Crema"] == getraenke["Kaffee"] == kaffee
    assert getraenke["Milchkaffee"] == getraenke["Caffè Latte"] == installieren.PROGRAMM_PRAEFIX + "beverage_caffe_latte"
    assert getraenke["Caffè XL"] == getraenke["XL-Kaffee"] == installieren.PROGRAMM_PRAEFIX + "beverage_x_l_coffee"
    assert "Kaffee" not in [n for n, _ in installieren.getraenke_liste({}, None)]  # nur im Skript, nicht in der Auswahl


def test_getraenke_liste() -> None:
    zustaende = {
        AKTIV: {"attributes": {"options": [CAPPUCCINO, ESPRESSO, installieren.PROGRAMM_PRAEFIX + "cleaning_modes_rinse",
                                           installieren.PROGRAMM_PRAEFIX + "beverage_new_fancy_drink"]}}
    }
    assert installieren.getraenke_liste(zustaende, AKTIV) == [
        ("Cappuccino", CAPPUCCINO), ("Espresso", ESPRESSO),
        ("New Fancy Drink", installieren.PROGRAMM_PRAEFIX + "beverage_new_fancy_drink"),
    ]
    # Ohne Getränkeliste gilt die bekannte Liste.
    assert len(installieren.getraenke_liste({}, AKTIV)) == len(installieren.GETRAENKE_NAMEN)
    assert installieren.getraenke_liste({}, None)[0] == ("Espresso", ESPRESSO)


# --- Die Zeichnung der Kaffeemaschine im Kopf des Dashboards -----------------

BILDER = REPO / "beispiele" / "bilder"


@pytest.mark.parametrize("variante", list(installieren.BILD_VARIANTEN))
def test_zeichnung_ist_gueltiges_svg(variante: str) -> None:
    """Jede Zustandsvariante ist wohlgeformtes SVG, trägt Namen und Status und ist als Data-URI eingebettet."""
    svg = installieren.konrad_svg(variante, "Konrad")
    wurzel = ET.fromstring(svg)
    assert wurzel.tag == "{http://www.w3.org/2000/svg}svg"
    assert wurzel.get("viewBox") == "0 0 1200 500"
    status = installieren.BILD_VARIANTEN[variante][0]
    assert ">Konrad<" in svg and f">{status}<" in svg
    uri = installieren.bild_uri(svg)
    assert uri.startswith("data:image/svg+xml;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]).decode("utf-8") == svg
    assert (BILDER / f"konrad_{variante}.svg").read_text(encoding="utf-8") == svg


def test_zeichnung_animiert_je_zustand() -> None:
    """Brühen zeigt Strahl, Dampf und laufenden Fortschritt; fertig die volle Tasse mit Dampf; aus bleibt still."""
    brueht = installieren.konrad_svg("brueht")
    assert brueht.count("stroke-dashoffset") == 2  # zwei Kaffeestrahlen
    assert brueht.count("animateTransform") == 3  # drei Dampfschwaden
    assert 'attributeName="height" values="0;54"' in brueht  # das Glas füllt sich
    assert 'attributeName="width" values="0;288"' in brueht  # der Fortschrittsbalken auf dem Bildschirm läuft
    fertig = installieren.konrad_svg("fertig")
    assert "stroke-dashoffset" not in fertig and "animateTransform" in fertig
    assert 'height="54" fill="#5d3a1a"' in fertig
    aus = installieren.konrad_svg("aus")
    assert "<animate" not in aus and "animateTransform" not in aus
    assert 'attributeName="opacity" values="1;.25;1"' in installieren.konrad_svg("achtung")  # Warndreieck blinkt
    assert 'values="1;.3;1"' in installieren.konrad_svg("bereit")  # die Start-Taste auf dem Bildschirm atmet
    assert ">KONRAD<" in installieren.konrad_svg("bereit")  # der Schriftzug trägt den Namen der Maschine
    assert ">Barista<" in installieren.konrad_svg("bereit") and ">Comfort<" in installieren.konrad_svg("aus")


def test_kopfkarte_deckt_alle_betriebszustaende_ab() -> None:
    beispiel = installieren.beispiel_konfigurationen()
    ansicht = beispiel["dashboard"]["views"][0]
    assert "header" not in ansicht
    kopf = ansicht["sections"][0]
    assert kopf["column_span"] == 3
    [karte] = kopf["cards"]
    assert karte["type"] == "picture-elements"
    assert karte["image"] == installieren.LEERES_BILD  # nur das Seitenverhältnis, sonst leer
    assert karte["grid_options"] == {"columns": "full", "rows": "auto"}
    bild, einschalter, tasse, *weitere = karte["elements"]
    assert bild["entity"] == "sensor.konrad_betriebszustand"
    assert set(bild["state_image"]) == {
        "inactive", "ready", "delayedstart", "aborting", "run", "pause", "finished", "actionrequired", "error"
    }
    for zustand, variante in installieren.ZUSTAND_BILDER.items():
        assert bild["state_image"][zustand] == installieren.bild_uri(installieren.konrad_svg(variante, "Konrad"))
    assert bild["image"] == bild["state_image"]["inactive"]
    assert bild["filter"] == "none" and bild["aspect_ratio"] == "12x5"
    assert bild["style"] == {"left": "50%", "top": "50%", "width": "100%"}
    assert einschalter["entity"] == "switch.konrad_einschalter" and einschalter["tap_action"] == {"action": "toggle"}
    assert einschalter["style"]["left"] == "12.5%" and einschalter["style"]["top"] == "44.0%"
    assert tasse["tap_action"]["perform_action"] == "script.kaffee_zubereiten"
    assert tasse["tap_action"]["confirmation"] == installieren.TASSE
    # Im Bildschirmkopf das laufende, sonst das ausgewählte Programm; Fortschritt nur, während ein
    # Getränk läuft; Hinweise nur, wenn sie anliegen.
    bedingte = [e for e in weitere if e["type"] == "conditional"]
    assert len(bedingte) == len(weitere) == 3 + 4  # 2× Programm, Fortschritt, 4 Wartungshinweise im Beispiel
    assert bedingte[0]["conditions"] == [{"entity": "sensor.konrad_betriebszustand", "state": "run"}]
    assert bedingte[0]["elements"][0]["entity"] == "select.konrad_aktives_programm"
    assert bedingte[0]["elements"][0]["style"]["left"] == "27.5%"  # Kopfzeile des Bildschirms
    assert bedingte[1]["conditions"] == [
        {"entity": "sensor.konrad_betriebszustand", "state_not": "run"},
        {"entity": "sensor.konrad_betriebszustand", "state_not": "inactive"},
        {"entity": "sensor.konrad_betriebszustand", "state_not": "unknown"},
        {"entity": "sensor.konrad_betriebszustand", "state_not": "unavailable"},
        {"entity": "select.konrad_ausgewahltes_programm", "state_not": "unknown"},
        {"entity": "select.konrad_ausgewahltes_programm", "state_not": "unavailable"},
    ]
    assert bedingte[1]["elements"][0]["entity"] == "select.konrad_ausgewahltes_programm"
    assert bedingte[2]["elements"][0]["prefix"] == "Fortschritt "
    wassertank = next(e for e in bedingte if e["elements"][0].get("icon") == "mdi:water-alert")
    assert wassertank["conditions"] == [
        {"entity": "sensor.konrad_wassertank_leer", "state_not": "off"},
        {"entity": "sensor.konrad_wassertank_leer", "state_not": "unknown"},
        {"entity": "sensor.konrad_wassertank_leer", "state_not": "unavailable"},
    ]
    assert wassertank["elements"][0]["tap_action"] == {"action": "more-info"}
    assert {e["elements"][0]["icon"] for e in bedingte[3:]} == {
        "mdi:water-alert", "mdi:seed-off", "mdi:tray-alert", "mdi:water-off"
    }


def test_ohne_betriebszustand_keine_zeichnung() -> None:
    maschine = {"einschalter": "switch.konrad_einschalter", "aktiv": "select.konrad_aktives_programm"}
    assert installieren.kopfkarte("Konrad", maschine, {}) is None
    dashboard = installieren.dashboard_konfiguration("Konrad", maschine, {}, {}, {})
    assert dashboard["views"][0]["sections"][0]["cards"][0] == {
        "type": "heading", "heading": "Getränk zusammenstellen", "heading_style": "title", "icon": "mdi:coffee-maker"
    }


def test_ohne_bilder_kuerzt_nur_eingebettete_bilder() -> None:
    uri = installieren.bild_uri("<svg/>")
    assert installieren.ohne_bilder({"a": [uri, "data:text/plain,x", 3], "b": {"c": uri}}) == {
        "a": [f"data:image/svg+xml;base64,… ({len(uri)} Zeichen)", "data:text/plain,x", 3],
        "b": {"c": f"data:image/svg+xml;base64,… ({len(uri)} Zeichen)"},
    }
