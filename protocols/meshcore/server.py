import asyncio
import glob
import logging
import os
import random
import re
import secrets
import struct
import time
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

import aioserial

from core.bbs import BBSCore
from protocols.base import BaseProtocol
from protocols.meshcore.packet import (
    ADV_TYPE_REPEATER,
    Contact,
    IncomingMessage,
    PUSH_ADVERT,
    PUSH_MSG_WAITING,
    PUSH_CODE_TRACE_DATA,
    PUSH_PATH_UPDATED,
    PUSH_SEND_CONFIRMED,
    RESP_CONTACT,
    RESP_CONTACT_MSG,
    RESP_CONTACT_MSG_V3,
    RESP_CHANNEL_MSG,
    RESP_CHANNEL_MSG_V3,
    RESP_CONTACTS_END,
    RESP_DEFAULT_FLOOD_SCOPE,
    RESP_DEVICE_INFO,
    RESP_ERR,
    RESP_NO_MORE_MSGS,
    RESP_OK,
    RESP_SELF_INFO,
    RESP_SENT,
    build_add_contact,
    build_app_start,
    build_device_query,
    build_get_contacts,
    build_get_default_flood_scope,
    build_trace_path,
    build_reset_path,
    build_send_channel_msg,
    build_send_self_advert,
    build_send_txt,
    build_set_default_flood_scope,
    build_set_flood_scope_key,
    build_set_path_hash_mode,
    build_set_time,
    build_set_tx_power,
    build_sync_next_message,
    contact_add_uri,
    parse_channel_msg,
    parse_contact,
    parse_contact_msg,
    parse_default_flood_scope,
    parse_frames,
    parse_trace_data,
    region_scope_key,
)
from core.validation import USERNAME_RE
from core import sanitize
from storage.database import Database

logger = logging.getLogger(__name__)

NODE_TIMEOUT    = 600   # Sekunden bis Node als "offline" gilt
# Befehle mit Zahlenargument, die zusaetzlich zur "X 5"-Form auch attached "X5"
# akzeptieren (siehe Normalisierung in _dispatch_bbs) -- WX1/WX3 duerfen hier NICHT
# rein, sonst wuerde "WX1" faelschlich zu WX + Argument 1 zerlegt. BL/NL nehmen
# seit der Zusammenlegung mit BLO/NLO ebenfalls ein optionales Zahlenargument
# entgegen (BLO/NLO bleiben als funktionierende Alt-Formen erhalten). Die
# deutschsprachigen Langform-Aliase (LESEN, LOESCHEN, BOARDLISTE,
# NACHRICHTENLISTE, ...) bekommen bewusst KEINE Attached-Digit-Bequemlichkeit --
# "LESEN5" waere unhandlich, wer die Langform braucht tippt ohnehin mit Leerzeichen.
_NUMERIC_ARG_CMDS = {"R", "K", "ND", "BLO", "NLO", "BL", "NL"}
_USER_RE = USERNAME_RE  # siehe core/validation.py
MAX_MSG_LEN     = 150   # Firmware-Limit: max 150 Zeichen pro Paket
CONFIRM_TIMEOUT = 30.0  # Fallback-Sekunden, falls Node kein est_timeout liefert
CONFIRM_FLOOR   = 15.0  # Mindest-Wartezeit auf ACK
CONFIRM_CAP     = 90.0  # Max-Wartezeit auf ACK
MAX_RETRIES     = 2     # danach aufgeben

# Self-Service-Registrierung ('add' via Kanal): Pubkey-Bestaetigung statt
# blindem Vertrauen auf den im Klartext mitgeschickten Pubkey.
REGISTRATION_CHALLENGE_DELAY = 600   # Sekunden bis zur Bestaetigungs-DM (Zeit
                                      # fuer den User, das BBS als Kontakt anzulegen)
REGISTRATION_CHALLENGE_TIMEOUT = 600  # Sekunden, die der Code nach Versand gueltig ist
REGISTRATION_RATE_LIMIT  = 2    # max. akzeptierte 'add'-Anfragen ...
REGISTRATION_RATE_WINDOW = 60.0  # ... pro Sekunden (Anti-Spam/Squatting-Flood)
REGISTRATION_MAX_ATTEMPTS = 5   # falsche Bestaetigungscode-Versuche, danach Registrierung
                                 # sofort verworfen (Defense-in-Depth zusaetzlich zur langsamen
                                 # LoRa-Bandbreite als natuerlichem Rate-Limit)
SENT_WAIT       = 5.0   # Sekunden auf RESP_CODE_SENT nach einem DM-Send

# Pubkey-Sicherheitshinweis vor dem ersten S/SB: Namen sind faelschbar/duplizierbar,
# nur der Pubkey beweist Identitaet. Muss per "OK <Code>"-DM bestaetigt werden,
# bevor S/SB freigeschaltet wird (siehe _pubkey_ack_gate / _confirm_pubkey_ack).
PUBKEY_ACK_TIMEOUT = 900   # Sekunden, die ein Bestaetigungscode gueltig ist (15 Min)
_PUBKEY_ACK_RE = re.compile(r'^OK\s*(\d{6})$', re.IGNORECASE)

# Destruktive Befehle (REMOVE, K/ND) erfordern eine Bestaetigung durch erneutes
# Senden desselben Befehls innerhalb des jeweiligen Zeitfensters -- verhindert
# versehentliches, unwiderrufliches Loeschen durch Tippfehler/verfruehtes Absenden.
REMOVE_CONFIRM_WINDOW = 120.0   # Sekunden, in denen ein erneutes REMOVE bestaetigt
KILL_CONFIRM_WINDOW   = 60.0    # Sekunden, in denen ein erneutes K<n>/ND<n> bestaetigt

REPEATER_LIST_LIMIT = 15   # PING ohne Argument: max. angezeigte Repeater auf einmal
                           # (verhindert einen unbegrenzten Chunk-Schwall bei vielen
                           # bekannten Repeatern)


@dataclass
class _Pending:
    """Offene DM mit ACK-Tracking nach Companion-Protokoll v1.16.0."""
    chunks: list           # alle gesendeten Chunks (Fallback fuer Resend)
    sent_at: float
    retries: int
    acks: dict             # expected_ack (4B) -> chunk-Text, noch unbestaetigt
    timeout: float         # Sekunden bis Retry (aus est_timeout abgeleitet)
    is_flood: bool = False # True wenn via Flood gesendet – kein Retry, nur best-effort


def _utf8_len(text: str) -> int:
    """Byte-Laenge in UTF-8. Das MeshCore-Paketlimit zaehlt Bytes, nicht Zeichen –
    ein 150-*Zeichen*-Chunk mit Umlauten/Emojis kann deutlich ueber 150 Byte liegen."""
    return len(text.encode("utf-8"))


def _split_to_bytes(text: str, max_bytes: int) -> list[str]:
    """Zerlegt einen zu langen String in Stuecke von hoechstens max_bytes UTF-8-Bytes,
    ohne je eine Mehrbyte-Sequenz (Umlaut/Emoji) zu zerschneiden – getrennt wird nur
    an Zeichengrenzen. Ein einzelnes Zeichen, das allein groesser als max_bytes ist,
    bleibt als eigenes (leicht ueberzaehliges) Stueck erhalten statt zerschnitten zu werden."""
    parts: list[str] = []
    current = ""
    cur_bytes = 0
    for ch in text:
        cb = len(ch.encode("utf-8"))
        if current and cur_bytes + cb > max_bytes:
            parts.append(current)
            current, cur_bytes = "", 0
        current += ch
        cur_bytes += cb
    if current:
        parts.append(current)
    return parts


def _truncate_to_bytes(text: str, max_bytes: int) -> str:
    """Kuerzt text auf hoechstens max_bytes UTF-8-Bytes, ohne eine Mehrbyte-Sequenz
    zu zerschneiden (schneidet an der letzten passenden Zeichengrenze)."""
    if _utf8_len(text) <= max_bytes:
        return text
    out = ""
    used = 0
    for ch in text:
        cb = len(ch.encode("utf-8"))
        if used + cb > max_bytes:
            break
        out += ch
        used += cb
    return out


