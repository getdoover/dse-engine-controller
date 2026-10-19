"""DSE GenComm (Modbus RTU) register map for the E-series engine controllers.

Reference: "GenComm standard for use with generating set control equipment",
DSE, v2.228, 16/07/2020 (kept in the team's Tech Resources/DSE folder). Only
the pages this app reads or writes are described here; the full standard is
276 pages and most of it is generator-only.

Addressing
----------
GenComm is organised as 256 pages of 256 holding registers. A page/offset pair
maps to a plain Modbus holding-register address as ``page * 256 + offset``, so
page 4 offset 5 (engine battery volts) is address 1029 (hex 0x0405). All reads
are Modbus function 3 and all writes function 16; Doover's ``modbus_interface``
calls both "register_type=4" (see the doover-facts note on the inverted type
codes).

Bit numbering
-------------
The standard labels bits 1..16 with 16 the most significant. Everything here
is converted to 0-based shifts, so a field the standard calls ``11/16`` is
``(value >> 10) & 1``. The alarm flag positions follow the standard; the
decoder also reads the alarm pages themselves, so a fault is never inferred
from the status word alone.

Sentinels
---------
A register the module doesn't implement, or an instrument that is out of
range, returns a sentinel rather than an exception: 0xFFFF and the values just
below it for unsigned words, 0x7FFF and the values just below it for signed.
:func:`u16` / :func:`s16` / :func:`u32` return ``None`` for any of them, so a
reading that is absent never decodes to a plausible number.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

PAGE_SIZE = 256


def address(page: int, offset: int) -> int:
    """Modbus holding-register address for a GenComm page/offset."""
    if not 0 <= page < 256 or not 0 <= offset < PAGE_SIZE:
        raise ValueError(f"GenComm page/offset out of range: {page}/{offset}")
    return page * PAGE_SIZE + offset


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

PAGE_STATUS = 3  # Generating set status information
PAGE_BASIC = 4  # Basic instrumentation
PAGE_EXTENDED = 5  # Extended instrumentation (J1939 derived)
PAGE_ACCUMULATED = 7  # Accumulated instrumentation (run time, starts, fuel)
PAGE_CONTROL = 16  # Control registers (system control keys)
PAGE_DTC = 17  # J1939 active trouble codes, decoded
PAGE_SPEED_CONTROL = 137  # Active speed control (Exxx: target speed, maintain value)
PAGE_UNNAMED_ALARMS = 153  # Unnamed alarm conditions (inputs, flex senders, PLC)
PAGE_NAMED_ALARMS = 154  # Named alarm conditions
PAGE_FLEX_SENSORS = 178  # Internal analogue sensor readings and unit info

# --- Page 3: status -------------------------------------------------------
P3_MANUFACTURER = 0
P3_MODEL = 1
P3_SERIAL = 2  # 32-bit, offsets 2-3
P3_CONTROL_MODE = 4
P3_ALARM_FLAGS = 6
P3_PANEL_LOCK = 38

# Page 3 offset 6 alarm flag bits (0-based shifts; standard numbers them 1-16).
FLAG_NOT_CONFIGURED = 15
FLAG_CONTROL_UNIT_FAILURE = 13
FLAG_SHUTDOWN = 12
FLAG_ELECTRICAL_TRIP = 11
FLAG_WARNING = 10
FLAG_TELEMETRY = 9
FLAG_CONTROLLED_SHUTDOWN = 6

# Page 3 offset 38 panel-lock bits.
LOCK_EXTERNAL = 15
LOCK_STOP_AND_PANEL = 14
LOCK_USER_CONTROLS = 13
LOCK_TELEMETRY = 12


class ControlMode(enum.IntEnum):
    STOP = 0
    AUTO = 1
    MANUAL = 2
    TEST_ON_LOAD = 3
    AUTO_MANUAL_RESTORE = 4
    USER_CONFIG = 5
    TEST_OFF_LOAD = 6
    OFF = 7
    UNIMPLEMENTED = 0xFFFF

    @property
    def label(self) -> str:
        return {
            ControlMode.STOP: "Stop",
            ControlMode.AUTO: "Auto",
            ControlMode.MANUAL: "Manual",
            ControlMode.TEST_ON_LOAD: "Test on load",
            ControlMode.AUTO_MANUAL_RESTORE: "Auto (manual restore)",
            ControlMode.USER_CONFIG: "Configuration",
            ControlMode.TEST_OFF_LOAD: "Test off load",
            ControlMode.OFF: "Off",
        }.get(self, "Unknown")


def control_mode(raw: int | None) -> ControlMode:
    try:
        return ControlMode(raw)
    except (ValueError, TypeError):
        return ControlMode.UNIMPLEMENTED


# --- Page 4: basic instrumentation ------------------------------------------
P4_OIL_PRESSURE = 0  # kPa, u16
P4_COOLANT_TEMP = 1  # °C, s16
P4_OIL_TEMP = 2  # °C, s16
P4_FUEL_LEVEL = 3  # %, u16
P4_CHARGE_ALT_VOLTS = 4  # 0.1 V, u16
P4_BATTERY_VOLTS = 5  # 0.1 V, u16
P4_ENGINE_SPEED = 6  # rpm, u16
P4_COUNT = 7

# --- Page 5: extended instrumentation ---------------------------------------
P5_TURBO_PRESSURE = 4  # kPa, u16
P5_INLET_MANIFOLD_TEMP = 6  # °C, s16
P5_EXHAUST_TEMP = 8  # °C, s16
P5_FUEL_CONSUMPTION = 10  # 0.01 L/h, u32 (offsets 10-11)
P5_ATMOSPHERIC_PRESSURE = 14  # kPa
P5_FUEL_TEMP = 15  # °C, s16
P5_HEAD_COUNT = 19  # offsets 0..18 are one contiguous read
P5_PERCENT_LOAD = 74  # %, u16
P5_ACCELERATOR = 75
P5_OIL_LEVEL = 77  # %
P5_CRANK_CASE_PRESSURE = 78
P5_COOLANT_LEVEL = 79  # %, u16
P5_LOAD_BLOCK_START = 74
P5_LOAD_BLOCK_COUNT = 6  # 74..79
P5_ENGINE_OPERATING_STATE = 128
P5_MANUAL_RUN_TIME_REMAINING = 254  # 0.1 s, u32 (254-255), E400 v4.0+


class EngineOperatingState(enum.IntEnum):
    """Page 5 offset 128 (J1939 engine operating state)."""

    STOPPED = 0
    PRE_START = 1
    WARMING_UP = 2
    RUNNING = 3
    COOLING_DOWN = 4
    ENGINE_STOPPED = 5
    POST_RUN = 6
    NOT_AVAILABLE = 15

    @property
    def label(self) -> str:
        return {
            EngineOperatingState.STOPPED: "Stopped",
            EngineOperatingState.PRE_START: "Pre-start",
            EngineOperatingState.WARMING_UP: "Warming up",
            EngineOperatingState.RUNNING: "Running",
            EngineOperatingState.COOLING_DOWN: "Cooling down",
            EngineOperatingState.ENGINE_STOPPED: "Stopped",
            EngineOperatingState.POST_RUN: "Post run",
        }.get(self, "Unknown")


def engine_operating_state(raw: int | None) -> EngineOperatingState | None:
    """Decode the operating state, or ``None`` when the module doesn't report it."""
    if raw is None:
        return None
    try:
        state = EngineOperatingState(raw)
    except ValueError:
        return None
    return None if state is EngineOperatingState.NOT_AVAILABLE else state


