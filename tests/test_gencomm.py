"""Unit tests for the GenComm register map and the DSE snapshot decoder.

These pin down the arithmetic the rest of the app rests on: page/offset to
address, sentinel handling, signed and 32-bit words, alarm nibble unpacking,
control-key payloads, and the snapshot's Kensho-shaped property surface.
"""

from __future__ import annotations

import pytest

from dse_engine_controller import gencomm as gc
from dse_engine_controller.dse_state import (
    RUNNING_RPM_THRESHOLD,
    DseModel,
    DseSnapshot,
    DseStateStore,
)


# ---------------------------------------------------------------------------
# Address math
# ---------------------------------------------------------------------------


def test_address_is_page_times_256_plus_offset():
    # The operator manual's own examples.
    assert gc.address(4, 5) == 0x0405  # battery volts
    assert gc.address(3, 4) == 0x0304  # control mode
    assert gc.address(3, 6) == 0x0306  # alarm flags
    assert gc.address(16, 8) == 0x1008  # control key
    assert gc.address(137, 147) == 35219  # target speed
    assert gc.address(137, 154) == 35226  # maintain value set point


def test_address_rejects_out_of_range():
    with pytest.raises(ValueError):
        gc.address(256, 0)
    with pytest.raises(ValueError):
        gc.address(0, 256)


def test_read_plan_blocks_fit_one_modbus_request():
    for block in gc.READ_PLAN:
        assert 0 < block.count <= 125, block
        assert block.start + block.count <= 256, block


def test_read_plan_fast_tier_covers_what_the_loop_needs():
    fast = {(b.page, b.start) for b in gc.blocks(gc.Tier.FAST)}
    assert (gc.PAGE_STATUS, 0) in fast
    assert (gc.PAGE_BASIC, 0) in fast
    assert (gc.PAGE_SPEED_CONTROL, gc.P137_TARGET_SPEED) in fast
    assert (gc.PAGE_NAMED_ALARMS, 0) in fast


# ---------------------------------------------------------------------------
# Word decoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, 0),
        (1234, 1234),
        (0xFFF7, 0xFFF7),
        (0xFFF8, None),  # reserved sentinel
        (0xFFF9, None),  # low digital input
        (0xFFFD, None),  # under range
        (0xFFFE, None),  # over range
        (0xFFFF, None),  # unimplemented
        (None, None),
    ],
)
def test_u16_sentinels(raw, expected):
    assert gc.u16(raw) == expected


def test_u16_scaling():
    assert gc.u16(138, 0.1) == pytest.approx(13.8)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, 0),
        (85, 85),
        (0xFFFF, -1),
        (0xFFCE, -50),
        (0x8000, -32768),
        (0x7FF7, 0x7FF7),
        (0x7FF8, None),
        (0x7FFD, None),  # under range
        (0x7FFE, None),  # over range
        (0x7FFF, None),  # unimplemented
    ],
)
def test_s16_sentinels_and_sign(raw, expected):
    assert gc.s16(raw) == expected


def test_u32_high_word_first():
    assert gc.u32(1, 0) == 65536
    assert gc.u32(0, 3600) == 3600
    assert gc.u32(0xFFFF, 0xFFFF) is None
    assert gc.u32(0xFFFF, 0xFFF8) is None
    assert gc.u32(0xFFFF, 0xFFF7) == 0xFFFFFFF7
    assert gc.u32(0, 150, 0.01) == pytest.approx(1.5)


def test_s32():
    assert gc.s32(0xFFFF, 0xFFFF) == -1
    assert gc.s32(0x7FFF, 0xFFFF) is None
    assert gc.s32(0x7FFF, 0xFFF7) == 0x7FFFFFF7


def test_to_s16_round_trip():
    for value in (-32768, -250, -1, 0, 1, 250, 0x7FF7):
        assert gc.s16(gc.to_s16(value)) == value
    with pytest.raises(ValueError):
        gc.to_s16(40000)


