import asyncio
import logging
import os
import signal
import sys

import yaml

from core import crypto
from storage.database import Database
from protocols.meshcore.server import MeshCoreServer
from protocols.web.server import WebAdminServer, WEBCONFIG_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("nnp-bbs")


def _deep_merge(base: dict, overrides: dict) -> None:
    for key, val in overrides.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val


def _load_messages_key(config: dict):
    """Laedt den At-Rest-Schluessel (Base64, 32 Byte) aus der ersten verfuegbaren
    Quelle. Rueckgabe: (key_bytes|None, quelle_str|None).

    Reihenfolge – bevorzugt die maschinen-gebundene, nicht kopierbare Quelle:
      1. systemd-Credential: $CREDENTIALS_DIRECTORY/messages_key. Wird von
         LoadCredentialEncrypted= beim Service-Start automatisch (ohne Handgriff)
         nach tmpfs entschluesselt; der Klartext-Key liegt nie auf der Platte.
      2. storage.messages_key_file: Pfad zu einer root-only Key-Datei ausserhalb
         von Repo/Backup (Fallback ohne systemd).
      3. storage.messages_key: Inline-Base64 in secrets.yaml (Legacy, world-readable
         wenn Dateirechte offen – nur noch aus Kompatibilitaetsgruenden).
    """
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir:
        cred_path = os.path.join(cred_dir, "messages_key")
        if os.path.exists(cred_path):
            with open(cred_path, "r", encoding="utf-8") as f:
                return crypto.load_key(f.read().strip()), "systemd-credential"

    storage = config.get("storage", {}) or {}
    key_file = storage.get("messages_key_file")
    if key_file and os.path.exists(key_file):
        with open(key_file, "r", encoding="utf-8") as f:
            return crypto.load_key(f.read().strip()), key_file

    raw_key = storage.get("messages_key")
    if raw_key:
        return crypto.load_key(raw_key), "secrets.yaml (inline)"

    return None, None


def _validate_config(config: dict) -> None:
    for key in ("callsign",):
        if key not in config:
            raise ValueError(f"Erforderlicher Config-Key fehlt: '{key}'")


async def _board_purge_loop(db: Database, config: dict):
    """Loescht periodisch abgelaufene (nicht-sticky) Board-Nachrichten.
    Liest die Aufbewahrungsfrist bei jedem Lauf neu aus config, damit
    Aenderungen ueber die Web-Admin-Oberflaeche ohne Neustart wirken."""
    while True:
        days = config.get("board", {}).get("retention_days", 14)
        try:
            deleted = await db.purge_old_board_messages(days)
            if deleted:
                logger.info("Board-Bereinigung: %d Nachricht(en) aelter als %d Tage geloescht",
                            deleted, days)
        except Exception:
            logger.exception("Board-Bereinigung fehlgeschlagen")
        await asyncio.sleep(6 * 3600)


UNREAD_WARN_DAYS = 3   # Vorlauf der Loesch-Erinnerung (fix, nicht konfigurierbar)


async def _unread_message_retention_loop(db: Database, config: dict, notify_dm):
    """Ungelesene private Nachrichten: UNREAD_WARN_DAYS vor Ablauf der
    Aufbewahrungsfrist (messages.unread_retention_days) eine Erinnerungs-DM an
    den Empfaenger senden (einmalig, per warned-Flag), danach bei Ablauf loeschen.
    Liest die Frist bei jedem Lauf neu aus config (Web-Admin, ohne Neustart)."""
    while True:
        retention_days = config.get("messages", {}).get("unread_retention_days", 30)
        try:
            expiring = await db.get_unwarned_expiring_messages(retention_days, UNREAD_WARN_DAYS)
            for msg in expiring:
                if notify_dm:
                    text = (f"⏰ Nachricht #{msg.id} von {msg.from_call} "
                            f"(\"{msg.subject}\") wird in {UNREAD_WARN_DAYS} Tagen geloescht, "
                            f"falls nicht gelesen. R{msg.id} zum Lesen.")
                    try:
                        await notify_dm(msg.to_call, text)
                    except Exception:
                        logger.warning("Loesch-Erinnerung an %s fehlgeschlagen", msg.to_call,
                                       exc_info=True)
                await db.mark_warned(msg.id)
            if expiring:
                logger.info("Loesch-Erinnerung an %d ungelesene Nachricht(en) verschickt",
                            len(expiring))

            deleted = await db.purge_expired_unread_messages(retention_days)
            if deleted:
                logger.info("Ungelesene Nachrichten bereinigt: %d aelter als %d Tage geloescht",
                            deleted, retention_days)
        except Exception:
            logger.exception("Ungelesene-Nachrichten-Bereinigung fehlgeschlagen")
        await asyncio.sleep(6 * 3600)


