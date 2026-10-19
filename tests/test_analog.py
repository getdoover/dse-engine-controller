"""Tests for native analog inputs: scaling, filtering and fault detection."""

from dse_engine_controller.analog import (
    FAULT_NO_READING,
    FAULT_OPEN_CIRCUIT,
    FAULT_OVER_RANGE,
    AnalogInput,
)


def _current_loop(**kw):
    """A 4-20 mA input scaled 0-1000 kPa, unfiltered unless asked."""
    defaults = dict(signal_min=4.0, signal_max=20.0, value_min=0.0, value_max=1000.0)
    defaults.update(kw)
    return AnalogInput(**defaults)


# ---------------------------------------------------------------------------
# Scaling
# ---------------------------------------------------------------------------


def test_scales_across_the_signal_range():
    a = _current_loop()
    assert a.update(4.0, 1.0) == 0.0
    assert a.update(12.0, 1.0) == 500.0
    assert a.update(20.0, 1.0) == 1000.0
    assert a.fault is False
    assert a.fault_reason == ""


def test_scales_a_reversed_value_range():
    # Ullage-style sensor: 4 mA is full, 20 mA is empty.
    a = _current_loop(value_min=1000.0, value_max=0.0)
    assert a.update(4.0, 1.0) == 1000.0
    assert a.update(12.0, 1.0) == 500.0
    assert a.update(20.0, 1.0) == 0.0


def test_clamps_within_range_slack():
    # Sensors idle a hair under 4 mA at zero — clamp rather than cry fault.
    a = _current_loop()
    assert a.update(3.7, 1.0) == 0.0
    assert a.fault is False
    assert a.update(20.3, 1.0) == 1000.0
    assert a.fault is False


def test_custom_range_scales_from_zero():
    a = AnalogInput(
        signal_min=0.0, signal_max=20.0, value_min=0.0, value_max=200.0,
        fault_below=None,
    )
    assert a.update(0.0, 1.0) == 0.0
    assert a.update(10.0, 1.0) == 100.0
    assert a.fault is False


# ---------------------------------------------------------------------------
# Faults
# ---------------------------------------------------------------------------


def test_open_circuit_holds_the_last_good_value():
    a = _current_loop()
    a.update(12.0, 1.0)
    assert a.update(2.0, 1.0) == 500.0  # held, not scaled from garbage
    assert a.fault is True
    assert a.fault_reason == FAULT_OPEN_CIRCUIT
    assert a.raw == 2.0  # raw still published for commissioning


def test_over_range_faults():
    a = _current_loop()
    a.update(12.0, 1.0)
    a.update(21.0, 1.0)
    assert a.fault_reason == FAULT_OVER_RANGE
    assert a.value == 500.0


def test_missing_reading_faults_and_holds():
    a = _current_loop()
    a.update(12.0, 1.0)
    assert a.update(None, 1.0) == 500.0
    assert a.fault_reason == FAULT_NO_READING


def test_starts_faulted_until_first_reading():
    a = _current_loop()
    assert a.value is None
    assert a.fault is True
    assert a.fault_reason == FAULT_NO_READING


def test_voltage_input_treats_zero_as_valid():
    # 0 V is a real reading, so open-circuit detection is off for voltage.
    a = AnalogInput(
        signal_min=0.0, signal_max=10.0, value_min=0.0, value_max=100.0,
        fault_below=None,
    )
    assert a.update(0.0, 1.0) == 0.0
    assert a.fault is False
    # Well below range is still wrong, though.
    a.update(-1.0, 1.0)
    assert a.fault_reason == FAULT_OPEN_CIRCUIT
    a.update(10.6, 1.0)
    assert a.fault_reason == FAULT_OVER_RANGE


def test_stale_reading_goes_none_after_the_hold_window():
    a = _current_loop(hold_seconds=15.0)
    a.update(12.0, 1.0, now=0.0)

    # The window runs from the first faulted poll, not from the last good one.
    assert a.update(None, 1.0, now=5.0) == 500.0
    # Held while the fault is fresh — a glitch shouldn't blank the gauge.
    assert a.update(None, 1.0, now=15.0) == 500.0
    assert a.update(None, 1.0, now=20.0) == 500.0  # 15s faulted, not yet over
    assert a.stale is False

    # Past the window there's nothing worth standing behind.
    assert a.update(None, 1.0, now=20.1) is None
    assert a.value is None
    assert a.stale is True
    # The last good reading is still known internally, for the filter and for
    # "has this input ever worked".
    assert a.last_good == 500.0


def test_open_circuit_also_expires():
    # Any fault means no trustworthy value, not just a failed read.
    a = _current_loop(hold_seconds=15.0)
    a.update(12.0, 1.0, now=0.0)
    a.update(2.0, 1.0, now=10.0)  # fault starts here
    a.update(2.0, 1.0, now=30.0)  # 20s later — past the window
    assert a.value is None
    assert a.fault_reason == FAULT_OPEN_CIRCUIT
    assert a.raw == 2.0  # raw signal still published — that's the diagnostic


