"""At-Rest-Verschluesselung fuer private Nachrichten (msg_type='P').

AES-256-GCM (authentifizierte Verschluesselung: Vertraulichkeit + Integritaet).
Der Schluessel lebt ausschliesslich in config/secrets.yaml (nie im Repo, nie
in der DB) - eine gestohlene bbs.db-Datei oder ein Backup allein ist damit
wertlos. Schuetzt NICHT gegen jemanden mit Code-Ausfuehrung auf dem laufenden
Prozess, da die BBS den Klartext zur Zustellung ohnehin benoetigt.
"""

import base64
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_LEN = 32   # AES-256
_PREFIX = "enc1:"

# scrypt-Parameter fuer die Passwort-Ableitung (gesalzen, speicherhart).
# N=2^14, r=8, p=1 → ca. 16 MB Speicher pro Hash; laeuft auch auf einem Pi Zero 2 W.
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_TAG = "scrypt"
# Harte Speicherobergrenze fuer scrypt (unsere Parameter brauchen ~16 MB). Bei
# verify_password werden n/r/p aus dem gespeicherten Hash gelesen; maxmem verhindert,
# dass ein manipulierter Hash mit absurd hohen Parametern den Prozess per
# Speicher-Allokation lahmlegt (scrypt wirft dann, verify faengt ab -> False).
_SCRYPT_MAXMEM = 64 * 1024 * 1024   # 64 MiB


def generate_key() -> str:
    """Erzeugt einen neuen Base64-kodierten 256-Bit-Schluessel fuer secrets.yaml."""
    return base64.b64encode(os.urandom(KEY_LEN)).decode("ascii")


def load_key(b64_key: str) -> bytes:
    key = base64.b64decode(b64_key)
    if len(key) != KEY_LEN:
        raise ValueError(f"messages_key muss {KEY_LEN} Byte (Base64-kodiert) sein, ist {len(key)}")
    return key


def is_encrypted(value: str) -> bool:
    return value.startswith(_PREFIX)


def encrypt(plaintext: str, key: bytes) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return _PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt(value: str, key: bytes) -> str:
    """Entschluesselt einen mit encrypt() erzeugten Wert. Werte ohne Praefix
    (z.B. Alt-Nachrichten vor Einfuehrung der Verschluesselung) werden
    unveraendert zurueckgegeben."""
    if not is_encrypted(value):
        return value
    try:
        raw = base64.b64decode(value[len(_PREFIX):])
        nonce, ct = raw[:12], raw[12:]
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except (InvalidTag, ValueError):
        # InvalidTag = falscher Schluessel/manipulierter Ciphertext; ValueError deckt
        # defektes Base64 (binascii.Error) und ungueltiges UTF-8 (UnicodeDecodeError)
        # ab. Andere Ausnahmen (MemoryError, KeyboardInterrupt, ...) bewusst NICHT
        # verschlucken, damit echte Fehler nicht als "falscher Schluessel" getarnt werden.
        return "[Entschluesselung fehlgeschlagen – falscher Schluessel?]"


# ---------------------------------------------------------------------------
# Passwort-Blockliste (Web-Admin) – Basisfilter gegen die offensichtlichsten/
# haeufigsten Passwoerter. Keine vollstaendige Staerkepruefung (kein zxcvbn),
# nur ein grobes Netz VOR der Mindestlaengen-Pruefung.
# ---------------------------------------------------------------------------
_COMMON_WEAK_PASSWORDS = {
    "123456", "123456789", "1234567890", "12345678", "password", "passwort",
    "qwerty", "qwertyuiop", "111111", "123123", "abc123", "1234567",
    "password1", "passwort1", "iloveyou", "letmein", "welcome", "monkey",
    "dragon", "football", "1q2w3e4r", "administrator", "changeme",
    "sonnenschein", "admin1234", "meshcore", "meshcorebbs", "nnpbbs", "nnp-bbs",
    "adminadmin", "sysopsysop", "funkfunkfunk",
}


def is_weak_password(password: str, extra_forbidden: tuple = ()) -> bool:
    """True, wenn das Passwort in der Blockliste steht (case-insensitive),
    nur aus einem einzigen wiederholten Zeichen besteht (z.B. 'aaaaaaaaaaaa')
    oder eine simple auf-/absteigende Ziffernfolge ist (z.B. '123456789012').
    extra_forbidden: zusaetzliche verbotene Werte, z.B. der eigene Benutzername
    oder das BBS-Rufzeichen (case-insensitive verglichen)."""
    pw = password.strip().lower()
    if pw in _COMMON_WEAK_PASSWORDS:
        return True
    if len(set(pw)) <= 1:
        return True
    if pw.isdigit():
        ascending = "".join(str(i % 10) for i in range(len(pw)))
        descending = "".join(str((9 - i) % 10) for i in range(len(pw)))
        if pw in (ascending, descending):
            return True
    return pw in {str(f).strip().lower() for f in extra_forbidden if f}


def hash_password(password: str) -> str:
    """Erzeugt einen gesalzenen scrypt-Hash im Format
    'scrypt$N$r$p$<salt_b64>$<hash_b64>'. Der Klartext wird nie gespeichert."""
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
                        maxmem=_SCRYPT_MAXMEM)
    return "${}${}${}${}${}${}".format(
        _SCRYPT_TAG, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def is_legacy_password_hash(stored: str) -> bool:
    """True, wenn der gespeicherte Hash noch das alte ungesalzene SHA-256-Hex ist
    (64 Hex-Zeichen) – dann sollte beim naechsten Login auf scrypt migriert werden."""
    if not stored or stored.startswith("$" + _SCRYPT_TAG + "$"):
        return False
    return len(stored) == 64 and all(c in "0123456789abcdef" for c in stored.lower())


def verify_password(password: str, stored: str) -> bool:
    """Prueft ein Passwort gegen einen scrypt-Hash. Akzeptiert uebergangsweise auch
    das alte ungesalzene SHA-256-Hex (Legacy), damit bestehende Installationen nach
    dem Update nicht ausgesperrt werden – siehe is_legacy_password_hash()."""
    if not stored:
        return False
    if stored.startswith("$" + _SCRYPT_TAG + "$"):
        try:
            _, _tag, n, r, p, salt_b64, hash_b64 = stored.split("$")
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(hash_b64)
            dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=int(n), r=int(r), p=int(p), dklen=len(expected),
                                maxmem=_SCRYPT_MAXMEM)
            return hmac.compare_digest(dk, expected)
        except Exception:
            return False
    if is_legacy_password_hash(stored):
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, stored.lower())
    return False
