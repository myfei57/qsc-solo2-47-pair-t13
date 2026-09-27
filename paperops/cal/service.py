"""Calender stack control.

The stack is a fixed train of nips that close in a declared order. Each nip
carries its own thickness target band: a nip only closes when the measured sheet
thickness lands inside its band, every attempt is judged and remembered, and a
nip that has not made its target blocks the rest of the train. The all-closed
record stays a pre-gate for the reel, and each new spool freezes the per-nip
verdicts so a finished roll can be audited later.
"""

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

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
from ..interlock.sequence import COMPLETE
from ..store import Journal, keys
from ..verdict import WITHIN, Band, VerdictHistory
from . import calc
from .view import CalView, NipView

NIP_BAND = Band(100.0, 800.0)
SEQUENCE_NAME = "cal.nips"
OPEN = "open"


@dataclass(frozen=True)
class NipSpec:
    """One nip in the train: its name and its sheet thickness target band."""

    name: str
    band: Band


DEFAULT_NIPS: tuple[NipSpec, ...] = (
    NipSpec("nip1", Band(0.088, 0.100)),
    NipSpec("nip2", Band(0.078, 0.088)),
    NipSpec("nip3", Band(0.068, 0.078)),
)


class SpeedSource(Protocol):
    """Anything that can report the speed the dry section settled on."""

    def speed(self) -> float: ...


class _NipState:
    """Runtime position of one nip."""

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self.closed = False
        self.pressure = 0.0
        self.thickness = 0.0
        self.verdict = OPEN


