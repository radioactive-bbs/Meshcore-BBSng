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

Diese README ist in drei Blöcke gegliedert:

- **[Für User: BBS-Befehle](#für-user-bbs-befehle)** — alles, was man als Nutzer über den MeshCore-Kanal senden kann
- **[Für SysAdmins: Web-Admin & Features](#für-sysadmins-web-admin--features)** — Web-Admin-Oberfläche, Feature-Übersicht, Protokolldetails, Konfiguration
- **[Installation & Sicherheit](#installation--sicherheit)** — Einrichtung, Update, Verschlüsselung/Zugangsschutz, Architektur, Lizenz

## Für User: BBS-Befehle

Alle Befehle laufen über den MeshCore-Kanal (Broadcast) bzw. Direktnachrichten. Groß-/Kleinschreibung ist egal.

### Cheatsheet (Kurzübersicht)

Jede Zeile zeigt Kürzel **und** deutsche Langform (in Klammern) — beide funktionieren immer gleichwertig.

| Befehl | | Befehl | |
|---|---|---|---|
| `H` / `?` | **[H]auptmenü** | `WX` (`WETTER`) / `WX1` (`MORGEN`) / `WX3` (`DREITAGE`) | Wetter: aktuell / morgen / 3 Tage |
| `N` (`NACHRICHTEN`) · `B` (`BOARD`) · `W` · `I` (`INFO`) · `A` (`ACCOUNT`) | **[N]achrichten** · **[B]oard** · **[W]etter** · **[I]nfo** · **[A]ccount** | `SI` (`SYSINFO`) | **[S]ys[I]nfo** |
| `NL` / `NLO <n>` (`NACHRICHTENLISTE [<n>]`) | **[N]achrichten[L]iste** / weitere ab `n` | `O` (`ONLINE`) | **[O]nline** |
| `BL` / `BLO <n>` (`BOARDLISTE [<n>]`) | **[B]oard-[L]iste** (ein Eintrag je Thread) / weitere ab `n` | `LU` (`USERLISTE`) | **[L]iste [U]ser** |
| `R <nr>` (`LESEN <nr>`) | Nachricht/Board-Eintrag lesen | `PING` / `PING <Name>` | Repeaterliste (max. 15) / Traceroute |
| `S TO\|Betreff\|Text` (`SENDEN`) | **[S]enden** – private Nachricht | `PK` / `PK <Name>` (`PUBKEY`) | **[P]ub[K]ey** – eigener / fremder |
| `RS<nr>\|Text` (`ANTWORT<nr>\|Text`) | Antwort (Empfänger/Betreff automatisch) | `MI` (`MEINEINFO`) | **[M]eine [I]nfo** |
| `SB Thema\|Text` (`BULLETIN`) | **[S]enden [B]ulletin** (veröffentlichen) | `MC <mail>` (`MAIL <mail>`) | **[M]ail [C]ontact** setzen |
| `SBR<nr>\|Text` (`BULLETINANTWORT<nr>\|Text`) | Antwort auf ein Bulletin (hängt am Thread) | `OK <Code>` | Pubkey-Sicherheitshinweis bestätigen |
| `BT <nr>` (`BOARDTHREAD <nr>`) | **[B]oard-[T]hread** aufklappen (Anfang + Antworten) | `add NAME:PUBKEY` | Registrieren (nur Kanal) |
| `NT <nr>` (`NACHRICHTENTHREAD <nr>`) | **[N]achrichten[T]hread** – eigener Verlauf, markiert als gelesen | | |
| `ND <nr>` / `K <nr>` (`LOESCHEN <nr>`) | Nachricht/Bulletin löschen (eigene, **mit Rückfrage**) | `REMOVE` | Abmelden (nur Direktnachricht, **mit Rückfrage**) |

Zahlenargumente bei den **Kürzeln** (`R`, `NLO`, `BLO`, `ND`, `K`, `BL`, `NL`, `BT`, `NT`) auch direkt angehängt: `R5` = `R 5`. Bei den deutschen Langformen (`LESEN`, `LOESCHEN`, `BOARDLISTE`, `NACHRICHTENLISTE`, ...) immer mit Leerzeichen: `LESEN 5`. Details, Berechtigungen und Grenzfälle siehe die Tabellen unten.

Zum Ausdrucken gibt es außerdem eine Kreditkarten-große Steckkarten-Version (Vorder-/Rückseite, zum Ausschneiden und Laminieren): [`docs/cheatsheet.html`](docs/cheatsheet.html) im Browser öffnen und drucken (`Drucken → Tatsächliche Größe`).

### Navigation (zeigt Untermenü)

| Befehl | Langform | Bedeutung |
|---|---|---|
| `H` / `?` | `HELP` | **[H]auptmenü** |
| `N` | `NACHRICHTEN` | **[N]achrichten**-Menü |
| `B` | `BOARD` | **[B]oard**-Menü |
| `W` | *(`WETTER` liefert direkt die Daten, siehe unten)* | **[W]etter**-Menü |
| `I` | `INFO` | **[I]nfo**-Menü |
| `A` | `ACCOUNT` | **[A]ccount**-Menü |

### Nachrichten & Board

| Befehl | Langform | Bedeutung |
|---|---|---|
| `NL` / `NLO <n>` | `NACHRICHTENLISTE [<n>]` | Eigene **[N]achrichten[L]iste** (neueste Aktivität zuerst); **ein Eintrag je Thread** mit einer bereits empfangenen Person — Zähler `(gesamt)` hinter dem Betreff, bei ungelesenen Nachrichten `(gesamt/neu)`, z. B. `(5/4)`. Mit Zahlenargument weitere ab Position `n` (gezählt werden Threads). `NLO` ist eine weiterhin funktionierende Alt-Form |
| `BL` / `BLO <n>` | `BOARDLISTE [<n>]` | **[B]oard-[L]iste** (Sticky zuerst); **ein Eintrag je Thread** — Zähler `(gesamt)` hinter dem Thema, bei Aktivität seit deinem letzten `BL`-Aufruf `(gesamt/neu)`, z. B. `(5/4)`. Datum = letzte Aktivität, Threads mit frischer Antwort stehen oben. Mit Zahlenargument weitere ab Position `n` (gezählt werden Threads). `BLO` ist ebenso eine weiterhin funktionierende Alt-Form |
| `BT <nr>` | `BOARDTHREAD <nr>` | **[B]oard-[T]hread** `<nr>` aufklappen: Anfang + alle Antworten mit ihren Nummern, danach mit `R <nr>` lesen. `<nr>` darf der Thread-Anfang **oder** eine seiner Antworten sein |
| `NT <nr>` | `NACHRICHTENTHREAD <nr>` | **[N]achrichten[T]hread** `<nr>` aufklappen: eigener Verlauf mit einer Person, danach mit `R <nr>` lesen oder `RS<nr>\|Text` antworten. Markiert beim Öffnen alle Nachrichten im Thread als gelesen (wie eine Unterhaltung öffnen) — `R <nr>` einzeln bleibt zusätzlich möglich |
| `R <nr>` | `LESEN <nr>` | Nachricht/Board-Eintrag `<nr>` lesen |
| `S TO\|Betreff\|Text` | `SENDEN TO\|Betreff\|Text` | **[S]enden** – private Nachricht. Betreff darf kein `\|` enthalten (wird als Trennzeichen verwendet). Ist `TO` nicht registriert, warnt die Bestätigung explizit statt eine Zustellung vorzutäuschen |
| `RS<nr>\|Text` | `ANTWORT<nr>\|Text` | Antwort auf empfangene private Nachricht `<nr>` — Empfänger und Betreff (mit „Re: "-Präfix) werden automatisch aus der Original-Nachricht übernommen, nur für den tatsächlichen Empfänger nutzbar. Hängt am selben Thread wie die Originalnachricht (in `NL`/`NT` sichtbar) |
| `SB Thema\|Text` | `BULLETIN Thema\|Text` | **[S]enden [B]ulletin** (Board-Nachricht veröffentlichen). Thema darf kein `\|` enthalten |
| `SBR<nr>\|Text` | `BULLETINANTWORT<nr>\|Text` | Antwort auf ein Board-Bulletin `<nr>`. Die Antwort hängt am Thread und erscheint in `BL` als Zähler beim Thread-Anfang statt als eigene Zeile; das Thema (mit „Re: "-Präfix) kommt automatisch vom Thread-Anfang. Antwort auf eine Antwort landet im selben Thread — bewusst nur eine Ebene. **Alle bisherigen Teilnehmer** des Threads (nicht nur der Autor des Anfangs) bekommen die Antwort zusätzlich als Direktnachricht |
| `ND <nr>` / `K <nr>` | `LOESCHEN <nr>` | Nachricht `<nr>` löschen — bei privaten Nachrichten nur der Empfänger, bei Board-Bulletins nur der Autor, zusätzlich immer der SysOp und die konfigurierten Co-SysOps. **Erfordert Bestätigung:** derselbe Befehl muss innerhalb von 60 Sekunden erneut gesendet werden, sonst wird nur nachgefragt und nichts gelöscht |

Befehle mit Zahlenargument bei den Kürzeln (`R`, `NLO`, `BLO`, `ND`, `K`, `BL`, `NL`, `BT`, `NT`) akzeptieren die Nummer wahlweise mit Leerzeichen (`R 5`) oder direkt angehängt (`R5`). Die deutschen Langformen (`LESEN`, `LOESCHEN`, `BOARDLISTE`, `BOARDTHREAD`, `NACHRICHTENLISTE`, `NACHRICHTENTHREAD`) benötigen immer ein Leerzeichen (`LESEN 5`).

### Wetter (Home-Assistant-Integration)

| Befehl | Langform | Bedeutung |
|---|---|---|
| `WX` | `WETTER` | Aktuelles Wetter |
| `WX1` | `MORGEN` | Vorhersage morgen |
| `WX3` | `DREITAGE` | Vorhersage 3 Tage |

### Info & Sonstiges

| Befehl | Langform | Bedeutung |
|---|---|---|
| `SI` | `SYSINFO` | **[S]ys[I]nfo** (Nutzerzahl, Nachrichten, aktive Sessions) |
| `O` | `ONLINE` | **[O]nline** – wer ist gerade aktiv |
| `LU` | `USERLISTE` | **[L]iste** **[U]ser** – alle registrierten Nutzer |
| `PING` | – | Liste bekannter Repeater (max. 15 auf einmal, mit Hinweis auf `PING <Teilname>` bei mehr) |
| `PING <Name>` | – | Traceroute zu einem Node/Repeater — Pfad, Laufzeit, SNR je Hop |
| `PK` | `PUBKEY` | **[P]ub[K]ey** (eigener, 64 Hex) – zur Weitergabe an andere |
| `PK <Name>` | `PUBKEY <Name>` | **[P]ub[K]ey** eines Kontakts (64 Hex) — vor dem Senden abgleichen, da Namen fälschbar/duplizierbar sind |
| `MI` | `MEINEINFO` | **[M]eine [I]nfo** (Account-Status) |
| `MC <mail>` | `MAIL <mail>` | **[M]ail [C]ontact** hinterlegen, z. B. `MC name@example.com` |
| `REMOVE` | – | Eigene Registrierung löschen (nur per Direktnachricht). **Erfordert Bestätigung:** `REMOVE` muss innerhalb von 2 Minuten ein zweites Mal gesendet werden, sonst erfolgt nur ein Warnhinweis und nichts wird gelöscht |

### Pubkey-Sicherheitshinweis (einmalig pro User)

Vor dem ersten `S`/`SB` (bzw. `RS`/`SBR` und deren Langformen) muss jeder User (auch Bestandsuser) per Direktnachricht bestätigen, dass er verstanden hat: der angezeigte **Name** ist kein Identitätsnachweis — nur der **Pubkey** ist verlässlich. Ein Sendeversuch ohne Bestätigung liefert eine Fehlermeldung plus den Hinweistext mit einem 6-stelligen Code; Antwort per `OK <Code>` (15 Min. gültig) schaltet das Senden frei. Andere Befehle (`H`, `NL`, `R`, `WX`, ...) bleiben in der Zwischenzeit normal nutzbar. Der ursprünglich blockierte Sendeversuch wird nach erfolgreicher Bestätigung automatisch nachgeholt — er muss nicht erneut eingetippt werden.

### Self-Service-Registrierung (nur MeshCore-Kanal)

Neue Nutzer registrieren sich selbst über eine Nachricht im öffentlichen BBS-Kanal — kein Zutun des SysOp nötig.

**1. Registrierung beantragen** — im Kanal senden:

```
add BENUTZERNAME:PUBKEY
```

`BENUTZERNAME` ist frei wählbar (3–16 Zeichen, Buchstaben/Zahlen/`+-.!"§$%&/()=`). `PUBKEY` ist der eigene 64-stellige Hex-Pubkey des MeshCore-Node (in der MeshCore-App unter den eigenen Geräte-Details zu finden).

**2. Sofortige Antworten im Kanal** — die BBS bestätigt den Antrag, schickt eine `meshcore://contact/<pubkey>`-Einladung (die MeshCore-App bietet damit direkt einen "Kontakt hinzufügen"-Dialog an) als eigene Nachricht, und weist je nach Registrierungsmodus (vom SysOp gewählt, siehe [Für SysAdmins](#für-sysadmins-web-admin--features)) auf die nächsten Schritte hin (Bestätigungscode per DM, sofort aktiv, oder Warten auf SysOp-Freischaltung). **Wichtig:** in dieser Zeit den BBS-Kontakt über den Link anlegen, sonst kann die BBS später keine Direktnachricht zustellen.

**3. Abschluss je nach Modus:**
- **Pubkey-Bestätigung (Status quo):** rund 10 Minuten nach dem Antrag schickt die BBS eine Direktnachricht mit einem 6-stelligen Code, der **als Antwort per Direktnachricht** zurückgeschickt werden muss. Bei korrektem Code ist der Account sofort aktiv, die BBS bestätigt per DM und der SysOp erhält automatisch eine Benachrichtigung. Kommt innerhalb von 10 Minuten nach der Code-DM keine (korrekte) Antwort, verfällt der Antrag automatisch — der Benutzername wird wieder frei. Ohne Bestätigung landet **kein** Eintrag in der Nutzerdatenbank — der behauptete Pubkey allein reicht nicht aus.
- **Sofortige Freischaltung:** der Account ist sofort aktiv, die BBS schickt direkt eine Willkommens-DM, der SysOp wird informiert.
- **SysOp-Freischaltung:** keine automatische Aktion — der SysOp schaltet im Web-Admin frei oder lehnt ab, der Nutzer bekommt das per DM mitgeteilt.

Zum Schutz vor Missbrauch/Spam sind maximal 2 neue Registrierungsanträge pro Minute zulässig; weitere `add`-Versuche werden in dieser Zeit mit einer Fehlermeldung abgewiesen.

Danach: `REMOVE` als Direktnachricht löscht die eigene Registrierung jederzeit wieder — aus Sicherheitsgründen erst nach zweimaligem Senden innerhalb von 2 Minuten (siehe [Cheatsheet](#cheatsheet-kurzübersicht)).

**Hinweis:** Accounts ohne jede BBS-Aktivität werden nach einer gewissen Zeit automatisch entfernt (mit Vorwarnungen per DM vorher) — Fristen und Details sind vom SysOp konfigurierbar, siehe [Inaktivitäts-Bereinigung](#inaktivitäts-bereinigung) im nächsten Block.

## Für SysAdmins: Web-Admin & Features

### BBS-Kern-Features
- **Deutschsprachige Langform-Aliase** — jeder Kurzbefehl hat eine ausführlichere deutsche Alternative (z. B. `NACHRICHTEN` statt `N`, `SENDEN` statt `S`, `LOESCHEN` statt `K`/`ND`) für Nutzer, die sich mit den Kürzeln schwertun. Beide Formen funktionieren immer gleichwertig nebeneinander, nichts wurde ersetzt
- **Private Nachrichten** — Postfach je Rufzeichen, konfigurierbares Limit, AES-256-GCM-verschlüsselt at-rest (siehe [Sicherheit](#sicherheit)). Direktantwort per `RS<nr>|Text` ohne erneute Eingabe von Empfänger/Betreff. Beim Senden wird sofort geprüft, ob das Ziel-Rufzeichen überhaupt registriert ist
- **Proaktive Zustellung** — neue private Nachrichten werden dem Empfänger sofort per Direktnachricht mit vollem Inhalt zugestellt, plus eine einmalige Erinnerung 3 Tage vor Löschung einer ungelesenen Nachricht (Löschfrist konfigurierbar, Default 30 Tage)
- **Board/Bulletins** — öffentliche Nachrichten, sticky-Flag (nie automatisch gelöscht), automatische Aufräumung nach konfigurierbarer Frist
- **Bestätigung vor destruktiven Befehlen** — `REMOVE` und `K`/`ND`/`LOESCHEN` verlangen dieselbe Eingabe ein zweites Mal innerhalb eines kurzen Zeitfensters, schützt vor Tippfehlern und versehentlichem Absenden
- **Self-Service-Registrierung** — Nutzer registrieren sich per `add` direkt über den MeshCore-Kanal. Drei Modi wählbar (Web-Admin -> Einstellungen): Pubkey-Bestätigung per Direktnachricht-Challenge, sofortige Freischaltung ohne Prüfung, oder manuelle Freischaltung durch den SysOp im Web-Admin (siehe [Self-Service-Registrierung](#self-service-registrierung-nur-meshcore-kanal))
- **Kontakt-Einladung per QR/Link** — die BBS schickt eine `meshcore://contact/<pubkey>`-URI, die die MeshCore-App direkt als "Kontakt hinzufügen"-Dialog anbietet
- **Inaktivitäts-Bereinigung** — User ohne jede BBS-Aktivität werden nach konfigurierbarer Frist automatisch entfernt, mit bis zu 3 frei einstellbaren Erinnerungs-DMs vorher (Details siehe [unten](#inaktivitäts-bereinigung))
- **Pubkey-Sicherheitshinweis & Senderecht** — vor dem ersten Senden muss jeder User per Direktnachricht-Challenge bestätigen, dass der Pubkey (nicht der Name) die Identität beweist (Nutzerablauf siehe [Für User](#pubkey-sicherheitshinweis-einmalig-pro-user)). Der SysOp kann das Senderecht je User im Web-Admin dauerhaft sperren/entsperren
- **Wetter-Integration** — aktuelle Werte + 1-/3-Tage-Vorhersage über eine angebundene [Home Assistant](https://www.home-assistant.io/)-Instanz
- **PING/Traceroute** — Pfad- und Laufzeitmessung zu einzelnen Nodes/Repeatern im Mesh, mit automatischem Retry bei Paketverlust. Die Repeaterliste ist auf 15 Einträge gedeckelt
- **Feature-Flags** — jede Funktionsgruppe (Nachrichten, Board, Wetter, Sysinfo, Online-Liste, Userliste, PING, Account, Self-Service) einzeln im Web-Admin abschaltbar, wirkt sofort ohne Neustart

### Zugangsweg
- **MeshCore-Funknetz** — Kanal-Broadcasts (öffentlich) und Direktnachrichten (privat) über das serielle Companion-Protokoll eines angeschlossenen MeshCore-Node (z. B. Heltec WiFi LoRa 32)

### Web-Admin (HTTPS)
- Dashboard mit Node-/Serial-Status, Region-Scope-Bestätigung, Nachrichtenstatistik
- Nutzerverwaltung (registrieren, sperren, Mail-Kontakt setzen, Senderecht sperren/entsperren, ausstehende Freischaltungen genehmigen/ablehnen im `sysop_approval`-Modus)
- Nachrichtenverwaltung (Board: Volltext + Sticky-Toggle; Privat: nur Metadaten)
- Live-Einstellungen: TX-Power, Path-Hash-Mode, Region-Scope, Kanalname — wirken sofort am Node, kein Neustart nötig
- Registrierungs- und Inaktivitäts-Einstellungen: Registrierungsmodus, Inaktivitätsfrist, Warnschwellen, Nachrichten-Löschverhalten bei User-Entfernung
- Statistik-Dashboard: Nachrichtenaufkommen, Routing-Art (Flood / Direkt bestätigt / Multihop / Pfad unbekannt), SNR-Verlauf je Nutzer, Feature-Nutzung (Aufrufe je Befehl, heute + Zeitraum + Verlauf)
- Debug-Ansicht mit Live-Journal-Log (journalctl-Anbindung)
- Datenbank-Backup-Download (konsistenter SQLite-Snapshot)
- Eigenes self-signed HTTPS-Zertifikat (automatisch erzeugt) oder Import eines eigenen Zertifikats
- **Co-SysOps** — weitere Rufzeichen mit SysOp-Rechten im Mesh (z. B. Nachrichten löschen), unter *Einstellungen* pflegbar
- **Mehrere Admin-Konten** — zusätzlich zum Standardkonto `admin` beliebig viele weitere Web-Admin-Logins mit eigenem Benutzernamen/Passwort anlegbar (gleichberechtigt, keine Rollen), unter *Einstellungen* verwaltbar

### MeshCore-Protokolldetails
- **Region-Scoping** — alle Flood-Pakete (Kanal-Broadcasts, Flood-DMs, Adverts) werden mit einem Region-Code versehen, der auf Repeatern gefiltert werden kann (Firmware ≥ v1.15 für persistenten Default-Scope)
- **Best-Effort-Multihop-DMs** — Direktnachrichten über bekannte Pfade, automatischer Fallback auf Flood bei ausbleibendem ACK
- **V3-Protokoll** (3-Byte-Pfad-Header) mit automatischem Downgrade-Schutz bei Node-Neustart

### Inaktivitäts-Bereinigung

Nutzer ohne jede BBS-Aktivität (jede angenommene Direktnachricht zählt) werden nach `users.inactivity_days` (Default 60 Tage, im Web-Admin unter *Einstellungen* änderbar) automatisch entfernt. Vor der Entfernung verschickt die BBS Erinnerungs-DMs mit Hinweis auf die bevorstehende Löschung — wann, ist über `users.inactivity_warn_before_days` einstellbar (bis zu 3 Werte, Tage *vor* der Entfernung, Default `[10, 5, 1]`; weniger als 3 Werte = entsprechend weniger Warnungen, leer = keine Warnung).

Bei jeder Art der Entfernung (Inaktivität, Web-Admin *Entfernen*/*Sperren*, Self-Service `REMOVE`) werden empfangene private Nachrichten immer gelöscht. Ob zusätzlich auch vom entfernten User **gesendete** private Nachrichten bzw. eigene Board-Bulletins gelöscht werden, steuert getrennt `users.delete_sent_private_messages` und `users.delete_sent_board_messages` (beide Default an, im Web-Admin unter *Einstellungen -> Registrierung* einzeln umschaltbar).

### Konfiguration

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
| `board` | `retention_days` (Aufbewahrung; gerechnet ab der **letzten Aktivität im Thread**, Anfang und Antworten werden gemeinsam gelöscht — Sticky ausgenommen) |
| `messages` | `max_personal` (Postfach-Limit), `unread_retention_days` (Löschfrist ungelesener Nachrichten, Erinnerung 3 Tage vorher) |
| `registration` | `mode` (`challenge`/`open`/`sysop_approval`, siehe [Self-Service-Registrierung](#self-service-registrierung-nur-meshcore-kanal)) |
| `users` | `inactivity_days` (automatische Entfernung nach N Tagen Inaktivität), `inactivity_warn_before_days` (bis zu 3 Warn-DMs, Tage vor der Entfernung), `delete_sent_private_messages`/`delete_sent_board_messages` (gesendete Nachrichten bzw. Bulletins bei Entfernung getrennt mitlöschen) |
| `homeassistant` | `url`, `verify_ssl` (Token separat in `secrets.yaml`) |

Viele Optionen (TX-Power, Path-Hash-Mode, Region-Scope, Kanalname, Feature-Flags, Betreiberdaten) sind zusätzlich **live im Web-Admin unter *Einstellungen*** änderbar und wirken sofort ohne Neustart.

## Installation & Sicherheit

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
chmod 600 config/secrets.yaml config/config.local.yaml   # enthalten Geheimnisse/Betreiberdaten
# (main.py korrigiert falsche Rechte an diesen Dateien beim Start ohnehin automatisch)

# At-Rest-Schluessel erzeugen und eintragen (sonst unverschluesselte Nachrichten!)
python -c "from core.crypto import generate_key; print(generate_key())"
# -> Ausgabe als storage.messages_key in secrets.yaml eintragen

python main.py
```

### Update

```bash
sudo -u coreadmin git -C /home/coreadmin/nnp-bbs pull && sudo systemctl restart nnp-bbs
```

### Sicherheit

- **At-Rest-Verschlüsselung** — private Nachrichten (`msg_type='P'`) liegen AES-256-GCM-verschlüsselt in der SQLite-Datenbank. Der Schlüssel wird in dieser Reihenfolge gesucht: (1) systemd-Credential (`LoadCredentialEncrypted=`, wird beim Service-Start automatisch nach tmpfs entschlüsselt, Klartext-Key landet nie auf Platte — Standard bei `setup_pi.sh`), (2) eine root-only Key-Datei außerhalb von Repo/Backup (`storage.messages_key_file`), (3) inline Base64 in `secrets.yaml` (Legacy-Fallback, nur so sicher wie die Dateirechte). Ohne jeden dieser Wege werden private Nachrichten unverschlüsselt gespeichert — die BBS warnt beim Start explizit davor.
- **Web-Admin-Zugang** — Passwort als scrypt-Hash in `config/webconfig.yaml`, nie im Klartext gespeichert. Ohne gesetztes Passwort erzeugt der erste Start ein Zufallspasswort in `data/initial-web-password.txt` (0600); setzbar auch über `scripts/set_web_password.py`.
- **HTTPS** — der Web-Admin läuft ausschließlich über TLS (kein Klartext-HTTP), mit automatisch erzeugtem self-signed Zertifikat oder eigenem Import unter *Einstellungen*.
- **Datei-Rechte** — gitignored Config-Dateien mit Geheimnissen/Betreiberdaten (`secrets.yaml`, `config.local.yaml`, `webconfig.yaml`) werden beim Start automatisch auf `0600` korrigiert, falls sie z. B. durch ein einfaches `cp` mit offenerem Umask entstanden sind.
- **Pubkey statt Name als Identitätsnachweis** — Namen im MeshCore-Netz sind fälschbar/duplizierbar; Senderecht wird erst nach einer Direktnachricht-Challenge freigeschaltet, die bestätigt, dass der User den Unterschied verstanden hat (siehe [Pubkey-Sicherheitshinweis](#pubkey-sicherheitshinweis-einmalig-pro-user)).
- **`sudo`-Rechte für Deployment** — `setup_pi.sh` richtet gezielte NOPASSWD-Regeln nur für Service-Steuerung (`systemctl restart/start/stop/status nnp-bbs`) und `git pull` im Projektverzeichnis ein, keine pauschale Root-Freigabe.

### Architektur

```
main.py                    Einstiegspunkt, Config-Merge, Service-Start
core/
  bbs.py                   BBS-Logik: Befehle, Menues, Feature-Flags
  crypto.py                  At-Rest-Verschluesselung, Passwort-Hashing
  validation.py               Rufzeichen/Namen-Validierung
  sanitize.py                  Log-Ausgabe-Bereinigung
  timeutil.py                   Zeitstempel-Hilfsfunktion (Python-3.12-sicher)
  weather.py                     Home-Assistant-Wetter-Client
  webtls.py                       Self-signed-Zertifikat-Erzeugung
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

### Lizenz

MIT — siehe [LICENSE](LICENSE).
