"""
MeshCore Companion Radio Protocol – Frame-Kodierung und Paket-Typen.

Framing (USB):
  Senden  (App → Node):  0x3C ('<') + uint16_le(len) + payload
  Empfang (Node → App):  0x3E ('>') + uint16_le(len) + payload

Quellen:
  https://github.com/ripplebiz/MeshCore/wiki/Companion-Radio-Protocol
  https://github.com/meshcore-dev/meshcore_py
"""

import hashlib
import struct
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from core import sanitize

# ---------------------------------------------------------------------------
# Kommando-Bytes (App → Node)
# ---------------------------------------------------------------------------
CMD_APP_START           = 0x01
CMD_SEND_TXT_MSG        = 0x02
CMD_SEND_CHANNEL_MSG    = 0x03
CMD_GET_CONTACTS        = 0x04
CMD_GET_DEVICE_TIME     = 0x05
CMD_SET_DEVICE_TIME     = 0x06
CMD_SEND_SELF_ADVERT    = 0x07   # Eigenwerbung senden → Node im Mesh bekannt machen
CMD_ADD_UPDATE_CONTACT  = 0x09
CMD_SYNC_NEXT_MESSAGE   = 0x0A
CMD_SET_RADIO_TX_POWER  = 0x0C   # LoRa-Sendeleistung setzen (persistiert via savePrefs)
CMD_RESET_PATH          = 0x0D   # Routing-Pfad eines Kontakts zuruecksetzen
CMD_SET_CHANNEL         = 0x20
CMD_DEVICE_QUERY        = 0x16
CMD_SET_PATH_HASH_MODE  = 0x3D   # OTA-Path-Hash-Groesse (Sende-Header pro Hop) setzen
CMD_SEND_TRACE_PATH     = 0x24   # Traceroute/Ping laengs eines Hop-Pfades (Antwort: 0x89)
CMD_SET_FLOOD_SCOPE_KEY     = 0x36   # 54: Session-Scope fuer ausgehende Floods (RAM, bis Node-Reboot)
CMD_SET_DEFAULT_FLOOD_SCOPE = 0x3F   # 63: persistenter Default-Scope (savePrefs, FW v1.15+)
CMD_GET_DEFAULT_FLOOD_SCOPE = 0x40   # 64: Default-Scope abfragen (Antwort: 0x1C)

# ---------------------------------------------------------------------------
# Antwort-/Push-Bytes (Node → App)
# ---------------------------------------------------------------------------
RESP_OK                 = 0x00
RESP_ERR                = 0x01
RESP_CONTACTS_START     = 0x02
RESP_CONTACT            = 0x03
RESP_CONTACTS_END       = 0x04
RESP_SELF_INFO          = 0x05
RESP_SENT               = 0x06
RESP_CONTACT_MSG        = 0x07   # eingehende Direktnachricht
RESP_CHANNEL_MSG        = 0x08   # eingehende Kanalnachricht
RESP_DEVICE_INFO        = 0x0D
RESP_CONTACT_MSG_V3     = 0x10   # wie 0x07, zusaetzlich SNR-Byte
RESP_CHANNEL_MSG_V3     = 0x11   # wie 0x08, zusaetzlich SNR-Byte
RESP_NO_MORE_MSGS       = 0x0A   # keine weiteren Nachrichten in der Queue
RESP_DEFAULT_FLOOD_SCOPE = 0x1C  # 28: Antwort auf CMD_GET_DEFAULT_FLOOD_SCOPE

PUSH_ADVERT             = 0x80   # Node-Advertisement empfangen
PUSH_PATH_UPDATED       = 0x81
PUSH_SEND_CONFIRMED     = 0x82
PUSH_MSG_WAITING        = 0x83   # neue Nachricht(en) warten
PUSH_CODE_TRACE_DATA    = 0x89   # Antwort auf CMD_SEND_TRACE_PATH (Traceroute-Ergebnis)

