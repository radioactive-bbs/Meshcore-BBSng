# Meshcore BBSng

Ein Bulletin-Board-System (BBS) für [MeshCore](https://meshcore.co.uk/)-Mesh-Netzwerke — erreichbar per Telnet und über das MeshCore-Funknetz (Kanal-Broadcast + Direktnachrichten), mit einer eigenen HTTPS-Web-Admin-Oberfläche zur Verwaltung.

Klassisches BBS-Feeling (private Nachrichten, Board/Bulletins, Wetterabfrage) auf moderner MeshCore-LoRa-Hardware.

```
███╗   ███╗███████╗███████╗██╗  ██╗ ██████╗ ██████╗ ██████╗ ███████╗
████╗ ████║██╔════╝██╔════╝██║  ██║██╔════╝██╔═══██╗██╔══██╗██╔════╝
██╔████╔██║█████╗  ███████╗███████║██║     ██║   ██║██████╔╝█████╗
██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║██║     ██║   ██║██╔══██╗██╔══╝
██║ ╚═╝ ██║███████╗███████║██║  ██║╚██████╗╚██████╔╝██║  ██║███████╗
╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝
             ██████╗ ██████╗ ███████╗
             ██╔══██╗██╔══██╗██╔════╝
     ═══════ ██████╔╝██████╔╝███████╗ ██████╗  ██████╗
             ██╔══██╗██╔══██╗╚════██║ ██╔══██╗ ██╔══██╗
             ██████╔╝██████╔╝███████║ ██  ██   ██████╔╝
             ╚═════╝ ╚═════╝ ╚══════╝ ██  ██      ██║
                                                    ╚═╝
```

## Inhalt

- [Features](#features)
- [BBS-Befehle](#bbs-befehle)
- [Installation](#installation)
- [Konfiguration](#konfiguration)
- [Architektur](#architektur)
- [Lizenz](#lizenz)

## Features

### BBS-Kern
- **Private Nachrichten** — Postfach je Rufzeichen, konfigurierbares Limit, AES-256-GCM-verschlüsselt at-rest
- **Proaktive DM-Benachrichtigungen** — Empfänger bekommen sofort eine DM bei neuer Nachricht sowie eine einmalige Erinnerung 3 Tage vor Löschung einer ungelesenen Nachricht (Löschfrist konfigurierbar, Default 30 Tage)
- **Board/Bulletins** — öffentliche Nachrichten, sticky-Flag (nie automatisch gelöscht), automatische Aufräumung nach konfigurierbarer Frist
- **Self-Service-Registrierung** — Nutzer registrieren sich per `add` direkt über den MeshCore-Kanal, kein manuelles Anlegen durch den SysOp nötig
- **Kontakt-Einladung per QR/Link** — nach der Registrierung schickt die BBS eine `meshcore://contact/add`-URI, die die MeshCore-App direkt als "Kontakt hinzufügen"-Dialog anbietet
- **Wetter-Integration** — aktuelle Werte + 1-/3-Tage-Vorhersage über eine angebundene [Home Assistant](https://www.home-assistant.io/)-Instanz
- **PING/Traceroute** — Pfad- und Laufzeitmessung zu einzelnen Nodes/Repeatern im Mesh, mit automatischem Retry bei Paketverlust
- **Feature-Flags** — jede Funktionsgruppe (Nachrichten, Board, Wetter, Sysinfo, Online-Liste, Userliste, PING, Account, Self-Service) einzeln im Web-Admin abschaltbar, wirkt sofort ohne Neustart

### Zwei Zugangswege
- **Telnet** (lokal, unauthentifiziert) — klassisches Terminal-BBS-Erlebnis für den SysOp/lokale Nutzung
- **MeshCore-Funknetz** — Kanal-Broadcasts (öffentlich) und Direktnachrichten (privat) über das serielle Companion-Protokoll eines angeschlossenen MeshCore-Node (z. B. Heltec WiFi LoRa 32)

### Web-Admin (HTTPS)
- Dashboard mit Node-/Serial-Status, Region-Scope-Bestätigung, Nachrichtenstatistik
- Nutzerverwaltung (registrieren, sperren, Mail-Kontakt setzen)
- Nachrichtenverwaltung (Board: Volltext + Sticky-Toggle; Privat: nur Metadaten)
- Live-Einstellungen: TX-Power, Path-Hash-Mode, Region-Scope, Kanalname — wirken sofort am Node, kein Neustart nötig
- Statistik-Dashboard: Nachrichtenaufkommen, Routing-Art (Flood/Direkt/Multihop), SNR-Verlauf je Nutzer
- Debug-Ansicht mit Live-Journal-Log (journalctl-Anbindung)
- Datenbank-Backup-Download (konsistenter SQLite-Snapshot)
- Eigenes self-signed HTTPS-Zertifikat (automatisch erzeugt) oder Import eines eigenen Zertifikats

### MeshCore-Protokolldetails
- **Region-Scoping** — alle Flood-Pakete (Kanal-Broadcasts, Flood-DMs, Adverts) werden mit einem Region-Code versehen, der auf Repeatern gefiltert werden kann (Firmware ≥ v1.15 für persistenten Default-Scope)
- **Best-Effort-Multihop-DMs** — Direktnachrichten über bekannte Pfade, automatischer Fallback auf Flood bei ausbleibendem ACK
- **V3-Protokoll** (3-Byte-Pfad-Header) mit automatischem Downgrade-Schutz bei Node-Neustart

## BBS-Befehle

Die Befehle sind über **Telnet** und den **MeshCore-Kanal/DM** weitgehend identisch, mit ein paar plattformbedingten Unterschieden (siehe Fußnoten). Groß-/Kleinschreibung ist egal.

### Navigation (zeigt Untermenü)

| Befehl | Bedeutung |
|---|---|
| `H` / `?` | Hauptmenü |
| `N` | Nachrichten-Menü *(nur MeshCore — bei Telnet ist `N` "Namen setzen", siehe unten)* |
| `B` | Board-Menü |
| `W` | Wetter-Menü *(nur MeshCore — bei Telnet ist `W` "wer ist online")* |
| `I` | Info-Menü |
| `A` | Account-Menü *(nur MeshCore)* |

### Nachrichten & Board

| Befehl | Bedeutung |
|---|---|
| `NL` | Eigene Nachrichtenliste (neueste zuerst) |
| `NLO <n>` | Weitere Nachrichten ab Position `n` |
| `BL` | Board-Liste (Sticky zuerst) |
| `BLO <n>` | Weitere Board-Einträge ab Position `n` |
| `R <nr>` | Nachricht/Board-Eintrag `<nr>` lesen |
| `S TO\|Betreff\|Text` | Private Nachricht senden (Telnet: interaktiver Dialog mit `/EX` zum Absenden, `/ABORT` zum Abbrechen) |
| `SB Thema\|Text` | Board-Nachricht (Bulletin) veröffentlichen |
| `K <nr>` | Eigene Nachricht `<nr>` löschen |

### Wetter (Home-Assistant-Integration)

| Befehl | Bedeutung |
|---|---|
| `WX` | Aktuelles Wetter |
| `WX1` | Vorhersage morgen |
| `WX3` | Vorhersage 3 Tage |

### Info & Sonstiges

| Befehl | Bedeutung |
|---|---|
| `SI` | Sysinfo (Nutzerzahl, Nachrichten, aktive Sessions) |
| `O` | Wer ist gerade online/aktiv *(Telnet: `W`)* |
| `LU` | Liste aller registrierten Nutzer |
| `PING` | Liste bekannter Repeater *(nur MeshCore)* |
| `PING <Name>` | Traceroute zu einem Node/Repeater — Pfad, Laufzeit, SNR je Hop *(nur MeshCore)* |
| `MI` | Eigene Account-Info *(nur MeshCore)* |
| `MC <mail>` | Mail-Kontaktadresse hinterlegen, z. B. `MC name@example.com` *(nur MeshCore)* |
| `REMOVE` | Eigene Registrierung löschen (nur per Direktnachricht) *(nur MeshCore)* |
| `B` / `BYE` / `Q` / `QUIT` | Verbindung trennen *(nur Telnet)* |
| `N <Name>` | Eigenen Anzeigenamen setzen *(nur Telnet)* |

### Self-Service-Registrierung (nur MeshCore-Kanal)

```
add BENUTZERNAME:PUBKEY
```

`PUBKEY` ist der eigene 64-stellige Hex-Pubkey des MeshCore-Node. Nach erfolgreicher Registrierung schickt die BBS eine `meshcore://contact/add`-Einladung zurück in den Kanal — die MeshCore-App erkennt den Link und bietet direkt einen "Kontakt hinzufügen"-Dialog an.

## Installation

### Voraussetzungen

- Raspberry Pi (oder anderer Linux-Host) mit Python 3.11+
- Ein MeshCore-fähiges LoRa-Gerät (getestet: Heltec WiFi LoRa 32 v4) mit Companion-Firmware, per USB angeschlossen
- Für die Wetter-Integration (optional): eine erreichbare Home-Assistant-Instanz mit Long-Lived-Access-Token

### Automatische Ersteinrichtung (Raspberry Pi)

```bash
git clone -b main https://github.com/radioactive-bbs/Meshcore-BBSng.git
cd Meshcore-BBSng
bash scripts/setup_pi.sh
```

Das Skript ist idempotent (mehrfach ausführbar) und richtet automatisch ein:

1. Systempakete (Python, Build-Header für `cryptography`)
2. Dedizierten Service-User (`coreadmin`) inkl. `dialout`-Gruppe für den seriellen Port
3. Python-Virtualenv + Abhängigkeiten
4. `config/secrets.yaml` und `config/config.local.yaml` aus den Vorlagen (danach manuell mit echten Werten füllen)
5. At-Rest-Verschlüsselungsschlüssel als verschlüsseltes systemd-Credential (automatisch, kein manueller Schritt)
6. Web-Admin-Passwort (scrypt-Hash) — interaktive Eingabe oder automatisch generiertes Zufallspasswort
7. udev-Regel für einen stabilen `/dev/meshcore`-Symlink
8. systemd-Service (Autostart, automatischer Neustart bei Fehlern)
9. `sudo`-NOPASSWD-Regeln für Service-Steuerung und einfaches Deployment

Vor dem Ausführen ggf. anpassen (Kopf des Skripts): `REPO_URL`/`BRANCH` (bei eigenem Fork), `UDEV_VENDOR`/`UDEV_PRODUCT`/`UDEV_SERIAL` (bei anderer LoRa-Hardware — Seriennummer ermitteln mit `udevadm info -a -n /dev/ttyACM0 | grep '{serial}'`).

### Nach der Einrichtung

```bash
# Home-Assistant-Token eintragen (fuer Wetter-Feature)
sudo -u coreadmin nano /home/coreadmin/nnp-bbs/config/secrets.yaml

# Eigene Betreiberdaten eintragen (Rufzeichen, QTH, MeshCore-Kanal/-Kontakte)
sudo -u coreadmin nano /home/coreadmin/nnp-bbs/config/config.local.yaml

# MeshCore-Node per USB anschliessen, dann starten
sudo systemctl start nnp-bbs

# Logs verfolgen
journalctl -fu nnp-bbs
```

Web-Admin danach erreichbar unter `https://<Server-IP>:8080` (self-signed Zertifikat, Browser-Warnung beim ersten Zugriff bestätigen). Telnet lokal unter `localhost:6300`.

### Manuelle Installation (ohne `setup_pi.sh`)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config/secrets.yaml.example config/secrets.yaml       # HA-Token eintragen
cp config/config.local.yaml.example config/config.local.yaml   # eigene Betreiberdaten eintragen

# At-Rest-Schluessel erzeugen und eintragen (sonst unverschluesselte Nachrichten!)
python -c "from core.crypto import generate_key; print(generate_key())"
# -> Ausgabe als storage.messages_key in secrets.yaml eintragen

python main.py
```

### Update

```bash
sudo -u coreadmin git -C /home/coreadmin/nnp-bbs pull && sudo systemctl restart nnp-bbs
```

## Konfiguration

Konfiguration wird in dieser Reihenfolge geladen und zusammengeführt (spätere Stufen überschreiben gleichnamige Keys der vorherigen):

```
config/config.yaml            # generische Defaults (dieses Repo)
  -> config/config.local.yaml   # eigene Betreiberdaten (gitignored)
    -> config/webconfig.yaml      # Live-Einstellungen aus dem Web-Admin (gitignored)
      -> config/secrets.yaml        # Geheimnisse: HA-Token, ggf. At-Rest-Schluessel (gitignored)
```

Wichtige Optionen in `config/config.yaml` (Details/Kommentare direkt in der Datei):

| Bereich | Optionen |
|---|---|
| `telnet` | `enabled`, `host` (Default `127.0.0.1`, unauthentifiziert per Design), `port` |
| `web` | `enabled`, `host`, `port`, `tls.*` (HTTPS-Zertifikat) |
| `meshcore` | `serial_port`, `baud_rate`, `channel`, `channel_name`, `channel_region`, `tx_power`, `path_hash_mode`, `contacts` |
| `storage` | `path` (SQLite-Datei) |
| `board` | `retention_days` |
| `messages` | `max_personal` (Postfach-Limit), `unread_retention_days` (Löschfrist ungelesener Nachrichten, Erinnerung 3 Tage vorher) |
| `homeassistant` | `url`, `verify_ssl` (Token separat in `secrets.yaml`) |

Viele Optionen (TX-Power, Path-Hash-Mode, Region-Scope, Kanalname, Feature-Flags, Betreiberdaten) sind zusätzlich **live im Web-Admin unter *Einstellungen*** änderbar und wirken sofort ohne Neustart.

## Architektur

```
main.py                    Einstiegspunkt, Config-Merge, Service-Start
core/
  bbs.py                   BBS-Logik: Befehle, Menues, Feature-Flags
  session.py                Telnet-Session-State-Machine
  crypto.py                  At-Rest-Verschluesselung, Passwort-Hashing
  validation.py               gemeinsame Rufzeichen/Namen-Validierung
  sanitize.py                  Terminal-/Log-Ausgabe-Bereinigung
  weather.py                    Home-Assistant-Wetter-Client
  webtls.py                      Self-signed-Zertifikat-Erzeugung
protocols/
  telnet/server.py           Telnet-Server (RFC-854-Negotiation)
  meshcore/
    server.py                 MeshCore-Companion-Protokoll, Frame-Dispatch
    packet.py                  Frame-Encoding/-Parsing, Kommando-Konstruktoren
  web/server.py               HTTPS-Web-Admin (aiohttp)
storage/database.py         SQLite-Zugriffsschicht (aiosqlite, parametrisiert)
scripts/
  setup_pi.sh                Automatische Ersteinrichtung
  set_web_password.py         Web-Admin-Passwort setzen (CLI)
  sync_github.sh               Maintainer-Tool: internen Stand oeffentlich synchronisieren
```

**Tech-Stack**: Python 3.11+, `asyncio`, `aiohttp` (Web-Admin), `aiosqlite`, `aioserial` (MeshCore-Companion-Protokoll), `cryptography` (AES-256-GCM), `pyyaml`.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
