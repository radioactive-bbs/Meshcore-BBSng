from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class User:
    callsign: str
    name: str = ""
    last_seen: Optional[datetime] = None
    message_count: int = 0
