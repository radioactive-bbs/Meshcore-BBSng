"""Zeit-Helfer: naive UTC-Zeitstempel ueber die nicht-deprecatete API.

datetime.utcnow() ist ab Python 3.12 deprecated. now_utc() liefert exakt denselben
*naiven* UTC-Zeitstempel (ohne tzinfo) wie utcnow() -- bewusst naiv, damit sich das
.isoformat()-Speicherformat (kein '+00:00'-Suffix) und alle bestehenden Vergleiche
in DB und Web-UI NICHT aendern. Ein Wechsel auf aware datetimes wuerde das Format
aendern und aware/naive gemischt TypeErrors werfen (z.B. expires_at - now).
"""

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Aktueller UTC-Zeitpunkt als *naives* datetime (Drop-in fuer datetime.utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