async def main():
    with open("config/config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Betreiberdaten (Rufzeichen, QTH, HA-URL, MeshCore-Kanal/-Kontakte, ...).
    # Gitignored – config.yaml bleibt dadurch generisch/oeffentlich (GitHub),
    # siehe config/config.local.yaml.example.
    local_path = "config/config.local.yaml"
    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            local_config = yaml.safe_load(f) or {}
        _deep_merge(config, local_config)
        logger.info("Betreiberdaten geladen: %s", local_path)
    else:
        logger.warning(
            "Keine %s gefunden – BBS laeuft mit generischen Defaults aus config.yaml. "
            "Siehe config/config.local.yaml.example.", local_path)

    # Overlay der Web-Admin-Oberflaeche (Einstellungen aus der UI)
    if os.path.exists(WEBCONFIG_PATH):
        with open(WEBCONFIG_PATH, "r", encoding="utf-8") as f:
            webconfig = yaml.safe_load(f) or {}
        _deep_merge(config, webconfig)
        logger.info("Web-Overrides geladen: %s", WEBCONFIG_PATH)

    secrets_path = "config/secrets.yaml"
    if os.path.exists(secrets_path):
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = yaml.safe_load(f) or {}
        _deep_merge(config, secrets)
        logger.info("Secrets geladen: %s", secrets_path)
    else:
        logger.warning("Keine secrets.yaml gefunden – HA-Token fehlt moeglicherweise")

    _validate_config(config)

    messages_key, key_source = _load_messages_key(config)
    if messages_key:
        logger.info("At-Rest-Verschluesselung fuer private Nachrichten aktiv "
                    "(AES-256-GCM, Schluessel aus: %s)", key_source)
    else:
        logger.warning(
            "ACHTUNG: Kein At-Rest-Schluessel gefunden (systemd-Credential, "
            "storage.messages_key_file oder storage.messages_key) – private "
            "Nachrichten (msg_type='P') werden UNVERSCHLUESSELT in der DB "
            "gespeichert. Schluessel erzeugen: core/crypto.py generate_key()")

    db_path = config.get("storage", {}).get("path", "data/bbs.db")
    db = Database(db_path, messages_key=messages_key)
    try:
        await db.connect()
        logger.info("Datenbank verbunden: %s", db_path)

        servers = []

        meshcore = None
        if config.get("meshcore", {}).get("enabled", False):
            meshcore = MeshCoreServer(db, config)
            await meshcore.start()
            servers.append(meshcore)

        # notify_dm fuer proaktive DM-Benachrichtigungen (neue Nachricht /
        # Loesch-Erinnerung), siehe _unread_message_retention_loop unten.
        notify_dm = meshcore.notify_dm if meshcore else None

        if config.get("web", {}).get("enabled", False):
            web_admin = WebAdminServer(db, config, meshcore=meshcore)
            await web_admin.start()
            servers.append(web_admin)

        purge_task = asyncio.create_task(_board_purge_loop(db, config))
        unread_retention_task = asyncio.create_task(
            _unread_message_retention_loop(db, config, notify_dm))

        logger.info("Meshcore BBSng gestartet. CTRL-C zum Beenden.")

        stop_event = asyncio.Event()

        def _shutdown():
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _shutdown)
            except NotImplementedError:
                # Windows unterstuetzt add_signal_handler nicht vollstaendig
                pass

        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass
        finally:
            logger.info("Fahre herunter...")
            purge_task.cancel()
            unread_retention_task.cancel()
            for task in (purge_task, unread_retention_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for server in servers:
                await server.stop()
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
