"""TLS fuer die Web-Admin-Oberflaeche.

Erzeugt bei Bedarf ein self-signed Zertifikat (mit der ohnehin vorhandenen
cryptography-Bibliothek – kein openssl-CLI noetig), baut daraus einen
ssl.SSLContext fuer aiohttp und validiert/importiert vom SysOp hochgeladene
Zertifikate. Cert und Key liegen in data/ (nicht im Repo), der Key mit 0600.

Ablauf beim Start (siehe WebAdminServer.start):
  - cert/key vorhanden  → laden
  - fehlen              → self-signed erzeugen
  - importiertes Paar defekt → auf self-signed zurueckfallen (nie ganz ohne TLS,
    solange TLS aktiviert ist)
"""

import datetime
import ipaddress
import os
import socket
import ssl
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _write_secret(path: str, data: bytes):
    """Schreibt eine Datei mit 0600 (fuer den Private Key). os.open() erzwingt den
    Modus nur bei NEU angelegten Dateien – existiert path bereits (z.B. manuell
    kopierter Key, Backup-Restore mit anderer Umask), behaelt die Datei trotz
    O_TRUNC ihre alten Rechte. Der explizite chmod() danach korrigiert das in
    jedem Fall, unabhaengig davon ob die Datei neu oder bereits vorhanden war."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)


def _local_ips() -> set:
    """Best-Effort: eigene IPv4-Adressen fuer die Zertifikats-SAN (damit der
    Zugriff per LAN-IP zumindest bei importierter CA sauber validiert)."""
    ips = {"127.0.0.1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))   # kein echter Traffic, nur Routing-Lookup
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        ips.update(addrs)
    except OSError:
        pass
    return ips


def _sans() -> list:
    san = [x509.DNSName("localhost")]
    try:
        host = socket.gethostname()
        if host and host != "localhost":
            san.append(x509.DNSName(host))
    except OSError:
        pass
    for ip in sorted(_local_ips()):
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    return san


def generate_self_signed(cert_path: str, key_path: str):
    """Erzeugt ein neues self-signed RSA-2048-Zertifikat (10 Jahre gueltig)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Meshcore BBSng Web-Admin")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(_sans()), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    _write_secret(key_path, key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


# TLS-<=1.2-Cipher-Suites auf ECDHE/DHE (Forward Secrecy) + AEAD (GCM/ChaCha20)
# beschraenken. Der OpenSSL-Default enthaelt sonst u.a. PSK-/SRP-Suiten (fuer dieses
# Web-Admin-Panel sinnlose Angriffsflaeche, es ist keine PSK-Identity konfiguriert)
# sowie aeltere Suiten ohne Forward Secrecy. Betrifft nur TLS <=1.2; TLS-1.3-Suiten
# sind davon unabhaengig (immer stark) und bleiben unveraendert nutzbar.
_HARDENED_CIPHERS = ("ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:"
                     "!aNULL:!eNULL:!MD5:!3DES:!RC4:!PSK:!SRP:!DSS")


def load_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # Veraltete Protokolle (TLS 1.0/1.1) ausschliessen – unabhaengig von der OpenSSL-Policy.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers(_HARDENED_CIPHERS)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


def ensure_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """Liefert einen SSLContext: laedt vorhandenes Paar oder erzeugt self-signed.
    Ein defektes/nicht zusammenpassendes Paar wird durch ein frisches self-signed
    ersetzt, damit der Web-Admin nie ungewollt ohne TLS hochkommt."""
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        generate_self_signed(cert_path, key_path)
    try:
        return load_context(cert_path, key_path)
    except (ssl.SSLError, OSError, ValueError):
        generate_self_signed(cert_path, key_path)
        return load_context(cert_path, key_path)


def validate_pair(cert_bytes: bytes, key_bytes: bytes) -> tuple[bool, str]:
    """Prueft, ob cert_bytes ein gueltiges PEM-Zertifikat ist, key_bytes ein
    passender (unverschluesselter) Private Key. Gibt (ok, Fehlermeldung)."""
    try:
        cert = x509.load_pem_x509_certificate(cert_bytes)
    except Exception as exc:
        return False, f"Zertifikat ist kein gueltiges PEM: {exc}"
    try:
        key = serialization.load_pem_private_key(key_bytes, password=None)
    except TypeError:
        return False, "Private Key ist passwortgeschuetzt – bitte ohne Passphrase exportieren"
    except Exception as exc:
        return False, f"Private Key ist kein gueltiges PEM: {exc}"
    cert_pub = cert.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    key_pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    if cert_pub != key_pub:
        return False, "Zertifikat und Private Key passen nicht zusammen"
    # Gueltigkeitszeitraum pruefen – ein abgelaufenes/noch nicht gueltiges Zert wuerde
    # sonst importiert und beim Neustart geladen, der Browser lehnt es aber ab.
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before
    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    if not_before.tzinfo is None:
        not_before = not_before.replace(tzinfo=datetime.timezone.utc)
    if not_after.tzinfo is None:
        not_after = not_after.replace(tzinfo=datetime.timezone.utc)
    if now < not_before:
        return False, f"Zertifikat ist noch nicht gueltig (erst ab {not_before:%d.%m.%Y})"
    if now > not_after:
        return False, f"Zertifikat ist bereits abgelaufen (seit {not_after:%d.%m.%Y})"
    return True, ""


def write_pair(cert_path: str, key_path: str, cert_bytes: bytes, key_bytes: bytes):
    """Speichert ein (zuvor validiertes) Zertifikat/Key-Paar; Key mit 0600."""
    os.makedirs(os.path.dirname(cert_path) or ".", exist_ok=True)
    with open(cert_path, "wb") as f:
        f.write(cert_bytes)
    _write_secret(key_path, key_bytes)


def cert_info(cert_path: str) -> Optional[dict]:
    """Liest Anzeige-Infos aus dem Zertifikat (Subject, Gueltigkeit, Fingerprint,
    SANs, self-signed?). None, wenn keine Datei existiert oder sie unlesbar ist."""
    if not os.path.exists(cert_path):
        return None
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
    except Exception:
        return None
    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        sans = [str(v) for v in ext.get_values_for_type(x509.DNSName)]
        sans += [str(v) for v in ext.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        sans = []
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "not_after": not_after,
        "fingerprint": cert.fingerprint(hashes.SHA256()).hex(":"),
        "sans": sans,
        "self_signed": cert.issuer == cert.subject,
    }
