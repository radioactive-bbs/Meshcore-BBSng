import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from core.message import Message
from core.weather import fetch_forecast_1day, fetch_forecast_3days, fetch_weather
from core import sanitize
from core.timeutil import now_utc
from core.validation import is_valid_email
from storage.database import Database

logger = logging.getLogger(__name__)

# Signatur: async def notify_dm(to_call: str, text: str) -> bool
# Versucht, dem angegebenen Rufzeichen per MeshCore-DM zuzustellen. Gibt False
# zurueck, wenn der Name nicht als MeshCore-Kontakt registriert ist (z.B. Tippfehler
# oder noch nicht registrierter Empfaenger) - kein Fehler, nur "konnte nicht benachrichtigen".
NotifyDM = Callable[[str, str], Awaitable[bool]]

# Schaltbare BBS-Funktionen (Web-Admin: Einstellungen -> Funktionen).
# key -> (Label fuer die Web-UI, Default)
FEATURES = {
    "messages":    ("Nachrichten (N/S/NL/R/K)", True),
    "board":       ("Board (B/SB/BL)", True),
    "weather":     ("Wetter (W/WX/WX1/WX3)", True),
    "sysinfo":     ("Sysinfo (SI)", True),
    "online":      ("Online-Anzeige (O)", True),
    "userlist":    ("Userliste (LU)", True),
    "ping":        ("PING/Traceroute", True),
    "account":     ("Account (A/MI/MC)", True),
    "selfservice": ("Kanal-Registrierung (add)", True),
}


