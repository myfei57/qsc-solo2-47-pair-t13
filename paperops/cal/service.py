"""Calender stack control.

The stack is several nips rather than one coarse load. The nips close in a fixed
order (nip1, nip2, nip3); a nip may only close once every earlier nip is closed
and the machine speed has settled on the dry section's durable figure. A closed
nip then has to prove itself: a measured calendered thickness has to land inside
that nip's own target range, and until it does the closing procedure is held on
that nip, so a thin or thick nip can never be pushed through to the next one.

Every measurement is written to the record stream under the spool that was being
wound, so a finished reel can later be asked how each nip judged its paper.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from ..clock import LogicalClock
from ..errors import (
    DuplicateStageError,
    OutOfRangeError,
    OutOfSequenceError,
    PreGateError,
    UnknownKeyError,
    UnknownStageError,
)
from ..event import EventBus, topics
from ..interlock import PreGate, StageSequence
from ..store import Journal, keys
from ..verdict import Band, HysteresisBand
from . import calc
from .view import CalView, ZoneView

NIP_BAND = Band(100.0, 800.0)

PROCEDURE_NAME = "cal.procedure"
ZONE_1 = "nip1"
ZONES = (
    # Name, order, lower thickness target, upper thickness target.
    (ZONE_1, 0.110, 0.130),
    ("nip2", 0.090, 0.108),
    ("nip3", 0.072, 0.088),
)

UNMEASURED = "unmeasured"
CLOSE = "close"
MEASURE = "measure"
OPEN = "open"


@dataclass(frozen=True)
class ZoneSpec:
    """A nip's fixed place in the stack and its thickness target range."""

    name: str
    order: int
    target_low: float
    target_high: float


@dataclass
class ZoneState:
    """Where one nip currently stands in the closing procedure."""

    spec: ZoneSpec
    pressure: float = 0.0
    thickness: float = 0.0
    verdict: str = UNMEASURED
    closed: bool = False
    accepted: bool = False
    attempts: int = 0


class SpeedSource(Protocol):
    """Anything that can report the speed the dry section settled on."""

    def speed(self) -> float: ...