# ---------------------------------------------------------------------------
# Advert-/Kontakttypen (contact_type in RESP_CONTACT)
# ---------------------------------------------------------------------------
ADV_TYPE_NONE     = 0
ADV_TYPE_CHAT     = 1
ADV_TYPE_REPEATER = 2
ADV_TYPE_ROOM     = 3
ADV_TYPE_SENSOR   = 4


# ---------------------------------------------------------------------------
# Frame-Hilfsfunktionen
# ---------------------------------------------------------------------------

def encode_frame(payload: bytes) -> bytes:
    """Verpackt Payload in einen Send-Frame: 0x3C + uint16_le + payload."""
    return b'\x3c' + struct.pack('<H', len(payload)) + payload


def parse_frames(buf: bytearray) -> tuple[list[bytes], bytearray]:
    """
    Extrahiert vollstaendige Receive-Frames aus dem Eingangspuffer.
    Gibt (Liste von Payloads, Rest-Puffer) zurueck.
    """
    frames = []
    while len(buf) >= 3:
        idx = buf.find(0x3E)
        if idx < 0:
            buf.clear()
            break
        if idx > 0:
            del buf[:idx]
        if len(buf) < 3:
            break
        length = struct.unpack_from('<H', buf, 1)[0]
        if length > 300:          # Sanity-Check
            del buf[:1]
            continue
        total = 3 + length
        if len(buf) < total:
            break
        frames.append(bytes(buf[3:total]))
        del buf[:total]
    return frames, buf


# ---------------------------------------------------------------------------
# Kommando-Konstruktoren
# ---------------------------------------------------------------------------

def build_device_query() -> bytes:
    # 2. Byte = Protokollversion (0x03), wie in meshcore_py
    return encode_frame(bytes([CMD_DEVICE_QUERY, 0x03]))


def build_app_start(app_name: str = "Meshcore BBSng") -> bytes:
    # Format laut meshcore_py: CMD(1) + app_ver(0x03) + 6 reserved Bytes + Name
    payload = bytes([CMD_APP_START, 0x03]) + b'      ' + app_name.encode()
    return encode_frame(payload)


def build_get_contacts() -> bytes:
    # 'since' timestamp = 0 → alle Kontakte zurueckgeben
    return encode_frame(bytes([CMD_GET_CONTACTS]) + struct.pack('<I', 0))


def build_sync_next_message() -> bytes:
    return encode_frame(bytes([CMD_SYNC_NEXT_MESSAGE]))


def build_set_time() -> bytes:
    ts = struct.pack('<I', int(time.time()))
    return encode_frame(bytes([CMD_SET_DEVICE_TIME]) + ts)


def build_send_self_advert() -> bytes:
    """Node broadcastet sich selbst im Mesh – noetig damit andere Nodes DMs routen koennen."""
    return encode_frame(bytes([CMD_SEND_SELF_ADVERT]))


def build_trace_path(tag: int, path: bytes = b'', auth_code: int = 0, flags: int = 0) -> bytes:
    """Traceroute/Ping laengs eines Hop-Pfades. Antwort: PUSH_CODE_TRACE_DATA (0x89)
    mit passendem tag und SNR je Hop.
    Layout: [CMD_SEND_TRACE_PATH][tag(int32 LE)][auth_code(int32 LE)][flags(1)][path(hop-hashes)].
    flags: path_sz = flags & 0x03 = (hash_size - 1) → Bytes je Hop-Hash. Muss zur OTA-Path-Hash-
    Groesse des Node passen (mode 2 = 3 Byte ⇒ flags=2). path = Kette von Hop-Hashes bei dieser
    Groesse; fuer direkten Nachbarn genuegt dessen eigener Hash (erste hash_size Pubkey-Bytes)."""
    return encode_frame(bytes([CMD_SEND_TRACE_PATH])
                        + struct.pack('<ii', tag, auth_code)
                        + bytes([flags & 0xFF])
                        + path)