def test_sentinel_label():
    assert gc.sentinel_label(0xFFFF) == "unimplemented"
    assert gc.sentinel_label(0xFFFC) == "transducer fault"
    assert gc.sentinel_label(42) is None


def test_scaled_treats_multiplier_as_power_of_ten():
    assert gc.scaled(250, -2, 2) == pytest.approx(2.5)
    assert gc.scaled(250, 0, 0) == 250
    assert gc.scaled(7, 1, 0) == 70
    assert gc.scaled(None, -2) is None
    # Nonsense multiplier (a sentinel that got through) is ignored, not exploded.
    assert gc.scaled(5, 300) == 5


def test_unscale_inverts_scaled():
    assert gc.unscale(2.5, -2) == 250
    assert gc.unscale(2.504, -2) == 250
    assert gc.unscale(70, 1) == 7
    assert gc.unscale(1500, 0) == 1500


def test_decode_string():
    words = [ord(c) for c in "bar"] + [0x20] * 7
    assert gc.decode_string(words) == "bar"
    assert gc.decode_string([0x20] * 10) == ""
    assert gc.decode_string(None) == ""
    assert gc.decode_string([ord("k"), ord("P"), ord("a"), 0, ord("x")]) == "kPa"


# ---------------------------------------------------------------------------
# Control keys
# ---------------------------------------------------------------------------


def test_control_key_pair_is_key_and_complement():
    # The operator manual's example: Auto = 35701 and 65535 - 35701.
    assert gc.control_key_pair(gc.ControlKey.SELECT_AUTO) == [35701, 29834]
    assert sum(gc.control_key_pair(gc.ControlKey.TELEMETRY_START)) == 0xFFFF


def test_control_key_function_codes():
    assert gc.ControlKey.SELECT_STOP.function_code == 0
    assert gc.ControlKey.SELECT_AUTO.function_code == 1
    assert gc.ControlKey.TELEMETRY_START.function_code == 32
    assert gc.ControlKey.RESET_ALL_ALARMS.function_code == 34
    assert gc.ControlKey.THROTTLE_UP.function_code == 78


def test_key_supported_reads_msb_first_bitmap():
    # Function 0 is bit 16/16 (MSB) of word 0; function 32 is MSB of word 2.
    bitmap = [0] * 8
    bitmap[0] = 1 << 15  # function 0 (Stop)
    bitmap[0] |= 1 << 14  # function 1 (Auto)
    bitmap[2] = 1 << 15  # function 32 (Telemetry start)
    assert gc.key_supported(bitmap, gc.ControlKey.SELECT_STOP) is True
    assert gc.key_supported(bitmap, gc.ControlKey.SELECT_AUTO) is True
    assert gc.key_supported(bitmap, gc.ControlKey.SELECT_MANUAL) is False
    assert gc.key_supported(bitmap, gc.ControlKey.TELEMETRY_START) is True
    assert gc.key_supported(bitmap, gc.ControlKey.CANCEL_TELEMETRY_START) is False
    assert gc.key_supported(None, gc.ControlKey.SELECT_STOP) is None


# ---------------------------------------------------------------------------
# Alarms
# ---------------------------------------------------------------------------


def test_alarm_nibbles_first_alarm_in_top_nibble():
    word = (2 << 12) | (1 << 8) | (3 << 4) | 15
    conds = gc.alarm_nibbles(word)
    assert conds == [
        gc.AlarmCondition.WARNING,
        gc.AlarmCondition.INACTIVE,
        gc.AlarmCondition.SHUTDOWN,
        gc.AlarmCondition.UNIMPLEMENTED,
    ]
    assert gc.alarm_nibbles(None) == [gc.AlarmCondition.UNIMPLEMENTED] * 4


