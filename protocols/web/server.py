"""Web-Admin-Oberflaeche fuer Meshcore BBSng.

Stellt ein Management-Interface auf Basis von aiohttp bereit:
Dashboard, Einstellungen (mit Live-Apply fuer TX-Power/Path-Hash),
Benutzerverwaltung, Nachrichten und eine Debug-Seite mit Journal-Log.

Einstellungen werden als Overlay nach config/webconfig.yaml geschrieben
(config.yaml bleibt unangetastet, Kommentare bleiben erhalten) und beim
naechsten Start von main.py ueber die Basis-Config gemergt.
"""

import asyncio
import hmac
import html
import logging
import os
import re
import secrets as pysecrets
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import yaml
from aiohttp import web

from core import crypto, webtls
from core.bbs import FEATURES
from core.message import Message
from core.timeutil import now_utc
from core.validation import USERNAME_RE, is_valid_email
from protocols.base import BaseProtocol
from storage.database import Database

logger = logging.getLogger(__name__)

WEBCONFIG_PATH = "config/webconfig.yaml"
SESSION_COOKIE = "nnpbbs_session"
SESSION_TTL = 12 * 3600  # 12h
ADMIN_USERNAME_RE = re.compile(r'^[A-Za-z0-9_.-]{3,32}$')  # weitere Admin-Konten
MIN_PASSWORD_LEN = 12  # Mindestlaenge fuer alle Web-Admin-Passwoerter (eigenes + neue Konten)

# Login-Brute-Force-Schutz: max. Fehlversuche je IP innerhalb des Fensters,
# danach Sperre. Ergaenzt die 1s-Verzoegerung (die allein parallele Versuche
# nicht bremst).
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW    = 300   # Sekunden – Zeitfenster fuer die Fehlerzaehlung
LOGIN_LOCKOUT   = 300   # Sekunden – Sperrdauer nach Ueberschreiten
LOGIN_FAILS_MAX = 2048  # Obergrenze des Rate-Limit-Caches (Schutz vor Dict-Aufblaehung)
INITIAL_PW_FILE = "initial-web-password.txt"

# Editierbare Einstellungen: (form-key, config-pfad, label, typ, min, max, live)
# live=True → wird sofort am Node angewendet, sonst erst nach Neustart wirksam.
SETTINGS_SPEC = [
    ("callsign",       ("callsign",),                      "BBS-Rufzeichen",          str,   None, None, False),
    ("sysop",          ("sysop",),                         "SysOp",                   str,   None, None, False),
    ("sysop_mail",     ("sysop_mail",),                    "SysOp-Mail",              str,   None, None, False),
    ("qth",            ("qth",),                           "QTH",                     str,   None, None, False),
    ("locator",        ("locator",),                       "Locator",                 str,   None, None, False),
    ("tx_power",       ("meshcore", "tx_power"),           "TX-Power (dBm)",          int,   -9,   22,   True),
    ("path_hash_mode", ("meshcore", "path_hash_mode"),     "Path-Hash-Mode (0-2)",    int,   0,    2,    True),
    ("channel_name",   ("meshcore", "channel_name"),       "Kanalname",               str,   None, None, False),
    ("channel_region", ("meshcore", "channel_region"),     "Region-Scope (Floods)",   str,   None, None, True),
    ("max_msg_len",    ("meshcore", "max_message_length"), "Max. Zeichen/Paket",      int,   50,   150,  False),
    ("chunk_delay",    ("meshcore", "chunk_delay"),        "Chunk-Pause (s)",         float, 0.5,  10.0, False),
    ("max_chunks",     ("meshcore", "max_chunks"),         "Max. Pakete/Antwort",     int,   1,    10,   False),
    ("retention_days", ("board", "retention_days"),        "Board: Aufbewahrung (Tage)", int, 1,    365,  False),
    ("max_personal",   ("messages", "max_personal"),       "Nachrichten: Max. privates Postfach", int, 1, 500, True),
    ("unread_retention_days", ("messages", "unread_retention_days"),
                                                            "Nachrichten: Loeschfrist ungelesen (Tage)", int, 4, 365, True),
    ("inactivity_days", ("users", "inactivity_days"),
                                                            "Inaktivitaet: Entfernung nach (Tage)", int, 14, 3650, True),
]

REGISTRATION_MODES = [
    ("challenge", "Bestaetigungscode per DM (Status quo)"),
    ("open", "Offen (sofort aktiv, keine Pruefung)"),
    ("sysop_approval", "Freischaltung durch SysOp (im Web-Admin)"),
]

ADV_TYPES = {1: "Client", 2: "Repeater", 3: "Room", 4: "Sensor"}

CSS = """
:root { --bg:#12161c; --panel:#1a2029; --line:#2a3341; --fg:#d6dde6; --dim:#8494a8;
        --acc:#4da3ff; --ok:#3fbf7f; --warn:#e0a83c; --err:#e0574f; }
* { box-sizing:border-box; }
body { margin:0; font:15px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif;
       background:var(--bg); color:var(--fg); }
a { color:var(--acc); text-decoration:none; }
nav { display:flex; gap:4px; align-items:center; padding:10px 16px; flex-wrap:wrap;
      background:var(--panel); border-bottom:1px solid var(--line); }
nav .brand { font-weight:700; margin-right:16px; color:var(--fg); }
nav a { padding:6px 12px; border-radius:6px; color:var(--dim); }
nav a.active, nav a:hover { background:var(--line); color:var(--fg); }
nav .right { margin-left:auto; }
main { max-width:1100px; margin:0 auto; padding:20px 16px 60px; }
h1 { font-size:20px; margin:8px 0 16px; }
h2 { font-size:16px; margin:24px 0 8px; color:var(--dim); }
.cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:12px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
.card .k { font-size:12px; color:var(--dim); text-transform:uppercase; letter-spacing:.04em; }
.card .v { font-size:19px; font-weight:600; margin-top:2px; word-break:break-all; }
.card .v small { font-size:12px; font-weight:400; color:var(--dim); }
.ok { color:var(--ok); } .warn { color:var(--warn); } .err { color:var(--err); }
table { width:100%; border-collapse:collapse; background:var(--panel);
        border:1px solid var(--line); border-radius:10px; overflow:hidden; }
th, td { padding:8px 10px; text-align:left; border-bottom:1px solid var(--line);
         font-size:14px; vertical-align:top; }
th { color:var(--dim); font-weight:600; font-size:12px; text-transform:uppercase; }
tr:last-child td { border-bottom:none; }
code, .mono { font-family:ui-monospace,Consolas,monospace; font-size:13px; }
form.inline { display:inline; }
input, select, textarea { background:var(--bg); color:var(--fg); border:1px solid var(--line);
        border-radius:6px; padding:7px 9px; font:inherit; }
input:focus, textarea:focus { outline:1px solid var(--acc); }
button { background:var(--acc); color:#0b1017; border:none; border-radius:6px;
         padding:7px 14px; font:inherit; font-weight:600; cursor:pointer; }
button.small { padding:3px 10px; font-size:13px; }
button.danger { background:var(--err); color:#fff; }
button.ghost { background:var(--line); color:var(--fg); }
.grid2 { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:10px 24px; }
.field { display:flex; align-items:center; justify-content:space-between; gap:10px;
         padding:6px 0; border-bottom:1px dashed var(--line); }
.field label { color:var(--dim); }
.field input { width:180px; }
.flash { padding:10px 14px; border-radius:8px; margin-bottom:16px; }
.flash.ok { background:#15321f; border:1px solid var(--ok); }
.flash.err { background:#3a1a18; border:1px solid var(--err); }
pre.log { background:#0b0f14; border:1px solid var(--line); border-radius:10px;
          padding:12px; font-size:12.5px; line-height:1.45; overflow-x:auto;
          white-space:pre-wrap; word-break:break-all; max-height:70vh; overflow-y:auto; }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px;
         background:var(--line); color:var(--dim); }
.login-box { max-width:340px; margin:12vh auto; background:var(--panel);
             border:1px solid var(--line); border-radius:12px; padding:28px; }
.login-box input { width:100%; margin:8px 0 14px; }
details.msg > summary { cursor:pointer; }
.actions-bar { display:flex; gap:8px; flex-wrap:wrap; margin:10px 0 18px; }
"""


LOGO = r"""
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
"""

LOGIN_CSS = """
body.c64 { background:#7869c4; display:flex; align-items:center; justify-content:center;
           min-height:100vh; margin:0; }
.c64-screen { background:#40318d; border:24px solid #7869c4; border-radius:8px;
              padding:28px 34px; max-width:720px; width:100%;
              font-family:ui-monospace,'Cascadia Mono',Consolas,monospace;
              color:#7869c4; box-shadow:0 0 60px rgba(0,0,0,.45); }
.c64-screen pre.logo { color:#a59fe0; font-size:10px; line-height:1.2; margin:0 0 14px;
                       overflow-x:auto; text-shadow:0 0 6px rgba(165,159,224,.5); }
.c64-screen .boot { color:#a59fe0; font-size:14px; margin:0 0 4px; letter-spacing:.02em; }
.c64-screen .ready { color:#a59fe0; font-size:14px; margin:14px 0 6px; }
.c64-screen form { display:flex; gap:0; align-items:center; font-size:14px; }
.c64-screen input { background:#40318d; border:none; outline:none; color:#a59fe0;
                    font:inherit; flex:1; padding:2px 0; letter-spacing:.1em; }
.c64-screen button { background:#a59fe0; color:#40318d; border:none; padding:4px 14px;
                     font:inherit; font-weight:700; cursor:pointer; }
.c64-screen .err-line { color:#e0574f; font-size:14px; margin:0 0 4px; }
.c64-screen .ok-line { color:#3fbf7f; font-size:14px; margin:0 0 4px; }
.cursor { display:inline-block; width:9px; height:15px; background:#a59fe0;
          margin-left:2px; animation:blink 1s steps(1) infinite; vertical-align:text-bottom; }
@keyframes blink { 50% { opacity:0; } }
@media (max-width:640px) { .c64-screen { border-width:12px; padding:16px; }
                           .c64-screen pre.logo { font-size:6.5px; } }
"""


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _fmt_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "nie"
    if seconds < 90:
        return f"vor {int(seconds)}s"
    if seconds < 5400:
        return f"vor {int(seconds / 60)}min"
    if seconds < 172800:
        return f"vor {seconds / 3600:.1f}h"
    return f"vor {int(seconds / 86400)}d"


def _fmt_ts(iso: Optional[str]) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso


def _fmt_path(node: Optional[dict]) -> str:
    """Zeigt den vom Node bekannten Routing-Pfad an. path_known unterscheidet
    'Pfad dem Node unbekannt' (haeufig, z.B. vor dem ersten Nachrichtenaustausch)
    von einem bestaetigten Pfad -- beides fiel frueher unter 'direkt/flood'."""
    if not node or not node.get("path_known"):
        return "unbekannt"
    return node["path"] or "direkt (bestaetigt)"