def build_set_path_hash_mode(mode: int) -> bytes:
    """Setzt die Default Path Hash Size (OTA-Sende-Header pro Hop) am Node.
    Sende-Header = (mode + 1) Byte → mode 0=1B, 1=2B, 2=3B. Persistiert via savePrefs.
    Layout: [CMD_SET_PATH_HASH_MODE][0][mode], mode ∈ {0,1,2}."""
    mode = max(0, min(int(mode), 2))
    return encode_frame(bytes([CMD_SET_PATH_HASH_MODE, 0, mode]))


def contact_add_uri(name: str, pubkey_hex: str, contact_type: int = ADV_TYPE_CHAT) -> str:
    """Baut eine meshcore://contact/add-URI. Die MeshCore-App erkennt sie in einer
    Nachricht (oder als QR-Code) und bietet dem Empfaenger einen 'Kontakt hinzufuegen'-
    Dialog an – kein manuelles Abtippen des 64-Hex-Pubkeys noetig.
    type: 1=Companion/Chat, 2=Repeater, 3=Room-Server, 4=Sensor (ADV_TYPE_*)."""
    return (f"meshcore://contact/add?name={quote(name, safe='')}"
            f"&public_key={pubkey_hex}&type={contact_type}")


def region_scope_key(region: str) -> bytes:
    """TransportKey einer Region: SHA256("#" + name)[:16].
    Entspricht TransportKeyStore::getAutoKeyFor in der Firmware – der Key ist rein
    aus dem oeffentlichen Regionsnamen abgeleitet, kein Geheimnis."""
    return hashlib.sha256(("#" + region).encode("utf-8")).digest()[:16]


def build_set_flood_scope_key(key: Optional[bytes]) -> bytes:
    """Setzt den Session-Scope fuer ALLE ausgehenden Floods des Node (Channel-Msgs,
    Flood-DMs, ACKs). Nicht persistiert – geht bei Node-Reboot verloren, daher bei
    jeder (Re-)Initialisierung erneut senden. key=None → Scope-Override loeschen.
    Layout: [0x36][sub=0][key(16)] bzw. [0x36][0] zum Loeschen."""
    payload = bytes([CMD_SET_FLOOD_SCOPE_KEY, 0])
    if key:
        payload += key[:16]
    return encode_frame(payload)


def build_set_default_flood_scope(region: str) -> bytes:
    """Setzt den persistenten Default-Scope am Node (FW v1.15+, savePrefs).
    Gilt fuer alle Flood-Pakete inkl. Self-Adverts. Leerer Name → Scope loeschen.
    Layout: [0x3F][name(31, null-terminiert)][key(16)]; nur [0x3F] loescht."""
    if not region:
        return encode_frame(bytes([CMD_SET_DEFAULT_FLOOD_SCOPE]))
    name_b = region.encode("utf-8")[:30].ljust(31, b"\x00")
    return encode_frame(bytes([CMD_SET_DEFAULT_FLOOD_SCOPE]) + name_b + region_scope_key(region))


def build_get_default_flood_scope() -> bytes:
    """Fragt den persistenten Default-Scope ab (Antwort: RESP_DEFAULT_FLOOD_SCOPE)."""
    return encode_frame(bytes([CMD_GET_DEFAULT_FLOOD_SCOPE]))


def parse_default_flood_scope(payload: bytes) -> str:
    """Parst RESP_DEFAULT_FLOOD_SCOPE (0x1C, ohne Typ-Byte).
    Layout: name(31) + key(16); leeres Frame = kein Default-Scope gesetzt."""
    if len(payload) < 31:
        return ""
    return payload[:31].split(b"\x00")[0].decode("utf-8", errors="replace")


def build_set_tx_power(power_dbm: int) -> bytes:
    """Setzt die LoRa-Sendeleistung am Node (persistiert via savePrefs).
    Gueltiger Bereich: -9..22 dBm (Heltec V4: MAX_LORA_TX_POWER = 22)."""
    power = max(-9, min(int(power_dbm), 22))
    return encode_frame(bytes([CMD_SET_RADIO_TX_POWER]) + struct.pack('b', power))