def test_decode_named_alarms_names_and_positions():
    words = [0x1111] * gc.NAMED_ALARM_REGISTERS  # everything inactive
    words[0] = (1 << 12) | (3 << 8) | (1 << 4) | 1  # low oil pressure shutdown
    words[2] = (1 << 12) | (2 << 8) | (1 << 4) | 1  # fail to start warning
    alarms = gc.decode_named_alarms(words)
    assert [(a.name, a.condition) for a in alarms] == [
        ("Low oil pressure", gc.AlarmCondition.SHUTDOWN),
        ("Fail to start", gc.AlarmCondition.WARNING),
    ]
    assert alarms[0].offset == 1 and alarms[0].nibble == 1
    assert alarms[1].offset == 3 and alarms[1].nibble == 1


def test_decode_named_alarms_skips_unimplemented_and_indications_off():
    words = [0xFFFF] * gc.NAMED_ALARM_REGISTERS
    words[1] = (8 << 12) | (9 << 8) | (0 << 4) | 1
    assert gc.decode_named_alarms(words) == []


def test_decode_unnamed_alarms_uses_e400_names():
    words = [0xFFFF] * gc.UNNAMED_ALARM_REGISTERS_E400
    words[45 - 1] = (10 << 12) | 0xFFF  # maintenance alarm 1 active indication
    words[39 - 1] = 0xF3FF  # flexible sensor A high, shutdown
    alarms = gc.decode_unnamed_alarms(words)
    assert ("Engine maintenance 1 due", gc.AlarmCondition.ACTIVE_INDICATION) in [
        (a.name, a.condition) for a in alarms
    ]
    assert ("Flexible sensor A high", gc.AlarmCondition.SHUTDOWN) in [
        (a.name, a.condition) for a in alarms
    ]


def test_worst_alarm_orders_by_severity():
    alarms = [
        gc.Alarm("a", gc.AlarmCondition.WARNING, 154, 1, 0),
        gc.Alarm("b", gc.AlarmCondition.CONTROLLED_SHUTDOWN, 154, 1, 1),
        gc.Alarm("c", gc.AlarmCondition.SHUTDOWN, 154, 1, 2),
    ]
    assert gc.worst(alarms).name == "c"
    assert gc.worst(alarms[:2]).name == "b"
    assert gc.worst([]) is None


def test_named_alarm_table_matches_standard():
    assert len(gc.NAMED_ALARMS) == 58
    assert gc.NAMED_ALARMS[0] == "Emergency stop"
    assert gc.NAMED_ALARMS[9] == "Fail to start"
    assert gc.NAMED_ALARMS[57] == "Incorrect speed"


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def image(**pages) -> dict[int, int]:
    """Build a sparse register image from ``{page: {offset: word}}``."""
    out: dict[int, int] = {}
    for page, words in pages.items():
        for offset, word in words.items():
            out[gc.address(int(page), offset)] = word
    return out


