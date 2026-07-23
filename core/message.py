from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.timeutil import now_utc


@dataclass
class Message:
    id: Optional[int]
    msg_type: str       # 'P' = Personal, 'B' = Bulletin
    to_call: str
    from_call: str
    subject: str
    body: str
    created_at: datetime = field(default_factory=now_utc)
    read: bool = False
    bid: Optional[str] = None   # Bulletin-ID für Forwarding
    sticky: bool = False        # Board-Nachricht von der Auto-Loeschung ausgenommen
    views: int = 0              # wie oft per R<id> gelesen (Board: mehrere User moeglich)