def _chunk(lines: list[str], max_bytes: int) -> list[str]:
    """Packt Zeilen in Chunks von hoechstens max_bytes UTF-8-Bytes. Das Limit ist
    bewusst byte- und nicht zeichenbasiert: build_send_txt/build_send_channel_msg
    kappen den Text byte-genau (Firmware-Limit), sodass eine zeichenbasierte Grenze
    mehrsprachigen Text mit Umlauten/Emojis still abgeschnitten haette. Zu lange
    Einzelzeilen werden an Zeichengrenzen aufgeteilt."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        sep = "\n" if current else ""
        if _utf8_len(current) + _utf8_len(sep) + _utf8_len(line) <= max_bytes:
            current += sep + line
        else:
            if current:
                chunks.append(current)
                current = ""
            pieces = _split_to_bytes(line, max_bytes)
            chunks.extend(pieces[:-1])
            current = pieces[-1] if pieces else ""
    if current:
        chunks.append(current)
    return chunks


class MeshCoreServer(BaseProtocol):
    def __init__(self, db: Database, config: dict):
        self.db      = db
        self.config  = config
        # self.notify_dm als eigene Methode: BBSCore ruft sie fuer proaktive
        # DM-Benachrichtigungen auf (neue Nachricht / Loesch-Erinnerung).
        self.bbs     = BBSCore(db, config, notify_dm=self.notify_dm)

        # pubkey_prefix (6B hex) -> Contact
        self._contacts: dict[str, Contact] = {}
        # pubkey_prefix (6B hex) -> registrierter DB-Name (stabile Identitaet fuer
        # Statistik-Events; der Node kann seinen Kontaktnamen jederzeit aendern,
        # z.B. Gross-/Kleinschreibung oder ein "/P"-Suffix, ohne dass sich der
        # Pubkey aendert -- siehe _canonical_name())
        self._registered_names: dict[str, str] = {}
        # pubkey_prefix (6B hex) -> last_seen timestamp
        self._nodes: dict[str, float] = {}
        # pubkey_prefix (6B hex) -> current menu state
        self._menu_states: dict[str, str] = {}
        # pubkey_prefix (6B hex) -> _Pending (ACK-Tracking)
        self._pending: dict[str, _Pending] = {}
        # trace-tag (int32) -> (Future, sent_at, name) fuer alle laufenden PING/Traces
        # (MeshCore-Menue PING <name> und Web-Admin Ping-Button)
        self._pending_traces: dict[int, tuple[asyncio.Future, float, str]] = {}
        # Sperrliste (Cache): 12-Hex-Prefixe und volle Pubkeys
        self._blocked_prefixes: set[str] = set()
        self._blocked_pubkeys: set[str] = set()

        # pubkey_prefix (6B hex) -> {"username", "pubkey_hex", "code"} fuer
        # Self-Service-Registrierungen, die auf die Pubkey-Bestaetigung warten
        # (siehe _channel_add / _send_registration_challenge)
        self._pending_registrations: dict[str, dict] = {}
        # Timestamps akzeptierter 'add'-Anfragen, fuer das Rate-Limit
        # (max. REGISTRATION_RATE_LIMIT pro REGISTRATION_RATE_WINDOW Sekunden)
        self._registration_request_times: list[float] = []

        # pubkey_prefix (6B hex) -> {"code", "sent_at"} fuer ausstehende Pubkey-
        # Sicherheitshinweis-Bestaetigungen (siehe _pubkey_ack_gate / _confirm_pubkey_ack)
        self._pending_pubkey_ack: dict[str, dict] = {}
        # pubkey_prefix (6B hex) -> urspruenglicher S/SB/RS/SBR-Text, der durch den
        # Pubkey-Hinweis blockiert wurde -- wird nach erfolgreicher OK-Bestaetigung
        # automatisch nachgeholt (siehe _confirm_pubkey_ack), damit der User seine
        # Nachricht nicht erneut eintippen muss.
        self._pending_send_replay: dict[str, str] = {}

        # pubkey_prefix (6B hex) -> Zeitpunkt der ersten REMOVE-Anfrage, wartet auf
        # Bestaetigung durch ein zweites REMOVE innerhalb REMOVE_CONFIRM_WINDOW
        self._pending_remove_confirm: dict[str, float] = {}
        # pubkey_prefix (6B hex) -> (msg_id, Zeitpunkt) der ersten K/ND-Anfrage,
        # wartet auf Bestaetigung durch denselben Befehl innerhalb KILL_CONFIRM_WINDOW
        self._pending_kill_confirm: dict[str, tuple[int, float]] = {}

        self._self_pubkey: Optional[bytes] = None
        self._serial: Optional[aioserial.AioSerial] = None
        self._running = False
        self._buf = bytearray()
        self._send_lock = asyncio.Lock()
        self._txn_lock = asyncio.Lock()          # serialisiert DM-Send + RESP_SENT-Korrelation
        self._sent_waiter: Optional[asyncio.Future] = None

        self._last_rx_ts: float = 0.0
        self._self_info_expected_until: float = 0.0  # Gnadenfrist fuer Keepalive-Antworten
        self._initialized: bool = False
        self._node_default_scope: Optional[str] = None  # vom Node bestaetigter Default-Scope
        self._reinit_in_progress: bool = False
        self._reconnecting: bool = False
        self._device_info_seen: bool = False
        # True, sobald der Node RESP_NO_MORE_MSGS meldet -- laesst _fetch_messages den
        # Drain-Loop sofort beenden statt stur alle 20 SYNCs abzusenden.
        self._queue_drained: bool = False
        self._tasks: set[asyncio.Task] = set()

        mc = self.config.get("meshcore", {})
        self.port           = mc.get("serial_port",        "/dev/ttyACM0")
        self.baud           = mc.get("baud_rate",           115200)
        self.channel        = mc.get("channel",             0)
        self.channel_name   = mc.get("channel_name",        f"CH{mc.get('channel', 0)}")
        self.channel_region = mc.get("channel_region",      "")
        # Byte-Budget je DM (nicht Zeichen!). max(1, ...) verhindert eine Entartung
        # in _chunk, falls max_message_length versehentlich auf <=0 gesetzt wird
        # (die Web-UI erzwingt ohnehin 50..150).
        self.max_len        = max(1, min(mc.get("max_message_length", MAX_MSG_LEN), 150))
        self.chunk_delay    = mc.get("chunk_delay",         2.0)
        self.max_chunks     = mc.get("max_chunks",          5)
        self.tx_power       = mc.get("tx_power",            None)   # dBm; None = Node-Wert beibehalten
        self.path_hash_mode = mc.get("path_hash_mode",      None)   # 0=1B,1=2B,2=3B OTA-Sende-Header; None=unveraendert

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _create_tracked_task(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _find_port(self) -> str:
        """Gibt den konfigurierten Port zurück, oder sucht ttyACM*/ttyUSB* als Fallback."""
        if os.path.exists(self.port):
            return self.port
        candidates = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
        if candidates:
            logger.warning("Port %s nicht gefunden – verwende %s", self.port, candidates[0])
            return candidates[0]
        return self.port

    def _open_serial(self) -> aioserial.AioSerial:
        port = self._find_port()
        return aioserial.AioSerial(port=port, baudrate=self.baud, timeout=0.1)

    async def start(self):
        """Startet den Server. Ist der Companion beim ersten Versuch nicht erreichbar
        (z.B. Dev/QA-Instanz ohne angeschlossene Hardware), blockiert das NICHT den
        restlichen BBS-Start (Web-Admin) – der Reconnect laeuft im Hintergrund
        und die Runtime-Loops starten automatisch, sobald der Companion verfuegbar ist."""
        self._running = True
        try:
            self._serial = self._open_serial()
        except Exception as exc:
            logger.warning("MeshCore-Companion nicht verfuegbar (%s) – BBS startet trotzdem, "
                            "Reconnect laeuft im Hintergrund.", exc)
            self._reconnecting = True
            self._create_tracked_task(self._connect_serial_loop())
            return
        await self._finish_startup()

    async def _connect_serial_loop(self):
        """Versucht im Hintergrund die serielle Verbindung herzustellen, ohne den
        BBS-Start zu blockieren."""
        backoff = 5
        while self._running:
            try:
                self._serial = self._open_serial()
                break
            except Exception as exc:
                logger.warning("Serial-Port nicht verfuegbar, warte %ds: %s", backoff, exc)
                await asyncio.sleep(backoff)
                backoff = min(int(backoff * 1.5), 30)
        if not self._running:
            return
        self._reconnecting = False
        await self._finish_startup()
        logger.info("MeshCore-Companion nachtraeglich verbunden – Server jetzt aktiv.")

    async def _finish_startup(self):
        """Laedt Kontakte/Sperrliste, initialisiert die Session und startet alle
        Runtime-Loops. Wird sowohl bei sofortigem als auch bei verzoegertem
        Verbindungsaufbau aufgerufen."""
        self._load_known_contacts()
        await self._load_db_contacts()
        await self.refresh_blocklist()
        await self._init_session()
        self._last_rx_ts = time.time()
        self._create_tracked_task(self._read_loop())
        self._create_tracked_task(self._poll_loop())
        self._create_tracked_task(self._watchdog_loop())
        self._create_tracked_task(self._keepalive_loop())
        self._create_tracked_task(self._daily_advert_loop())
        self._create_tracked_task(self._confirm_watchdog_loop())
        self._create_tracked_task(self._register_contacts_with_node())
        region = f" [{self.channel_region}]" if self.channel_region else ""
        logger.info("MeshCore-Server gestartet auf %s (%d baud) | Kanal %s%s",
                    self.port, self.baud, self.channel_name, region)

    def _load_known_contacts(self):
        """Laedt vorkonfigurierte Kontakte aus der Config."""
        for entry in self.config.get("meshcore", {}).get("contacts", []):
            try:
                pubkey = bytes.fromhex(entry["pubkey"])
                name   = entry["name"].upper()
                c      = Contact(pubkey=pubkey, name=name)
                self._contacts[c.pubkey_prefix.hex()] = c
                self._registered_names[c.pubkey_prefix.hex()] = name
                logger.info("Bekannter Kontakt geladen: %s (%s)", name, c.pubkey_prefix.hex())
            except Exception as exc:
                logger.warning("Kontakt-Konfigfehler: %s", exc)

    async def _load_db_contacts(self):
        """Laedt per Self-Service registrierte Kontakte aus der DB in den Speicher."""
        entries = await self.db.load_mc_contacts()
        for pubkey_hex, name in entries:
            pubkey = bytes.fromhex(pubkey_hex)
            c = Contact(pubkey=pubkey, name=name)
            self._contacts[c.pubkey_prefix.hex()] = c
            self._registered_names[c.pubkey_prefix.hex()] = name
            logger.info("DB-Kontakt geladen: %s (%s)", name, c.pubkey_prefix.hex())

    @staticmethod
    def _hops_for_path(path: bytes) -> int:
        """Gesamt-Hopzahl eines bekannten Routing-Pfads: contact.path speichert
        1 Byte je Repeater (Zwischenstation), die Gesamt-Hops sind Repeater
        plus der letzte Sprung zum Empfaenger."""
        return len(path) + 1

    @staticmethod
    def _hop_bucket(n_hops: int) -> str:
        """Ordnet eine Hop-Zahl einer Statistik-Kategorie zu (fuer events.route):
        1 Hop / 2-5 Hops / >5 Hops. Nur fuer Direktpfad-Zustellungen (nicht Flood) --
        siehe hop_info-Ermittlung in _handle_message() und den ACK/noack-Handlern."""
        if n_hops <= 1:
            return "hop_1"
        if n_hops <= 5:
            return "hop_2_5"
        return "hop_gt5"

    @classmethod
    def _route_for_direct_contact(cls, contact: Optional[Contact]) -> str:
        """events.route fuer eine Direktpfad-Zustellung (is_direct=True bzw. ACK/
        Retry-Handling eines nicht-geflooteten Sends). Contact.path ist nur
        aussagekraeftig, wenn contact.path_known True ist -- sonst ist der Pfad dem
        Node schlicht unbekannt (haeufig bei diesem Firmware-Verhalten!) und darf
        NICHT stillschweigend als '1 Hop' angenommen werden (fruehere Annahme,
        siehe Bugreport: Statistik zeigte dadurch nie echte Multihop-Direktzustellungen)."""
        if not contact or not contact.path_known:
            return "unbekannt"
        return cls._hop_bucket(cls._hops_for_path(contact.path))

    def _canonical_name(self, prefix_hex: str, fallback: str) -> str:
        """Stabiler Name fuer Statistik-Events (events.callsign): bevorzugt den
        registrierten DB-Namen. Der vom Node gemeldete Kontaktname (self._contacts)
        kann sich jederzeit aendern (Gross-/Kleinschreibung, '/P'-Suffix, Emoji...),
        ohne dass eine Neu-Registrierung stattfindet -- sonst zerfaellt derselbe
        User in der Statistik in mehrere Zeilen."""
        return self._registered_names.get(prefix_hex, fallback)

    async def _register_contacts_with_node(self):
        """Registriert alle DB-Kontakte beim Node NACH dem APP_START.
        Companion-registrierte Kontakte gehen bei Node-Neustart verloren."""
        await asyncio.sleep(2.0)   # APP_START Sequenz abwarten
        entries = await self.db.load_mc_contacts()
        for pubkey_hex, name in entries:
            await self._send(build_add_contact(pubkey_hex, name))
            await asyncio.sleep(0.3)
            logger.info("Kontakt beim Node re-registriert: %s (%s...)", name, pubkey_hex[:12])
        if entries:
            await asyncio.sleep(0.5)
            await self._send(build_get_contacts())  # Pfade nach Re-Registrierung verifizieren

    async def stop(self):
        self._running = False
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._serial and self._serial.is_open:
            self._serial.close()
        logger.info("MeshCore-Server gestoppt.")

    # ------------------------------------------------------------------
    # Oeffentliche API fuer die Web-Admin-Oberflaeche
    # ------------------------------------------------------------------

    def status_snapshot(self) -> dict:
        """Momentaufnahme des Node-/Serial-Zustands fuer das Dashboard."""
        return {
            "running": self._running,
            "initialized": self._initialized,
            "reconnecting": self._reconnecting,
            "serial_open": bool(self._serial and self._serial.is_open),
            "port": self.port,
            "last_rx_age": (time.time() - self._last_rx_ts) if self._last_rx_ts else None,
            "self_pubkey": self._self_pubkey.hex() if self._self_pubkey else None,
            "tx_power": self.tx_power,
            "path_hash_mode": self.path_hash_mode,
            "channel_name": self.channel_name,
            "channel_region": self.channel_region,
            "node_default_scope": self._node_default_scope,
            "node_contacts": len(self._contacts),
            "pending_dms": len(self._pending),
        }

    def node_contact_list(self) -> list[dict]:
        """Alle Kontakte aus der Node-Kontaktliste inkl. Pfad und zuletzt gesehen."""
        now = time.time()
        entries = []
        for prefix_hex, contact in self._contacts.items():
            last = self._nodes.get(prefix_hex)
            entries.append({
                "prefix": prefix_hex,
                "name": contact.name,
                "type": contact.type,
                "path": contact.path.hex() if contact.path else "",
                "path_known": contact.path_known,
                "last_seen_age": (now - last) if last else None,
            })
        return sorted(entries, key=lambda e: e["name"].lower())

    async def apply_tx_power(self, dbm: int):
        """Setzt die TX-Power sofort am Node (persistiert wird via webconfig)."""
        self.tx_power = dbm
        await self._send(build_set_tx_power(dbm))
        logger.info("TX-Power via Web-Admin gesetzt: %s dBm", dbm)

    async def apply_path_hash_mode(self, mode: int):
        """Setzt die Path-Hash-Size sofort am Node (0=1B, 1=2B, 2=3B)."""
        self.path_hash_mode = mode
        await self._send(build_set_path_hash_mode(mode))
        logger.info("Path-Hash-Size via Web-Admin gesetzt: mode=%s", mode)

    async def apply_channel_region(self, region: str):
        """Setzt den Region-Scope sofort am Node (persistiert wird via webconfig).
        Leerer String → unscoped senden."""
        self.channel_region = region
        self._node_default_scope = None
        await self._apply_region_scope()
        logger.info("Region-Scope via Web-Admin gesetzt: '%s'", region or "(kein Scope)")

    async def send_advert(self):
        await self._send(build_send_self_advert())

    async def reload_node_contacts(self):
        await self._send(build_get_contacts())

    async def register_user(self, pubkey_hex: str, name: str):
        """Registriert einen User via Web-Admin: DB + Kontakt am Node anlegen."""
        await self.db.save_mc_contact(pubkey_hex, name)
        await self._send(build_add_contact(pubkey_hex, name.upper()))
        await asyncio.sleep(0.3)
        await self._send(build_get_contacts())
        logger.info("Web-Admin: %s registriert (pubkey %s...)", name.upper(), pubkey_hex[:12])

    async def approve_registration(self, prefix_hex: str) -> bool:
        """Schaltet eine im Modus 'sysop_approval' wartende Registrierung frei
        (Web-Admin -> Benutzer -> Ausstehende Freischaltungen)."""
        pending = await self.db.pop_pending_registration(prefix_hex)
        if not pending:
            return False
        await self.register_user(pending["pubkey"], pending["name"])
        bbs_call = self.config.get("callsign", "SysOp")
        await self._reply(bytes.fromhex(prefix_hex),
                           f"Freigeschaltet! Hallo {pending['name']}, dein Account ist jetzt "
                           f"aktiv. H = Hilfe. Hinweis: vor dem ersten S/SB ist einmalig "
                           f"eine kurze Pubkey-Bestaetigung per Code noetig. 73 de {bbs_call}")
        logger.info("Web-Admin: Registrierung von %s freigeschaltet (pubkey %s...)",
                    pending["name"], pending["pubkey"][:12])
        return True

    async def reject_registration(self, prefix_hex: str) -> bool:
        """Lehnt eine im Modus 'sysop_approval' wartende Registrierung ab."""
        pending = await self.db.pop_pending_registration(prefix_hex)
        if not pending:
            return False
        self._contacts.pop(prefix_hex, None)
        await self._reply(bytes.fromhex(prefix_hex),
                           f"Registrierung von '{pending['name']}' wurde vom SysOp abgelehnt.")
        logger.info("Web-Admin: Registrierung von %s abgelehnt (pubkey %s...)",
                    pending["name"], pending["pubkey"][:12])
        return True

    async def remove_user(self, name: str) -> bool:
        """Entfernt einen registrierten User aus der DB (Node-Kontakt bleibt bis Reboot)."""
        found = await self.db.find_mc_contact_by_name(name)
        if not found:
            return False
        pubkey_hex, uname = found
        await self.db.delete_mc_contact(pubkey_hex)
        await self.db.purge_user_messages(uname, *self._delete_sent_flags())
        self.evict_contact_cache(pubkey_hex)
        logger.info("Web-Admin: %s entfernt", name.upper())
        return True

    def evict_contact_cache(self, pubkey_hex: str):
        """Entfernt einen Pubkey aus den In-Memory-Caches (_contacts/_registered_names),
        z.B. nach einer Entfernung aus der DB (Web-Admin, Self-Service REMOVE,
        Inaktivitaets-Bereinigung in main.py). Der Node-Kontakt selbst bleibt bis
        zum naechsten Reboot bestehen."""
        prefix_hex = pubkey_hex[:12]
        self._contacts.pop(prefix_hex, None)
        self._registered_names.pop(prefix_hex, None)

    def _delete_sent_flags(self) -> tuple[bool, bool]:
        """Ob bei einer User-Entfernung auch von ihm gesendete private Nachrichten
        bzw. eigene Board-Bulletins geloescht werden (empfangene private
        Nachrichten werden immer geloescht) -- getrennte Web-Admin-Einstellungen
        users.delete_sent_private_messages / delete_sent_board_messages,
        Default jeweils True. Rueckgabe: (delete_sent_private, delete_sent_board)."""
        users_cfg = self.config.get("users", {})
        return (bool(users_cfg.get("delete_sent_private_messages", True)),
                bool(users_cfg.get("delete_sent_board_messages", True)))

    async def _dm_to_registered_user(self, name: str, text: str) -> bool:
        """Sendet eine DM an einen registrierten User (per Name/Rufzeichen), falls
        bekannt. Gemeinsame Grundlage fuer SysOp-DMs und automatische Benachrichtigungen
        (neue Nachricht / Loesch-Erinnerung). Gibt False zurueck, wenn der Name nicht
        als MeshCore-Kontakt registriert ist (z.B. Tippfehler oder unregistrierter Name)."""
        found = await self.db.find_mc_contact_by_name(name)
        if not found:
            return False
        prefix = bytes.fromhex(found[0])[:6]
        await self._send_dm_chunks(prefix, self._finalize_chunks(text.splitlines() or [text]))
        return True

    async def sysop_dm(self, name: str, text: str) -> bool:
        """Sendet eine DM vom SysOp an einen registrierten User (Web-Admin)."""
        sent = await self._dm_to_registered_user(name, text)
        if sent:
            logger.info("Web-Admin: SysOp-DM an %s", name.upper())
        return sent

    async def notify_dm(self, name: str, text: str) -> bool:
        """Automatische Benachrichtigung (neue Nachricht / Loesch-Erinnerung) an
        einen registrierten User. Wird der BBSCore als notify_dm-Callback uebergeben,
        siehe core/bbs.py."""
        sent = await self._dm_to_registered_user(name, text)
        if sent:
            logger.info("Benachrichtigung an %s gesendet", name.upper())
        return sent

    async def send_channel_broadcast(self, text: str):
        """Sendet eine Nachricht in den BBS-Kanal (Web-Admin)."""
        await self._reply_channel(text)
        logger.info("Web-Admin: Kanal-Broadcast [CH%d]: %s", self.channel, text[:80])

    async def refresh_blocklist(self):
        """Laedt die Sperrliste aus der DB in den Cache."""
        entries = await self.db.get_blocked()
        self._blocked_pubkeys = {e["pubkey"] for e in entries}
        self._blocked_prefixes = {e["pubkey"][:12] for e in entries}
        if entries:
            logger.info("Sperrliste geladen: %d Eintraege", len(entries))

    async def block_user(self, name: str, reason: str = "") -> bool:
        """Sperrt einen registrierten User: Blocklist + Registrierung entfernen."""
        found = await self.db.find_mc_contact_by_name(name)
        if not found:
            return False
        pubkey_hex, uname = found
        await self.db.add_blocked(pubkey_hex, uname, reason)
        await self.db.delete_mc_contact(pubkey_hex)
        await self.db.purge_user_messages(uname, *self._delete_sent_flags())
        self.evict_contact_cache(pubkey_hex)
        await self.refresh_blocklist()
        logger.warning("Web-Admin: %s gesperrt (%s)", uname, reason or "kein Grund angegeben")
        return True

    async def block_pubkey(self, pubkey_hex: str, name: str = "", reason: str = ""):
        """Sperrt einen beliebigen Pubkey (auch nicht registrierte Nodes)."""
        await self.db.add_blocked(pubkey_hex, name, reason)
        await self.refresh_blocklist()
        logger.warning("Web-Admin: Pubkey %s... gesperrt", pubkey_hex[:12])

    async def unblock_pubkey(self, pubkey_hex: str):
        await self.db.remove_blocked(pubkey_hex)
        await self.refresh_blocklist()
        logger.info("Web-Admin: Pubkey %s... entsperrt", pubkey_hex[:12])

    async def web_ping(self, prefix_hex: str) -> str:
        """Ping/Trace aus der Web-UI: bis zu 3 Versuche à 10s (siehe _trace_with_retries)."""
        contact = self._contacts.get(prefix_hex)
        if not contact:
            return f"Kontakt {prefix_hex} nicht in Node-Liste"
        return await self._trace_with_retries(contact)

    # ------------------------------------------------------------------
    # Initialisierung
    # ------------------------------------------------------------------

    async def _send(self, frame: bytes) -> bool:
        """Schreibt einen Frame. Faengt Schreibfehler ab (z.B. waehrend USB-Trennung)
        und stoesst den Reconnect an, statt die aufrufende Task abstuerzen zu lassen.
        Gibt True bei Erfolg zurueck."""
        try:
            async with self._send_lock:
                await self._serial.write_async(frame)
            return True
        except Exception as exc:
            if not self._reconnecting:
                logger.error("Serial-Schreibfehler: %s – starte Reconnect", exc)
                self._reconnecting = True
                self._create_tracked_task(self._reconnect_serial())
            return False

    async def _init_session(self):
        """Initialisierungssequenz: DEVICE_QUERY → APP_START → SET_TIME → GET_CONTACTS."""
        await self._send(build_device_query())
        await asyncio.sleep(0.5)

        await self._send(build_app_start())
        await asyncio.sleep(0.5)

        await self._send(build_set_time())
        await asyncio.sleep(0.3)

        if self.tx_power is not None:
            await self._send(build_set_tx_power(self.tx_power))
            await asyncio.sleep(0.3)
            logger.info("TX-Power gesetzt: %s dBm", self.tx_power)

        if self.path_hash_mode is not None:
            await self._send(build_set_path_hash_mode(self.path_hash_mode))
            await asyncio.sleep(0.3)
            logger.info("Path-Hash-Size gesetzt: mode=%s (%s-Byte-Header)",
                        self.path_hash_mode, self.path_hash_mode + 1)

        await self._apply_region_scope()

        await self._send(build_get_contacts())
        logger.info("MeshCore Initialisierung gesendet, lade Kontakte...")

    async def _apply_region_scope(self):
        """Setzt den Region-Scope am Node: Session-Scope (0x36, RAM – muss nach jedem
        Node-Reboot erneut gesetzt werden) UND persistenten Default-Scope (0x3F,
        FW v1.15+, deckt zusaetzlich Self-Adverts ab). Damit werden alle Flood-Pakete
        (Channel-Broadcasts, Flood-DMs, ACKs) mit dem Region-Code gestempelt.
        Direkt geroutete DMs sind kein Flood – dort greift Scoping nicht (by design).
        Ohne konfigurierte Region werden beide Scopes geloescht (unscoped senden)."""
        if self.channel_region:
            key = region_scope_key(self.channel_region)
            await self._send(build_set_flood_scope_key(key))
            await asyncio.sleep(0.3)
            await self._send(build_set_default_flood_scope(self.channel_region))
            logger.info("Region-Scope gesetzt: '%s' (key %s...)",
                        self.channel_region, key[:4].hex())
        else:
            await self._send(build_set_flood_scope_key(None))
            await asyncio.sleep(0.3)
            await self._send(build_set_default_flood_scope(""))
            logger.info("Region-Scope geloescht – Floods gehen unscoped")
        await asyncio.sleep(0.3)
        # Default-Scope zur Bestaetigung abfragen (Antwort: RESP_DEFAULT_FLOOD_SCOPE).
        # Aeltere Firmware (< v1.15) lehnt 0x3F/0x40 mit ERR ab – der Session-Scope
        # (0x36) funktioniert dort trotzdem ab Protokoll v8.
        await self._send(build_get_default_flood_scope())
        await asyncio.sleep(0.3)

    # ------------------------------------------------------------------
    # Empfangs-Loop
    # ------------------------------------------------------------------

    async def _read_loop(self):
        while self._running:
            try:
                raw = await asyncio.wait_for(self._serial.read_async(256), timeout=5.0)
                if raw:
                    self._last_rx_ts = time.time()
                    self._reconnecting = False
                    # Kein Hex-Dump: koennte Klartext von Direktnachrichten enthalten
                    logger.debug("RAW [%dB] empfangen", len(raw))
                    self._buf.extend(raw)
                    frames, self._buf = parse_frames(self._buf)
                    for frame in frames:
                        self._create_tracked_task(self._dispatch_frame(frame))
            except asyncio.TimeoutError:
                # 5s ohne Byte – Serial/USB hängt, Reconnect einleiten
                if not self._reconnecting:
                    logger.error("Serial-Read Timeout (USB-Hang?) – starte Reconnect")
                    self._reconnecting = True
                    self._create_tracked_task(self._reconnect_serial())
                await asyncio.sleep(1)
            except Exception as exc:
                if not self._reconnecting:
                    logger.error("Serial-Lesefehler: %s – starte Reconnect", exc)
                    self._reconnecting = True
                    self._create_tracked_task(self._reconnect_serial())
                await asyncio.sleep(1)

    async def _dispatch_frame(self, payload: bytes):
        if not payload:
            return
        ptype = payload[0]
        data  = payload[1:]

        if ptype in (RESP_CONTACT_MSG, RESP_CONTACT_MSG_V3):
            # Direktnachrichten sind privat - kein Payload-Hex im Log
            logger.debug("Frame: typ=0x%02X len=%d (Direktnachricht, Inhalt nicht geloggt)",
                         ptype, len(payload))
        else:
            logger.debug("Frame: typ=0x%02X len=%d payload=%s", ptype, len(payload), payload[:12].hex())

        if ptype == RESP_OK:
            logger.info("Node: OK (0x00)")

        elif ptype == RESP_ERR:
            logger.warning("Node: ERR (0x01) – letzter Befehl abgelehnt: %s", data.hex())

        elif ptype == RESP_SENT:
            # v1.16.0: [route_flag(1)][expected_ack(4)][est_timeout(4, ms)]
            route_flag   = data[0] if len(data) >= 1 else 0
            expected_ack = bytes(data[1:5]) if len(data) >= 5 else b''
            est_timeout  = struct.unpack_from('<I', data, 5)[0] if len(data) >= 9 else 0
            logger.info("Node: SENT (%s, ack=%s, t=%dms)",
                        "Flood" if route_flag == 1 else "Direkt",
                        expected_ack.hex() or "-", est_timeout)
            if self._sent_waiter is not None and not self._sent_waiter.done():
                self._sent_waiter.set_result((route_flag, expected_ack, est_timeout))

        elif ptype == PUSH_SEND_CONFIRMED:
            # v1.16.0: [ack_code(4)][round_trip(4, ms)]
            ack_code = bytes(data[:4])
            rtt = struct.unpack_from('<I', data, 4)[0] if len(data) >= 8 else 0
            matched = False
            for prefix_hex, pend in self._pending.items():
                if ack_code in pend.acks:
                    del pend.acks[ack_code]
                    contact = self._contacts.get(prefix_hex)
                    name = contact.name if contact else prefix_hex
                    if pend.acks:
                        logger.info("Teil-ACK von %s (%d Chunk(s) offen, RTT %dms)",
                                    name, len(pend.acks), rtt)
                    else:
                        del self._pending[prefix_hex]
                        logger.info("ACK von %s ✓ (RTT %dms)", name, rtt)
                        route = "flood" if pend.is_flood else self._route_for_direct_contact(contact)
                        self._create_tracked_task(self.db.log_event(
                            "ack", self._canonical_name(prefix_hex, name), str(rtt), route=route))
                    matched = True
                    break
            if not matched:
                logger.debug("ACK 0x%s ohne passenden Pending-Eintrag (RTT %dms)",
                             ack_code.hex(), rtt)

        elif ptype == PUSH_PATH_UPDATED:
            logger.info("Node: Routing-Pfad aktualisiert – lade Kontakte neu")
            await self._send(build_get_contacts())

        elif ptype == RESP_SELF_INFO:
            # SELF_INFO: adv_type(1) + tx_power(1, signed dBm) + max_tx_power(1) + public_key(32) + ...
            # Der Pubkey beginnt erst bei Offset 3, NICHT bei 0!
            tx_power = struct.unpack_from('b', data, 1)[0] if len(data) >= 2 else None
            max_tx   = struct.unpack_from('b', data, 2)[0] if len(data) >= 3 else None
            self._self_pubkey = data[3:35]
            logger.info("Eigene PubKey: %s | TX-Power: %s dBm (max %s dBm)",
                        self._self_pubkey.hex(), tx_power, max_tx)
            # APP_START/DEVICE_QUERY (Keepalive) werden vom Node regulaer mit SELF_INFO
            # beantwortet - das ist KEIN Reboot-Indiz. Nur ein SELF_INFO, das wir nicht
            # gerade selbst angefordert haben, bedeutet einen echten (unaufgeforderten)
            # Node-Neustart.
            if (self._initialized and not self._reinit_in_progress
                    and time.time() > self._self_info_expected_until):
                logger.warning("RESP_SELF_INFO unaufgefordert – Node-Neustart erkannt")
                self._create_tracked_task(self._reinit_after_reboot())
            self._initialized = True

        elif ptype == RESP_CONTACT:
            contact = parse_contact(payload)
            if contact and contact.name:
                key = contact.pubkey_prefix.hex()
                self._contacts[key] = contact
                if not contact.path_known:
                    path_info = "unbekannt"
                elif contact.path:
                    path_info = contact.path.hex()
                else:
                    path_info = "direkt (bestaetigt)"
                logger.info("Kontakt geladen: %s (%s) path=%s", contact.name, key, path_info)

        elif ptype == RESP_NO_MORE_MSGS:
            self._queue_drained = True   # Queue leer – _fetch_messages bricht den Drain-Loop ab

        elif ptype == RESP_DEFAULT_FLOOD_SCOPE:
            scope = parse_default_flood_scope(data)
            self._node_default_scope = scope
            if scope == (self.channel_region or ""):
                logger.info("Node bestaetigt Default-Scope: '%s'", scope or "(kein Scope)")
            else:
                logger.warning("Default-Scope am Node ('%s') weicht von Config ('%s') ab",
                               scope, self.channel_region)

        elif ptype == RESP_CONTACTS_END:
            logger.info("Kontaktliste geladen: %d Kontakte", len(self._contacts))

        elif ptype == PUSH_MSG_WAITING:
            self._create_tracked_task(self._fetch_messages())

        elif ptype in (RESP_CONTACT_MSG, RESP_CONTACT_MSG_V3):
            logger.debug("DIRECT-MSG Frame [%dB] (Inhalt nicht geloggt)", len(data))
            msg = parse_contact_msg(data, v3=(ptype == RESP_CONTACT_MSG_V3))
            if msg:
                self._create_tracked_task(self._handle_message(msg))

        elif ptype in (RESP_CHANNEL_MSG, RESP_CHANNEL_MSG_V3):
            msg = parse_channel_msg(data, v3=(ptype == RESP_CHANNEL_MSG_V3))
            if msg:
                self._create_tracked_task(self._handle_channel_cmd(msg))

        elif ptype == PUSH_ADVERT:
            # Neue Node hat sich angekuendigt – Kontaktliste neu laden
            await self._send(build_get_contacts())

        elif ptype == RESP_DEVICE_INFO:
            # Antwort auf DEVICE_QUERY – bestaetigt app_target_ver=3 (V3/3-Byte-Header).
            # Layout: ver_code(1) + max_contacts/2(1) + max_channels(1) + ble_pin(4)
            #         + build_date(12) + manufacturer(40) + firmware_version(20)
            ver_code = data[0] if len(data) >= 1 else 0
            fw_ver = (data[59:79].split(b'\x00')[0].decode('ascii', 'replace')
                      if len(data) >= 60 else "?")
            if not self._device_info_seen:
                logger.info("Node DEVICE_INFO: FW %s (ver_code=%d) – V3/3-Byte-Header ausgehandelt",
                            fw_ver, ver_code)
                self._device_info_seen = True
            else:
                logger.debug("DEVICE_INFO: FW %s ver_code=%d", fw_ver, ver_code)

        elif ptype == PUSH_CODE_TRACE_DATA:
            logger.debug("PUSH_CODE_TRACE_DATA [%dB]: %s", len(data), data.hex())
            trace = parse_trace_data(data)
            if not trace:
                logger.warning("Trace-Antwort: Parse fehlgeschlagen [%dB]: %s",
                               len(data), data.hex())
            else:
                pending = self._pending_traces.pop(trace.tag, None)
                if pending:
                    fut, sent_at, name = pending
                    rtt = int((time.time() - sent_at) * 1000)
                    n_hops = trace.hop_count
                    path_hex = trace.path_hashes.hex() or "-"
                    snrs = ",".join(f"{s:.1f}" for s in trace.path_snrs) or "-"
                    result = f"Pong {name}: {rtt}ms via {n_hops} Hop(s) [{path_hex}] SNR {snrs}dB"
                    logger.info(result)
                    if not fut.done():
                        fut.set_result(result)
                else:
                    logger.info("Trace-Antwort tag=%d ohne Pending-Eintrag (Timeout/Retry abgelaufen?)",
                                trace.tag)

        else:
            # 0x88 = LOG_DATA (RF-Mithoerlogs); unbekannte Push-Typen (>=0x80) sichtbar loggen
            if ptype >= 0x80:
                logger.info("Unbekannter Push-Frame 0x%02X [%dB]: %s",
                            ptype, len(payload), payload[:32].hex())
            else:
                logger.debug("Unbehandelter Frame 0x%02X [%dB]: %s",
                             ptype, len(payload), payload.hex())

    # ------------------------------------------------------------------
    # Nachrichten abholen
    # ------------------------------------------------------------------

    async def _fetch_messages(self):
        """Ruft Nachrichten ab bis keine mehr vorhanden (max. 20 am Stueck). Bricht
        ab, sobald der Node RESP_NO_MORE_MSGS gemeldet hat (self._queue_drained),
        statt stur 20 SYNCs zu senden."""
        self._queue_drained = False
        for _ in range(20):
            await self._send(build_sync_next_message())
            await asyncio.sleep(0.3)
            if self._queue_drained:
                break

    async def _poll_loop(self):
        """Polling-Fallback: fragt alle 5s nach neuen Nachrichten,
        falls PUSH_MSG_WAITING (0x83) nicht geliefert wird."""
        await asyncio.sleep(5)   # Initialisierung abwarten
        while self._running:
            logger.debug("Poll: sende CMD_SYNC_NEXT_MESSAGE")
            await self._send(build_sync_next_message())
            await asyncio.sleep(5)

    async def _keepalive_loop(self):
        """Sendet alle 20s APP_START + DEVICE_QUERY, damit der Node nicht in den
        Idle-Modus faellt und aufhoert, auf Serial-Kommandos zu antworten. APP_START
        ist noetig, um den Node aus dem Idle zu wecken - DEVICE_QUERY allein genuegt
        dafuer nicht (Watchdog feuerte sonst alle ~2min dauerhaft, da der Node
        zwischen den Keepalives durchgehend eingeschlafen ist). SEND_SELF_ADVERT
        NICHT hier - das flutet das Mesh unnoetig, siehe _daily_advert_loop."""
        await asyncio.sleep(20)
        while self._running:
            if not self._reinit_in_progress and not self._reconnecting:
                logger.debug("Keepalive: APP_START + DEVICE_QUERY")
                # Beide Kommandos beantwortet der Node regulaer mit RESP_SELF_INFO -
                # Gnadenfrist setzen, damit das nicht als spontaner Reboot gewertet wird
                self._self_info_expected_until = time.time() + 5.0
                await self._send(build_app_start())
                await asyncio.sleep(0.3)
                # DEVICE_QUERY re-asserten: haelt app_target_ver=3 (V3/3-Byte-Header)
                # gepinnt, falls ein anderer Client (z.B. BLE-App) den Node auf V1 wirft
                await self._send(build_device_query())
            await asyncio.sleep(20)

    async def _daily_advert_loop(self):
        """Sendet SEND_SELF_ADVERT genau einmal taeglich um 03:40 Uhr (Serverzeit) -
        haeufigere Adverts fluten unnoetig das gesamte Mesh mit Selbstankuendigungen."""
        while self._running:
            now = datetime.now()
            target = now.replace(hour=3, minute=40, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            if not self._running:
                return
            await self._send(build_send_self_advert())
            logger.info("Taeglicher SELF_ADVERT gesendet (03:40)")

    # ------------------------------------------------------------------
    # Watchdog & Reconnect
    # ------------------------------------------------------------------

    async def _watchdog_loop(self):
        """Überwacht die Node-Verbindung. Kein Frame für >90s → vollständige Re-Initialisierung."""
        await asyncio.sleep(30)
        while self._running:
            await asyncio.sleep(30)
            age = time.time() - self._last_rx_ts
            if age > 90 and not self._reinit_in_progress:
                logger.warning("Watchdog: Kein Frame seit %.0fs – sende Initialisierungssequenz", age)
                self._create_tracked_task(self._full_reinit())

    async def _full_reinit(self):
        """Vollständige Neuinitialisierung inkl. APP_START (z.B. nach Watchdog-Timeout)."""
        if self._reinit_in_progress:
            return
        self._reinit_in_progress = True
        try:
            await self._init_session()          # DEVICE_QUERY + APP_START + SET_TIME + GET_CONTACTS
            await asyncio.sleep(2.0)
            entries = await self.db.load_mc_contacts()
            for pubkey_hex, name in entries:
                await self._send(build_add_contact(pubkey_hex, name))
                await asyncio.sleep(0.3)
            self._last_rx_ts = time.time()
            logger.info("Watchdog: Re-Initialisierung abgeschlossen, %d Kontakte re-registriert",
                        len(entries))
        except Exception as exc:
            logger.error("Watchdog: Re-Initialisierung fehlgeschlagen: %s", exc)
        finally:
            self._reinit_in_progress = False

    async def _reinit_after_reboot(self):
        """Schnelle Re-Initialisierung nach spontanem Node-Neustart (APP_START bereits erfolgt)."""
        if self._reinit_in_progress:
            return
        self._reinit_in_progress = True
        try:
            logger.info("Re-Initialisierung nach Node-Neustart...")
            await asyncio.sleep(1.0)
            await self._send(build_device_query())   # app_target_ver=3 → V3/3-Byte-Header wiederherstellen
            await asyncio.sleep(0.3)
            await self._send(build_set_time())
            await asyncio.sleep(0.3)
            # Session-Scope (0x36) liegt nur im RAM des Node – nach Reboot neu setzen
            await self._apply_region_scope()
            await self._send(build_get_contacts())
            await asyncio.sleep(0.5)
            entries = await self.db.load_mc_contacts()
            for pubkey_hex, name in entries:
                await self._send(build_add_contact(pubkey_hex, name))
                await asyncio.sleep(0.3)
            logger.info("Re-Initialisierung abgeschlossen, %d Kontakte neu registriert", len(entries))
        except Exception as exc:
            logger.error("Re-Initialisierung fehlgeschlagen: %s", exc)
        finally:
            self._reinit_in_progress = False

    async def _reconnect_serial(self):
        """Stellt die serielle Verbindung nach einem Hard-Reboot oder USB-Trennung wieder her."""
        logger.warning("Serial: Verbindung verloren – versuche Reconnect...")
        for attempt in range(1, 31):
            await asyncio.sleep(min(5 * attempt, 30))
            if not self._running:
                return
            try:
                if self._serial:
                    try:
                        self._serial.close()
                    except Exception:
                        pass
                self._serial = self._open_serial()
                self._buf.clear()
                logger.info("Serial wiederhergestellt (Versuch %d) – sende Initialisierung", attempt)
                self._initialized = False
                await self._init_session()
                await asyncio.sleep(2.0)
                await self._register_contacts_with_node()
                self._last_rx_ts = time.time()
                self._reconnecting = False
                return
            except Exception as exc:
                logger.warning("Reconnect-Versuch %d fehlgeschlagen: %s", attempt, exc)
        logger.error("Serial-Reconnect nach 30 Versuchen aufgegeben – BBS braucht Neustart")

    # ------------------------------------------------------------------
    # Nachricht verarbeiten
    # ------------------------------------------------------------------

    async def _handle_message(self, msg: IncomingMessage):
        prefix_hex = msg.pubkey_prefix.hex()
        self._nodes[prefix_hex] = time.time()

        if prefix_hex in self._blocked_prefixes:
            logger.info("Msg von gesperrtem Sender %s – ignoriert", prefix_hex)
            return

        contact = self._contacts.get(prefix_hex)
        if not contact:
            known = list(self._contacts.keys())
            logger.info("Direktnachricht von unbekanntem Sender %s – ignoriert (bekannte Prefixe: %s)",
                        prefix_hex, known)
            return
        # Ausstehende Self-Service-Registrierung: solange ein Bestaetigungscode fuer
        # diesen Pubkey-Prefix aussteht, wird JEDE DM als Code-Antwort behandelt und
        # NICHT als BBS-Befehl weiterverarbeitet (siehe _confirm_registration).
        if prefix_hex in self._pending_registrations and \
                await self._confirm_registration(prefix_hex, msg.text):
            return
        # Pubkey-Hinweis-Bestaetigung: nur "OK <Code>"-Antworten werden abgefangen,
        # alle anderen Befehle bleiben waehrend der Wartezeit normal nutzbar (nur
        # S/SB sind bis zur Bestaetigung gesperrt, siehe _pubkey_ack_gate).
        if prefix_hex in self._pending_pubkey_ack and \
                await self._confirm_pubkey_ack(prefix_hex, msg.text):
            return

        # Identitaet IMMER ueber den Pubkey-Prefix aufloesen, nie ueber den vom Node
        # gemeldeten Kontaktnamen: der kann sich jederzeit aendern (Gross-/Kleinschreibung,
        # "/P"-Suffix, Emoji...) ohne Neu-Registrierung, sonst landen NL/R/S & Co. unter
        # einer anderen Identitaet als der, unter der Nachrichten gespeichert wurden.
        callsign = self._canonical_name(prefix_hex, contact.name.upper())
        self._create_tracked_task(self.db.touch_last_active(callsign))

        snr_info = f" SNR:{msg.snr:.1f}dB" if msg.snr is not None else ""
        if msg.is_direct:
            route = self._route_for_direct_contact(contact)
            if contact.path_known:
                n_hops = self._hops_for_path(contact.path)
                hop_info = f" direkt/{n_hops}Hop"
            else:
                hop_info = " direkt/Pfad unbekannt"
        elif msg.hop_count == 0:
            route, hop_info = "flood", " flood"
        else:
            route, hop_info = "flood", f" flood/{msg.hop_count}Hop"
        # Kein Klartext im Log: Direktnachrichten sind privat (koennen Nachrichteninhalte
        # enthalten, z.B. beim S-Befehl), im Gegensatz zu Channel-Broadcasts (oeffentlich).
        logger.info("Msg von %s%s%s: ###private message###", callsign, hop_info, snr_info)
        self._create_tracked_task(
            self.db.log_event("rx", callsign, hop_info.strip(), snr=msg.snr, route=route))

        # Wartungsmodus: alles mit Hinweis beantworten, nichts ausfuehren
        maint = self.config.get("maintenance", {})
        if maint.get("enabled"):
            text = maint.get("text") or "BBS im Wartungsmodus, bitte spaeter erneut versuchen."
            await self._reply(msg.pubkey_prefix, f"\U0001f6e0 {text}")
            return

        # remove nur per Direct Message erlaubt (Pubkey-Prefix Verifikation)
        cmd0 = msg.text.strip().split(None, 1)[0].upper() if msg.text.strip() else ""
        if cmd0 == "REMOVE":
            await self._handle_remove_request(msg.pubkey_prefix, prefix_hex, callsign)
            return

        response_lines = await self._dispatch_bbs(callsign, msg.text.strip(), prefix_hex)
        if not response_lines:
            return

        await self._send_dm_chunks(msg.pubkey_prefix, self._finalize_chunks(response_lines))

    # ------------------------------------------------------------------
    # BBS-Dispatcher
    # ------------------------------------------------------------------

    async def _dispatch_bbs(self, callsign: str, text: str, prefix_hex: str = "") -> list[str]:
        """
        Flacher Dispatcher – jeder Shortcut eindeutig, von überall nutzbar. Jeder
        Kurzbefehl hat zusaetzlich eine deutschsprachige Langform als Alias (fuer
        User, die sich mit den Kuerzeln schwertun) -- beide Formen funktionieren
        immer gleichwertig nebeneinander, siehe Tabelle unten.

        Navigation (zeigt Submenu):
          H/?/HELP  BBS-Main   N/NACHRICHTEN  Nachrichten   B/BOARD  Board
          W  Wetter (WETTER liefert die Daten direkt, ohne Menue)
          I/INFO  Info          A/ACCOUNT  Account

        Befehle:
          WX/WETTER  Wetter      WX1/MORGEN  morgen      WX3/DREITAGE  3 Tage
          SI/SYSINFO  Sysinfo    O/ONLINE  Online        LU/USERLISTE  Userliste
          PK/PUBKEY  eigener Pubkey (voll)   PK/PUBKEY <Name>  Pubkey eines Kontakts
          BL/BLO/BOARDLISTE [<n>]  Board-Liste (mit optionalem Offset fuer Folgeseiten)
          NL/NLO/NACHRICHTENLISTE [<n>]  Nachrichten-Liste (dito)
          R<n>/LESEN <n>  Lesen
          S/SP/SENDEN CALL|Betr|Text     SB/BULLETIN Thema|Text
          RS<n>|Text / ANTWORT<n>|Text  Antwort auf empfangene private Nachricht <n>
                     (Empfaenger/Betreff automatisch)
          SBR<n>|Text / BULLETINANTWORT<n>|Text  Antwort auf Board-Bulletin <n>
                     (als neues Bulletin)
          K<n>/ND<n>/LOESCHEN <n>  Loeschen (nur eigene: erhaltene Nachrichten bzw.
                     eigene Bulletins, erfordert Bestaetigung durch erneutes Senden)
          MI/MEINEINFO  Meine Info   MC/MAIL mail   REMOVE Abmelden
        """
        # RS<n>|Text / ANTWORT<n>|Text: Nummer und Pipe-Text haengen direkt
        # aneinander (wie K<n>/ND<n>), der restliche Text nach dem Pipe darf aber
        # Leerzeichen enthalten -- der generische Leerzeichen-Split unten wuerde
        # das falsch zerlegen, daher hier per eigenem Regex VOR dem generischen
        # Parsing behandelt. ANTWORT ist die deutschsprachige Langform von RS.
        m_reply = re.match(r'^(?:RS|ANTWORT)(\d+)\|(.*)$', text.strip(), re.IGNORECASE | re.DOTALL)
        if m_reply:
            if not self.bbs.feature_enabled("messages"):
                return [f"Unbekannt: RS  H=Hilfe"]
            gate = await self._pubkey_ack_gate(prefix_hex, callsign)
            if gate:
                self._pending_send_replay[prefix_hex] = text
                return gate
            body = m_reply.group(2).strip()
            if not body:
                return ["Format: ANTWORT<Nummer>|Text (oder RS<Nummer>|Text)"]
            return await self.bbs.cmd_reply(callsign, int(m_reply.group(1)), body)

        # SBR<n>|Text / BULLETINANTWORT<n>|Text: Antwort auf ein Board-Bulletin als
        # neues Bulletin (Thema mit "Re: "-Praefix) -- Pendant zu RS/ANTWORT fuer
        # Board-Nachrichten. BULLETINANTWORT ist die deutschsprachige Langform.
        m_bulletin_reply = re.match(r'^(?:SBR|BULLETINANTWORT)(\d+)\|(.*)$',
                                    text.strip(), re.IGNORECASE | re.DOTALL)
        if m_bulletin_reply:
            if not self.bbs.feature_enabled("board"):
                return [f"Unbekannt: SBR  H=Hilfe"]
            gate = await self._pubkey_ack_gate(prefix_hex, callsign)
            if gate:
                self._pending_send_replay[prefix_hex] = text
                return gate
            body = m_bulletin_reply.group(2).strip()
            if not body:
                return ["Format: BULLETINANTWORT<Nummer>|Text (oder SBR<Nummer>|Text)"]
            return await self.bbs.cmd_bulletin_reply(callsign, int(m_bulletin_reply.group(1)), body)

        parts = text.strip().split(None, 1)
        if not parts:
            return []
        cmd = parts[0].upper()
        arg = parts[1] if len(parts) > 1 else ""
        # Hilfetexte zeigen Befehle wie "K<n>"/"ND<n>" ohne Leerzeichen, der Parser
        # spaltet aber nur an Leerzeichen -- ohne diese Normalisierung wuerde "K5"
        # als unbekannter Befehl "K5" durchfallen statt als K mit Argument 5 erkannt
        # zu werden. Nur fuer Befehle mit Zahlenargument, sonst wuerde z.B. "WX1"
        # faelschlich zu WX + Argument 1 zerlegt.
        if not arg:
            m = re.match(r'^([A-Z]+)(\d+)$', cmd)
            if m and m.group(1) in _NUMERIC_ARG_CMDS:
                cmd, arg = m.group(1), m.group(2)

        active = [c.name for c in self._contacts.values()
                  if self._nodes.get(c.pubkey_prefix.hex(), 0) > time.time() - NODE_TIMEOUT]

        def set_state(s: str):
            self._menu_states[prefix_hex] = s

        feat = self.bbs.feature_enabled
        # L/R/K gehoeren zu Nachrichten UND Board – aktiv solange eins davon an ist
        msgs_or_board = feat("messages") or feat("board")
        unknown = [f"Unbekannt: {cmd}  H=Hilfe"]

        # Navigation – deaktivierte Funktionen verhalten sich wie unbekannte Befehle
        if cmd in ("H", "?", "HELP"):
            set_state("main")
            return await self.bbs.menu_main(callsign)
        if cmd in ("N", "NACHRICHTEN"):
            if not feat("messages"):
                return unknown
            set_state("msg")
            return await self.bbs.menu_messages(callsign)
        if cmd in ("B", "BOARD"):
            if not feat("board"):
                return unknown
            set_state("board")
            return await self.bbs.menu_board()
        if cmd == "W":
            if not feat("weather"):
                return unknown
            set_state("wx")
            return await self.bbs.menu_weather()
        if cmd in ("I", "INFO"):
            set_state("info")
            return await self.bbs.menu_info()
        if cmd in ("A", "ACCOUNT"):
            if not feat("account"):
                return unknown
            set_state("account")
            return await self.bbs.menu_account()

        # Befehle
        if cmd in ("WX", "WETTER"):
            return await self.bbs.cmd_weather() if feat("weather") else unknown
        if cmd in ("WX1", "MORGEN"):
            return await self.bbs.cmd_forecast_1day() if feat("weather") else unknown
        if cmd in ("WX3", "DREITAGE"):
            return await self.bbs.cmd_forecast_3days() if feat("weather") else unknown
        if cmd in ("SI", "SYSINFO"):
            return await self.bbs.cmd_info(len(active)) if feat("sysinfo") else unknown
        if cmd in ("O", "ONLINE"):
            return await self.bbs.cmd_who(active) if feat("online") else unknown
        if cmd in ("LU", "USERLISTE"):
            return await self.bbs.cmd_list_users() if feat("userlist") else unknown
        if cmd == "PING":
            return await self._cmd_ping(arg, prefix_hex) if feat("ping") else unknown
        if cmd in ("PK", "PUBKEY"):
            return await self._cmd_pubkey(arg, prefix_hex)
        if cmd in ("MI", "MEINEINFO"):
            return await self.bbs.cmd_my_info(callsign) if feat("account") else unknown
        if cmd in ("MC", "MAIL"):
            if not feat("account"):
                return unknown
            if not arg:
                return ["Format: MAIL deine@mail.de (oder MC deine@mail.de)"]
            return await self.bbs.cmd_set_mail(callsign, arg)
        if cmd == "L":
            return await self.bbs.cmd_list() if msgs_or_board else unknown
        if cmd in ("BL", "BLO", "BOARDLISTE"):
            # BL/BLO/BOARDLISTE sind zusammengefuehrt: ohne Argument erste Seite,
            # mit Zahlenargument die entsprechende Folgeseite -- BLO existierte
            # frueher als eigenes Wort nur fuer den Offset-Fall, ist jetzt nur noch
            # eine funktionierende Alt-Form desselben Befehls.
            if not feat("board"):
                return unknown
            if not arg:
                return await self.bbs.cmd_list_board()
            if not arg.isdigit():
                return ["Format: BOARDLISTE <Zahl> (oder BL <Zahl> / BLO <Zahl>)"]
            return await self.bbs.cmd_list_board(int(arg))
        if cmd in ("NL", "NLO", "NACHRICHTENLISTE"):
            if not feat("messages"):
                return unknown
            if not arg:
                return await self.bbs.cmd_list_personal(callsign)
            if not arg.isdigit():
                return ["Format: NACHRICHTENLISTE <Zahl> (oder NL <Zahl> / NLO <Zahl>)"]
            return await self.bbs.cmd_list_personal(callsign, int(arg))
        if cmd in ("R", "LESEN"):
            if not msgs_or_board:
                return unknown
            if not arg.isdigit():
                return ["Format: LESEN <Nummer> (oder R <Nummer>)"]
            return await self.bbs.cmd_read(callsign, int(arg))
        if cmd in ("S", "SP", "SENDEN"):
            if not feat("messages"):
                return unknown
            gate = await self._pubkey_ack_gate(prefix_hex, callsign)
            if gate:
                self._pending_send_replay[prefix_hex] = text
                return gate
            p = arg.split("|", 2)
            if len(p) < 3:
                return ["Format: SENDEN CALL|Betr|Text (oder S CALL|Betr|Text; "
                        "Betreff darf kein '|' enthalten)"]
            return await self.bbs.cmd_send(callsign, p[0].strip(), p[1].strip(), p[2].strip())
        if cmd in ("SB", "BULLETIN"):
            if not feat("board"):
                return unknown
            gate = await self._pubkey_ack_gate(prefix_hex, callsign)
            if gate:
                self._pending_send_replay[prefix_hex] = text
                return gate
            p = arg.split("|", 1)
            if len(p) < 2:
                return ["Format: BULLETIN Thema|Text (oder SB Thema|Text; "
                        "Thema darf kein '|' enthalten)"]
            return await self.bbs.cmd_bulletin(callsign, p[0].strip(), p[1].strip())
        if cmd in ("K", "ND", "LOESCHEN"):
            if not msgs_or_board:
                return unknown
            if not arg.isdigit():
                return ["Format: LOESCHEN <Nummer> (oder K/ND <Nummer>) - nur eigene "
                        "Nachrichten: als Empfaenger erhaltene Nachrichten bzw. eigene Bulletins"]
            return await self._handle_kill_request(prefix_hex, callsign, int(arg), cmd)

        return unknown

    # ------------------------------------------------------------------
    # PING / Path Discovery
    # ------------------------------------------------------------------

    async def _cmd_ping(self, arg: str, requester_prefix_hex: str) -> list[str]:
        """PING <Name>: Path Discovery (Traceroute) zu einem Node/Repeater aus der
        Kontaktliste. Das Ergebnis (Pfad + Laufzeit) kommt asynchron per DM."""
        arg = arg.strip()
        if not arg or arg == "?":
            await self._list_repeaters(requester_prefix_hex)
            return []
        q = arg.lower()
        matches = [c for c in self._contacts.values() if q in c.name.lower()]
        if not matches:
            return [f"Kein Kontakt fuer '{arg}' gefunden"]
        if len(matches) > 1:
            names = ", ".join(c.name for c in matches[:5])
            extra = " ..." if len(matches) > 5 else ""
            return [f"Mehrdeutig ({len(matches)}): {names}{extra}"]
        target = matches[0]
        # Trace NACH dem Reply-DM senden – verhindert RESP_SENT-Race-Condition
        self._create_tracked_task(self._deferred_trace(target, requester_prefix_hex))
        return [f"Ping an {target.name} gesendet, warte auf Antwort..."]

    async def _trace_once(self, target: Contact, timeout: float) -> Optional[str]:
        """Sendet einen einzelnen CMD_SEND_TRACE_PATH innerhalb _txn_lock mit eigenem
        _sent_waiter (ein evtl. RESP_SENT wird so absorbiert und kapert keinen DM-Waiter)
        und wartet bis timeout auf die Pong-Antwort (PUSH_CODE_TRACE_DATA).
        path = bekannter Hop-Pfad des Ziels, sonst dessen eigener Hash (fuer direkt
        benachbarte Nodes/Repeater). Gibt den Ergebnistext zurueck oder None bei Timeout."""
        tag = random.getrandbits(31) or 1
        # Trace-Hash-Groesse: path_sz = flags & 0x03 = log2(Bytes) → 0=1B, 1=2B, 2=4B.
        # 2 Byte (flags=1) ist bei vielen Kontakten eindeutig genug und wird vom Node
        # akzeptiert (flags=2/4B wird abgelehnt). Unabhaengig vom OTA-Path-Hash-Modus.
        flags = 1
        hash_size = 1 << flags   # 2 Byte
        path = target.path if target.path else target.pubkey[:hash_size]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        async with self._txn_lock:
            self._sent_waiter = loop.create_future()
            try:
                await self._send(build_trace_path(tag, path, flags=flags))
                await asyncio.wait_for(self._sent_waiter, timeout=SENT_WAIT)
            except asyncio.TimeoutError:
                pass
            finally:
                self._sent_waiter = None
        self._pending_traces[tag] = (fut, time.time(), target.name)
        logger.info("PING/Trace an %s (tag=%d, flags=%d, path=[%s]) gesendet",
                    target.name, tag, flags, path.hex())
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_traces.pop(tag, None)
            return None

    async def _trace_with_retries(self, target: Contact, attempts: int = 3,
                                  per_attempt_timeout: float = 10.0) -> str:
        """CMD_SEND_TRACE_PATH ist ein Single-Shot-RF-Paket ohne eingebauten Retry
        (anders als DMs) – ein einzelnes verlorenes Paket (hin oder zurueck) laesst
        den Ping sonst grundlos scheitern. Bis zu `attempts` Versuche."""
        for attempt in range(1, attempts + 1):
            result = await self._trace_once(target, per_attempt_timeout)
            if result:
                return result
            if attempt < attempts:
                logger.info("PING/Trace an %s: kein Pong (Versuch %d/%d) – erneuter Versuch",
                            target.name, attempt, attempts)
        return f"Keine Antwort von {target.name} (nach {attempts} Versuchen)"

    async def _deferred_trace(self, target: Contact, requester_prefix_hex: str):
        """Sendet den Trace (mit Retries) und antwortet dem Anfragenden per DM."""
        await asyncio.sleep(0)   # einmal yielden: _send_txt_capture greift txn_lock zuerst
        result = await self._trace_with_retries(target)
        await self._reply(bytes.fromhex(requester_prefix_hex), result)

    async def _list_repeaters(self, requester_prefix_hex: str):
        """Listet dem Node bekannte Repeater (ADV_TYPE_REPEATER), begrenzt auf
        REPEATER_LIST_LIMIT (per PING ohne Argument) -- verhindert einen unbegrenzten
        Chunk-Schwall bei vielen bekannten Repeatern (z.B. hunderte Kontakte im
        Livebetrieb). Bei mehr Repeatern als angezeigt: Hinweis auf PING <Teilname>
        zum Eingrenzen (nutzt die bereits vorhandene Substring-Suche in _cmd_ping)."""
        reps = sorted((c for c in self._contacts.values() if c.type == ADV_TYPE_REPEATER),
                      key=lambda c: c.name.lower())
        requester = bytes.fromhex(requester_prefix_hex)
        if not reps:
            await self._reply(requester, "Keine Repeater bekannt")
            return
        shown = reps[:REPEATER_LIST_LIMIT]
        lines = [f"Repeater ({len(shown)} von {len(reps)}):"] + [c.name for c in shown]
        if len(reps) > REPEATER_LIST_LIMIT:
            lines.append("Tipp: PING <Teil des Namens> zum gezielten Suchen/Anpingen.")
        chunks = self._finalize_chunks(lines)
        await self._send_dm_chunks(requester, chunks)
        logger.info("Repeater-Liste an %s: %d von %d Repeater, %d Chunks",
                    requester_prefix_hex, len(shown), len(reps), len(chunks))

    # ------------------------------------------------------------------
    # Channel Self-Service (add / remove)
    # ------------------------------------------------------------------

    async def _handle_channel_cmd(self, msg: IncomingMessage):
        sender = msg.sender   # Rufzeichen aus "NAME/CALLSIGN: " Prefix
        logger.info("Channel-Msg von %s [CH%d]: %s", sanitize.for_log(sender) or "?",
                    msg.channel_idx, sanitize.for_log(msg.text[:80]))

        parts = msg.text.strip().split(None, 1)
        if not parts:
            return
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "add":
            if not self.bbs.feature_enabled("selfservice"):
                logger.info("Kanal-Registrierung deaktiviert – 'add' von %s ignoriert",
                            sanitize.for_log(sender) or "?")
                return
            if ':' not in arg:
                await self._reply_channel(
                    "Format: add BENUTZERNAME:PUBKEY(64 Hex)\n"
                    "Erlaubt: Buchstaben, Zahlen, +-.!\"§$%&/()=  (3-16 Zeichen)")
                return
            username, _, pubkey_hex = arg.partition(':')
            await self._channel_add(username.strip(), pubkey_hex.strip(), path=msg.path)

    async def _channel_add(self, username: str, pubkey_hex: str, path: bytes = b''):
        """add BENUTZERNAME:PUBKEY(64 Hex) – Benutzername frei waehlbar."""
        bbs_call = self.config.get("callsign", "SysOp")

        if not _USER_RE.match(username):
            await self._reply_channel(
                f"Fehler: Benutzername '{username}' ungueltig.\n"
                "3-16 Zeichen, erlaubt: Buchstaben, Zahlen, +-.!\"§$%&/()=")
            return

        pubkey_hex = pubkey_hex.strip().lower()
        if len(pubkey_hex) != 64:
            await self._reply_channel(
                "Fehler: PUBKEY ungueltig (64 Hex-Zeichen erforderlich)\n"
                "Format: add BENUTZERNAME:PUBKEY")
            return
        if pubkey_hex in self._blocked_pubkeys:
            logger.warning("Registrierung von gesperrtem Pubkey %s... abgelehnt", pubkey_hex[:12])
            await self._reply_channel("Fehler: Registrierung nicht moeglich")
            return
        try:
            bytes.fromhex(pubkey_hex)
        except ValueError:
            await self._reply_channel("Fehler: PUBKEY enthaelt ungueltige Zeichen")
            return

        if await self.db.find_mc_contact_by_pubkey(pubkey_hex):
            logger.info("ADD abgelehnt: PUBKEY %s bereits in DB", pubkey_hex[:12])
            await self._reply_channel("Fehler: Dieser PUBKEY ist bereits registriert")
            return
        if await self.db.find_mc_contact_by_name(username):
            logger.info("ADD abgelehnt: Name %s bereits in DB", username)
            await self._reply_channel(f"Fehler: Benutzername '{username}' ist bereits vergeben")
            return

        prefix_hex = bytes.fromhex(pubkey_hex)[:6].hex()
        if prefix_hex in self._pending_registrations or any(
                p["username"] == username for p in self._pending_registrations.values()):
            await self._reply_channel(f"Fehler: '{username}' wartet bereits auf Bestaetigung")
            return
        if await self.db.find_pending_registration_by_name(username) or \
                await self.db.find_pending_registration_by_pubkey(pubkey_hex):
            await self._reply_channel(f"Fehler: '{username}' wartet bereits auf Freischaltung durch den SysOp")
            return

        # Rate-Limit gegen Squatting-Flut / Spam: der im Klartext mitgeschickte PUBKEY
        # ist bis zur Bestaetigungs-Antwort unbewiesen (siehe _send_registration_challenge),
        # jede Annahme reserviert also einen Namen ohne Identitaetsnachweis.
        now = time.time()
        self._registration_request_times = [
            t for t in self._registration_request_times if now - t < REGISTRATION_RATE_WINDOW]
        if len(self._registration_request_times) >= REGISTRATION_RATE_LIMIT:
            await self._reply_channel(
                "Fehler: Zu viele Neuanmeldungen gerade. Bitte in einer Minute erneut versuchen.")
            return
        self._registration_request_times.append(now)

        # path_known bewusst NICHT gesetzt: die 6-Byte-Channel-Metadaten (Parameter
        # 'path') sind kein gueltiger Routing-Pfad (siehe Kommentar unten) und duerfen
        # nicht in Contact.path/.path_known landen, sonst verfaelscht das spaeter die
        # Hop-Statistik (_route_for_direct_contact) bis zum naechsten RESP_CONTACT-Refresh.
        c = Contact(pubkey=bytes.fromhex(pubkey_hex), name=username)
        self._contacts[c.pubkey_prefix.hex()] = c
        self._pending_registrations[c.pubkey_prefix.hex()] = {
            "username": username, "pubkey_hex": pubkey_hex, "code": None, "attempts": 0,
        }
        logger.info("ADD: %s wartet auf Pubkey-Bestaetigung, prefix=%s, path=%s",
                    username, c.pubkey_prefix.hex(), path.hex() if path else "leer")

        # path=b'' → out_path_len=0xFF (Flooding).
        # Die 6-Byte-Metadaten aus dem Channel-Frame sind KEIN gueltiger Routing-Pfad
        # und wuerden den korrekten, vom Node selbst gelernten Pfad ueberschreiben.
        # Der Node kennt den Weg bereits durch die empfangene Channel-Nachricht
        # und entdeckt optimale Routen eigenstaendig nach dem ersten gesendeten DM.
        # Der Kontakt muss trotz ausstehender Bestaetigung schon jetzt beim Node
        # angelegt werden, sonst kann spaeter keine Bestaetigungs-DM geroutet werden.
        await self._send(build_add_contact(pubkey_hex, username))
        await asyncio.sleep(0.5)

        await self._reply_channel(
            f"{username}: Antrag erhalten, Willkommen auf {self.channel_name}! 73 de {bbs_call}")

        if self._self_pubkey:
            qth = self.config.get("qth", "BBS")
            # Name kappen, damit die URI unter dem 135-Byte-Kanal-Limit bleibt
            # (build_send_channel_msg schneidet sonst still ab und zerstoert den Link).
            bbs_name = f"BBS-{qth}"[:20]
            uri = contact_add_uri(bbs_name, self._self_pubkey.hex())
            await asyncio.sleep(1.5)
            await self._reply_channel(f"Fuege {bbs_name} als Kontakt hinzu (Link antippen):")
            # URI als eigene Nachricht: die App linkt nur eine Nachricht, die AUS der URI
            # besteht; ausserdem bliebe sie mit Prefix ueber dem 135-Byte-Kanal-Limit haengen.
            await asyncio.sleep(1.5)
            await self._reply_channel(uri)

        await asyncio.sleep(2.0)
        mode = self.config.get("registration", {}).get("mode", "challenge")

        if mode == "open":
            await self._reply_channel(
                f"{username}: Registrierung abgeschlossen, dein Account ist sofort aktiv. "
                f"73 de {bbs_call}")
            await self.db.save_mc_contact(pubkey_hex, username)
            self._registered_names[prefix_hex] = username
            welcome = (
                f"Willkommen {username}, dein Account ist jetzt aktiv. "
                f"Du erreichst das BBS per Direktnachricht, H = Hilfe. Hinweis: vor dem "
                f"ersten S/SB ist einmalig eine kurze Pubkey-Bestaetigung per Code noetig. "
                f"REMOVE loescht den Account (mit Rueckfrage zur Sicherheit). 73 de {bbs_call}"
            )
            await self._reply(bytes.fromhex(pubkey_hex)[:6], welcome)
            logger.info("Self-Service (open): %s registriert (pubkey %s...)",
                        username, pubkey_hex[:12])
            sysop_call = self.config.get("sysop", "")
            if sysop_call:
                await self.sysop_dm(
                    sysop_call,
                    f"Neuer User registriert (Modus: open): {username} (Pubkey {pubkey_hex[:12]}...)")

        elif mode == "sysop_approval":
            await self._reply_channel(
                f"{username}: Antrag wartet auf Freischaltung durch den SysOp. 73 de {bbs_call}")
            await self.db.add_pending_registration(prefix_hex, pubkey_hex, username,
                                                    path.hex() if path else "")
            logger.info("Self-Service (sysop_approval): %s wartet auf Freischaltung (pubkey %s...)",
                        username, pubkey_hex[:12])
            sysop_call = self.config.get("sysop", "")
            if sysop_call:
                await self.sysop_dm(
                    sysop_call,
                    f"Neue Registrierungsanfrage: {username} (Pubkey {pubkey_hex[:12]}...) "
                    f"wartet im Web-Admin -> Benutzer auf Freischaltung.")

        else:  # mode == "challenge" (Default/Status quo)
            await self._reply_channel(
                f"{username}: in ca. {REGISTRATION_CHALLENGE_DELAY // 60} Minuten erhaeltst du eine "
                f"DM mit einem Bestaetigungscode zum Abschluss der Registrierung (Zeit zum "
                f"Kontakt-Anlegen). 73 de {bbs_call}")
            logger.info("Self-Service (challenge): %s beantragt (pubkey %s..., path=%s)",
                        username, pubkey_hex[:12], path.hex() if path else "leer")
            self._create_tracked_task(self._send_registration_challenge(c.pubkey_prefix.hex()))

    async def _send_registration_challenge(self, prefix_hex: str):
        """Wartet REGISTRATION_CHALLENGE_DELAY, damit der User Zeit hat, das BBS als
        Kontakt anzulegen (sonst geht die DM ins Leere), und schickt dann einen
        zufaelligen Bestaetigungscode per Direktnachricht an genau den Pubkey, der
        im 'add'-Befehl behauptet wurde. Nur eine Antwort DAVON beweist, dass der
        Absender tatsaechlich diesen Pubkey/dieses Geraet besitzt -- der Klartext-
        Pubkey im Kanal-Befehl allein ist keine Identitaetsverifikation."""
        await asyncio.sleep(REGISTRATION_CHALLENGE_DELAY)
        pending = self._pending_registrations.get(prefix_hex)
        if not pending:
            return  # inzwischen abgelaufen oder Server neu gestartet (State ist nur RAM)

        code = f"{secrets.randbelow(1_000_000):06d}"
        pending["code"] = code
        pending["challenge_sent_at"] = time.time()
        await self._reply(bytes.fromhex(prefix_hex),
                           f"Bestaetigungscode: {code}\n"
                           f"Sende ihn als Antwort (Direktnachricht) um die Registrierung "
                           f"von '{pending['username']}' abzuschliessen. Gueltig "
                           f"{REGISTRATION_CHALLENGE_TIMEOUT // 60} Minuten.")
        logger.info("Registrierung: Bestaetigungscode an %s (%s...) gesendet",
                    pending["username"], prefix_hex[:12])

        await asyncio.sleep(REGISTRATION_CHALLENGE_TIMEOUT)
        expired = self._pending_registrations.get(prefix_hex)
        if expired is pending and expired.get("code") == code:
            self._pending_registrations.pop(prefix_hex, None)
            self._contacts.pop(prefix_hex, None)
            logger.info("Registrierung: Bestaetigungscode fuer %s abgelaufen, verworfen",
                        pending["username"])
            await self._reply(bytes.fromhex(prefix_hex),
                               f"Registrierung von '{pending['username']}' abgelaufen "
                               f"(keine Bestaetigung erhalten). Bitte erneut per 'add' versuchen.")

    async def _confirm_registration(self, prefix_hex: str, code: str) -> bool:
        """Prueft eine per DM eingegangene Bestaetigungscode-Antwort gegen eine
        ausstehende Registrierung und schliesst sie bei Uebereinstimmung ab.
        Gibt True zurueck, wenn die Nachricht als Bestaetigungsversuch behandelt
        wurde (unabhaengig davon ob der Code stimmte) -- der Aufrufer darf die
        Nachricht dann NICHT mehr als normalen BBS-Befehl weiterverarbeiten."""
        pending = self._pending_registrations.get(prefix_hex)
        if not pending or not pending.get("code"):
            return False

        if code.strip() != pending["code"]:
            pending["attempts"] = pending.get("attempts", 0) + 1
            if pending["attempts"] >= REGISTRATION_MAX_ATTEMPTS:
                username = pending["username"]
                self._pending_registrations.pop(prefix_hex, None)
                self._contacts.pop(prefix_hex, None)
                logger.warning("Registrierung von %s nach %d falschen Code-Versuchen verworfen",
                               username, pending["attempts"])
                await self._reply(bytes.fromhex(prefix_hex),
                                   f"Zu viele falsche Versuche. Registrierung von '{username}' "
                                   f"verworfen. Bitte erneut per 'add' versuchen.")
                return True
            await self._reply(bytes.fromhex(prefix_hex),
                               "Falscher Code. Bitte den Bestaetigungscode aus der "
                               "vorherigen DM erneut senden.")
            return True

        username, pubkey_hex = pending["username"], pending["pubkey_hex"]
        self._pending_registrations.pop(prefix_hex, None)

        if await self.db.find_mc_contact_by_pubkey(pubkey_hex) or \
                await self.db.find_mc_contact_by_name(username):
            # Zwischenzeitlich anderweitig vergeben (z.B. Admin via Web-Admin) -- abbrechen.
            await self._reply(bytes.fromhex(prefix_hex),
                               f"Fehler: '{username}' ist inzwischen anderweitig vergeben.")
            return True

        await self.db.save_mc_contact(pubkey_hex, username)
        self._registered_names[prefix_hex] = username
        bbs_call = self.config.get("callsign", "SysOp")
        welcome = (
            f"Bestaetigt! Hallo {username}, dein Account ist jetzt aktiv. "
            f"Du erreichst das BBS per Direktnachricht, H = Hilfe. Hinweis: vor dem "
            f"ersten S/SB ist einmalig eine kurze Pubkey-Bestaetigung per Code noetig. "
            f"REMOVE loescht den Account (mit Rueckfrage zur Sicherheit). 73 de {bbs_call}"
        )
        await self._reply(bytes.fromhex(prefix_hex), welcome)
        logger.info("Self-Service: %s bestaetigt und registriert (pubkey %s...)",
                    username, pubkey_hex[:12])

        sysop_call = self.config.get("sysop", "")
        if sysop_call:
            await self.sysop_dm(
                sysop_call,
                f"Neuer User registriert: {username} (Pubkey {pubkey_hex[:12]}...)")
        return True

    # ------------------------------------------------------------------
    # Pubkey-Sicherheitshinweis: Namen sind faelschbar/duplizierbar, nur der
    # Pubkey beweist Identitaet. Vor dem ersten S/SB muss jeder User (auch
    # Bestandsuser, siehe DB-Migration) dies per "OK <Code>"-DM bestaetigen.
    # ------------------------------------------------------------------

    async def _pubkey_ack_gate(self, prefix_hex: str, callsign: str) -> Optional[list[str]]:
        """Prueft, ob callsign den Pubkey-Sicherheitshinweis bereits bestaetigt hat.
        None = freigeschaltet, S/SB darf weiterlaufen. Sonst: Fehlermeldung + Hinweis
        (mit neuem oder wiederverwendetem, noch gueltigem Code) als Antwort-Zeilen.
        send_locked (nur per Web-Admin/SysOp setzbar) ueberstimmt eine evtl. bereits
        vorhandene Bestaetigung -- eine dauerhafte Sperre laesst sich NICHT durch
        erneutes 'OK <Code>' umgehen."""
        confirmed, locked = await self.db.get_pubkey_ack_status(callsign)
        if locked:
            return ["Fehler: Senden fuer dieses Konto dauerhaft gesperrt (SysOp)."]
        if confirmed:
            return None

        now = time.time()
        pending = self._pending_pubkey_ack.get(prefix_hex)
        if not pending or now - pending["sent_at"] > PUBKEY_ACK_TIMEOUT:
            pending = {"code": f"{secrets.randbelow(1_000_000):06d}", "sent_at": now}
            self._pending_pubkey_ack[prefix_hex] = pending
            logger.info("Pubkey-Hinweis-Code an %s (%s...) gesendet",
                        callsign, prefix_hex[:12])

        return [
            "Fehler: Erst Sicherheitshinweis bestaetigen (siehe unten).",
            f"WICHTIG: Pubkey beweist Identitaet, nicht der Name! PK <Name> zeigt "
            f"dessen Pubkey. Bestaetigen: OK {pending['code']} (15 Min) - deine "
            f"Nachricht wird danach automatisch gesendet. 73 SysOp",
        ]

    async def _confirm_pubkey_ack(self, prefix_hex: str, text: str) -> bool:
        """Prueft eine per DM eingegangene 'OK <Code>'-Antwort gegen eine
        ausstehende Pubkey-Hinweis-Bestaetigung. Gibt True zurueck, wenn die
        Nachricht als Bestaetigungsversuch erkannt wurde (dann NICHT mehr als
        normalen BBS-Befehl weiterverarbeiten) -- alle anderen Texte werden
        ignoriert (False), damit S/SB gesperrt bleibt, aber sonstige Befehle
        (H, NL, R, WX, ...) waehrend der Wartezeit normal nutzbar bleiben."""
        pending = self._pending_pubkey_ack.get(prefix_hex)
        if not pending:
            return False
        m = _PUBKEY_ACK_RE.match(text.strip())
        if not m:
            return False

        if m.group(1) != pending["code"]:
            await self._reply(bytes.fromhex(prefix_hex),
                               "Falscher Code. Bitte OK <Code> aus der vorherigen "
                               "Nachricht erneut senden.")
            return True

        self._pending_pubkey_ack.pop(prefix_hex, None)
        name = self._registered_names.get(prefix_hex, "")
        if name:
            await self.db.set_pubkey_ack_confirmed(name, True)
        await self._reply(bytes.fromhex(prefix_hex),
                           "Bestaetigt! Du kannst jetzt Nachrichten senden (S/SB). 73!")
        logger.info("Pubkey-Hinweis bestaetigt: %s (%s...)", name, prefix_hex[:12])

        # Urspruenglich durch den Pubkey-Hinweis blockierten Sendeversuch (S/SB/RS/
        # SBR) automatisch nachholen, damit der User ihn nicht erneut eintippen muss.
        pending_text = self._pending_send_replay.pop(prefix_hex, None)
        if pending_text and name:
            result = await self._dispatch_bbs(name, pending_text, prefix_hex)
            if result:
                await self._send_dm_chunks(bytes.fromhex(prefix_hex),
                                           self._finalize_chunks(result))
        return True

    async def _cmd_pubkey(self, arg: str, prefix_hex: str) -> list[str]:
        """PK: eigener voller Pubkey (64 Hex) zur Weitergabe an andere.
        PK <Name>: voller Pubkey (64 Hex) eines Kontakts, zum Abgleich VOR
        dem Senden -- der angezeigte Name ist kein Identitaetsnachweis."""
        arg = arg.strip()
        if not arg:
            contact = self._contacts.get(prefix_hex)
            if not contact:
                return ["Fehler: eigener Kontakt nicht gefunden."]
            return [f"Dein Pubkey: {contact.pubkey_hex}"]
        info = await self.db.get_mc_contact_info(arg)
        if not info:
            return [f"Kein Kontakt '{arg}' gefunden."]
        pubkey_hex, _, _ = info
        return [f"{arg.upper()}: {pubkey_hex}"]

    async def _channel_remove_by_callsign(self, sender: str):
        """remove via Kanal – verifiziert per Rufzeichen aus dem Nachrichten-Prefix."""
        if not sender:
            await self._reply_channel("Fehler: Rufzeichen nicht erkannt")
            return
        entry = await self.db.find_mc_contact_by_name(sender)
        if not entry:
            await self._reply_channel(f"Fehler: {sender} nicht registriert")
            return
        stored_pubkey_hex, _ = entry
        pubkey_prefix = bytes.fromhex(stored_pubkey_hex)[:6]
        await self.db.delete_mc_contact(stored_pubkey_hex)
        self._contacts.pop(pubkey_prefix.hex(), None)
        self._registered_names.pop(pubkey_prefix.hex(), None)
        await self._reply_channel(f"{sender} entfernt. 73!")
        logger.info("Self-Service: %s via Kanal entfernt", sender)

    async def _handle_remove_request(self, pubkey_prefix: bytes, prefix_hex: str, callsign: str):
        """REMOVE loescht erst nach Bestaetigung: derselbe Befehl muss innerhalb von
        REMOVE_CONFIRM_WINDOW erneut gesendet werden -- verhindert versehentliches,
        unwiderrufliches Loeschen des Kontos (inkl. Nachrichten, je nach Konfiguration)
        durch einen Tippfehler oder zu fruehes Absenden. REMOVE bezieht sich immer nur
        auf den eigenen Account (kein fremder Zielparameter) -- anders als bei K/ND
        gibt es hier kein Enumerations-Risiko, die Rueckfrage kann also vor jeder
        weiteren Pruefung erfolgen."""
        now = time.time()
        pending_since = self._pending_remove_confirm.get(prefix_hex)
        if pending_since is not None and now - pending_since <= REMOVE_CONFIRM_WINDOW:
            self._pending_remove_confirm.pop(prefix_hex, None)
            await self._direct_remove(pubkey_prefix, prefix_hex, callsign)
            return
        self._pending_remove_confirm[prefix_hex] = now
        await self._reply(pubkey_prefix,
            f"Achtung: REMOVE loescht deinen Account dauerhaft (inkl. empfangener "
            f"privater Nachrichten, ggf. auch gesendeter). Zum Bestaetigen innerhalb "
            f"von {int(REMOVE_CONFIRM_WINDOW)}s erneut REMOVE senden.")

    async def _direct_remove(self, pubkey_prefix: bytes, prefix_hex: str, callsign: str):
        """remove via Direct Message – prueft ob prefix_hex zum gespeicherten Pubkey passt."""
        entry = await self.db.find_mc_contact_by_name(callsign)
        if not entry:
            await self._reply(pubkey_prefix, f"Fehler: {callsign} nicht registriert")
            return

        stored_pubkey_hex, _ = entry
        if stored_pubkey_hex[:12] != prefix_hex:
            await self._reply(pubkey_prefix, "Fehler: Pubkey stimmt nicht ueberein")
            return

        await self.db.delete_mc_contact(stored_pubkey_hex)
        await self.db.purge_user_messages(callsign, *self._delete_sent_flags())
        self.evict_contact_cache(stored_pubkey_hex)
        await self._reply(pubkey_prefix, f"{callsign} entfernt. 73!")
        logger.info("Self-Service: %s entfernt (%s)", callsign, prefix_hex)

    async def _handle_kill_request(self, prefix_hex: str, callsign: str, msg_id: int,
                                   cmd_word: str) -> list[str]:
        """K/ND loeschen erst nach Bestaetigung: derselbe Befehl (gleiche Nachrichten-
        ID) muss innerhalb von KILL_CONFIRM_WINDOW erneut gesendet werden. Die
        Berechtigungspruefung (can_kill, inkl. Maskierung nicht existierender/fremder
        privater Nachrichten als "nicht gefunden") laeuft VOR der Rueckfrage und bei
        jedem Versuch erneut -- damit bekommen unautorisierte Versuche exakt dieselbe
        Antwort wie ohne Rueckfrage-Mechanismus (kein neues Enumerations-Leck)."""
        ok, err = await self.bbs.can_kill(callsign, msg_id)
        if not ok:
            return err
        now = time.time()
        pending = self._pending_kill_confirm.get(prefix_hex)
        if pending and pending[0] == msg_id and now - pending[1] <= KILL_CONFIRM_WINDOW:
            self._pending_kill_confirm.pop(prefix_hex, None)
            return await self.bbs.cmd_kill(callsign, msg_id)
        self._pending_kill_confirm[prefix_hex] = (msg_id, now)
        return [f"Nachricht #{msg_id} wirklich loeschen? Zum Bestaetigen innerhalb von "
                f"{int(KILL_CONFIRM_WINDOW)}s erneut {cmd_word} {msg_id} senden."]

    def _retry_timeout(self, est_timeout_ms: int) -> float:
        """Leitet die ACK-Wartezeit aus dem vom Node gemeldeten est_timeout ab."""
        secs = est_timeout_ms / 1000.0 if est_timeout_ms else CONFIRM_TIMEOUT
        return max(CONFIRM_FLOOR, min(secs, CONFIRM_CAP))

    async def _send_txt_capture(self, pubkey_prefix: bytes, chunk: str):
        """Sendet einen DM-Chunk und faengt das RESP_CODE_SENT ab.
        Gibt (expected_ack|None, est_timeout_ms, is_flood) zurueck."""
        async with self._txn_lock:
            loop = asyncio.get_running_loop()
            self._sent_waiter = loop.create_future()
            try:
                if not await self._send(build_send_txt(pubkey_prefix, chunk)):
                    return None, 0, False
                route_flag, expected_ack, est_timeout = await asyncio.wait_for(
                    self._sent_waiter, timeout=SENT_WAIT)
                return (expected_ack or None), est_timeout, (route_flag == 1)
            except asyncio.TimeoutError:
                logger.warning("Kein RESP_CODE_SENT nach DM an %s", pubkey_prefix.hex())
                return None, 0, False
            finally:
                self._sent_waiter = None

    def _finalize_chunks(self, lines: list[str]) -> list[str]:
        """Zerlegt lines byte-genau in Chunks (max_len) und deckelt sie auf max_chunks.
        Das letzte Stueck wird bei Ueberlauf byte-sicher gekuerzt und mit ' [+]' als
        unvollstaendig markiert -- stille Kuerzung waere bei zugestelltem Inhalt
        irrefuehrend (der Empfaenger muss erkennen, dass nicht alles ankam; der Text
        selbst bleibt in der DB vollstaendig)."""
        chunks = _chunk(lines, self.max_len)
        if len(chunks) > self.max_chunks:
            suffix = " [+]"
            chunks = chunks[:self.max_chunks]
            chunks[-1] = _truncate_to_bytes(chunks[-1], self.max_len - _utf8_len(suffix)) + suffix
        return chunks

    async def _send_dm_chunks(self, pubkey_prefix: bytes, chunks: list[str]):
        """Sendet DM-Chunks und registriert sie fuer ACK-Tracking (expected_ack je Chunk)."""
        prefix_hex = pubkey_prefix.hex()
        acks: dict = {}
        est_max = 0
        any_flood = False
        for i, chunk in enumerate(chunks):
            if i > 0:
                await asyncio.sleep(self.chunk_delay)
            ack, est, is_flood = await self._send_txt_capture(pubkey_prefix, chunk)
            if ack:
                acks[ack] = chunk
            est_max = max(est_max, est)
            if is_flood:
                any_flood = True
        self._pending[prefix_hex] = _Pending(
            chunks=chunks, sent_at=time.time(), retries=0,
            acks=acks, timeout=self._retry_timeout(est_max),
            is_flood=any_flood)

    async def _confirm_watchdog_loop(self):
        """Wiederholt unbestaetigte DMs. Timeout aus est_timeout; ab dem 1. Retry
        wird der Pfad zurueckgesetzt (Flood erzwingen) – wichtig fuer Multi-Hop."""
        await asyncio.sleep(10)
        while self._running:
            await asyncio.sleep(5)
            now = time.time()
            for prefix_hex in list(self._pending.keys()):
                pend = self._pending.get(prefix_hex)
                if pend is None or now - pend.sent_at < pend.timeout:
                    continue
                contact = self._contacts.get(prefix_hex)
                name = contact.name if contact else prefix_hex
                # Flood-DMs: kein Retry – best-effort, ACK kommt bei schlechtem Link nicht zurueck
                max_retries = 0 if pend.is_flood else MAX_RETRIES
                if pend.retries >= max_retries:
                    canonical = self._canonical_name(prefix_hex, name)
                    if pend.is_flood:
                        logger.info("Flood-DM an %s: kein ACK (best-effort gesendet)", name)
                        self._create_tracked_task(self.db.log_event("noack", canonical, "flood", route="flood"))
                    else:
                        logger.warning("Keine Bestaetigung von %s nach %d Versuchen – aufgegeben",
                                       name, MAX_RETRIES + 1)
                        route = self._route_for_direct_contact(contact)
                        self._create_tracked_task(self.db.log_event("noack", canonical, "retries", route=route))
                    self._pending.pop(prefix_hex, None)
                    continue
                # Eintrag herausnehmen, damit spaete Alt-ACKs ihn nicht wiederbeleben
                pend = self._pending.pop(prefix_hex)
                pubkey_prefix = bytes.fromhex(prefix_hex)
                # nur noch unbestaetigte Chunks erneut senden (sonst Duplikate)
                resend = list(pend.acks.values()) if pend.acks else pend.chunks
                logger.warning("Kein ACK von %s – Retry %d/%d (%d Chunk(s))",
                               name, pend.retries + 1, MAX_RETRIES, len(resend))
                if pend.retries == 0:
                    # Vollen 32-Byte-Pubkey senden – 6-Byte-Prefix wird vom Node abgelehnt
                    reset_pubkey = contact.pubkey if contact else pubkey_prefix
                    logger.info("Reset-Path fuer %s – Flood erzwingen", name)
                    await self._send(build_reset_path(reset_pubkey))
                    await asyncio.sleep(1.0)
                acks: dict = {}
                est_max = 0
                for i, chunk in enumerate(resend):
                    if i > 0:
                        await asyncio.sleep(self.chunk_delay)
                    ack, est, _ = await self._send_txt_capture(pubkey_prefix, chunk)
                    if ack:
                        acks[ack] = chunk
                    est_max = max(est_max, est)
                self._pending[prefix_hex] = _Pending(
                    chunks=pend.chunks, sent_at=time.time(), retries=pend.retries + 1,
                    acks=acks, timeout=self._retry_timeout(est_max),
                    is_flood=pend.is_flood)

    async def _reply(self, pubkey_prefix: bytes, text: str):
        # Ueber _finalize_chunks statt [text]: lange Antworten (z.B. mehrsaetzige
        # Willkommens-/Bestaetigungstexte ueber 150 Byte) wurden sonst von
        # build_send_txt still auf 150 Byte abgeschnitten -- jetzt byte-genau in
        # mehrere DMs aufgeteilt.
        await self._send_dm_chunks(pubkey_prefix, self._finalize_chunks(text.splitlines() or [text]))

    async def _reply_channel(self, text: str):
        await self._send(build_send_channel_msg(self.channel, text))
