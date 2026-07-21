import logging
import aiosqlite
from datetime import datetime
from typing import List, Optional

from core import crypto
from core.message import Message

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, path: str, messages_key: Optional[bytes] = None):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None
        self._messages_key = messages_key   # AES-256-Key fuer 'P'-Nachrichten, siehe core/crypto.py

    async def connect(self):
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()
        if self._messages_key:
            await self._encrypt_legacy_private_messages()

    async def close(self):
        if self._db:
            await self._db.close()

    async def _create_tables(self):
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS mc_contacts (
                pubkey   TEXT PRIMARY KEY,
                name     TEXT UNIQUE NOT NULL,
                added_at TEXT NOT NULL,
                mail     TEXT DEFAULT ''
            )
        """)
        # migration for existing DBs without mail column
        try:
            await self._db.execute("ALTER TABLE mc_contacts ADD COLUMN mail TEXT DEFAULT ''")
            await self._db.commit()
            logger.debug("Schema-Migration: mail-Spalte hinzugefuegt")
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logger.error("Schema-Migration fehlgeschlagen: %s", exc, exc_info=True)
        # migration for existing DBs without pubkey_ack_confirmed column (Pubkey-
        # Sicherheitshinweis bestaetigt? Default 0 auch fuer Bestandsuser, siehe
        # PUBKEY_ACK_TIMEOUT / _pubkey_ack_gate in protocols/meshcore/server.py)
        try:
            await self._db.execute(
                "ALTER TABLE mc_contacts ADD COLUMN pubkey_ack_confirmed INTEGER DEFAULT 0")
            await self._db.commit()
            logger.debug("Schema-Migration: pubkey_ack_confirmed-Spalte hinzugefuegt")
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logger.error("Schema-Migration fehlgeschlagen: %s", exc, exc_info=True)
        # migration for existing DBs without send_locked column (dauerhafte Sperre
        # des Senderechts durch den SysOp/Web-Admin, unabhaengig von/staerker als
        # pubkey_ack_confirmed - siehe _pubkey_ack_gate in protocols/meshcore/server.py)
        try:
            await self._db.execute(
                "ALTER TABLE mc_contacts ADD COLUMN send_locked INTEGER DEFAULT 0")
            await self._db.commit()
            logger.debug("Schema-Migration: send_locked-Spalte hinzugefuegt")
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logger.error("Schema-Migration fehlgeschlagen: %s", exc, exc_info=True)
        # migration for existing DBs without last_active column (letzte Interaktion,
        # Basis fuer die Inaktivitaets-Bereinigung - siehe touch_last_active/
        # purge_inactive_contacts). NULL = seit Registrierung keine Aktivitaet,
        # dann gilt added_at als Basis (siehe COALESCE in den Abfragen unten).
        try:
            await self._db.execute(
                "ALTER TABLE mc_contacts ADD COLUMN last_active TEXT DEFAULT NULL")
            await self._db.commit()
            logger.debug("Schema-Migration: last_active-Spalte hinzugefuegt")
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logger.error("Schema-Migration fehlgeschlagen: %s", exc, exc_info=True)
        # migration for existing DBs without inactivity_warned_days column (comma-Liste
        # bereits verschickter Inaktivitaets-Warnschwellen, z.B. "50,55" - wird bei
        # erneuter Aktivitaet zurueckgesetzt, siehe touch_last_active)
        try:
            await self._db.execute(
                "ALTER TABLE mc_contacts ADD COLUMN inactivity_warned_days TEXT DEFAULT ''")
            await self._db.commit()
            logger.debug("Schema-Migration: inactivity_warned_days-Spalte hinzugefuegt")
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logger.error("Schema-Migration fehlgeschlagen: %s", exc, exc_info=True)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_type    TEXT NOT NULL,
                to_call     TEXT NOT NULL,
                from_call   TEXT NOT NULL,
                subject     TEXT NOT NULL,
                body        TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                read        INTEGER DEFAULT 0,
                bid         TEXT,
                sticky      INTEGER DEFAULT 0,
                views       INTEGER DEFAULT 0
            )
        """)
        # migration for existing DBs without sticky column
        try:
            await self._db.execute("ALTER TABLE messages ADD COLUMN sticky INTEGER DEFAULT 0")
            await self._db.commit()
            logger.debug("Schema-Migration: sticky-Spalte zu messages hinzugefuegt")
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logger.error("Schema-Migration fehlgeschlagen: %s", exc, exc_info=True)
        # migration for existing DBs without views column (Aufrufzaehler)
        try:
            await self._db.execute("ALTER TABLE messages ADD COLUMN views INTEGER DEFAULT 0")
            await self._db.commit()
            logger.debug("Schema-Migration: views-Spalte zu messages hinzugefuegt")
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logger.error("Schema-Migration fehlgeschlagen: %s", exc, exc_info=True)
        # migration for existing DBs without warned column (Loesch-Erinnerung gesendet?)
        try:
            await self._db.execute("ALTER TABLE messages ADD COLUMN warned INTEGER DEFAULT 0")
            await self._db.commit()
            logger.debug("Schema-Migration: warned-Spalte zu messages hinzugefuegt")
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logger.error("Schema-Migration fehlgeschlagen: %s", exc, exc_info=True)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL,
                type     TEXT NOT NULL,
                callsign TEXT DEFAULT '',
                detail   TEXT DEFAULT ''
            )
        """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        # migration for existing DBs without snr column (Link-Qualitaet)
        try:
            await self._db.execute("ALTER TABLE events ADD COLUMN snr REAL")
            await self._db.commit()
            logger.debug("Schema-Migration: snr-Spalte zu events hinzugefuegt")
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logger.error("Schema-Migration fehlgeschlagen: %s", exc, exc_info=True)
        # migration for existing DBs without route column (flood/direct/multihop)
        try:
            await self._db.execute("ALTER TABLE events ADD COLUMN route TEXT")
            await self._db.commit()
            logger.debug("Schema-Migration: route-Spalte zu events hinzugefuegt")
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logger.error("Schema-Migration fehlgeschlagen: %s", exc, exc_info=True)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS blocked (
                pubkey     TEXT PRIMARY KEY,
                name       TEXT DEFAULT '',
                reason     TEXT DEFAULT '',
                blocked_at TEXT NOT NULL
            )
        """)
        # Ausstehende Registrierungen im Modus "sysop_approval" (registration.mode,
        # siehe protocols/meshcore/server.py) - persistent, da eine Freischaltung
        # durch den SysOp beliebig lange dauern kann (anders als der kurzlebige
        # RAM-Bestaetigungscode im Modus "challenge").
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS pending_registrations (
                prefix_hex   TEXT PRIMARY KEY,
                pubkey       TEXT NOT NULL,
                name         TEXT NOT NULL,
                path         TEXT DEFAULT '',
                requested_at TEXT NOT NULL
            )
        """)
        # Events aelter als 90 Tage aufraeumen
        await self._db.execute(
            "DELETE FROM events WHERE ts < datetime('now', '-90 days')")
        await self._db.commit()

    async def _encrypt_legacy_private_messages(self):
        """Verschluesselt nachtraeglich alle 'P'-Nachrichten, die noch aus der Zeit
        vor der At-Rest-Verschluesselung im Klartext in der DB liegen (einmalig,
        idempotent - bereits verschluesselte Zeilen werden uebersprungen)."""
        cursor = await self._db.execute(
            "SELECT id, subject, body FROM messages WHERE msg_type = 'P'")
        rows = await cursor.fetchall()
        updated = 0
        for row in rows:
            if crypto.is_encrypted(row["subject"]) and crypto.is_encrypted(row["body"]):
                continue
            subject = row["subject"] if crypto.is_encrypted(row["subject"]) \
                else crypto.encrypt(row["subject"], self._messages_key)
            body = row["body"] if crypto.is_encrypted(row["body"]) \
                else crypto.encrypt(row["body"], self._messages_key)
            await self._db.execute(
                "UPDATE messages SET subject = ?, body = ? WHERE id = ?",
                (subject, body, row["id"]))
            updated += 1
        if updated:
            await self._db.commit()
            logger.info("At-Rest-Verschluesselung: %d bestehende private Nachricht(en) nachtraeglich verschluesselt", updated)

    async def save_message(self, msg: Message) -> int:
        subject, body = msg.subject, msg.body
        if msg.msg_type == "P" and self._messages_key:
            subject = crypto.encrypt(subject, self._messages_key)
            body = crypto.encrypt(body, self._messages_key)
        cursor = await self._db.execute(
            """INSERT INTO messages (msg_type, to_call, from_call, subject, body, created_at, bid, sticky)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg.msg_type, msg.to_call.upper(), msg.from_call.upper(),
             subject, body, msg.created_at.isoformat(), msg.bid, int(msg.sticky)),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_messages(self, for_call: Optional[str] = None) -> List[Message]:
        if for_call:
            cursor = await self._db.execute(
                "SELECT * FROM messages WHERE to_call = ? OR msg_type = 'B' ORDER BY id DESC",
                (for_call.upper(),),
            )
        else:
            cursor = await self._db.execute("SELECT * FROM messages ORDER BY id DESC")
        return [self._row_to_message(r) for r in await cursor.fetchall()]

    async def get_message(self, msg_id: int) -> Optional[Message]:
        cursor = await self._db.execute("SELECT * FROM messages WHERE id = ?", (msg_id,))
        row = await cursor.fetchone()
        return self._row_to_message(row) if row else None

    async def count_personal_messages(self, callsign: str) -> int:
        """Anzahl privater Nachrichten im Postfach von callsign (fuer Quota/Anzeige)."""
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM messages WHERE msg_type = 'P' AND to_call = ?",
            (callsign.upper(),))
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def count_unread_personal(self, callsign: str) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM messages WHERE msg_type = 'P' AND to_call = ? AND read = 0",
            (callsign.upper(),))
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def mark_read(self, msg_id: int):
        """Markiert als gelesen und zaehlt den Aufruf (views) – bei Board-Nachrichten
        liest i.d.R. mehr als ein User dieselbe Nachricht, daher Zaehler statt Flag."""
        await self._db.execute(
            "UPDATE messages SET read = 1, views = views + 1 WHERE id = ?", (msg_id,))
        await self._db.commit()

    async def delete_message(self, msg_id: int):
        await self._db.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
        await self._db.commit()

    async def set_sticky(self, msg_id: int, sticky: bool):
        await self._db.execute(
            "UPDATE messages SET sticky = ? WHERE id = ?", (int(sticky), msg_id))
        await self._db.commit()

    async def purge_old_board_messages(self, days: int) -> int:
        """Loescht Board-Nachrichten (msg_type='B') aelter als N Tage, sticky ausgenommen.
        Gibt die Anzahl geloeschter Nachrichten zurueck."""
        cursor = await self._db.execute(
            "DELETE FROM messages WHERE msg_type = 'B' AND sticky = 0 "
            "AND created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        await self._db.commit()
        return cursor.rowcount

    async def get_unwarned_expiring_messages(self, retention_days: int, warn_days: int) -> List[Message]:
        """Ungelesene private Nachrichten ('P'), die die Loesch-Erinnerung noch nicht
        bekommen haben und deren Alter die Warnschwelle (retention_days - warn_days)
        erreicht hat. warned wird von markiere_warned() gesetzt, damit die Erinnerung
        nur einmal pro Nachricht verschickt wird."""
        cursor = await self._db.execute(
            "SELECT * FROM messages WHERE msg_type = 'P' AND read = 0 AND warned = 0 "
            "AND created_at < datetime('now', ?)",
            (f"-{retention_days - warn_days} days",),
        )
        return [self._row_to_message(r) for r in await cursor.fetchall()]

    async def mark_warned(self, msg_id: int):
        await self._db.execute("UPDATE messages SET warned = 1 WHERE id = ?", (msg_id,))
        await self._db.commit()

    async def purge_expired_unread_messages(self, retention_days: int) -> int:
        """Loescht ungelesene private Nachrichten ('P') aelter als retention_days.
        Gibt die Anzahl geloeschter Nachrichten zurueck."""
        cursor = await self._db.execute(
            "DELETE FROM messages WHERE msg_type = 'P' AND read = 0 "
            "AND created_at < datetime('now', ?)",
            (f"-{retention_days} days",),
        )
        await self._db.commit()
        return cursor.rowcount

    async def load_mc_contacts(self) -> list[tuple[str, str]]:
        cursor = await self._db.execute("SELECT pubkey, name FROM mc_contacts")
        return [(r["pubkey"], r["name"]) for r in await cursor.fetchall()]

    async def find_mc_contact_by_name(self, name: str) -> Optional[tuple[str, str]]:
        cursor = await self._db.execute(
            "SELECT pubkey, name FROM mc_contacts WHERE name = ?", (name.upper(),)
        )
        row = await cursor.fetchone()
        return (row["pubkey"], row["name"]) if row else None

    async def find_mc_contact_by_pubkey(self, pubkey_hex: str) -> Optional[tuple[str, str]]:
        cursor = await self._db.execute(
            "SELECT pubkey, name FROM mc_contacts WHERE pubkey = ?", (pubkey_hex.lower(),)
        )
        row = await cursor.fetchone()
        return (row["pubkey"], row["name"]) if row else None

    async def save_mc_contact(self, pubkey_hex: str, name: str):
        await self._db.execute(
            "INSERT INTO mc_contacts (pubkey, name, added_at) VALUES (?, ?, ?)",
            (pubkey_hex.lower(), name.upper(), datetime.utcnow().isoformat()),
        )
        await self._db.commit()

    async def delete_mc_contact(self, pubkey_hex: str):
        await self._db.execute(
            "DELETE FROM mc_contacts WHERE pubkey = ?", (pubkey_hex.lower(),)
        )
        await self._db.commit()

    async def count_mc_contacts(self) -> int:
        cursor = await self._db.execute("SELECT COUNT(*) FROM mc_contacts")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def list_mc_contacts(self) -> list[tuple[str, str]]:
        """Returns (name, added_at) for all contacts, sorted by added_at."""
        cursor = await self._db.execute(
            "SELECT name, added_at FROM mc_contacts ORDER BY added_at"
        )
        return [(r["name"], r["added_at"]) for r in await cursor.fetchall()]

    async def get_mc_contact_info(self, name: str) -> Optional[tuple[str, str, str]]:
        """Returns (pubkey, added_at, mail) or None."""
        cursor = await self._db.execute(
            "SELECT pubkey, added_at, mail FROM mc_contacts WHERE name = ?", (name.upper(),)
        )
        row = await cursor.fetchone()
        return (row["pubkey"], row["added_at"], row["mail"] or "") if row else None

    async def set_mc_contact_mail(self, name: str, mail: str):
        await self._db.execute(
            "UPDATE mc_contacts SET mail = ? WHERE name = ?", (mail.strip(), name.upper())
        )
        await self._db.commit()

    async def is_pubkey_ack_confirmed(self, name: str) -> bool:
        """True, wenn der Pubkey-Sicherheitshinweis fuer diesen User bereits per
        OK-Challenge bestaetigt wurde (siehe _pubkey_ack_gate in server.py)."""
        cursor = await self._db.execute(
            "SELECT pubkey_ack_confirmed FROM mc_contacts WHERE name = ?", (name.upper(),)
        )
        row = await cursor.fetchone()
        return bool(row["pubkey_ack_confirmed"]) if row else False

    async def set_pubkey_ack_confirmed(self, name: str, confirmed: bool = True):
        await self._db.execute(
            "UPDATE mc_contacts SET pubkey_ack_confirmed = ? WHERE name = ?",
            (int(confirmed), name.upper()),
        )
        await self._db.commit()

    async def get_pubkey_ack_status(self, name: str) -> tuple[bool, bool]:
        """Returns (confirmed, send_locked) in einer Abfrage fuer _pubkey_ack_gate.
        send_locked ist eine dauerhafte, nur vom SysOp/Web-Admin setzbare Sperre
        des Senderechts -- staerker als/unabhaengig von pubkey_ack_confirmed."""
        cursor = await self._db.execute(
            "SELECT pubkey_ack_confirmed, send_locked FROM mc_contacts WHERE name = ?",
            (name.upper(),),
        )
        row = await cursor.fetchone()
        if not row:
            return False, False
        return bool(row["pubkey_ack_confirmed"]), bool(row["send_locked"])

    async def set_send_locked(self, name: str, locked: bool = True):
        await self._db.execute(
            "UPDATE mc_contacts SET send_locked = ? WHERE name = ?",
            (int(locked), name.upper()),
        )
        await self._db.commit()

    async def get_all_mc_contacts(self) -> list[dict]:
        """Alle registrierten MeshCore-User mit allen Feldern (fuer Web-Admin)."""
        cursor = await self._db.execute(
            "SELECT pubkey, name, added_at, mail, pubkey_ack_confirmed, send_locked, "
            "COALESCE(last_active, added_at) AS last_active "
            "FROM mc_contacts ORDER BY name"
        )
        return [dict(r) for r in await cursor.fetchall()]

    # --- Inaktivitaets-Bereinigung ------------------------------------------

    async def touch_last_active(self, name: str):
        """Aktualisiert die letzte Aktivitaet (jede angenommene DM zaehlt, siehe
        _handle_message in protocols/meshcore/server.py) und setzt bereits
        verschickte Inaktivitaets-Warnungen zurueck, da der User wieder aktiv ist."""
        await self._db.execute(
            "UPDATE mc_contacts SET last_active = ?, inactivity_warned_days = '' "
            "WHERE name = ?",
            (datetime.utcnow().isoformat(), name.upper()),
        )
        await self._db.commit()

    async def get_unwarned_inactive_contacts(self, warn_day: int) -> list[dict]:
        """User, deren letzte Aktivitaet mindestens warn_day Tage zurueckliegt und
        die fuer GENAU diese Warnschwelle noch keine Erinnerungs-DM bekommen haben
        (Filterung der bereits verschickten Schwellen in Python, da eine
        comma-Liste in SQL nur unhandlich abfragbar waere)."""
        cursor = await self._db.execute(
            "SELECT name, COALESCE(last_active, added_at) AS basis, inactivity_warned_days "
            "FROM mc_contacts WHERE COALESCE(last_active, added_at) < datetime('now', ?)",
            (f"-{warn_day} days",),
        )
        due = []
        for row in await cursor.fetchall():
            warned = {int(d) for d in row["inactivity_warned_days"].split(",") if d}
            if warn_day not in warned:
                due.append(dict(row))
        return due

    async def mark_inactivity_warned(self, name: str, warn_day: int):
        cursor = await self._db.execute(
            "SELECT inactivity_warned_days FROM mc_contacts WHERE name = ?", (name.upper(),)
        )
        row = await cursor.fetchone()
        if not row:
            return
        warned = {int(d) for d in row["inactivity_warned_days"].split(",") if d}
        warned.add(warn_day)
        await self._db.execute(
            "UPDATE mc_contacts SET inactivity_warned_days = ? WHERE name = ?",
            (",".join(str(d) for d in sorted(warned)), name.upper()),
        )
        await self._db.commit()

    async def purge_inactive_contacts(self, days: int) -> list[dict]:
        """Entfernt User, deren letzte Aktivitaet mindestens `days` Tage
        zurueckliegt. Gibt die entfernten {name, pubkey} zurueck (fuer Logging
        und In-Memory-Cleanup am Node, siehe main.py _inactivity_loop)."""
        cursor = await self._db.execute(
            "SELECT name, pubkey FROM mc_contacts "
            "WHERE COALESCE(last_active, added_at) < datetime('now', ?)",
            (f"-{days} days",),
        )
        removed = [dict(r) for r in await cursor.fetchall()]
        if removed:
            await self._db.execute(
                "DELETE FROM mc_contacts WHERE COALESCE(last_active, added_at) < datetime('now', ?)",
                (f"-{days} days",),
            )
            await self._db.commit()
        return removed

    async def purge_user_messages(self, name: str, delete_sent_private: bool = True,
                                  delete_sent_board: bool = True):
        """Loescht bei einer User-Entfernung (Inaktivitaet, Web-Admin Entfernen/
        Sperren, Self-Service REMOVE) die private Post: empfangene private
        Nachrichten immer; gesendete private Nachrichten und eigene
        Board-Bulletins je einzeln nur wenn die jeweilige users.delete_sent_*
        Einstellung (Web-Admin) an ist."""
        name = name.upper()
        await self._db.execute(
            "DELETE FROM messages WHERE msg_type = 'P' AND to_call = ?", (name,))
        if delete_sent_private:
            await self._db.execute(
                "DELETE FROM messages WHERE msg_type = 'P' AND from_call = ?", (name,))
        if delete_sent_board:
            await self._db.execute(
                "DELETE FROM messages WHERE msg_type = 'B' AND from_call = ?", (name,))
        await self._db.commit()

    # --- Ausstehende Freischaltungen (registration.mode=sysop_approval) ----

    async def add_pending_registration(self, prefix_hex: str, pubkey_hex: str,
                                       name: str, path_hex: str = ""):
        await self._db.execute(
            "INSERT OR REPLACE INTO pending_registrations "
            "(prefix_hex, pubkey, name, path, requested_at) VALUES (?, ?, ?, ?, ?)",
            (prefix_hex, pubkey_hex.lower(), name.upper(), path_hex,
             datetime.utcnow().isoformat()),
        )
        await self._db.commit()

    async def get_pending_registrations(self) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT prefix_hex, pubkey, name, path, requested_at "
            "FROM pending_registrations ORDER BY requested_at"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def pop_pending_registration(self, prefix_hex: str) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT prefix_hex, pubkey, name, path, requested_at "
            "FROM pending_registrations WHERE prefix_hex = ?", (prefix_hex,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        await self._db.execute(
            "DELETE FROM pending_registrations WHERE prefix_hex = ?", (prefix_hex,))
        await self._db.commit()
        return dict(row)

    async def find_pending_registration_by_name(self, name: str) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT prefix_hex, pubkey, name, path, requested_at "
            "FROM pending_registrations WHERE name = ?", (name.upper(),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def find_pending_registration_by_pubkey(self, pubkey_hex: str) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT prefix_hex, pubkey, name, path, requested_at "
            "FROM pending_registrations WHERE pubkey = ?", (pubkey_hex.lower(),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # --- Statistik-Events (fuer Web-Admin) --------------------------------

    async def log_event(self, ev_type: str, callsign: str = "", detail: str = "",
                        snr: Optional[float] = None, route: Optional[str] = None):
        """Protokolliert ein Statistik-Event: rx / ack / noack / channel.
        snr wird nur bei rx-Events mitgegeben (Empfangsqualitaet, aus dem Frame).
        route (nur bei rx/ack/noack): 'flood' | 'direct' | 'multihop' – Routing-Art
        der Nachricht (Flood, direkter Nachbar, oder direkt via bekanntem Mehrhop-Pfad)."""
        await self._db.execute(
            "INSERT INTO events (ts, type, callsign, detail, snr, route) VALUES (datetime('now'), ?, ?, ?, ?, ?)",
            (ev_type, callsign.upper(), detail, snr, route),
        )
        await self._db.commit()

    async def get_daily_stats(self, days: int = 14) -> list[dict]:
        """Events je Tag und Typ der letzten N Tage."""
        cursor = await self._db.execute(
            """SELECT substr(ts, 1, 10) AS day, type, COUNT(*) AS n
               FROM events WHERE ts >= datetime('now', ?)
               GROUP BY day, type ORDER BY day DESC""",
            (f"-{days} days",),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_daily_route_stats(self, days: int = 30, ev_type: str = "rx") -> list[dict]:
        """Empfangene Nachrichten je Tag und Routing-Art (flood/direct/multihop)
        der letzten N Tage – fuer den gestapelten Verlaufs-Chart."""
        cursor = await self._db.execute(
            """SELECT substr(ts, 1, 10) AS day, COALESCE(route, 'unbekannt') AS route, COUNT(*) AS n
               FROM events WHERE type = ? AND ts >= datetime('now', ?)
               GROUP BY day, route ORDER BY day""",
            (ev_type, f"-{days} days"),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_user_daily_route_stats(self, callsign: str, days: int = 30,
                                         ev_type: str = "rx") -> list[dict]:
        """Wie get_daily_route_stats, aber auf einen einzelnen User eingeschraenkt
        (fuer die Detailansicht je User)."""
        cursor = await self._db.execute(
            """SELECT substr(ts, 1, 10) AS day, COALESCE(route, 'unbekannt') AS route, COUNT(*) AS n
               FROM events WHERE type = ? AND callsign = ? AND ts >= datetime('now', ?)
               GROUP BY day, route ORDER BY day""",
            (ev_type, callsign.upper(), f"-{days} days"),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_user_route_stats(self, days: int = 14, ev_type: str = "rx") -> list[dict]:
        """Empfangene Nachrichten je User und Routing-Art (flood/direct/multihop)
        der letzten N Tage – fuer die Detailansicht je User."""
        cursor = await self._db.execute(
            """SELECT callsign, COALESCE(route, 'unbekannt') AS route, COUNT(*) AS n
               FROM events WHERE type = ? AND ts >= datetime('now', ?) AND callsign != ''
               GROUP BY callsign, route""",
            (ev_type, f"-{days} days"),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_user_stats(self, days: int = 14) -> list[dict]:
        """Events je User und Typ, plus mittlere ACK-RTT (detail = RTT in ms) und
        SNR-Statistik der rx-Events (Empfangsqualitaet: wie gut kommt der User bei uns an)."""
        cursor = await self._db.execute(
            """SELECT callsign, type, COUNT(*) AS n,
                      AVG(CASE WHEN type = 'ack' AND detail != ''
                               THEN CAST(detail AS INTEGER) END) AS avg_rtt,
                      AVG(CASE WHEN type = 'rx' THEN snr END) AS avg_snr,
                      MIN(CASE WHEN type = 'rx' THEN snr END) AS min_snr,
                      MAX(CASE WHEN type = 'rx' THEN snr END) AS max_snr
               FROM events WHERE ts >= datetime('now', ?) AND callsign != ''
               GROUP BY callsign, type""",
            (f"-{days} days",),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_last_snr(self) -> dict[str, tuple[float, str]]:
        """Letzter bekannter SNR-Wert je User (unabhaengig vom Statistik-Zeitraum),
        fuer eine aktuelle 'wie steht's gerade' Anzeige."""
        cursor = await self._db.execute(
            """SELECT callsign, snr, ts FROM events e1
               WHERE type = 'rx' AND snr IS NOT NULL AND callsign != ''
               AND ts = (SELECT MAX(ts) FROM events e2
                         WHERE e2.callsign = e1.callsign AND e2.type = 'rx' AND e2.snr IS NOT NULL)"""
        )
        return {r["callsign"]: (r["snr"], r["ts"]) for r in await cursor.fetchall()}

    async def get_snr_history(self, days: int = 14, max_per_user: int = 40) -> dict[str, list[float]]:
        """SNR-Werte je User in chronologischer Reihenfolge (fuer Sparkline-Trend),
        auf die neuesten max_per_user Werte begrenzt."""
        cursor = await self._db.execute(
            """SELECT callsign, snr FROM events
               WHERE type = 'rx' AND snr IS NOT NULL AND callsign != ''
               AND ts >= datetime('now', ?)
               ORDER BY ts ASC""",
            (f"-{days} days",),
        )
        history: dict[str, list[float]] = {}
        for r in await cursor.fetchall():
            history.setdefault(r["callsign"], []).append(r["snr"])
        return {call: values[-max_per_user:] for call, values in history.items()}

    # --- Sperrliste --------------------------------------------------------

    async def add_blocked(self, pubkey_hex: str, name: str = "", reason: str = ""):
        await self._db.execute(
            "INSERT OR REPLACE INTO blocked (pubkey, name, reason, blocked_at) "
            "VALUES (?, ?, ?, ?)",
            (pubkey_hex.lower(), name.upper(), reason, datetime.utcnow().isoformat()),
        )
        await self._db.commit()

    async def remove_blocked(self, pubkey_hex: str):
        await self._db.execute("DELETE FROM blocked WHERE pubkey = ?", (pubkey_hex.lower(),))
        await self._db.commit()

    async def get_blocked(self) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT pubkey, name, reason, blocked_at FROM blocked ORDER BY blocked_at DESC")
        return [dict(r) for r in await cursor.fetchall()]

    async def backup_to(self, path: str):
        """Konsistentes Backup der Datenbank via VACUUM INTO."""
        await self._db.execute("VACUUM INTO ?", (path,))

    def _row_to_message(self, row) -> Message:
        subject, body = row["subject"], row["body"]
        if row["msg_type"] == "P" and self._messages_key:
            subject = crypto.decrypt(subject, self._messages_key)
            body = crypto.decrypt(body, self._messages_key)
        return Message(
            id=row["id"],
            msg_type=row["msg_type"],
            to_call=row["to_call"],
            from_call=row["from_call"],
            subject=subject,
            body=body,
            created_at=datetime.fromisoformat(row["created_at"]),
            read=bool(row["read"]),
            bid=row["bid"],
            sticky=bool(row["sticky"]) if row["sticky"] is not None else False,
            views=row["views"] or 0,
        )