class CalService:
    """Multi-zone calender loading, thickness gating and reel verdicts."""

    def __init__(self, journal: Journal, bus: EventBus, clock: LogicalClock) -> None:
        self._journal = journal
        self._bus = bus
        self._clock = clock
        self._band = HysteresisBand(NIP_BAND.low, NIP_BAND.high, 10.0)
        self._dry_speed_gate = PreGate("cal.nip", journal, keys.DRY_SPEED)
        specs = self._build_specs()
        self._procedure = StageSequence(PROCEDURE_NAME, tuple(spec.name for spec in specs))
        self._zones: dict[str, ZoneState] = {spec.name: ZoneState(spec) for spec in specs}
        self._nip_closed = False
        self._nip_pressure = 0.0
        self._speed = 0.0
        self._target_speed = 0.0
        self._active_spool = ""

    @staticmethod
    def _build_specs() -> tuple[ZoneSpec, ...]:
        return tuple(
            ZoneSpec(name=name, order=index + 1, target_low=low, target_high=high)
            for index, (name, low, high) in enumerate(ZONES)
        )

    # ----------------------------------------------------------------- reading

    def nip_closed(self) -> bool:
        return self._nip_closed

    def nip_pressure(self) -> float:
        return self._nip_pressure

    def speed(self) -> float:
        return self._speed

    def target_speed(self) -> float:
        return self._target_speed

    def load(self) -> float:
        return calc.nip_load(self._nip_pressure, 6.0)

    def verdict(self) -> str:
        self._band.classify(self.load())
        return self._band.last

    def procedure(self) -> dict[str, Any]:
        return self._procedure.state()

    def zone_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._build_specs())

    def zone(self, name: str) -> ZoneState:
        state = self._zones.get(name)
        if state is None:
            raise UnknownStageError(f"{PROCEDURE_NAME} does not declare zone {name}")
        return state

    def zones(self) -> tuple[ZoneState, ...]:
        return tuple(self._zones.values())

    def zone_view(self, name: str) -> ZoneView:
        return self._zone_view(self.zone(name))

    def active_spool(self) -> str:
        return self._active_spool

    def spool_verdict(self, spool_id: str) -> dict[str, Any]:
        """The per-zone thickness verdict frozen while a spool was being wound."""
        record = self._journal.read(keys.cal_reel_key(spool_id))
        if record is None:
            raise UnknownKeyError(f"no calender verdict recorded for reel {spool_id}")
        return dict(record)

    def view(self) -> CalView:
        return CalView(
            nip_closed=self._nip_closed,
            nip_pressure=self._nip_pressure,
            speed=self._speed,
            target_speed=self._target_speed,
            load=self.load(),
            verdict=self.verdict(),
            procedure=self._procedure.state(),
            zones=tuple(self._zone_view(state) for state in self._zones.values()),
            spool_id=self._active_spool,
        )

    # ------------------------------------------------------------------ writes

    def set_speed(self, speed: float, target: float) -> dict[str, Any]:
        """Record the thread speed and where the drive is heading."""
        if speed < 0 or target < 0:
            raise PreGateError("cal: speed must not be negative")
        tick = self._clock.tick
        row = {"speed": float(speed), "target": float(target), "tick": tick}
        with self._journal.transaction("cal-speed") as batch:
            self._journal.append(keys.CAL_SPEED, row, batch)
        self._speed = float(speed)
        self._target_speed = float(target)
        self._bus.publish(topics.TOPIC_CAL_SPEED, self._speed)
        return dict(row)

    def close_nip(self, dry: SpeedSource, pressure: float) -> dict[str, Any]:
        """Backwards-compatible coarse close: load the first zone of the stack."""
        return self.close_zone(ZONE_1, dry, pressure)

    def close_zone(self, zone: str, dry: SpeedSource, pressure: float) -> dict[str, Any]:
        """Close one nip in the fixed order; speed has to be durable and settled."""
        state = self.zone(zone)
        self._require_closable(state)
        self._require_speed_ready(dry)
        self._band.require(calc.nip_load(pressure, 6.0), f"calender {zone} load")
        tick = self._clock.tick
        row = {
            "kind": CLOSE,
            "zone": zone,
            "closed": True,
            "nip_closed": zone == ZONE_1,
            "pressure": float(pressure),
            "speed": self._speed,
            "tick": tick,
        }
        with self._journal.transaction("cal-zone-close") as batch:
            self._journal.append(keys.cal_zone_key(zone), row, batch)
            if zone == ZONE_1:
                self._journal.append(
                    keys.CAL_NIP,
                    {"pressure": float(pressure), "speed": self._speed, "tick": tick},
                    batch,
                )
        state.closed = True
        state.pressure = float(pressure)
        if zone == ZONE_1:
            self._nip_closed = True
            self._nip_pressure = float(pressure)
        self._bus.publish(
            topics.TOPIC_CAL_ZONE,
            {"zone": zone, "closed": True, "pressure": float(pressure), "tick": tick},
        )
        if zone == ZONE_1:
            self._bus.publish(topics.TOPIC_CAL_NIP, True)
        return dict(row)

    def measure_zone(self, zone: str, thickness: float) -> dict[str, Any]:
        """Judge a closed nip's thickness against its own target range.

        The measurement is made durable either way; when it sits outside the
        band the procedure stays parked on this nip and the next nip stays shut.
        """
        state = self.zone(zone)
        if not state.closed:
            raise PreGateError(f"cal: zone {zone} is not closed")
        if state.accepted:
            raise DuplicateStageError(f"cal: zone {zone} already accepted")
        state.attempts += 1
        verdict = calc.thickness_verdict(
            thickness, state.spec.target_low, state.spec.target_high
        )
        accepted = verdict == calc.IN_BAND
        tick = self._clock.tick
        row = {
            "kind": MEASURE,
            "zone": zone,
            "thickness": float(thickness),
            "verdict": verdict,
            "accepted": accepted,
            "attempt": state.attempts,
            "spool_id": self._active_spool,
            "tick": tick,
        }
        with self._journal.transaction("cal-zone-measure") as batch:
            self._journal.append(keys.cal_zone_key(zone), row, batch)
        state.thickness = float(thickness)
        state.verdict = verdict
        self._bus.publish(topics.TOPIC_CAL_ZONE, dict(row))
        if not accepted:
            raise PreGateError(
                f"cal: zone {zone} thickness {float(thickness)} is {verdict}, "
                f"target [{state.spec.target_low}, {state.spec.target_high}]"
            )
        state.accepted = True
        self._procedure.advance(zone)
        return dict(row)

    def open_nip(self) -> dict[str, Any]:
        """Backwards-compatible coarse open: release the loaded side of the stack."""
        closed = [state for state in self._zones.values() if state.closed]
        if not closed:
            tick = self._clock.tick
            with self._journal.transaction("cal-nip") as batch:
                self._journal.append(
                    keys.CAL_NIP,
                    {"pressure": 0.0, "nip_closed": False, "tick": tick},
                    batch,
                )
            self._bus.publish(topics.TOPIC_CAL_NIP, False)
            return {"nip_closed": False, "tick": tick}
        return self.open_zone(closed[-1].spec.name)

    def open_zone(self, zone: str | None = None) -> dict[str, Any]:
        """Open a closed nip; only the nip furthest down the stack may be released."""
        if zone is None:
            closed = [state for state in self._zones.values() if state.closed]
            if not closed:
                raise PreGateError("cal: no zone is closed")
            zone = closed[-1].spec.name
        state = self.zone(zone)
        if not state.closed:
            raise PreGateError(f"cal: zone {zone} is not closed")
        for later in tuple(self._zones.values())[state.spec.order :]:
            if later.closed:
                raise OutOfSequenceError(
                    f"cal: cannot open {zone} while {later.spec.name} is still closed"
                )
        tick = self._clock.tick
        first_nip = zone == ZONE_1
        row = {
            "kind": OPEN,
            "zone": zone,
            "closed": False,
            "nip_closed": not first_nip,
            "tick": tick,
        }
        with self._journal.transaction("cal-zone-open") as batch:
            self._journal.append(keys.cal_zone_key(zone), row, batch)
            if first_nip:
                self._journal.append(
                    keys.CAL_NIP,
                    {"pressure": 0.0, "nip_closed": False, "tick": tick},
                    batch,
                )
        self._reset_zone(state)
        self._rebuild_procedure()
        self._bus.publish(topics.TOPIC_CAL_ZONE, dict(row))
        if zone == ZONE_1:
            self._bus.publish(topics.TOPIC_CAL_NIP, False)
        return dict(row)

    def freeze_spool(self, spool_id: str) -> dict[str, Any]:
        """Snapshot every zone's verdict for the spool currently being wound."""
        tick = self._clock.tick
        row = {
            "spool_id": spool_id,
            "tick": tick,
            "zones": [self._zone_view(state).to_dict() for state in self._zones.values()],
        }
        with self._journal.transaction("cal-reel-freeze") as batch:
            self._journal.append(keys.cal_reel_key(spool_id), row, batch)
        self._active_spool = spool_id
        self._bus.publish(topics.TOPIC_CAL_REEL, {"spool_id": spool_id, "tick": tick})
        return dict(row)

    def wire(self) -> None:
        """Follow spool transfers so each reel carries a frozen per-zone verdict."""
        self._bus.subscribe(topics.TOPIC_REEL_SPOOL, self._on_spool_loaded)

    def reload(self) -> CalView:
        speed = self._journal.read(keys.CAL_SPEED)
        if speed is not None:
            self._speed = float(speed["speed"])
            self._target_speed = float(speed["target"])
        for state in self._zones.values():
            for entry in self._journal.history(keys.cal_zone_key(state.spec.name)):
                self._replay_zone(state, entry.value)
        self._rebuild_procedure()
        nip = self._journal.read(keys.CAL_NIP)
        if nip is not None:
            self._nip_pressure = float(nip["pressure"])
            self._nip_closed = bool(nip.get("nip_closed", self._nip_pressure > 0.0))
        spool = self._journal.read(keys.REEL_SPOOL)
        if spool is not None:
            self._active_spool = str(spool["spool_id"])
        return self.view()

    # -------------------------------------------------------------- arithmetic

    def nip_load(self, width: float) -> float:
        return calc.nip_load(self._nip_pressure, width)

    def nip_impulse(self, dwell: float) -> float:
        return calc.nip_impulse(self._nip_pressure, dwell)

    def speed_difference(self) -> float:
        return calc.speed_difference(self._speed, self._target_speed)

    def settled(self, tolerance: float = 1.0) -> bool:
        return calc.settled(self._speed, self._target_speed, tolerance)

    def nip_verdict(self) -> str:
        return calc.nip_verdict(self.load(), self._band.low, self._band.high)

    # ---------------------------------------------------------------- internals

    def _require_closable(self, state: ZoneState) -> None:
        if state.closed:
            raise DuplicateStageError(f"cal: zone {state.spec.name} is already closed")
        for earlier in tuple(self._zones.values())[: state.spec.order - 1]:
            if not earlier.accepted:
                raise OutOfSequenceError(
                    f"cal cannot close {state.spec.name} before {earlier.spec.name} "
                    "holds its thickness band"
                )

    def _require_speed_ready(self, dry: SpeedSource) -> None:
        self._dry_speed_gate.require()
        speed = dry.speed()
        self._speed = speed
        if not calc.settled(speed, self._target_speed):
            raise PreGateError("cal: the machine speed has not settled")

    def _zone_view(self, state: ZoneState) -> ZoneView:
        return ZoneView(
            zone=state.spec.name,
            order=state.spec.order,
            target_low=state.spec.target_low,
            target_high=state.spec.target_high,
            pressure=state.pressure,
            thickness=state.thickness,
            verdict=state.verdict,
            closed=state.closed,
            accepted=state.accepted,
            attempts=state.attempts,
        )

    def _reset_zone(self, state: ZoneState) -> None:
        state.closed = False
        state.accepted = False
        state.pressure = 0.0
        state.thickness = 0.0
        state.verdict = UNMEASURED
        state.attempts = 0
        if state.spec.name == ZONE_1:
            self._nip_closed = False
            self._nip_pressure = 0.0

    def _rebuild_procedure(self) -> None:
        """Re-derive the ordered procedure from the zones that qualified."""
        self._procedure.reset()
        for state in self._zones.values():
            if state.accepted:
                self._procedure.advance(state.spec.name)

    def _replay_zone(self, state: ZoneState, row: dict[str, Any]) -> None:
        kind = row.get("kind")
        if kind == CLOSE:
            state.closed = True
            state.pressure = float(row["pressure"])
        elif kind == MEASURE:
            state.attempts = int(row["attempt"])
            state.thickness = float(row["thickness"])
            state.verdict = str(row["verdict"])
            state.accepted = bool(row["accepted"])
        elif kind == OPEN:
            self._reset_zone(state)
        else:  # legacy single-nip rows carry no kind
            state.closed = float(row.get("pressure", 0.0)) > 0.0
            state.pressure = float(row.get("pressure", 0.0))
            state.verdict = calc.nip_verdict(
                calc.nip_load(state.pressure, 6.0), NIP_BAND.low, NIP_BAND.high
            )

    def _on_spool_loaded(self, event: Any) -> None:
        spool_id = str(event.payload)
        self.freeze_spool(spool_id)