def build_reset_path(pubkey: bytes) -> bytes:
    """Setzt den Routing-Pfad eines Kontakts zurueck (Flooding-Neuentdeckung).
    Benoetigt den vollen 32-Byte-Pubkey (nicht nur den 6-Byte-Prefix)."""
    return encode_frame(bytes([CMD_RESET_PATH]) + pubkey[:32])


def build_send_txt(dst_pubkey_prefix: bytes, text: str, attempt: int = 0) -> bytes:
    ts = struct.pack('<I', int(time.time()))
    payload = (bytes([CMD_SEND_TXT_MSG, 0x00, attempt])   # 0x00 = TXT_TYPE_PLAIN
               + ts
               + dst_pubkey_prefix[:6]
               + text.encode('utf-8')[:150])   # Firmware-Limit: max 150 B Text pro DM
    return encode_frame(payload)


def build_send_channel_msg(channel_idx: int, text: str) -> bytes:
    ts = struct.pack('<I', int(time.time()))
    payload = bytes([CMD_SEND_CHANNEL_MSG, channel_idx, 0]) + ts + text.encode('utf-8')[:135]  # Kanal-Limit: 135 B
    return encode_frame(payload)


def build_set_channel(channel_idx: int, name: str) -> bytes:
    """Legt einen Kanal an oder aktualisiert ihn.
    PSK wird als SHA256(name)[:16] berechnet (Standard fuer #-Kanaele).
    """
    name_b = name.encode('utf-8').ljust(32, b'\x00')[:32]
    psk    = hashlib.sha256(name.encode('utf-8')).digest()[:16]
    payload = bytes([CMD_SET_CHANNEL, channel_idx]) + name_b + psk
    return encode_frame(payload)


CONTACT_TYPE_CLI = 0x01   # Companion/Chat-Client (normaler User)

