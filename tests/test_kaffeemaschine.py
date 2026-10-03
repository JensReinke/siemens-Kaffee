"""Tests für „Kaffeemaschine an, wenn die Alarmanlage morgens unscharf geschaltet wird“.

Die Tests starten ein echtes Home Assistant, laden den Blueprint bzw. die
YAML-Automation ohne Blueprint und spielen damit typische Situationen durch.
Der Aufruf von ``switch.turn_on`` wird dabei abgefangen und gezählt.
"""

import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.blueprint.models import Blueprint
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from homeassistant.util import yaml as ha_yaml
from pytest_homeassistant_custom_component.common import async_mock_service

REPO = Path(__file__).resolve().parents[1]
BLUEPRINT_PFAD = "siemens_kaffee/kaffeemaschine_bei_unscharf.yaml"
BLUEPRINT = REPO / "blueprints" / "automation" / BLUEPRINT_PFAD
AUTOMATION_OHNE_BLUEPRINT = REPO / "beispiele" / "automation_ohne_blueprint.yaml"

# Dieselben Entitäts-IDs wie in beispiele/automation_ohne_blueprint.yaml.
ALARMANLAGE = "alarm_control_panel.alarmanlage"
KAFFEEMASCHINE = "switch.kaffeevollautomat_einschalter"

MONTAG = "2026-10-05"
SAMSTAG = "2026-10-10"
BERLIN = ZoneInfo("Europe/Berlin")


def um(uhrzeit: str, tag: str = MONTAG) -> datetime:
    """Gibt eine Uhrzeit in deutscher Ortszeit zurück."""
    return datetime.fromisoformat(f"{tag} {uhrzeit}").replace(tzinfo=BERLIN)


@pytest.fixture(autouse=True)
async def deutsche_zeitzone(hass: HomeAssistant) -> None:
    """Lässt Home Assistant in deutscher Zeit laufen."""
    await hass.config.async_set_time_zone("Europe/Berlin")


@pytest.fixture
def einschalten(hass: HomeAssistant) -> list[ServiceCall]:
    """Fängt alle Aufrufe von switch.turn_on ab."""
    return async_mock_service(hass, "switch", "turn_on")


async def blueprint_einrichten(
    hass: HomeAssistant, config_dir: Path, **eingaben: Any
) -> None:
    """Legt eine Automation aus dem Blueprint an – wie über die Oberfläche."""
    hass.config.config_dir = str(config_dir)
    ziel = config_dir / "blueprints" / "automation" / BLUEPRINT_PFAD
    ziel.parent.mkdir(parents=True)
    shutil.copy(BLUEPRINT, ziel)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "use_blueprint": {
                    "path": BLUEPRINT_PFAD,
                    "input": {
                        "alarmanlage": ALARMANLAGE,
                        "kaffeemaschine": KAFFEEMASCHINE,
                        **eingaben,
                    },
                }
            }
        },
    )
    await hass.async_block_till_done()
    automation_ist_aktiv(hass)


async def yaml_automation_einrichten(hass: HomeAssistant) -> None:
    """Legt die Automation aus beispiele/automation_ohne_blueprint.yaml an."""
    automation = yaml.safe_load(AUTOMATION_OHNE_BLUEPRINT.read_text(encoding="utf-8"))
    assert await async_setup_component(hass, "automation", {"automation": [automation]})
    await hass.async_block_till_done()
    automation_ist_aktiv(hass)


def automation_ist_aktiv(hass: HomeAssistant) -> None:
    """Prüft, dass genau eine Automation fehlerfrei geladen wurde."""
    automationen = hass.states.async_all("automation")
    assert len(automationen) == 1
    assert automationen[0].state == "on", automationen[0]


@pytest.fixture(params=["blueprint", "ohne_blueprint"])
async def automation(
    request: pytest.FixtureRequest, hass: HomeAssistant, tmp_path: Path
) -> None:
    """Führt jeden Test mit dem Blueprint und mit der YAML-Variante aus."""
    if request.param == "blueprint":
        await blueprint_einrichten(hass, tmp_path)
    else:
        await yaml_automation_einrichten(hass)


async def alarmanlage_schalten(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    zustand: str,
    zeitpunkt: datetime,
) -> None:
    """Setzt den Zustand der Alarmanlage zum angegebenen Zeitpunkt."""
    freezer.move_to(zeitpunkt)
    hass.states.async_set(ALARMANLAGE, zustand)
    await hass.async_block_till_done()