class BBSCore:
    # BL/NL ohne Argument zeigen die juengsten FIRST_PAGE Nachrichten (+ Sticky bei
    # Board, nur auf der ersten Seite). BLO/NLO <n> blaettert danach in PAGE_SIZE-
    # Schritten (z.B. BLO 10 -> Eintraege 10-19, BLO 20 -> 20-29).
    FIRST_PAGE = 9
    PAGE_SIZE = 10
    # Zeilen, die BT<n> maximal ausgibt. Der MeshCore-Chunker sendet hoechstens
    # max_chunks (Default 5) Pakete a ~150 Byte mit chunk_delay Sekunden Abstand --
    # ein langer Thread wuerde die Ausgabe sonst abschneiden bzw. den Kanal blockieren.
    THREAD_MAX_ROWS = 10
    DEFAULT_MAX_PERSONAL_MESSAGES = 30   # Fallback falls messages.max_personal nicht konfiguriert ist

    def __init__(self, db: Database, config: dict, notify_dm: Optional[NotifyDM] = None):
        self.db = db
        self.config = config
        # Optionaler Callback fuer proaktive DM-Benachrichtigungen (neue Nachricht,
        # Loesch-Erinnerung). None, wenn kein Protokoll mit Push-Faehigkeit (MeshCore)
        # verfuegbar ist - Feature bleibt dann einfach inaktiv, kein Fehler.
        self._notify_dm = notify_dm
        # Referenzen auf im Hintergrund laufende Benachrichtigungs-Tasks (siehe
        # _fire_notify) - ohne das haelt nichts den Task am Leben, asyncio darf ihn
        # sonst mitten in der Ausfuehrung einsammeln (dokumentiertes asyncio-Fallstrick).
        self._background_tasks: set = set()

    async def _try_notify(self, to_call: str, text: str):
        """Best-effort-Benachrichtigung: Fehler/fehlender Callback duerfen den
        eigentlichen BBS-Vorgang (Nachricht speichern etc.) nie verhindern."""
        if not self._notify_dm:
            return
        try:
            await self._notify_dm(to_call, text)
        except Exception:
            logger.warning("Benachrichtigung an %s fehlgeschlagen", to_call, exc_info=True)

    def _fire_notify(self, to_call: str, text: str):
        """Wie _try_notify, aber nicht abgewartet: der eigentliche DM-Versand ist
        ueber den Node seriell und dauert pro Empfaenger typischerweise 10-30s (bis
        zu ~90s bei schlechter Verbindung, siehe CONFIRM_CAP in server.py). Wuerde
        cmd_bulletin_reply mehrere Benachrichtigungen NACHEINANDER abwarten, haengt
        die Befehlsantwort (und damit das ganze BBS fuer alle User, da der Node-Send
        global serialisiert ist) minutenlang. Die Benachrichtigungen laufen daher im
        Hintergrund weiter, waehrend der Befehl selbst sofort zurueckkehrt."""
        task = asyncio.create_task(self._try_notify(to_call, text))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def feature_enabled(self, key: str) -> bool:
        """Liest Feature-Flags live aus der Config (Web-UI schaltet ohne Neustart)."""
        default = FEATURES.get(key, ("", True))[1]
        return bool(self.config.get("features", {}).get(key, default))

    @property
    def max_personal_messages(self) -> int:
        """Postfach-Limit je Empfaenger, live aus der Config (Web-Admin: Einstellungen).
        Wird das Limit nachtraeglich verkleinert, bleiben bereits vorhandene Nachrichten
        ueber dem Limit erhalten - S/SP lehnt nur *neue* Sendungen ab, solange das
        Postfach am/ueber dem Limit liegt (siehe cmd_send)."""
        return int(self.config.get("messages", {}).get("max_personal", self.DEFAULT_MAX_PERSONAL_MESSAGES))

    def _any_info_feature(self) -> bool:
        return any(self.feature_enabled(k) for k in ("sysinfo", "online", "userlist", "ping"))

    # ------------------------------------------------------------------
    # Menüs. Nur aktivierte Funktionen werden angezeigt. Werden ueber mehrere
    # ~150-Byte-Chunks gesendet, falls noetig (siehe MeshCoreServer._chunk) -
    # kein Byte-Limit auf Gesamtlaenge, aber jeder zusaetzliche Chunk kostet
    # chunk_delay Sekunden mehr Airtime, daher trotzdem kompakt halten.
    #
    # Kurzbefehl-Notation [X]wort: der Buchstabe in Klammern zeigt, welcher
    # Buchstabe des folgenden Wortes der Shortcut ist (z.B. [N]achrichten = N).
    # Nur wo das Kuerzel tatsaechlich aus dem deutschen Wort stammt - mehrere
    # Kuerzel sind klassischer Packet-Radio-BBS-Jargon (englisch) und passen
    # NICHT zum deutschen Anzeigetext, bleiben daher bewusst unmarkiert:
    # R (= "Read") -> Lesen, K/ND (= "Kill"/"No Deliver") -> Loeschen,
    # SP (= "Send Personal"), RS/SBR (= "Reply-Send") -> Antwort,
    # WX/WX1/WX3 (= engl. Wetter-Jargon) -> Wetter/Morgen/Dreitage.
    # ------------------------------------------------------------------

    async def cmd_help(self, callsign: str = "") -> list[str]:
        return await self.menu_main(callsign)

    async def menu_main(self, callsign: str = "") -> list[str]:
        lines = ["\U0001f4e1 BBS-Main"]
        if self.feature_enabled("messages"):
            badge = ""
            if callsign:
                unread = await self.db.count_unread_personal(callsign)
                if unread:
                    badge = f" ({unread} neu)"
            lines.append(f"\U0001f4e8 [N]achrichten{badge}")
        if self.feature_enabled("board"):
            badge = ""
            if callsign:
                seen = await self.db.get_board_seen_at(callsign)
                new_threads = await self.db.count_board_threads_since(seen)
                if new_threads:
                    badge = f" ({new_threads} neu)"
            lines.append(f"\U0001f4cb [B]oard{badge}")
        if self.feature_enabled("weather"):
            lines.append("⛅ [W]etter")
        if self._any_info_feature():
            lines.append("\U0001f4d8 [I]nfo")
        if self.feature_enabled("account"):
            lines.append("\U0001f464 [A]ccount")
        result = ["\n".join(lines)]
        motd = str(self.config.get("motd", "") or "").strip()
        if motd:
            result.append("")   # Leerzeile zwischen Menue und MOTD
            result.append(f"\U0001f4e2 {motd}")
        return result

    async def menu_messages(self, callsign: str = "") -> list[str]:
        count = await self.db.count_personal_messages(callsign) if callsign else 0
        return ["\n".join([
            f"\U0001f4e8 Nachrichten {count}/{self.max_personal_messages}",
            "\U0001f4cb [N]achrichten[L]iste",
            "\U0001f4d6 R<n> Lesen",
            "\U0001f4d1 [N]achrichten[T]hread <n> (Verlauf, markiert gelesen)",
            "✉ [S]enden TO|Betr|Text  RS<n>|Text Antwort",
            "\U0001f5d1 ND<n> Loeschen (nur eigene erhaltene)  \U0001f4e1 H Main",
            "Auch: NACHRICHTENLISTE, NACHRICHTENTHREAD, LESEN, SENDEN, ANTWORT, LOESCHEN",
        ])]

    async def menu_board(self) -> list[str]:
        return ["\n".join([
            "\U0001f4cb Board",
            "\U0001f4cb [B]oard[L]iste",
            "\U0001f4d6 R<n> Lesen",
            "\U0001f4d1 [B]oard[T]hread <n> (Antworten anzeigen)",
            "\U0001f4dd [S]enden [B]ulletin Thema|Text  SBR<n>|Text Antwort",
            "\U0001f5d1 ND<n> Loeschen (nur eigene Bulletins)  \U0001f4e1 H  BBS-Main",
            "Auch: BOARDLISTE, BOARDTHREAD, BULLETIN, BULLETINANTWORT, LOESCHEN",
        ])]

    async def menu_weather(self) -> list[str]:
        return ["\n".join([
            "⛅ [W]etter",
            "⛅ WX   Aktuell",
            "\U0001f324 WX1  Morgen",
            "\U0001f4c5 WX3  3 Tage",
            "\U0001f4e1 H    BBS-Main",
            "Auch: WETTER, MORGEN, DREITAGE",
        ])]

    async def menu_info(self) -> list[str]:
        lines = ["\U0001f4d8 Info"]
        if self.feature_enabled("sysinfo"):
            lines.append("\U0001f4d8 [S]ys[I]nfo")
        if self.feature_enabled("online"):
            lines.append("\U0001f465 [O]nline")
        if self.feature_enabled("userlist"):
            lines.append("\U0001f465 [L]iste [U]ser")
        if self.feature_enabled("ping"):
            lines.append("\U0001f4e1 PING  Repeaterliste")
            lines.append("\U0001f4e1 PING <Name>  Node-Ping")
        lines.append("\U0001f511 [P]ub[K]ey <Name> (ohne Name: eigener)")
        lines.append("\U0001f4e1 H  BBS-Main")
        lines.append("Auch: SYSINFO, ONLINE, USERLISTE, PUBKEY")
        return ["\n".join(lines)]

    async def menu_account(self) -> list[str]:
        return ["\n".join([
            "\U0001f464 Account",
            "\U0001f464 [M]eine [I]nfo",
            "\U0001f4e7 [M]ail [C]ontact",
            "\U0001f6aa REMOVE  Abmelden",
            "\U0001f4e1 H  BBS-Main",
            "Auch: MEINEINFO, MAIL",
        ])]

    # ------------------------------------------------------------------
    # Befehle
    # ------------------------------------------------------------------

    async def cmd_info(self, active_count: int = 0) -> list[str]:
        cfg = self.config
        msgs = await self.db.get_messages()
        users = await self.db.count_mc_contacts()
        return ["\n".join([
            f"BBS: {cfg.get('callsign', 'Meshcore BBSng')}",
            f"SysOp: {cfg.get('sysop', '-')}  QTH: {cfg.get('qth', '-')}",
            f"Loc: {cfg.get('locator', '-')}",
            f"Kontakt: {cfg.get('sysop_mail', '-')}",
            f"User: {users}  Msgs: {len(msgs)}  Online: {active_count}",
        ])]

    async def cmd_list(self) -> list[str]:
        """Alter kombinierter L-Befehl: seit der BL/NL-Trennung nur noch ein Hinweis,
        L zeigte vorher Board+privat gemischt und ungefiltert fuer jeden Absender an
        (>CALLSIGN liess sogar fremde Postfach-Betreffs mitlesen - bewusst nicht
        uebernommen)."""
        return ["BL/BOARDLISTE = Board Liste, NL/NACHRICHTENLISTE = Nachrichten Liste"]

    async def cmd_list_board(self, callsign: str = "", offset: Optional[int] = None) -> list[str]:
        # SQL-seitig gefiltert/paginiert statt die ganze Tabelle zu laden: Board-
        # Nachrichten fassen 'P'-Zeilen gar nicht mehr an (kein Entschluesseln
        # fremder Postfaecher). Sticky bleibt aus der Offset-Paginierung ausgenommen
        # und nur auf Seite 1 sichtbar -- daher getrennte Queries statt LIMIT/OFFSET.
        # Gelistet werden nur Thread-Anfaenge (mit Antwortzaehler); die Antworten
        # selbst zeigt BT<Nr>. Das haelt die Liste kurz - jede Zeile kostet Airtime.
        callsign = callsign.upper()
        # Alten Stand VOR dem Rendern lesen, damit die Zeilen-Markierung noch den
        # Stand vor diesem Aufruf zeigt - danach wird board_seen_at aktualisiert,
        # das raeumt sowohl die Zeilen-Marker als auch den Hauptmenue-Badge auf
        # (jeder BL-Aufruf gilt als "Board angesehen", unabhaengig von R<n>).
        old_seen = await self.db.get_board_seen_at(callsign) if callsign else None
        old_seen_dt = datetime.fromisoformat(old_seen) if old_seen else None
        total_other = await self.db.count_board_threads()
        if offset is None:
            sticky_heads = await self.db.list_board_thread_heads(sticky=True, seen_at=old_seen_dt)
            page = sticky_heads + await self.db.list_board_thread_heads(
                self.FIRST_PAGE, 0, seen_at=old_seen_dt)
        else:
            # offset ist 1-basiert (BLO 10 -> Thread Nr. 10 der Liste, lueckenlos
            # anschliessend an die "juengsten 9" der ersten Seite).
            start = max(offset - 1, 0)
            page = await self.db.list_board_thread_heads(self.PAGE_SIZE, start, seen_at=old_seen_dt)

        if not page:
            if callsign:
                await self.db.set_board_seen_at(callsign, now_utc())
            return ["Keine Board-Nachrichten." if offset is None
                    else f"Keine weiteren Board-Nachrichten ab {offset}."]

        lines = ["\U0001f4cb Board" + (f" ab {offset}" if offset else "")]
        # Feldreihenfolge: Nr, Sticky, Datum, Von, Betreff. Kopfzeile-Spaltenbreiten
        # spiegeln exakt die der Datenzeilen (Nr=3, Sticky=6, Datum=8, Von=9).
        # Beide Sticky-Icons stammen bewusst aus demselben Unicode-Block (Misc.
        # Symbols and Pictographs) und werden damit auf MeshCore-Displays gleich
        # breit dargestellt - so bleiben sticky/nicht-sticky Zeilen untereinander
        # ausgerichtet, auch wenn beide etwas breiter sind als die Kopfzeile.
        lines.append(f"{'Nr':<3} {'Sticky':<6} {'Datum':<8} {'Von':<9} Betreff")
        for m, replies, last_activity, new_count in page:
            pin = "\U0001f4cc" if m.sticky else "\U0001f4c4"   # 📌 sticky / 📄 nicht sticky
            # Datum = letzte Aktivitaet im Thread, nicht Erstelldatum: nur so ist die
            # Sortierung (Thread mit frischer Antwort steht oben) nachvollziehbar.
            date = last_activity.strftime("%d.%m.%y")
            total = replies + 1
            suffix = self._count_suffix(total, new_count if callsign else 0)
            # Bei Zaehler-Suffix frueher kuerzen, damit die Zeile so breit bleibt wie ohne.
            cut = 11 if suffix else 15
            subject = m.subject if len(m.subject) <= cut else m.subject[:cut] + "..."
            subject += suffix
            lines.append(f"{m.id:>3} {pin:<6} {date:<8} {m.from_call:<9} {subject}")
        if any(replies for _, replies, _, _ in page):
            lines.append("BT<Nr> zeigt die Antworten")
        if offset is None and total_other > self.FIRST_PAGE:
            lines.append(f"BL {self.FIRST_PAGE + 1} fuer weitere ({total_other} gesamt)")
        elif offset is not None and total_other > start + self.PAGE_SIZE:
            lines.append(f"BL {offset + self.PAGE_SIZE} fuer weitere ({total_other} gesamt)")
        if callsign:
            await self.db.set_board_seen_at(callsign, now_utc())
        return lines

    @staticmethod
    def _count_suffix(total: int, new: int) -> str:
        """Zaehler-Suffix fuer Thread-Listen (BL/NL): '(gesamt/neu)' wenn es etwas
        Neues gibt (auch bei nur 1 Nachricht - ein frischer Einzel-Thread zaehlt),
        sonst nur '(gesamt)' wenn mehr als eine Nachricht im Thread steckt, sonst
        gar nichts (Standardfall: gelesen, keine Antworten)."""
        if new:
            return f" ({total}/{new})"
        if total > 1:
            return f" ({total})"
        return ""

    async def cmd_board_thread(self, msg_id: int) -> list[str]:
        """Zeigt einen Board-Thread: Anfang + alle Antworten daran. msg_id darf der
        Thread-Anfang ODER eine seiner Antworten sein (aus BL kommt die Anfangs-Nummer,
        aus einer gelesenen Antwort deren eigene) - beides landet beim selben Thread."""
        msg = await self.db.get_message(msg_id)
        if not msg or msg.msg_type != "B":
            return [f"Bulletin #{msg_id} nicht gefunden."]
        root_id = await self._thread_root_id(msg)
        thread = await self.db.get_thread(root_id)
        if not thread:
            return [f"Bulletin #{msg_id} nicht gefunden."]
        root, replies = thread[0], thread[1:]
        subject = root.subject if len(root.subject) <= 20 else root.subject[:20] + "..."
        lines = [f"\U0001f4cb Thread #{root.id} {subject}"]
        # Bei langen Threads die AELTESTEN Antworten weglassen, nicht die neuesten -
        # der Anfang bleibt als Kontext immer stehen.
        skipped = max(len(replies) - (self.THREAD_MAX_ROWS - 1), 0)
        for m in [root] + replies[skipped:]:
            date = m.created_at.strftime("%d.%m.%y")
            mark = " (Start)" if m.id == root.id else ""
            lines.append(f"{m.id:>3} {date:<8} {m.from_call:<9}{mark}")
            if m.id == root.id and skipped:
                lines.append(f"... {skipped} aeltere Antworten ausgelassen")
        lines.append("R<Nr> zum Lesen, SBR<Nr>|Text zum Antworten")
        return lines

    async def _thread_root_id(self, msg: Message) -> int:
        """Thread-Anfang zu einer Board-Nachricht. Zeigt thread_id auf eine inzwischen
        geloeschte Nachricht (Waise), ist die Nachricht selbst wieder der Anfang -
        dieselbe Regel wie in der Board-Liste (siehe Database._BOARD_ROOT_COND)."""
        if not msg.thread_id or msg.thread_id == msg.id:
            return msg.id
        return msg.id if await self.db.get_message(msg.thread_id) is None else msg.thread_id

    async def cmd_list_personal(self, callsign: str, offset: Optional[int] = None) -> list[str]:
        callsign = callsign.upper()
        # SQL-seitig paginiert, gruppiert nach Thread (analog cmd_list_board) - ein
        # privates Postfach ist gerichtet (to_call), daher hier Postfach-skalierte
        # Thread-Koepfe statt eines globalen Roots (siehe Database.
        # list_personal_thread_heads). Quota-Anzeige (count/max) bleibt Zeilen-
        # basiert, das ist weiterhin der harte Limit-Check in cmd_send.
        raw_count = await self.db.count_personal_messages(callsign)
        total = await self.db.count_personal_threads(callsign)
        if offset is None:
            page = await self.db.list_personal_thread_heads(callsign, self.FIRST_PAGE, 0)
        else:
            start = max(offset - 1, 0)
            page = await self.db.list_personal_thread_heads(callsign, self.PAGE_SIZE, start)

        if not page:
            return ["Keine Nachrichten." if offset is None
                    else f"Keine weiteren Nachrichten ab {offset}."]

        lines = [f"✉ Nachrichten: {total} Threads ({raw_count}/{self.max_personal_messages})"
                + (f" ab {offset}" if offset else "")]
        lines.append(f"{'Nr':<3} {'Von':<9} {'Datum':<8} Betreff")
        for m, replies, last_activity, unread in page:
            date = last_activity.strftime("%d.%m.%y")
            suffix = self._count_suffix(replies + 1, unread)
            cut = 11 if suffix else 15
            subject = m.subject if len(m.subject) <= cut else m.subject[:cut] + "..."
            subject += suffix
            lines.append(f"{m.id:>3} {m.from_call:<9} {date:<8} {subject}")
        if any(replies for _, replies, _, _ in page):
            lines.append("NT<Nr> zeigt den Verlauf")
        if offset is None and total > self.FIRST_PAGE:
            lines.append(f"NL {self.FIRST_PAGE + 1} fuer weitere ({total} gesamt)")
        elif offset is not None and total > start + self.PAGE_SIZE:
            lines.append(f"NL {offset + self.PAGE_SIZE} fuer weitere ({total} gesamt)")
        return lines

    async def cmd_personal_thread(self, callsign: str, msg_id: int) -> list[str]:
        """Zeigt den Verlauf einer privaten Thread-Gruppe (eigener Empfang, siehe
        Database.get_personal_thread) und markiert ihn komplett als gelesen - wie eine
        Chat-Unterhaltung oeffnen. Zugriffskontrolle wie cmd_read: nur der tatsaechliche
        Empfaenger, sonst dieselbe 'nicht gefunden'-Maskierung gegen Enumeration."""
        callsign = callsign.upper()
        msg = await self.db.get_message(msg_id)
        if not msg or msg.msg_type != "P" or msg.to_call.upper() != callsign:
            return [f"Nachricht #{msg_id} nicht gefunden."]
        root_key = msg.thread_id or msg.id
        thread = await self.db.get_personal_thread(callsign, root_key)
        if not thread:
            return [f"Nachricht #{msg_id} nicht gefunden."]
        root, replies = thread[0], thread[1:]
        subject = root.subject if len(root.subject) <= 20 else root.subject[:20] + "..."
        lines = [f"✉ Verlauf mit {root.from_call} ({subject})"]
        skipped = max(len(replies) - (self.THREAD_MAX_ROWS - 1), 0)
        for m in [root] + replies[skipped:]:
            date = m.created_at.strftime("%d.%m.%y")
            lines.append(f"{m.id:>3} {date:<8} {m.from_call:<9}")
            if m.id == root.id and skipped:
                lines.append(f"... {skipped} aeltere Nachrichten ausgelassen")
        lines.append("R<Nr> zum Lesen, RS<Nr>|Text zum Antworten")
        await self.db.mark_thread_read(callsign, root_key)
        return lines

    async def cmd_read(self, callsign: str, msg_id: int) -> list[str]:
        msg = await self.db.get_message(msg_id)
        if not msg:
            return [f"Nachricht #{msg_id} nicht gefunden."]
        # Zugriffskontrolle: private Nachrichten ('P') darf NUR Empfaenger oder Absender
        # lesen. Der SysOp ist bewusst ausgeschlossen – Postfaecher sind vertraulich, auch
        # vor dem Betreiber (das Web-Admin zeigt private Inhalte ebenfalls nicht an, nur
        # Metadaten + Loeschen). Board-Nachrichten ('B') sind oeffentlich. Bewusst die
        # gleiche "nicht gefunden"-Meldung wie bei fehlender ID, damit fremde Postfaecher
        # nicht per R<n>-Enumeration aufgezaehlt werden koennen.
        caller = callsign.upper()
        if msg.msg_type == "P" and caller not in (msg.to_call.upper(), msg.from_call.upper()):
            return [f"Nachricht #{msg_id} nicht gefunden."]
        await self.db.mark_read(msg_id)
        lines = [
            f"#{msg.id} [{msg.msg_type}] {msg.created_at.strftime('%d.%m.%y %H:%M')} UTC",
            f"Von: {msg.from_call}  An: {msg.to_call}",
            f"Betreff: {msg.subject}",
        ]
        # Bei Board-Nachrichten den Thread-Bezug sichtbar machen: eine Antwort nennt
        # ihren Thread-Anfang, ein Thread-Anfang seine Antworten (gedeckelt, damit ein
        # langer Thread die Ausgabe nicht ueber das Chunk-Budget hinaus aufblaeht).
        if msg.msg_type == "B":
            root_id = await self._thread_root_id(msg)
            if root_id != msg.id:
                lines.append(f"Antwort auf #{root_id} (BT{root_id} = Thread)")
            else:
                reply_ids = await self.db.list_thread_reply_ids(msg.id)
                if reply_ids:
                    shown = " ".join(f"#{i}" for i in reply_ids[:5])
                    more = " ..." if len(reply_ids) > 5 else ""
                    lines.append(f"{len(reply_ids)} Antworten: {shown}{more}")
        elif caller == msg.to_call.upper():
            # Direkter Antwort-Hinweis wie beim Board-Pendant oben -- ohne den gibt es
            # fuer den haeufigsten Lesepfad (R<n> direkt nach einer Push-Benachrichtigung)
            # gar keine sichtbare Anleitung zum Antworten, nur ueber den Umweg NT<n>.
            lines.append(f"RS{msg.id}|Text zum Antworten")
        lines += ["---", msg.body, "---"]
        return lines

    async def cmd_send(self, from_call: str, to_call: str, subject: str, body: str,
                       thread_id: Optional[int] = None) -> list[str]:
        to_call = to_call.upper()
        count = await self.db.count_personal_messages(to_call)
        if count >= self.max_personal_messages:
            return [f"Postfach von {to_call} ist voll ({count}/{self.max_personal_messages}). "
                    f"Nicht gesendet."]
        # Registrierung VOR dem Speichern pruefen: ohne diese Pruefung sah die
        # Bestaetigung bei jedem Tippfehler im Rufzeichen (oder wenn der Empfaenger
        # sich zwischenzeitlich entfernt hat) exakt gleich aus wie bei echter
        # Zustellung - der Absender hatte keine Moeglichkeit zu erkennen, dass die
        # Nachricht nie irgendjemand lesen wird.
        is_registered = await self.db.find_mc_contact_by_name(to_call) is not None
        msg = Message(
            id=None,
            msg_type="P",
            to_call=to_call,
            from_call=from_call.upper(),
            subject=subject,
            body=body,
            created_at=now_utc(),
            thread_id=thread_id,
        )
        msg_id = await self.db.save_message(msg)
        if not is_registered:
            return [f"Msg #{msg_id} gespeichert. ACHTUNG: {to_call} ist nicht registriert - "
                    f"die Nachricht wird erst sichtbar, falls sich {to_call} spaeter anmeldet."]
        # Inhalt direkt per Push-DM zustellen statt nur eines Hinweises -- der
        # Empfaenger muss nicht extra NL/R<id> senden, um zu lesen. Bleibt bis zum
        # expliziten R<id> als "ungelesen" markiert (Badge/Loesch-Erinnerung
        # unveraendert), ist bei Nichterreichbarkeit best-effort (_fire_notify).
        # Nicht abgewartet: der DM-Versand ueber den Node ist seriell und dauert
        # typischerweise 10-30s (bis zu ~90s) - das darf die Bestaetigung an den
        # Absender nicht blockieren.
        self._fire_notify(
            to_call,
            f"\U0001f4e8 Neue Nachricht #{msg_id} von {from_call.upper()}\n"
            f"Betreff: {subject}\n"
            f"---\n"
            f"{body}\n"
            f"---\n"
            f"RS{msg_id}|Text zum Antworten")
        return [f"Msg #{msg_id} an {to_call} gespeichert, Zustellung per DM angestossen. 73!"]

    async def cmd_reply(self, callsign: str, msg_id: int, body: str) -> list[str]:
        """Antwortet auf eine empfangene private Nachricht, ohne Empfaenger/Betreff
        erneut eintippen zu muessen: Empfaenger = urspruenglicher Absender, Betreff
        = Original-Betreff mit 'Re: '-Praefix (nicht doppelt, falls schon vorhanden).
        Nur fuer private Nachrichten ('P') und nur durch den tatsaechlichen
        Empfaenger nutzbar (bewusst keine Board-Bulletins - dieselbe 'nicht
        gefunden'-Logik wie bei cmd_read, damit fremde Postfaecher nicht per
        RS<n>-Enumeration aufgezaehlt werden koennen)."""
        msg = await self.db.get_message(msg_id)
        if not msg or msg.msg_type != "P" or callsign.upper() != msg.to_call.upper():
            return [f"Nachricht #{msg_id} nicht gefunden."]
        subject = msg.subject if msg.subject.upper().startswith("RE:") else f"Re: {msg.subject}"
        root_id = await self._thread_root_id(msg)
        return await self.cmd_send(callsign, msg.from_call, subject, body, thread_id=root_id)

    async def cmd_bulletin(self, from_call: str, topic: str, body: str,
                           thread_id: Optional[int] = None) -> list[str]:
        msg = Message(
            id=None,
            msg_type="B",
            to_call="ALL",
            from_call=from_call.upper(),
            subject=topic,
            body=body,
            created_at=now_utc(),
            thread_id=thread_id,
        )
        msg_id = await self.db.save_message(msg)
        return [f"Bulletin #{msg_id} gespeichert. 73!"]

    async def cmd_bulletin_reply(self, from_call: str, msg_id: int, body: str) -> list[str]:
        """Antwortet auf ein Board-Bulletin (Thema mit 'Re: '-Praefix, nicht doppelt
        falls schon vorhanden) - Pendant zu cmd_reply fuer Board-Nachrichten. Die
        Antwort haengt am Thread-Anfang, nicht an der angesprochenen Nachricht: eine
        Antwort auf eine Antwort landet also im selben Thread statt eine zweite Ebene
        aufzumachen (auf einem MeshCore-Display ist ein Baum nicht darstellbar) - und
        das Thema kommt vom Anfang, damit nie 'Re: Re: ...' entsteht.
        Board-Bulletins sind oeffentlich (per BL sichtbar), daher keine Maskierung
        wie bei privaten Nachrichten noetig."""
        msg = await self.db.get_message(msg_id)
        if not msg or msg.msg_type != "B":
            return [f"Bulletin #{msg_id} nicht gefunden."]
        root_id = await self._thread_root_id(msg)
        root = msg if root_id == msg.id else (await self.db.get_message(root_id)) or msg
        topic = root.subject if root.subject.upper().startswith("RE:") else f"Re: {root.subject}"
        result = await self.cmd_bulletin(from_call, topic, body, thread_id=root_id)
        # Push-DM an ALLE bisherigen Thread-Teilnehmer (nicht nur den Autor des
        # Thread-Anfangs), damit niemand eine Antwort verpasst, der schon mitdiskutiert
        # hat - bewusster Kompromiss: erhoeht die Sendelast bei Threads mit vielen
        # Teilnehmern (eine Antwort -> N DMs statt 1), explizit so gewuenscht. Nicht
        # abgewartet (_fire_notify): der Node-Send ist seriell, mehrere Benachrichtigungen
        # nacheinander abzuwarten wuerde die Befehlsantwort minutenlang blockieren.
        replier = from_call.upper()
        thread = await self.db.get_thread(root_id)
        participants = {m.from_call.upper() for m in thread} - {replier}
        for participant in participants:
            self._fire_notify(
                participant,
                f"\U0001f4cb Neue Antwort von {replier} im Thread #{root.id}\n"
                f"Betreff: {root.subject}\n"
                f"---\n"
                f"{body}")
        return result

    def _is_sysop(self, callsign: str) -> bool:
        """True, wenn callsign der primaere SysOp oder einer der konfigurierten
        Co-SysOps ist (config.sysop / config.co_sysops, live aus der Config -
        Web-Admin: Einstellungen)."""
        caller = callsign.upper()
        if caller == str(self.config.get("sysop", "")).upper():
            return True
        co_sysops = self.config.get("co_sysops") or []
        return caller in {str(c).upper() for c in co_sysops}

    async def can_kill(self, callsign: str, msg_id: int) -> tuple[bool, list[str]]:
        """Prueft, ob callsign Nachricht msg_id loeschen darf, OHNE zu loeschen.
        Rueckgabe: (True, []) wenn erlaubt, sonst (False, <Fehlerzeilen>) -- exakt
        dieselbe Maskierung wie bisher in cmd_kill (private Nachricht fuer Dritte =
        "nicht gefunden", damit keine Enumeration moeglich ist). Getrennt von
        cmd_kill, damit der MeshCore-Server vor einer Loesch-Rueckfrage pruefen kann,
        ob ueberhaupt etwas Echtes geloescht wuerde, ohne die Maskierung zu umgehen."""
        msg = await self.db.get_message(msg_id)
        if not msg:
            return False, [f"Nachricht #{msg_id} nicht gefunden."]
        caller = callsign.upper()
        is_sysop = self._is_sysop(caller)
        if msg.msg_type == "P":
            # Wer die private Nachricht nicht einmal lesen darf (weder Empfaenger noch
            # Absender), bekommt dieselbe "nicht gefunden"-Antwort wie in cmd_read --
            # sonst liesse sich per K<n>/ND<n> aufzaehlen, welche fremden Postfach-IDs
            # existieren (Enumerierung). Loeschen darf danach nur der Empfaenger (sein
            # Postfach); der Absender darf die Nachricht zwar lesen, aber nicht loeschen.
            if caller not in (msg.to_call.upper(), msg.from_call.upper()) and not is_sysop:
                return False, [f"Nachricht #{msg_id} nicht gefunden."]
            if caller != msg.to_call.upper() and not is_sysop:
                return False, ["Keine Berechtigung."]
        else:
            # Board-Bulletin: Existenz ist oeffentlich (per BL sichtbar), daher kein
            # Masking noetig. Loeschen darf nur der Autor oder ein SysOp.
            if caller != msg.from_call.upper() and not is_sysop:
                return False, ["Keine Berechtigung."]
        return True, []

    async def cmd_kill(self, callsign: str, msg_id: int) -> list[str]:
        ok, err = await self.can_kill(callsign, msg_id)
        if not ok:
            return err
        await self.db.delete_message(msg_id)
        return [f"Nachricht #{msg_id} geloescht."]

    async def cmd_list_users(self) -> list[str]:
        contacts = await self.db.list_mc_contacts()
        if not contacts:
            return ["\U0001f465 Keine User registriert."]
        lines = [f"\U0001f465 User ({len(contacts)}):"]
        for name, added_at in contacts:
            try:
                dt = datetime.fromisoformat(added_at)
                lines.append(f"{name}  {dt.strftime('%d.%m.%y')}")
            except (ValueError, TypeError):
                lines.append(name)
        return lines

    async def cmd_who(self, active_calls: list[str]) -> list[str]:
        if not active_calls:
            return ["Niemand online."]
        return [f"{len(active_calls)} online: " + ", ".join(active_calls)]

    async def cmd_lotto(self) -> list[str]:
        zahlen = sorted(random.sample(range(1, 50), 6))
        heute = now_utc()
        naechster_samstag = heute + timedelta(days=(5 - heute.weekday()) % 7)
        return [f"Vorschlag Lottozahlen fuer den {naechster_samstag.strftime('%d.%m.%y')}:\n\n"
                + " - ".join(str(z) for z in zahlen)
                + "\n\nViel Glueck \U0001F340"]

    def _ha_settings(self) -> tuple[str, str, str, object]:
        """(url, token, qth, verify_ssl) aus der Config. verify_ssl: True (Default,
        Zertifikat pruefen), False (Alt-Setup, ungeprueft) oder CA-Pfad-String."""
        ha = self.config.get("homeassistant", {})
        return (ha.get("url", ""), ha.get("token", ""),
                self.config.get("qth", "QTH"), ha.get("verify_ssl", True))

    async def cmd_weather(self) -> list[str]:
        # "Home Assistant" ist ein internes Implementierungsdetail (Backend fuer die
        # Wetterdaten) -- fuer einen regulaeren Mesh-User ohne Bedeutung, daher in
        # den nutzersichtbaren Texten generisch "Wetterdienst" statt "Home Assistant".
        url, token, qth, verify = self._ha_settings()
        if not url or not token:
            return ["WX: Wetterdienst nicht konfiguriert"]
        return await fetch_weather(url, token, qth, verify_ssl=verify)

    async def cmd_forecast_1day(self) -> list[str]:
        url, token, qth, verify = self._ha_settings()
        if not url or not token:
            return ["WX1: Wetterdienst nicht konfiguriert"]
        return await fetch_forecast_1day(url, token, qth, verify_ssl=verify)

    async def cmd_forecast_3days(self) -> list[str]:
        url, token, qth, verify = self._ha_settings()
        if not url or not token:
            return ["WX3: Wetterdienst nicht konfiguriert"]
        return await fetch_forecast_3days(url, token, qth, verify_ssl=verify)

    async def cmd_my_info(self, callsign: str) -> list[str]:
        details = await self.db.get_mc_contact_info(callsign)
        msgs = await self.db.get_messages(callsign)
        sent = sum(1 for m in msgs if m.from_call == callsign.upper() and m.msg_type == "P")
        recv = sum(1 for m in msgs if m.to_call == callsign.upper() and m.msg_type == "P")
        lines = [f"\U0001f464 {callsign.upper()}"]
        if details:
            _, added_at, mail = details
            try:
                dt = datetime.fromisoformat(added_at)
                lines.append(f"Seit: {dt.strftime('%d.%m.%y')}")
            except (ValueError, TypeError):
                pass
            lines.append(f"Mail: {mail if mail else 'MC deine@mail.de'}")
        lines.append(f"Msgs: {sent} gesendet / {recv} empfangen")
        lines.append(f"Postfach: {recv}/{self.max_personal_messages}")
        return ["\n".join(lines)]

    async def cmd_set_mail(self, callsign: str, mail: str) -> list[str]:
        # Steuerzeichen entfernen und Format pruefen – Wert stammt aus dem Mesh.
        mail = sanitize.for_log(mail).strip()
        if not is_valid_email(mail):
            return ["Ungueltige Mailadresse. Format: MAIL name@domain.de (oder MC name@domain.de)"]
        await self.db.set_mc_contact_mail(callsign, mail)
        return [f"Mailkontakt gespeichert:\n{mail}"]