class WebAdminServer(BaseProtocol):
    def __init__(self, db: Database, config: dict, meshcore=None):
        self.db = db
        self.config = config
        self.meshcore = meshcore
        web_cfg = config.get("web", {})
        self.host = web_cfg.get("host", "0.0.0.0")
        self.port = web_cfg.get("port", 8080)
        self.password = str(web_cfg.get("password", "nnp-bbs"))
        # In der UI/Installer gesetztes Passwort (scrypt-Hash aus webconfig.yaml) hat Vorrang
        self.password_hash = str(web_cfg.get("password_hash", "") or "")
        # Weitere, namentliche Admin-Konten: username -> scrypt-Hash (web.admins in
        # webconfig.yaml). Das urspruengliche/legacy Konto bleibt fest unter dem
        # Namen "admin" ueber self.password_hash erreichbar (siehe _check_login) -
        # bestehende Installationen brauchen keine Migration.
        self._admins: dict[str, str] = dict(web_cfg.get("admins", {}) or {})
        # TLS direkt im Server (aiohttp ssl_context). cert/key liegen in data/ (nicht im Repo);
        # fehlen sie, wird beim Start ein self-signed Zertifikat erzeugt.
        tls_cfg = web_cfg.get("tls", {}) or {}
        self._tls_enabled = bool(tls_cfg.get("enabled", False))
        data_dir = os.path.dirname(config.get("storage", {}).get("path", "data/bbs.db")) or "."
        self._cert_path = tls_cfg.get("cert_file") or os.path.join(data_dir, "web-cert.pem")
        self._key_path = tls_cfg.get("key_file") or os.path.join(data_dir, "web-key.pem")
        self._tls_active = False   # wird in start() gesetzt, sobald der Context wirklich steht
        # Session-Cookie nur mit Secure-Flag, wenn tatsaechlich ueber HTTPS ausgeliefert wird.
        # Explizit setzbar fuer den Betrieb hinter einem TLS-terminierenden Reverse-Proxy.
        self._secure_cookie_cfg = bool(web_cfg.get("secure_cookie", False))
        self._secure_cookie = self._secure_cookie_cfg
        # Login-Lockout ist pro Client-IP – hinter einem Reverse-Proxy liefert
        # request.remote sonst fuer alle Clients dieselbe Proxy-IP (gemeinsames
        # Lockout). Nur mit explizit gesetztem web.trust_proxy_headers wird
        # X-Forwarded-For statt request.remote herangezogen (sonst durch jeden
        # Client faelschbar, der den Proxy umgeht) – Default: aus.
        self._trust_proxy_headers = bool(web_cfg.get("trust_proxy_headers", False))
        # session-token -> (created_ts, csrf_token, username)
        self._sessions: dict[str, tuple[float, str, str]] = {}
        # IP -> (fail_count, window_start, locked_until) fuer den Login-Brute-Force-Schutz
        self._login_fails: dict[str, tuple[int, float, float]] = {}
        # Wegwerf-Hash fuer den Konstantzeit-Vergleich bei unbekanntem Benutzernamen
        # (verhindert User-Enumeration ueber die Antwortzeit, siehe _check_login).
        self._dummy_hash = crypto.hash_password(pysecrets.token_hex(16))
        self._runner: Optional[web.AppRunner] = None
        self._started_at = time.time()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        self._ensure_initial_password()
        app = web.Application(middlewares=[self._auth_middleware])
        app.add_routes([
            web.get("/login", self.page_login),
            web.post("/login", self.do_login),
            web.get("/logout", self.do_logout),
            web.get("/", self.page_dashboard),
            web.get("/settings", self.page_settings),
            web.post("/settings", self.do_settings),
            web.post("/features", self.do_features),
            web.post("/cosysops", self.do_cosysops),
            web.post("/registration", self.do_registration_mode),
            web.post("/admins/add", self.do_admin_add),
            web.post("/admins/delete", self.do_admin_delete),
            web.post("/operation", self.do_operation),
            web.post("/password", self.do_password),
            web.post("/tls/import", self.do_tls_import),
            web.post("/tls/regenerate", self.do_tls_regenerate),
            web.post("/restart", self.do_restart),
            web.get("/backup", self.do_backup),
            web.get("/users", self.page_users),
            web.post("/users/add", self.do_user_add),
            web.post("/users/delete", self.do_user_delete),
            web.post("/users/mail", self.do_user_mail),
            web.post("/users/block", self.do_user_block),
            web.post("/users/blockkey", self.do_block_pubkey),
            web.post("/users/unblock", self.do_unblock),
            web.post("/users/ackgrant", self.do_ack_grant),
            web.post("/users/ackrevoke", self.do_ack_revoke),
            web.post("/users/sendlock", self.do_send_lock),
            web.post("/users/sendunlock", self.do_send_unlock),
            web.post("/users/approve", self.do_registration_approve),
            web.post("/users/reject", self.do_registration_reject),
            web.get("/messages", self.page_messages),
            web.get("/messages/new", self.page_message_new),
            web.post("/messages/new", self.do_message_new),
            web.post("/messages/delete", self.do_message_delete),
            web.post("/messages/sticky", self.do_message_sticky),
            web.get("/stats", self.page_stats),
            web.get("/stats/user", self.page_stats_user),
            web.get("/debug", self.page_debug),
            web.get("/api/logs", self.api_logs),
            web.post("/debug/advert", self.do_advert),
            web.post("/debug/reload", self.do_reload_contacts),
            web.post("/debug/dm", self.do_sysop_dm),
            web.post("/debug/channel", self.do_channel_broadcast),
            web.post("/debug/ping", self.do_ping),
        ])
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()

        ssl_ctx = None
        if self._tls_enabled:
            try:
                ssl_ctx = webtls.ensure_context(self._cert_path, self._key_path)
            except Exception as exc:
                logger.error("TLS-Initialisierung fehlgeschlagen (%s) – Web-Admin startet "
                             "UNVERSCHLUESSELT (HTTP)! Zertifikat unter Einstellungen pruefen.", exc)
        self._tls_active = ssl_ctx is not None
        # Secure-Cookie nur, wenn wirklich HTTPS ausgeliefert wird (oder explizit fuer
        # Proxy-Betrieb gesetzt und kein direktes TLS aktiv) – sonst bliebe der Login unmoeglich.
        self._secure_cookie = self._tls_active or (self._secure_cookie_cfg and not self._tls_enabled)

        site = web.TCPSite(self._runner, self.host, self.port, ssl_context=ssl_ctx)
        await site.start()
        scheme = "https" if self._tls_active else "http"
        logger.info("Web-Admin gestartet auf %s://%s:%d", scheme, self.host, self.port)

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
        logger.info("Web-Admin gestoppt.")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        if request.path == "/login":
            return await handler(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        entry = self._sessions.get(token)
        if not entry or time.time() - entry[0] > SESSION_TTL:
            self._sessions.pop(token, None)
            raise web.HTTPFound("/login")
        # CSRF-Schutz fuer zustandsaendernde Requests (Synchronizer-Token-Pattern).
        # SameSite=Strict deckt das schon weitgehend ab; der Token ist Defense-in-depth.
        if request.method == "POST":
            form = await request.post()
            if not hmac.compare_digest(str(form.get("_csrf", "")), entry[1]):
                raise web.HTTPForbidden(text="CSRF-Token ungueltig – bitte Seite neu laden.")
        return await handler(request)

    def _csrf_token(self, request: web.Request) -> str:
        """CSRF-Token der aktuellen Session (leer, wenn keine gueltige Session)."""
        entry = self._sessions.get(request.cookies.get(SESSION_COOKIE, ""))
        return entry[1] if entry else ""

    def _current_username(self, request: web.Request) -> str:
        """Benutzername der aktuellen Session ('' wenn keine gueltige Session)."""
        entry = self._sessions.get(request.cookies.get(SESSION_COOKIE, ""))
        return entry[2] if entry else ""

    def _check_login(self, username: str, candidate: str) -> bool:
        """Prueft username+Passwort. 'admin' ist das feste Legacy-Konto (siehe
        _check_password, inkl. SHA256->scrypt-Migration); alle anderen Namen werden
        gegen web.admins (Web-Admin: Einstellungen -> Co-SysOps/Weitere Admin-Konten)
        geprueft."""
        username = username.strip()
        if username == "admin" or not username:
            return self._check_password(candidate)
        stored = self._admins.get(username)
        if not stored:
            # Konstantzeit-Dummy-Vergleich: ein unbekannter Benutzername soll nicht
            # merklich schneller abgelehnt werden als ein existierender (Enumeration).
            crypto.verify_password(candidate, self._dummy_hash)
            return False
        return crypto.verify_password(candidate, stored)

    def _check_password(self, candidate: str) -> bool:
        """Prueft das Passwort: gesetzter scrypt-Hash (webconfig/Installer) vor
        Klartext-Fallback (config/secrets, nur Alt-Setups). Ein noch vorhandener
        Legacy-SHA256-Hash wird bei erfolgreicher Anmeldung transparent auf scrypt migriert."""
        if self.password_hash:
            if not crypto.verify_password(candidate, self.password_hash):
                return False
            if crypto.is_legacy_password_hash(self.password_hash):
                self._upgrade_password_hash(candidate)
            return True
        if self.password:
            return hmac.compare_digest(candidate, self.password)
        return False

    def _upgrade_password_hash(self, plaintext: str):
        """Ersetzt einen verifizierten Legacy-SHA256-Hash durch einen frischen scrypt-Hash."""
        try:
            digest = crypto.hash_password(plaintext)
            overlay = self._load_overlay()
            overlay.setdefault("web", {})["password_hash"] = digest
            self._save_overlay(overlay)
            self.password_hash = digest
            self.config.setdefault("web", {})["password_hash"] = digest
            logger.info("Web-Admin: Legacy-Passwort-Hash auf scrypt migriert")
        except Exception as exc:
            logger.warning("Passwort-Hash-Migration fehlgeschlagen: %s", exc)

    # --- Login-Brute-Force-Schutz (pro IP) --------------------------------

    def _login_blocked(self, ip: str) -> bool:
        rec = self._login_fails.get(ip)
        return bool(rec) and time.time() < rec[2]

    def _prune_login_fails(self, now: float):
        """Entfernt abgelaufene Eintraege (Fenster vorbei, nicht mehr gesperrt) und
        deckelt die Cache-Groesse gegen Aufblaehung durch viele verschiedene IPs."""
        stale = [ip for ip, (_c, ws, lu) in self._login_fails.items()
                 if now - ws > LOGIN_WINDOW and now >= lu]
        for ip in stale:
            del self._login_fails[ip]
        if len(self._login_fails) > LOGIN_FAILS_MAX:
            # aeltestes Zeitfenster zuerst verdraengen
            overflow = sorted(self._login_fails, key=lambda k: self._login_fails[k][1])
            for ip in overflow[:len(self._login_fails) - LOGIN_FAILS_MAX]:
                del self._login_fails[ip]

    def _record_login_fail(self, ip: str):
        now = time.time()
        self._prune_login_fails(now)
        count, window_start, _ = self._login_fails.get(ip, (0, now, 0.0))
        if now - window_start > LOGIN_WINDOW:
            count, window_start = 0, now
        count += 1
        locked_until = now + LOGIN_LOCKOUT if count >= LOGIN_MAX_FAILS else 0.0
        self._login_fails[ip] = (count, window_start, locked_until)
        if locked_until:
            logger.warning("Web-Admin: Login von %s nach %d Fehlversuchen fuer %ds gesperrt",
                           ip, count, LOGIN_LOCKOUT)

    def _clear_login_fails(self, ip: str):
        self._login_fails.pop(ip, None)

    def _prune_sessions(self):
        """Entfernt abgelaufene Sessions aus dem RAM-Cache. Ohne das wuerde er ueber
        die Uptime hinweg wachsen, da abgelaufene Tokens sonst nur beim erneuten
        Zugriff auf genau dieses Token verworfen werden (siehe _auth_middleware)."""
        now = time.time()
        stale = [tok for tok, entry in self._sessions.items() if now - entry[0] > SESSION_TTL]
        for tok in stale:
            del self._sessions[tok]

    # --- Erststart-Passwort ------------------------------------------------

    def _initial_pw_path(self) -> str:
        db_path = self.config.get("storage", {}).get("path", "data/bbs.db")
        return os.path.join(os.path.dirname(db_path) or ".", INITIAL_PW_FILE)

    def _ensure_initial_password(self):
        """Erststart ohne gesetztes Passwort: erzeugt ein Zufallspasswort, schreibt den
        scrypt-Hash nach webconfig.yaml und legt den Klartext einmalig in
        data/initial-web-password.txt (0600) ab. So ist das Panel nie mit dem
        ausgelieferten Default-Passwort erreichbar. Nach dem ersten Passwortwechsel
        wird die Datei entfernt (siehe do_password)."""
        if self.password_hash:
            return
        if self.password and self.password != "nnp-bbs":
            return   # explizit gesetztes Klartext-Passwort (Alt-Setup) respektieren
        pw = pysecrets.token_urlsafe(12)
        digest = crypto.hash_password(pw)
        overlay = self._load_overlay()
        overlay.setdefault("web", {})["password_hash"] = digest
        self._save_overlay(overlay)
        self.password_hash = digest
        self.config.setdefault("web", {})["password_hash"] = digest
        path = self._initial_pw_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            # Datei direkt mit 0600 anlegen – kein Fenster mit Weltlese-Rechten
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(pw + "\n")
            logger.warning("Kein Web-Admin-Passwort gesetzt – Zufallspasswort erzeugt und in "
                           "'%s' abgelegt. Anmelden, aendern; die Datei wird danach geloescht.", path)
        except OSError as exc:
            logger.error("Initiales Web-Passwort konnte nicht abgelegt werden (%s). "
                         "Bitte per scripts/set_web_password.py setzen.", exc)

    @staticmethod
    def _ram_boot_line() -> str:
        """Echte RAM-Werte im Stil der C64-Bootmeldung ('64K RAM SYSTEM ...')."""
        try:
            info = {}
            with open("/proc/meminfo", "r", encoding="ascii") as f:
                for line in f:
                    key, _, rest = line.partition(":")
                    info[key] = int(rest.strip().split()[0])  # kB
            total_kb = info["MemTotal"]
            free_bytes = info.get("MemAvailable", info.get("MemFree", 0)) * 1024
            if total_kb >= 1048576:
                total = f"{total_kb / 1048576:.0f}G"
            else:
                total = f"{total_kb / 1024:.0f}M"
            return f"{total} RAM SYSTEM&nbsp; {free_bytes} BASIC BYTES FREE"
        except (OSError, KeyError, ValueError):
            return "64K RAM SYSTEM&nbsp; 38911 BASIC BYTES FREE"

    async def page_login(self, request: web.Request) -> web.Response:
        notice = ""
        if "err" in request.query:
            notice = "<p class='err-line'>?ACCESS DENIED&nbsp; ERROR</p>"
        elif "msg" in request.query:
            notice = f"<p class='ok-line'>{_esc(request.query['msg']).upper()}</p>"
        # Benutzername-Feld nur zeigen, wenn ueberhaupt weitere Admin-Konten existieren -
        # beim Standard-Einzelkonto ("admin") bleibt die Login-Zeile unveraendert.
        user_field = ("<input name='username' placeholder='user' autofocus "
                      "autocomplete='username' style='width:5em'>&nbsp;"
                      if self._admins else "")
        pw_autofocus = "" if self._admins else " autofocus"
        body = f"""<div class="c64-screen">
          <pre class="logo">{LOGO}</pre>
          <p class="boot">**** MESHCORE BBSng SYSOP TERMINAL V2 ****</p>
          <p class="boot">{self._ram_boot_line()}</p>
          <p class="ready">READY.</p>{notice}
          <form method="post" action="/login">
            <span>LOAD&nbsp;"ADMIN",8,1:&nbsp;</span>
            {user_field}<input type="password" name="password"{pw_autofocus} autocomplete="current-password">
            <span class="cursor"></span>
            <button>RUN</button>
          </form></div>"""
        return web.Response(text=f"<!doctype html><html lang='de'><head><meta charset='utf-8'>"
                                 f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                                 f"<title>Meshcore BBSng Admin – Login</title><style>{LOGIN_CSS}</style></head>"
                                 f"<body class='c64'>{body}</body></html>",
                            content_type="text/html")

    def _client_ip(self, request: web.Request) -> str:
        """Client-IP fuers Login-Lockout: request.remote (Standard), oder bei
        web.trust_proxy_headers=true der erste Eintrag aus X-Forwarded-For
        (gesetzt vom vorgelagerten TLS-Proxy) – siehe Kommentar in __init__."""
        if self._trust_proxy_headers:
            fwd = request.headers.get("X-Forwarded-For", "")
            first = fwd.split(",")[0].strip()
            if first:
                return first
        return request.remote or "?"

    async def do_login(self, request: web.Request) -> web.Response:
        ip = self._client_ip(request)
        if self._login_blocked(ip):
            await asyncio.sleep(1.0)
            raise web.HTTPFound("/login?err=1")
        form = await request.post()
        username = str(form.get("username", "")).strip() or "admin"
        if not self._check_login(username, str(form.get("password", ""))):
            self._record_login_fail(ip)
            await asyncio.sleep(1.0)  # einfache Brute-Force-Bremse (zusaetzlich zum IP-Lockout)
            raise web.HTTPFound("/login?err=1")
        self._clear_login_fails(ip)
        self._prune_sessions()
        token = pysecrets.token_hex(32)
        self._sessions[token] = (time.time(), pysecrets.token_hex(32), username)
        resp = web.HTTPFound("/")
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Strict",
                        max_age=SESSION_TTL, secure=self._secure_cookie)
        return resp

    async def do_logout(self, request: web.Request) -> web.Response:
        self._sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        resp = web.HTTPFound("/login")
        resp.del_cookie(SESSION_COOKIE)
        return resp

    # ------------------------------------------------------------------
    # HTML-Geruest
    # ------------------------------------------------------------------

    def _page(self, request: web.Request, title: str, active: str, body: str) -> web.Response:
        flash = ""
        if "ok" in request.query:
            flash = f"<div class='flash ok'>{_esc(request.query['ok'])}</div>"
        elif "err" in request.query:
            flash = f"<div class='flash err'>{_esc(request.query['err'])}</div>"
        navs = [("/", "Dashboard"), ("/settings", "Einstellungen"), ("/users", "Benutzer"),
                ("/messages", "Nachrichten"), ("/stats", "Statistik"), ("/debug", "Debug")]
        nav_html = "".join(
            f"<a href='{path}' class='{'active' if path == active else ''}'>{label}</a>"
            for path, label in navs)
        warn = ""
        if os.path.exists(self._initial_pw_path()):
            warn = ("<div class='flash err'>Automatisch erzeugtes Erststart-Passwort aktiv. "
                    "Bitte unter <a href='/settings'>Einstellungen → Eigenes Passwort</a> ein eigenes "
                    "setzen – danach wird die Passwortdatei geloescht.</div>")
        if self.config.get("maintenance", {}).get("enabled"):
            warn += ("<div class='flash err'>\U0001f6e0 Wartungsmodus aktiv – die BBS beantwortet "
                     "alle Anfragen nur mit dem Wartungshinweis. "
                     "<a href='/settings'>Deaktivieren</a></div>")
        # CSRF-Token zentral in jedes POST-Formular injizieren (statt jedes einzeln)
        csrf = self._csrf_token(request)
        if csrf:
            body = re.sub(r'(<form\b[^>]*\bmethod=["\']?post["\']?[^>]*>)',
                          r'\1<input type="hidden" name="_csrf" value="' + csrf + '">',
                          body, flags=re.IGNORECASE)
        page = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} – Meshcore BBSng</title><style>{CSS}</style></head><body>
