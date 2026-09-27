from __future__ import annotations

from typing import Optional


class TargetError(RuntimeError):
    pass


class Target:
    """A channel the shopper talks to. One instance = one conversation."""

    name: str = "target"
    team: Optional[str] = None  # team or script version label, never an individual

    def start(self) -> Optional[str]:
        """Open the conversation. May return the channel's greeting."""
        return None

    def send(self, message: str) -> str:
        """Send one shopper message and return the channel's reply."""
        raise NotImplementedError

    def close(self) -> None:
        pass