async def morgens_unscharf_schalten(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    *,
    uhrzeit: str = "06:30:00",
    tag: str = MONTAG,
    vorher: str = "armed_night",
    kaffeemaschine: str = "off",
) -> None:
    """Alarmanlage geht am Vorabend in den Zustand `vorher`, um `uhrzeit` auf unscharf."""
    vorabend = (date.fromisoformat(tag) - timedelta(days=1)).isoformat()
    hass.states.async_set(KAFFEEMASCHINE, kaffeemaschine)
    await alarmanlage_schalten(hass, freezer, vorher, um("23:00:00", vorabend))
    await alarmanlage_schalten(hass, freezer, "disarmed", um(uhrzeit, tag))


def assert_eingeschaltet(einschalten: list[ServiceCall], anzahl: int = 1) -> None:
    """Prüft, dass die Kaffeemaschine `anzahl`-mal eingeschaltet wurde."""
    assert len(einschalten) == anzahl
    for aufruf in einschalten:
        assert aufruf.data["entity_id"] == [KAFFEEMASCHINE]


# --- Verhalten: gilt für Blueprint und YAML-Variante -------------------------


@pytest.mark.usefixtures("automation")
@pytest.mark.parametrize(
    "vorher",
    [
        "armed_home",
        "armed_away",
        "armed_night",
        "armed_vacation",
        "armed_custom_bypass",
        "pending",  # Eingangsverzögerung läuft
        "triggered",  # Alarm wurde ausgelöst
        "disarming",  # manche Anlagen melden das kurz vor „unscharf“
    ],
)
async def test_schaltet_ein_wenn_morgens_unscharf(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    einschalten: list[ServiceCall],
    vorher: str,
) -> None:
    """Unscharfschalten im Zeitfenster schaltet die Kaffeemaschine ein."""
    await morgens_unscharf_schalten(hass, freezer, vorher=vorher)
    assert_eingeschaltet(einschalten)


@pytest.mark.usefixtures("automation")
@pytest.mark.parametrize(
    ("uhrzeit", "erwartet"),
    [
        ("00:15:00", 0),
        ("04:59:59", 0),
        ("05:00:00", 1),
        ("06:30:00", 1),
        ("08:59:59", 1),
        ("09:00:00", 0),
        ("12:00:00", 0),
        ("22:00:00", 0),
    ],
)
async def test_nur_von_5_bis_9_uhr(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    einschalten: list[ServiceCall],
    uhrzeit: str,
    erwartet: int,
) -> None:
    """Nur zwischen 05:00 und 08:59:59 Uhr wird eingeschaltet."""
    await morgens_unscharf_schalten(hass, freezer, uhrzeit=uhrzeit)
    assert_eingeschaltet(einschalten, erwartet)


@pytest.mark.usefixtures("automation")
@pytest.mark.parametrize("tag", ["2026-07-06", "2026-12-07"])
@pytest.mark.parametrize(("uhrzeit", "erwartet"), [("04:30:00", 0), ("05:30:00", 1)])
async def test_sommer_und_winterzeit(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    einschalten: list[ServiceCall],
    tag: str,
    uhrzeit: str,
    erwartet: int,
) -> None:
    """Das Zeitfenster gilt in deutscher Ortszeit, egal ob Sommer- oder Winterzeit."""
    await morgens_unscharf_schalten(hass, freezer, uhrzeit=uhrzeit, tag=tag)
    assert_eingeschaltet(einschalten, erwartet)


@pytest.mark.usefixtures("automation")
@pytest.mark.parametrize(
    "vorher",
    [
        "unavailable",  # Neustart von Home Assistant oder der Integration
        "unknown",
        "arming",  # Scharfschalten wurde abgebrochen
    ],
)
async def test_kein_einschalten_ohne_echtes_unscharfschalten(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    einschalten: list[ServiceCall],
    vorher: str,
) -> None:
    """Neustart oder abgebrochenes Scharfschalten schalten nichts ein."""
    await morgens_unscharf_schalten(hass, freezer, vorher=vorher)
    assert_eingeschaltet(einschalten, 0)


@pytest.mark.usefixtures("automation")
async def test_nichts_tun_wenn_kaffeemaschine_schon_an(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    einschalten: list[ServiceCall],
) -> None:
    """Ist die Kaffeemaschine schon an, wird sie nicht erneut eingeschaltet."""
    await morgens_unscharf_schalten(hass, freezer, kaffeemaschine="on")
    assert_eingeschaltet(einschalten, 0)


