"""Pure model of a DSE E400 engine controller as seen over GenComm.

This module has no third-party imports (no pymodbus, no pydoover) so the
app's test suite can import it directly. ``main.py`` wires it to a pymodbus
TCP server and to the doover device agent.

The register map is duplicated from the app's protocol contract,
``src/dse_engine_controller/gencomm.py``: the simulator is built into its own
Docker image that doesn't contain the app package, so it can't import it. If
an address or bit changes there, change it here too (``tests/test_simulator.py``
cross-checks the important ones against gencomm.py).

Addressing is ``page * 256 + offset``, the plain zero-based Modbus
holding-register (PDU) address. Anything the model doesn't implement reads as
0xFFFF (GenComm's "unimplemented" sentinel) rather than raising, because the
app reads whole contiguous blocks and the real module answers them the same
way.

Lifecycle (E400 operator manual):
  * Stop mode: the engine stops immediately, a pending start request is
    dropped, and latched alarms clear once their underlying condition is gone.
  * Auto mode: a start request (telemetry start key, or the simulated remote
    start input) runs start delay -> crank -> safety-on / warm-up -> running;
    removing the request runs return delay -> cooling down -> stopped.
  * Manual mode: the Start key cranks immediately (no start delay); the
    engine runs until Stop is selected.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Register map (mirrors src/dse_engine_controller/gencomm.py)
# ---------------------------------------------------------------------------

PAGE_SIZE = 256
EPS = 1e-6  # float slack on phase timers
UNIMPLEMENTED = 0xFFFF  # unsigned "unimplemented" sentinel
S16_UNIMPLEMENTED = 0x7FFF  # signed "unimplemented" sentinel


def addr(page: int, offset: int) -> int:
    return page * PAGE_SIZE + offset


PAGE_STATUS = 3
PAGE_BASIC = 4
PAGE_EXTENDED = 5
PAGE_ACCUMULATED = 7
PAGE_CONTROL = 16
PAGE_SPEED_CONTROL = 137
PAGE_UNNAMED_ALARMS = 153
PAGE_NAMED_ALARMS = 154
PAGE_FLEX_SENSORS = 178

# Page 3
P3_MANUFACTURER = 0
P3_MODEL = 1
P3_SERIAL = 2  # u32, 2-3
P3_CONTROL_MODE = 4
P3_ALARM_FLAGS = 6
P3_PANEL_LOCK = 38
FLAG_SHUTDOWN = 12
FLAG_ELECTRICAL_TRIP = 11
FLAG_WARNING = 10
FLAG_CONTROLLED_SHUTDOWN = 6

MANUFACTURER_DSE = 1
# The model number register is manufacturer-specific; the simulator reports 400.
MODEL_NUMBER_E400 = 400
SERIAL_NUMBER = 12345678

# Page 4
P4_OIL_PRESSURE = 0
P4_COOLANT_TEMP = 1
P4_OIL_TEMP = 2
P4_FUEL_LEVEL = 3
P4_CHARGE_ALT_VOLTS = 4
P4_BATTERY_VOLTS = 5
P4_ENGINE_SPEED = 6

# Page 5
P5_TURBO_PRESSURE = 4
P5_INLET_MANIFOLD_TEMP = 6
P5_EXHAUST_TEMP = 8
P5_FUEL_CONSUMPTION = 10  # 0.01 L/h, u32
P5_ATMOSPHERIC_PRESSURE = 14
P5_FUEL_TEMP = 15
P5_PERCENT_LOAD = 74
P5_COOLANT_LEVEL = 79
P5_ENGINE_OPERATING_STATE = 128
P5_MANUAL_RUN_TIME_REMAINING = 254  # 0.1 s, u32

# Page 7 (Exxx allocation)
P7_TIME_NOW = 0
P7_TIME_TO_MAINTENANCE = 2  # s32
P7_ENGINE_RUN_TIME = 6
P7_NUMBER_OF_STARTS = 8
P7_FUEL_USED = 10

# Page 16
P16_SUPPORTED_BITMAP = 0  # 0..7
P16_CONTROL_KEY = 8
P16_CONTROL_KEY_COMPLEMENT = 9

# Page 137 (Exxx variation)
P137_TARGET_SPEED = 147
P137_P_GAIN = 151
P137_I_GAIN = 152
P137_D_GAIN = 153
P137_SET_POINT = 154
P137_SET_POINT_MIN = 155
P137_SET_POINT_MAX = 156
P137_CONTROL_SOURCE_VALUE = 157
P137_CONTROL_SOURCE_UNIT_ID = 158
P137_CONTROL_SOURCE_MULTIPLIER = 159
P137_CONTROL_SOURCE_PRECISION = 160
P137_CONTROL_SOURCE_RANGE_MIN = 161
P137_CONTROL_SOURCE_RANGE_MAX = 162
P137_CONTROL_SOURCE_SUFFIX = 163  # 163..172
P137_DISPLAY_UNITS = 173
P137_SET_POINT_TARGET_SOURCE = 174

# Page 178
P178_READINGS_COUNT = 12
P178_UNIT_INFO_START = 16
P178_UNIT_INFO_STRIDE = 15
SUFFIX_LEN = 10

UNIT_NOT_USED = 0
UNIT_TEMPERATURE = 1
UNIT_PRESSURE = 2
UNIT_PERCENTAGE = 3


class Mode(enum.IntEnum):
    """Page 3 offset 4 control mode."""

    STOP = 0
    AUTO = 1
    MANUAL = 2


class OperatingState(enum.IntEnum):
    """Page 5 offset 128 engine operating state."""

    STOPPED = 0
    PRE_START = 1
    WARMING_UP = 2
    RUNNING = 3
    COOLING_DOWN = 4


class Phase(enum.Enum):
    """The sim's internal lifecycle phase (finer than the operating state)."""

    STOPPED = "stopped"
    START_DELAY = "start_delay"
    CRANKING = "cranking"
    WARMING_UP = "warming_up"
    RUNNING = "running"
    RETURN_DELAY = "return_delay"
    COOLING_DOWN = "cooling_down"