# --- Page 7: accumulated instrumentation (Exxx allocation) ------------------
P7_TIME_NOW = 0  # seconds since 1/1/70, u32
P7_TIME_TO_MAINTENANCE = 2  # s32
P7_ENGINE_RUN_TIME = 6  # seconds, u32 (6-7)
P7_NUMBER_OF_STARTS = 8  # u32 (8-9)
P7_FUEL_USED = 10  # litres, u32 (10-11)
P7_COUNT = 12

# --- Page 16: control -------------------------------------------------------
P16_SUPPORTED_BITMAP = 0  # offsets 0..7, one bit per control function
P16_SUPPORTED_COUNT = 8
P16_CONTROL_KEY = 8  # write key here...
P16_CONTROL_KEY_COMPLEMENT = 9  # ...and 65535 - key here, in one write


class ControlKey(enum.IntEnum):
    """System control keys (page 16 offset 8). Function code = key - 35700."""

    SELECT_STOP = 35700
    SELECT_AUTO = 35701
    SELECT_MANUAL = 35702
    SELECT_TEST_ON_LOAD = 35703
    SELECT_AUTO_MANUAL_RESTORE = 35704
    START_ENGINE = 35705  # only in manual / test modes
    MUTE_ALARM = 35706
    RESET_ALARMS = 35707
    TELEMETRY_START = 35732  # start if in auto mode
    CANCEL_TELEMETRY_START = 35733
    RESET_ALL_ALARMS = 35734  # resets any alarm condition that can be reset
    CLEAR_TELEMETRY_FLAG = 35735
    LOCK_USER_CONTROLS = 35736
    UNLOCK_USER_CONTROLS = 35737
    SELECT_OFF = 35776
    THROTTLE_DOWN = 35777
    THROTTLE_UP = 35778
    WAKE_ECU = 35779
    LAMP_TEST = 35780

    @property
    def function_code(self) -> int:
        return int(self) - 35700


