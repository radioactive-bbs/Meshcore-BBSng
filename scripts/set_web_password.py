#!/usr/bin/env python3
"""Setzt das Web-Admin-Passwort der Meshcore BBSng.

Liest das Klartext-Passwort bevorzugt von stdin (kein Leak in der Prozessliste),
alternativ aus argv[1], und schreibt ausschliesslich den gesalzenen scrypt-Hash
nach config/webconfig.yaml. Der Klartext wird nie gespeichert.

Aufruf (aus dem Installer oder von Hand):
    printf '%s' "$PW" | .venv/bin/python scripts/set_web_password.py
    .venv/bin/python scripts/set_web_password.py "meinPasswort"
"""

import os
import sys

# Repo-Root in den Importpfad, damit core.crypto gefunden wird
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import yaml  # noqa: E402

from core import crypto  # noqa: E402

WEBCONFIG_PATH = os.path.join(_ROOT, "config", "webconfig.yaml")
MIN_LEN = 8


def _read_password() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    return sys.stdin.readline().rstrip("\r\n")


def _remove_initial_pw() -> bool:
    """Entfernt eine evtl. vorhandene data/initial-web-password.txt – sobald ein
    Passwort gesetzt ist, ist die Erststart-Datei obsolet. Das Datenverzeichnis wird
    wie im Server aus storage.path (config.yaml/webconfig.yaml) abgeleitet."""
    cfg: dict = {}
    for name in ("config.yaml", "webconfig.yaml"):
        try:
            with open(os.path.join(_ROOT, "config", name), "r", encoding="utf-8") as f:
                cfg.update(yaml.safe_load(f) or {})
        except FileNotFoundError:
            pass
    db_path = (cfg.get("storage") or {}).get("path", "data/bbs.db")
    pw_file = os.path.join(_ROOT, os.path.dirname(db_path) or ".", "initial-web-password.txt")
    try:
        os.remove(pw_file)
        return True
    except OSError:
        return False


def main() -> int:
    pw = _read_password()
    if len(pw) < MIN_LEN:
        print(f"Fehler: Passwort muss mindestens {MIN_LEN} Zeichen haben.", file=sys.stderr)
        return 1

    try:
        with open(WEBCONFIG_PATH, "r", encoding="utf-8") as f:
            overlay = yaml.safe_load(f) or {}
    except FileNotFoundError:
        overlay = {}

    overlay.setdefault("web", {})["password_hash"] = crypto.hash_password(pw)

    with open(WEBCONFIG_PATH, "w", encoding="utf-8") as f:
        f.write("# Von der Web-Admin-Oberflaeche verwaltete Overrides.\n"
                "# Wird in main.py ueber config.yaml gemergt - nicht von Hand editieren.\n")
        yaml.safe_dump(overlay, f, allow_unicode=True, sort_keys=False)
    try:
        os.chmod(WEBCONFIG_PATH, 0o600)
    except OSError:
        pass

    msg = "Web-Admin-Passwort gesetzt (scrypt-Hash in config/webconfig.yaml)."
    if _remove_initial_pw():
        msg += " Erststart-Passwortdatei entfernt."
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