PHASE_TO_STATE = {
    Phase.STOPPED: OperatingState.STOPPED,
    Phase.START_DELAY: OperatingState.PRE_START,
    Phase.CRANKING: OperatingState.PRE_START,
    Phase.WARMING_UP: OperatingState.WARMING_UP,
    Phase.RUNNING: OperatingState.RUNNING,
    Phase.RETURN_DELAY: OperatingState.RUNNING,
    Phase.COOLING_DOWN: OperatingState.COOLING_DOWN,
}


class Key(enum.IntEnum):
    """System control keys the sim honours (gencomm.ControlKey subset)."""

    SELECT_STOP = 35700
    SELECT_AUTO = 35701
    SELECT_MANUAL = 35702
    START_ENGINE = 35705
    RESET_ALARMS = 35707
    TELEMETRY_START = 35732
    CANCEL_TELEMETRY_START = 35733
    RESET_ALL_ALARMS = 35734
    THROTTLE_DOWN = 35777
    THROTTLE_UP = 35778


SUPPORTED_KEYS = frozenset(int(k) for k in Key)
KEY_BASE = 35700


def supported_bitmap(keys=SUPPORTED_KEYS) -> list[int]:
    """Page 16 offsets 0..7: function n is bit (15 - n % 16) of word n // 16."""
    words = [0] * 8
    for key in keys:
        code = key - KEY_BASE
        words[code // 16] |= 1 << (15 - code % 16)
    return words


class Condition(enum.IntEnum):
    """Alarm nibble values (gencomm.AlarmCondition subset)."""

    INACTIVE = 1
    WARNING = 2
    SHUTDOWN = 3
    ELECTRICAL_TRIP = 4
    CONTROLLED_SHUTDOWN = 5
    UNIMPLEMENTED = 15


LOCKOUT = frozenset({Condition.SHUTDOWN, Condition.ELECTRICAL_TRIP, Condition.CONTROLLED_SHUTDOWN})
CONDITION_FLAG = {
    Condition.SHUTDOWN: FLAG_SHUTDOWN,
    Condition.ELECTRICAL_TRIP: FLAG_ELECTRICAL_TRIP,
    Condition.WARNING: FLAG_WARNING,
    Condition.CONTROLLED_SHUTDOWN: FLAG_CONTROLLED_SHUTDOWN,
}
# Most serious first (when one alarm is injected twice, keep the worse one).
SEVERITY = (
    Condition.SHUTDOWN,
    Condition.CONTROLLED_SHUTDOWN,
    Condition.ELECTRICAL_TRIP,
    Condition.WARNING,
)


def parse_condition(text: str | int | None) -> Condition:
    if isinstance(text, int):
        return Condition(text)
    norm = (text or "shutdown").strip().lower().replace("_", " ").replace("-", " ")
    table = {
        "warning": Condition.WARNING,
        "shutdown": Condition.SHUTDOWN,
        "controlled shutdown": Condition.CONTROLLED_SHUTDOWN,
        "electrical trip": Condition.ELECTRICAL_TRIP,
        "trip": Condition.ELECTRICAL_TRIP,
    }
    if norm not in table:
        raise ValueError(f"unknown alarm condition: {text!r}")
    return table[norm]


# Page 154 named alarms, Exxx allocation (gencomm.NAMED_ALARMS).
NAMED_ALARMS = (
    "Emergency stop", "Low oil pressure", "High coolant temperature",
    "Low coolant temperature", "Under speed", "Over speed", "Battery low voltage",
    "Battery high voltage", "Charge alternator failure", "Fail to start",
    "Fail to stop", "Oil pressure sender fault", "Loss of magnetic pick up",
    "Magnetic pick up open circuit", "Calibration lost", "Low fuel level",
    "CAN ECU warning", "CAN ECU shutdown", "CAN ECU data fail", "Low oil level switch",
    "High temperature switch", "Low fuel level switch", "Expansion unit watchdog",
    "Fuel usage running", "Fuel usage stopped", "Protections disabled",
    "Protections blocked", "Air flap closed", "Charge air temperature",
    "High coolant temperature electrical trip", "Temperature sender open circuit",
    "ECU protect", "ECU malfunction", "ECU information", "ECU shutdown", "ECU warning",
    "ECU electrical trip", "ECU after-treatment", "ECU water in fuel", "ECU heater",
    "ECU cooler", "MSC data error", "MSC ID error", "MSC failure", "MSC too few sets",
    "MSC alarms inhibited", "MSC old version units", "LCD heater low voltage",
    "LCD heater high voltage", "LCD heater temperature", "LCD heater sensor failure",
    "DEF level low", "SCR inducement", "PWM(i) 1 active", "PWM(i) 2 active",
    "PWM(i) 3 active", "PWM(i) 4 active", "Incorrect speed",
)
NAMED_ALARM_REGISTERS = (len(NAMED_ALARMS) + 3) // 4  # 15
UNNAMED_ALARM_REGISTERS = 57  # E400


def _unnamed_alarms() -> dict[tuple[int, int], str]:
    """Page 153, Exxx/E400 allocation (gencomm.UNNAMED_ALARMS)."""
    flex = "ABCDEFGHIJKL"
    table: dict[tuple[int, int], str] = {}
    for n in range(4):
        table[(1, n)] = f"Digital input {chr(ord('A') + n)}"
        table[(5, n)] = f"Flexible input {flex[n]} (digital)"
        table[(6, n)] = f"Flexible input {flex[4 + n]} (digital)"
        table[(7, n)] = f"Flexible input {flex[8 + n]} (digital)"
    for i, letter in enumerate(flex):
        reg, half = 39 + i // 2, (i % 2) * 2
        table[(reg, half)] = f"Flexible sensor {letter} low"
        table[(reg, half + 1)] = f"Flexible sensor {letter} high"
    for n in range(4):
        table[(45, n)] = f"Engine maintenance {n + 1} due"
    for i in range(20):
        table[(46 + i // 4, i % 4)] = f"PLC function {i + 1}"
    for n in range(4):
        table[(51, n)] = f"Flexible sensor {flex[n]} fault"
        table[(52, n)] = f"Flexible sensor {flex[4 + n]} fault"
        table[(53, n)] = f"Flexible sensor {flex[8 + n]} fault"
        table[(54, n)] = f"Engine maintenance {5 + n} due"
        table[(55, n)] = f"CAN instrument {n + 1}"
        table[(56, n)] = f"CAN instrument {5 + n}"
    table[(57, 0)] = "CAN instrument 9"
    table[(57, 1)] = "CAN instrument 10"
    table[(57, 2)] = "Engine maintenance 9 due"
    table[(57, 3)] = "Engine maintenance 10 due"
    return table


UNNAMED_ALARMS = _unnamed_alarms()

# name (lower case) -> (page, register offset, nibble 0..3)
ALARM_POSITIONS: dict[str, tuple[int, int, int]] = {
    name.lower(): (PAGE_NAMED_ALARMS, 1 + i // 4, i % 4) for i, name in enumerate(NAMED_ALARMS)
}
ALARM_POSITIONS.update(
    {name.lower(): (PAGE_UNNAMED_ALARMS, off, nib) for (off, nib), name in UNNAMED_ALARMS.items()}
)
_CANONICAL = {n.lower(): n for n in (*NAMED_ALARMS, *UNNAMED_ALARMS.values())}


def _suffix(text: str) -> list[int]:
    """GenComm Unicode string: one UTF-16 code unit per register, space padded."""
    return [ord(c) for c in text[:SUFFIX_LEN]] + [0x20] * (SUFFIX_LEN - len(text[:SUFFIX_LEN]))


def _u32(value: float) -> tuple[int, int]:
    raw = max(0, int(value)) & 0xFFFFFFFF
    return (raw >> 16) & 0xFFFF, raw & 0xFFFF


def _s16(value: float) -> int:
    v = max(-0x8000, min(0x7FF7, int(round(value))))
    return v & 0xFFFF


def _from_s16(raw: int) -> int:
    raw &= 0xFFFF
    return raw - 0x10000 if raw >= 0x8000 else raw


def _approach(value: float, target: float, tau: float, dt: float) -> float:
    """First-order lag step."""
    if tau <= 0:
        return target
    alpha = min(1.0, dt / tau)
    return value + (target - value) * alpha


def _ramp(value: float, target: float, rate: float, dt: float) -> float:
    step = rate * dt
    if abs(target - value) <= step:
        return target
    return value + step if target > value else value - step


@dataclass
class Timings:
    start_delay: float = 2.0
    crank: float = 3.0
    warmup: float = 10.0
    return_delay: float = 2.0
    cooldown: float = 10.0


class E400Model:
    """A DSE E400 controller and the engine it runs, as a GenComm register image.

    Public API:
      read(address, count) -> list[int]
      write(address, values) -> bool
      tick(dt=None)                         advance the simulation
      Sim controls: set_remote_start(bool), sim_start(), sim_stop(),
        shortcut_to_running(), set_mode(mode), inject_alarm(name, condition),
        clear_alarm(name=None), maintain_enabled, flex_a_kpa / flex_b_c /
        flex_c_pct overrides (None = derived)
      Readback: phase, operating_state, mode, rpm, active_alarms(),
        alarm_summary()
    """

    # Engine constants
    IDLE_RPM = 900.0
    CRANK_RPM = 150.0
    RPM_RAMP = 100.0  # rpm/s while running / tracking a target
    WARMUP_RAMP = 300.0  # rpm/s up to idle after firing
    THROTTLE_STEP = 50

    # Maintain-value plant: control source (0.01 bar) = PLANT_GAIN * rpm, lagged.
    PLANT_GAIN = 0.2  # raw units (0.01 bar) per rpm -> 1500 rpm = 3.00 bar
    PLANT_TAU = 1.5  # s
    PID_RPM_PER_BAR = 400.0  # converts PID output (bar) to an rpm command

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        timings: Timings | None = None,
        min_speed: int = 800,
        max_speed: int = 2200,
    ):
        self.clock = clock or time.monotonic
        self.wall_clock = wall_clock or time.time
        self.timings = timings or Timings()
        self.min_speed = min_speed
        self.max_speed = max_speed

        self.regs: dict[int, int] = {}
        self._last_clock = self.clock()
        self.now = 0.0  # sim seconds since construction

        # Controller state
        self.mode = Mode.STOP
        self.phase = Phase.STOPPED
        self._phase_entered = 0.0
        self.telemetry_start = False  # latched by key 35732, cleared by 35733 / Stop
        self.remote_start_input = False  # the simulated remote start digital input
        self.manual_start_requested = False

        # Alarms: injected = the underlying condition (sim control), latched = what
        # the controller is showing. Warnings self-clear, lockouts latch until reset.
        self.injected: dict[str, Condition] = {}
        self.latched: dict[str, Condition] = {}

        # Engine / instruments
        self.rpm = 0.0
        self.coolant_c = 25.0
        self.oil_temp_c = 25.0
        self.oil_kpa = 0.0
        self.fuel_pct = 80.0
        self.run_seconds = 125 * 3600.0
        self.starts = 42
        self.fuel_used_l = 3150.0
        self.manual_run_time_s = 0.0  # 0 = no manual run timer configured
        self._manual_remaining = 0.0

        # Page 137
        self.target_speed = 1500
        self.p_gain = 20  # 0.1 scaling -> 2.0
        self.i_gain = 5  # 0.5
        self.d_gain = 0
        self.set_point_min = 100  # 1.00 bar
        self.set_point_max = 800  # 8.00 bar
        self.set_point = 250  # 2.50 bar
        self.control_source = 0.0  # raw 0.01 bar
        self.maintain_enabled = True
        self._pid_rpm = self.IDLE_RPM
        self._pid_prev_err = 0.0

        # Flex sensor overrides (None = derived from the model)
        self.flex_a_kpa: float | None = None
        self.flex_b_c: float | None = None
        self.flex_c_pct: float | None = None

        self._init_static()
        self._publish()

    # ------------------------------------------------------------------
    # Register access
    # ------------------------------------------------------------------

    def read(self, address: int, count: int = 1) -> list[int]:
        """Holding registers ``address .. address + count - 1``; 0xFFFF where unimplemented."""
        return [self.regs.get(a, UNIMPLEMENTED) for a in range(address, address + count)]

    def write(self, address: int, values: list[int]) -> bool:
        """Apply a function-16 style write. Returns False to reject it.

        A rejected write changes nothing (the Modbus layer answers with an
        illegal-data-value exception).
        """
        values = [int(v) & 0xFFFF for v in values]
        if not values:
            return False
        end = address + len(values)
        if address == addr(PAGE_CONTROL, P16_CONTROL_KEY) and len(values) == 2:
            return self._write_control_key(values[0], values[1])
        if addr(PAGE_CONTROL, 0) <= address < addr(PAGE_CONTROL + 1, 0) or (
            addr(PAGE_CONTROL, 0) <= end - 1 < addr(PAGE_CONTROL + 1, 0)
        ):
            log.warning("Rejected page 16 write at %s x%s (key + complement must be one 2-register write to offset 8)", address, len(values))
            return False

        writable = {
            addr(PAGE_SPEED_CONTROL, P137_TARGET_SPEED),
            addr(PAGE_SPEED_CONTROL, P137_P_GAIN),
            addr(PAGE_SPEED_CONTROL, P137_I_GAIN),
            addr(PAGE_SPEED_CONTROL, P137_D_GAIN),
            addr(PAGE_SPEED_CONTROL, P137_SET_POINT),
        }
        if any(a not in writable for a in range(address, end)):
            log.warning("Rejected write to read-only/unimplemented register(s) %s..%s", address, end - 1)
            return False

        # Validate everything before applying anything.
        updates: dict[int, int] = dict(zip(range(address, end), values))
        ts = updates.get(addr(PAGE_SPEED_CONTROL, P137_TARGET_SPEED))
        if ts is not None and not self.min_speed <= ts <= self.max_speed:
            log.warning("Target speed %s rpm outside [%s, %s]; ignored", ts, self.min_speed, self.max_speed)
            return False
        for off in (P137_P_GAIN, P137_I_GAIN, P137_D_GAIN):
            g = updates.get(addr(PAGE_SPEED_CONTROL, off))
            if g is not None and g > 1000:
                log.warning("PID gain %s out of range 0..1000 (0.1 scaling); ignored", g)
                return False

        if ts is not None:
            # With the maintain-value loop enabled the target speed is stored
            # and echoed back but does not move the engine -- the PID owns the
            # speed. A site running Fixed and Maintain Value modes together
            # should confirm this precedence on its module during commissioning.
            self.target_speed = ts
            log.info("Target speed set to %s rpm", ts)
        for off, name in ((P137_P_GAIN, "p_gain"), (P137_I_GAIN, "i_gain"), (P137_D_GAIN, "d_gain")):
            g = updates.get(addr(PAGE_SPEED_CONTROL, off))
            if g is not None:
                setattr(self, name, g)
        sp = updates.get(addr(PAGE_SPEED_CONTROL, P137_SET_POINT))
        if sp is not None:
            requested = _from_s16(sp)
            self.set_point = max(self.set_point_min, min(self.set_point_max, requested))
            if self.set_point != requested:
                log.info("Set point %s clamped to %s", requested, self.set_point)
        self._publish()
        return True

    # ------------------------------------------------------------------
    # Control keys
    # ------------------------------------------------------------------

    def _write_control_key(self, key: int, complement: int) -> bool:
        if complement != 0xFFFF - key:
            log.warning("Rejected control key %s: complement %s != %s", key, complement, 0xFFFF - key)
            return False
        if key not in SUPPORTED_KEYS:
            log.warning("Rejected unsupported control key %s", key)
            return False
        log.info("Control key %s (%s)", key, Key(key).name)
        self.press_key(Key(key))
        return True

    def press_key(self, key: Key) -> None:
        if key is Key.SELECT_STOP:
            self.set_mode(Mode.STOP)
        elif key is Key.SELECT_AUTO:
            self.set_mode(Mode.AUTO)
        elif key is Key.SELECT_MANUAL:
            self.set_mode(Mode.MANUAL)
        elif key is Key.START_ENGINE:
            if self.mode is Mode.MANUAL:
                self.manual_start_requested = True
                if self.phase is Phase.STOPPED and not self._locked_out():
                    self._enter(Phase.CRANKING)
            else:
                log.info("Start key ignored: only acts in Manual mode")
        elif key in (Key.RESET_ALARMS, Key.RESET_ALL_ALARMS):
            self._reset_alarms()
        elif key is Key.TELEMETRY_START:
            if self.mode is Mode.AUTO:
                self.telemetry_start = True
            else:
                log.info("Telemetry start ignored: only acts in Auto mode")
        elif key is Key.CANCEL_TELEMETRY_START:
            self.telemetry_start = False
        elif key is Key.THROTTLE_DOWN:
            self.target_speed = max(self.min_speed, self.target_speed - self.THROTTLE_STEP)
        elif key is Key.THROTTLE_UP:
            self.target_speed = min(self.max_speed, self.target_speed + self.THROTTLE_STEP)
        self._publish()

    # ------------------------------------------------------------------
    # Sim controls
    # ------------------------------------------------------------------

    def set_mode(self, mode: Mode | int | str) -> None:
        if isinstance(mode, str):
            mode = Mode[mode.strip().upper()]
        mode = Mode(mode)
        if mode is self.mode:
            if mode is Mode.STOP:
                self._stop_mode_actions()
            return
        log.info("Mode %s -> %s", self.mode.name, mode.name)
        self.mode = mode
        if mode is Mode.STOP:
            self._stop_mode_actions()
        elif mode is Mode.MANUAL:
            # Selecting Manual from Auto keeps a running set running (the
            # E400 doesn't stop the engine on Auto -> Manual); a set that is
            # only in its start delay abandons the start.
            self.telemetry_start = False
            if self.phase is Phase.START_DELAY:
                self._enter(Phase.STOPPED)
            elif self.phase in (Phase.RETURN_DELAY, Phase.COOLING_DOWN):
                self._enter(Phase.RUNNING)
        elif mode is Mode.AUTO:
            self.manual_start_requested = False
        self._publish()

    def _stop_mode_actions(self) -> None:
        self.telemetry_start = False
        self.manual_start_requested = False
        if self.phase is not Phase.STOPPED:
            self._enter(Phase.STOPPED)
        self._reset_alarms()

    def set_remote_start(self, active: bool) -> None:
        """The remote-start digital input (acts in Auto mode only)."""
        self.remote_start_input = bool(active)

    def sim_start(self) -> None:
        """As if the remote start input went active (Auto mode)."""
        self.set_remote_start(True)

    def sim_stop(self) -> None:
        """Remove every start request; in Manual, stop as the Stop key would."""
        self.remote_start_input = False
        self.telemetry_start = False
        if self.mode is Mode.MANUAL:
            self.manual_start_requested = False
            self._enter(Phase.STOPPED)

    def shortcut_to_running(self) -> None:
        """Test shortcut: Auto mode, start request latched, running at target."""
        self.mode = Mode.AUTO
        self.remote_start_input = True
        self._enter(Phase.RUNNING)
        self.rpm = float(self.target_speed)
        self._pid_rpm = self.rpm
        self.oil_kpa = 350.0
        self.control_source = self.PLANT_GAIN * self.rpm
        self._publish()

    def inject_alarm(self, name: str, condition: Condition | str | int = Condition.SHUTDOWN) -> str:
        key = name.strip().lower()
        if key not in ALARM_POSITIONS:
            raise ValueError(f"unknown alarm name: {name!r}")
        cond = parse_condition(condition) if not isinstance(condition, Condition) else condition
        canonical = _CANONICAL[key]
        self.injected[canonical] = cond
        prev = self.latched.get(canonical)
        if prev is None or SEVERITY.index(cond) < SEVERITY.index(prev):
            self.latched[canonical] = cond
        log.info("Alarm injected: %s (%s)", canonical, cond.name)
        if cond in LOCKOUT and self.phase is not Phase.STOPPED:
            log.info("Lockout alarm: engine stopped")
            self._enter(Phase.STOPPED)
        self._publish()
        return canonical

    def clear_alarm(self, name: str | None = None) -> None:
        """Clear the underlying condition. Lockouts stay latched until a reset."""
        if name is None:
            self.injected.clear()
        else:
            self.injected.pop(_CANONICAL.get(name.strip().lower(), name), None)
        # Warnings are non-latching: they go when the condition goes.
        for n, c in list(self.latched.items()):
            if c is Condition.WARNING and n not in self.injected:
                del self.latched[n]
        self._publish()

    def _reset_alarms(self) -> None:
        for n in list(self.latched):
            if n not in self.injected:
                del self.latched[n]

    def _locked_out(self) -> bool:
        return any(c in LOCKOUT for c in self.latched.values())

    def active_alarms(self) -> dict[str, Condition]:
        return dict(self.latched)

    def alarm_summary(self) -> str:
        if not self.latched:
            return ""
        return ", ".join(f"{n} ({c.name.lower().replace('_', ' ')})" for n, c in self.latched.items())

    @property
    def operating_state(self) -> OperatingState:
        return PHASE_TO_STATE[self.phase]

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------

    def tick(self, dt: float | None = None) -> None:
        """Advance the simulation by ``dt`` seconds (default: time since the last tick)."""
        now = self.clock()
        if dt is None:
            dt = max(0.0, now - self._last_clock)
        self._last_clock = now
        while dt > 1e-9:
            step = min(dt, 0.1)
            self._step(step)
            dt -= step
        self._publish()

    def _enter(self, phase: Phase) -> None:
        if phase is self.phase:
            return
        log.info("Engine %s -> %s", self.phase.value, phase.value)
        self.phase = phase
        self._phase_entered = self.now
        if phase is Phase.CRANKING:
            self.starts += 1
        if phase is Phase.RUNNING:
            self._pid_rpm = max(self.rpm, self.IDLE_RPM)
            self._pid_prev_err = 0.0
            if self.mode is Mode.MANUAL and self.manual_run_time_s > 0:
                self._manual_remaining = self.manual_run_time_s
        if phase is Phase.STOPPED:
            self._manual_remaining = 0.0

    def _start_requested(self) -> bool:
        return self.telemetry_start or self.remote_start_input

    def _step(self, dt: float) -> None:
        self.now += dt
        t = self.now - self._phase_entered
        tm = self.timings

        if self.mode is Mode.STOP:
            self._reset_alarms()  # latched alarms clear once their condition is gone

        locked = self._locked_out()
        if locked and self.phase is not Phase.STOPPED:
            self._enter(Phase.STOPPED)

        # --- lifecycle ---------------------------------------------------
        ph = self.phase
        if self.mode is Mode.AUTO:
            want = self._start_requested() and not locked
            if ph is Phase.STOPPED and want:
                self._enter(Phase.START_DELAY)
            elif ph is Phase.START_DELAY:
                if not want:
                    self._enter(Phase.STOPPED)
                elif t >= tm.start_delay - EPS:
                    self._enter(Phase.CRANKING)
            elif ph in (Phase.CRANKING, Phase.WARMING_UP) and not want:
                # Request removed before the set came on line: go straight
                # to cooling (warm-up) or stop (cranking).
                self._enter(Phase.COOLING_DOWN if ph is Phase.WARMING_UP else Phase.STOPPED)
            elif ph is Phase.RUNNING and not want:
                self._enter(Phase.RETURN_DELAY)
            elif ph is Phase.RETURN_DELAY:
                if want:
                    self._enter(Phase.RUNNING)
                elif t >= tm.return_delay - EPS:
                    self._enter(Phase.COOLING_DOWN)
            elif ph is Phase.COOLING_DOWN:
                if want:
                    self._enter(Phase.RUNNING)
                elif t >= tm.cooldown - EPS:
                    self._enter(Phase.STOPPED)
        if self.phase is Phase.CRANKING and self.now - self._phase_entered >= tm.crank - EPS:
            self._enter(Phase.WARMING_UP)
        elif self.phase is Phase.WARMING_UP and self.now - self._phase_entered >= tm.warmup - EPS:
            self._enter(Phase.RUNNING)
        if self.mode is Mode.MANUAL and self.phase is Phase.RUNNING and self.manual_run_time_s > 0:
            self._manual_remaining = max(0.0, self._manual_remaining - dt)
            if self._manual_remaining <= 0:
                self._enter(Phase.STOPPED)

        # --- engine speed -----------------------------------------------
        ph = self.phase
        if ph in (Phase.STOPPED, Phase.START_DELAY):
            self.rpm = _ramp(self.rpm, 0.0, 600.0, dt)  # spin-down
        elif ph is Phase.CRANKING:
            self.rpm = self.CRANK_RPM
        elif ph in (Phase.WARMING_UP, Phase.COOLING_DOWN):
            self.rpm = _ramp(self.rpm, self.IDLE_RPM, self.WARMUP_RAMP, dt)
        else:  # RUNNING / RETURN_DELAY
            if self.mode is Mode.AUTO and self.maintain_enabled:
                self._run_pid(dt)
                target = self._pid_rpm
            else:
                target = float(self.target_speed)
            self.rpm = _ramp(self.rpm, target, self.RPM_RAMP, dt)

        # --- maintain-value plant -----------------------------------------
        self.control_source = _approach(
            self.control_source, self.PLANT_GAIN * self.rpm, self.PLANT_TAU, dt
        )

        # --- instruments --------------------------------------------------
        firing = ph in (Phase.WARMING_UP, Phase.RUNNING, Phase.RETURN_DELAY, Phase.COOLING_DOWN)
        self.oil_kpa = _approach(self.oil_kpa, 350.0 * min(1.0, self.rpm / self.IDLE_RPM) if firing else 0.0, 1.0, dt)
        self.coolant_c = _approach(self.coolant_c, 85.0 if firing else 25.0, 60.0 if firing else 300.0, dt)
        self.oil_temp_c = _approach(self.oil_temp_c, 95.0 if firing else 25.0, 90.0 if firing else 400.0, dt)
        if firing:
            burn = self.fuel_rate_lph() * dt / 3600.0
            self.fuel_used_l += burn
            self.fuel_pct = max(0.0, self.fuel_pct - burn / 4.0)  # 400 L tank
            self.run_seconds += dt

    def _run_pid(self, dt: float) -> None:
        """Velocity-form PI moving an rpm command so control source -> set point.

        The D gain register is stored and echoed but not used: a derivative
        term on this noiseless first-order plant adds nothing worth testing.
        """
        err = (self.set_point - self.control_source) / 100.0  # bar
        kp, ki = self.p_gain / 10.0, self.i_gain / 10.0
        delta = kp * (err - self._pid_prev_err) + ki * err * dt
        self._pid_prev_err = err
        self._pid_rpm += delta * self.PID_RPM_PER_BAR
        self._pid_rpm = max(float(self.min_speed), min(float(self.max_speed), self._pid_rpm))

    def fuel_rate_lph(self) -> float:
        if self.phase in (Phase.STOPPED, Phase.START_DELAY):
            return 0.0
        return 2.0 + 0.012 * self.rpm * (0.3 + 0.7 * self.load_pct() / 100.0)

    def load_pct(self) -> float:
        if self.phase in (Phase.STOPPED, Phase.START_DELAY, Phase.CRANKING):
            return 0.0
        return max(0.0, min(100.0, 10.0 + 60.0 * (self.rpm - self.IDLE_RPM) / (self.max_speed - self.IDLE_RPM)))

    # ------------------------------------------------------------------
    # Register image
    # ------------------------------------------------------------------

    def _set(self, page: int, offset: int, value: int) -> None:
        self.regs[addr(page, offset)] = int(value) & 0xFFFF

    def _set_u32(self, page: int, offset: int, value: float) -> None:
        hi, lo = _u32(value)
        self._set(page, offset, hi)
        self._set(page, offset + 1, lo)

    def _init_static(self) -> None:
        self._set(PAGE_STATUS, P3_MANUFACTURER, MANUFACTURER_DSE)
        self._set(PAGE_STATUS, P3_MODEL, MODEL_NUMBER_E400)
        self._set_u32(PAGE_STATUS, P3_SERIAL, SERIAL_NUMBER)
        self._set(PAGE_STATUS, P3_PANEL_LOCK, 0)

        for i, word in enumerate(supported_bitmap()):
            self._set(PAGE_CONTROL, P16_SUPPORTED_BITMAP + i, word)

        # Page 137 control-source description (Exxx variation).
        self._set(PAGE_SPEED_CONTROL, P137_CONTROL_SOURCE_UNIT_ID, UNIT_PRESSURE)
        self._set(PAGE_SPEED_CONTROL, P137_CONTROL_SOURCE_MULTIPLIER, _s16(-2))  # raw 250 = 2.50 bar
        self._set(PAGE_SPEED_CONTROL, P137_CONTROL_SOURCE_PRECISION, 2)
        self._set(PAGE_SPEED_CONTROL, P137_CONTROL_SOURCE_RANGE_MIN, _s16(0))
        self._set(PAGE_SPEED_CONTROL, P137_CONTROL_SOURCE_RANGE_MAX, _s16(1000))
        for i, w in enumerate(_suffix("bar")):
            self._set(PAGE_SPEED_CONTROL, P137_CONTROL_SOURCE_SUFFIX + i, w)
        self._set(PAGE_SPEED_CONTROL, P137_DISPLAY_UNITS, 1)  # bar
        self._set(PAGE_SPEED_CONTROL, P137_SET_POINT_TARGET_SOURCE, 0)  # specified value

        # Page 178 unit info, inputs A..G (H..L live in a block the E400 lacks).
        units = [
            (UNIT_PRESSURE, 0, 0, 0, 1600, "kPa"),
            (UNIT_TEMPERATURE, 0, 0, -50, 200, "°C"),
            (UNIT_PERCENTAGE, 0, 0, 0, 100, "%"),
        ] + [(UNIT_NOT_USED, 0, 0, 0, 0, "")] * 4
        for i, (uid, mult, prec, lo, hi, suffix) in enumerate(units):
            base = P178_UNIT_INFO_START + i * P178_UNIT_INFO_STRIDE
            self._set(PAGE_FLEX_SENSORS, base + 0, uid)
            self._set(PAGE_FLEX_SENSORS, base + 1, _s16(mult))
            self._set(PAGE_FLEX_SENSORS, base + 2, prec)
            self._set(PAGE_FLEX_SENSORS, base + 3, _s16(lo))
            self._set(PAGE_FLEX_SENSORS, base + 4, _s16(hi))
            for j, w in enumerate(_suffix(suffix)):
                self._set(PAGE_FLEX_SENSORS, base + 5 + j, w)

        # Counts at offset 0 of the alarm pages.
        self._set(PAGE_NAMED_ALARMS, 0, len(NAMED_ALARMS))
        self._set(PAGE_UNNAMED_ALARMS, 0, UNNAMED_ALARM_REGISTERS * 4)
        # Time to maintenance not configured: s32 unimplemented sentinel.
        self._set(PAGE_ACCUMULATED, P7_TIME_TO_MAINTENANCE, S16_UNIMPLEMENTED)
        self._set(PAGE_ACCUMULATED, P7_TIME_TO_MAINTENANCE + 1, 0xFFFF)

    def _publish(self) -> None:
        """Refresh the dynamic part of the register image from the model state."""
        firing = self.phase in (Phase.WARMING_UP, Phase.RUNNING, Phase.RETURN_DELAY, Phase.COOLING_DOWN)

        # Page 3
        self._set(PAGE_STATUS, P3_CONTROL_MODE, int(self.mode))
        flags = 0
        for cond in self.latched.values():
            flags |= 1 << CONDITION_FLAG[cond]
        self._set(PAGE_STATUS, P3_ALARM_FLAGS, flags)

        # Page 4
        self._set(PAGE_BASIC, P4_OIL_PRESSURE, round(self.oil_kpa))
        self._set(PAGE_BASIC, P4_COOLANT_TEMP, _s16(self.coolant_c))
        self._set(PAGE_BASIC, P4_OIL_TEMP, _s16(self.oil_temp_c))
        self._set(PAGE_BASIC, P4_FUEL_LEVEL, round(self.fuel_pct))
        self._set(PAGE_BASIC, P4_CHARGE_ALT_VOLTS, 141 if firing else 0)
        self._set(PAGE_BASIC, P4_BATTERY_VOLTS, 138 if firing else (118 if self.phase is Phase.CRANKING else 128))
        self._set(PAGE_BASIC, P4_ENGINE_SPEED, round(self.rpm))

        # Page 5
        load = self.load_pct()
        self._set(PAGE_EXTENDED, P5_TURBO_PRESSURE, round(load * 1.5) if firing else 0)
        self._set(PAGE_EXTENDED, P5_INLET_MANIFOLD_TEMP, _s16(self.coolant_c * 0.5 + 15))
        self._set(PAGE_EXTENDED, P5_EXHAUST_TEMP, _s16(150 + 4 * load if firing else self.coolant_c))
        self._set_u32(PAGE_EXTENDED, P5_FUEL_CONSUMPTION, self.fuel_rate_lph() * 100)
        self._set(PAGE_EXTENDED, P5_ATMOSPHERIC_PRESSURE, 101)
        self._set(PAGE_EXTENDED, P5_FUEL_TEMP, _s16(30 + (self.coolant_c - 25) * 0.2))
        self._set(PAGE_EXTENDED, P5_PERCENT_LOAD, round(load))
        self._set(PAGE_EXTENDED, P5_COOLANT_LEVEL, 92)
        self._set(PAGE_EXTENDED, P5_ENGINE_OPERATING_STATE, int(self.operating_state))
        self._set_u32(PAGE_EXTENDED, P5_MANUAL_RUN_TIME_REMAINING, self._manual_remaining * 10)

        # Page 7
        self._set_u32(PAGE_ACCUMULATED, P7_TIME_NOW, self.wall_clock())
        self._set_u32(PAGE_ACCUMULATED, P7_ENGINE_RUN_TIME, self.run_seconds)
        self._set_u32(PAGE_ACCUMULATED, P7_NUMBER_OF_STARTS, self.starts)
        self._set_u32(PAGE_ACCUMULATED, P7_FUEL_USED, self.fuel_used_l)

        # Page 137
        self._set(PAGE_SPEED_CONTROL, P137_TARGET_SPEED, self.target_speed)
        self._set(PAGE_SPEED_CONTROL, P137_P_GAIN, self.p_gain)
        self._set(PAGE_SPEED_CONTROL, P137_I_GAIN, self.i_gain)
        self._set(PAGE_SPEED_CONTROL, P137_D_GAIN, self.d_gain)
        self._set(PAGE_SPEED_CONTROL, P137_SET_POINT, _s16(self.set_point))
        self._set(PAGE_SPEED_CONTROL, P137_SET_POINT_MIN, _s16(self.set_point_min))
        self._set(PAGE_SPEED_CONTROL, P137_SET_POINT_MAX, _s16(self.set_point_max))
        self._set(PAGE_SPEED_CONTROL, P137_CONTROL_SOURCE_VALUE, _s16(self.control_source))

        # Page 178 readings A..L
        a = self.flex_a_kpa if self.flex_a_kpa is not None else self.control_source  # 0.01 bar == 1 kPa
        b = self.flex_b_c if self.flex_b_c is not None else self.coolant_c
        c = self.flex_c_pct if self.flex_c_pct is not None else self.fuel_pct
        readings = [_s16(a), _s16(b), _s16(c)] + [S16_UNIMPLEMENTED] * (P178_READINGS_COUNT - 3)
        for i, w in enumerate(readings):
            self._set(PAGE_FLEX_SENSORS, i, w)

        # Alarm pages
        self._publish_alarms()

    def _publish_alarms(self) -> None:
        named = [[Condition.INACTIVE] * 4 for _ in range(NAMED_ALARM_REGISTERS)]
        # Positions past the end of the named list are unimplemented.
        for i in range(len(NAMED_ALARMS), NAMED_ALARM_REGISTERS * 4):
            named[i // 4][i % 4] = Condition.UNIMPLEMENTED
        unnamed = [[Condition.UNIMPLEMENTED] * 4 for _ in range(UNNAMED_ALARM_REGISTERS)]
        for (off, nib) in UNNAMED_ALARMS:
            unnamed[off - 1][nib] = Condition.INACTIVE
        for name, cond in self.latched.items():
            page, off, nib = ALARM_POSITIONS[name.lower()]
            (named if page == PAGE_NAMED_ALARMS else unnamed)[off - 1][nib] = cond
        for page, regs in ((PAGE_NAMED_ALARMS, named), (PAGE_UNNAMED_ALARMS, unnamed)):
            for i, nibs in enumerate(regs):
                word = (nibs[0] << 12) | (nibs[1] << 8) | (nibs[2] << 4) | nibs[3]
                self._set(page, 1 + i, word)
