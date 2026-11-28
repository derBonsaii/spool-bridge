# spool-bridge

NFC-Spulenerkennung und Spulenwaage für **Bambu Lab X2D** (AMS) und **Klipper/Moonraker (Sovol SV08)**, mit **Spoolman** als einziger, zentraler Filament-Datenbank.

- **X2D:** Spule an den Leser halten oder auf die Waage legen, dann in einen beliebigen AMS-Slot stecken. Die Bridge erkennt den Slot, stellt dort Material, Farbe und Temperaturen ein und setzt in Spoolman den Lagerort.
- **SV08:** Spule scannen. Sie wird in Moonraker als aktive Spule gesetzt, und Moonraker bucht den Verbrauch selbst in Spoolman ab.
- **Waage:** Spule auflegen. Das Restgewicht wird automatisch in Spoolman eingetragen.
- **Eine Seite für alles:** `http://<NAS-IP>:8099` zeigt Live-Gewicht, aufgelegte Spule, Drucker, Lager und Ereignisse. Tara und Kalibrieren gehen direkt dort.
- **Original-Bambu-Spulen** brauchen keinen Sticker. Das AMS erkennt sie per RFID, wahlweise übernimmt Bambuddy (Abschnitt 8).

```
 NFC-Leser / Waage (ESP32 + PN532, ESPHome)
        │  HTTP: {"reader": "x2d", "uid": "04-A1-…", "weight": "845.0"}
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
5. [Waage bauen und einrichten](#5-waage-bauen-und-einrichten)
6. [SV08 / Moonraker anbinden](#6-sv08--moonraker-anbinden)
7. [Spulen erfassen und Tags verknüpfen](#7-spulen-erfassen-und-tags-verknüpfen)
8. [Zusammen mit Bambuddy](#8-zusammen-mit-bambuddy)
9. [Alltag](#9-alltag)
10. [Verbrauchserfassung](#10-verbrauchserfassung)
11. [Konfiguration (config.yaml)](#11-konfiguration-configyaml)
12. [Fehlersuche und Diagnose](#12-fehlersuche-und-diagnose)
13. [Bekannte Grenzen](#13-bekannte-grenzen)

---

## 1. Was du brauchst

| Teil | Pro | Bemerkung |
|---|---|---|
| ESP32-WROOM-32 DevKit **oder** ESP32-S3 DevKitC-1 | Leser/Waage | Beide funktionieren gleich, weil die Slot-Erkennung in der Bridge läuft |
| PN532-NFC-Modul (z. B. Binghe/Elechouse V3) | Leser/Waage | WROOM: über I2C (4-Pin-Header), S3: über SPI |
| NFC-Sticker **NTAG215** (oder 213/216) | Spule | 1 pro Fremdspule, auf die Flanke kleben |
| Wägezelle + HX711, optional OLED und Taster | Waage | siehe Abschnitt 5 |
| TrueNAS SCALE mit Apps (Docker) | einmal | für Spoolman und die Bridge |
| ESPHome (z. B. als Home-Assistant-Add-on) | einmal | zum Flashen der Leser |

> Original-Bambu-Spulen musst du nicht taggen. Das AMS liest ihren RFID-Chip selbst.

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

### 3.1 Docker-Image

Bei jedem Push auf `main` baut GitHub Actions automatisch das Image **`ghcr.io/derbonsaii/spool-bridge:latest`** (Tab **Actions** im Repo).

Das Paket muss **öffentlich** sein, damit TrueNAS es ohne Anmeldung laden kann: GitHub → Profil → **Packages** → `spool-bridge` → **Package settings** → *Change visibility* → **Public**. Im Image steckt kein Geheimnis, deine Zugangsdaten liegen nur in `config.yaml` auf dem NAS.

Willst du es privat halten, trägst du in TrueNAS unter *Apps → Configuration → Docker Registries* `ghcr.io` mit deinem GitHub-Benutzer und einem Personal Access Token (Scope `read:packages`) ein.

### 3.2 Ordner und Konfiguration

In der TrueNAS-Shell (System → Shell) einen Ordner anlegen. Pool und Pfad passt du an, z. B. `/mnt/Apps/apps/spool-bridge`:

```sh
mkdir -p /mnt/<pool>/apps/spool-bridge
```

Kopiere [`config.example.yaml`](config.example.yaml) als `config.yaml` in diesen Ordner und trage die Werte ein (Abschnitt 11). Bearbeiten geht in der Shell mit `nano /mnt/<pool>/apps/spool-bridge/config.yaml`. Speichern mit **Ctrl+O**, Enter, dann **Ctrl+X**.

### 3.3 App installieren

TrueNAS → **Apps → Discover Apps → ⋮ → Install via YAML**. Den Inhalt von [`docker-compose.yaml`](docker-compose.yaml) einfügen und den Pfad beim Volume auf deinen Ordner anpassen.

Läuft Spoolman bei dir schon, lässt du den `spoolman`-Block weg. Dann installierst du nur die Bridge:

```yaml
services:
  spool-bridge:
    image: ghcr.io/derbonsaii/spool-bridge:latest
    pull_policy: always        # bei jedem Start das neueste Image holen
    restart: unless-stopped
    ports:
      - "8099:8099"
    volumes:
      - /mnt/<pool>/apps/spool-bridge:/config
    environment:
      - TZ=Europe/Zurich
      - LOG_LEVEL=INFO