def _base_image(overrides: dict | None = None) -> dict[int, int]:
    """A healthy E400 in Auto, stopped, no alarms.

    ``overrides`` is ``{page: {offset: word}}`` merged over the base.
    """
    pages = {
        gc.PAGE_STATUS: {0: 1, 1: 400, 2: 0, 3: 12345, 4: gc.ControlMode.AUTO, 5: 0xFFFF, 6: 0, 7: 0},
        gc.PAGE_BASIC: {0: 0, 1: 25, 2: 0x7FFF, 3: 80, 4: 0, 5: 128, 6: 0},
        gc.PAGE_EXTENDED: {gc.P5_ENGINE_OPERATING_STATE: gc.EngineOperatingState.STOPPED},
        gc.PAGE_NAMED_ALARMS: {0: 58, **{i: 0x1111 for i in range(1, 16)}},
        gc.PAGE_SPEED_CONTROL: {
            gc.P137_TARGET_SPEED: 1500,
            gc.P137_P_GAIN: 10,
            gc.P137_I_GAIN: 5,
            gc.P137_D_GAIN: 0,
            gc.P137_SET_POINT: 250,
            gc.P137_SET_POINT_MIN: 0,
            gc.P137_SET_POINT_MAX: 1000,
            gc.P137_CONTROL_SOURCE_VALUE: 150,
            gc.P137_CONTROL_SOURCE_UNIT_ID: gc.UnitId.PRESSURE,
            gc.P137_CONTROL_SOURCE_MULTIPLIER: gc.to_s16(-2),
            gc.P137_CONTROL_SOURCE_PRECISION: 2,
            gc.P137_CONTROL_SOURCE_RANGE_MIN: 0,
            gc.P137_CONTROL_SOURCE_RANGE_MAX: 1000,
            **{gc.P137_CONTROL_SOURCE_SUFFIX + i: w for i, w in enumerate([ord("b"), ord("a"), ord("r")] + [0x20] * 7)},
            gc.P137_DISPLAY_UNITS: 1,
            gc.P137_SET_POINT_TARGET_SOURCE: 0,
        },
    }
    for page, words in (overrides or {}).items():
        pages.setdefault(page, {}).update(words)
    return image(**{str(k): v for k, v in pages.items()})


def test_snapshot_identity_and_mode():
    snap = DseSnapshot(_base_image(), 0.0)
    assert snap.manufacturer_code == 1
    assert snap.model_number == 400
    assert snap.serial_number == 12345
    assert snap.control_mode is gc.ControlMode.AUTO
    assert snap.is_auto and not snap.is_manual and not snap.is_stop_mode
    assert snap.control_mode_label == "Auto"


def test_snapshot_unknown_control_mode_is_unimplemented():
    snap = DseSnapshot(_base_image({gc.PAGE_STATUS: {4: 0xFFFF}}), 0.0)
    assert snap.control_mode is gc.ControlMode.UNIMPLEMENTED
    assert not snap.is_auto
    snap = DseSnapshot({}, 0.0)
    assert snap.control_mode is gc.ControlMode.UNIMPLEMENTED


def test_snapshot_basic_instrumentation_and_sentinels():
    snap = DseSnapshot(_base_image(), 0.0)
    assert snap.battery_voltage == pytest.approx(12.8)
    assert snap.coolant_temp_c == 25
    assert snap.oil_temp_c is None  # 0x7FFF unimplemented
    assert snap.fuel_level_pct == 80
    assert snap.engine_rpm == 0
    assert snap.oil_pressure_kpa == 0
    # Not read yet (slow tier) → None, not zero.
    assert snap.engine_load_pct is None
    assert snap.total_engine_hours is None
    assert snap.fuel_rate_lph is None


def test_snapshot_running_from_operating_state_or_rpm():
    stopped = DseSnapshot(_base_image(), 0.0)
    assert not stopped.is_running
    running = DseSnapshot(
        _base_image({gc.PAGE_BASIC: {6: 1500}, gc.PAGE_EXTENDED: {128: gc.EngineOperatingState.RUNNING}}), 0.0
    )
    assert running.is_running and not running.is_warming_up
    warming = DseSnapshot(
        _base_image({gc.PAGE_BASIC: {6: 900}, gc.PAGE_EXTENDED: {128: gc.EngineOperatingState.WARMING_UP}}), 0.0
    )
    assert warming.is_running and warming.is_warming_up and not warming.is_cooling_down
    cooling = DseSnapshot(_base_image({gc.PAGE_EXTENDED: {128: gc.EngineOperatingState.COOLING_DOWN}}), 0.0)
    assert cooling.is_running and cooling.is_cooling_down
    # No operating state (mechanical engine) → speed decides.
    rpm_only = DseSnapshot(_base_image({gc.PAGE_BASIC: {6: RUNNING_RPM_THRESHOLD}, gc.PAGE_EXTENDED: {128: 0xFFFF}}), 0.0)
    assert rpm_only.is_running
    assert rpm_only.engine_operating_state is None
    cranking = DseSnapshot(_base_image({gc.PAGE_BASIC: {6: 150}, gc.PAGE_EXTENDED: {128: 0xFFFF}}), 0.0)
    assert not cranking.is_running