def build_add_contact(pubkey_hex: str, name: str, path: bytes = b'') -> bytes:
    """Registriert einen Kontakt beim Node (Format wie meshcore_py update_contact).
    Layout:
      [1:33] pubkey  [33] type=1 (CLI)  [34] flags=0
      [35] out_path_len  [36:100] out_path (64 B, je Hop 6 Byte)
      [100:132] adv_name (32 B)  [132:136] last_advert  [136:144] lat/lon
    out_path_len=0xFF → Flooding (kein bekannter Pfad)
    """
    pubkey = bytes.fromhex(pubkey_hex)
    if path:
        n_hops       = min(len(path) // 6, 10)   # max 10 Hops à 6 Byte
        out_path_len = n_hops
        out_path     = path[:n_hops * 6].ljust(64, b'\x00')
    else:
        out_path_len = 0xFF                        # Flooding
        out_path     = b'\x00' * 64
    name_b  = name.encode('ascii', 'replace').ljust(32, b'\x00')[:32]
    ts      = struct.pack('<I', int(time.time()))
    lat_lon = struct.pack('<ii', 0, 0)
    payload = (bytes([CMD_ADD_UPDATE_CONTACT]) + pubkey
               + bytes([CONTACT_TYPE_CLI, 0, out_path_len]) + out_path + name_b + ts + lat_lon)
    return encode_frame(payload)


# ---------------------------------------------------------------------------
# Datenklassen fuer geparste Nachrichten
# ---------------------------------------------------------------------------

@dataclass
class IncomingMessage:
    pubkey_prefix: bytes    # 6 Byte – bei Direct-Msgs; leer bei Channel-Msgs
    text: str
    is_channel: bool = False
    channel_idx: int = 0
    snr: Optional[float] = None
    sender: str = ""        # Rufzeichen aus "NAME/CALL: " Prefix (Channel-Msgs)
    path: bytes = b''       # Routing-Pfad aus Channel-Frame (6 Byte Metadata)
    hop_count: int = 0      # Anzahl Flood-Hops (path_len & 0x3F, 0 = ohne Repeater)
    is_direct: bool = False # True = via bekannten Direktpfad zugestellt (path_len=0xFF)


@dataclass
class Contact:
    pubkey: bytes           # 32 Byte
    name: str
    path: bytes = b''       # Routing-Pfad (leer = direkt/unbekannt)
    type: int = 0           # ADV_TYPE_* (1=Chat, 2=Repeater, 3=Room, 4=Sensor)

    @property
    def pubkey_prefix(self) -> bytes:
        return self.pubkey[:6]

    @property
    def pubkey_hex(self) -> str:
        return self.pubkey.hex()


@dataclass
class TraceData:
    tag: int               # entspricht dem tag der Anfrage
    path_hashes: bytes     # rohe Hop-Hashes, hash_size Byte je Hop (NICHT 1 Byte je Hop!)
    path_snrs: list        # SNR (dB) je Hop, Laenge = hop_count + 1 (letzter = Hop zu uns)
    hash_size: int = 1     # Byte pro Hop-Hash, aus dem Flags-Byte der Antwort

    @property
    def hop_count(self) -> int:
        return len(self.path_hashes) // self.hash_size if self.hash_size else 0


# ---------------------------------------------------------------------------
# Payload-Parser
# ---------------------------------------------------------------------------

def parse_contact_msg(payload: bytes, v3: bool = False) -> Optional[IncomingMessage]:
    """Parst RESP_CONTACT_MSG (0x07) und RESP_CONTACT_MSG_V3 (0x10).

    V3-Layout: snr(1) + reserved(2) + pubkey_prefix(6) + path_len(1)
               + txt_type(1) + timestamp(4) + text
    V1-Layout: pubkey_prefix(6) + path_len(1) + txt_type(1) + timestamp(4) + text

    path_len: 0xFF = via bekannten Direktpfad zugestellt (MyMesh.cpp:
    pkt->isRouteFlood() ? pkt->path_len : 0xFF). Sonst bit-gepackt (Packet.h):
    Bits 7-6 = Path-Hash-Groesse minus 1 (getPathHashSize), Bits 5-0 = Anzahl
    Hops (getPathHashCount, path_len & 63). Kein Pfad-Puffer im Frame – die
    Hop-Adressen bleiben intern im Node.
    """
    try:
        offset = 0
        snr = None
        if v3:
            snr = struct.unpack_from('b', payload, offset)[0] / 4.0
            offset += 1 + 2           # snr(1) + reserved(2)
        pubkey_prefix = payload[offset:offset + 6]
        offset += 6
        path_len = payload[offset]
        offset += 1 + 1 + 4          # path_len(gelesen) + txt_type(1) + timestamp(4)
        text = payload[offset:].decode('utf-8', errors='replace').rstrip('\x00')
        if path_len == 0xFF:
            return IncomingMessage(pubkey_prefix=pubkey_prefix, text=text,
                                   snr=snr, is_direct=True)
        return IncomingMessage(pubkey_prefix=pubkey_prefix, text=text,
                               snr=snr, hop_count=path_len & 0x3F)
    except Exception:
        return None


def parse_channel_msg(payload: bytes, v3: bool = False) -> Optional[IncomingMessage]:
    """Parst RESP_CHANNEL_MSG (0x08) und RESP_CHANNEL_MSG_V3 (0x11).

    Beobachtetes Frame-Layout (nach dem Typ-Byte):
      channel_idx (1) + metadata (6) + text (Rest)
    Der Text enthaelt den Absender als "NAME/CALLSIGN: Nachricht".
    """
    try:
        offset = 0
        snr = None
        if v3:
            snr = struct.unpack_from('b', payload, offset)[0] / 4.0
            offset += 1
        channel_idx = payload[offset]
        offset += 1
        path = payload[offset:offset + 6]   # 6 Byte Routing-Pfad
        offset += 6
        raw_text = payload[offset:].decode('utf-8', errors='replace').rstrip('\x00')

        # Absender-Rufzeichen aus "NAME/CALLSIGN: Nachricht" extrahieren
        # Format: "DisplayName/Rufzeichen: Text" — Rufzeichen kann /P /M enthalten
        # z.B. "Martin/DL9MU" → "DL9MU", "JSP/DO6JSP/P" → "DO6JSP/P"
        sender = ""
        text = raw_text
        if ': ' in raw_text:
            prefix_part, _, text = raw_text.partition(': ')
            if '/' in prefix_part:
                sender = prefix_part.split('/', 1)[1].upper()
            else:
                sender = prefix_part.upper()

        return IncomingMessage(pubkey_prefix=b'\x00' * 6, text=text.strip(),
                               is_channel=True, channel_idx=channel_idx,
                               snr=snr, sender=sender, path=path)
    except Exception:
        return None


def parse_trace_data(payload: bytes) -> Optional[TraceData]:
    """Parst PUSH_CODE_TRACE_DATA (0x89, ohne Typ-Byte).
    Layout: reserved(1) + path_len(1) + flags(1) + tag(int32 LE) + auth_code(int32 LE)
            + path_hashes(path_len) + path_snrs(hop_count+1)

    path_len ist die BYTE-Laenge von path_hashes, NICHT die Hop-Anzahl! flags & 0x03
    = path_sz = log2(Byte/Hash) -> hash_size = 1<<path_sz (0=1B,1=2B,2=4B), analog zur
    path_len-Bit-Packung in parse_contact_msg. hop_count = path_len // hash_size.
    path_snrs: signed int8, SNR*4 → dB = wert/4. hop_count+1 Werte (letzter = Hop zu uns)."""
    try:
        if len(payload) < 11:
            return None
        path_len = payload[1]
        hash_size = 1 << (payload[2] & 0x03)
        tag = struct.unpack_from('<i', payload, 3)[0]
        i = 11
        path_hashes = payload[i:i + path_len]; i += path_len
        hop_count = path_len // hash_size if hash_size else 0
        snr_raw = payload[i:i + hop_count + 1]
        path_snrs = [struct.unpack_from('b', snr_raw, k)[0] / 4.0 for k in range(len(snr_raw))]
        return TraceData(tag=tag, path_hashes=path_hashes, path_snrs=path_snrs, hash_size=hash_size)
    except Exception:
        return None


def parse_contact(payload: bytes) -> Optional[Contact]:
    """Parst RESP_CONTACT (0x03).
    Layout: type(1) + pubkey(32) + contact_type(1) + flags(1) + path_len(1) + path(64) + name(32) + ...
    """
    try:
        if len(payload) < 132:
            return None
        pubkey       = payload[1:33]
        contact_type = payload[33]   # ADV_TYPE_* (1=Chat, 2=Repeater, 3=Room, 4=Sensor)
        # [34] = flags, [35] = out_path_len (signed int8): -1/0xFF = kein Pfad,
        # sonst Anzahl Bytes. out_path[36:100] = 1 Byte je Hop (Hash-Byte des Repeaters).
        path_len = struct.unpack_from('b', payload, 35)[0]   # signed
        path     = payload[36:36 + path_len] if 0 < path_len <= 64 else b''
        name_raw  = payload[100:132]
        name = name_raw.split(b'\x00')[0].decode('utf-8', errors='replace')
        # Advert-Name ist angreiferkontrolliert (beliebige Bytes ausser \x00) – Steuer-
        # zeichen/ANSI/Zeilenumbrueche hier an der Grenze entfernen, damit sie nicht in
        # Logs (Log-Injection) oder fremde Terminals gelangen. Emoji/Unicode bleiben.
        name = sanitize.for_log(name)
        return Contact(pubkey=pubkey, name=name, path=path, type=contact_type)
    except Exception:
        return None