```

### 3.4 Prüfen

- **Logs** der App: Die erste Zeile zeigt die Version (`spool-bridge 0.6.2`). Bei Bambu-Druckern folgt `x2d: verbunden mit …`.
- **Statusseite:** `http://<NAS-IP>:8099`. Der X2D erscheint mit grünem Punkt und seinen AMS-Slots.
- **Spoolman:** Unter *Einstellungen → Extra-Felder* hat die Bridge diese Felder angelegt:
  - bei der Spule: **NFC Tag**, **Bambu Spulen-UUID**, **Stammplatz**
  - beim Filament: **Bambu Filament-ID**

### 3.5 Aktualisieren

Die Bridge prüft alle 6 Stunden auf GitHub, ob es eine neue Version gibt, und zeigt dann oben auf der Statusseite **«Update verfügbar»** an. Abschalten lässt sich das mit der Umgebungsvariable `UPDATE_CHECK=0`.

**Mit `pull_policy: always`** in der App-YAML (siehe 3.3) genügt zum Aktualisieren:
1. Bei der App **Stop**, dann **Start** klicken. TrueNAS lädt dabei automatisch das neueste Image.
2. Im Log oder auf der Statusseite die Versionsnummer kontrollieren.

Ohne `pull_policy` holst du das Image vorher in der TrueNAS-Shell mit `docker pull ghcr.io/derbonsaii/spool-bridge:latest`. Eine bestehende App stellst du um, indem du sie bearbeitest (Apps → spoolbridge → **Edit**) und die Zeile `pull_policy: always` beim Service ergänzt.

Die `config.yaml` bleibt dabei erhalten. Nach Änderungen an der `config.yaml` reicht ein Neustart der App.

---

## 4. NFC-Leser bauen und flashen

| Board | Firmware | PN532 | Rückmeldung |
|---|---|---|---|
| ESP32-WROOM-32 DevKit | [`esphome/spool-reader-wroom32.yaml`](esphome/spool-reader-wroom32.yaml) | I2C | blaue Onboard-LED (Blinkmuster) |
| ESP32-S3 DevKitC-1 | [`esphome/spool-reader.yaml`](esphome/spool-reader.yaml) | SPI | RGB-LED (Farben) |
| ESP32-WROOM-32 mit Waage | [`esphome/spool-waage-wroom32.yaml`](esphome/spool-waage-wroom32.yaml) | I2C | LED, optional Display (Abschnitt 5) |

### 4.1 Verdrahtung

**PN532 → ESP32-WROOM-32 (I2C).** Viele PN532-Module haben nur einen 4-Pin-Header mit GND / VCC / TXD / RXD. TXD dient dabei als SDA, RXD als SCL.

| PN532 | ESP32-WROOM-32 |
|---|---|
| GND | GND |
| VCC | 3V3 |
| TXD (= SDA) | GPIO21 |
| RXD (= SCL) | GPIO22 |

DIP-Schalter für **I2C**: Schalter 1 = ON, Schalter 2 = OFF.

**PN532 → ESP32-S3 DevKitC-1 (SPI)**