<nav><span class="brand">📻 Meshcore BBSng</span>{nav_html}
<span class="right">{(_esc(self._current_username(request)) + '&nbsp;·&nbsp;') if self._admins else ''}<a href="/logout">Abmelden</a></span></nav>
<main>{warn}{flash}<h1>{_esc(title)}</h1>{body}</main></body></html>"""
        return web.Response(text=page, content_type="text/html")

    @staticmethod
    def _redirect(path: str, ok: str = "", err: str = "", tab: str = "") -> web.HTTPFound:
        params = []
        if tab:
            params.append(f"tab={quote(tab)}")
        if ok:
            params.append(f"ok={quote(ok)}")
        elif err:
            params.append(f"err={quote(err)}")
        if params:
            path += "?" + "&".join(params)
        return web.HTTPFound(path)

    @staticmethod
    def _load_overlay() -> dict:
        try:
            with open(WEBCONFIG_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            return {}

    @staticmethod
    def _save_overlay(overlay: dict):
        with open(WEBCONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("# Von der Web-Admin-Oberflaeche verwaltete Overrides.\n"
                    "# Wird in main.py ueber config.yaml gemergt – nicht von Hand editieren.\n")
            yaml.safe_dump(overlay, f, allow_unicode=True, sort_keys=False)
        # Enthaelt den Passwort-Hash -- bei JEDEM Schreiben erzwingen, nicht nur bei
        # der Erst-Erzeugung (_ensure_initial_password/set_web_password.py setzen es
        # bereits, aber open(path, "w") aendert den Modus einer bereits bestehenden
        # Datei nicht; ohne dies bleiben einmal falsche Rechte -- z.B. durch manuelles
        # Anlegen -- dauerhaft falsch).
        try:
            os.chmod(WEBCONFIG_PATH, 0o600)
        except OSError:
            pass

    def _cfg_get(self, path: tuple):
        node = self.config
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    async def page_dashboard(self, request: web.Request) -> web.Response:
        mc = self.meshcore.status_snapshot() if self.meshcore else None
        users = await self.db.count_mc_contacts()
        total_msgs = await self.db.count_messages()
        unread = await self.db.count_all_unread_personal()
        uptime = _fmt_age(time.time() - self._started_at).replace("vor ", "")

        cards = []
        if mc:
            if mc["serial_open"] and not mc["reconnecting"]:
                age = mc["last_rx_age"]
                cls, txt = ("ok", "verbunden") if (age is not None and age < 120) else ("warn", "still")
                serial_v = f"<span class='{cls}'>{txt}</span> <small>RX {_fmt_age(age)}</small>"
            else:
                serial_v = "<span class='err'>getrennt</span>"
            cards += [
                ("Serial / Node", f"{serial_v}<br><small class='mono'>{_esc(mc['port'])}</small>"),
                ("TX-Power", f"{_esc(mc['tx_power'])} dBm"),
                ("Path-Hash", f"Mode {_esc(mc['path_hash_mode'])} <small>({(mc['path_hash_mode'] or 0) + 1} Byte)</small>"),
                ("Node-Kontakte", str(mc["node_contacts"])),
                ("Offene DMs", str(mc["pending_dms"])),
                ("Kanal", _esc(mc["channel_name"])),
                ("Region-Scope", self._region_card(mc)),
            ]
        else:
            cards.append(("MeshCore", "<span class='err'>deaktiviert</span>"))
        cards += [
            ("Registrierte User", str(users)),
            ("Nachrichten", f"{total_msgs} <small>({unread} ungelesen)</small>"),
            ("BBS-Uptime", _esc(uptime)),
            ("SysOp", _esc(self.config.get("sysop", "-"))),
            ("QTH", f"{_esc(self.config.get('qth', '-'))} <small>{_esc(self.config.get('locator', ''))}</small>"),
        ]
        cards_html = "".join(f"<div class='card'><div class='k'>{k}</div><div class='v'>{v}</div></div>"
                             for k, v in cards)
        pubkey = ""
        if mc and mc["self_pubkey"]:
            pubkey = (f"<h2>Node-Identitaet</h2><p class='mono'>PubKey: {mc['self_pubkey']}</p>")
        return self._page(request, "Dashboard", "/", f"<div class='cards'>{cards_html}</div>{pubkey}")

    @staticmethod
    def _region_card(mc: dict) -> str:
        """Dashboard-Karte: konfigurierter Region-Scope + Node-Bestaetigung."""
        region = mc.get("channel_region") or ""
        node_scope = mc.get("node_default_scope")
        if not region:
            return "<span class='warn'>unscoped</span>"
        if node_scope is None:
            status = "<small class='warn'>Node: unbestaetigt</small>"
        elif node_scope == region:
            status = "<small class='ok'>Node: bestaetigt</small>"
        else:
            status = f"<small class='err'>Node: '{_esc(node_scope)}'</small>"
        return f"{_esc(region)}<br>{status}"

    # ------------------------------------------------------------------
    # Einstellungen
    # ------------------------------------------------------------------

    async def page_settings(self, request: web.Request) -> web.Response:
        fields = []
        for key, path, label, typ, lo, hi, live in SETTINGS_SPEC:
            val = self._cfg_get(path)
            hint = " <span class='badge'>live</span>" if live else " <span class='badge'>Neustart</span>"
            attrs = ""
            if typ in (int, float):
                attrs = f" type='number'{f' min={lo}' if lo is not None else ''}{f' max={hi}' if hi is not None else ''}"
                if typ is float:
                    attrs += " step='0.1'"
            fields.append(f"<div class='field'><label>{_esc(label)}{hint}</label>"
                          f"<input name='{key}'{attrs} value='{_esc(val)}'></div>")
        body = f"""
        <form method="post" action="/settings">
          <div class="grid2">{''.join(fields)}</div>
          <p><button>Speichern</button></p>
        </form>
        <p style="color:var(--dim)">Felder mit <span class="badge">live</span> wirken sofort am Node.
        Alle Werte landen in <code>config/webconfig.yaml</code> (Overlay ueber config.yaml)
        und gelten dauerhaft ab dem naechsten Start.</p>
        <h2>Registrierung</h2>
        <form method="post" action="/registration">
          <div class="field"><label>Modus fuer neue Anmeldungen (Channel-Befehl "add")</label>
            <select name="mode">{self._registration_mode_options()}</select></div>
          <div class="field" style="max-width:600px;align-items:flex-start"><label>Nachrichten entfernter User
            (empfangene private Nachrichten werden bei Entfernung immer geloescht)</label>
            <label style="font-weight:normal"><input type="checkbox" name="delete_sent_private_messages"
                   {' checked' if self.config.get('users', {}).get('delete_sent_private_messages', True) else ''}
                   style="width:auto"> auch gesendete private Nachrichten loeschen</label>
            <label style="font-weight:normal"><input type="checkbox" name="delete_sent_board_messages"
                   {' checked' if self.config.get('users', {}).get('delete_sent_board_messages', True) else ''}
                   style="width:auto"> auch eigene Board-Bulletins loeschen</label></div>
          <div class="field" style="align-items:flex-start"><label>Inaktivitaets-Warnungen
            (Tage VOR der Entfernung, bis zu 3, Komma-getrennt)</label>
            <input name="warn_before_days" style="width:150px" maxlength="20"
                   value="{_esc(', '.join(str(d) for d in self.config.get('users', {}).get('inactivity_warn_before_days', [10, 5, 1])))}"
                   placeholder="z.B. 10, 5, 1"></div>
          <p><button>Registrierung speichern</button></p>
        </form>
        <p style="color:var(--dim)">Wirkt sofort, auch fuer die automatische
        Inaktivitaets-Entfernung (Feld "Inaktivitaet: Entfernung nach (Tage)" oben).
        Leer lassen bzw. weniger als 3 Werte angeben = entsprechend weniger Warnungen.
        Gilt zusammen mit dem Nachrichten-Feld auch fuer jede manuelle Entfernung/Sperre
        auf der Benutzer-Seite.</p>
        <h2>Betrieb</h2>
        <form method="post" action="/operation">
          <div class="field"><label>MOTD (wird im Hauptmenue angezeigt)</label></div>
          <textarea name="motd" rows="2" style="width:100%;max-width:600px"
                    maxlength="145" placeholder="z.B. Contest am Samstag – Board beachten! (max 145 Zeichen)">{_esc(self.config.get('motd', ''))}</textarea>
          <div class="field" style="max-width:600px"><label>\U0001f6e0 Wartungsmodus</label>
            <input type="checkbox" name="maintenance"{' checked' if self.config.get('maintenance', {}).get('enabled') else ''} style="width:auto"></div>
          <div class="field" style="max-width:600px"><label>Wartungstext</label>
            <input name="maintenance_text" style="width:320px" maxlength="140"
                   value="{_esc(self.config.get('maintenance', {}).get('text', ''))}"
                   placeholder="BBS im Wartungsmodus, bitte spaeter erneut versuchen."></div>
          <p><button>Betrieb speichern</button></p>
        </form>
        <p style="color:var(--dim)">Beides wirkt sofort. Im Wartungsmodus beantwortet die BBS
        jede DM nur noch mit dem Wartungstext.</p>
        <h2>Backup</h2>
        <p><a href="/backup"><button type="button" class="ghost">\U0001f4be Datenbank herunterladen</button></a>
        <span style="color:var(--dim)">Konsistenter Snapshot der bbs.db (User, Nachrichten, Statistik).</span></p>
        {self._tls_section()}
        <h2>BBS-Funktionen (MeshCore-Menue)</h2>
        <form method="post" action="/features">
          <div class="grid2">{self._features_checkboxes()}</div>
          <p><button>Funktionen speichern</button></p>
        </form>
        <p style="color:var(--dim)">Wirkt <b>sofort</b>: Deaktivierte Funktionen verschwinden aus
        den Menues und die zugehoerigen Befehle antworten mit "Unbekannt".
        REMOVE (Abmelden) bleibt immer aktiv.</p>
        <h2>Co-SysOps</h2>
        <form method="post" action="/cosysops">
          <div class="field" style="align-items:flex-start"><label>Rufzeichen (Komma-getrennt)</label>
            <input name="co_sysops" style="width:320px" maxlength="200"
                   value="{_esc(', '.join(self.config.get('co_sysops') or []))}"
                   placeholder="z.B. DL1ABC, DL2XYZ"></div>
          <p><button>Co-SysOps speichern</button></p>
        </form>
        <p style="color:var(--dim)">Diese Rufzeichen erhalten im Mesh dieselben SysOp-Rechte wie
        das oben eingetragene SysOp-Rufzeichen (aktuell: Nachrichten anderer User loeschen).
        Wirkt sofort.</p>
        <h2>Eigenes Passwort ({_esc(self._current_username(request) or 'admin')})</h2>
        <form method="post" action="/password" style="display:flex;gap:8px;flex-wrap:wrap">
          <input type="password" name="current" placeholder="Aktuelles Passwort" required>
          <input type="password" name="new1" placeholder="Neues Passwort (min. {MIN_PASSWORD_LEN})" minlength="{MIN_PASSWORD_LEN}" required>
          <input type="password" name="new2" placeholder="Neues Passwort wiederholen" minlength="{MIN_PASSWORD_LEN}" required>
          <button>Passwort aendern</button>
        </form>
        <p style="color:var(--dim)">Wird als scrypt-Hash in <code>config/webconfig.yaml</code>
        gespeichert und gilt sofort – nur die eigenen Sitzungen werden abgemeldet.</p>
        <h2>Weitere Admin-Konten ({len(self._admins)})</h2>
        {self._admins_table()}
        <form method="post" action="/admins/add" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
          <input name="username" placeholder="Benutzername" maxlength="32" required>
          <input type="password" name="password" placeholder="Initiales Passwort (min. {MIN_PASSWORD_LEN})" minlength="{MIN_PASSWORD_LEN}" required>
          <button>Konto anlegen</button>
        </form>
        <p style="color:var(--dim)">Zusaetzliche Admins mit vollem Zugriff auf dieses Web-Panel
        (keine Rollen/Rechtestufen). Jeder Admin kann sein eigenes Passwort oben selbst aendern.
        Das urspruengliche Konto "admin" kann hier nicht entfernt werden (bleibt immer als
        Fallback erreichbar).</p>
        <h2>Dienst</h2>
        <form method="post" action="/restart"
              onsubmit="return confirm('BBS wirklich neu starten? Verbindungen werden getrennt.')">
          <button class="danger">BBS neu starten</button>
        </form>"""
        return self._page(request, "Einstellungen", "/settings", body)

    def _admins_table(self) -> str:
        if not self._admins:
            return "<p style='color:var(--dim)'>Keine weiteren Admin-Konten.</p>"
        rows = "".join(f"""<tr><td class='mono'>{_esc(name)}</td>
              <td><form class='inline' method='post' action='/admins/delete'
                        onsubmit="return confirm('Admin-Konto {_esc(name)} wirklich entfernen?')">
                    <input type='hidden' name='username' value='{_esc(name)}'>
                    <button class='small danger'>Entfernen</button></form></td></tr>"""
                        for name in sorted(self._admins))
        return f"<table><tr><th>Benutzername</th><th></th></tr>{rows}</table>"

    async def do_admin_add(self, request: web.Request) -> web.Response:
        form = await request.post()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        if username.lower() == "admin":
            return self._redirect("/settings", err="'admin' ist das feste Standardkonto, "
                                                     "bitte einen anderen Namen waehlen")
        if not ADMIN_USERNAME_RE.match(username):
            return self._redirect("/settings", err="Benutzername: 3-32 Zeichen, "
                                                     "Buchstaben/Zahlen/_-. erlaubt")
        if len(password) < MIN_PASSWORD_LEN:
            return self._redirect("/settings", err=f"Passwort muss mindestens {MIN_PASSWORD_LEN} Zeichen haben")
        if crypto.is_weak_password(password, extra_forbidden=(username, self.config.get("callsign", ""))):
            return self._redirect("/settings", err="Passwort ist zu einfach/verbreitet – bitte ein "
                                                     "anderes waehlen")
        if username in self._admins:
            return self._redirect("/settings", err=f"Konto '{username}' existiert bereits")
        self._admins[username] = crypto.hash_password(password)
        overlay = self._load_overlay()
        overlay.setdefault("web", {})["admins"] = dict(self._admins)
        self._save_overlay(overlay)
        self.config.setdefault("web", {})["admins"] = dict(self._admins)
        logger.info("Web-Admin: Admin-Konto '%s' angelegt", username)
        return self._redirect("/settings", ok=f"Admin-Konto '{username}' angelegt")

    async def do_admin_delete(self, request: web.Request) -> web.Response:
        form = await request.post()
        username = str(form.get("username", "")).strip()
        if username not in self._admins:
            return self._redirect("/settings", err=f"Konto '{username}' nicht gefunden")
        del self._admins[username]
        overlay = self._load_overlay()
        overlay.setdefault("web", {})["admins"] = dict(self._admins)
        self._save_overlay(overlay)
        self.config.setdefault("web", {})["admins"] = dict(self._admins)
        # Sitzungen dieses Kontos sofort beenden
        stale = [tok for tok, entry in self._sessions.items() if entry[2] == username]
        for tok in stale:
            del self._sessions[tok]
        logger.info("Web-Admin: Admin-Konto '%s' entfernt", username)
        return self._redirect("/settings", ok=f"Admin-Konto '{username}' entfernt")

    def _features_checkboxes(self) -> str:
        current = self.config.get("features", {})
        boxes = []
        for key, (label, default) in FEATURES.items():
            checked = " checked" if current.get(key, default) else ""
            boxes.append(f"""<div class='field'><label>{_esc(label)}</label>
              <input type='checkbox' name='{key}'{checked} style='width:auto'></div>""")
        return "".join(boxes)

    async def do_features(self, request: web.Request) -> web.Response:
        form = await request.post()
        features = {key: (key in form) for key in FEATURES}
        overlay = self._load_overlay()
        overlay["features"] = features
        self._save_overlay(overlay)
        self.config["features"] = features  # wirkt sofort im laufenden Dispatcher
        off = [FEATURES[k][0].split(" (")[0] for k, on in features.items() if not on]
        logger.info("Web-Admin: Funktionen geaendert, deaktiviert: %s", ", ".join(off) or "keine")
        msg = "Funktionen gespeichert." + (f" Deaktiviert: {', '.join(off)}." if off else " Alle aktiv.")
        return self._redirect("/settings", ok=msg)

    async def do_cosysops(self, request: web.Request) -> web.Response:
        form = await request.post()
        raw = str(form.get("co_sysops", ""))
        names = [n.strip().upper() for n in raw.split(",") if n.strip()]
        invalid = [n for n in names if not USERNAME_RE.match(n)]
        if invalid:
            return self._redirect("/settings", err=f"Ungueltige Rufzeichen: {', '.join(invalid)}")
        overlay = self._load_overlay()
        overlay["co_sysops"] = names
        self._save_overlay(overlay)
        self.config["co_sysops"] = names  # wirkt sofort
        logger.info("Web-Admin: Co-SysOps geaendert: %s", ", ".join(names) or "keine")
        msg = f"Co-SysOps gespeichert: {', '.join(names)}." if names else "Co-SysOps entfernt."
        return self._redirect("/settings", ok=msg)

    def _registration_mode_options(self) -> str:
        current = self.config.get("registration", {}).get("mode", "challenge")
        return "".join(
            f"<option value='{value}'{' selected' if value == current else ''}>{_esc(label)}</option>"
            for value, label in REGISTRATION_MODES
        )

    async def do_registration_mode(self, request: web.Request) -> web.Response:
        form = await request.post()
        mode = str(form.get("mode", "")).strip()
        valid_modes = {v for v, _ in REGISTRATION_MODES}
        if mode not in valid_modes:
            return self._redirect("/settings", err=f"Ungueltiger Registrierungs-Modus: {mode}")
        delete_sent_private = "delete_sent_private_messages" in form
        delete_sent_board = "delete_sent_board_messages" in form

        raw_warn = str(form.get("warn_before_days", "")).strip()
        warn_before_days = []
        if raw_warn:
            parts = [p.strip() for p in raw_warn.split(",") if p.strip()]
            if len(parts) > 3:
                return self._redirect("/settings", err="Maximal 3 Inaktivitaets-Warnungen erlaubt")
            for p in parts:
                if not p.isdigit() or int(p) <= 0:
                    return self._redirect("/settings", err=f"Ungueltiger Wert bei Inaktivitaets-Warnungen: {p}")
                warn_before_days.append(int(p))

        overlay = self._load_overlay()
        overlay["registration"] = {"mode": mode}
        overlay.setdefault("users", {})["delete_sent_private_messages"] = delete_sent_private
        overlay["users"]["delete_sent_board_messages"] = delete_sent_board
        overlay["users"]["inactivity_warn_before_days"] = warn_before_days
        self._save_overlay(overlay)
        self.config["registration"] = {"mode": mode}
        self.config.setdefault("users", {})["delete_sent_private_messages"] = delete_sent_private
        self.config["users"]["delete_sent_board_messages"] = delete_sent_board
        self.config["users"]["inactivity_warn_before_days"] = warn_before_days
        logger.info("Web-Admin: Registrierungs-Modus '%s', gesendete private Nachrichten "
                    "loeschen: %s, Board-Bulletins loeschen: %s, Inaktivitaets-Warnungen "
                    "(Tage vorher): %s",
                    mode, delete_sent_private, delete_sent_board, warn_before_days or "keine")
        return self._redirect("/settings", ok=f"Registrierung gespeichert (Modus: {mode}).")

    async def do_operation(self, request: web.Request) -> web.Response:
        form = await request.post()
        # 145 Zeichen: mit "📢 "-Prefix bleibt die Menue-Zeile unter dem 150er-Paketlimit
        motd = str(form.get("motd", "")).strip()[:145]
        maint_on = "maintenance" in form
        maint_text = str(form.get("maintenance_text", "")).strip()[:140]
        overlay = self._load_overlay()
        overlay["motd"] = motd
        overlay["maintenance"] = {"enabled": maint_on, "text": maint_text}
        self._save_overlay(overlay)
        self.config["motd"] = motd
        self.config["maintenance"] = {"enabled": maint_on, "text": maint_text}
        logger.info("Web-Admin: Betrieb gespeichert (Wartungsmodus: %s)", "AN" if maint_on else "aus")
        msg = "Betrieb gespeichert." + (" Wartungsmodus ist AKTIV!" if maint_on else "")
        return self._redirect("/settings", ok=msg)

    async def do_backup(self, request: web.Request) -> web.Response:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # Privates Temp-Verzeichnis (0700) statt vorhersehbarem /tmp-Pfad: die DB
        # (Board, Userliste, Mailadressen, Statistik im Klartext) ist so nicht fuer
        # andere lokale Nutzer lesbar. VACUUM INTO braucht eine noch nicht existierende
        # Zieldatei – im 0700-Verzeichnis angelegt bleibt sie trotzdem geschuetzt.
        tmpdir = tempfile.mkdtemp(prefix="nnp-bbs-backup-")
        tmp = os.path.join(tmpdir, f"nnp-bbs-{stamp}.db")
        try:
            await self.db.backup_to(tmp)
        except Exception as exc:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return self._redirect("/settings", err=f"Backup fehlgeschlagen: {exc}")
        logger.info("Web-Admin: Backup erstellt (%s)", tmp)
        # Temp-Verzeichnis nach dem Download aufraeumen (60s reichen fuer die Uebertragung)
        asyncio.get_running_loop().call_later(
            60, lambda: shutil.rmtree(tmpdir, ignore_errors=True))
        return web.FileResponse(tmp, headers={
            "Content-Disposition": f'attachment; filename="nnp-bbs-{stamp}.db"'})

    async def do_password(self, request: web.Request) -> web.Response:
        """Aendert das Passwort des GERADE ANGEMELDETEN Kontos (self._current_username) -
        bei mehreren Admin-Konten also nicht zwingend 'admin'."""
        form = await request.post()
        current = str(form.get("current", ""))
        new1 = str(form.get("new1", ""))
        new2 = str(form.get("new2", ""))
        username = self._current_username(request) or "admin"
        if not self._check_login(username, current):
            return self._redirect("/settings", err="Aktuelles Passwort ist falsch")
        if len(new1) < MIN_PASSWORD_LEN:
            return self._redirect("/settings", err=f"Neues Passwort muss mindestens {MIN_PASSWORD_LEN} Zeichen haben")
        if crypto.is_weak_password(new1, extra_forbidden=(username, self.config.get("callsign", ""))):
            return self._redirect("/settings", err="Neues Passwort ist zu einfach/verbreitet – bitte "
                                                     "ein anderes waehlen")
        if new1 != new2:
            return self._redirect("/settings", err="Neue Passwoerter stimmen nicht ueberein")
        digest = crypto.hash_password(new1)
        overlay = self._load_overlay()
        if username == "admin":
            overlay.setdefault("web", {})["password_hash"] = digest
            self.password_hash = digest
            self.config.setdefault("web", {})["password_hash"] = digest
            # Erststart-Passwortdatei entfernen – ab jetzt gilt das selbst gesetzte Passwort
            try:
                os.remove(self._initial_pw_path())
                logger.info("Erststart-Passwortdatei entfernt")
            except OSError:
                pass
        else:
            self._admins[username] = digest
            overlay.setdefault("web", {})["admins"] = dict(self._admins)
            self.config.setdefault("web", {})["admins"] = dict(self._admins)
        self._save_overlay(overlay)
        # Nur die Sitzungen DIESES Kontos abmelden, nicht die anderer Admins.
        stale = [tok for tok, entry in self._sessions.items() if entry[2] == username]
        for tok in stale:
            del self._sessions[tok]
        logger.info("Web-Admin: Passwort geaendert (%s)", username)
        return web.HTTPFound("/login?msg=" + quote("Passwort geaendert – bitte neu anmelden."))

    # ------------------------------------------------------------------
    # TLS / HTTPS
    # ------------------------------------------------------------------

    def _tls_section(self) -> str:
        """HTML-Abschnitt fuer die Einstellungsseite: Zertifikats-Info, Import, Neu-Erzeugen."""
        if not self._tls_enabled:
            return ("<h2>HTTPS / TLS</h2><p style='color:var(--dim)'>TLS ist deaktiviert "
                    "(<code>web.tls.enabled: false</code>) – der Web-Admin laeuft unverschluesselt "
                    "ueber HTTP.</p>")
        info = webtls.cert_info(self._cert_path)
        if info:
            kind = "self-signed" if info["self_signed"] else "importiert"
            try:
                valid = info["not_after"].strftime("%d.%m.%Y")
            except Exception:
                valid = "?"
            sans = ", ".join(info["sans"]) or "-"
            status = "aktiv (HTTPS)" if self._tls_active else "konfiguriert – Neustart noetig"
            cls = "ok" if self._tls_active else "warn"
            detail = (f"<table><tr><th>Status</th><td class='{cls}'>{_esc(status)}</td></tr>"
                      f"<tr><th>Typ</th><td>{_esc(kind)}</td></tr>"
                      f"<tr><th>Subject</th><td class='mono'>{_esc(info['subject'])}</td></tr>"
                      f"<tr><th>Gueltig bis</th><td>{_esc(valid)}</td></tr>"
                      f"<tr><th>Gueltig fuer</th><td class='mono'>{_esc(sans)}</td></tr>"
                      f"<tr><th>SHA-256</th><td class='mono' style='word-break:break-all'>"
                      f"{_esc(info['fingerprint'])}</td></tr></table>")
        else:
            detail = ("<p class='warn'>Noch kein Zertifikat vorhanden – beim naechsten Start wird "
                      "automatisch ein self-signed Zertifikat erzeugt.</p>")
        return f"""
        <h2>HTTPS / TLS</h2>
        {detail}
        <p style="color:var(--dim)">Aenderungen an Zertifikat/Key werden erst nach einem
        <b>Neustart</b> (siehe unten) aktiv. Bei self-signed zeigt der Browser einmalig eine
        Sicherheitswarnung – das ist normal.</p>
        <div class="grid2">
          <form method="post" action="/tls/import" enctype="multipart/form-data">
            <div class="field"><label>Zertifikat (PEM)</label>
              <input type="file" name="cert" accept=".pem,.crt,.cer" required></div>
            <div class="field"><label>Private Key (PEM, ohne Passphrase)</label>
              <input type="file" name="key" accept=".pem,.key" required></div>
            <p><button>Zertifikat importieren</button></p>
          </form>
          <form method="post" action="/tls/regenerate"
                onsubmit="return confirm('Neues self-signed Zertifikat erzeugen? Das bisherige wird ersetzt.')">
            <div class="field"><label>Self-signed neu erzeugen</label></div>
            <p><button class="ghost">Neu erzeugen</button></p>
          </form>
        </div>"""

    async def do_tls_import(self, request: web.Request) -> web.Response:
        if not self._tls_enabled:
            return self._redirect("/settings", err="TLS ist deaktiviert (web.tls.enabled)")
        post = await request.post()

        def _field_bytes(name: str) -> bytes:
            fld = post.get(name)
            if fld is None or not hasattr(fld, "file"):
                return b""
            return fld.file.read()

        cert_bytes = _field_bytes("cert")
        key_bytes = _field_bytes("key")
        if not cert_bytes or not key_bytes:
            return self._redirect("/settings", err="Zertifikat und Private Key (beide PEM) erforderlich")
        ok, err = webtls.validate_pair(cert_bytes, key_bytes)
        if not ok:
            return self._redirect("/settings", err=err)
        try:
            webtls.write_pair(self._cert_path, self._key_path, cert_bytes, key_bytes)
        except OSError as exc:
            return self._redirect("/settings", err=f"Speichern fehlgeschlagen: {exc}")
        logger.info("Web-Admin: TLS-Zertifikat importiert")
        return self._redirect("/settings", ok="Zertifikat importiert. Zum Aktivieren die BBS neu starten.")

    async def do_tls_regenerate(self, request: web.Request) -> web.Response:
        if not self._tls_enabled:
            return self._redirect("/settings", err="TLS ist deaktiviert (web.tls.enabled)")
        try:
            webtls.generate_self_signed(self._cert_path, self._key_path)
        except Exception as exc:
            return self._redirect("/settings", err=f"Zert-Erzeugung fehlgeschlagen: {exc}")
        logger.warning("Web-Admin: self-signed TLS-Zertifikat neu erzeugt")
        return self._redirect("/settings", ok="Neues self-signed Zertifikat erzeugt. Zum Aktivieren die BBS neu starten.")

    async def do_settings(self, request: web.Request) -> web.Response:
        form = await request.post()
        overlay = self._load_overlay()

        live_tx: Optional[int] = None
        live_phm: Optional[int] = None
        live_region: Optional[str] = None
        for key, path, label, typ, lo, hi, live in SETTINGS_SPEC:
            raw = str(form.get(key, "")).strip()
            # Region darf leer sein (= Scope loeschen, unscoped senden)
            if raw == "" and key != "channel_region":
                continue
            if key == "channel_region":
                raw = raw.lower()
                if not re.fullmatch(r"[a-z0-9-]{0,29}", raw):
                    return self._redirect("/settings", err="Region-Scope: nur Kleinbuchstaben, "
                                                           "Ziffern und '-' (max. 29 Zeichen)")
            try:
                val = typ(raw)
            except ValueError:
                return self._redirect("/settings", err=f"Ungueltiger Wert fuer {label}: {raw}")
            if typ in (int, float) and ((lo is not None and val < lo) or (hi is not None and val > hi)):
                return self._redirect("/settings", err=f"{label} muss zwischen {lo} und {hi} liegen")
            # Overlay-Struktur + laufende Config aktualisieren
            node = overlay
            cfg = self.config
            for part in path[:-1]:
                node = node.setdefault(part, {})
                cfg = cfg.setdefault(part, {})
            changed = cfg.get(path[-1]) != val
            node[path[-1]] = val
            cfg[path[-1]] = val
            if changed and live and self.meshcore:
                if key == "tx_power":
                    live_tx = val
                elif key == "path_hash_mode":
                    live_phm = val
                elif key == "channel_region":
                    live_region = val

        self._save_overlay(overlay)

        applied = []
        if live_tx is not None:
            await self.meshcore.apply_tx_power(live_tx)
            applied.append(f"TX-Power {live_tx} dBm")
        if live_phm is not None:
            await self.meshcore.apply_path_hash_mode(live_phm)
            applied.append(f"Path-Hash-Mode {live_phm}")
        if live_region is not None:
            await self.meshcore.apply_channel_region(live_region)
            applied.append(f"Region-Scope '{live_region}'" if live_region
                           else "Region-Scope entfernt (unscoped)")
        msg = "Gespeichert." + (f" Sofort angewendet: {', '.join(applied)}." if applied else "")
        return self._redirect("/settings", ok=msg)

    async def do_restart(self, request: web.Request) -> web.Response:
        async def _delayed_restart():
            await asyncio.sleep(1.0)
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-n", "systemctl", "restart", "nnp-bbs")
            await proc.wait()
        asyncio.create_task(_delayed_restart())
        logger.warning("Web-Admin: Neustart angefordert")
        return web.Response(
            text="<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='8;url=/'>"
                 "<body style='background:#12161c;color:#d6dde6;font-family:system-ui'>"
                 "<p style='margin:20vh auto;max-width:400px'>BBS startet neu – "
                 "diese Seite laedt in 8 Sekunden automatisch neu...</p></body>",
            content_type="text/html")

    # ------------------------------------------------------------------
    # Benutzer
    # ------------------------------------------------------------------

    async def page_users(self, request: web.Request) -> web.Response:
        contacts = await self.db.get_all_mc_contacts()
        live = {e["prefix"]: e for e in (self.meshcore.node_contact_list() if self.meshcore else [])}
        rows = []
        for c in contacts:
            prefix = c["pubkey"][:12]
            node = live.get(prefix)
            path = _fmt_path(node)
            seen = _fmt_age(node["last_seen_age"]) if node and node["last_seen_age"] is not None else "-"

            # Senderecht (Pubkey-Sicherheitshinweis): dauerhafte Sperre (send_locked)
            # sticht eine evtl. vorhandene Bestaetigung, siehe _pubkey_ack_gate.
            if c.get("send_locked"):
                ack_badge = "<span class='err'>\U0001f512 Gesperrt</span>"
                ack_actions = f"""<form class='inline' method='post' action='/users/sendunlock'>
                        <input type='hidden' name='name' value='{_esc(c['name'])}'>
                        <button class='small ghost'>Entsperren</button></form>"""
            elif c.get("pubkey_ack_confirmed"):
                ack_badge = "<span class='ok'>✓ Bestaetigt</span>"
                ack_actions = f"""<form class='inline' method='post' action='/users/ackrevoke'>
                        <input type='hidden' name='name' value='{_esc(c['name'])}'>
                        <button class='small ghost'>Entziehen</button></form>
                      <form class='inline' method='post' action='/users/sendlock'
                            onsubmit="return confirm('{_esc(c['name'])} das Senden dauerhaft sperren?')">
                        <input type='hidden' name='name' value='{_esc(c['name'])}'>
                        <button class='small danger'>Dauerhaft sperren</button></form>"""
            else:
                ack_badge = "<span class='warn'>✗ Unbestaetigt</span>"
                ack_actions = f"""<form class='inline' method='post' action='/users/ackgrant'>
                        <input type='hidden' name='name' value='{_esc(c['name'])}'>
                        <button class='small ghost'>Manuell bestaetigen</button></form>
                      <form class='inline' method='post' action='/users/sendlock'
                            onsubmit="return confirm('{_esc(c['name'])} das Senden dauerhaft sperren?')">
                        <input type='hidden' name='name' value='{_esc(c['name'])}'>
                        <button class='small danger'>Dauerhaft sperren</button></form>"""

            rows.append(f"""<tr>
              <td><b>{_esc(c['name'])}</b><br><span class='mono' style='color:var(--dim)'>{_esc(prefix)}…</span></td>
              <td>{_fmt_ts(c['added_at'])}</td>
              <td>{_esc(seen)}</td>
              <td>{_fmt_ts(c['last_active'])}</td>
              <td class='mono'>{_esc(path)}</td>
              <td><form class='inline' method='post' action='/users/mail'>
                    <input type='hidden' name='name' value='{_esc(c['name'])}'>
                    <input name='mail' value='{_esc(c['mail'])}' placeholder='Mail' style='width:150px'>
                    <button class='small ghost'>OK</button></form></td>
              <td>{ack_badge}<br>{ack_actions}</td>
              <td><form class='inline' method='post' action='/users/delete'
                        onsubmit="return confirm('{_esc(c['name'])} wirklich entfernen?')">
                    <input type='hidden' name='name' value='{_esc(c['name'])}'>
                    <button class='small danger'>Entfernen</button></form>
                  <form class='inline' method='post' action='/users/block'
                        onsubmit="return confirm('{_esc(c['name'])} sperren? Registrierung wird entfernt und der Pubkey blockiert.')">
                    <input type='hidden' name='name' value='{_esc(c['name'])}'>
                    <button class='small ghost'>Sperren</button></form></td>
            </tr>""")
        mesh_table = (f"<table><tr><th>Name / Pubkey</th><th>Registriert</th><th>Zuletzt gesehen</th>"
                      f"<th>Zuletzt aktiv</th><th>Pfad</th><th>Mail</th><th>Senderecht</th><th></th></tr>"
                      f"{''.join(rows)}</table>"
                      if rows else "<p>Keine registrierten MeshCore-User.</p>")

        pending = await self.db.get_pending_registrations()
        prows = "".join(f"""<tr><td><b>{_esc(p['name'])}</b><br>
              <span class='mono' style='color:var(--dim)'>{_esc(p['pubkey'][:12])}…</span></td>
              <td>{_fmt_ts(p['requested_at'])}</td>
              <td><form class='inline' method='post' action='/users/approve'>
                    <input type='hidden' name='prefix_hex' value='{_esc(p['prefix_hex'])}'>
                    <button class='small ghost'>Freischalten</button></form>
                  <form class='inline' method='post' action='/users/reject'
                        onsubmit="return confirm('Registrierung von {_esc(p['name'])} ablehnen?')">
                    <input type='hidden' name='prefix_hex' value='{_esc(p['prefix_hex'])}'>
                    <button class='small danger'>Ablehnen</button></form></td></tr>"""
                       for p in pending)
        pending_section = (f"""<h2>Ausstehende Freischaltungen ({len(pending)})</h2>
        <table><tr><th>Name / Pubkey</th><th>Angefragt</th><th></th></tr>{prows}</table>"""
                           if pending else "")

        blocked = await self.db.get_blocked()
        brows = "".join(f"""<tr><td><b>{_esc(b['name']) or '-'}</b></td>
              <td class='mono'>{_esc(b['pubkey'][:16])}…</td>
              <td>{_esc(b['reason']) or '-'}</td><td>{_fmt_ts(b['blocked_at'])}</td>
              <td><form class='inline' method='post' action='/users/unblock'>
                    <input type='hidden' name='pubkey' value='{_esc(b['pubkey'])}'>
                    <button class='small ghost'>Entsperren</button></form></td></tr>"""
                        for b in blocked)
        blocked_table = (f"<table><tr><th>Name</th><th>Pubkey</th><th>Grund</th>"
                         f"<th>Gesperrt am</th><th></th></tr>{brows}</table>"
                         if brows else "<p>Keine gesperrten Nodes.</p>")

        body = f"""
        <h2>MeshCore-User ({len(contacts)})</h2>{mesh_table}
        {pending_section}
        <h2>Manuell registrieren</h2>
        <form method="post" action="/users/add">
          <input name="name" placeholder="Rufzeichen/Name" maxlength="16" required>
          <input name="pubkey" placeholder="PubKey (64 Hex-Zeichen)" size="52"
                 pattern="[0-9a-fA-F]{{64}}" class="mono" required>
          <button>Registrieren</button>
        </form>
        <h2>Sperrliste ({len(blocked)})</h2>{blocked_table}
        <form method="post" action="/users/blockkey" style="margin-top:8px">
          <input name="pubkey" placeholder="PubKey sperren (64 Hex)" size="52"
                 pattern="[0-9a-fA-F]{{64}}" class="mono" required>
          <input name="name" placeholder="Name (optional)" maxlength="16">
          <input name="reason" placeholder="Grund (optional)" maxlength="60">
          <button class="danger">Sperren</button>
        </form>
        """
        return self._page(request, "Benutzer", "/users", body)

    async def do_user_add(self, request: web.Request) -> web.Response:
        form = await request.post()
        name = str(form.get("name", "")).strip()
        pubkey = str(form.get("pubkey", "")).strip().lower()
        if not name or len(pubkey) != 64:
            return self._redirect("/users", err="Name und 64-stelliger Hex-PubKey erforderlich")
        try:
            bytes.fromhex(pubkey)
        except ValueError:
            return self._redirect("/users", err="PubKey ist kein gueltiges Hex")
        if await self.db.find_mc_contact_by_name(name):
            return self._redirect("/users", err=f"{name} ist bereits registriert")
        if not self.meshcore:
            return self._redirect("/users", err="MeshCore ist deaktiviert")
        await self.meshcore.register_user(pubkey, name)
        return self._redirect("/users", ok=f"{name.upper()} registriert")

    async def do_user_delete(self, request: web.Request) -> web.Response:
        form = await request.post()
        name = str(form.get("name", "")).strip()
        if self.meshcore and await self.meshcore.remove_user(name):
            return self._redirect("/users", ok=f"{name.upper()} entfernt")
        return self._redirect("/users", err=f"{name} nicht gefunden")

    async def do_user_block(self, request: web.Request) -> web.Response:
        form = await request.post()
        name = str(form.get("name", "")).strip()
        if not self.meshcore:
            return self._redirect("/users", err="MeshCore ist deaktiviert")
        if await self.meshcore.block_user(name, reason="Via Web-Admin gesperrt"):
            return self._redirect("/users", ok=f"{name.upper()} gesperrt")
        return self._redirect("/users", err=f"{name} nicht gefunden")

    async def do_block_pubkey(self, request: web.Request) -> web.Response:
        form = await request.post()
        pubkey = str(form.get("pubkey", "")).strip().lower()
        if len(pubkey) != 64:
            return self._redirect("/users", err="PubKey muss 64 Hex-Zeichen haben")
        try:
            bytes.fromhex(pubkey)
        except ValueError:
            return self._redirect("/users", err="PubKey ist kein gueltiges Hex")
        if not self.meshcore:
            return self._redirect("/users", err="MeshCore ist deaktiviert")
        await self.meshcore.block_pubkey(pubkey, str(form.get("name", "")).strip(),
                                         str(form.get("reason", "")).strip())
        return self._redirect("/users", ok=f"Pubkey {pubkey[:12]}… gesperrt")

    async def do_unblock(self, request: web.Request) -> web.Response:
        form = await request.post()
        pubkey = str(form.get("pubkey", "")).strip().lower()
        if not self.meshcore:
            return self._redirect("/users", err="MeshCore ist deaktiviert")
        await self.meshcore.unblock_pubkey(pubkey)
        return self._redirect("/users", ok=f"Pubkey {pubkey[:12]}… entsperrt")

    async def do_user_mail(self, request: web.Request) -> web.Response:
        form = await request.post()
        name = str(form.get("name", "")).strip()
        mail = str(form.get("mail", "")).strip()
        if not await self.db.find_mc_contact_by_name(name):
            return self._redirect("/users", err=f"{name} nicht gefunden")
        if mail and not is_valid_email(mail):
            return self._redirect("/users", err="Ungueltige Mailadresse (Format: name@domain.tld)")
        await self.db.set_mc_contact_mail(name, mail)
        return self._redirect("/users", ok=f"Mail fuer {name.upper()} gespeichert")

    async def do_ack_grant(self, request: web.Request) -> web.Response:
        """Setzt das Senderecht manuell (ohne OK-Challenge des Users) -- z.B. fuer
        Bestandsuser, die der SysOp bereits anderweitig verifiziert hat."""
        form = await request.post()
        name = str(form.get("name", "")).strip()
        if not await self.db.find_mc_contact_by_name(name):
            return self._redirect("/users", err=f"{name} nicht gefunden")
        await self.db.set_pubkey_ack_confirmed(name, True)
        return self._redirect("/users", ok=f"Senderecht fuer {name.upper()} gesetzt")

    async def do_ack_revoke(self, request: web.Request) -> web.Response:
        """Entzieht das Senderecht wieder -- der User muss die OK-Challenge beim
        naechsten S/SB-Versuch erneut durchlaufen (kein dauerhaftes Verbot)."""
        form = await request.post()
        name = str(form.get("name", "")).strip()
        if not await self.db.find_mc_contact_by_name(name):
            return self._redirect("/users", err=f"{name} nicht gefunden")
        await self.db.set_pubkey_ack_confirmed(name, False)
        return self._redirect("/users", ok=f"Senderecht fuer {name.upper()} entzogen")

    async def do_send_lock(self, request: web.Request) -> web.Response:
        """Dauerhafte Sperre des Senderechts -- staerker als ackrevoke: laesst sich
        NICHT durch eine erneute OK-Challenge des Users umgehen, siehe
        _pubkey_ack_gate in protocols/meshcore/server.py."""
        form = await request.post()
        name = str(form.get("name", "")).strip()
        if not await self.db.find_mc_contact_by_name(name):
            return self._redirect("/users", err=f"{name} nicht gefunden")
        await self.db.set_send_locked(name, True)
        return self._redirect("/users", ok=f"{name.upper()} dauerhaft vom Senden gesperrt")

    async def do_send_unlock(self, request: web.Request) -> web.Response:
        form = await request.post()
        name = str(form.get("name", "")).strip()
        if not await self.db.find_mc_contact_by_name(name):
            return self._redirect("/users", err=f"{name} nicht gefunden")
        await self.db.set_send_locked(name, False)
        return self._redirect("/users", ok=f"Dauerhafte Sperre fuer {name.upper()} aufgehoben")

    async def do_registration_approve(self, request: web.Request) -> web.Response:
        """Freischaltung einer im Modus 'sysop_approval' wartenden Registrierung."""
        form = await request.post()
        prefix_hex = str(form.get("prefix_hex", "")).strip()
        if not self.meshcore:
            return self._redirect("/users", err="MeshCore ist deaktiviert")
        if not await self.meshcore.approve_registration(prefix_hex):
            return self._redirect("/users", err="Registrierung nicht gefunden (evtl. bereits bearbeitet)")
        return self._redirect("/users", ok="Registrierung freigeschaltet")

    async def do_registration_reject(self, request: web.Request) -> web.Response:
        form = await request.post()
        prefix_hex = str(form.get("prefix_hex", "")).strip()
        if not self.meshcore:
            return self._redirect("/users", err="MeshCore ist deaktiviert")
        if not await self.meshcore.reject_registration(prefix_hex):
            return self._redirect("/users", err="Registrierung nicht gefunden (evtl. bereits bearbeitet)")
        return self._redirect("/users", ok="Registrierung abgelehnt")

    # ------------------------------------------------------------------
    # Nachrichten
    # ------------------------------------------------------------------

    async def page_messages(self, request: web.Request) -> web.Response:
        tab = request.query.get("tab", "board")
        if tab not in ("board", "private"):
            tab = "board"
        msgs = await self.db.get_messages()
        days = self._cfg_get(("board", "retention_days")) or 14
        now = now_utc()

        board_msgs = sorted((m for m in msgs if m.msg_type == "B"),
                            key=lambda m: (0 if m.sticky else 1, -m.id))
        private_msgs = sorted((m for m in msgs if m.msg_type == "P"), key=lambda m: -m.id)

        def _tab_link(label: str, key: str, count: int) -> str:
            text = f"{label} ({count})"
            if key == tab:
                return f"<button>{text}</button>"
            return f"<a href='/messages?tab={key}'><button class='ghost'>{text}</button></a>"

        tab_nav = (f"<div class='actions-bar'>{_tab_link('📋 Board', 'board', len(board_msgs))}"
                   f"{_tab_link('🔒 Privat', 'private', len(private_msgs))}</div>")

        if tab == "board":
            rows = []
            for m in board_msgs:
                pin = "📌" if m.sticky else "📄"   # gleiche Icons wie in der MeshCore-Board-Liste (BL)
                label = "Loesen" if m.sticky else "Sticky"
                sticky_cell = (f"""<form class='inline' method='post' action='/messages/sticky'>
                    <input type='hidden' name='id' value='{m.id}'>
                    <input type='hidden' name='sticky' value='{0 if m.sticky else 1}'>
                    <button class='small ghost'>{pin} {label}</button></form>""")
                if m.sticky:
                    expiry_cell = "📌 nie"
                else:
                    expires_at = m.created_at + timedelta(days=days)
                    remaining = (expires_at - now).days
                    cls = " class='warn'" if remaining <= 1 else ""
                    expiry_cell = (f"<span{cls}>{expires_at.strftime('%d.%m.%y')}</span> "
                                   f"<small>({max(remaining, 0)}d)</small>")
                content_cell = (f"<details class='msg'><summary>{_esc(m.subject) or '(kein Betreff)'}</summary>"
                               f"<pre style='white-space:pre-wrap'>{_esc(m.body)}</pre></details>")
                rows.append(f"""<tr>
                  <td>{m.id}</td>
                  <td>{pin}</td>
                  <td>{_esc(m.from_call)}</td>
                  <td>{content_cell}</td>
                  <td>{m.created_at.strftime('%d.%m.%y %H:%M')}</td>
                  <td>{expiry_cell}</td>
                  <td>👁 {m.views}</td>
                  <td style="white-space:nowrap">{sticky_cell}
                      <form class='inline' method='post' action='/messages/delete'
                            onsubmit="return confirm('Nachricht #{m.id} loeschen?')">
                        <input type='hidden' name='id' value='{m.id}'>
                        <input type='hidden' name='tab' value='board'>
                        <button class='small danger'>X</button></form></td>
                </tr>""")
            table = (f"<table><tr><th>#</th><th></th><th>Von</th><th>Betreff / Text</th>"
                     f"<th>Datum</th><th>Löschung</th><th>Aufrufe</th><th></th></tr>{''.join(rows)}</table>"
                     if rows else "<p>Keine Board-Nachrichten vorhanden.</p>")
            top_board = sorted(board_msgs, key=lambda m: -m.views)[:5]
            top_html = ("".join(
                f"<div class='card'><div class='k'>{_esc(m.subject) or '(kein Betreff)'} "
                f"<small>#{m.id}</small></div><div class='v'>👁 {m.views}</div></div>"
                for m in top_board if m.views) or "<p style='color:var(--dim)'>Noch keine Aufrufe erfasst.</p>")
            body = (tab_nav +
                    f"<div class='actions-bar'><a href='/messages/new'><button>+ Neue Board-Nachricht</button></a></div>"
                    f"<p style='color:var(--dim)'>Board-Nachrichten werden automatisch nach {days} Tagen geloescht "
                    f"(einstellbar unter <a href='/settings'>Einstellungen</a>). 📌 Sticky-Nachrichten sind davon ausgenommen. "
                    f"👁 Aufrufe = wie oft die Nachricht per R&lt;Nr&gt; gelesen wurde (ueber alle User summiert).</p>"
                    f"<h2>Meistgelesene Board-Nachrichten</h2><div class='cards'>{top_html}</div>"
                    f"{table}")
            count = len(board_msgs)
        else:
            rows = []
            for m in private_msgs:
                state = "✓ gelesen" if m.read else "● ungelesen"
                rows.append(f"""<tr>
                  <td>{m.id}</td>
                  <td>{state}</td>
                  <td>{_esc(m.from_call)} → {_esc(m.to_call)}</td>
                  <td><span style='color:var(--dim)'>\U0001f512 Privat – Inhalt geschuetzt</span></td>
                  <td>{m.created_at.strftime('%d.%m.%y %H:%M')}</td>
                  <td style="white-space:nowrap">
                      <form class='inline' method='post' action='/messages/delete'
                            onsubmit="return confirm('Nachricht #{m.id} loeschen?')">
                        <input type='hidden' name='id' value='{m.id}'>
                        <input type='hidden' name='tab' value='private'>
                        <button class='small danger'>X</button></form></td>
                </tr>""")
            table = (f"<table><tr><th>#</th><th>Status</th><th>Von → An</th><th>Betreff / Text</th>"
                     f"<th>Datum</th><th></th></tr>{''.join(rows)}</table>"
                     if rows else "<p>Keine privaten Nachrichten vorhanden.</p>")
            max_personal = self._cfg_get(("messages", "max_personal")) or 30
            body = (tab_nav +
                    f"<p style='color:var(--dim)'>\U0001f512 Betreff/Text privater Nachrichten sind hier bewusst nicht "
                    f"einsehbar (Datenschutz) – loeschen ist trotzdem moeglich. Postfach-Limit: {max_personal} Nachrichten je "
                    f"Empfaenger, weitere Sendeversuche werden abgelehnt statt aeltere automatisch zu loeschen.</p>"
                    f"{table}")
            count = len(private_msgs)

        title = f"Nachrichten – {'Board' if tab == 'board' else 'Privat'} ({count})"
        return self._page(request, title, "/messages", body)

    async def page_message_new(self, request: web.Request) -> web.Response:
        default_from = self.config.get("callsign", "SYSOP")
        body = f"""
        <form method="post" action="/messages/new">
          <div class="field"><label>Von</label><input name="from_call" value="{_esc(default_from)}" maxlength="16"></div>
          <div class="field"><label>An</label><input name="to_call" value="ALL" maxlength="16"></div>
          <div class="field"><label>Betreff</label><input name="subject" maxlength="80" required></div>
          <div class="field" style="align-items:flex-start">
            <label>Text</label>
            <textarea name="body" rows="6" style="width:100%;max-width:600px" required></textarea>
          </div>
          <div class="field"><label>📌 Sticky (nicht automatisch loeschen)</label>
            <input type="checkbox" name="sticky" style="width:auto"></div>
          <p><button>Veroeffentlichen</button> <a href="/messages"><button type="button" class="ghost">Abbrechen</button></a></p>
        </form>"""
        return self._page(request, "Neue Board-Nachricht", "/messages", body)

    async def do_message_new(self, request: web.Request) -> web.Response:
        form = await request.post()
        subject = str(form.get("subject", "")).strip()
        text = str(form.get("body", "")).strip()
        if not subject or not text:
            return self._redirect("/messages/new", err="Betreff und Text sind erforderlich")
        from_call = str(form.get("from_call", "")).strip() or self.config.get("callsign", "SYSOP")
        to_call = str(form.get("to_call", "")).strip() or "ALL"
        msg = Message(
            id=None, msg_type="B", to_call=to_call, from_call=from_call,
            subject=subject, body=text, sticky="sticky" in form,
        )
        msg_id = await self.db.save_message(msg)
        logger.info("Web-Admin: Board-Nachricht #%d erstellt (von %s)", msg_id, from_call.upper())
        return self._redirect("/messages", ok=f"Board-Nachricht #{msg_id} veroeffentlicht", tab="board")

    async def do_message_delete(self, request: web.Request) -> web.Response:
        form = await request.post()
        try:
            msg_id = int(form.get("id", ""))
        except ValueError:
            return self._redirect("/messages", err="Ungueltige ID")
        tab = str(form.get("tab", "board"))
        await self.db.delete_message(msg_id)
        return self._redirect("/messages", ok=f"Nachricht #{msg_id} geloescht", tab=tab)

    async def do_message_sticky(self, request: web.Request) -> web.Response:
        form = await request.post()
        try:
            msg_id = int(form.get("id", ""))
        except ValueError:
            return self._redirect("/messages", err="Ungueltige ID")
        sticky = str(form.get("sticky", "0")) == "1"
        await self.db.set_sticky(msg_id, sticky)
        return self._redirect("/messages", ok=f"Nachricht #{msg_id} {'als sticky markiert' if sticky else 'nicht mehr sticky'}",
                              tab="board")

    # ------------------------------------------------------------------
    # Statistik
    # ------------------------------------------------------------------

    # Routing-Art einer Nachricht: Kategorial (Flood, Grau=Alt-Daten) + eine
    # ordinale Hop-Rampe (hop_1/hop_2_5/hop_gt5 – ein Grundton, monoton heller->
    # dunkler nach Hop-Zahl) fuer Direktpfad-Zustellungen. Gegen Panel #1a2029
    # validiert mit dataviz-Skill scripts/validate_palette.js (--ordinal fuer die
    # Hop-Rampe, kategorial fuer die Gesamtfolge inkl. Flood/Unbekannt).
    _ROUTE_COLORS = {"flood": "#3987e5", "hop_1": "#2eaa7b", "hop_2_5": "#009063",
                     "hop_gt5": "#00764b", "unbekannt": "#5b6472"}
    _ROUTE_LABELS = {"flood": "Flood", "hop_1": "Direkt (1 Hop, bestaetigt)",
                     "hop_2_5": "Multihop (2-5 Hops)", "hop_gt5": "Multihop (>5 Hops)",
                     "unbekannt": "Direkt (Pfad unbekannt) / Alt-Daten"}
    _ROUTE_ORDER = ("flood", "hop_1", "hop_2_5", "hop_gt5", "unbekannt")

    @classmethod
    def _render_stacked_route_chart(cls, rows: list[dict], days: int, title: str, aria_label: str) -> str:
        """Gestapeltes Balkendiagramm (SVG): Nachrichten pro Tag nach Routing-Art
        (Flood/Direkt/Multihop), je Kategorie eine feste Farbe (siehe _ROUTE_COLORS).
        Legende immer sichtbar (>=2 Serien), Werte-Label nur am Tages-Maximum,
        Rest via Hover-Tooltip; 2px Surface-Gap zwischen gestapelten Segmenten."""
        today = now_utc().date()
        day_list = [(today - timedelta(days=i)) for i in range(days - 1, -1, -1)]
        by_day: dict[str, dict[str, int]] = {}
        for r in rows:
            by_day.setdefault(r["day"], {})[r["route"]] = r["n"]
        present = [r for r in cls._ROUTE_ORDER
                  if any(by_day.get(d.isoformat(), {}).get(r) for d in day_list)]
        if not present:
            return "<p>Noch keine Daten fuer diesen Zeitraum.</p>"
        totals = [sum(by_day.get(d.isoformat(), {}).values()) for d in day_list]
        vmax = max(totals)

        step = 1
        for cand in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
            if vmax <= cand * 4:
                step = cand
                break
        ymax = max(step * -(-vmax // step), step) if vmax else step * 4

        W, H, ml, mr, mt, mb = 920, 240, 40, 8, 14, 26
        pw, ph = W - ml - mr, H - mt - mb
        band = pw / days
        bw = min(band - 2, 24)
        gap = 2

        parts = [f"<svg viewBox='0 0 {W} {H}' role='img' preserveAspectRatio='xMidYMid meet' "
                 f"aria-label='{_esc(aria_label)}' style='width:100%;height:auto;display:block'>"]
        for i in range(0, ymax + 1, step):
            y = mt + ph - ph * i / ymax
            parts.append(f"<line x1='{ml}' y1='{y:.1f}' x2='{W - mr}' y2='{y:.1f}' "
                         f"stroke='#2a3341' stroke-width='1'/>")
            parts.append(f"<text x='{ml - 6}' y='{y + 4:.1f}' text-anchor='end' "
                         f"font-size='11' fill='#8494a8'>{i}</text>")
        imax = totals.index(vmax) if vmax else -1
        for i, d in enumerate(day_list):
            x = ml + i * band + (band - bw) / 2
            label = d.strftime("%d.%m.")
            counts = by_day.get(d.isoformat(), {})
            y_cursor = mt + ph
            for route in present:
                v = counts.get(route, 0)
                if not v:
                    continue
                h = ph * v / ymax
                y_top = y_cursor - h
                seg_h = max(h - gap, 2)               # min. 2px sichtbar, auch bei kleinen Werten
                parts.append(f"<rect x='{x:.1f}' y='{y_top:.1f}' width='{bw:.1f}' "
                             f"height='{seg_h:.1f}' rx='2' fill='{cls._ROUTE_COLORS[route]}'/>")
                y_cursor = y_top - gap
            if i == imax:
                y_top_total = mt + ph - ph * totals[i] / ymax
                parts.append(f"<text x='{x + bw / 2:.1f}' y='{y_top_total - 5:.1f}' text-anchor='middle' "
                             f"font-size='11' font-weight='600' fill='#d6dde6'>{totals[i]}</text>")
            if i % 5 == 0 or i == days - 1:
                parts.append(f"<text x='{x + bw / 2:.1f}' y='{H - 8}' text-anchor='middle' "
                             f"font-size='11' fill='#8494a8'>{label}</text>")
            tip = " · ".join(f"{cls._ROUTE_LABELS[r]}: {counts.get(r, 0)}" for r in present if counts.get(r))
            parts.append(f"<rect x='{ml + i * band:.1f}' y='{mt}' width='{band:.1f}' height='{ph}' "
                         f"fill='transparent' class='hov' data-d='{label}' data-v='{_esc(tip or 'keine')}'/>")
        parts.append("</svg>")
        svg = "".join(parts)
        legend = "".join(
            f"<span style='display:inline-flex;align-items:center;gap:6px;margin-right:16px'>"
            f"<span style='width:10px;height:10px;border-radius:2px;background:{cls._ROUTE_COLORS[r]};"
            f"display:inline-block'></span><span style='color:var(--dim);font-size:13px'>"
            f"{_esc(cls._ROUTE_LABELS[r])}</span></span>" for r in present)
        return f"""
        <div class="card" style="padding:16px">
          <div class="k" style="margin-bottom:8px">{_esc(title)}</div>
          <div style="margin-bottom:10px">{legend}</div>
          <div style="position:relative">{svg}
            <div id="charttip" style="position:absolute;display:none;background:#0b0f14;
                 border:1px solid #2a3341;border-radius:6px;padding:4px 9px;font-size:12.5px;
                 pointer-events:none;white-space:nowrap"></div>
          </div>
        </div>
        <script>
        (function() {{
          const tip = document.getElementById('charttip');
          document.querySelectorAll('rect.hov').forEach(r => {{
            r.addEventListener('mousemove', e => {{
              tip.textContent = r.dataset.d + ': ' + r.dataset.v;
              tip.style.display = 'block';
              const box = tip.parentElement.getBoundingClientRect();
              tip.style.left = Math.min(e.clientX - box.left + 12, box.width - 260) + 'px';
              tip.style.top = (e.clientY - box.top - 30) + 'px';
            }});
            r.addEventListener('mouseleave', () => tip.style.display = 'none');
          }});
        }})();
        </script>"""

    _HEX_PREFIX_RE = re.compile(r'^[0-9A-F]{12}$')

    @classmethod
    def _fmt_callsign(cls, call: str) -> str:
        """Events koennen statt eines Namens den rohen Pubkey-Prefix als callsign
        tragen (siehe MeshCoreServer._canonical_name / PUSH_SEND_CONFIRMED-Handler):
        das passiert, wenn ein ACK/NOACK eintrifft, bevor bzw. nachdem der Kontakt in
        self._contacts bekannt ist -- typischerweise waehrend einer noch nicht
        abgeschlossenen Self-Service-Registrierung (Bestaetigungscode verschickt,
        aber Code nie/falsch beantwortet). Statt des kryptischen Hex-Strings zeigen
        wir das als 'Vorregistrierung' an."""
        if cls._HEX_PREFIX_RE.match(call):
            return (f"<span style='color:var(--dim)' "
                    f"title='Pubkey-Prefix {call} – hat eine Registrierungs-DM erhalten, "
                    f"die Registrierung aber nie abgeschlossen'>⏳ Vorregistrierung ({call[:6]}…)</span>")
        return _esc(call)

    @staticmethod
    def _snr_class(snr: Optional[float]) -> str:
        """Grobe Einordnung fuer LoRa-SNR: >=0dB komfortabel, -10..0 grenzwertig, <-10 schwach."""
        if snr is None:
            return ""
        if snr >= 0:
            return "ok"
        if snr >= -10:
            return "warn"
        return "err"

    _SNR_DOT_COLOR = {"ok": "#3fbf7f", "warn": "#e0a83c", "err": "#e0574f"}

    @classmethod
    def _render_snr_sparkline(cls, values: list[float]) -> str:
        """Kompakte Inline-Sparkline der letzten SNR-Werte: Linie in Text-Dim-Ton,
        letzter Punkt in Ampelfarbe je nach aktuellem SNR (siehe _snr_class)."""
        if len(values) < 2:
            return ""
        w, h, pad = 108, 26, 3
        lo, hi = min(values), max(values)
        span = (hi - lo) or 1.0
        n = len(values)

        def _xy(i: int, v: float) -> tuple[float, float]:
            x = pad + (w - 2 * pad) * i / (n - 1)
            y = pad + (h - 2 * pad) * (1 - (v - lo) / span)
            return x, y

        pts = [_xy(i, v) for i, v in enumerate(values)]
        path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        last_x, last_y = pts[-1]
        dot = cls._SNR_DOT_COLOR.get(cls._snr_class(values[-1]), "#8494a8")
        return (f"<svg width='{w}' height='{h}' viewBox='0 0 {w} {h}' style='display:block' "
                f"role='img' aria-label='SNR-Verlauf, zuletzt {values[-1]:.1f} dB'>"
                f"<path d='{path}' fill='none' stroke='#8494a8' stroke-width='2' "
                f"stroke-linejoin='round' stroke-linecap='round'/>"
                f"<circle cx='{last_x:.1f}' cy='{last_y:.1f}' r='4' fill='{dot}'/></svg>")

    async def page_stats(self, request: web.Request) -> web.Response:
        try:
            days = max(1, min(int(request.query.get("days", "14")), 90))
        except ValueError:
            days = 14
        daily = await self.db.get_daily_stats(days)
        users = await self.db.get_user_stats(days)
        user_routes = await self.db.get_user_route_stats(days, "rx")
        last_snr = await self.db.get_last_snr()
        snr_history = await self.db.get_snr_history(days)
        by_type = await self.db.count_messages_by_type()

        # Chart: empfangene Nachrichten pro Tag nach Routing-Art, fest ueber 30 Tage
        route_rows = await self.db.get_daily_route_stats(30, "rx")
        chart = self._render_stacked_route_chart(
            route_rows, 30, "Empfangene Nachrichten nach Routing-Art (30 Tage)",
            "Empfangene Nachrichten pro Tag nach Routing-Art, letzte 30 Tage")

        # Routing-Mix je User (fuer die kleinen Farbpunkte in der User-Tabelle)
        by_user_routes: dict[str, dict[str, int]] = {}
        for row in user_routes:
            by_user_routes.setdefault(row["callsign"], {})[row["route"]] = row["n"]

        # Tages-Tabelle: rx / ack / noack je Tag
        by_day: dict[str, dict] = {}
        for row in daily:
            by_day.setdefault(row["day"], {})[row["type"]] = row["n"]
        drows = "".join(
            f"<tr><td>{_esc(day)}</td><td>{d.get('rx', 0)}</td>"
            f"<td class='ok'>{d.get('ack', 0)}</td><td class='err'>{d.get('noack', 0)}</td></tr>"
            for day, d in sorted(by_day.items(), reverse=True))
        daily_table = (f"<table><tr><th>Tag</th><th>Empfangen</th><th>ACK ✓</th>"
                       f"<th>Kein ACK</th></tr>{drows}</table>"
                       if drows else "<p>Noch keine Ereignisse erfasst.</p>")

        # User-Tabelle: rx, ack, noack, Quote, mittlere RTT, SNR (Empfangsqualitaet)
        by_user: dict[str, dict] = {}
        for row in users:
            entry = by_user.setdefault(row["callsign"], {"avg_rtt": None, "avg_snr": None})
            entry[row["type"]] = row["n"]
            if row["type"] == "ack" and row["avg_rtt"] is not None:
                entry["avg_rtt"] = row["avg_rtt"]
            if row["type"] == "rx" and row["avg_snr"] is not None:
                entry["avg_snr"] = row["avg_snr"]
                entry["min_snr"] = row["min_snr"]
                entry["max_snr"] = row["max_snr"]
        urows = []
        for call, d in sorted(by_user.items(), key=lambda kv: -kv[1].get("rx", 0)):
            ack, noack = d.get("ack", 0), d.get("noack", 0)
            total = ack + noack
            ack_quote = f"{100 * ack / total:.0f}%" if total else "-"
            cls = "ok" if total and ack / total >= 0.8 else ("warn" if total else "")
            rtt = f"{d['avg_rtt']:.0f} ms" if d.get("avg_rtt") else "-"
            last = last_snr.get(call)
            last_cls = self._snr_class(last[0] if last else None)
            last_txt = f"{last[0]:.1f} dB" if last else "-"
            if d.get("avg_snr") is not None:
                snr_cls = self._snr_class(d["avg_snr"])
                snr_txt = (f"{d['avg_snr']:.1f} dB "
                          f"<small>({d['min_snr']:.0f}…{d['max_snr']:.0f})</small>")
            else:
                snr_cls, snr_txt = "", "-"
            spark = (self._render_snr_sparkline(snr_history.get(call, []))
                    or "<span style='color:var(--dim)'>-</span>")
            route_mix = by_user_routes.get(call, {})
            dots = "".join(
                f"<span title='{_esc(self._ROUTE_LABELS[r])}: {route_mix[r]}' style='display:inline-flex;"
                f"align-items:center;gap:3px;margin-right:8px'>"
                f"<span style='width:8px;height:8px;border-radius:2px;background:{self._ROUTE_COLORS[r]};"
                f"display:inline-block'></span><span style='font-size:12.5px'>{route_mix[r]}</span></span>"
                for r in self._ROUTE_ORDER if route_mix.get(r))
            routing_cell = (f"<a href='/stats/user?call={quote(call)}&days={days}' "
                            f"style='display:inline-block'>{dots}</a>" if dots
                            else "<span style='color:var(--dim)'>-</span>")
            urows.append(f"<tr><td><b>{self._fmt_callsign(call)}</b></td><td>{d.get('rx', 0)}</td>"
                         f"<td>{routing_cell}</td>"
                         f"<td>{ack}</td><td>{noack}</td>"
                         f"<td class='{cls}'>{ack_quote}</td><td>{rtt}</td>"
                         f"<td class='{last_cls}'>{last_txt}</td>"
                         f"<td class='{snr_cls}'>{snr_txt}</td>"
                         f"<td>{spark}</td></tr>")
        user_table = (f"<div style='overflow-x:auto'><table><tr><th>User</th><th>Empfangen</th>"
                      f"<th>Routing (Details →)</th>"
                      f"<th>ACK ✓</th><th>Kein ACK</th><th>ACK-Quote</th><th>Ø RTT</th>"
                      f"<th>Letzte SNR</th><th>Ø SNR (Min…Max)</th><th>Verlauf</th></tr>"
                      f"{''.join(urows)}</table></div>"
                      if urows else "<p>Noch keine User-Ereignisse erfasst.</p>")

        board = by_type.get("B", 0)
        priv = by_type.get("P", 0)
        total_msgs = sum(by_type.values())
        body = f"""
        <p>Zeitraum:
          <a href="/stats?days=7">7 Tage</a> · <a href="/stats?days=14">14 Tage</a> ·
          <a href="/stats?days=30">30 Tage</a> · <a href="/stats?days=90">90 Tage</a>
          <span class="badge">aktuell: {days} Tage</span></p>
        <div class="cards">
          <div class="card"><div class="k">Nachrichten gesamt</div><div class="v">{total_msgs}</div></div>
          <div class="card"><div class="k">Privat</div><div class="v">{priv}</div></div>
          <div class="card"><div class="k">Board</div><div class="v">{board}</div></div>
        </div>
        <h2>Verlauf</h2>{chart}
        <h2>Aktivitaet je Tag</h2>{daily_table}
        <h2>Je User (sortiert nach Aktivitaet)</h2>{user_table}
        <p style="color:var(--dim)">ACK-Quote = bestaetigte / gesendete Antworten. SNR = Signal-
        Rausch-Abstand der empfangenen Pakete (>=0dB komfortabel, -10..0dB grenzwertig, darunter
        schwach); "Verlauf" zeigt die letzten SNR-Werte als Trend, der Punkt faerbt sich nach dem
        aktuellsten Wert. Eine niedrige ACK-Quote bei gleichzeitig gutem SNR deutet auf einen
        asymmetrischen Funklink hin (Hinweg ok, Rueckweg schwach) – schlechtes SNR erklaert
        Probleme in beide Richtungen. Routing-Punkte in der User-Tabelle fuehren zur Detailansicht
        je User. Ereignisse werden 90 Tage aufbewahrt.</p>"""
        return self._page(request, "Statistik", "/stats", body)

    async def page_stats_user(self, request: web.Request) -> web.Response:
        call = str(request.query.get("call", "")).strip().upper()
        try:
            days = max(1, min(int(request.query.get("days", "30")), 90))
        except ValueError:
            days = 30
        if not call:
            return self._redirect("/stats", err="Kein User angegeben")

        route_rows = await self.db.get_user_daily_route_stats(call, days, "rx")
        totals: dict[str, int] = {}
        for r in route_rows:
            totals[r["route"]] = totals.get(r["route"], 0) + r["n"]
        total_all = sum(totals.values())
        cards = "".join(
            f"<div class='card'><div class='k'>{_esc(self._ROUTE_LABELS.get(route, route))}</div>"
            f"<div class='v'>{totals[route]}</div></div>"
            for route in self._ROUTE_ORDER if totals.get(route))
        chart = self._render_stacked_route_chart(
            route_rows, days, f"Empfangen von {call} nach Routing-Art ({days} Tage)",
            f"Empfangene Nachrichten von {call} pro Tag nach Routing-Art")
        body = f"""
        <p><a href="/stats?days={days}">&larr; zurueck zur Statistik</a></p>
        <div class="cards">
          <div class="card"><div class="k">Gesamt empfangen</div><div class="v">{total_all}</div></div>
          {cards}
        </div>
        <h2>Verlauf</h2>{chart}"""
        return self._page(request, f"Statistik – {call}", "/stats", body)

    # ------------------------------------------------------------------
    # Debug
    # ------------------------------------------------------------------

    async def page_debug(self, request: web.Request) -> web.Response:
        contacts = self.meshcore.node_contact_list() if self.meshcore else []
        crows = "".join(
            f"<tr><td>{_esc(c['name'])}</td><td class='mono'>{_esc(c['prefix'])}</td>"
            f"<td>{ADV_TYPES.get(c['type'], c['type'])}</td>"
            f"<td class='mono'>{_esc(_fmt_path(c))}</td>"
            f"<td>{_fmt_age(c['last_seen_age']) if c['last_seen_age'] is not None else '-'}</td>"
            f"<td><form class='inline' method='post' action='/debug/ping' "
            f"onsubmit=\"this.querySelector('button').textContent='...';\">"
            f"<input type='hidden' name='prefix' value='{_esc(c['prefix'])}'>"
            f"<button class='small ghost'>Ping</button></form></td></tr>"
            for c in contacts)
        contacts_html = (f"<table><tr><th>Name</th><th>Prefix</th><th>Typ</th><th>Pfad</th>"
                         f"<th>Gehoert</th><th></th></tr>{crows}</table>"
                         if crows else "<p>Keine Node-Kontakte geladen.</p>")
        body = f"""
        <div class="actions-bar">
          <form class="inline" method="post" action="/debug/advert"><button class="ghost">Advert senden</button></form>
          <form class="inline" method="post" action="/debug/reload"><button class="ghost">Node-Kontakte neu laden</button></form>
        </div>
        <h2>SysOp-DM senden</h2>
        <form method="post" action="/debug/dm" style="display:flex;gap:8px;flex-wrap:wrap">
          <input name="name" placeholder="Empfaenger (Name)" required>
          <input name="text" placeholder="Nachricht" size="50" maxlength="600" required>
          <button>Senden</button>
        </form>
        <h2>Kanal-Broadcast ({_esc(self.meshcore.channel_name if self.meshcore else '-')})</h2>
        <form method="post" action="/debug/channel" style="display:flex;gap:8px;flex-wrap:wrap"
              onsubmit="return confirm('Nachricht wirklich in den Kanal senden? Alle Mitleser empfangen sie.')">
          <input name="text" placeholder="Nachricht an alle im Kanal (max 135 Zeichen)" size="60" maxlength="135" required>
          <button>In Kanal senden</button>
        </form>
        <h2>Log (journalctl -u nnp-bbs)</h2>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
          <select id="lines"><option>100</option><option selected>300</option><option>1000</option></select>
          <input id="filter" placeholder="Filter (z.B. DL9MU, ACK, WARNING)" size="30">
          <button class="ghost" onclick="loadLogs()">Aktualisieren</button>
          <label><input type="checkbox" id="auto"> Auto-Refresh (5s)</label>
        </div>
        <pre class="log" id="logbox">Lade...</pre>
        <h2>Node-Kontaktliste ({len(contacts)})</h2>{contacts_html}
        <script>
        async function loadLogs() {{
          const n = document.getElementById('lines').value;
          const f = encodeURIComponent(document.getElementById('filter').value);
          const r = await fetch(`/api/logs?lines=${{n}}&filter=${{f}}`);
          const box = document.getElementById('logbox');
          box.textContent = await r.text();
          box.scrollTop = box.scrollHeight;
        }}
        setInterval(() => {{ if (document.getElementById('auto').checked) loadLogs(); }}, 5000);
        document.getElementById('filter').addEventListener('keydown',
          e => {{ if (e.key === 'Enter') loadLogs(); }});
        loadLogs();
        </script>"""
        return self._page(request, "Debug", "/debug", body)

    async def api_logs(self, request: web.Request) -> web.Response:
        try:
            lines = min(int(request.query.get("lines", "300")), 5000)
        except ValueError:
            lines = 300
        needle = request.query.get("filter", "").strip().lower()
        try:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-u", "nnp-bbs", "-n", str(lines if not needle else 5000),
                "--no-pager", "-o", "short",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            text = out.decode("utf-8", errors="replace")
        except Exception as exc:
            return web.Response(text=f"journalctl fehlgeschlagen: {exc}")
        if needle:
            matched = [l for l in text.splitlines() if needle in l.lower()]
            text = "\n".join(matched[-lines:]) or "(keine Treffer)"
        return web.Response(text=text)

    async def do_advert(self, request: web.Request) -> web.Response:
        if not self.meshcore:
            return self._redirect("/debug", err="MeshCore ist deaktiviert")
        await self.meshcore.send_advert()
        return self._redirect("/debug", ok="Advert gesendet")

    async def do_reload_contacts(self, request: web.Request) -> web.Response:
        if not self.meshcore:
            return self._redirect("/debug", err="MeshCore ist deaktiviert")
        await self.meshcore.reload_node_contacts()
        return self._redirect("/debug", ok="Kontaktliste wird neu geladen")

    async def do_channel_broadcast(self, request: web.Request) -> web.Response:
        form = await request.post()
        text = str(form.get("text", "")).strip()
        if not text:
            return self._redirect("/debug", err="Text erforderlich")
        if not self.meshcore:
            return self._redirect("/debug", err="MeshCore ist deaktiviert")
        await self.meshcore.send_channel_broadcast(text)
        return self._redirect("/debug", ok="Broadcast in den Kanal gesendet")

    async def do_ping(self, request: web.Request) -> web.Response:
        form = await request.post()
        prefix = str(form.get("prefix", "")).strip()
        if not self.meshcore:
            return self._redirect("/debug", err="MeshCore ist deaktiviert")
        result = await self.meshcore.web_ping(prefix)   # blockiert bis Antwort/Timeout 30s
        ok = result.startswith("Pong")
        return self._redirect("/debug", ok=result) if ok else self._redirect("/debug", err=result)

    async def do_sysop_dm(self, request: web.Request) -> web.Response:
        form = await request.post()
        name = str(form.get("name", "")).strip()
        text = str(form.get("text", "")).strip()
        if not name or not text:
            return self._redirect("/debug", err="Empfaenger und Text erforderlich")
        if not self.meshcore:
            return self._redirect("/debug", err="MeshCore ist deaktiviert")
        if await self.meshcore.sysop_dm(name, text):
            return self._redirect("/debug", ok=f"DM an {name.upper()} gesendet (ACK-Status im Log)")
        return self._redirect("/debug", err=f"{name} ist nicht registriert")