def control_key_pair(key: int) -> list[int]:
    """The two-register payload for a control key write: key and its complement."""
    key = int(key)
    if not 0 <= key <= 0xFFFF:
        raise ValueError(f"control key out of range: {key}")
    return [key, 0xFFFF - key]


def key_supported(bitmap: list[int] | None, key: int) -> bool | None:
    """Whether the module advertises support for a control function.

    ``bitmap`` is page 16 offsets 0..7 as read. Function *n* lives in register
    ``n // 16``; within it the standard puts function 0 at bit 16/16 (the MSB),
    so function ``n % 16`` is ``15 - n % 16`` as a 0-based shift. ``None`` when
    the bitmap hasn't been read.
    """
    if not bitmap or len(bitmap) < P16_SUPPORTED_COUNT:
        return None
    code = int(key) - 35700
    if not 0 <= code < 128:
        return False
    word = int(bitmap[code // 16])
    return bool((word >> (15 - code % 16)) & 1)


# --- Page 137: active speed control (Exxx variation) -------------------------
P137_TARGET_SPEED = 147  # rpm, u16, read/write
P137_P_GAIN = 151  # 0.1, read/write
P137_I_GAIN = 152
P137_D_GAIN = 153
P137_SET_POINT = 154  # s16 in control-source units, read/write
P137_SET_POINT_MIN = 155  # s16
P137_SET_POINT_MAX = 156  # s16
P137_CONTROL_SOURCE_VALUE = 157  # s16
P137_CONTROL_SOURCE_UNIT_ID = 158
P137_CONTROL_SOURCE_MULTIPLIER = 159  # s16
P137_CONTROL_SOURCE_PRECISION = 160
P137_CONTROL_SOURCE_RANGE_MIN = 161  # s16
P137_CONTROL_SOURCE_RANGE_MAX = 162  # s16
P137_CONTROL_SOURCE_SUFFIX = 163  # 10 registers of UTF-16, 163..172
P137_DISPLAY_UNITS = 173
P137_SET_POINT_TARGET_SOURCE = 174
P137_BLOCK_START = P137_TARGET_SPEED
P137_BLOCK_COUNT = P137_SET_POINT_TARGET_SOURCE - P137_TARGET_SPEED + 1  # 28

TARGET_SOURCE_SPECIFIED_VALUE = 0  # set point comes from register 154 / front panel
TARGET_SOURCE_PLC_STORE_BASE = 256  # 256..263 = PLC store 0..7
TARGET_SOURCE_PLC_REGISTER_BASE = 512  # 512..531 = PLC register 0..19
TARGET_SOURCE_NOT_USED = 0xFFFF

TARGET_SPEED_MAX = 8000


def target_source_label(raw: int | None) -> str:
    if raw is None or raw == TARGET_SOURCE_NOT_USED:
        return "Not used"
    if raw == TARGET_SOURCE_SPECIFIED_VALUE:
        return "Specified value"
    if TARGET_SOURCE_PLC_STORE_BASE <= raw < TARGET_SOURCE_PLC_STORE_BASE + 8:
        return f"PLC store {raw - TARGET_SOURCE_PLC_STORE_BASE}"
    if TARGET_SOURCE_PLC_REGISTER_BASE <= raw < TARGET_SOURCE_PLC_REGISTER_BASE + 20:
        return f"PLC register {raw - TARGET_SOURCE_PLC_REGISTER_BASE}"
    return f"Unknown ({raw})"


# --- Page 178: internal analogue (flexible) sensors --------------------------
P178_READINGS = 0  # offsets 0..11: inputs A..L, s16
P178_READINGS_COUNT = 12
P178_UNIT_INFO_START = 16  # unit-info blocks start here, 15 registers each
P178_UNIT_INFO_STRIDE = 15
P178_UNIT_ID = 0  # offsets within a unit-info block
P178_MULTIPLIER = 1  # s16
P178_PRECISION = 2
P178_RANGE_MIN = 3  # s16
P178_RANGE_MAX = 4  # s16
P178_SUFFIX = 5  # 10 registers UTF-16
P178_SUFFIX_LEN = 10

FLEX_SENSOR_LETTERS = "ABCDEFGHIJKL"


def flex_unit_info_offset(index: int) -> int:
    """Page 178 offset of the unit-info block for flexible input ``index`` (0 = A)."""
    return P178_UNIT_INFO_START + index * P178_UNIT_INFO_STRIDE


class UnitId(enum.IntEnum):
    NOT_USED = 0
    TEMPERATURE = 1
    PRESSURE = 2
    PERCENTAGE = 3
    DIGITAL_INPUT = 0xFFFE
    USER_DEFINED = 0xFFFF


# Display units (page 137 offset 173), indexed by (unit id, code).
DISPLAY_UNITS = {
    (UnitId.TEMPERATURE, 0): "°C",
    (UnitId.TEMPERATURE, 1): "°F",
    (UnitId.PRESSURE, 0): "kPa",
    (UnitId.PRESSURE, 1): "bar",
    (UnitId.PRESSURE, 2): "psi",
    (UnitId.PERCENTAGE, 0): "%",
}


# ---------------------------------------------------------------------------
# Value decoding
# ---------------------------------------------------------------------------

# Everything from the "reserved" sentinel upward is a sentinel. Unsigned words
# use 0xFFF8..0xFFFF (unimplemented, over range, under range, transducer
# fault, bad data, high/low digital input, reserved); signed words mirror that
# at 0x7FF8..0x7FFF.
U16_SENTINEL_FLOOR = 0xFFF8
S16_SENTINEL_FLOOR = 0x7FF8
U32_SENTINEL_FLOOR = 0xFFFFFFF8
S32_SENTINEL_FLOOR = 0x7FFFFFF8

SENTINEL_UNIMPLEMENTED = 0xFFFF
SENTINEL_OVER_RANGE = 0xFFFE
SENTINEL_UNDER_RANGE = 0xFFFD
SENTINEL_TRANSDUCER_FAULT = 0xFFFC
SENTINEL_BAD_DATA = 0xFFFB
SENTINEL_HIGH_DIGITAL = 0xFFFA
SENTINEL_LOW_DIGITAL = 0xFFF9

SENTINEL_LABELS = {
    SENTINEL_UNIMPLEMENTED: "unimplemented",
    SENTINEL_OVER_RANGE: "over range",
    SENTINEL_UNDER_RANGE: "under range",
    SENTINEL_TRANSDUCER_FAULT: "transducer fault",
    SENTINEL_BAD_DATA: "bad data",
    SENTINEL_HIGH_DIGITAL: "high digital input",
    SENTINEL_LOW_DIGITAL: "low digital input",
}


def u16(raw: int | None, scale: float = 1.0) -> float | int | None:
    """Unsigned word, or ``None`` for a sentinel / missing register."""
    if raw is None:
        return None
    raw = int(raw) & 0xFFFF
    if raw >= U16_SENTINEL_FLOOR:
        return None
    return raw * scale if scale != 1.0 else raw


def s16(raw: int | None, scale: float = 1.0) -> float | int | None:
    """Signed word, or ``None`` for a sentinel / missing register."""
    if raw is None:
        return None
    raw = int(raw) & 0xFFFF
    if S16_SENTINEL_FLOOR <= raw <= 0x7FFF:
        return None
    if raw >= 0x8000:
        raw -= 0x10000
    return raw * scale if scale != 1.0 else raw


def u32(hi: int | None, lo: int | None, scale: float = 1.0) -> float | int | None:
    """Unsigned double word (high word first), or ``None`` for a sentinel."""
    if hi is None or lo is None:
        return None
    raw = ((int(hi) & 0xFFFF) << 16) | (int(lo) & 0xFFFF)
    if raw >= U32_SENTINEL_FLOOR:
        return None
    return raw * scale if scale != 1.0 else raw


def s32(hi: int | None, lo: int | None, scale: float = 1.0) -> float | int | None:
    if hi is None or lo is None:
        return None
    raw = ((int(hi) & 0xFFFF) << 16) | (int(lo) & 0xFFFF)
    if S32_SENTINEL_FLOOR <= raw <= 0x7FFFFFFF:
        return None
    if raw >= 0x80000000:
        raw -= 0x100000000
    return raw * scale if scale != 1.0 else raw


def to_s16(value: int) -> int:
    """Encode a signed integer as a 16-bit register value."""
    value = int(value)
    if not -0x8000 <= value <= 0x7FFF:
        raise ValueError(f"value does not fit a signed word: {value}")
    return value & 0xFFFF


def sentinel_label(raw: int | None) -> str | None:
    """Why an unsigned instrument reads as absent, if it is a sentinel."""
    if raw is None:
        return None
    return SENTINEL_LABELS.get(int(raw) & 0xFFFF)


def scaled(raw: int | None, multiplier: int | None, precision: int | None = None):
    """Engineering value for a flexible-sensor word given its unit info.

    The standard publishes a ``Multiplier`` (signed, -128..127) and a
    ``Precision`` with each reading but does not spell out the arithmetic. The
    reading is a signed word, so a 2.50 bar sensor can't be carried in it with
    its decimals unless something scales it; this treats the multiplier as a
    power-of-ten exponent (``raw * 10 ** multiplier``) and the precision as the
    number of decimal places the module displays, which is also how the
    Exxx front-panel values come out. When commissioning a flexible sensor
    with a non-zero multiplier, check the published value against the
    module's own display; ``raw`` is kept alongside for that comparison.
    """
    if raw is None:
        return None
    exponent = int(multiplier) if multiplier is not None else 0
    if abs(exponent) > 9:
        exponent = 0  # a sentinel got through, or nonsense config — don't explode
    value = raw * (10.0 ** exponent)
    if precision is not None and 0 <= int(precision) <= 9:
        value = round(value, int(precision))
    return value


def unscale(value: float, multiplier: int | None) -> int:
    """Inverse of :func:`scaled`: engineering value to a raw signed word."""
    exponent = int(multiplier) if multiplier is not None else 0
    if abs(exponent) > 9:
        exponent = 0
    return int(round(float(value) / (10.0 ** exponent)))


def decode_string(words: list[int] | None) -> str:
    """A GenComm Unicode string: one UTF-16 code unit per register, space padded."""
    if not words:
        return ""
    chars = []
    for word in words:
        if word is None:
            break
        code = int(word) & 0xFFFF
        if code == 0:
            break
        chars.append(chr(code))
    return "".join(chars).strip()


# ---------------------------------------------------------------------------
# Alarms (pages 153 / 154)
# ---------------------------------------------------------------------------


class AlarmCondition(enum.IntEnum):
    DISABLED = 0
    INACTIVE = 1
    WARNING = 2
    SHUTDOWN = 3
    ELECTRICAL_TRIP = 4
    CONTROLLED_SHUTDOWN = 5
    INACTIVE_INDICATION = 8
    INACTIVE_INDICATION_STRING = 9
    ACTIVE_INDICATION = 10
    UNIMPLEMENTED = 15

    @property
    def label(self) -> str:
        return {
            AlarmCondition.WARNING: "Warning",
            AlarmCondition.SHUTDOWN: "Shutdown",
            AlarmCondition.ELECTRICAL_TRIP: "Electrical trip",
            AlarmCondition.CONTROLLED_SHUTDOWN: "Controlled shutdown",
            AlarmCondition.ACTIVE_INDICATION: "Indication",
        }.get(self, self.name.replace("_", " ").title())

    @property
    def is_active(self) -> bool:
        return self in ACTIVE_CONDITIONS

    @property
    def is_lockout(self) -> bool:
        """Whether this condition stops the engine and holds it stopped."""
        return self in LOCKOUT_CONDITIONS


ACTIVE_CONDITIONS = frozenset(
    {
        AlarmCondition.WARNING,
        AlarmCondition.SHUTDOWN,
        AlarmCondition.ELECTRICAL_TRIP,
        AlarmCondition.CONTROLLED_SHUTDOWN,
        AlarmCondition.ACTIVE_INDICATION,
    }
)
LOCKOUT_CONDITIONS = frozenset(
    {
        AlarmCondition.SHUTDOWN,
        AlarmCondition.ELECTRICAL_TRIP,
        AlarmCondition.CONTROLLED_SHUTDOWN,
    }
)

# Severity order for picking "the" fault to show: most serious first.
SEVERITY_ORDER = (
    AlarmCondition.SHUTDOWN,
    AlarmCondition.CONTROLLED_SHUTDOWN,
    AlarmCondition.ELECTRICAL_TRIP,
    AlarmCondition.WARNING,
    AlarmCondition.ACTIVE_INDICATION,
)


def alarm_condition(raw: int) -> AlarmCondition:
    try:
        return AlarmCondition(int(raw) & 0xF)
    except ValueError:
        return AlarmCondition.UNIMPLEMENTED


def alarm_nibbles(word: int | None) -> list[AlarmCondition]:
    """The four alarm conditions packed in one register, first alarm first.

    The standard packs alarm 1 of a register in bits 13-16 (the top nibble),
    alarm 2 in bits 9-12, and so on down.
    """
    if word is None:
        return [AlarmCondition.UNIMPLEMENTED] * 4
    word = int(word) & 0xFFFF
    return [alarm_condition((word >> shift) & 0xF) for shift in (12, 8, 4, 0)]


# Page 154, Exxx allocation: 58 named alarms, four per register from offset 1.
NAMED_ALARMS = (
    "Emergency stop",
    "Low oil pressure",
    "High coolant temperature",
    "Low coolant temperature",
    "Under speed",
    "Over speed",
    "Battery low voltage",
    "Battery high voltage",
    "Charge alternator failure",
    "Fail to start",
    "Fail to stop",
    "Oil pressure sender fault",
    "Loss of magnetic pick up",
    "Magnetic pick up open circuit",
    "Calibration lost",
    "Low fuel level",
    "CAN ECU warning",
    "CAN ECU shutdown",
    "CAN ECU data fail",
    "Low oil level switch",
    "High temperature switch",
    "Low fuel level switch",
    "Expansion unit watchdog",
    "Fuel usage running",
    "Fuel usage stopped",
    "Protections disabled",
    "Protections blocked",
    "Air flap closed",
    "Charge air temperature",
    "High coolant temperature electrical trip",
    "Temperature sender open circuit",
    "ECU protect",
    "ECU malfunction",
    "ECU information",
    "ECU shutdown",
    "ECU warning",
    "ECU electrical trip",
    "ECU after-treatment",
    "ECU water in fuel",
    "ECU heater",
    "ECU cooler",
    "MSC data error",
    "MSC ID error",
    "MSC failure",
    "MSC too few sets",
    "MSC alarms inhibited",
    "MSC old version units",
    "LCD heater low voltage",
    "LCD heater high voltage",
    "LCD heater temperature",
    "LCD heater sensor failure",
    "DEF level low",
    "SCR inducement",
    "PWM(i) 1 active",
    "PWM(i) 2 active",
    "PWM(i) 3 active",
    "PWM(i) 4 active",
    "Incorrect speed",
)
NAMED_ALARM_REGISTERS = (len(NAMED_ALARMS) + 3) // 4  # 15
P154_BLOCK_COUNT = NAMED_ALARM_REGISTERS + 1  # offset 0 is the count

# Page 153, Exxx allocation, E400 column. Keyed by (register offset, nibble).
# Anything not listed decodes with a generic name; the module returns 15
# (unimplemented) for positions it doesn't have, which are skipped anyway.
_FLEX = FLEX_SENSOR_LETTERS
UNNAMED_ALARMS: dict[tuple[int, int], str] = {
    **{(1, n): f"Digital input {chr(ord('A') + n)}" for n in range(4)},
    **{(5, n): f"Flexible input {_FLEX[n]} (digital)" for n in range(4)},
    **{(6, n): f"Flexible input {_FLEX[4 + n]} (digital)" for n in range(4)},
    **{(7, n): f"Flexible input {_FLEX[8 + n]} (digital)" for n in range(4)},
    (39, 0): "Flexible sensor A low",
    (39, 1): "Flexible sensor A high",
    (39, 2): "Flexible sensor B low",
    (39, 3): "Flexible sensor B high",
    (40, 0): "Flexible sensor C low",
    (40, 1): "Flexible sensor C high",
    (40, 2): "Flexible sensor D low",
    (40, 3): "Flexible sensor D high",
    (41, 0): "Flexible sensor E low",
    (41, 1): "Flexible sensor E high",
    (41, 2): "Flexible sensor F low",
    (41, 3): "Flexible sensor F high",
    (42, 0): "Flexible sensor G low",
    (42, 1): "Flexible sensor G high",
    (42, 2): "Flexible sensor H low",
    (42, 3): "Flexible sensor H high",
    (43, 0): "Flexible sensor I low",
    (43, 1): "Flexible sensor I high",
    (43, 2): "Flexible sensor J low",
    (43, 3): "Flexible sensor J high",
    (44, 0): "Flexible sensor K low",
    (44, 1): "Flexible sensor K high",
    (44, 2): "Flexible sensor L low",
    (44, 3): "Flexible sensor L high",
    **{(45, n): f"Engine maintenance {n + 1} due" for n in range(4)},
    **{(46 + i // 4, i % 4): f"PLC function {i + 1}" for i in range(20)},
    **{(51, n): f"Flexible sensor {_FLEX[n]} fault" for n in range(4)},
    **{(52, n): f"Flexible sensor {_FLEX[4 + n]} fault" for n in range(4)},
    **{(53, n): f"Flexible sensor {_FLEX[8 + n]} fault" for n in range(4)},
    **{(54, n): f"Engine maintenance {5 + n} due" for n in range(4)},
    **{(55, n): f"CAN instrument {n + 1}" for n in range(4)},
    **{(56, n): f"CAN instrument {5 + n}" for n in range(4)},
    (57, 0): "CAN instrument 9",
    (57, 1): "CAN instrument 10",
    (57, 2): "Engine maintenance 9 due",
    (57, 3): "Engine maintenance 10 due",
}
# E400: 228 unnamed alarms over 57 registers; E800: 396 over 99.
UNNAMED_ALARM_REGISTERS_E400 = 57
UNNAMED_ALARM_REGISTERS_E800 = 99


@dataclass(frozen=True)
class Alarm:
    """One active alarm, as decoded from pages 153 / 154."""

    name: str
    condition: AlarmCondition
    page: int
    offset: int
    nibble: int

    @property
    def label(self) -> str:
        return f"{self.name} ({self.condition.label})"


def decode_named_alarms(words: list[int] | None) -> list[Alarm]:
    """Active alarms from page 154 offsets 1..15 (``words[0]`` is offset 1)."""
    found: list[Alarm] = []
    if not words:
        return found
    for reg_index, word in enumerate(words):
        for nibble, condition in enumerate(alarm_nibbles(word)):
            if not condition.is_active:
                continue
            index = reg_index * 4 + nibble
            name = (
                NAMED_ALARMS[index]
                if index < len(NAMED_ALARMS)
                else f"Named alarm {index + 1}"
            )
            found.append(Alarm(name, condition, PAGE_NAMED_ALARMS, reg_index + 1, nibble))
    return found


def decode_unnamed_alarms(words: list[int] | None) -> list[Alarm]:
    """Active alarms from page 153 offsets 1..N (``words[0]`` is offset 1)."""
    found: list[Alarm] = []
    if not words:
        return found
    for reg_index, word in enumerate(words):
        offset = reg_index + 1
        for nibble, condition in enumerate(alarm_nibbles(word)):
            if not condition.is_active:
                continue
            name = UNNAMED_ALARMS.get((offset, nibble), f"Input alarm {offset}.{nibble + 1}")
            found.append(Alarm(name, condition, PAGE_UNNAMED_ALARMS, offset, nibble))
    return found


def worst(alarms: list[Alarm]) -> Alarm | None:
    """The most serious active alarm, or ``None``."""
    for severity in SEVERITY_ORDER:
        for alarm in alarms:
            if alarm.condition is severity:
                return alarm
    return None


# ---------------------------------------------------------------------------
# Read plan
# ---------------------------------------------------------------------------


class Tier(enum.Enum):
    FAST = "fast"  # every poll
    SLOW = "slow"  # every slow_period seconds
    STARTUP = "startup"  # once at startup and rarely after


@dataclass(frozen=True)
class Block:
    page: int
    start: int
    count: int
    tier: Tier
    label: str

    @property
    def address(self) -> int:
        return address(self.page, self.start)

    @property
    def end(self) -> int:
        return self.address + self.count


# What one poll reads. Blocks are contiguous runs of *defined* registers, so a
# module that raises an exception for reserved offsets (the standard permits
# either an exception or a sentinel) can't fail a whole read. Modbus allows
# 125 registers per request; nothing here goes near it.
READ_PLAN: tuple[Block, ...] = (
    Block(PAGE_STATUS, 0, 8, Tier.FAST, "status"),
    Block(PAGE_BASIC, 0, P4_COUNT, Tier.FAST, "basic instrumentation"),
    Block(PAGE_EXTENDED, P5_ENGINE_OPERATING_STATE, 1, Tier.FAST, "engine state"),
    Block(PAGE_SPEED_CONTROL, P137_BLOCK_START, P137_BLOCK_COUNT, Tier.FAST, "speed control"),
    Block(PAGE_NAMED_ALARMS, 0, P154_BLOCK_COUNT, Tier.FAST, "named alarms"),
    Block(PAGE_EXTENDED, 0, P5_HEAD_COUNT, Tier.SLOW, "extended instrumentation"),
    Block(PAGE_EXTENDED, P5_LOAD_BLOCK_START, P5_LOAD_BLOCK_COUNT, Tier.SLOW, "engine load"),
    Block(PAGE_EXTENDED, P5_MANUAL_RUN_TIME_REMAINING, 2, Tier.SLOW, "manual run time"),
    Block(PAGE_ACCUMULATED, 0, P7_COUNT, Tier.SLOW, "accumulated"),
    Block(PAGE_UNNAMED_ALARMS, 0, UNNAMED_ALARM_REGISTERS_E400 + 1, Tier.SLOW, "unnamed alarms"),
    Block(PAGE_FLEX_SENSORS, 0, P178_READINGS_COUNT, Tier.SLOW, "flex sensor readings"),
    Block(PAGE_STATUS, P3_PANEL_LOCK, 1, Tier.SLOW, "panel lock"),
    Block(PAGE_CONTROL, 0, P16_SUPPORTED_COUNT, Tier.STARTUP, "supported control keys"),
    Block(
        PAGE_FLEX_SENSORS,
        P178_UNIT_INFO_START,
        P178_UNIT_INFO_STRIDE * 7,  # inputs A..G; E800 reads a second block
        Tier.STARTUP,
        "flex sensor units",
    ),
)


def blocks(tier: Tier) -> list[Block]:
    return [b for b in READ_PLAN if b.tier is tier]


def merge(image: dict[int, int], start_address: int, values: list[int]) -> None:
    """Write a block of read values into a sparse register image."""
    for i, value in enumerate(values):
        image[start_address + i] = int(value) & 0xFFFF