@pytest.mark.usefixtures("automation")
async def test_jedes_unscharfschalten_im_zeitfenster_zaehlt(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    einschalten: list[ServiceCall],
) -> None:
    """Mehrfaches Unscharfschalten schaltet die Maschine jedes Mal ein, wenn sie aus ist."""
    await morgens_unscharf_schalten(hass, freezer, uhrzeit="05:15:00")
    assert_eingeschaltet(einschalten, 1)

    # Kurz wieder scharf, Maschine ist inzwischen von selbst in Standby gegangen.
    await alarmanlage_schalten(hass, freezer, "armed_home", um("05:20:00"))
    hass.states.async_set(KAFFEEMASCHINE, "off")
    await alarmanlage_schalten(hass, freezer, "disarmed", um("07:45:00"))
    assert_eingeschaltet(einschalten, 2)

    # Noch einmal scharf und unscharf, die Maschine läuft aber noch.
    await alarmanlage_schalten(hass, freezer, "armed_away", um("07:50:00"))
    hass.states.async_set(KAFFEEMASCHINE, "on")
    await alarmanlage_schalten(hass, freezer, "disarmed", um("08:10:00"))
    assert_eingeschaltet(einschalten, 2)


# --- Einstellungen, die nur der Blueprint bietet -----------------------------


@pytest.mark.parametrize(
    ("uhrzeit", "erwartet"),
    [("06:00:00", 0), ("06:30:00", 1), ("07:59:59", 1), ("08:00:00", 0)],
)
async def test_blueprint_eigenes_zeitfenster(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    einschalten: list[ServiceCall],
    tmp_path: Path,
    uhrzeit: str,
    erwartet: int,
) -> None:
    """Das Zeitfenster lässt sich im Blueprint ändern."""
    await blueprint_einrichten(hass, tmp_path, zeit_von="06:30:00", zeit_bis="08:00:00")
    await morgens_unscharf_schalten(hass, freezer, uhrzeit=uhrzeit)
    assert_eingeschaltet(einschalten, erwartet)


@pytest.mark.parametrize(("tag", "erwartet"), [(MONTAG, 1), (SAMSTAG, 0)])
async def test_blueprint_nur_an_bestimmten_wochentagen(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    einschalten: list[ServiceCall],
    tmp_path: Path,
    tag: str,
    erwartet: int,
) -> None:
    """Mit „nur werktags“ bleibt die Maschine am Wochenende aus."""
    await blueprint_einrichten(
        hass, tmp_path, wochentage=["mon", "tue", "wed", "thu", "fri"]
    )
    await morgens_unscharf_schalten(hass, freezer, tag=tag)
    assert_eingeschaltet(einschalten, erwartet)


@pytest.mark.parametrize(("person", "erwartet"), [("home", 1), ("not_home", 0)])
async def test_blueprint_zusaetzliche_bedingungen(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    einschalten: list[ServiceCall],
    tmp_path: Path,
    person: str,
    erwartet: int,
) -> None:
    """Zusätzliche Bedingungen müssen ebenfalls erfüllt sein."""
    await blueprint_einrichten(
        hass,
        tmp_path,
        zusaetzliche_bedingungen=[
            {"condition": "state", "entity_id": "person.bewohner", "state": "home"}
        ],
    )
    hass.states.async_set("person.bewohner", person)
    await morgens_unscharf_schalten(hass, freezer)
    assert_eingeschaltet(einschalten, erwartet)


async def test_blueprint_zusaetzliche_aktionen(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    einschalten: list[ServiceCall],
    tmp_path: Path,
) -> None:
    """Zusätzliche Aktionen laufen nach dem Einschalten – aber nur dann."""
    benachrichtigungen = async_mock_service(hass, "notify", "mobile_app_handy")
    await blueprint_einrichten(
        hass,
        tmp_path,
        zusaetzliche_aktionen=[
            {
                "action": "notify.mobile_app_handy",
                "data": {"message": "Die Kaffeemaschine ist an."},
            }
        ],
    )

    await morgens_unscharf_schalten(hass, freezer, uhrzeit="10:00:00")
    assert_eingeschaltet(einschalten, 0)
    assert len(benachrichtigungen) == 0

    await morgens_unscharf_schalten(hass, freezer, tag="2026-10-06")
    assert_eingeschaltet(einschalten, 1)
    assert len(benachrichtigungen) == 1
    assert benachrichtigungen[0].data["message"] == "Die Kaffeemaschine ist an."


def test_blueprint_ist_gueltig() -> None:
    """Der Blueprint entspricht dem Schema von Home Assistant."""
    blueprint = Blueprint(
        ha_yaml.load_yaml(BLUEPRINT),
        expected_domain="automation",
        path=BLUEPRINT_PFAD,
        schema=BLUEPRINT_SCHEMA,
    )
    # validate() prüft u. a., ob die getestete HA-Version die min_version erfüllt.
    assert blueprint.validate() is None
    assert set(blueprint.inputs) == {
        "alarmanlage",
        "kaffeemaschine",
        "zeit_von",
        "zeit_bis",
        "wochentage",
        "zusaetzliche_bedingungen",
        "zusaetzliche_aktionen",
    }
    assert blueprint.metadata["source_url"].endswith(
        "/JensReinke/siemens-Kaffee/blob/HEAD/blueprints/automation/" + BLUEPRINT_PFAD
    )