class CalService:
    """Ordered calender nips, per-nip thickness targets and roll verdict records."""

    def __init__(
        self,
        journal: Journal,
        bus: EventBus,
        clock: LogicalClock,
        nips: Sequence[NipSpec] = DEFAULT_NIPS,
    ) -> None:
        if not nips:
            raise ValueError("the calender stack needs at least one nip")
        self._journal = journal
        self._bus = bus
        self._clock = clock
        self._specs = tuple(nips)
        names = [spec.name for spec in self._specs]
        self._sequence = StageSequence(SEQUENCE_NAME, names)
        self._dry_speed_gate = PreGate("cal.nip", journal, keys.DRY_SPEED)
        self._states = {spec.name: _NipState() for spec in self._specs}
        self._histories = {
            spec.name: VerdictHistory(f"cal.nip.{spec.name}") for spec in self._specs
        }
        self._rolls: dict[str, dict[str, Any]] = {}
        self._speed = 0.0
        self._target_speed = 0.0

    # ----------------------------------------------------------------- reading

    def nip_closed(self) -> bool:
        """True only when every nip in the train is closed."""
        return self._sequence.next_required() is None

    def next_nip(self) -> str:
        return self._sequence.next_required() or COMPLETE

    def nip_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._specs)

    def nip_state(self, name: str) -> NipView:
        return self._nip_view(self._spec(name))

    def nip_history(self, name: str) -> VerdictHistory:
        self._spec(name)
        return self._histories[name]

    def nip_histories(self) -> dict[str, VerdictHistory]:
        """The verdict history of every nip, keyed by history subject."""
        return {history.subject: history for history in self._histories.values()}

    def speed(self) -> float:
        return self._speed

    def target_speed(self) -> float:
        return self._target_speed

    def load(self) -> float:
        return calc.nip_load(self._last_pressure(), 6.0)

    def verdict(self) -> str:
        return "in_band" if self.nip_closed() else OPEN

    def view(self) -> CalView:
        return CalView(
            nips=tuple(self._nip_view(spec) for spec in self._specs),
            all_closed=self.nip_closed(),
            next_nip=self.next_nip(),
            speed=self._speed,
            target_speed=self._target_speed,
        )

    def rolls(self) -> tuple[str, ...]:
        return tuple(sorted(self._rolls))

    def roll_verdicts(self, spool_id: str) -> dict[str, Any]:
        """The per-nip verdicts frozen for one roll."""
        row = self._rolls.get(spool_id)
        if row is None:
            stored = self._journal.read(keys.cal_roll_key(spool_id))
            if stored is None:
                raise UnknownKeyError(f"no roll record for {spool_id}")
            row = stored
        return dict(row)

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

    def close_nip(
        self,
        dry: SpeedSource,
        name: str,
        pressure: float,
        thickness: float,
    ) -> dict[str, Any]:
        """Close one nip; the train only advances in order and on its thickness target."""
        self._require_next(name)
        spec = self._spec(name)
        self._dry_speed_gate.require()
        speed = dry.speed()
        self._speed = speed
        if not calc.settled(speed, self._target_speed):
            raise PreGateError("cal: the machine speed has not settled")
        verdict = spec.band.classify(thickness)
        self._histories[name].record(
            verdict,
            self._journal.watermark,
            self._clock.tick,
            f"thickness={thickness}",
        )
        if verdict != WITHIN:
            raise OutOfRangeError(
                f"cal: {name} thickness {thickness} is outside "
                f"[{spec.band.low}, {spec.band.high}]"
            )
        NIP_BAND.require(calc.nip_load(pressure, 6.0), "calender nip load")
        self._sequence.advance(name)
        tick = self._clock.tick
        row = {
            "nip": name,
            "closed": True,
            "pressure": float(pressure),
            "thickness": float(thickness),
            "verdict": verdict,
            "speed": speed,
            "tick": tick,
        }
        with self._journal.transaction("cal-nip") as batch:
            self._journal.append(keys.cal_nip_key(name), row, batch)
            if self.nip_closed():
                self._journal.append(
                    keys.CAL_NIP,
                    {
                        "closed": True,
                        "nips": list(self._sequence.completed()),
                        "tick": tick,
                    },
                    batch,
                )
        state = self._states[name]
        state.closed = True
        state.pressure = float(pressure)
        state.thickness = float(thickness)
        state.verdict = verdict
        self._bus.publish(topics.TOPIC_CAL_NIP, {"nip": name, "closed": True})
        return dict(row)

    def open_nip(self, name: str) -> dict[str, Any]:
        """Open one nip; every later nip in the train opens with it."""
        self._spec(name)
        names = [spec.name for spec in self._specs]
        index = names.index(name)
        opened = [candidate for candidate in names[index:] if self._states[candidate].closed]
        tick = self._clock.tick
        with self._journal.transaction("cal-nip") as batch:
            for opened_name in opened:
                self._journal.append(
                    keys.cal_nip_key(opened_name),
                    {"nip": opened_name, "closed": False, "pressure": 0.0, "tick": tick},
                    batch,
                )
            self._journal.append(
                keys.CAL_NIP,
                {"closed": False, "nip": name, "tick": tick},
                batch,
            )
        for opened_name in opened:
            self._states[opened_name].clear()
        self._sequence.reset()
        for earlier in names[:index]:
            if self._states[earlier].closed:
                self._sequence.advance(earlier)
        self._bus.publish(topics.TOPIC_CAL_NIP, {"nip": name, "closed": False})
        return {"opened": opened, "nip_closed": self.nip_closed(), "tick": tick}

    def freeze_roll(self, spool_id: str) -> dict[str, Any]:
        """Freeze the current per-nip verdicts as the record of one roll."""
        tick = self._clock.tick
        row: dict[str, Any] = {
            "spool_id": spool_id,
            "nips": {spec.name: self._nip_report(spec.name) for spec in self._specs},
            "watermark": self._journal.watermark,
            "tick": tick,
        }
        with self._journal.transaction("cal-roll") as batch:
            self._journal.append(keys.cal_roll_key(spool_id), row, batch)
        self._rolls[spool_id] = row
        self._bus.publish(topics.TOPIC_CAL_ROLL, spool_id)
        return dict(row)

    def reload(self) -> CalView:
        """Restore the train, speed and roll records from the committed stream."""
        speed = self._journal.read(keys.CAL_SPEED)
        if speed is not None:
            self._speed = float(speed["speed"])
            self._target_speed = float(speed["target"])
        self._sequence.reset()
        for state in self._states.values():
            state.clear()
        for spec in self._specs:
            row = self._journal.read(keys.cal_nip_key(spec.name))
            if row is None or not bool(row.get("closed", False)):
                break
            state = self._states[spec.name]
            state.closed = True
            state.pressure = float(row.get("pressure", 0.0))
            state.thickness = float(row.get("thickness", 0.0))
            state.verdict = str(row.get("verdict", WITHIN))
            self._sequence.advance(spec.name)
        self._rolls = {}
        for key in self._journal.keys(keys.CAL_ROLL_PREFIX):
            row = self._journal.read(key)
            self._rolls[str(row["spool_id"])] = row
        return self.view()

    def wire(self) -> None:
        """Freeze the per-nip verdicts whenever a new spool starts a roll."""
        self._bus.subscribe(topics.TOPIC_REEL_SPOOL, self._on_spool)

    # -------------------------------------------------------------- arithmetic

    def nip_load(self, width: float, nip: str | None = None) -> float:
        return calc.nip_load(self._pressure_of(nip), width)

    def nip_impulse(self, dwell: float, nip: str | None = None) -> float:
        return calc.nip_impulse(self._pressure_of(nip), dwell)

    def speed_difference(self) -> float:
        return calc.speed_difference(self._speed, self._target_speed)

    def settled(self, tolerance: float = 1.0) -> bool:
        return calc.settled(self._speed, self._target_speed, tolerance)

    def nip_verdict(self) -> str:
        return calc.nip_verdict(self.load(), NIP_BAND.low, NIP_BAND.high)

    # ---------------------------------------------------------------- internals

    def _on_spool(self, event: Any) -> None:
        self.freeze_roll(str(event.payload))

    def _require_next(self, name: str) -> None:
        """Refuse anything that is not the next nip in the train."""
        if name not in self._states:
            raise UnknownStageError(f"{SEQUENCE_NAME} does not declare stage {name}")
        if self._sequence.is_done(name):
            raise DuplicateStageError(f"{SEQUENCE_NAME} stage {name} already closed")
        required = self._sequence.next_required()
        if required != name:
            raise OutOfSequenceError(
                f"{SEQUENCE_NAME} cannot close {name} before {required}"
            )

    def _spec(self, name: str) -> NipSpec:
        for spec in self._specs:
            if spec.name == name:
                return spec
        raise UnknownStageError(f"{SEQUENCE_NAME} does not declare stage {name}")

    def _pressure_of(self, name: str | None) -> float:
        if name is None:
            return self._last_pressure()
        self._spec(name)
        return self._states[name].pressure

    def _last_pressure(self) -> float:
        completed = self._sequence.completed()
        if not completed:
            return 0.0
        return self._states[completed[-1]].pressure

    def _nip_view(self, spec: NipSpec) -> NipView:
        state = self._states[spec.name]
        return NipView(
            name=spec.name,
            closed=state.closed,
            pressure=state.pressure,
            thickness=state.thickness,
            verdict=state.verdict,
            low=spec.band.low,
            high=spec.band.high,
        )

    def _nip_report(self, name: str) -> dict[str, Any]:
        state = self._states[name]
        return {
            "closed": state.closed,
            "verdict": state.verdict,
            "thickness": state.thickness,
            "pressure": state.pressure,
        }