| PN532 | ESP32-S3 |
|---|---|
| VCC | 3V3 |
| GND | GND |
| SCK | GPIO12 |
| MISO | GPIO13 |
| MOSI | GPIO11 |
| SS / NSS | GPIO10 |

DIP-Schalter für **SPI**: Schalter 1 = OFF, Schalter 2 = ON.

Die DIP-Schalter sind die häufigste Fehlerquelle. Prüf den Aufdruck auf deiner Platine und mach das Modul nach dem Umstellen einmal **stromlos**, denn der PN532 liest den Modus nur beim Einschalten.

### 4.2 Firmware

1. In ESPHome **New Device → Skip** wählen und den Inhalt der passenden Datei einfügen.
2. Die `substitutions` anpassen:
   - `name`: Gerätename im Netz, z. B. `spool-reader-x2d`
   - `reader_id`: muss unter `readers:` in der `config.yaml` stehen, z. B. `x2d`
   - `bridge_url`: `http://<NAS-IP>:8099/api/scan`. Achtung: Port **8099** (Bridge), nicht 7912 (Spoolman)
   - `led_pin`: WROOM-32 `GPIO2`. S3 `GPIO48`, oder `GPIO38`, falls die LED nicht leuchtet
3. Unter *Secrets* `wifi_ssid` und `wifi_password` setzen.
4. Den ersten Flash per USB machen, danach geht es per OTA (**Install → Wirelessly**).

Für einen zweiten Leser kopierst du die Datei und änderst `name` und `reader_id`.

**Gerät umbenannt oder alte Firmware drauf?** ESPHome sucht das Gerät unter dem Namen aus der YAML. Heisst es noch anders, bleibt das Update in der Warteschlange («wird installiert, sobald das Gerät online ist»). Dann trägst du einmalig die IP ein, flashst und entfernst die Zeile danach wieder:

```yaml
wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
  use_address: 192.168.1.114   # IP des ESP
```

### 4.3 LED-Rückmeldung

| WROOM-32 (blaue LED) | S3 (RGB-LED) | Bedeutung |
|---|---|---|
| – | weiss (kurz) | Tag gelesen, Anfrage läuft |
| 1× lang | grün | Spule erkannt. X2D: jetzt einstecken. SV08: aktiv gesetzt |
| 2× kurz | gelb | unbekannter Tag, im Web-UI zuweisen |
| 5× schnell | rot | Fehler, z. B. Bridge nicht erreichbar |

> Im ESPHome-Log erscheint bei einem unbekannten Tag `HTTP Request failed … Code: 404`. Das ist **kein Fehler**: Die Bridge hat den Tag empfangen und kennt ihn nur noch nicht.

### 4.4 Platzierung

- **X2D:** direkt neben das AMS, z. B. in einem kleinen Gehäuse an der Seite. Die Lesereichweite liegt bei etwa 2–4 cm.
- **SV08:** neben dem Drucker oder direkt hinter dem Spulenhalter. Liegt der Sticker beim Aufsetzen der Spule vor dem Leser, wird die Spule ohne Antippen erkannt.

---

## 5. Waage bauen und einrichten

Spule auf den Teller legen: Der Leser unter dem Teller liest den Sticker, die Waage das Gewicht. Die Bridge trägt in Spoolman ein:

**Restgewicht = gewogenes Gewicht − Leergewicht der Spule**

Die Waage meldet das Gewicht **laufend** an die Bridge, bei jeder Änderung sofort und sonst alle 2 Sekunden. Liegt eine bekannte Spule **ruhig** auf (3 Meldungen innerhalb von 2 g), trägt die Bridge das Restgewicht ein. Ändert es sich danach um mehr als 2 g, trägt sie es erneut ein. Du musst eine Spule also nicht abheben und neu auflegen, etwa nach dem Kalibrieren.

Danach geht es weiter wie beim Leser: Am X2D nimmst du die Spule vom Teller und steckst sie innerhalb von 60 s in einen AMS-Slot. Hältst du eine Spule nur an den Leser, ohne sie aufzulegen (unter 30 g), wird kein Gewicht übertragen.

### 5.1 Teile

ESP32-WROOM-32 und PN532 wie beim Leser, dazu:

