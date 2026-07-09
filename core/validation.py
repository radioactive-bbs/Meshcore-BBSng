"""Validierung von Benutzernamen / Rufzeichen fuer die Self-Service-Registrierung
ueber den MeshCore-Kanal ('add').

Erlaubt: 3-16 Zeichen aus Buchstaben, Ziffern und +-.!"§$%&/()=.
Bewusst NICHT erlaubt: Leerzeichen, Steuerzeichen, ESC (ANSI), sowie ;|<>*`` –
damit sind Escape-/Log-Injection und Mehrdeutigkeiten (Mehr-Wort-Namen) von
vornherein ausgeschlossen. Alle erlaubten Zeichen sind druckbares ASCII.
"""

import re

USERNAME_RE = re.compile(r'^[A-Za-z0-9+\-.!"§$%&/()=]{3,16}$')


def is_valid_username(name: str) -> bool:
    return bool(USERNAME_RE.match(name))
