"""Validierung von Benutzernamen / Rufzeichen fuer die Self-Service-Registrierung
ueber den MeshCore-Kanal ('add').

Erlaubt: 3-16 Zeichen aus Buchstaben, Ziffern und +-.!"§$%&/()=.
Bewusst NICHT erlaubt: Leerzeichen, Steuerzeichen, ESC (ANSI), sowie ;|<>*`` –
damit sind Escape-/Log-Injection und Mehrdeutigkeiten (Mehr-Wort-Namen) von
vornherein ausgeschlossen. Alle erlaubten Zeichen sind druckbares ASCII.
"""

import re

USERNAME_RE = re.compile(r'^[A-Za-z0-9+\-.!"§$%&/()=]{3,16}$')

# Simple Mail-Validierung (Wert stammt aus dem Mesh bzw. Web-Admin, also angreifer-
# kontrolliert): genau ein @, kein Whitespace/Steuerzeichen, plausible Laenge. Kein
# RFC-5322-Anspruch. Gemeinsame Quelle fuer MeshCore (MC-Befehl) und Web-Admin.
EMAIL_RE = re.compile(r'^[^\s@]{1,40}@[^\s@]{1,40}\.[^\s@]{2,20}$')


def is_valid_username(name: str) -> bool:
    return bool(USERNAME_RE.match(name))


def is_valid_email(mail: str) -> bool:
    return bool(EMAIL_RE.match(mail))
