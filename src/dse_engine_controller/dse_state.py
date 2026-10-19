"""Decoded view of a DSE E-series controller, built from a GenComm register image.

The application polls the blocks in :data:`gencomm.READ_PLAN` into one sparse
``{address: word}`` image and hands it here. :class:`DseSnapshot` turns that
into named readings with the same property surface the auto-start, run-mode
and UI code grew up on with the Kensho panel — ``is_auto``, ``is_running``,
``is_warming_up``, ``is_faulted``, ``fault_string``, ``engine_rpm`` and so on
— so those parts of the app don't care which panel is underneath.

Where the two panels differ in kind rather than detail, the difference is
named here rather than papered over:

* A Kensho reports one fault code; a DSE reports a condition for every alarm
  it knows about. ``active_alarms`` carries them all, ``fault_string`` names
  the worst.
* A Kensho has warm-up / line-fill / cool-down status bits; a DSE reports a
  J1939 engine operating state. Warming up and cooling down map across; line
  fill doesn't exist here.
* A Kensho takes a target RPM; an E-series takes a target RPM *and* can hold a
  flexible-sensor reading at a set point with its own PID loop (Maintain Value
  Control). Both are surfaced, see :attr:`maintain`.
"""

from __future__ import annotations

import enum
import logging
import time
from collections import deque
from dataclasses import dataclass

from . import gencomm as gc

log = logging.getLogger(__name__)

# Engine speed at or above which the engine counts as turning when the module
# doesn't report an operating state. Well above crank speed noise on a magnetic
# pickup, well below any idle.
RUNNING_RPM_THRESHOLD = 300


class DseModel(enum.Enum):
    """E-series controller variant. Decides page sizes and what the UI shows."""

    E400 = "E400"
    E800 = "E800"

    @property
    def flex_sensor_count(self) -> int:
        return 7 if self is DseModel.E400 else 12

    @property
    def unnamed_alarm_registers(self) -> int:
        return (
            gc.UNNAMED_ALARM_REGISTERS_E400
            if self is DseModel.E400
            else gc.UNNAMED_ALARM_REGISTERS_E800
        )


@dataclass(frozen=True)
class FlexSensor:
    """One of the module's flexible analogue inputs, as the module describes it."""

    index: int  # 0 = A
    value: float | None  # engineering units, scaled by the module's unit info
    raw: int | None  # the signed word as read
    unit_id: int | None
    multiplier: int | None
    precision: int | None
    range_min: float | None
    range_max: float | None
    suffix: str

    @property
    def letter(self) -> str:
        return gc.FLEX_SENSOR_LETTERS[self.index]

    @property
    def label(self) -> str:
        return f"Flexible sensor {self.letter}"

    @property
    def configured(self) -> bool:
        """Whether the module has a sensor configured on this input."""
        return self.unit_id not in (None, gc.UnitId.NOT_USED)

    @property
    def is_digital(self) -> bool:
        return self.unit_id == gc.UnitId.DIGITAL_INPUT

    @property
    def units(self) -> str:
        if self.suffix:
            return self.suffix
        return {
            gc.UnitId.TEMPERATURE: "°C",
            gc.UnitId.PRESSURE: "kPa",
            gc.UnitId.PERCENTAGE: "%",
        }.get(self.unit_id, "")


@dataclass(frozen=True)
class MaintainValue:
    """Page 137 (Exxx): target speed and the Maintain Value Control loop."""

    target_speed_rpm: int | None
    p_gain: float | None
    i_gain: float | None
    d_gain: float | None
    set_point_raw: int | None
    set_point_min_raw: int | None
    set_point_max_raw: int | None
    control_value_raw: int | None
    unit_id: int | None
    multiplier: int | None
    precision: int | None
    range_min_raw: int | None
    range_max_raw: int | None
    suffix: str
    display_units: int | None
    target_source: int | None

    def _scaled(self, raw):
        return gc.scaled(raw, self.multiplier, self.precision)

    @property
    def set_point(self) -> float | None:
        return self._scaled(self.set_point_raw)

    @property
    def set_point_min(self) -> float | None:
        return self._scaled(self.set_point_min_raw)

    @property
    def set_point_max(self) -> float | None:
        return self._scaled(self.set_point_max_raw)

    @property
    def control_value(self) -> float | None:
        return self._scaled(self.control_value_raw)

    @property
    def range_min(self) -> float | None:
        return self._scaled(self.range_min_raw)

    @property
    def range_max(self) -> float | None:
        return self._scaled(self.range_max_raw)

    @property
    def available(self) -> bool:
        """Whether the module exposes a Maintain Value loop at all.

        An E-series that has the feature configured reports a set point and a
        control source; one that doesn't returns sentinels for both.
        """
        return self.set_point_raw is not None and self.unit_id not in (
            None,
            gc.UnitId.NOT_USED,
        )

    @property
    def units(self) -> str:
        if self.suffix:
            return self.suffix
        if self.unit_id is not None and self.display_units is not None:
            try:
                return gc.DISPLAY_UNITS.get((gc.UnitId(self.unit_id), self.display_units), "")
            except ValueError:
                return ""
        return ""

    @property
    def target_source_label(self) -> str:
        return gc.target_source_label(self.target_source)

    def raw_set_point_for(self, value: float) -> int:
        """Encode an engineering set point as the word to write to offset 154."""
        return gc.to_s16(gc.unscale(value, self.multiplier))