def test_snapshot_faults_from_alarm_pages():
    words = {1: (1 << 12) | (3 << 8) | (1 << 4) | 1}  # low oil pressure shutdown
    snap = DseSnapshot(_base_image({gc.PAGE_NAMED_ALARMS: words}), 0.0)
    assert snap.is_faulted
    assert snap.has_common_alarm
    assert snap.fault_string == "Low oil pressure"
    assert snap.display_state == "Fault: Low oil pressure"
    assert snap.alarm_count == 1
    assert snap.alarm_summary == "Low oil pressure (Shutdown)"
    assert [a.name for a in snap.lockout_alarms] == ["Low oil pressure"]


def test_snapshot_warning_is_not_a_fault():
    words = {4: (1 << 12) | (1 << 8) | (1 << 4) | 2}  # low fuel level warning
    snap = DseSnapshot(_base_image({gc.PAGE_NAMED_ALARMS: words}), 0.0)
    assert not snap.is_faulted
    assert snap.has_warning and snap.has_common_alarm
    assert snap.fault_string == "Low fuel level"
    assert snap.display_state == "Stopped (warning: Low fuel level)"


def test_snapshot_faults_from_status_word_alone():
    snap = DseSnapshot(_base_image({gc.PAGE_STATUS: {6: 1 << gc.FLAG_SHUTDOWN}}), 0.0)
    assert snap.is_faulted
    assert snap.fault_string == "Shutdown alarm"
    snap = DseSnapshot(_base_image({gc.PAGE_STATUS: {6: 1 << gc.FLAG_CONTROLLED_SHUTDOWN}}), 0.0)
    assert snap.is_faulted and snap.fault_string == "Controlled shutdown"
    snap = DseSnapshot(_base_image({gc.PAGE_STATUS: {6: 1 << gc.FLAG_WARNING}}), 0.0)
    assert not snap.is_faulted and snap.has_warning


def test_snapshot_display_state():
    assert DseSnapshot(_base_image(), 0.0).display_state == "Stopped"
    manual_running = DseSnapshot(
        _base_image({gc.PAGE_STATUS: {4: gc.ControlMode.MANUAL}, gc.PAGE_BASIC: {6: 1500}, gc.PAGE_EXTENDED: {128: 3}}),
        0.0,
    )
    assert manual_running.display_state == "Manual Running"
    pre = DseSnapshot(_base_image({gc.PAGE_EXTENDED: {128: gc.EngineOperatingState.PRE_START}}), 0.0)
    assert pre.display_state == "Starting"


def test_snapshot_maintain_value_block():
    snap = DseSnapshot(_base_image(), 0.0)
    m = snap.maintain
    assert m.available
    assert m.target_speed_rpm == 1500
    assert snap.remote_rpm_setpoint == 1500
    assert m.set_point == pytest.approx(2.5)
    assert m.set_point_min == 0
    assert m.set_point_max == pytest.approx(10.0)
    assert m.control_value == pytest.approx(1.5)
    assert m.units == "bar"
    assert m.p_gain == pytest.approx(1.0)
    assert m.target_source_label == "Specified value"
    assert m.raw_set_point_for(3.25) == 325
    assert m.raw_set_point_for(-1.0) == gc.to_s16(-100)


def test_snapshot_maintain_value_unavailable():
    snap = DseSnapshot(
        _base_image({gc.PAGE_SPEED_CONTROL: {gc.P137_SET_POINT: 0x7FFF, gc.P137_CONTROL_SOURCE_UNIT_ID: 0xFFFF}}), 0.0
    )
    assert not snap.maintain.available
    assert snap.maintain.set_point is None
    assert snap.maintain.target_speed_rpm == 1500  # target speed still there


