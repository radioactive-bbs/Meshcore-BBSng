from abc import ABC, abstractmethod


class BaseProtocol(ABC):
    """Basisklasse fuer alle Protokoll-Adapter."""

    @abstractmethod
    async def start(self):
        """Server/Verbindung starten."""

    @abstractmethod
    async def stop(self):
        """Server/Verbindung beenden."""
