"""Bereinigung nutzerkontrollierter Strings fuer Terminal-Ausgabe und Logs.

Verhindert Terminal-Escape-Injection (ANSI-/Steuerzeichen, die ein fremdes
Terminal manipulieren – z.B. Bildschirm ueberschreiben, Prompt faelschen) und
Log-Injection (gefaelschte Log-Zeilen via Zeilenumbrueche). Rufzeichen, Namen,
Betreff/Text usw. koennen aus Telnet ODER aus dem Mesh (RF) stammen und muessen
vor der Ausgabe an andere Nutzer bzw. ins Journal bereinigt werden.
"""

import re

# CSI-Sequenzen (ESC [ ... Endbyte) sowie sonstige ESC-eingeleitete Sequenzen
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]')


def for_terminal(text: str) -> str:
    """Fuer Telnet-Ausgabe: entfernt ANSI-Escapes und alle Steuerzeichen bis auf
    Zeilenumbruch (\\n) und Tab (\\t). \\r wird verworfen – die Ausgabe-
    Normalisierung in BBSSession.send() fuegt \\r\\n selbst wieder ein.
    Druckbares Unicode (Umlaute, Box-Zeichen, Emoji) bleibt erhalten."""
    text = _ANSI_RE.sub('', str(text))
    return ''.join(ch for ch in text if ch in '\n\t' or ch.isprintable())


def for_log(text: str) -> str:
    """Fuer Logausgaben: entfernt ALLE Steuerzeichen inkl. Zeilenumbruechen,
    sodass der Wert garantiert einzeilig bleibt (keine gefaelschten Log-Zeilen)."""
    return ''.join(ch for ch in str(text) if ch.isprintable())
