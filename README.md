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
  oder ein Scharfschalten noch während der Ausgangsverzögerung abgebrochen
  wird.
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
  eingebunden (Anleitung: [Home Connect einrichten](#home-connect-einrichten)).
  Dort hat sie einen Schalter **„Einschalter“** (englisch „Power“), z. B.
  `switch.kaffeevollautomat_einschalter` oder `switch.kaffeevollautomat_power`.
- Die Alarmanlage ist in Home Assistant eingebunden – als Alarmzentrale
  (`alarm_control_panel.…`, z. B. über die Integration des Herstellers oder
  über [Alarmo](https://github.com/nielsfaber/alarmo)) **oder** als andere
  Entität, die ihren Zustand meldet, z. B. eine Systemvariable der
  Homematic-CCU (OpenCCU/RaspberryMatic) über
  [Homematic(IP) Local](https://github.com/sukramj/homematicip_local) als
  Sensor, Auswahl oder Schalter. Dann muss nur bekannt sein, welcher Zustand
  „unscharf“ bedeutet (z. B. `Unscharf` oder `off`). Es dürfen auch mehrere
  Entitäten sein, etwa je eine Variable für Hüllschutz und Vollschutz.
  Homematic(IP) Local legt Systemvariablen standardmäßig als **deaktivierte**
  Entitäten an. Das Skript kann sie selbst aktivieren; von Hand geht es unter
  **Einstellungen → Geräte & Dienste → Entitäten**, Filter „Deaktivierte
  Entitäten anzeigen“, Entität öffnen → Zahnrad → „Aktiviert“. Oder in der
  CCU `hahm` in die Beschreibung der Systemvariable schreiben, dann wird sie
  aktiv (und schaltbar) angelegt.

Kurzer Test vorab: Schalte den „Einschalter“ der Kaffeemaschine in Home
Assistant einmal von Hand ein. Geht die Maschine an, funktioniert auch die
Automation.

## Einrichten per Skript (ein Befehl)

[`installieren.py`](installieren.py) richtet die Automation von einem Rechner
im Heimnetz aus ein – z. B. vom Mac mini. Es sucht Alarmanlage und
Kaffeemaschine selbst heraus, legt die Automation an und prüft, dass sie aktiv
ist. Es braucht nur Python 3, keine Zusatzpakete.

1. Terminal öffnen (Mac: Programme → Dienstprogramme → Terminal) und
   einfügen:

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/JensReinke/siemens-Kaffee/HEAD/installieren.py
   python3 installieren.py --url http://homeassistant.local:8123
   ```

   Falls macOS fragt, ob die „Befehlszeilen-Entwicklerwerkzeuge“ installiert
   werden sollen: bestätigen und den Befehl danach noch einmal ausführen.
   Ist Home Assistant unter einer anderen Adresse erreichbar, diese bei
   `--url` eintragen.
2. Benutzername und Passwort von Home Assistant eingeben (ein Benutzer mit
   Administratorrechten). Das Skript meldet sich damit genauso an wie die
   Oberfläche und widerruft die Anmeldung am Ende wieder.
3. Findet das Skript mehrere Alarmanlagen oder mehrere Home-Connect-Geräte,
   fragt es nach, welche gemeint sind (bei der Alarmanlage auch mehrere, z. B.
   `1,2`). Ist die Alarmanlage keine Alarmzentrale (z. B. eine
   CCU-Systemvariable), fragt es außerdem, welcher Zustand „unscharf“
   bedeutet, und schlägt den aktuellen Zustand bzw. die möglichen Werte vor.
   Passende, aber deaktivierte Entitäten (so legt Homematic(IP) Local
   Systemvariablen an) bietet es zum Aktivieren an und wartet dann, bis
   Home Assistant die Integration neu geladen hat.

Ein zweiter Aufruf aktualisiert die Automation, statt sie doppelt anzulegen.
Nützliche Optionen (`python3 installieren.py --hilfe` zeigt alle):

| Option | Wirkung |
| --- | --- |
| `--probelauf` | Löst die Automation nach dem Einrichten einmal aus – die Kaffeemaschine geht wirklich an. |
| `--nur-anzeigen` | Zeigt nur, was eingerichtet würde, ändert nichts. |
| `--diagnose` | Zeigt nur, was Home Assistant über Home Connect und Hausgeräte weiß (Integrationen mit Zustand, Geräte, alle Entitäten), zur Fehlersuche. |
| `--von 6:00 --bis 10:00` | Anderes Zeitfenster. |
| `--alarmanlage …`, `--kaffeemaschine …` | Entitäts-IDs vorgeben statt suchen zu lassen; `--alarmanlage` auch mehrfach, eine deaktivierte Entität wird dabei aktiviert. |
| `--unscharf Unscharf` | Zustand, der „unscharf“ bedeutet, wenn die Alarmanlage keine Alarmzentrale ist. |
| `--licht switch.led_kaffee` | Ein Licht oder eine Steckdose an der Maschine (`light.…` oder `switch.…`) geht in einer zweiten Automation mit der Kaffeemaschine an und aus – egal ob die Automation, Home Assistant oder jemand am Gerät sie einschaltet. Eine deaktivierte Entität wird dabei aktiviert. |
| `--token …` oder `HA_TOKEN` | Langlebiger Zugangstoken statt Benutzername/Passwort (Profil → Sicherheit). |

Das Skript legt die Automation ohne Blueprint an (wie in
[`beispiele/automation_ohne_blueprint.yaml`](beispiele/automation_ohne_blueprint.yaml);
mit `--licht` zusätzlich
[`beispiele/licht_folgt_kaffeemaschine.yaml`](beispiele/licht_folgt_kaffeemaschine.yaml)).
Sie lässt sich danach ganz normal unter **Einstellungen → Automationen &
Szenen** bearbeiten.

Die eingebaute Tassenbeleuchtung eines Siemens-Vollautomaten lässt sich so
nicht schalten: Home Connect meldet sie nur als Zustand, nicht als
Einstellung, und auch im Gerätemenü gibt es dafür keine Option. `--licht`
ist für ein zusätzliches Licht an der Maschine gedacht, z. B. eine
LED-Leiste an einer Homematic-Schaltsteckdose.

## Einrichten mit Blueprint

1. Blueprint importieren:

   [![Blueprint in Home Assistant importieren](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FJensReinke%2Fsiemens-Kaffee%2Fblob%2FHEAD%2Fblueprints%2Fautomation%2Fsiemens_kaffee%2Fkaffeemaschine_bei_unscharf.yaml)

   Oder von Hand: **Einstellungen → Automationen & Szenen → Blueprints →
   Blueprint importieren** und diese Adresse einfügen:

   ```text
   https://github.com/JensReinke/siemens-Kaffee/blob/HEAD/blueprints/automation/siemens_kaffee/kaffeemaschine_bei_unscharf.yaml
   ```

2. Beim Blueprint **„Kaffeemaschine an, wenn die Alarmanlage morgens unscharf
   geschaltet wird“** auf **Automation erstellen** klicken.
3. **Alarmanlage** (eine oder mehrere Entitäten) und **Kaffeemaschine**
   auswählen. Ist die Alarmanlage keine Alarmzentrale (z. B. eine
   CCU-Systemvariable), bei **Zustand „unscharf“** den passenden Wert
   eintragen. Das Zeitfenster steht schon auf 05:00 bis 09:00 Uhr.
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

## Home Connect einrichten

Die Kaffeemaschine kommt über die Integration
[Home Connect](https://www.home-assistant.io/integrations/home_connect/) nach
Home Assistant. Dafür braucht es einmalig einen kostenlosen Entwicklerzugang
bei Home Connect; rechne mit etwa 15 Minuten plus Wartezeit.

1. **Entwicklerkonto anlegen:** Auf <https://developer.home-connect.com>
   registrieren. Bei der Anmeldung unter „Default Home Connect User Account for
   Testing“ die E-Mail-Adresse eintragen, mit der du dich in der
   Home-Connect-App anmeldest – **komplett in Kleinbuchstaben**, sonst
   scheitert später die Anmeldung.
2. **Anwendung registrieren:** Im Entwicklerportal **Applications → Register
   Application** mit diesen Werten:
   - Application ID: `Home Assistant` (frei wählbar)
   - OAuth Flow: `Authorization Code Grant Flow`
   - Redirect URI: `https://my.home-assistant.io/redirect/oauth`

   Danach bei der Anwendung auf **Details** klicken und **Client ID** und
   **Client Secret** notieren.
3. **15 Minuten warten, dann abmelden:** Änderungen im Entwicklerportal
   brauchen etwa 15 Minuten. Danach dort **ausloggen**. Bleibst du angemeldet,
   scheitert der nächste Schritt mit `unauthorized_client`.
4. **Adresse hinterlegen:** Auf <https://my.home-assistant.io/> muss die
   Adresse deines Home Assistant stehen, z. B. `http://homeassistant.local:8123`.
5. **Integration hinzufügen:** In Home Assistant **Einstellungen → Geräte &
   Dienste → Integration hinzufügen → „Home Connect“**, Client ID und Client
   Secret eintragen, dann bei Home Connect anmelden und den Zugriff erlauben.
6. Die Kaffeemaschine erscheint mit dem Schalter **„Einschalter“**
   (`switch.…_power` bzw. `…_einschalter`). Jetzt `installieren.py` erneut
   ausführen.

## Ausprobieren

Damit du nicht bis morgen früh warten musst: Stell das Zeitfenster der
Automation kurz so ein, dass die aktuelle Uhrzeit darin liegt. Schalte die
Alarmanlage scharf, warte, bis sie wirklich scharf ist (Ausgangsverzögerung
vorbei), und schalte sie wieder unscharf – die Kaffeemaschine sollte angehen.
Danach das Zeitfenster wieder auf 05:00 bis 09:00 Uhr zurückstellen.

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
| [`beispiele/licht_folgt_kaffeemaschine.yaml`](beispiele/licht_folgt_kaffeemaschine.yaml) | Zweite Automation (optional): Licht an der Maschine folgt der Kaffeemaschine |
| [`installieren.py`](installieren.py) | Richtet die Automation per Befehl in Home Assistant ein |
| [`tests/`](tests/) | Automatische Tests |

## Tests

Die Tests starten ein echtes Home Assistant, laden Blueprint und YAML-Variante
und spielen u. a. Uhrzeiten an den Grenzen des Zeitfensters, alle
Alarmanlagen-Zustände, Neustarts, abgebrochenes Scharfschalten und eine schon
laufende Maschine durch. `installieren.py` wird gegen den echten HTTP-Server
von Home Assistant getestet, samt Anmeldung, Gerätesuche und Probelauf. Die
Tests laufen bei jedem Push über GitHub Actions; lokal mit Python 3.14:

```bash
pip install -r requirements_test.txt
pytest
```
