"""Bereinigung nutzerkontrollierter Strings fuer Logs.

Verhindert Log-Injection (gefaelschte Log-Zeilen via Zeilenumbrueche).
Rufzeichen, Namen, Betreff/Text usw. stammen aus dem Mesh (RF) und muessen
vor der Ausgabe ins Journal bereinigt werden.
"""


def for_log(text: str) -> str:
    """Fuer Logausgaben: entfernt ALLE Steuerzeichen inkl. Zeilenumbruechen,
    sodass der Wert garantiert einzeilig bleibt (keine gefaelschten Log-Zeilen)."""
    return ''.join(ch for ch in str(text) if ch.isprintable())
