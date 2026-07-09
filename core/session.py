import asyncio
from datetime import datetime
from typing import Optional

from core.bbs import BBSCore
from core.user import User
from core import sanitize
from storage.database import Database

BANNER = r"""
+----------------------------------------------------------+
|                                                          |
|   _  _  _  _  ___      ___  ___  ___                    |
|  | \| || \| ||   \    | _ )| _ )/ __|                   |
|  | .` ||  . || |) |   | _ \| _ \\__ \                   |
|  |_|\_||_|\_||___/    |___/|___/|___/                   |
|                                                          |
|        Amateurfunk Bulletin Board System                 |
|           ~~ Wir sind auf Sendung ~~                     |
|                                                          |
+----------------------------------------------------------+"""


class BBSSession:
    def __init__(self, callsign: str, db: Database, writer: asyncio.StreamWriter,
                 active_sessions: dict, config: dict, notify_dm=None):
        self.callsign = callsign.upper()
        self.db = db
        self.writer = writer
        self.active_sessions = active_sessions
        self.config = config
        self.bbs = BBSCore(db, config, notify_dm=notify_dm)
        self.user: Optional[User] = None
        self._collecting_message = False
        self._awaiting_subject = False
        self._message_lines: list = []
        self._message_to = ""
        self._message_subject = ""
        self._message_type = "P"

    def send(self, text: str):
        # Steuerzeichen/ANSI entfernen, bevor nutzerkontrollierte Strings (Namen,
        # Betreff, Text – auch aus dem Mesh) in fremde Terminals geschrieben werden.
        text = sanitize.for_terminal(text)
        text = text.replace('\r\n', '\n').replace('\n', '\r\n')
        self.writer.write((text + "\r\n").encode())

    def send_lines(self, lines: list[str]):
        for line in lines:
            self.send(line)

    def send_prompt(self):
        bbs_call = self.config.get("callsign", "Meshcore BBSng")
        self.writer.write(f"\r\n{self.callsign} de {bbs_call}> ".encode())

    async def start(self):
        self.user = await self.db.get_or_create_user(self.callsign)
        self.user.last_seen = datetime.utcnow()
        await self.db.update_user(self.user)

        bbs_call = self.config.get("callsign", "Meshcore BBSng")
        sysop = self.config.get("sysop", "SYSOP")
        self.send(BANNER)
        self.send(f"  BBS:     {bbs_call}  |  SysOp: {sysop}  |  QTH: {self.config.get('qth', '-')}")
        self.send("+----------------------------------------------------------+")

        greeting = f"Hallo {self.callsign}"
        if self.user.name:
            greeting += f" ({self.user.name})"
        self.send(greeting + "!")

        msgs = await self.db.get_messages(self.callsign)
        new_msgs = [m for m in msgs if m.msg_type == "P" and m.to_call == self.callsign and not m.read]
        if new_msgs:
            self.send(f"Du hast {len(new_msgs)} neue Nachricht(en).")
        self.send("? fuer Hilfe")
        self.send_prompt()

    async def handle_input(self, line: str):
        line = line.strip()

        if self._collecting_message:
            await self._handle_message_input(line)
            return

        if not line:
            self.send_prompt()
            return

        parts = line.split(None, 1)
        cmd = parts[0].upper()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("B", "BYE", "Q", "QUIT"):
            bbs_call = self.config.get("callsign", "Meshcore BBSng")
            self.send(f"73! TU {bbs_call} de {self.callsign}")
            self.writer.close()
            return

        elif cmd in ("H", "?", "HELP"):
            self.send_lines(await self.bbs.cmd_help(self.callsign))

        elif cmd in ("I", "INFO"):
            self.send_lines(await self.bbs.cmd_info(len(self.active_sessions)))

        elif cmd == "L":
            self.send_lines(await self.bbs.cmd_list())

        elif cmd == "BL":
            self.send_lines(await self.bbs.cmd_list_board())

        elif cmd == "BLO":
            if not arg.isdigit():
                self.send("Verwendung: BLO <Zahl>")
            else:
                self.send_lines(await self.bbs.cmd_list_board(int(arg)))

        elif cmd == "NL":
            self.send_lines(await self.bbs.cmd_list_personal(self.callsign))

        elif cmd == "NLO":
            if not arg.isdigit():
                self.send("Verwendung: NLO <Zahl>")
            else:
                self.send_lines(await self.bbs.cmd_list_personal(self.callsign, int(arg)))

        elif cmd == "R":
            if not arg.isdigit():
                self.send("Verwendung: R <Nummer>")
            else:
                self.send_lines(await self.bbs.cmd_read(self.callsign, int(arg)))

        elif cmd in ("S", "SP"):
            await self._start_send_personal(arg)
            return

        elif cmd == "SB":
            await self._start_send_bulletin(arg)
            return

        elif cmd in ("K", "KM"):
            if not arg.isdigit():
                self.send("Verwendung: K <Nummer>")
            else:
                self.send_lines(await self.bbs.cmd_kill(self.callsign, int(arg)))

        elif cmd == "N":
            await self._cmd_setname(arg)

        elif cmd == "W":
            self.send_lines(await self.bbs.cmd_who(list(self.active_sessions.keys())))

        else:
            self.send(f"Unbekannter Befehl: {cmd}. ? fuer Hilfe.")

        self.send_prompt()

    async def _start_send_personal(self, arg: str):
        if not arg:
            self.send("Verwendung: S <Rufzeichen>")
            self.send_prompt()
            return
        self._message_to = arg.upper()
        self._message_type = "P"
        self._message_lines = []
        self._awaiting_subject = True
        self._collecting_message = True
        self.send(f"Nachricht an {self._message_to}")
        self.writer.write(b"Betreff: ")

    async def _start_send_bulletin(self, arg: str):
        if not arg:
            self.send("Verwendung: SB <Thema>")
            self.send_prompt()
            return
        self._message_to = "ALL"
        self._message_subject = arg
        self._message_type = "B"
        self._message_lines = []
        self._awaiting_subject = False
        self._collecting_message = True
        self.send(f"Bulletin an alle, Thema: {arg}")
        self.send("Text eingeben. /EX zum Senden, /ABORT abbrechen:")

    async def _handle_message_input(self, line: str):
        if self._awaiting_subject:
            self._message_subject = line
            self._awaiting_subject = False
            self.send("Text eingeben. /EX zum Senden, /ABORT abbrechen:")
            return

        if line.upper() in ("/EX", "***", "/SEND"):
            body = "\r\n".join(self._message_lines)
            if self._message_type == "P":
                result = await self.bbs.cmd_send(
                    self.callsign, self._message_to, self._message_subject, body
                )
            else:
                result = await self.bbs.cmd_bulletin(
                    self.callsign, self._message_subject, body
                )
            self.send_lines(result)
            self._collecting_message = False
            self._message_lines = []
            self.send_prompt()

        elif line.upper() == "/ABORT":
            self.send("Abgebrochen.")
            self._collecting_message = False
            self._message_lines = []
            self.send_prompt()

        else:
            self._message_lines.append(line)

    async def _cmd_setname(self, arg: str):
        if not self.user:
            return
        if not arg:
            self.send(f"Aktueller Name: {self.user.name or '(nicht gesetzt)'}")
            return
        self.user.name = arg
        await self.db.update_user(self.user)
        self.send(f"Name gesetzt: {arg}")
