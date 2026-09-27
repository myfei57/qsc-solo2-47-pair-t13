"""Read model for the calender stack."""

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ZoneView:
    """One calender nip: its order, thickness target and where it stands."""

    zone: str
    order: int
    target_low: float
    target_high: float
    pressure: float
    thickness: float
    verdict: str
    closed: bool
    accepted: bool
    attempts: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone": self.zone,
            "order": self.order,
            "target_low": self.target_low,
            "target_high": self.target_high,
            "pressure": self.pressure,
            "thickness": self.thickness,
            "verdict": self.verdict,
            "closed": self.closed,
            "accepted": self.accepted,
            "attempts": self.attempts,
        }


@dataclass(frozen=True)
class CalView:
    nip_closed: bool
    nip_pressure: float
    speed: float
    target_speed: float
    load: float
    verdict: str
    procedure: Mapping[str, Any]
    zones: Sequence[ZoneView]
    spool_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "nip_closed": self.nip_closed,
            "nip_pressure": self.nip_pressure,
            "speed": self.speed,
            "target_speed": self.target_speed,
            "load": self.load,
            "verdict": self.verdict,
            "procedure": dict(self.procedure),
            "zones": [zone.to_dict() for zone in self.zones],
            "spool_id": self.spool_id,
        }
