"""Tests for the DSE E400 simulator's pure model (simulators/dse/model.py).

The model has no pymodbus / pydoover imports, so it's imported straight from
the simulator directory and driven with explicit ``tick(dt)`` calls. Register
addresses come from the app's gencomm.py so the sim and the app can't drift
apart silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dse_engine_controller import gencomm as gc

SIM_DIR = Path(__file__).resolve().parent.parent / "simulators" / "dse"
if str(SIM_DIR) not in sys.path:
    sys.path.insert(0, str(SIM_DIR))

import model as sim  # noqa: E402

A = gc.address
P137 = gc.PAGE_SPEED_CONTROL
KEY_ADDR = A(gc.PAGE_CONTROL, gc.P16_CONTROL_KEY)


@pytest.fixture
def m():
    return sim.E400Model(clock=lambda: 0.0, wall_clock=lambda: 1_700_000_000.0)


def reg(m, page, offset):
    return m.read(A(page, offset), 1)[0]


def rpm(m):
    return reg(m, gc.PAGE_BASIC, gc.P4_ENGINE_SPEED)


def op_state(m):
    return gc.engine_operating_state(reg(m, gc.PAGE_EXTENDED, gc.P5_ENGINE_OPERATING_STATE))


def key(m, k):
    return m.write(KEY_ADDR, gc.control_key_pair(k))


def run_for(m, seconds, dt=0.1):
    for _ in range(int(round(seconds / dt))):
        m.tick(dt)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_auto_telemetry_start_full_cycle(m):
    S = gc.EngineOperatingState
    assert key(m, gc.ControlKey.SELECT_AUTO)
    assert gc.control_mode(reg(m, gc.PAGE_STATUS, gc.P3_CONTROL_MODE)) is gc.ControlMode.AUTO
    assert op_state(m) is S.STOPPED
    starts0 = gc.u32(*m.read(A(gc.PAGE_ACCUMULATED, gc.P7_NUMBER_OF_STARTS), 2))

    assert key(m, gc.ControlKey.TELEMETRY_START)
    m.tick(0.1)
    assert op_state(m) is S.PRE_START  # start delay
    assert rpm(m) == 0

    run_for(m, 2.0)  # start delay over -> cranking
    assert op_state(m) is S.PRE_START
    assert rpm(m) == 150
    assert gc.u32(*m.read(A(gc.PAGE_ACCUMULATED, gc.P7_NUMBER_OF_STARTS), 2)) == starts0 + 1

    run_for(m, 3.0)  # crank -> warm-up
    assert op_state(m) is S.WARMING_UP
    run_for(m, 5.0)
    assert rpm(m) == 900  # idle

    run_for(m, 5.0)  # warm-up -> running
    assert op_state(m) is S.RUNNING
    run_for(m, 30.0)
    assert reg(m, gc.PAGE_BASIC, gc.P4_OIL_PRESSURE) > 300
    assert reg(m, gc.PAGE_BASIC, gc.P4_CHARGE_ALT_VOLTS) == 141
    run_time = gc.u32(*m.read(A(gc.PAGE_ACCUMULATED, gc.P7_ENGINE_RUN_TIME), 2))

    assert key(m, gc.ControlKey.CANCEL_TELEMETRY_START)
    m.tick(0.1)
    assert op_state(m) is S.RUNNING  # return delay
    run_for(m, 2.0)
    assert op_state(m) is S.COOLING_DOWN
    run_for(m, 5.0)
    assert rpm(m) == 900
    run_for(m, 5.0)
    assert op_state(m) is S.STOPPED
    run_for(m, 3.0)
    assert rpm(m) == 0
    assert reg(m, gc.PAGE_BASIC, gc.P4_CHARGE_ALT_VOLTS) == 0
    assert gc.u32(*m.read(A(gc.PAGE_ACCUMULATED, gc.P7_ENGINE_RUN_TIME), 2)) > run_time


def test_telemetry_start_ignored_outside_auto(m):
    assert key(m, gc.ControlKey.TELEMETRY_START)  # accepted, but Stop mode
    run_for(m, 3.0)
    assert op_state(m) is gc.EngineOperatingState.STOPPED


def test_manual_start_key_and_stop_key(m):
    assert key(m, gc.ControlKey.SELECT_MANUAL)
    assert gc.control_mode(reg(m, gc.PAGE_STATUS, gc.P3_CONTROL_MODE)) is gc.ControlMode.MANUAL
    assert key(m, gc.ControlKey.START_ENGINE)
    m.tick(0.1)
    assert rpm(m) == 150  # cranking straight away, no start delay
    run_for(m, 3.0 + 10.0 + 10.0)
    assert op_state(m) is gc.EngineOperatingState.RUNNING
    assert rpm(m) == 1500  # tracks target speed in Manual

    assert key(m, gc.ControlKey.SELECT_STOP)
    m.tick(0.1)
    assert op_state(m) is gc.EngineOperatingState.STOPPED
    assert gc.control_mode(reg(m, gc.PAGE_STATUS, gc.P3_CONTROL_MODE)) is gc.ControlMode.STOP


def test_start_key_ignored_in_auto(m):
    key(m, gc.ControlKey.SELECT_AUTO)
    assert key(m, gc.ControlKey.START_ENGINE)
    run_for(m, 1.0)
    assert op_state(m) is gc.EngineOperatingState.STOPPED


# ---------------------------------------------------------------------------
# Control key write semantics
# ---------------------------------------------------------------------------


def test_control_key_requires_complement(m):
    k = int(gc.ControlKey.SELECT_AUTO)
    assert m.write(KEY_ADDR, [k, k]) is False
    assert m.write(KEY_ADDR, [k]) is False  # key without its complement
    assert m.write(KEY_ADDR + 1, [0xFFFF - k]) is False
    assert reg(m, gc.PAGE_STATUS, gc.P3_CONTROL_MODE) == gc.ControlMode.STOP
    assert key(m, k) is True
    assert reg(m, gc.PAGE_STATUS, gc.P3_CONTROL_MODE) == gc.ControlMode.AUTO


def test_unsupported_key_rejected(m):
    assert key(m, gc.ControlKey.SELECT_TEST_ON_LOAD) is False
    assert key(m, gc.ControlKey.LAMP_TEST) is False
    assert reg(m, gc.PAGE_STATUS, gc.P3_CONTROL_MODE) == gc.ControlMode.STOP


def test_supported_bitmap_matches_honoured_keys(m):
    bitmap = m.read(A(gc.PAGE_CONTROL, gc.P16_SUPPORTED_BITMAP), gc.P16_SUPPORTED_COUNT)
    for k in gc.ControlKey:
        expected = int(k) in sim.SUPPORTED_KEYS
        assert gc.key_supported(bitmap, k) is expected, k
        # An advertised key is accepted; an unadvertised one is rejected.
        fresh = sim.E400Model(clock=lambda: 0.0)
        assert fresh.write(KEY_ADDR, gc.control_key_pair(k)) is expected, k
    # Spot-check the bit math directly: function n -> bit 15 - n % 16 of word n // 16.
    assert bitmap[0] & (1 << 15)  # function 0, Select Stop
    assert bitmap[2] & (1 << 15)  # function 32, Telemetry start
    assert bitmap[4] & (1 << (15 - 13))  # function 77, Throttle down


def test_throttle_keys_move_target_speed(m):
    key(m, gc.ControlKey.THROTTLE_UP)
    assert reg(m, P137, gc.P137_TARGET_SPEED) == 1550
    key(m, gc.ControlKey.THROTTLE_DOWN)
    key(m, gc.ControlKey.THROTTLE_DOWN)
    assert reg(m, P137, gc.P137_TARGET_SPEED) == 1450


def test_read_only_and_unimplemented_writes_rejected(m):
    assert m.write(A(gc.PAGE_BASIC, gc.P4_ENGINE_SPEED), [1234]) is False
    assert m.write(A(P137, gc.P137_SET_POINT_MIN), [0]) is False
    assert m.write(A(gc.PAGE_CONTROL, 0), [0xFFFF]) is False


# ---------------------------------------------------------------------------
# Alarms
# ---------------------------------------------------------------------------


def test_injected_shutdown_stops_engine_and_sets_flags(m):
    m.shortcut_to_running()
    m.tick(0.1)
    assert op_state(m) is gc.EngineOperatingState.RUNNING

    m.inject_alarm("Low oil pressure", "shutdown")
    m.tick(0.1)
    assert op_state(m) is gc.EngineOperatingState.STOPPED
    flags = reg(m, gc.PAGE_STATUS, gc.P3_ALARM_FLAGS)
    assert (flags >> gc.FLAG_SHUTDOWN) & 1
    assert not (flags >> gc.FLAG_WARNING) & 1

    words = m.read(A(gc.PAGE_NAMED_ALARMS, 0), gc.P154_BLOCK_COUNT)
    assert words[0] == 58
    # Low oil pressure is named alarm 2: register 1, second nibble.
    assert gc.alarm_nibbles(words[1])[1] is gc.AlarmCondition.SHUTDOWN
    alarms = gc.decode_named_alarms(words[1:])
    assert [(a.name, a.condition) for a in alarms] == [
        ("Low oil pressure", gc.AlarmCondition.SHUTDOWN)
    ]

    # Still running request, but locked out: it doesn't restart.
    run_for(m, 5.0)
    assert op_state(m) is gc.EngineOperatingState.STOPPED


def test_alarm_reset_needs_condition_cleared(m):
    key(m, gc.ControlKey.SELECT_AUTO)
    m.inject_alarm("Over speed", "shutdown")
    key(m, gc.ControlKey.RESET_ALARMS)
    assert (reg(m, gc.PAGE_STATUS, gc.P3_ALARM_FLAGS) >> gc.FLAG_SHUTDOWN) & 1  # still present

    m.clear_alarm("Over speed")
    assert (reg(m, gc.PAGE_STATUS, gc.P3_ALARM_FLAGS) >> gc.FLAG_SHUTDOWN) & 1  # latched
    key(m, gc.ControlKey.RESET_ALL_ALARMS)
    assert reg(m, gc.PAGE_STATUS, gc.P3_ALARM_FLAGS) == 0
    assert gc.decode_named_alarms(m.read(A(gc.PAGE_NAMED_ALARMS, 1), 15)) == []


def test_stop_key_clears_latched_shutdown(m):
    m.shortcut_to_running()
    m.inject_alarm("Fail to start", "shutdown")
    m.clear_alarm("Fail to start")
    m.tick(0.1)
    assert (reg(m, gc.PAGE_STATUS, gc.P3_ALARM_FLAGS) >> gc.FLAG_SHUTDOWN) & 1
    assert key(m, gc.ControlKey.SELECT_STOP)
    m.tick(0.1)
    assert reg(m, gc.PAGE_STATUS, gc.P3_ALARM_FLAGS) == 0
    assert gc.decode_named_alarms(m.read(A(gc.PAGE_NAMED_ALARMS, 1), 15)) == []


def test_warning_does_not_stop_engine(m):
    m.shortcut_to_running()
    m.inject_alarm("High coolant temperature", "warning")
    run_for(m, 1.0)
    assert op_state(m) is gc.EngineOperatingState.RUNNING
    assert (reg(m, gc.PAGE_STATUS, gc.P3_ALARM_FLAGS) >> gc.FLAG_WARNING) & 1
    m.clear_alarm("High coolant temperature")
    assert reg(m, gc.PAGE_STATUS, gc.P3_ALARM_FLAGS) == 0  # warnings self-clear


def test_controlled_shutdown_and_trip_flags(m):
    m.shortcut_to_running()
    m.inject_alarm("Battery low voltage", "controlled shutdown")
    m.inject_alarm("Charge alternator failure", "electrical trip")
    m.tick(0.1)
    flags = reg(m, gc.PAGE_STATUS, gc.P3_ALARM_FLAGS)
    assert (flags >> gc.FLAG_CONTROLLED_SHUTDOWN) & 1
    assert (flags >> gc.FLAG_ELECTRICAL_TRIP) & 1
    assert op_state(m) is gc.EngineOperatingState.STOPPED


def test_unnamed_alarm_page_defaults_and_injection(m):
    words = m.read(A(gc.PAGE_UNNAMED_ALARMS, 0), gc.UNNAMED_ALARM_REGISTERS_E400 + 1)
    for reg_index, word in enumerate(words[1:]):
        for nibble, cond in enumerate(gc.alarm_nibbles(word)):
            expected = (
                gc.AlarmCondition.INACTIVE
                if (reg_index + 1, nibble) in gc.UNNAMED_ALARMS
                else gc.AlarmCondition.UNIMPLEMENTED
            )
            assert cond is expected, (reg_index + 1, nibble)
    m.inject_alarm("Flexible sensor A high", "warning")
    words = m.read(A(gc.PAGE_UNNAMED_ALARMS, 1), gc.UNNAMED_ALARM_REGISTERS_E400)
    assert [a.name for a in gc.decode_unnamed_alarms(words)] == ["Flexible sensor A high"]


def test_sim_alarm_tables_match_gencomm():
    assert sim.NAMED_ALARMS == gc.NAMED_ALARMS
    assert sim.UNNAMED_ALARMS == gc.UNNAMED_ALARMS


def test_unknown_alarm_name_rejected(m):
    with pytest.raises(ValueError):
        m.inject_alarm("Not an alarm", "shutdown")


# ---------------------------------------------------------------------------
# Page 137: target speed and Maintain Value
# ---------------------------------------------------------------------------


def test_target_speed_write_moves_rpm_when_maintain_disabled(m):
    m.maintain_enabled = False
    m.shortcut_to_running()
    run_for(m, 1.0)
    assert rpm(m) == 1500
    assert m.write(A(P137, gc.P137_TARGET_SPEED), [1800]) is True
    assert reg(m, P137, gc.P137_TARGET_SPEED) == 1800
    m.tick(1.0)
    assert 1550 <= rpm(m) <= 1650  # ~100 rpm/s ramp
    run_for(m, 3.0)
    assert rpm(m) == 1800


def test_target_speed_out_of_range_rejected(m):
    assert m.write(A(P137, gc.P137_TARGET_SPEED), [500]) is False
    assert m.write(A(P137, gc.P137_TARGET_SPEED), [3000]) is False
    assert reg(m, P137, gc.P137_TARGET_SPEED) == 1500


def test_target_speed_echoed_but_ignored_while_maintaining(m):
    m.shortcut_to_running()
    run_for(m, 20.0)
    before = rpm(m)
    assert m.write(A(P137, gc.P137_TARGET_SPEED), [2100]) is True
    assert reg(m, P137, gc.P137_TARGET_SPEED) == 2100
    run_for(m, 5.0)
    assert abs(rpm(m) - before) < 20


def test_maintain_set_point_clamped_and_converges(m):
    m.shortcut_to_running()
    block = m.read(A(P137, gc.P137_BLOCK_START), gc.P137_BLOCK_COUNT)
    off = lambda o: block[o - gc.P137_BLOCK_START]  # noqa: E731
    lo, hi = gc.s16(off(gc.P137_SET_POINT_MIN)), gc.s16(off(gc.P137_SET_POINT_MAX))
    assert gc.s16(off(gc.P137_CONTROL_SOURCE_MULTIPLIER)) == -2
    assert off(gc.P137_CONTROL_SOURCE_UNIT_ID) == gc.UnitId.PRESSURE
    assert gc.decode_string(block[gc.P137_CONTROL_SOURCE_SUFFIX - gc.P137_BLOCK_START:][:10]) == "bar"
    assert off(gc.P137_SET_POINT_TARGET_SOURCE) == gc.TARGET_SOURCE_SPECIFIED_VALUE

    # Over max: clamped, not rejected.
    assert m.write(A(P137, gc.P137_SET_POINT), [gc.to_s16(hi + 500)]) is True
    assert gc.s16(reg(m, P137, gc.P137_SET_POINT)) == hi
    assert m.write(A(P137, gc.P137_SET_POINT), [gc.to_s16(-5)]) is True
    assert gc.s16(reg(m, P137, gc.P137_SET_POINT)) == lo

    assert m.write(A(P137, gc.P137_SET_POINT), [gc.to_s16(350)]) is True
    run_for(m, 40.0)
    pv = gc.s16(reg(m, P137, gc.P137_CONTROL_SOURCE_VALUE))
    assert abs(pv - 350) <= 3
    assert gc.scaled(pv, -2, 2) == pytest.approx(3.5, abs=0.03)
    assert m.min_speed <= rpm(m) <= m.max_speed


def test_pid_gains_writable(m):
    assert m.write(A(P137, gc.P137_P_GAIN), [30, 7, 1]) is True
    assert m.read(A(P137, gc.P137_P_GAIN), 3) == [30, 7, 1]


# ---------------------------------------------------------------------------
# Register image
# ---------------------------------------------------------------------------


def test_unimplemented_addresses_return_sentinel(m):
    assert m.read(A(200, 0), 4) == [0xFFFF] * 4
    assert m.read(A(P137, 148), 3) == [0xFFFF] * 3  # reserved inside the read block
    assert gc.u16(reg(m, gc.PAGE_EXTENDED, 0)) is None


def test_every_read_plan_block_is_served(m):
    m.tick(0.1)
    for block in gc.READ_PLAN:
        values = m.read(block.address, block.count)
        assert len(values) == block.count
        assert all(0 <= v <= 0xFFFF for v in values)


def test_status_and_instruments(m):
    assert reg(m, gc.PAGE_STATUS, gc.P3_PANEL_LOCK) == 0
    assert reg(m, gc.PAGE_BASIC, gc.P4_BATTERY_VOLTS) == 128
    assert gc.u32(*m.read(A(gc.PAGE_ACCUMULATED, gc.P7_TIME_NOW), 2)) == 1_700_000_000
    m.shortcut_to_running()
    run_for(m, 60.0)
    assert gc.u32(*m.read(A(gc.PAGE_EXTENDED, gc.P5_FUEL_CONSUMPTION), 2)) > 0
    assert reg(m, gc.PAGE_EXTENDED, gc.P5_PERCENT_LOAD) > 0
    assert reg(m, gc.PAGE_EXTENDED, gc.P5_COOLANT_LEVEL) == 92
    assert gc.s16(reg(m, gc.PAGE_BASIC, gc.P4_COOLANT_TEMP)) > 25


def test_flex_sensors(m):
    m.shortcut_to_running()
    run_for(m, 5.0)
    readings = m.read(A(gc.PAGE_FLEX_SENSORS, 0), gc.P178_READINGS_COUNT)
    assert gc.s16(readings[0]) == gc.s16(reg(m, P137, gc.P137_CONTROL_SOURCE_VALUE))
    assert gc.s16(readings[1]) == gc.s16(reg(m, gc.PAGE_BASIC, gc.P4_COOLANT_TEMP))
    assert gc.s16(readings[2]) == reg(m, gc.PAGE_BASIC, gc.P4_FUEL_LEVEL)
    assert all(gc.s16(r) is None for r in readings[3:])

    units = m.read(A(gc.PAGE_FLEX_SENSORS, gc.P178_UNIT_INFO_START), gc.P178_UNIT_INFO_STRIDE * 7)

    def info(i):
        b = units[i * 15 : (i + 1) * 15]
        return b[0], gc.s16(b[1]), b[2], gc.s16(b[3]), gc.s16(b[4]), gc.decode_string(b[5:15])

    assert info(0) == (gc.UnitId.PRESSURE, 0, 0, 0, 1600, "kPa")
    assert info(1)[0] == gc.UnitId.TEMPERATURE
    assert info(2)[0] == gc.UnitId.PERCENTAGE
    assert all(info(i)[0] == gc.UnitId.NOT_USED for i in range(3, 7))

    m.flex_a_kpa = 432
    m.tick(0.1)
    assert gc.s16(reg(m, gc.PAGE_FLEX_SENSORS, 0)) == 432


def test_injectable_clock_drives_tick():
    now = [100.0]
    m = sim.E400Model(clock=lambda: now[0])
    m.set_mode("auto")
    m.sim_start()
    now[0] += 2.5
    m.tick()
    assert m.phase is sim.Phase.CRANKING