| Teil | Bemerkung |
|---|---|
| Wägezelle 5 kg (Biegebalken) mit HX711-Modul | meist im Set |
| Gehäuse | [FilaMan Revamped auf MakerWorld](https://makerworld.com/models/1258715). Die Pins entsprechen dem FilaMan-System. Oberteil passend zur Wägezelle (LC1 M4 / LC2 M5) wählen, Elektronikfach passend zum Display |
| OLED 0.96" SSD1306, 128×64, I2C (optional) | auch die gelb/blaue Variante |
| Touch-Taster TTP223 (optional) | für Tara direkt am Gerät |

### 5.2 Verdrahtung (alles an den WROOM-32)

| Teil | Anschluss | ESP32 |
|---|---|---|
| PN532 | SDA / SCL | GPIO21 / GPIO22 |
| Display | SDA / SCL | GPIO21 / GPIO22 (parallel zum PN532) |
| HX711 | DT (DOUT) / SCK | GPIO16 / GPIO17 |
| Taster TTP223 | I/O | GPIO25 |
| alle | VCC / GND | 3V3 / GND |

Wägezelle → HX711: rot E+, schwarz E−, weiss A−, grün A+. Die Farben können je nach Hersteller abweichen. Ein vertauschtes Vorzeichen gleicht die Kalibrierung aus.

### 5.3 Einrichten

1. [`esphome/spool-waage-wroom32.yaml`](esphome/spool-waage-wroom32.yaml) flashen. In den `substitutions` `name`, `reader_id` und **`bridge`** setzen. `bridge` ist die Adresse der Bridge **ohne** `/api/...`, z. B. `http://192.168.1.9:8099`. Ersetzt die Waage einen bisherigen Leser, beim ersten Flash `use_address` setzen (Abschnitt 4.2).
2. Im ESPHome-Log prüfen: Beim Start müssen auf I2C **0x24** (PN532) und **0x3C** (Display) erscheinen. Meldet sich das Display auf **0x3D**, änderst du in der Firmware `address: 0x3C` entsprechend.
3. Die **Statusseite der Bridge** öffnen (`http://<NAS-IP>:8099`). Nach wenigen Sekunden erscheint dort die Karte **Waage** mit dem Live-Gewicht.
4. Teller leer lassen und **Tara** klicken (oder den Touch-Taster antippen).
5. Ein bekanntes Gewicht auflegen, z. B. eine gewogene volle Spule. Das Gewicht in Gramm ins Feld neben **Kalibrieren** eintragen und **Kalibrieren** klicken.
6. Kontrolle: Die Waage zeigt das richtige Gewicht. Tara und Kalibrierung bleiben nach einem Neustart erhalten.

Die Befehle gehen über die Bridge an die Waage und kommen innerhalb von etwa 2 Sekunden an. Alternativ gibt es dieselben Knöpfe auf der Weboberfläche des ESP (`http://<IP-des-ESP>/`).

### 5.4 Display

Nach dem Auflegen einer bekannten Spule:

```
PLA                    ← Material (gelber Streifen)
Matte Dark Brown       ← Filament / Farbe
      845 g            ← Live-Gewicht (brutto)
Rest 645 g - ins AMS   ← Restfilament + nächster Schritt
```

Ohne Spule steht oben «Bereit». Bei einem unbekannten Tag steht «Neuer Tag / im Web zuweisen», ist die Bridge nicht erreichbar, steht «Bridge offline». Nach 15 Sekunden springt die Anzeige zurück auf «Bereit».

### 5.5 Leergewicht in Spoolman

Die Bridge braucht das **Leergewicht der Spule** und sucht es in dieser Reihenfolge: **Spule → Filament → Hersteller**. Bei Filamenten aus der SpoolmanDB ist es meist schon hinterlegt. Sonst trägst du es am einfachsten einmal pro Hersteller ein. Fehlt es, steht im Ereignis-Log eine Warnung, und das Gewicht wird nicht übernommen.

### 5.6 Eine Waage für mehrere Drucker

Der Leser bzw. die Waage ist in der `config.yaml` einem Drucker zugeordnet (`readers:`). Ein Scan bereitet nur diesen Drucker vor. Ohne Drucker (`waage:` ohne Wert) trägt die Waage nur das Gewicht ein.

---

## 6. SV08 / Moonraker anbinden

In `moonraker.conf` am SV08:

```ini
[spoolman]
server: http://<NAS-IP>:7912
sync_rate: 5
```

Danach Moonraker neu starten. Ab jetzt bucht Moonraker beim Drucken den Verbrauch der aktiven Spule direkt in Spoolman ab. In Mainsail/Fluidd siehst du die aktive Spule.

Die Bridge setzt die aktive Spule über `POST /server/spoolman/spool_id`, sobald du sie am SV08-Leser scannst. Den Lagerort setzt sie auf `SV08`. Die vorher aktive Spule kehrt an ihren Stammplatz zurück (Abschnitt 9).

---

## 7. Spulen erfassen und Tags verknüpfen

### 7.1 Empfohlen: Filamente aus der SpoolmanDB

Spoolman kann Filamente aus der Community-Datenbank **SpoolmanDB** übernehmen, mit Name, Farbe, Dichte, Gewicht und **Leergewicht der Spule**. Beim Anlegen eines Filaments in Spoolman wählst du dafür «Aus externer Datenbank importieren».

Spulen musst du dann nicht von Hand anlegen:
- **Fremdspule mit Sticker:** Beim ersten Scan erscheint auf der Statusseite «Neuer Tag erkannt». Unter **«Neue Spule anlegen aus Filament»** wählst du das Filament und klickst auf **Verknüpfen**. Die Bridge legt die Spule an, hängt den Tag an und übernimmt gleich das Restgewicht, falls die Spule auf der Waage lag.
- **Bambu-Spule:** einfach ins AMS stecken (Abschnitt 9). Die Bridge sucht unter «Bambu Lab» das passende Filament: gleiches Material, Produktlinie im Namen (z. B. «Matte»), ähnlichste Farbe. Nur wenn nichts passt, legt sie ein neues an. Im Bambuddy-Modus übernimmt Bambuddy das (Abschnitt 8).

**Jede Rolle bekommt ihren eigenen Sticker.** Hast du z. B. 10 Rollen desselben Filaments, sind das in Spoolman **1 Filament und 10 Spulen**. Nur so stimmt das Restgewicht pro Rolle. Den Sticker klebst du am besten erst auf, wenn du die Rolle auspackst.

### 7.2 Von Hand

1. In Spoolman **Hersteller und Filament** anlegen. Die Felder **Material**, **Farbe** und **Extruder-Temperatur** nutzt die Bridge für die AMS-Einstellung.
2. **Spule** anlegen.
3. Den **Sticker** auf die Aussenseite der Flanke kleben.
4. Die Spule an den Leser halten. Auf der Statusseite (`:8099`) erscheint *Neuer Tag erkannt*. Unter **«Bestehende Spule»** die Spule wählen und **Verknüpfen** klicken. Die Bridge behandelt das wie einen Scan, die Spule ist also direkt bereit zum Einstecken.

### 7.3 Wo der Tag gespeichert wird

Auf den Sticker selbst wird **nichts geschrieben**. Die Bridge speichert nur seine fest eingebaute Kennung (UID) in Spoolman, bei der Spule im Extra-Feld **NFC Tag**. Mehrere Tags pro Spule sind möglich und stehen mit Komma getrennt darin, z. B. beide Seiten einer Bambu-Spule.

**Falsch verknüpft?** In Spoolman bei der falschen Spule die UID aus dem Feld *NFC Tag* löschen. Danach den Tag nochmals scannen und auf der Statusseite der richtigen Spule zuweisen.

### 7.4 Bambu-Filament-ID (optional)

Ohne Angabe setzt die Bridge im AMS-Slot das passende **Generic-Profil** von Bambu:

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
| HIPS | GFS98 (bitte am Drucker kontrollieren) |

Für ein bestimmtes Preset, etwa ein Bambu-Original oder ein eigenes, trägst du dessen ID in Spoolman beim **Filament** im Feld **Bambu Filament-ID** ein. Die ID steht in Bambu Studio in der JSON-Datei des Presets (`filament_id`).

**An den X2D übergeben werden:** Materialtyp, Filament-Profil (ID), Farbe und Düsentemperatur min/max (Extruder-Temperatur −20/+10 °C). Hersteller, Name, Restmenge und K-Wert werden nicht übergeben.

### 7.5 Namen in Spoolman

Statusseite und Display zeigen **Material + Filament-Name**, ohne Spoolman-Nummer und Hersteller. Am übersichtlichsten wird es, wenn der Filament-Name **Produktlinie + Farbe** enthält, z. B. Material `PLA`, Name `Matte Dark Brown`. Den Hersteller solltest du trotzdem eintragen: Die Bridge erkennt Bambu-Filamente über «Bambu Lab», und das Leergewicht lässt sich pro Hersteller hinterlegen.

---

## 8. Zusammen mit Bambuddy

Läuft **Bambuddy mit Spoolman-Sync**, kümmern sich sonst beide um die Original-Bambu-Spulen. Das führt zu doppelten Spulen und wechselnden Lagerorten. Deshalb beim X2D in der `config.yaml` eintragen:

```yaml
  x2d:
    type: bambu
    ...
    location: X2D          # genau wie der Drucker in Bambuddy heisst
    bambu_rfid: bambuddy
```

Dann gilt:

| | Bambuddy | Bridge |
|---|---|---|
| Bambu-Spulen (RFID) | anlegen, Lagerort, Verbrauch pro Druck | merkt sich nur die RFID-Kennung, damit Leser und Waage sie erkennen |
| Fremdspulen am X2D | – | scannen, Slot einstellen, Lagerort |
| Waage | – | Restgewicht |
| SV08 | – | aktive Spule in Moonraker |

Die Bridge nutzt dann das Ortsformat von Bambuddy (`X2D - AMS A Slot 1`). Die Statusseite zeigt deshalb auch die Spulen, die Bambuddy zugeordnet hat. In Bambuddy stellst du den Spoolman-Sync auf **Auto**.

Hat die Bridge vorher schon Bambu-Spulen angelegt (Feld *Bambu Spulen-UUID* gefüllt), legt Bambuddy sie ein zweites Mal an. Die alten Einträge der Bridge kannst du löschen.

Beide verbinden sich gleichzeitig per MQTT mit dem Drucker. Das funktioniert normalerweise. Fällt einer von beiden immer wieder raus, liegt es am Verbindungslimit des Druckers.

---

## 9. Alltag

### X2D: Fremdspule mit Sticker

1. Die Spule an den Leser halten oder auf die Waage legen. Die LED meldet «erkannt», auf der Statusseite steht «… bereit – jetzt in einen AMS-Slot stecken».
2. Innerhalb von 60 s die Spule in einen beliebigen AMS-Slot stecken.
3. Die Bridge stellt den Slot ein, liest die Bestätigung des Druckers zurück und setzt den Lagerort in Spoolman.

Die umgekehrte Reihenfolge geht auch: zuerst einstecken, dann innerhalb von 60 s scannen. Mehrere Spulen immer **nacheinander** einlegen, nicht gleichzeitig.

### X2D: Original-Bambu-Spule

Einfach ins AMS stecken, **nicht scannen**. Sobald das AMS den RFID-Chip gelesen hat:
- **Standard-Modus:** Die Bridge sucht die passende Spule und legt nur dann eine neue an, wenn es keine gibt. Gesucht wird in dieser Reihenfolge: über die Bambu-Spulen-UUID, über die Tag-Kennung, über die Spule, die gerade per Scan diesem Slot zugewiesen wurde, und über eine eindeutig passende Bambu-Spule ohne UUID (z. B. vorher über die Waage angelegt). Bei einer gefundenen Spule trägt sie die UUID nach und behält das Restgewicht der Waage. Danach setzt sie den Lagerort und führt die Restmenge nach, die das AMS meldet.
- **Bambuddy-Modus:** Bambuddy erledigt das (Abschnitt 8).

In beiden Fällen merkt sich die Bridge die **Kennung des RFID-Chips** im Feld *NFC Tag*. Danach erkennen Leser und Waage die Bambu-Spule. Jede Bambu-Spule hat **zwei Chips, einen auf jeder Seite**. Gelernt wird nur der, den das AMS gelesen hat. Die andere Seite verknüpfst du einmal auf der Statusseite mit derselben Spule.

### Spule entnehmen

Nimmst du eine Spule aus dem AMS, stellt die Bridge sie in Spoolman an ihren **Stammplatz** zurück, also an den Ort, an dem sie vor dem Einstecken war, z. B. `PLA`. Die Bridge merkt ihn sich im Feld *Stammplatz*. Hatte die Spule keinen Ort, kommt sie nach `storage_location` (Standard: `Lager`). Im Bambuddy-Modus fasst die Bridge Bambu-Spulen dabei nicht an.

### SV08

Spule scannen oder auf die Waage legen, fertig.

### Manuell korrigieren

Auf der Statusseite mit der Maus über einen belegten Slot fahren, eine Spule wählen und **Setzen** klicken.

---

## 10. Verbrauchserfassung

| Drucker / Spule | Wie |
|---|---|
| **SV08** | automatisch über Moonraker (Abschnitt 6) |
| **X2D, Bambu-Spule, Bambuddy-Modus** | Bambuddy bucht pro Druck aus der 3MF-Datei ab, auch bei abgebrochenen Drucken |
| **X2D, Bambu-Spule, Standard-Modus** | Die Bridge übernimmt die Prozent-Schätzung des AMS als Restgewicht (etwa ±10 g pro 1-kg-Spule). Dafür muss in den AMS-Einstellungen das **Schätzen der Restmenge** aktiv sein |
| **X2D, Fremdspule** | Das AMS meldet keine Restmenge. Das Restgewicht wird bei jedem Auflegen auf die **Waage** genau nachgeführt |

Bis eine neue Spule das erste Mal gewogen wird, steht in Spoolman das volle Nenngewicht.

---

## 11. Konfiguration (config.yaml)

Vorlage: [`config.example.yaml`](config.example.yaml)

| Schlüssel | Bedeutung | Standard |
|---|---|---|
| `spoolman_url` | Adresse von Spoolman, ohne `/api` | – (Pflicht) |
| `storage_location` | Lagerort für entnommene Spulen ohne Stammplatz | `Lager` |
| `window_seconds` | Zeitfenster zwischen Scan und Einstecken | `60` |
| `port` | Port für Statusseite und Leser-API | `8099` |

**Drucker** (`printers:`), Bambu:

| Schlüssel | Bedeutung |
|---|---|
| `type: bambu` | |
| `host` | IP des Druckers |
| `serial` | Seriennummer (in Anführungszeichen) |
| `access_code` | Zugangscode (in Anführungszeichen) |
| `location` | Lagerort-Präfix in Spoolman, z. B. `X2D` |
| `bambu_rfid` | `bridge` (Standard) oder `bambuddy` (Abschnitt 8) |
| `slot_location` | Ortsformat für AMS-Slots mit `{printer}`, `{unit}`, `{slot}`. Standard: `{printer} AMS {unit}{slot}` → `X2D AMS A1`, im Bambuddy-Modus `{printer} - AMS {unit} Slot {slot}` |

**Drucker**, Klipper:

| Schlüssel | Bedeutung |
|---|---|
| `type: moonraker` | |
| `url` | Moonraker-Adresse, z. B. `http://<SV08-IP>:7125` |
| `location` | Lagerort in Spoolman, z. B. `SV08` |

**Leser** (`readers:`): `<reader_id>: <Druckername>`. Ohne Druckername trägt der Leser bzw. die Waage nur das Gewicht ein.

Nach Änderungen die App in TrueNAS neu starten.

---

## 12. Fehlersuche und Diagnose

| Symptom | Ursache / Lösung |
|---|---|
| X2D rot, Log: `MQTT-Verbindung abgelehnt` | Access-Code falsch oder Developer-Modus aus (Abschnitt 2) |
| X2D rot, keine Log-Meldung | falsche IP oder Drucker aus. `host` prüfen |
| Alle AMS-Slots «leer», obwohl Spulen drin sind | `http://<NAS-IP>:8099/api/debug` öffnen und den Inhalt melden. Dort stehen die Rohmeldungen des Druckers |
| Log: `Befehl gesendet, aber keine Bestätigung` | Der Drucker hat die Slot-Einstellung nicht übernommen. Slot am Display prüfen, `/api/debug` anschauen |
| LED bleibt aus beim Scan | PN532 im falschen Modus (DIP-Schalter, danach stromlos machen) oder Verdrahtung falsch. ESPHome-Log: `PN532 not found` bzw. kein Gerät auf 0x24 |
| LED 5× schnell / rot | Bridge nicht erreichbar. `bridge_url` prüfen (Port **8099**), `http://<NAS-IP>:8099/health` im Browser testen |
| ESPHome-Log: `Code: 404` | normal bei einem unbekannten Tag, auf der Statusseite zuweisen |
| Scan kommt nicht an, Statusseite zeigt nichts | Läuft die neue Firmware wirklich? Gerätename im ESPHome-Log bzw. Router prüfen, ggf. mit `use_address` neu flashen (Abschnitt 4.2) |
| Gerät in ESPHome «offline» | ESPHome findet es nicht unter dem Namen aus der YAML. `use_address` setzen. Für Logs **Logs → Wirelessly** wählen |
| Log: `Unbekannter Leser` | `reader_id` in ESPHome stimmt nicht mit `readers:` in der `config.yaml` überein |
| Falscher Slot zugeordnet | Zwei Spulen innerhalb von 60 s eingesteckt. Immer nacheinander, oder auf der Statusseite korrigieren |
| Bambu-Spule erscheint nicht in Spoolman | Das AMS braucht nach dem Einstecken einige Sekunden für den RFID-Chip. Im Bambuddy-Modus den Bambuddy-Sync prüfen |
| Restmenge einer Bambu-Spule bleibt stehen | Standard-Modus: das Schätzen der Restmenge in den AMS-Einstellungen aktivieren |
| Waage: «kein Leergewicht» im Log | Leergewicht bei Spule, Filament oder Hersteller in Spoolman eintragen (Abschnitt 5.5) |
| Waage-Karte fehlt auf der Statusseite | Die Waage meldet sich noch nicht. Firmware mit `bridge: http://<NAS-IP>:8099` geflasht? ESPHome-Log prüfen |
| Waage zeigt «nicht kalibriert» | Tara, dann mit bekanntem Gewicht Kalibrieren (Abschnitt 5.3) |
| Update kommt nicht an | `docker pull …` und **Stop/Start** statt Restart (Abschnitt 3.5) |
| TrueNAS: `unauthorized` beim Start | Das Docker-Paket ist noch privat (Abschnitt 3.1) |

**Endpunkte der Bridge**

| Adresse | Inhalt |
|---|---|
| `/` | Statusseite |
| `/health` | `{"ok": true}`, wenn die Bridge läuft |
| `/api/state` | aktueller Stand als JSON |
| `/api/debug` | Rohdaten der AMS-Meldungen (letzte 5) |
| `POST /api/scan` | Scan vom Leser: `{"reader", "uid", "weight"}` |
| `POST /api/weight` | laufende Meldung der Waage: `{"reader", "uid", "weight"}`. Antwort enthält Befehle (`tare`, `cal:<g>`) |
| `POST /api/scale` | Befehl an die Waage: `{"reader", "cmd": "tare"}` oder `{"reader", "cmd": "calibrate", "grams": 1250}` |

---

## 13. Bekannte Grenzen

- **Automatische Slot-Erkennung nur für AMS-Slots** (AMS / AMS 2 Pro, über die Belegungsbits des Druckers). Die externe Spule zeigt die Bridge noch nicht an. AMS HT ist nicht getestet.
- **K-Wert (Pressure Advance)** wird nicht gesetzt. Der Drucker nutzt seine eigene Kalibrierung.
- **Slot-Befehl am X2D:** `ams_filament_setting` wird mit `ams_id`, `tray_id` und `slot_id` gesendet. Ob der X2D genau dieses Format annimmt, ist am Gerät noch zu bestätigen. Die Bridge liest nach jedem Setzen die Rückmeldung und warnt, wenn sie ausbleibt.
- **SV08 mit mehreren Werkzeugen (INDX) oder Tradrack/Happy Hare** wird noch nicht unterstützt. Der SV08 bekommt eine aktive Spule.

---

## Entwicklung

```sh
pip install -r requirements.txt pytest pytest-asyncio pytest-aiohttp
pytest -q
python -m bridge config/config.yaml
```