class DseSnapshot:
    """One decoded register image.

    ``registers`` is sparse: only addresses the read plan covers are present.
    Anything absent decodes to ``None``, the same as a sentinel, so a block
    that hasn't been read yet (the slow tier before its first pass) reads as
    unknown rather than zero.
    """

    def __init__(self, registers: dict[int, int], timestamp: float, model: DseModel = DseModel.E400):
        self.registers = registers
        self.timestamp = timestamp
        self.model = model
        # Decoded once per snapshot; the alarm pages are walked nibble by nibble.
        self._named = gc.decode_named_alarms(self._words(gc.PAGE_NAMED_ALARMS, 1, gc.NAMED_ALARM_REGISTERS))
        self._unnamed = gc.decode_unnamed_alarms(
            self._words(gc.PAGE_UNNAMED_ALARMS, 1, model.unnamed_alarm_registers)
        )

    # --- raw access --------------------------------------------------------

    def _raw(self, page: int, offset: int) -> int | None:
        return self.registers.get(gc.address(page, offset))

    def _u16(self, page: int, offset: int, scale: float = 1.0):
        return gc.u16(self._raw(page, offset), scale)

    def _s16(self, page: int, offset: int, scale: float = 1.0):
        return gc.s16(self._raw(page, offset), scale)

    def _u32(self, page: int, offset: int, scale: float = 1.0):
        return gc.u32(self._raw(page, offset), self._raw(page, offset + 1), scale)

    def _words(self, page: int, offset: int, count: int) -> list[int] | None:
        """``count`` consecutive words, or ``None`` if any is missing."""
        out = []
        for i in range(count):
            word = self._raw(page, offset + i)
            if word is None:
                return None
            out.append(word)
        return out

    def has(self, page: int, offset: int) -> bool:
        return gc.address(page, offset) in self.registers

    # --- identity ----------------------------------------------------------

    @property
    def manufacturer_code(self) -> int | None:
        return self._u16(gc.PAGE_STATUS, gc.P3_MANUFACTURER)

    @property
    def model_number(self) -> int | None:
        return self._u16(gc.PAGE_STATUS, gc.P3_MODEL)

    @property
    def serial_number(self) -> int | None:
        return self._u32(gc.PAGE_STATUS, gc.P3_SERIAL)

    # --- control mode / status --------------------------------------------

    @property
    def control_mode(self) -> gc.ControlMode:
        return gc.control_mode(self._raw(gc.PAGE_STATUS, gc.P3_CONTROL_MODE))

    @property
    def control_mode_label(self) -> str:
        return self.control_mode.label

    @property
    def is_auto(self) -> bool:
        return self.control_mode in (gc.ControlMode.AUTO, gc.ControlMode.AUTO_MANUAL_RESTORE)

    @property
    def is_manual(self) -> bool:
        return self.control_mode is gc.ControlMode.MANUAL

    @property
    def is_stop_mode(self) -> bool:
        return self.control_mode in (gc.ControlMode.STOP, gc.ControlMode.OFF)

    def _flag(self, bit: int) -> bool:
        word = self._raw(gc.PAGE_STATUS, gc.P3_ALARM_FLAGS)
        if word is None:
            return False
        return bool((int(word) >> bit) & 1)

    @property
    def shutdown_flag(self) -> bool:
        return self._flag(gc.FLAG_SHUTDOWN)

    @property
    def controlled_shutdown_flag(self) -> bool:
        return self._flag(gc.FLAG_CONTROLLED_SHUTDOWN)

    @property
    def electrical_trip_flag(self) -> bool:
        return self._flag(gc.FLAG_ELECTRICAL_TRIP)

    @property
    def warning_flag(self) -> bool:
        return self._flag(gc.FLAG_WARNING)

    @property
    def panel_locked(self) -> bool:
        word = self._raw(gc.PAGE_STATUS, gc.P3_PANEL_LOCK)
        if word is None or int(word) >= gc.U16_SENTINEL_FLOOR:
            return False
        mask = (1 << gc.LOCK_EXTERNAL) | (1 << gc.LOCK_STOP_AND_PANEL) | (1 << gc.LOCK_TELEMETRY)
        return bool(int(word) & mask)

    # --- engine state ------------------------------------------------------

    @property
    def engine_operating_state(self) -> gc.EngineOperatingState | None:
        return gc.engine_operating_state(
            gc.u16(self._raw(gc.PAGE_EXTENDED, gc.P5_ENGINE_OPERATING_STATE))
        )

    @property
    def engine_state_label(self) -> str:
        state = self.engine_operating_state
        return state.label if state is not None else ""

    @property
    def engine_rpm(self) -> float | None:
        return self._u16(gc.PAGE_BASIC, gc.P4_ENGINE_SPEED)

    @property
    def is_running(self) -> bool:
        """Whether the engine is turning.

        The engine operating state says so directly where the module reports
        it; engine speed is the fallback (and the tie-breaker if the state says
        stopped while the shaft is clearly turning).
        """
        rpm = self.engine_rpm
        if rpm is not None and rpm >= RUNNING_RPM_THRESHOLD:
            return True
        state = self.engine_operating_state
        return state in (
            gc.EngineOperatingState.WARMING_UP,
            gc.EngineOperatingState.RUNNING,
            gc.EngineOperatingState.COOLING_DOWN,
        )

    @property
    def is_pre_start(self) -> bool:
        return self.engine_operating_state is gc.EngineOperatingState.PRE_START

    @property
    def is_warming_up(self) -> bool:
        return self.engine_operating_state is gc.EngineOperatingState.WARMING_UP

    @property
    def is_cooling_down(self) -> bool:
        return self.engine_operating_state is gc.EngineOperatingState.COOLING_DOWN

    # --- alarms ------------------------------------------------------------

    @property
    def named_alarms(self) -> list[gc.Alarm]:
        return list(self._named)

    @property
    def unnamed_alarms(self) -> list[gc.Alarm]:
        return list(self._unnamed)

    @property
    def active_alarms(self) -> list[gc.Alarm]:
        return [*self._named, *self._unnamed]

    @property
    def lockout_alarms(self) -> list[gc.Alarm]:
        return [a for a in self.active_alarms if a.condition.is_lockout]

    @property
    def warning_alarms(self) -> list[gc.Alarm]:
        return [a for a in self.active_alarms if a.condition is gc.AlarmCondition.WARNING]

    @property
    def is_faulted(self) -> bool:
        """Whether the module is holding the engine off because of an alarm.

        Either the status word says shutdown / controlled shutdown / electrical
        trip, or an alarm page carries one of those conditions. Both are
        consulted so the answer never rests on a single bit.
        """
        return (
            self.shutdown_flag
            or self.controlled_shutdown_flag
            or self.electrical_trip_flag
            or bool(self.lockout_alarms)
        )

    @property
    def has_warning(self) -> bool:
        return self.warning_flag or bool(self.warning_alarms)

    @property
    def has_common_alarm(self) -> bool:
        """Any alarm at all: a shutdown, a warning, or an active indication."""
        return self.is_faulted or self.has_warning or bool(self.active_alarms)

    @property
    def worst_alarm(self) -> gc.Alarm | None:
        return gc.worst(self.active_alarms)

    @property
    def fault_string(self) -> str:
        """The most serious active alarm, or what the status word says if the
        alarm pages haven't named one."""
        alarm = self.worst_alarm
        if alarm is not None:
            return alarm.name
        if self.shutdown_flag:
            return "Shutdown alarm"
        if self.controlled_shutdown_flag:
            return "Controlled shutdown"
        if self.electrical_trip_flag:
            return "Electrical trip"
        if self.warning_flag:
            return "Warning"
        return ""

    @property
    def alarm_count(self) -> int:
        return len(self.active_alarms)

    @property
    def alarm_summary(self) -> str:
        """Every active alarm on one line, worst first."""
        alarms = sorted(
            self.active_alarms,
            key=lambda a: gc.SEVERITY_ORDER.index(a.condition)
            if a.condition in gc.SEVERITY_ORDER
            else len(gc.SEVERITY_ORDER),
        )
        return "; ".join(a.label for a in alarms)

    # --- basic instrumentation --------------------------------------------

    @property
    def oil_pressure_kpa(self) -> int | None:
        return self._u16(gc.PAGE_BASIC, gc.P4_OIL_PRESSURE)

    @property
    def coolant_temp_c(self) -> int | None:
        return self._s16(gc.PAGE_BASIC, gc.P4_COOLANT_TEMP)

    @property
    def oil_temp_c(self) -> int | None:
        return self._s16(gc.PAGE_BASIC, gc.P4_OIL_TEMP)

    @property
    def fuel_level_pct(self) -> int | None:
        return self._u16(gc.PAGE_BASIC, gc.P4_FUEL_LEVEL)

    @property
    def alternator_voltage(self) -> float | None:
        return self._u16(gc.PAGE_BASIC, gc.P4_CHARGE_ALT_VOLTS, 0.1)

    @property
    def battery_voltage(self) -> float | None:
        return self._u16(gc.PAGE_BASIC, gc.P4_BATTERY_VOLTS, 0.1)

    # --- extended instrumentation -----------------------------------------

    @property
    def engine_load_pct(self) -> int | None:
        return self._u16(gc.PAGE_EXTENDED, gc.P5_PERCENT_LOAD)

    @property
    def coolant_level_pct(self) -> int | None:
        return self._u16(gc.PAGE_EXTENDED, gc.P5_COOLANT_LEVEL)

    @property
    def oil_level_pct(self) -> int | None:
        return self._u16(gc.PAGE_EXTENDED, gc.P5_OIL_LEVEL)

    @property
    def fuel_rate_lph(self) -> float | None:
        return self._u32(gc.PAGE_EXTENDED, gc.P5_FUEL_CONSUMPTION, 0.01)

    @property
    def boost_pressure_kpa(self) -> int | None:
        return self._u16(gc.PAGE_EXTENDED, gc.P5_TURBO_PRESSURE)

    @property
    def intake_manifold_temp_c(self) -> int | None:
        return self._s16(gc.PAGE_EXTENDED, gc.P5_INLET_MANIFOLD_TEMP)

    @property
    def exhaust_temp_c(self) -> int | None:
        return self._s16(gc.PAGE_EXTENDED, gc.P5_EXHAUST_TEMP)

    @property
    def fuel_temp_c(self) -> int | None:
        return self._s16(gc.PAGE_EXTENDED, gc.P5_FUEL_TEMP)

    @property
    def manual_run_time_remaining_s(self) -> float | None:
        return self._u32(gc.PAGE_EXTENDED, gc.P5_MANUAL_RUN_TIME_REMAINING, 0.1)

    # --- accumulated ---------------------------------------------------------

    @property
    def engine_run_time_s(self) -> int | None:
        return self._u32(gc.PAGE_ACCUMULATED, gc.P7_ENGINE_RUN_TIME)

    @property
    def total_engine_hours(self) -> float | None:
        seconds = self.engine_run_time_s
        return None if seconds is None else seconds / 3600.0

    @property
    def number_of_starts(self) -> int | None:
        return self._u32(gc.PAGE_ACCUMULATED, gc.P7_NUMBER_OF_STARTS)

    @property
    def fuel_used_l(self) -> int | None:
        return self._u32(gc.PAGE_ACCUMULATED, gc.P7_FUEL_USED)

    # --- speed control / maintain value -----------------------------------

    @property
    def maintain(self) -> MaintainValue:
        p = gc.PAGE_SPEED_CONTROL
        return MaintainValue(
            target_speed_rpm=self._u16(p, gc.P137_TARGET_SPEED),
            p_gain=self._u16(p, gc.P137_P_GAIN, 0.1),
            i_gain=self._u16(p, gc.P137_I_GAIN, 0.1),
            d_gain=self._u16(p, gc.P137_D_GAIN, 0.1),
            set_point_raw=self._s16(p, gc.P137_SET_POINT),
            set_point_min_raw=self._s16(p, gc.P137_SET_POINT_MIN),
            set_point_max_raw=self._s16(p, gc.P137_SET_POINT_MAX),
            control_value_raw=self._s16(p, gc.P137_CONTROL_SOURCE_VALUE),
            unit_id=self._raw(p, gc.P137_CONTROL_SOURCE_UNIT_ID),
            multiplier=self._s16(p, gc.P137_CONTROL_SOURCE_MULTIPLIER),
            precision=self._u16(p, gc.P137_CONTROL_SOURCE_PRECISION),
            range_min_raw=self._s16(p, gc.P137_CONTROL_SOURCE_RANGE_MIN),
            range_max_raw=self._s16(p, gc.P137_CONTROL_SOURCE_RANGE_MAX),
            suffix=gc.decode_string(self._words(p, gc.P137_CONTROL_SOURCE_SUFFIX, 10)),
            display_units=self._u16(p, gc.P137_DISPLAY_UNITS),
            target_source=self._raw(p, gc.P137_SET_POINT_TARGET_SOURCE),
        )

    @property
    def target_speed_rpm(self) -> int | None:
        return self._u16(gc.PAGE_SPEED_CONTROL, gc.P137_TARGET_SPEED)

    @property
    def remote_rpm_setpoint(self) -> int | None:
        """The target speed the module is holding — the Kensho name for it."""
        return self.target_speed_rpm

    # --- flexible sensors ---------------------------------------------------

    def flex_sensor(self, index: int) -> FlexSensor:
        p = gc.PAGE_FLEX_SENSORS
        base = gc.flex_unit_info_offset(index)
        unit_id = self._raw(p, base + gc.P178_UNIT_ID)
        multiplier = self._s16(p, base + gc.P178_MULTIPLIER)
        precision = self._u16(p, base + gc.P178_PRECISION)
        raw = self._s16(p, gc.P178_READINGS + index)
        return FlexSensor(
            index=index,
            value=gc.scaled(raw, multiplier, precision),
            raw=raw,
            unit_id=unit_id,
            multiplier=multiplier,
            precision=precision,
            range_min=gc.scaled(self._s16(p, base + gc.P178_RANGE_MIN), multiplier, precision),
            range_max=gc.scaled(self._s16(p, base + gc.P178_RANGE_MAX), multiplier, precision),
            suffix=gc.decode_string(self._words(p, base + gc.P178_SUFFIX, gc.P178_SUFFIX_LEN)),
        )

    @property
    def flex_sensors(self) -> list[FlexSensor]:
        return [self.flex_sensor(i) for i in range(self.model.flex_sensor_count)]

    # --- display -------------------------------------------------------------

    @property
    def display_state(self) -> str:
        """Short human-readable state for the app tile."""
        if self.is_faulted:
            return f"Fault: {self.fault_string}"
        if self.is_running:
            prefix = "" if self.is_auto else f"{self.control_mode_label} "
            if self.is_warming_up:
                return f"{prefix}Warming Up"
            if self.is_cooling_down:
                return f"{prefix}Cooling Down"
            return f"{prefix}Running"
        if self.is_pre_start:
            return "Starting"
        if self.has_warning:
            return f"Stopped (warning: {self.fault_string})"
        return "Stopped"


