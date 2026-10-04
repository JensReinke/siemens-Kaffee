"""Tests für installieren.py.

Die Tests starten ein echtes Home Assistant samt HTTP-Server, bilden eine
Kaffeemaschine aus der Home-Connect-Integration nach und lassen das Skript
dagegen laufen – mit Token und mit Benutzername/Passwort.
"""

import os
import re
import sys
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime
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
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
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
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

import installieren

REPO = Path(__file__).resolve().parents[1]
AUTOMATION_OHNE_BLUEPRINT = REPO / "beispiele" / "automation_ohne_blueprint.yaml"
LICHT_FOLGT_KAFFEEMASCHINE = REPO / "beispiele" / "licht_folgt_kaffeemaschine.yaml"

HOME_CONNECT = "home_connect"
ALARMANLAGE = "alarm_control_panel.alarmanlage"
KAFFEEMASCHINE = "switch.kaffeevollautomat_einschalter"
GESCHIRRSPUELER = "switch.geschirrspuler_einschalter"
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
) -> MockConfigEntry:
    """Spielt Home Connect nach: Gerätename → (Modell, weitere Entitäten des Geräts).

    Jedes Gerät bekommt wie in echt die Schalter „Einschalter“ und
    „Kindersicherung“; die weiteren Entitäten (z. B. ein Kaffeezähler) stehen
    nur im Entitätsregister. ``variablen`` (Name → an?) legt stellvertretend für
    CCU-Systemvariablen Binärsensoren an, die in ``deaktiviert`` von Anfang an
    deaktiviert sind; ``deaktivierte_schalter`` (Gerät, Schaltername) sind
    Schalter, die von Anfang an deaktiviert sind. Gibt den Konfigurationseintrag
    zurück.
    """
    plattformen = ["switch", "binary_sensor"]

    async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        await hass.config_entries.async_forward_entry_setups(entry, plattformen)
        return True

    async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        return await hass.config_entries.async_unload_platforms(entry, plattformen)

    async def async_setup_switch(
        hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
    ) -> None:
        async_add_entities(
            HomeConnectSchalter(geraet, modell, name)
            for geraet, (modell, _) in geraete.items()
            for name in ("Einschalter", "Kindersicherung")
        )

    async def async_setup_binary_sensor(
        hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
    ) -> None:
        async_add_entities(Systemvariable(name, an) for name, an in (variablen or {}).items())

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
async def home_assistant(hass: HomeAssistant, tmp_path: Path) -> HomeAssistant:
    """Ein Home Assistant mit REST-API, Automations-Editor und eigenem Konfigurationsordner."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text("automation: !include automations.yaml\n")
    (tmp_path / "automations.yaml").write_text("[]\n")
    for komponente, config in (
        ("homeassistant", {}),  # u. a. die Dienste homeassistant.turn_on/turn_off
        ("auth", {}),
        ("api", {}),
        ("websocket_api", {}),
        ("config", {}),
        ("automation", {"automation": []}),
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
    assert home_assistant.states.get(KAFFEEMASCHINE).state == "on"


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


async def steckdose_einrichten(hass: HomeAssistant, deaktiviert: bool = False) -> None:
    """Legt die Steckdose „LED Kaffee“ (switch.led_kaffee) an – auf Wunsch deaktiviert."""

    async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        await hass.config_entries.async_forward_entry_setups(entry, ["switch"])
        return True

    async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        return await hass.config_entries.async_unload_platforms(entry, ["switch"])

    async def async_setup_switch(
        hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
    ) -> None:
        async_add_entities([Steckdose()])

    mock_integration(
        hass,
        MockModule(HOMEMATIC, async_setup_entry=async_setup_entry, async_unload_entry=async_unload_entry),
    )
    mock_platform(hass, f"{HOMEMATIC}.config_flow", None)
    mock_platform(hass, f"{HOMEMATIC}.switch", MockPlatform(async_setup_entry=async_setup_switch))
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
    assert "Das Licht an der Maschine geht mit ihr an und aus." in ausgabe
    assert home_assistant.states.get(LICHT).state == "on"

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
