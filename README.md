# spool-bridge

NFC-Spulenerkennung für **Bambu Lab X2D** (AMS) und **Klipper/Moonraker (Sovol SV08)** mit **Spoolman** als einziger, zentraler Filament-Datenbank.

**Ablauf am X2D:** Spule an den Leser halten, dann in einen beliebigen AMS-Slot stecken. Die Bridge erkennt den Slot und stellt dort Material, Farbe und Temperaturen ein. In Spoolman wird der Lagerort gesetzt (z. B. `X2D AMS A3`).
**Ablauf am SV08:** Spule an den Leser halten. Sie wird in Moonraker als aktive Spule gesetzt, und Moonraker bucht den Verbrauch selbst in Spoolman ab.

```
 NFC-Leser (ESP32-S3 + PN532, ESPHome)
        │  HTTP: {"reader": "x2d", "uid": "04-A1-…"}
        ▼
 spool-bridge (Docker auf TrueNAS) ──REST──► Spoolman (Spulen, Restmenge, Lagerort)
        │                     │
   MQTT (LAN)           HTTP (Moonraker-API)
        ▼                     ▼
      X2D / AMS             SV08 / Klipper
```

---

## Inhalt

1. [Was du brauchst](#1-was-du-brauchst)
2. [X2D vorbereiten](#2-x2d-vorbereiten)
3. [Spoolman und Bridge auf TrueNAS](#3-spoolman-und-bridge-auf-truenas)
4. [NFC-Leser bauen und flashen](#4-nfc-leser-bauen-und-flashen)
5. [SV08 / Moonraker anbinden](#5-sv08--moonraker-anbinden)
6. [Spulen erfassen und Tags verknüpfen](#6-spulen-erfassen-und-tags-verknüpfen)
7. [Alltag](#7-alltag)
8. [Verbrauchserfassung](#8-verbrauchserfassung)
9. [Fehlersuche](#9-fehlersuche)
10. [Grenzen der Version 0.1](#10-grenzen-der-version-01)

---

## 1. Was du brauchst

| Teil | Pro | Bemerkung |
|---|---|---|
| ESP32-S3 DevKitC-1 (z. B. diymore N16R8) | Leser | ein Leser pro Drucker |
| PN532-Modul (z. B. Binghe/Elechouse V3) | Leser | wird im SPI-Modus betrieben |
| 6 Dupont-Kabel | Leser | |
| NFC-Sticker **NTAG215** (oder 213/216) | Spule | 1 pro Spule, auf die Flanke kleben |
| TrueNAS SCALE mit Apps (Docker) | einmal | für Spoolman und die Bridge |
| ESPHome (z. B. als Home-Assistant-Add-on) | einmal | zum Flashen der Leser |

> Original-Bambu-Spulen musst du nicht taggen. Die erkennt das AMS selbst per RFID, und die Bridge legt sie automatisch in Spoolman an (siehe Abschnitt 7).

---

## 2. X2D vorbereiten

Die Bridge spricht lokal per MQTT mit dem Drucker. Seit den Bambu-Firmwares mit Authorization Control geht das schreibend nur im **LAN-Only- und Developer-Modus**.

1. Am Touchscreen des X2D: **Einstellungen → Netzwerk** → **LAN-Only-Modus** aktivieren.
2. Im selben Menü den **Developer-Modus** aktivieren.
3. Notieren:
   - **Zugangscode** (Access Code), steht in den Netzwerkeinstellungen
   - **Seriennummer**, unter Einstellungen → Gerät/Info
   - **IP-Adresse**. Vergib sie im Router als feste DHCP-Reservation, sonst findet die Bridge den Drucker nach einem Neustart nicht mehr.

> ⚠️ Im LAN-Only-Modus funktionieren Bambu Cloud und Bambu Handy nicht. Bambu Studio und OrcaSlicer drucken weiterhin über LAN.
> Die genauen Menünamen können je nach Firmware leicht abweichen.

---

## 3. Spoolman und Bridge auf TrueNAS

### 3.1 Docker-Image verfügbar machen (einmalig)

Bei jedem Push auf `main` baut GitHub Actions automatisch das Image `ghcr.io/derbonsaii/spool-bridge:latest` (Tab **Actions** im Repo).

Weil das Repo privat ist, ist auch das Image zuerst privat. Du hast zwei Möglichkeiten:

- **Einfach:** GitHub → dein Profil → **Packages** → `spool-bridge` → **Package settings** → *Change visibility* → **Public**. Im Image steckt kein Geheimnis, deine Zugangsdaten liegen nur in `config.yaml` auf dem NAS.
- **Privat lassen:** In TrueNAS unter *Apps → Configuration → Docker Registries* `ghcr.io` mit deinem GitHub-Benutzer und einem Personal Access Token (Scope `read:packages`) eintragen.

### 3.2 Ordner anlegen

In der TrueNAS-Shell oder über SMB, den Pool-Namen (`tank`) bitte anpassen:

```sh
mkdir -p /mnt/tank/apps/spoolman /mnt/tank/apps/spool-bridge
```

Kopiere `config.example.yaml` nach `/mnt/tank/apps/spool-bridge/config.yaml` und trage ein:

- `spoolman_url`: z. B. `http://<NAS-IP>:7912`
- unter `printers.x2d`: `host`, `serial`, `access_code` aus Schritt 2
- unter `printers.sv08`: `url` von Moonraker, z. B. `http://<SV08-IP>:7125`

### 3.3 Apps installieren

TrueNAS → **Apps → Discover Apps → ⋮ → Install via YAML**. Den Inhalt von [`docker-compose.yaml`](docker-compose.yaml) einfügen, die Pfade auf deinen Pool anpassen und speichern.

Läuft Spoolman bei dir schon, lässt du den `spoolman`-Block weg und trägst bei `spoolman_url` die bestehende Adresse ein.

### 3.4 Prüfen

- Spoolman: `http://<NAS-IP>:7912` → unter *Einstellungen → Extra-Felder* tauchen nach dem Bridge-Start **NFC Tag** (Spule) und **Bambu Filament-ID** (Filament) auf.
- Bridge: `http://<NAS-IP>:8099` → der X2D erscheint mit grünem Punkt und zeigt seine AMS-Slots.
- Logs: In TrueNAS bei der App **Logs** öffnen. Eine Zeile wie `x2d: verbunden mit …` bedeutet, dass alles passt.

---

## 4. NFC-Leser bauen und flashen

### 4.1 Verdrahtung (PN532 → ESP32-S3 DevKitC-1)

| PN532 | ESP32-S3 |
|---|---|
| VCC | 3V3 |
| GND | GND |
| SCK | GPIO12 |
| MISO | GPIO13 |
| MOSI | GPIO11 |
| SS / NSS | GPIO10 |

**DIP-Schalter am PN532 auf SPI stellen.** Beim Elechouse-/Binghe-V3 ist das meist *Schalter 1 = OFF, Schalter 2 = ON*. Schau trotzdem auf den Aufdruck auf der Platine, das ist die häufigste Fehlerquelle.

### 4.2 Firmware

1. In ESPHome **New Device → Skip** wählen, dann den Inhalt von [`esphome/spool-reader.yaml`](esphome/spool-reader.yaml) einfügen.
2. Die `substitutions` anpassen:
   - `name`: z. B. `spool-reader-x2d` bzw. `spool-reader-sv08`
   - `reader_id`: `x2d` bzw. `sv08`, passend zu `readers:` in `config.yaml`
   - `bridge_url`: `http://<NAS-IP>:8099/api/scan`
   - `led_pin`: `GPIO48`. Leuchtet die LED nicht, probierst du `GPIO38`, das hängt von der Board-Revision ab.
3. Unter *Secrets* `wifi_ssid` und `wifi_password` setzen.
4. Den ersten Flash per USB machen, danach geht es per OTA.

Für den zweiten Leser kopierst du die Datei und änderst nur `name` und `reader_id`.

### 4.3 LED-Rückmeldung

| Farbe | Bedeutung |
|---|---|
| weiss (kurz) | Tag gelesen, Anfrage läuft |
| grün | Spule erkannt. Am X2D: jetzt einstecken. Am SV08: aktiv gesetzt |
| gelb | unbekannter Tag, im Web-UI einer Spule zuweisen |
| rot | Fehler (Bridge nicht erreichbar o. ä.) |

### 4.4 Platzierung

- **X2D:** Der Leser kommt direkt neben das AMS, zum Beispiel in ein kleines Gehäuse an der Seite. Die Lesereichweite liegt bei etwa 2–4 cm.
- **SV08:** Der Leser kann neben dem Drucker stehen. Alternativ setzt du ihn direkt hinter den Spulenhalter, sodass der Sticker beim Aufsetzen der Spule vor dem Leser liegt. Dann wird die Spule ohne Antippen erkannt.

---

## 5. SV08 / Moonraker anbinden

In `moonraker.conf` am SV08:

```ini
[spoolman]
server: http://<NAS-IP>:7912
sync_rate: 5
```

Danach Moonraker neu starten. Ab jetzt bucht Moonraker beim Drucken den Verbrauch der aktiven Spule direkt in Spoolman ab. In Mainsail/Fluidd siehst du die aktive Spule.

Die Bridge setzt die aktive Spule über `POST /server/spoolman/spool_id`, sobald du sie am SV08-Leser scannst. Den Lagerort setzt sie auf `SV08`, die vorher aktive Spule geht zurück ins `Lager`.

---

## 6. Spulen erfassen und Tags verknüpfen

1. **In Spoolman Hersteller und Filamente anlegen** (Material, Farbe, Düsentemperatur, Dichte, Gewicht). Die Felder **Material**, **Farbe** und **Extruder-Temperatur** nutzt die Bridge für die AMS-Einstellung.
2. **Spulen anlegen** (Ausgangsgewicht/Restgewicht, Lagerort `Lager`).
3. **Sticker aufkleben**, am besten auf die Aussenseite der Flanke.
4. **Verknüpfen:** Halte die Spule an einen Leser. Die LED wird gelb, und im Web-UI (`:8099`) erscheint die Box *Neuer Tag erkannt*. Dort wählst du die Spule und klickst **Verknüpfen**. Die Bridge behandelt das gleich wie einen Scan, die Spule ist also direkt bereit zum Einstecken.

   Alternativ kannst du die UID auch manuell in Spoolman ins Feld *NFC Tag* der Spule eintragen.

Nur die UID des Stickers wird gespeichert. Auf den Tag selbst wird nichts geschrieben, alle Daten bleiben in Spoolman.

### Bambu-Filament-ID (optional, für präzisere Profile)

Ohne Angabe setzt die Bridge das passende **Generic-Profil** von Bambu:

| Material | ID |
|---|---|
| PLA | GFL99 |
| PETG | GFG99 |
| ABS | GFB99 |
| ASA | GFB98 |
| TPU | GFU99 |
| PA | GFN99 |
| PC | GFC99 |
| PVA | GFS99 |
| HIPS | GFS98 |

Willst du ein bestimmtes Preset, etwa ein Bambu-Original oder ein eigenes Cloud-Preset, trägst du dessen ID in Spoolman beim **Filament** im Extra-Feld **Bambu Filament-ID** ein. Die ID findest du in Bambu Studio in der JSON-Datei des Filament-Presets (`filament_id`).

> Die Zuordnung für HIPS (GFS98) solltest du einmal am Drucker kontrollieren. Stimmt sie nicht, trägst du die richtige ID beim Filament ein.

---

## 7. Alltag

**X2D:**
1. Spule an den Leser halten, die LED wird grün. Im Web-UI steht «Spule #… bereit».
2. Innerhalb von 60 s die Spule in einen beliebigen AMS-Slot stecken.
3. Die Bridge stellt den Slot ein, liest die Bestätigung des Druckers zurück und setzt den Lagerort in Spoolman.

Die umgekehrte Reihenfolge geht auch: zuerst einstecken, dann innerhalb von 60 s scannen.

Nimmst du eine Spule aus dem AMS, setzt die Bridge den Lagerort in Spoolman automatisch zurück auf `Lager`.

**Original-Bambu-Spulen (RFID):** Einfach ins AMS stecken, nicht scannen. Sobald das AMS den RFID-Chip gelesen hat, passiert Folgendes:
- Die Bridge sucht die Spule in Spoolman über ihre eindeutige Bambu-Spulen-UUID (Extra-Feld *Bambu Spulen-UUID*).
- Ist sie noch nicht vorhanden, legt sie Hersteller **Bambu Lab**, das Filament (z. B. *PLA Basic*, Farbe, Gewicht, Bambu-ID) und die Spule automatisch an. Gleiche Filamente werden wiederverwendet, es entsteht also pro Farbe/Sorte nur ein Eintrag.
- Sie setzt den Lagerort und übernimmt die Restmenge, die das AMS meldet (siehe Abschnitt 8).

Spulen, die beim Start der Bridge schon im AMS stecken, werden ebenfalls erfasst. Steckst du eine Bambu-Spule später in einen anderen Slot, wird nur der Lagerort angepasst, es entsteht keine doppelte Spule.

**SV08:** Spule scannen, fertig.

**Manuell korrigieren:** Fahre im Web-UI mit der Maus über einen belegten Slot, wähle eine Spule und klicke **Setzen**.

---

## 8. Verbrauchserfassung

- **SV08:** Läuft automatisch über Moonraker (Abschnitt 5).
- **X2D, Original-Bambu-Spulen:** Das AMS schätzt die Restmenge in Prozent, und die Bridge schreibt sie bei jeder Änderung als Restgewicht in Spoolman. Damit das funktioniert, muss in Bambu Studio bzw. am Drucker in den AMS-Einstellungen die Option zum **Aktualisieren der Restmenge** (Restkapazität schätzen) aktiv sein. Die Genauigkeit liegt bei etwa ±1 %, also rund 10 g pro 1-kg-Spule.
- **X2D, Fremdspulen mit NFC-Sticker:** Version 0.1 kümmert sich um **Erkennung, Slot-Einstellung und Lagerort**, bucht aber **noch keinen Verbrauch** ab. Für Fremdspulen meldet das AMS keine Restmenge. Dafür muss die 3MF des Druckauftrags ausgewertet werden, das ist als nächster Ausbauschritt geplant. Bis dahin kannst du Restgewichte von Zeit zu Zeit per Waage in Spoolman nachführen.

---

## 9. Fehlersuche

| Symptom | Ursache / Lösung |
|---|---|
| X2D im Web-UI rot, Log: `MQTT-Verbindung abgelehnt` | Access-Code falsch oder Developer-Modus aus (Schritt 2) |
| X2D rot, keine Log-Meldung | falsche IP oder Drucker aus. `host` prüfen |
| Log: `Befehl gesendet, aber keine Bestätigung` | Der Drucker hat die Slot-Einstellung nicht übernommen. Slot am Display prüfen, LOG_LEVEL=DEBUG setzen und Log anschauen (siehe Abschnitt 10) |
| LED bleibt aus beim Scan | PN532 nicht im SPI-Modus oder Verdrahtung falsch. ESPHome-Log zeigt `PN532 not found` |
| LED rot | `bridge_url` falsch oder Bridge läuft nicht. `http://<NAS-IP>:8099/health` im Browser testen |
| Log: `Unbekannter Leser` | `reader_id` in ESPHome stimmt nicht mit `readers:` in `config.yaml` überein |
| Falscher Slot zugeordnet | In den 60 s wurden zwei Spulen gleichzeitig gesteckt. Immer eine nach der anderen, oder im Web-UI korrigieren |
| Bambu-Spule erscheint nicht in Spoolman | Das AMS hat den RFID-Chip noch nicht gelesen, das dauert nach dem Einstecken einige Sekunden. Am Display muss die Sorte erscheinen. Danach im Log nachsehen |
| Restmenge einer Bambu-Spule bleibt stehen | Option zum Schätzen der Restmenge in den AMS-Einstellungen aktivieren |
| Spoolman-Felder fehlen | Die Bridge legt sie beim Start an. Erreicht sie Spoolman nicht, `spoolman_url` prüfen |

---

## 10. Grenzen der Version 0.1

- **Nur AMS-Slots werden automatisch erkannt** (AMS / AMS 2 Pro, über die Belegungsbits des Druckers). AMS HT und die externe Spule sind noch nicht getestet bzw. nicht automatisch.
- **K-Wert (Pressure Advance)** wird nicht gesetzt. Der Drucker nutzt dafür seine eigene Kalibrierung bzw. die Flussdynamik-Kalibrierung vor dem Druck.
- **Dual-Nozzle-Befehle:** Der Befehl `ams_filament_setting` wird mit `ams_id`, `tray_id` und `slot_id` gesendet, wie es neuere Bambu-Modelle erwarten. Ob der X2D genau dieses Format annimmt, ist ohne Drucker nicht getestet. Deshalb liest die Bridge nach jedem Setzen die Rückmeldung des Druckers und warnt, wenn sie ausbleibt.
- **Verbrauch am X2D** noch nicht (Abschnitt 8).

## Entwicklung

```sh
pip install -r requirements.txt pytest pytest-asyncio pytest-aiohttp
pytest -q
python -m bridge config/config.yaml
```