class DseStateStore:
    """Rolling history of decoded snapshots, with comms-loss tracking.

    Same shape as the Kensho store the auto-start logic was written against:
    a deque of recent reads so a single failed poll can be told from sustained
    comms loss.
    """

    def __init__(self, history: int = 30, model: DseModel = DseModel.E400):
        self.model = model
        self.snapshots: deque[DseSnapshot] = deque(maxlen=history)
        self.is_contactable: bool = False
        self.became_uncontactable_at: float | None = None
        self.iters_uncontactable: int = 0
        self.last_fault_string: str = ""

    def update(self, registers: dict[int, int] | None) -> None:
        if not registers:
            if self.is_contactable:
                self.became_uncontactable_at = time.time()
            self.is_contactable = False
            self.iters_uncontactable += 1
            return

        snap = DseSnapshot(dict(registers), time.time(), self.model)
        self.snapshots.append(snap)
        self.is_contactable = True
        self.became_uncontactable_at = None
        self.iters_uncontactable = 0
        if snap.is_faulted:
            self.last_fault_string = snap.fault_string

    @property
    def latest(self) -> DseSnapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    @property
    def num_records(self) -> int:
        return len(self.snapshots)

    def has_lost_comms(self, timeout_seconds: float = 15.0, min_iters: int = 3) -> bool:
        """True if comms have been lost for long enough to act on it."""
        if self.is_contactable:
            return False
        if self.became_uncontactable_at is None:
            return self.iters_uncontactable >= min_iters
        return (
            time.time() - self.became_uncontactable_at >= timeout_seconds
            and self.iters_uncontactable >= min_iters
        )