def test_going_stale_does_not_disarm_the_fault_action():
    # has_reading gates the Stop Engine action; blanking the display mustn't
    # make a working-then-broken sensor look like one that never read at all.
    a = _current_loop(hold_seconds=15.0)
    a.update(12.0, 1.0, now=0.0)
    a.update(None, 1.0, now=10.0)
    a.update(None, 1.0, now=60.0)
    assert a.value is None
    assert a.has_reading is True
    assert a.faulted_for(60.0) == 50.0


def test_never_read_input_is_not_stale_it_is_just_empty():
    a = _current_loop(hold_seconds=15.0)
    a.update(None, 1.0, now=0.0)
    assert a.update(None, 1.0, now=60.0) is None
    assert a.has_reading is False
    assert a.stale is False


def test_recovery_republishes_the_value():
    a = _current_loop(hold_seconds=15.0)
    a.update(12.0, 1.0, now=0.0)
    a.update(None, 1.0, now=10.0)
    a.update(None, 1.0, now=60.0)
    assert a.value is None
    assert a.update(16.0, 1.0, now=61.0) == 750.0
    assert a.stale is False
    assert a.fault is False


def test_hold_window_needs_a_timestamp():
    # Without `now` there's no way to measure staleness, so the value is held.
    a = _current_loop(hold_seconds=15.0)
    a.update(12.0, 1.0)
    assert a.update(None, 1.0) == 500.0


def test_fault_clears_on_a_good_reading():
    a = _current_loop()
    a.update(12.0, 1.0)
    a.update(2.0, 1.0, now=10.0)
    assert a.faulted_for(40.0) == 30.0
    a.update(12.0, 1.0, now=45.0)
    assert a.fault is False
    assert a.faulted_for(60.0) == 0.0


def test_fault_duration_measures_from_the_first_faulted_poll():
    a = _current_loop()
    a.update(12.0, 1.0, now=0.0)
    a.update(None, 1.0, now=5.0)
    a.update(None, 1.0, now=8.0)  # still the same fault episode
    assert a.faulted_for(15.0) == 10.0


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_filter_seeds_on_the_first_reading():
    a = _current_loop(filter_seconds=10.0)
    assert a.update(12.0, 2.0) == 500.0  # no ramp from zero at startup


def test_filter_converges_towards_a_step():
    a = _current_loop(filter_seconds=10.0)
    a.update(4.0, 2.0)  # seeds at 0
    first = a.update(20.0, 2.0)
    assert 0 < first < 1000
    for _ in range(60):
        a.update(20.0, 2.0)
    assert round(a.value) == 1000


def test_filter_disabled_passes_through():
    a = _current_loop(filter_seconds=0.0)
    a.update(4.0, 2.0)
    assert a.update(20.0, 2.0) == 1000.0


def test_zero_timestep_holds_the_filtered_value():
    a = _current_loop(filter_seconds=10.0)
    a.update(4.0, 2.0)
    assert a.update(20.0, 0.0) == 0.0


# ---------------------------------------------------------------------------
# Construction from config
# ---------------------------------------------------------------------------


def test_from_slot_carries_the_config_across():
    from dse_engine_controller.sensors import SENSOR_KIND_ANALOG, SensorSlot

    slot = SensorSlot(
        kind=SENSOR_KIND_ANALOG, label="Inlet", sanitised="inlet", tag_key="pin2",
        units="kPa", invert=False, sp_min=0.0, sp_max=800.0,
        pin=2, signal_min=4.0, signal_max=20.0, value_min=0.0, value_max=1600.0,
        fault_below=3.2, filter_seconds=6.0,
    )
    a = AnalogInput.from_slot(slot)
    assert (a.value_min, a.value_max) == (0.0, 1600.0)
    assert a.fault_below == 3.2
    assert a.filter_seconds == 6.0
    assert a.update(12.0, 1.0) == 800.0


# ---------------------------------------------------------------------------
# Regulator robustness (see also test_pump_control.py for the loop integration)
# ---------------------------------------------------------------------------


def _regulator(**kw):
    from dse_engine_controller.regulator import RpmRegulator

    defaults = dict(rpm_min=900, rpm_max=2000)
    defaults.update(kw)
    return RpmRegulator(**defaults)


def test_regulator_holds_through_a_nan_reading():
    """A NaN would poison the integral and then raise in int(round(...)) — which
    takes down the loop while it is controlling a running engine."""
    r = _regulator()
    r.reset(1500)
    assert r.update(float("nan"), 800, 5, 2.0) == 1500
    assert r.update(800, float("nan"), 5, 2.0) == 1500
    assert r.update(float("inf"), 800, 5, 2.0) == 1500
    assert r.update("not a number", 800, 5, 2.0) == 1500
    # Still healthy afterwards — a bad sample doesn't wedge the loop.
    assert r.update(700, 800, 5, 2.0) > 1500


def test_regulator_survives_a_zero_setpoint():
    # Relative error divides by |setpoint|; zero used to divide by 1e-6.
    r = _regulator()
    r.reset(1500)
    out = r.update(measured=5.0, setpoint=0.0, sensitivity=5, dt=2.0, span=100.0)
    # Bounded, and in the right direction: reading is above setpoint, so a
    # direct-acting loop slows down.
    assert 900 <= out < 1500