def test_snapshot_flex_sensors():
    base = gc.flex_unit_info_offset(0)
    page = {
        0: 350,
        1: 0x7FFF,
        base + gc.P178_UNIT_ID: gc.UnitId.PRESSURE,
        base + gc.P178_MULTIPLIER: gc.to_s16(-1),
        base + gc.P178_PRECISION: 1,
        base + gc.P178_RANGE_MIN: 0,
        base + gc.P178_RANGE_MAX: 1600,
        **{base + gc.P178_SUFFIX + i: w for i, w in enumerate([ord("k"), ord("P"), ord("a")] + [0x20] * 7)},
    }
    snap = DseSnapshot(_base_image({gc.PAGE_FLEX_SENSORS: page}), 0.0)
    a = snap.flex_sensor(0)
    assert a.letter == "A" and a.configured and not a.is_digital
    assert a.value == pytest.approx(35.0)
    assert a.range_max == pytest.approx(160.0)
    assert a.units == "kPa"
    b = snap.flex_sensor(1)
    assert b.value is None and not b.configured
    assert len(snap.flex_sensors) == 7
    assert len(DseSnapshot(snap.registers, 0.0, DseModel.E800).flex_sensors) == 12


def test_snapshot_accumulated_and_extended():
    snap = DseSnapshot(
        _base_image(
            {
                gc.PAGE_ACCUMULATED: {6: 0, 7: 7200, 8: 0, 9: 42, 10: 0, 11: 999},
                gc.PAGE_EXTENDED: {10: 0, 11: 150, 74: 55, 79: 90, 254: 0, 255: 6000},
            }
        ),
        0.0,
    )
    assert snap.total_engine_hours == pytest.approx(2.0)
    assert snap.number_of_starts == 42
    assert snap.fuel_used_l == 999
    assert snap.fuel_rate_lph == pytest.approx(1.5)
    assert snap.engine_load_pct == 55
    assert snap.coolant_level_pct == 90
    assert snap.manual_run_time_remaining_s == pytest.approx(600.0)


def test_snapshot_panel_lock():
    assert not DseSnapshot(_base_image(), 0.0).panel_locked
    locked = DseSnapshot(_base_image({gc.PAGE_STATUS: {38: 1 << gc.LOCK_EXTERNAL}}), 0.0)
    assert locked.panel_locked
    user_only = DseSnapshot(_base_image({gc.PAGE_STATUS: {38: 1 << gc.LOCK_USER_CONTROLS}}), 0.0)
    assert not user_only.panel_locked  # fascia lock doesn't block telemetry


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_store_tracks_contactability_and_last_fault():
    store = DseStateStore()
    assert store.latest is None and not store.is_contactable
    store.update(_base_image())
    assert store.is_contactable and store.num_records == 1
    assert not store.has_lost_comms()
    store.update(_base_image({gc.PAGE_NAMED_ALARMS: {1: 0x1311}}))
    assert store.last_fault_string == "Low oil pressure"
    store.update(None)
    store.update({})
    store.update(None)
    assert not store.is_contactable
    assert store.iters_uncontactable == 3
    assert store.has_lost_comms(timeout_seconds=0)
    assert not store.has_lost_comms(timeout_seconds=3600)
    assert store.latest is not None  # last good snapshot stays available


def test_store_never_contacted_counts_iterations():
    store = DseStateStore()
    store.update(None)
    store.update(None)
    assert not store.has_lost_comms(min_iters=3)
    store.update(None)
    assert store.has_lost_comms(min_iters=3)


def test_merge_writes_sparse_image():
    img: dict[int, int] = {}
    gc.merge(img, gc.address(4, 0), [1, 2, 0x1FFFF])
    assert img == {1024: 1, 1025: 2, 1026: 0xFFFF}
