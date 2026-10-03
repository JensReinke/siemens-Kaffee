# ☕ Siemens-Kaffeevollautomat startet, wenn die Alarmanlage morgens unscharf geschaltet wird

Eine Automation für [Home Assistant](https://www.home-assistant.io/): Wird die
Alarmanlage **zwischen 05:00 und 09:00 Uhr unscharf geschaltet**, schaltet sich
der Siemens-Kaffeevollautomat (Home Connect) automatisch ein. Bis du in der
Küche bist, ist er schon bereit.

## So verhält sich die Automation

- Auslöser ist das Unscharfschalten der Alarmanlage – egal ob sie vorher
  „Nacht“, „Zuhause“, „Abwesend“ o. Ä. scharf war.
- Nur von 05:00 bis 09:00 Uhr. 09:00 Uhr selbst zählt nicht mehr dazu.
- Kein Einschalten, wenn Home Assistant oder die Alarm-Integration neu startet
  oder ein Scharfschalten abgebrochen wird.
- Ist die Kaffeemaschine schon an, passiert nichts.
- Wird morgens mehrmals unscharf geschaltet, geht die Maschine jedes Mal an,
  wenn sie gerade aus ist.

Mit dem Blueprint lassen sich außerdem Zeitfenster und Wochentage ändern sowie
zusätzliche Bedingungen (z. B. „jemand ist zu Hause“) und Aktionen (z. B. eine
Benachrichtigung aufs Handy) ergänzen.

## Voraussetzungen

- Home Assistant 2024.10 oder neuer.
- Die Kaffeemaschine ist über die Integration
  [Home Connect](https://www.home-assistant.io/integrations/home_connect/)
  eingebunden. Dort hat sie einen Schalter **„Einschalter“** (englisch
  „Power“), z. B. `switch.kaffeevollautomat_einschalter` oder
  `switch.kaffeevollautomat_power`.
- Die Alarmanlage ist in Home Assistant als Alarmzentrale eingebunden
  (`alarm_control_panel.…`), z. B. über die Integration des Herstellers oder
  über [Alarmo](https://github.com/nielsfaber/alarmo).

Kurzer Test vorab: Schalte den „Einschalter“ der Kaffeemaschine in Home
Assistant einmal von Hand ein. Geht die Maschine an, funktioniert auch die
Automation.

## Einrichten mit Blueprint (empfohlen)

1. Blueprint importieren:

   [![Blueprint in Home Assistant importieren](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FJensReinke%2Fsiemens-Kaffee%2Fblob%2FHEAD%2Fblueprints%2Fautomation%2Fsiemens_kaffee%2Fkaffeemaschine_bei_unscharf.yaml)

   Oder von Hand: **Einstellungen → Automationen & Szenen → Blueprints →
   Blueprint importieren** und diese Adresse einfügen:

   ```text
   https://github.com/JensReinke/siemens-Kaffee/blob/HEAD/blueprints/automation/siemens_kaffee/kaffeemaschine_bei_unscharf.yaml
   ```

2. Beim Blueprint **„Kaffeemaschine an, wenn die Alarmanlage morgens unscharf
   geschaltet wird“** auf **Automation erstellen** klicken.
3. **Alarmanlage** und **Kaffeemaschine** auswählen. Das Zeitfenster steht
   schon auf 05:00 bis 09:00 Uhr.
4. Speichern – fertig.

## Einrichten ohne Blueprint

Wer lieber eine einfache Automation ohne Blueprint möchte:

1. **Einstellungen → Automationen & Szenen → Automation erstellen → Neue
   Automation erstellen**.
2. Oben rechts im Menü (⋮) **In YAML bearbeiten** wählen.
3. Den Inhalt von
   [`beispiele/automation_ohne_blueprint.yaml`](beispiele/automation_ohne_blueprint.yaml)
   einfügen.
4. Die mit `# <- anpassen` markierten Entitäts-IDs auf die eigene Alarmanlage
   und Kaffeemaschine ändern und speichern.

## Ausprobieren

Damit du nicht bis morgen früh warten musst: Stell im Blueprint das Zeitfenster
kurz so ein, dass die aktuelle Uhrzeit darin liegt, schalte die Alarmanlage
scharf und wieder unscharf – die Kaffeemaschine sollte angehen. Danach das
Zeitfenster wieder auf 05:00 bis 09:00 Uhr zurückstellen.

Warum die Automation (nicht) gelaufen ist, zeigt ihre **Ablaufverfolgung**
(Automation öffnen → Menü ⋮ → Ablaufverfolgung).

## Gut zu wissen

- Die Maschine spült nach dem Einschalten kurz – das Wasser landet in der
  Abtropfschale.
- Sie geht nach der in der Maschine eingestellten Zeit von selbst wieder aus.
- Damit sie sich aus der Ferne einschalten lässt, muss sie im Standby mit dem
  WLAN verbunden bleiben, darf also nicht am Netzschalter ausgeschaltet sein.
- Geht sie trotzdem nicht an: Prüfe, ob sie sich in der Home-Connect-App
  einschalten lässt. Meldet Home Assistant einen Fehler zur Fernsteuerung,
  erlaube in den Home-Connect-Einstellungen der Maschine die Fernsteuerung
  bzw. den Fernstart.

## Inhalt

| Datei | Zweck |
| --- | --- |
| [`blueprints/automation/siemens_kaffee/kaffeemaschine_bei_unscharf.yaml`](blueprints/automation/siemens_kaffee/kaffeemaschine_bei_unscharf.yaml) | Blueprint |
| [`beispiele/automation_ohne_blueprint.yaml`](beispiele/automation_ohne_blueprint.yaml) | Dieselbe Automation ohne Blueprint |
| [`tests/`](tests/) | Automatische Tests |

## Tests

Die Tests starten ein echtes Home Assistant, laden Blueprint und YAML-Variante
und spielen u. a. Uhrzeiten an den Grenzen des Zeitfensters, alle
Alarmanlagen-Zustände, Neustarts, abgebrochenes Scharfschalten und eine schon
laufende Maschine durch. Sie laufen bei jedem Push über GitHub Actions; lokal
mit Python 3.14:

```bash
pip install -r requirements_test.txt
pytest
```