def test_regulator_scale_floor_tames_a_small_setpoint():
    """A 5 kPa setpoint on a 0-1600 kPa sensor: without a span-based floor, one
    unit of error reads as a huge relative deviation and slams a rail."""
    unfloored = _regulator()
    unfloored.reset(1500)
    naive = unfloored.update(measured=1.0, setpoint=5.0, sensitivity=5, dt=2.0)

    floored = _regulator()
    floored.reset(1500)
    tamed = floored.update(measured=1.0, setpoint=5.0, sensitivity=5, dt=2.0, span=1600.0)

    # Both move up, but dividing by a 5-unit setpoint instead of a fraction of the
    # 1600-unit span makes the same 4-unit error look ~50x bigger.
    assert naive - 1500 > 20 * (tamed - 1500) > 0


def test_regulator_span_floor_leaves_a_normal_setpoint_alone():
    # A setpoint well above the floor behaves exactly as before.
    without = _regulator()
    without.reset(1500)
    with_span = _regulator()
    with_span.reset(1500)
    assert without.update(700, 800, 5, 2.0) == with_span.update(
        700, 800, 5, 2.0, span=1000.0
    )


def test_regulator_ignores_a_negative_sensitivity():
    r = _regulator()
    r.reset(1500)
    assert r.update(700, 800, -5, 2.0) == 1500  # clamped to zero gain, so no move


def test_regulator_with_no_rpm_band_holds_still():
    r = _regulator(rpm_min=1500, rpm_max=1500)
    r.reset(1500)
    assert r.update(700, 800, 5, 2.0) == 1500


# ---------------------------------------------------------------------------
# Sensitivity mapping
# ---------------------------------------------------------------------------


def test_sensitivity_gain_is_geometric_anchored_at_the_top():
    from dse_engine_controller.regulator import (
        REFERENCE_SENSITIVITY,
        SENSITIVITY_DECADE,
        sensitivity_gain,
    )

    assert sensitivity_gain(REFERENCE_SENSITIVITY) == 1.0
    assert round(sensitivity_gain(REFERENCE_SENSITIVITY - SENSITIVITY_DECADE), 6) == 0.1
    # The slider spans about 1.65 decades, all of it below the reference — which is
    # where the range was missing.
    assert 0.031 < sensitivity_gain(1) < 0.033


def test_top_of_the_knob_is_the_old_linear_six():
    """The fastest loop these pumps have needed was "6" on the old linear scale, so
    that's what the top of the new one means. Everything else buys gentleness."""
    from dse_engine_controller.regulator import RpmRegulator, sensitivity_gain

    span = 2000 - 900
    old_kp, old_ki = 0.05 * span * 6, 0.01 * span * 6  # the old linear mapping at 6
    r = RpmRegulator(rpm_min=900, rpm_max=2000)
    gain = sensitivity_gain(10)
    assert round(r.base_kp * span * gain) == round(old_kp)
    assert round(r.base_ki * span * gain, 1) == round(old_ki, 1)


def test_bottom_of_the_knob_is_well_clear_of_the_setting_that_hunted():
    """Station 3 (2026-08-18) hunted — ±40 RPM, ~18s period — at the old linear
    scale's minimum of 1. The bottom of the slider has to be clear of that, without
    being so slow it can't track anything."""
    from dse_engine_controller.regulator import RpmRegulator, sensitivity_gain

    span = 2000 - 900
    hunted_ki = 0.01 * span * 1  # the old scale's minimum, where it hunted
    r = RpmRegulator(rpm_min=900, rpm_max=2000)
    floor_ki = r.base_ki * span * sensitivity_gain(0.1)

    assert floor_ki < hunted_ki / 5  # comfortably gentler...
    assert floor_ki > hunted_ki / 20  # ...but not uselessly so


def test_lower_sensitivity_moves_rpm_less():
    steps = []
    for sensitivity in (1, 3, 5, 7, 10):
        r = _regulator()
        r.reset(1500)
        # A persistent 10% error, integrated over five 2s steps.
        for _ in range(5):
            r.update(measured=900.0, setpoint=1000.0, sensitivity=sensitivity, dt=2.0)
        steps.append(r.output - 1500)

    assert steps == sorted(steps), steps  # monotonic in the knob
    assert steps[-1] > steps[0] * 20  # and the ends are decades apart


def test_zero_sensitivity_still_holds():
    r = _regulator()
    r.reset(1500)
    assert r.update(900.0, 1000.0, 0, 2.0) == 1500


def test_default_sensitivity_is_a_gentle_starting_point():
    """A fresh install should not hunt: single RPM of immediate step for a 10%
    error, leaving the operator to turn it up until it tracks briskly enough."""
    from dse_engine_controller.regulator import DEFAULT_SENSITIVITY

    r = _regulator()
    r.reset(1500)
    first = r.update(900.0, 1000.0, DEFAULT_SENSITIVITY, 2.0) - 1500
    assert 0 < first < 3
