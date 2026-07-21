# Meshcore BBSng

[![Latest Release](https://img.shields.io/github/v/release/radioactive-bbs/Meshcore-BBSng?label=Release&color=blue)](https://github.com/radioactive-bbs/Meshcore-BBSng/releases/latest)

Ein Bulletin-Board-System (BBS) für [MeshCore](https://meshcore.io/)-Mesh-Netzwerke — erreichbar über das MeshCore-Funknetz (Kanal-Broadcast + Direktnachrichten), mit einer eigenen HTTPS-Web-Admin-Oberfläche zur Verwaltung.

→ [Alle Release-Notes](https://github.com/radioactive-bbs/Meshcore-BBSng/releases)

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
- **Proaktive Zustellung** — neue private Nachrichten werden dem Empfänger sofort per Direktnachricht mit vollem Inhalt zugestellt (kein Umweg über `NL`/`R<n>` nötig, gilt weiter als ungelesen bis explizit gelesen), plus eine einmalige Erinnerung 3 Tage vor Löschung einer ungelesenen Nachricht (Löschfrist konfigurierbar, Default 30 Tage). Ist der Empfänger gerade nicht erreichbar, zeigt das Hauptmenü beim nächsten Login einen Badge mit der Anzahl ungelesener Nachrichten
- **Board/Bulletins** — öffentliche Nachrichten, sticky-Flag (nie automatisch gelöscht), automatische Aufräumung nach konfigurierbarer Frist
- **Self-Service-Registrierung** — Nutzer registrieren sich per `add` direkt über den MeshCore-Kanal, kein manuelles Anlegen durch den SysOp nötig. Drei Modi wählbar (Web-Admin -> Einstellungen): Pubkey-Bestätigung per Direktnachricht-Challenge (Status quo, verhindert dass sich jemand einen fremden Rufzeichen-Namen unter dem eigenen Pubkey sichert), sofortige Freischaltung ohne Prüfung, oder manuelle Freischaltung durch den SysOp im Web-Admin (siehe [Self-Service-Registrierung](#self-service-registrierung-nur-meshcore-kanal))
- **Kontakt-Einladung per QR/Link** — die BBS schickt eine `meshcore://contact/<pubkey>`-URI, die die MeshCore-App direkt als "Kontakt hinzufügen"-Dialog anbietet
- **Inaktivitäts-Bereinigung** — User ohne jede BBS-Aktivität werden nach konfigurierbarer Frist (Default 60 Tage) automatisch entfernt, mit bis zu 3 frei einstellbaren Erinnerungs-DMs vorher (siehe [Inaktivitäts-Bereinigung](#inaktivitäts-bereinigung))
- **Pubkey-Sicherheitshinweis & Senderecht** — vor dem ersten Senden muss jeder User per Direktnachricht-Challenge bestätigen, dass der Pubkey (nicht der Name) die Identität beweist; der SysOp kann das Senderecht je User im Web-Admin dauerhaft sperren/entsperren
- **Wetter-Integration** — aktuelle Werte + 1-/3-Tage-Vorhersage über eine angebundene [Home Assistant](https://www.home-assistant.io/)-Instanz
- **PING/Traceroute** — Pfad- und Laufzeitmessung zu einzelnen Nodes/Repeatern im Mesh, mit automatischem Retry bei Paketverlust
- **Feature-Flags** — jede Funktionsgruppe (Nachrichten, Board, Wetter, Sysinfo, Online-Liste, Userliste, PING, Account, Self-Service) einzeln im Web-Admin abschaltbar, wirkt sofort ohne Neustart

### Zugangsweg
- **MeshCore-Funknetz** — Kanal-Broadcasts (öffentlich) und Direktnachrichten (privat) über das serielle Companion-Protokoll eines angeschlossenen MeshCore-Node (z. B. Heltec WiFi LoRa 32)

### Web-Admin (HTTPS)
- Dashboard mit Node-/Serial-Status, Region-Scope-Bestätigung, Nachrichtenstatistik
- Nutzerverwaltung (registrieren, sperren, Mail-Kontakt setzen, Senderecht sperren/entsperren, ausstehende Freischaltungen genehmigen/ablehnen im `sysop_approval`-Modus)
- Nachrichtenverwaltung (Board: Volltext + Sticky-Toggle; Privat: nur Metadaten)
- Live-Einstellungen: TX-Power, Path-Hash-Mode, Region-Scope, Kanalname — wirken sofort am Node, kein Neustart nötig
- Registrierungs- und Inaktivitäts-Einstellungen: Registrierungsmodus, Inaktivitätsfrist, Warnschwellen, Nachrichten-Löschverhalten bei User-Entfernung
- Statistik-Dashboard: Nachrichtenaufkommen, Routing-Art (Flood / Direkt bestätigt / Multihop / Pfad unbekannt), SNR-Verlauf je Nutzer
- Debug-Ansicht mit Live-Journal-Log (journalctl-Anbindung)
- Datenbank-Backup-Download (konsistenter SQLite-Snapshot)
- Eigenes self-signed HTTPS-Zertifikat (automatisch erzeugt) oder Import eines eigenen Zertifikats
- **Co-SysOps** — weitere Rufzeichen mit SysOp-Rechten im Mesh (z. B. Nachrichten löschen), unter *Einstellungen* pflegbar
- **Mehrere Admin-Konten** — zusätzlich zum Standardkonto `admin` beliebig viele weitere Web-Admin-Logins mit eigenem Benutzernamen/Passwort anlegbar (gleichberechtigt, keine Rollen), unter *Einstellungen* verwaltbar

### MeshCore-Protokolldetails
- **Region-Scoping** — alle Flood-Pakete (Kanal-Broadcasts, Flood-DMs, Adverts) werden mit einem Region-Code versehen, der auf Repeatern gefiltert werden kann (Firmware ≥ v1.15 für persistenten Default-Scope)
- **Best-Effort-Multihop-DMs** — Direktnachrichten über bekannte Pfade, automatischer Fallback auf Flood bei ausbleibendem ACK
- **V3-Protokoll** (3-Byte-Pfad-Header) mit automatischem Downgrade-Schutz bei Node-Neustart

## BBS-Befehle

Alle Befehle laufen über den MeshCore-Kanal (Broadcast) bzw. Direktnachrichten. Groß-/Kleinschreibung ist egal.

### Navigation (zeigt Untermenü)

| Befehl | Bedeutung |
|---|---|
| `H` / `?` | Hauptmenü |
| `N` | Nachrichten-Menü |
| `B` | Board-Menü |
| `W` | Wetter-Menü |
| `I` | Info-Menü |
| `A` | Account-Menü |

### Nachrichten & Board

| Befehl | Bedeutung |
|---|---|
| `NL` | Eigene Nachrichtenliste (neueste zuerst) |
| `NLO <n>` | Weitere Nachrichten ab Position `n` |
| `BL` | Board-Liste (Sticky zuerst) |
| `BLO <n>` | Weitere Board-Einträge ab Position `n` |
| `R <nr>` | Nachricht/Board-Eintrag `<nr>` lesen |
| `S TO\|Betreff\|Text` | Private Nachricht senden |
| `SB Thema\|Text` | Board-Nachricht (Bulletin) veröffentlichen |
| `ND <nr>` / `K <nr>` | Nachricht `<nr>` löschen — bei privaten Nachrichten nur der Empfänger, bei Board-Bulletins nur der Autor, zusätzlich immer der SysOp und die konfigurierten Co-SysOps (auch als `ND<nr>`/`K<nr>` ohne Leerzeichen) |

Befehle mit Zahlenargument (`R`, `NLO`, `BLO`, `ND`, `K`) akzeptieren die Nummer wahlweise mit Leerzeichen (`R 5`) oder direkt angehängt (`R5`).

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
| `O` | Wer ist gerade online/aktiv |
| `LU` | Liste aller registrierten Nutzer |
| `PING` | Liste bekannter Repeater |
| `PING <Name>` | Traceroute zu einem Node/Repeater — Pfad, Laufzeit, SNR je Hop |
| `PK` | Eigener voller Pubkey (64 Hex), zur Weitergabe an andere |
| `PK <Name>` | Voller Pubkey (64 Hex) eines Kontakts — vor dem Senden abgleichen, da Namen fälschbar/duplizierbar sind |
| `MI` | Eigene Account-Info |
| `MC <mail>` | Mail-Kontaktadresse hinterlegen, z. B. `MC name@example.com` |
| `REMOVE` | Eigene Registrierung löschen (nur per Direktnachricht) |

### Pubkey-Sicherheitshinweis (einmalig pro User)

Vor dem ersten `S`/`SB` muss jeder User (auch Bestandsuser) per Direktnachricht bestätigen, dass er verstanden hat: der angezeigte **Name** ist kein Identitätsnachweis — nur der **Pubkey** ist verlässlich. Ein Sendeversuch ohne Bestätigung liefert eine Fehlermeldung plus den Hinweistext mit einem 6-stelligen Code; Antwort per `OK <Code>` (15 Min. gültig) schaltet `S`/`SB` frei. Andere Befehle (`H`, `NL`, `R`, `WX`, ...) bleiben in der Zwischenzeit normal nutzbar.

### Self-Service-Registrierung (nur MeshCore-Kanal)

Neue Nutzer registrieren sich selbst über eine Nachricht im öffentlichen BBS-Kanal — kein Zutun des SysOp nötig. Der Modus, wie eine Anmeldung abgeschlossen wird, ist unter **Web-Admin -> Einstellungen -> Registrierung** wählbar (`registration.mode` in `config.yaml`):

- **`challenge`** (Status quo) — der eigene Pubkey wird per Rückfrage-Code bestätigt, damit niemand einen fremden Rufzeichen-Namen unter seinem eigenen Pubkey registrieren kann.
- **`open`** — der Account ist sofort aktiv, ohne Pubkey-Prüfung.
- **`sysop_approval`** — der Antrag landet im Web-Admin unter *Benutzer -> Ausstehende Freischaltungen*; der SysOp bekommt eine Hinweis-DM und schaltet manuell frei oder lehnt ab.

**1. Registrierung beantragen** — im Kanal senden:

```
add BENUTZERNAME:PUBKEY
```

`BENUTZERNAME` ist frei wählbar (3–16 Zeichen, Buchstaben/Zahlen/`+-.!"§$%&/()=`). `PUBKEY` ist der eigene 64-stellige Hex-Pubkey des MeshCore-Node (in der MeshCore-App unter den eigenen Geräte-Details zu finden).

**2. Sofortige Antworten im Kanal** — die BBS bestätigt den Antrag, schickt eine `meshcore://contact/<pubkey>`-Einladung (die MeshCore-App bietet damit direkt einen "Kontakt hinzufügen"-Dialog an) als eigene Nachricht, und weist je nach Modus auf die nächsten Schritte hin (Bestätigungscode per DM, sofort aktiv, oder Warten auf SysOp-Freischaltung). **Wichtig:** in dieser Zeit den BBS-Kontakt über den Link anlegen, sonst kann die BBS später keine Direktnachricht zustellen.

**3. Abschluss je nach Modus:**
- `challenge`: rund 10 Minuten nach dem Antrag schickt die BBS eine Direktnachricht mit einem 6-stelligen Code, der **als Antwort per Direktnachricht** zurückgeschickt werden muss. Bei korrektem Code ist der Account sofort aktiv, die BBS bestätigt per DM und der SysOp erhält automatisch eine Benachrichtigung. Kommt innerhalb von 10 Minuten nach der Code-DM keine (korrekte) Antwort, verfällt der Antrag automatisch — der Benutzername wird wieder frei. Ohne Bestätigung landet **kein** Eintrag in der Nutzerdatenbank — der behauptete Pubkey allein reicht nicht aus.
- `open`: der Account ist sofort aktiv, die BBS schickt direkt eine Willkommens-DM, der SysOp wird informiert.
- `sysop_approval`: keine automatische Aktion — der SysOp schaltet im Web-Admin frei oder lehnt ab, der Nutzer bekommt das per DM mitgeteilt.

Zum Schutz vor Missbrauch/Spam sind maximal 2 neue Registrierungsanträge pro Minute zulässig; weitere `add`-Versuche werden in dieser Zeit mit einer Fehlermeldung abgewiesen.

Danach: `REMOVE` als Direktnachricht löscht die eigene Registrierung jederzeit wieder.

### Inaktivitäts-Bereinigung

Nutzer ohne jede BBS-Aktivität (jede angenommene Direktnachricht zählt) werden nach `users.inactivity_days` (Default 60 Tage, im Web-Admin unter *Einstellungen* änderbar) automatisch entfernt. Vor der Entfernung verschickt die BBS Erinnerungs-DMs mit Hinweis auf die bevorstehende Löschung — wann, ist über `users.inactivity_warn_before_days` einstellbar (bis zu 3 Werte, Tage *vor* der Entfernung, Default `[10, 5, 1]`; weniger als 3 Werte = entsprechend weniger Warnungen, leer = keine Warnung).

Bei jeder Art der Entfernung (Inaktivität, Web-Admin *Entfernen*/*Sperren*, Self-Service `REMOVE`) werden empfangene private Nachrichten immer gelöscht. Ob zusätzlich auch vom entfernten User **gesendete** private Nachrichten bzw. eigene Board-Bulletins gelöscht werden, steuert getrennt `users.delete_sent_private_messages` und `users.delete_sent_board_messages` (beide Default an, im Web-Admin unter *Einstellungen -> Registrierung* einzeln umschaltbar).

## Installation

### Voraussetzungen

- Raspberry Pi (oder anderer Linux-Host) mit Python 3.11+
- Ein MeshCore-fähiges LoRa-Gerät (getestet: Heltec WiFi LoRa 32 v4) mit Companion-Firmware, per USB angeschlossen
- Für die Wetter-Integration (optional): eine erreichbare Home-Assistant-Instanz mit Long-Lived-Access-Token
- SSH-Zugang mit einem `sudo`-fähigen Benutzer (muss **nicht** `coreadmin` selbst sein — `setup_pi.sh` prüft, ob der dedizierte Service-User `coreadmin` bereits existiert, und legt ihn bei Bedarf automatisch an, siehe unten)

### Automatische Ersteinrichtung (Raspberry Pi)

```bash
git clone -b main https://github.com/radioactive-bbs/Meshcore-BBSng.git
cd Meshcore-BBSng
bash scripts/setup_pi.sh
```

Das Skript ist idempotent (mehrfach ausführbar) und richtet automatisch ein:

1. Systempakete (Python, Build-Header für `cryptography`)
2. Dedizierten Service-User `coreadmin` — wird geprüft (`id coreadmin`) und **nur bei Bedarf** neu angelegt (`useradd -m`), inkl. `dialout`-Gruppe für den seriellen Port. Existiert der User bereits, wird dieser Schritt übersprungen.
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

Web-Admin danach erreichbar unter `https://<Server-IP>:8080` (self-signed Zertifikat, Browser-Warnung beim ersten Zugriff bestätigen).

### Manuelle Installation (ohne `setup_pi.sh`)

Kein dedizierter `coreadmin`-User nötig — läuft unter dem aktuell angemeldeten Benutzer.

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
| `web` | `enabled`, `host`, `port`, `tls.*` (HTTPS-Zertifikat) |
| `meshcore` | `serial_port`, `baud_rate`, `channel`, `channel_name`, `channel_region`, `tx_power`, `path_hash_mode`, `contacts` |
| `storage` | `path` (SQLite-Datei) |
| `board` | `retention_days` |
| `messages` | `max_personal` (Postfach-Limit), `unread_retention_days` (Löschfrist ungelesener Nachrichten, Erinnerung 3 Tage vorher) |
| `registration` | `mode` (`challenge`/`open`/`sysop_approval`, siehe [Self-Service-Registrierung](#self-service-registrierung-nur-meshcore-kanal)) |
| `users` | `inactivity_days` (automatische Entfernung nach N Tagen Inaktivität), `inactivity_warn_before_days` (bis zu 3 Warn-DMs, Tage vor der Entfernung), `delete_sent_private_messages`/`delete_sent_board_messages` (gesendete Nachrichten bzw. Bulletins bei Entfernung getrennt mitlöschen) |
| `homeassistant` | `url`, `verify_ssl` (Token separat in `secrets.yaml`) |

Viele Optionen (TX-Power, Path-Hash-Mode, Region-Scope, Kanalname, Feature-Flags, Betreiberdaten) sind zusätzlich **live im Web-Admin unter *Einstellungen*** änderbar und wirken sofort ohne Neustart.

## Architektur

```
main.py                    Einstiegspunkt, Config-Merge, Service-Start
core/
  bbs.py                   BBS-Logik: Befehle, Menues, Feature-Flags
  crypto.py                  At-Rest-Verschluesselung, Passwort-Hashing
  validation.py               Rufzeichen/Namen-Validierung
  sanitize.py                  Log-Ausgabe-Bereinigung
  weather.py                    Home-Assistant-Wetter-Client
  webtls.py                      Self-signed-Zertifikat-Erzeugung
protocols/
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
