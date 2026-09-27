"""Read model for the calender stack."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NipView:
    """One nip: its position, last loading, reading and thickness target band."""

    name: str
    closed: bool
    pressure: float
    thickness: float
    verdict: str
    low: float
    high: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "closed": self.closed,
            "pressure": self.pressure,
            "thickness": self.thickness,
            "verdict": self.verdict,
            "low": self.low,
            "high": self.high,
        }


@dataclass(frozen=True)
class CalView:
    nips: tuple[NipView, ...]
    all_closed: bool
    next_nip: str
    speed: float
    target_speed: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "nip_closed": self.all_closed,
            "next_nip": self.next_nip,
            "nips": [nip.to_dict() for nip in self.nips],
            "speed": self.speed,
            "target_speed": self.target_speed,
        }
