import asyncio
import logging

from core.session import BBSSession
from core import sanitize
from core.validation import USERNAME_RE
from protocols.base import BaseProtocol
from storage.database import Database

logger = logging.getLogger(__name__)

# Telnet-Protokoll-Konstanten (RFC 854)
_IAC  = 0xFF
_WILL = 0xFB
_WONT = 0xFC
_DO   = 0xFD
_DONT = 0xFE
_SB   = 0xFA
_SE   = 0xF0

# Rufzeichen/Name: dieselbe Regel wie die Kanal-Registrierung (core/validation.py).
# Verhindert, dass Steuerzeichen/ANSI/Leerzeichen ueber den Namen in Logs oder
# fremde Terminals gelangen (Who-Liste, Prompts).

# Max. Zeilenlaenge ohne Zeilenende – schuetzt vor unbegrenztem Pufferwachstum
# (ein Client, der endlos ohne \n sendet, wuerde sonst den Speicher fluten).
_MAX_LINE = 4096


class TelnetStream:
    """Filtert Telnet IAC-Negotiation-Sequenzen aus dem Datenstrom
    und antwortet automatisch mit DONT/WONT auf alle Client-Anfragen."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._buf = bytearray()

    async def negotiate(self):
        """Initiale Negotiation: Echo und Suppress-Go-Ahead aktivieren,
        dann kurz warten um Client-Antworten aufzusaugen."""
        self._writer.write(bytes([
            _IAC, _WILL, 0x01,  # WILL ECHO
            _IAC, _WILL, 0x03,  # WILL Suppress Go Ahead
        ]))
        await self._writer.drain()
        try:
            chunk = await asyncio.wait_for(self._reader.read(128), timeout=0.5)
            self._buf.extend(self._filter(chunk))
            await self._writer.drain()
        except asyncio.TimeoutError:
            pass

    async def readline(self) -> bytes:
        """Liest eine Zeile, bereinigt von IAC-Sequenzen. Ueberschreitet der Puffer
        _MAX_LINE ohne Zeilenende, wird die Verbindung getrennt (DoS-Schutz)."""
        while True:
            for sep in (b'\r\n', b'\n', b'\r'):
                if sep in self._buf:
                    idx = self._buf.index(sep)
                    line = bytes(self._buf[:idx]) + b'\n'
                    del self._buf[:idx + len(sep)]
                    return line
            if len(self._buf) > _MAX_LINE:
                self._buf.clear()
                return b''   # ueberlange Zeile -> Verbindung schliessen
            chunk = await self._reader.read(256)
            if not chunk:
                return b''
            self._buf.extend(self._filter(chunk))
            await self._writer.drain()

    def _filter(self, data: bytes) -> bytearray:
        """Entfernt IAC-Sequenzen und sammelt Antworten."""
        result = bytearray()
        responses = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b == _IAC:
                if i + 1 >= len(data):
                    i += 1
                    continue
                cmd = data[i + 1]
                if cmd in (_WILL, _WONT) and i + 2 < len(data):
                    responses += bytes([_IAC, _DONT, data[i + 2]])
                    i += 3
                elif cmd in (_DO, _DONT) and i + 2 < len(data):
                    responses += bytes([_IAC, _WONT, data[i + 2]])
                    i += 3
                elif cmd == _SB:
                    i += 2
                    while i < len(data) - 1:
                        if data[i] == _IAC and data[i + 1] == _SE:
                            i += 2
                            break
                        i += 1
                elif cmd == _IAC:
                    result.append(_IAC)
                    i += 2
                else:
                    i += 2
            else:
                result.append(b)
                i += 1
        if responses:
            self._writer.write(responses)
        return result


class TelnetServer(BaseProtocol):
    def __init__(self, db: Database, config: dict):
        self.db = db
        self.config = config
        self.active_sessions: dict = {}
        self._server: asyncio.AbstractServer = None

    async def start(self):
        host = self.config.get("telnet", {}).get("host", "0.0.0.0")
        port = self.config.get("telnet", {}).get("port", 6300)
        self._server = await asyncio.start_server(self._handle_client, host, port)
        logger.info("Telnet-Server gestartet auf %s:%s", host, port)

    async def stop(self):
        for session in list(self.active_sessions.values()):
            try:
                session.writer.write(b"\r\nServer wird heruntergefahren...\r\n")
                await session.writer.drain()
                session.writer.close()
            except Exception:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Telnet-Server gestoppt.")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        logger.info("Neue Verbindung von %s", addr)
        callsign = ""

        stream = TelnetStream(reader, writer)
        await stream.negotiate()

        writer.write(b"Callsign: ")
        await writer.drain()

        try:
            line = await asyncio.wait_for(stream.readline(), timeout=30)
            callsign = line.decode(errors="replace").strip()
        except asyncio.TimeoutError:
            writer.write(b"Timeout.\r\n")
            writer.close()
            return

        # Gleiche Regel wie die Kanal-Registrierung (core/validation.py): vor dem
        # Uppercasing gegen den erlaubten Zeichensatz pruefen.
        if not USERNAME_RE.match(callsign):
            writer.write("Ungultiger Name (3-16 Zeichen; Buchstaben, Ziffern, +-.!\"§$%&/()=).\r\n"
                         .encode("utf-8"))
            writer.close()
            return
        callsign = callsign.upper()

        session = BBSSession(callsign, self.db, writer, self.active_sessions, self.config)
        self.active_sessions[callsign] = session

        try:
            await session.start()
            await writer.drain()

            while not writer.is_closing():
                try:
                    line = await asyncio.wait_for(stream.readline(), timeout=600)
                    if not line:
                        break
                    await session.handle_input(line.decode(errors="replace"))
                    await writer.drain()
                except asyncio.TimeoutError:
                    writer.write(b"\r\nTimeout - Verbindung getrennt.\r\n")
                    break
        except ConnectionResetError:
            pass
        finally:
            self.active_sessions.pop(callsign, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("Verbindung getrennt: %s (%s)", sanitize.for_log(callsign), addr)
