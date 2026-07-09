import logging
import re
from datetime import datetime
from typing import Awaitable, Callable, Optional

from core.message import Message
from core.weather import fetch_forecast_1day, fetch_forecast_3days, fetch_weather
from core import sanitize
from storage.database import Database

logger = logging.getLogger(__name__)

# Signatur: async def notify_dm(to_call: str, text: str) -> bool
# Versucht, dem angegebenen Rufzeichen per MeshCore-DM zuzustellen. Gibt False
# zurueck, wenn der Name nicht als MeshCore-Kontakt registriert ist (z.B. Tippfehler
# oder noch nicht registrierter Empfaenger) - kein Fehler, nur "konnte nicht benachrichtigen".
NotifyDM = Callable[[str, str], Awaitable[bool]]

# Einfache Mail-Validierung fuer den MC-Befehl (aus dem Mesh, angreiferkontrolliert):
# ein @, kein Whitespace/Steuerzeichen, plausible Laenge. Kein RFC-5322-Anspruch.
_MAIL_RE = re.compile(r'^[^\s@]{1,40}@[^\s@]{1,40}\.[^\s@]{2,20}$')

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
    DEFAULT_MAX_PERSONAL_MESSAGES = 30   # Fallback falls messages.max_personal nicht konfiguriert ist

    def __init__(self, db: Database, config: dict, notify_dm: Optional[NotifyDM] = None):
        self.db = db
        self.config = config
        # Optionaler Callback fuer proaktive DM-Benachrichtigungen (neue Nachricht,
        # Loesch-Erinnerung). None, wenn kein Protokoll mit Push-Faehigkeit (MeshCore)
        # verfuegbar ist - Feature bleibt dann einfach inaktiv, kein Fehler.
        self._notify_dm = notify_dm

    async def _try_notify(self, to_call: str, text: str):
        """Best-effort-Benachrichtigung: Fehler/fehlender Callback duerfen den
        eigentlichen BBS-Vorgang (Nachricht speichern etc.) nie verhindern."""
        if not self._notify_dm:
            return
        try:
            await self._notify_dm(to_call, text)
        except Exception:
            logger.warning("Benachrichtigung an %s fehlgeschlagen", to_call, exc_info=True)

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
    # Menüs (jedes als einzelner String ≤ 150 Byte UTF-8, Firmware-Paketlimit)
    # Nur aktivierte Funktionen werden angezeigt.
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
            lines.append(f"\U0001f4e8 N  Nachrichten{badge}")
        if self.feature_enabled("board"):
            lines.append("\U0001f4cb B  Board")
        if self.feature_enabled("weather"):
            lines.append("⛅ W  Wetter")
        if self._any_info_feature():
            lines.append("ℹ  I  Info")
        if self.feature_enabled("account"):
            lines.append("\U0001f464 A  Account")
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
            "\U0001f4cb NL Liste  \U0001f4d6 R<n> Lesen",
            "✉ S TO|Betr|Text",
            "\U0001f5d1 K<n> Kill  \U0001f4e1 H Main",
        ])]

    async def menu_board(self) -> list[str]:
        return ["\n".join([
            "\U0001f4cb Board",
            "\U0001f4cb BL Liste  \U0001f4d6 R<n> Lesen",
            "\U0001f4dd SB Thema|Text",
            "\U0001f4e1 H  BBS-Main",
        ])]

    async def menu_weather(self) -> list[str]:
        return ["\n".join([
            "⛅ Wetter",
            "⛅ WX   Aktuell",
            "\U0001f324 WX1  Morgen",
            "\U0001f4c5 WX3  3 Tage",
            "\U0001f4e1 H    BBS-Main",
        ])]

    async def menu_info(self) -> list[str]:
        lines = ["ℹ Info"]
        if self.feature_enabled("sysinfo"):
            lines.append("ℹ SI  Sysinfo")
        if self.feature_enabled("online"):
            lines.append("\U0001f465 O  Online")
        if self.feature_enabled("userlist"):
            lines.append("\U0001f465 LU  Liste User")
        if self.feature_enabled("ping"):
            lines.append("\U0001f4e1 PING  Repeaterliste")
            lines.append("\U0001f4e1 PING <Name>  Node-Ping")
        lines.append("\U0001f4e1 H  BBS-Main")
        return ["\n".join(lines)]

    async def menu_account(self) -> list[str]:
        return ["\n".join([
            "\U0001f464 Account",
            "\U0001f464 MI  Meine Info",
            "\U0001f4e7 MC  Mailkontakt",
            "\U0001f6aa REMOVE  Abmelden",
            "\U0001f4e1 H  BBS-Main",
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
        return ["BL = Board Liste, NL = Nachrichten Liste"]

    async def cmd_list_board(self, offset: Optional[int] = None) -> list[str]:
        msgs = await self.db.get_messages()
        board_msgs = [m for m in msgs if m.msg_type == "B"]
        sticky_msgs = sorted((m for m in board_msgs if m.sticky), key=lambda m: -m.id)
        other_msgs = sorted((m for m in board_msgs if not m.sticky), key=lambda m: -m.id)

        if offset is None:
            page = sticky_msgs + other_msgs[:self.FIRST_PAGE]
        else:
            # offset ist 1-basiert (BLO 10 -> Nachricht Nr. 10 der Liste, lueckenlos
            # anschliessend an die "juengsten 9" der ersten Seite).
            start = max(offset - 1, 0)
            page = other_msgs[start:start + self.PAGE_SIZE]

        if not page:
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
        for m in page:
            pin = "\U0001f4cc" if m.sticky else "\U0001f4c4"   # 📌 sticky / 📄 nicht sticky
            date = m.created_at.strftime("%d.%m.%y")
            subject = m.subject if len(m.subject) <= 15 else m.subject[:15] + "..."
            lines.append(f"{m.id:>3} {pin:<6} {date:<8} {m.from_call:<9} {subject}")
        if offset is None and len(other_msgs) > self.FIRST_PAGE:
            lines.append(f"BLO {self.FIRST_PAGE + 1} fuer weitere ({len(other_msgs)} gesamt)")
        elif offset is not None and len(other_msgs) > start + self.PAGE_SIZE:
            lines.append(f"BLO {offset + self.PAGE_SIZE} fuer weitere ({len(other_msgs)} gesamt)")
        return lines

    async def cmd_list_personal(self, callsign: str, offset: Optional[int] = None) -> list[str]:
        callsign = callsign.upper()
        msgs = await self.db.get_messages(callsign)
        personal_msgs = sorted(
            (m for m in msgs if m.msg_type == "P" and m.to_call == callsign),
            key=lambda m: -m.id)
        total = len(personal_msgs)

        if offset is None:
            page = personal_msgs[:self.FIRST_PAGE]
        else:
            start = max(offset - 1, 0)
            page = personal_msgs[start:start + self.PAGE_SIZE]

        if not page:
            return ["Keine Nachrichten." if offset is None
                    else f"Keine weiteren Nachrichten ab {offset}."]

        lines = [f"✉ Nachrichten {total}/{self.max_personal_messages}" + (f" ab {offset}" if offset else "")]
        lines.append(f"{'Nr':<4} {'Von':<9} {'Datum':<8} Betreff")
        for m in page:
            mark = "*" if not m.read else " "
            date = m.created_at.strftime("%d.%m.%y")
            lines.append(f"{m.id:>3}{mark} {m.from_call:<9} {date:<8} {m.subject[:15]}")
        if offset is None and total > self.FIRST_PAGE:
            lines.append(f"NLO {self.FIRST_PAGE + 1} fuer weitere ({total} gesamt)")
        elif offset is not None and total > start + self.PAGE_SIZE:
            lines.append(f"NLO {offset + self.PAGE_SIZE} fuer weitere ({total} gesamt)")
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
        if msg.msg_type == "P":
            caller = callsign.upper()
            if caller not in (msg.to_call.upper(), msg.from_call.upper()):
                return [f"Nachricht #{msg_id} nicht gefunden."]
        await self.db.mark_read(msg_id)
        return [
            f"#{msg.id} [{msg.msg_type}] {msg.created_at.strftime('%d.%m.%y %H:%M')} UTC",
            f"Von: {msg.from_call}  An: {msg.to_call}",
            f"Betreff: {msg.subject}",
            "---",
            msg.body,
            "---",
        ]

    async def cmd_send(self, from_call: str, to_call: str, subject: str, body: str) -> list[str]:
        to_call = to_call.upper()
        count = await self.db.count_personal_messages(to_call)
        if count >= self.max_personal_messages:
            return [f"Postfach von {to_call} ist voll ({count}/{self.max_personal_messages}). "
                    f"Nicht gesendet."]
        msg = Message(
            id=None,
            msg_type="P",
            to_call=to_call,
            from_call=from_call.upper(),
            subject=subject,
            body=body,
            created_at=datetime.utcnow(),
        )
        msg_id = await self.db.save_message(msg)
        await self._try_notify(
            to_call,
            f"\U0001f4e8 Neue Nachricht #{msg_id} von {from_call.upper()}: {subject}\n"
            f"NL zum Anzeigen, R{msg_id} zum Lesen")
        return [f"Msg #{msg_id} an {to_call} gespeichert. 73!"]

    async def cmd_bulletin(self, from_call: str, topic: str, body: str) -> list[str]:
        msg = Message(
            id=None,
            msg_type="B",
            to_call="ALL",
            from_call=from_call.upper(),
            subject=topic,
            body=body,
            created_at=datetime.utcnow(),
        )
        msg_id = await self.db.save_message(msg)
        return [f"Bulletin #{msg_id} gespeichert. 73!"]

    async def cmd_kill(self, callsign: str, msg_id: int) -> list[str]:
        msg = await self.db.get_message(msg_id)
        if not msg:
            return [f"Nachricht #{msg_id} nicht gefunden."]
        sysop_call = self.config.get("sysop", "").upper()
        if msg.from_call != callsign.upper() and callsign.upper() != sysop_call:
            return ["Keine Berechtigung."]
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

    def _ha_settings(self) -> tuple[str, str, str, object]:
        """(url, token, qth, verify_ssl) aus der Config. verify_ssl: True (Default,
        Zertifikat pruefen), False (Alt-Setup, ungeprueft) oder CA-Pfad-String."""
        ha = self.config.get("homeassistant", {})
        return (ha.get("url", ""), ha.get("token", ""),
                self.config.get("qth", "QTH"), ha.get("verify_ssl", True))

    async def cmd_weather(self) -> list[str]:
        url, token, qth, verify = self._ha_settings()
        if not url or not token:
            return ["WX: Home Assistant nicht konfiguriert"]
        return await fetch_weather(url, token, qth, verify_ssl=verify)

    async def cmd_forecast_1day(self) -> list[str]:
        url, token, qth, verify = self._ha_settings()
        if not url or not token:
            return ["WX1: Home Assistant nicht konfiguriert"]
        return await fetch_forecast_1day(url, token, qth, verify_ssl=verify)

    async def cmd_forecast_3days(self) -> list[str]:
        url, token, qth, verify = self._ha_settings()
        if not url or not token:
            return ["WX3: Home Assistant nicht konfiguriert"]
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
        if not _MAIL_RE.match(mail):
            return ["Ungueltige Mailadresse. Format: MC name@domain.de"]
        await self.db.set_mc_contact_mail(callsign, mail)
        return [f"Mailkontakt gespeichert:\n{mail}"]
