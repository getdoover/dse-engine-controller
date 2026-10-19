"""Tests for the pump-control rework: RPM regulator, remote-sensor config
slots, and the dynamically-built Regulate UI."""

import asyncio
import types

from dse_engine_controller.regulator import RpmRegulator


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# RpmRegulator (pure PI loop)
# ---------------------------------------------------------------------------


def test_regulator_holds_without_inputs():
    r = RpmRegulator(900, 2000)
    r.reset(1500)
    # Missing reading / setpoint / non-positive dt → output unchanged.
    assert r.update(None, 1400, 5, 1.0) == 1500
    assert r.update(1200, None, 5, 1.0) == 1500
    assert r.update(1200, 1400, 5, 0.0) == 1500


def test_regulator_reset_seeds_output():
    r = RpmRegulator(900, 2000)
    r.reset(1234)
    assert r.output == 1234
    # No current rpm → seeds to the midpoint of the band.
    r.reset(None)
    assert r.output == 1450


def test_regulator_direction_direct_acting():
    # measured below setpoint → speed up; above → slow down.
    up = RpmRegulator(900, 2000)
    up.reset(1500)
    assert up.update(1000, 1400, 3, 1.0) > 1500

    down = RpmRegulator(900, 2000)
    down.reset(1500)
    assert down.update(1800, 1400, 3, 1.0) < 1500


def test_regulator_invert_flips_direction():
    # With invert, measured below setpoint → slow down; above → speed up.
    inv_below = RpmRegulator(900, 2000)
    inv_below.reset(1500)
    assert inv_below.update(1000, 1400, 3, 1.0, invert=True) < 1500

    inv_above = RpmRegulator(900, 2000)
    inv_above.reset(1500)
    assert inv_above.update(1800, 1400, 3, 1.0, invert=True) > 1500


def test_regulator_clamps_to_bounds():
    r = RpmRegulator(900, 2000)
    r.reset(1500)
    # Huge persistent error can't drive the output past the rail.
    out = 1500
    for _ in range(50):
        out = r.update(measured=0, setpoint=5000, sensitivity=10, dt=5.0)
    assert 900 <= out <= 2000
    assert out == 2000


def test_regulator_converges_to_setpoint():
    # Plant: measured pressure rises linearly with RPM (direct-acting).
    def plant(rpm):
        return 0.8 * rpm

    r = RpmRegulator(900, 2000)
    r.reset(1200)
    setpoint = 1200  # achievable at rpm=1500
    measured = plant(1200)
    out = 1200
    # Knob 7 — a tenth of the top of the scale. The loop is deliberately unhurried
    # at these settings: it walks in, it doesn't lunge.
    for _ in range(1000):
        out = r.update(measured, setpoint, sensitivity=7.0, dt=1.0)
        measured = plant(out)
    assert abs(measured - setpoint) < 5


def test_regulator_anti_windup_recovers():
    # Saturate high against an unreachable setpoint, then drop the setpoint and
    # confirm the output comes back down promptly (integral didn't wind up).
    r = RpmRegulator(900, 2000)
    r.reset(1500)
    for _ in range(50):
        r.update(measured=0, setpoint=5000, sensitivity=10, dt=5.0)
    assert r.output == 2000
    # Now the variable is well above a low setpoint → should fall toward floor.
    out = r.output
    for _ in range(20):
        out = r.update(measured=2000, setpoint=100, sensitivity=10, dt=5.0)
    assert out < 1500


# ---------------------------------------------------------------------------
# Remote-sensor config slots
# ---------------------------------------------------------------------------


def test_remote_sensor_slots_present_and_wired_to_tags():
    from dse_engine_controller.app_config import (
        MAX_REMOTE_SENSORS,
        DseEngineControllerConfig,
    )
    from dse_engine_controller.app_tags import DseEngineControllerTags

    for i in range(MAX_REMOTE_SENSORS):
        slot = getattr(DseEngineControllerConfig, f"sensor_ref_{i}")
        # reference_name is fixed + hidden so the operator can't break the link.
        assert slot.reference_name.default == f"remote_sensor_{i}"
        assert slot.reference_name.hidden is True
        # The matching RemoteTag uses the same reference_name.
        tag = getattr(DseEngineControllerTags, f"remote_sensor_{i}")
        assert tag.reference_name == f"remote_sensor_{i}"
        assert tag.optional is True


def test_remote_sensor_slot_has_invert_option():
    from dse_engine_controller.app_config import DseEngineControllerConfig

    slot = DseEngineControllerConfig.sensor_ref_0
    assert slot.invert.default is False


# ---------------------------------------------------------------------------
# Dynamic Regulate / Sensors UI
# ---------------------------------------------------------------------------


class _FakeElem:
    _UNSET = object()

    def __init__(self, value=_UNSET):
        self._v = value

    @property
    def value(self):
        if self._v is self._UNSET:
            raise ValueError("unset")
        return self._v


class _FakeSlot:
    def __init__(
        self,
        app=None,
        tag=None,
        label="",
        units="",
        invert=False,
        setpoint_min=0.0,
        setpoint_max=1000.0,
        allow_rpm_control=True,
    ):
        self.app_name = _FakeElem(app)
        self.tag_name = _FakeElem(tag)
        self.sensor_label = _FakeElem(label)
        self.sensor_units = _FakeElem(units)
        self.invert = _FakeElem(invert)
        self.setpoint_min = _FakeElem(setpoint_min)
        self.setpoint_max = _FakeElem(setpoint_max)
        self.allow_rpm_control = _FakeElem(allow_rpm_control)


class _FakeRange:
    """One configured display band, as config.Array yields them."""

    def __init__(self, min_val=None, max_val=None, colour="green", label=""):
        self.min_val = _FakeElem(min_val)
        self.max_val = _FakeElem(max_val)
        self.colour = _FakeElem(colour)
        self.label = _FakeElem(label)


class _FakeAnalogSlot:
    """A native analog input slot, as app_config.AnalogInputSlot presents one."""

    def __init__(
        self,
        pin=None,
        label="",
        units="",
        signal_type="4-20 mA",
        signal_min=4.0,
        signal_max=20.0,
        value_min=0.0,
        value_max=100.0,
        invert=False,
        setpoint_min=0.0,
        setpoint_max=100.0,
        filter_seconds=4.0,
        fault_below=3.5,
        display_ranges=(),
        allow_rpm_control=True,
    ):
        self.ai_pin = _FakeElem(pin)
        self.sensor_label = _FakeElem(label)
        self.sensor_units = _FakeElem(units)
        self.signal_type = _FakeElem(signal_type)
        self.signal_min = _FakeElem(signal_min)
        self.signal_max = _FakeElem(signal_max)
        self.value_min = _FakeElem(value_min)
        self.value_max = _FakeElem(value_max)
        self.invert = _FakeElem(invert)
        self.setpoint_min = _FakeElem(setpoint_min)
        self.setpoint_max = _FakeElem(setpoint_max)
        self.filter_seconds = _FakeElem(filter_seconds)
        self.fault_below = _FakeElem(fault_below)
        self.display_ranges = _FakeElem(list(display_ranges))
        self.allow_rpm_control = _FakeElem(allow_rpm_control)


class _FakeModeSlot:
    """A run mode row, as app_config.RunModeSlot presents one."""

    def __init__(self, label="", control="Fixed", **kw):
        self.label = _FakeElem(label)
        self.control = _FakeElem(control)
        for field in ("target_rpm", "variable", "setpoint", "sensitivity"):
            setattr(self, field, _FakeElem(kw.get(field)))


class _FakeConfig:
    """Exposes only the wired sensor slots; others resolve to None."""

    def __init__(
        self, slots, sensors_enabled=True, analog=(), run_modes=(),
        include_default=True,
    ):
        # `sensors_enabled` gates the remote tag slots only; analog inputs are a
        # list and always active.
        self.enable_remote_tags = _FakeElem(sensors_enabled)
        for i, slot in enumerate(slots):
            setattr(self, f"sensor_ref_{i}", slot)
        self.analog_inputs = _FakeElem(list(analog))
        # Run modes are a list like the analog inputs; empty means the app
        # synthesises the single Fixed mode (see run_modes.build_modes).
        self.run_modes = _FakeElem(list(run_modes))
        self.run_mode_transitions = _FakeElem([])
        self.include_default_fixed_run_mode = _FakeElem(include_default)
        # Global sensitivity bounds the dynamic sensitivity slider reads.
        self.regulate_min_sensitivity = _FakeElem(0.1)
        self.regulate_max_sensitivity = _FakeElem(10.0)
        # Bounds for the Target RPM slider.
        self.target_rpm_min = _FakeElem(900)
        self.target_rpm_max = _FakeElem(2000)


def _built_ui(config, tags=None):
    """A UI built the way the framework does it: tags created from config first.

    The per-sensor tags (analog values, visibility flags) only exist after
    ``Tags.setup()`` has read the same config, so the UI has to be handed the
    live instance rather than the class.
    """
    from dse_engine_controller.app_tags import DseEngineControllerTags
    from dse_engine_controller.app_ui import DseEngineControllerUI

    if tags is None:
        tags = DseEngineControllerTags("kensho_test", None, config)
        _run(tags.setup())
    ui = DseEngineControllerUI(config, tags, "kensho_test")
    _run(ui.setup())
    return ui


def _tags_for(config):
    """A Tags instance with its config-driven sensor tags created."""
    from dse_engine_controller.app_tags import DseEngineControllerTags

    tags = DseEngineControllerTags("kensho_test", None, config)
    _run(tags.setup())
    return tags


def test_setup_builds_sensor_displays_and_picker():
    config = _FakeConfig(
        [
            _FakeSlot(app="app-a-1", tag="inlet", label="Inlet Pressure", units="kPa"),
            _FakeSlot(app="app-b-2", tag="level", label="Dam Level", units="%"),
        ]
    )
    ui = _built_ui(config)

    # Sensor displays are now top-level elements (below Engine Hours), not in a
    # submodule. One per configured sensor.
    sensor_names = [n for n in ui._elements if n.startswith("sensor_")]
    assert set(sensor_names) == {"sensor_inlet_pressure", "sensor_dam_level"}

    # The Start / Stop trigger pickers have one option per configured sensor,
    # keyed by the sanitised label the handler matches against. (There is no
    # Regulate picker any more — a Regulate mode names its sensor in config.)
    for picker_name in ("start_variable", "stop_variable"):
        picker = ui._find_element(picker_name)
        assert set(picker.options.keys() if isinstance(picker.options, dict)
                   else {o.name for o in picker.options}) == {
            "inlet_pressure",
            "dam_level",
        }


def test_setup_skips_all_slots_when_sensors_disabled():
    config = _FakeConfig(
        [_FakeSlot(app="app-a-1", tag="inlet", label="Inlet Pressure")],
        sensors_enabled=False,
    )
    ui = _built_ui(config)

    assert [n for n in ui._elements if n.startswith("sensor_")] == []


def test_setup_skips_unwired_slots():
    # Only one slot has both app + tag; a label-only slot is ignored.
    config = _FakeConfig(
        [
            _FakeSlot(app="app-a-1", tag="inlet", label="Inlet Pressure"),
            _FakeSlot(label="Half-configured"),
        ]
    )
    ui = _built_ui(config)

    assert len([n for n in ui._elements if n.startswith("sensor_")]) == 1


def test_regulate_mode_builds_one_setpoint_slider_from_its_own_sensor():
    """The variable is fixed per mode in config, which is what lets this be one
    setpoint slider instead of one per sensor. Its bounds and units come from
    that sensor, so it reads in the operator's engineering units."""
    from pydoover.ui.misc import NotSet

    config = _FakeConfig(
        [
            _FakeSlot(
                app="app-a-1",
                tag="inlet",
                label="Inlet Pressure",
                units="kPa",
                setpoint_min=100.0,
                setpoint_max=900.0,
            )
        ],
        run_modes=[_FakeModeSlot("Run", "Regulate", variable="Inlet Pressure")],
    )
    ui = _built_ui(config)

    setpoint = ui._find_element("mode_run_setpoint")
    assert (setpoint.min_val, setpoint.max_val) == (100.0, 900.0)
    assert setpoint.units == "kPa"
    assert setpoint.requires_confirm is not NotSet  # setpoint keeps its confirm

    sensitivity = ui._find_element("mode_run_sensitivity")
    assert (sensitivity.min_val, sensitivity.max_val) == (0.1, 10.0)
    # Sensitivity slider has no confirm dialog (adjusted live).
    assert sensitivity.requires_confirm is NotSet

    # A Regulate mode has no Target RPM — the loop decides the speed.
    for absent in ("mode_run_target_rpm", "regulate_variable"):
        try:
            ui._find_element(absent)
        except KeyError:
            continue
        raise AssertionError(f"{absent} should not exist")


def test_start_stop_setpoints_use_per_sensor_sliders():
    config = _FakeConfig(
        [
            _FakeSlot(app="a-1", tag="inlet", label="Inlet Pressure", units="kPa",
                      setpoint_min=100.0, setpoint_max=900.0),
        ]
    )
    ui = _built_ui(config)

    # Start and Stop sensor triggers get the same per-sensor setpoint sliders as
    # Regulate (bounds + units from config), not a plain numeric input.
    for name in ("start_setpoint_inlet_pressure", "stop_setpoint_inlet_pressure"):
        sl = ui._find_element(name)
        assert (sl.min_val, sl.max_val) == (100.0, 900.0)
        assert sl.units == "kPa"


# ---------------------------------------------------------------------------
# Unified sensor model (remote tags + native analog inputs)
# ---------------------------------------------------------------------------


def test_build_sensors_merges_analog_inputs_and_remote_slots():
    from dse_engine_controller.sensors import build_sensors

    config = _FakeConfig(
        [_FakeSlot(app="a-1", tag="level", label="Dam Level", units="%")],
        analog=[_FakeAnalogSlot(pin=2, label="Inlet Pressure", units="kPa",
                                value_max=1600.0, setpoint_max=1500.0)],
    )
    tags = _tags_for(config)
    sensors = build_sensors(config, tags)

    # Analog inputs first (the list), then the fixed remote slots.
    assert [s.label for s in sensors] == ["Inlet Pressure", "Dam Level"]
    assert [s.kind for s in sensors] == ["analog", "remote"]

    analog, remote = sensors
    assert analog.pin == 2
    assert (analog.signal_min, analog.signal_max) == (4.0, 20.0)
    assert analog.signal_units == "mA"
    assert analog.value_max == 1600.0
    assert analog.sp_max == 1500.0
    # Tags are keyed on the pin, so reordering the list can't move history.
    assert analog.value_tag_name == "analog_value_pin2"
    assert analog.raw_tag_name == "analog_raw_pin2"
    assert analog.fault_tag_name == "analog_fault_pin2"
    assert analog.tag.name == "analog_value_pin2"
    # Remote slots keep their declared RemoteTag.
    assert remote.value_tag_name == "remote_sensor_0"


def test_build_sensors_skips_analog_slots_without_a_pin():
    from dse_engine_controller.app_tags import DseEngineControllerTags
    from dse_engine_controller.sensors import build_sensors

    config = _FakeConfig([], analog=[_FakeAnalogSlot(label="Named but unwired")])
    assert build_sensors(config, DseEngineControllerTags) == []


def test_analog_inputs_do_not_need_the_remote_tags_switch():
    from dse_engine_controller.sensors import build_sensors

    # Analog inputs are a list, so they need no enabling switch — only the remote
    # tag slots (a whole form each) sit behind one.
    config = _FakeConfig(
        [_FakeSlot(app="a-1", tag="level", label="Dam Level")],
        sensors_enabled=False,
        analog=[_FakeAnalogSlot(pin=2, label="Inlet")],
    )
    sensors = build_sensors(config, _tags_for(config))

    assert [(s.kind, s.label) for s in sensors] == [("analog", "Inlet")]


def test_build_sensors_warns_about_remote_slots_the_switch_ignores(caplog):
    import logging

    from dse_engine_controller.sensors import build_sensors

    config = _FakeConfig(
        [_FakeSlot(app="a-1", tag="level", label="Dam Level")],
        sensors_enabled=False,
    )
    with caplog.at_level(logging.WARNING, logger="dse_engine_controller.sensors"):
        assert build_sensors(config, _tags_for(config)) == []
    assert "Enable Remote Tags" in caplog.text
    assert "1 remote tag slot(s)" in caplog.text


def test_build_sensors_stays_quiet_with_nothing_configured(caplog):
    import logging

    from dse_engine_controller.sensors import build_sensors

    config = _FakeConfig([], sensors_enabled=False)
    with caplog.at_level(logging.WARNING, logger="dse_engine_controller.sensors"):
        assert build_sensors(config, _tags_for(config)) == []
    assert caplog.text == ""


def test_build_sensors_dedupes_colliding_labels():
    from dse_engine_controller.app_tags import DseEngineControllerTags
    from dse_engine_controller.sensors import build_sensors

    # Same label on both sources — the pickers key on the sanitised name, so it
    # has to stay unique or a selection would be ambiguous.
    config = _FakeConfig(
        [_FakeSlot(app="a-1", tag="p", label="Pressure")],
        analog=[_FakeAnalogSlot(pin=3, label="Pressure")],
    )
    sensors = build_sensors(config, DseEngineControllerTags)
    assert [s.sanitised for s in sensors] == ["pressure", "pressure_2"]


def test_build_sensors_defaults_unlabelled_slots():
    from dse_engine_controller.app_tags import DseEngineControllerTags
    from dse_engine_controller.sensors import build_sensors

    config = _FakeConfig([], analog=[_FakeAnalogSlot(pin=4)])
    assert build_sensors(config, DseEngineControllerTags)[0].label == "Analog Input 1"


def test_analog_signal_type_presets():
    from dse_engine_controller.app_tags import DseEngineControllerTags
    from dse_engine_controller.sensors import build_sensors

    config = _FakeConfig(
        [],
        analog=[
            _FakeAnalogSlot(pin=2, label="Loop", signal_type="4-20 mA"),
            _FakeAnalogSlot(pin=0, label="Volts", signal_type="0-10 V"),
            _FakeAnalogSlot(pin=1, label="Odd", signal_type="Custom",
                            signal_min=0.0, signal_max=5.0),
        ],
    )
    loop, volts, odd = build_sensors(config, DseEngineControllerTags)

    # 4-20 mA gets open-circuit detection; 0 V is a legitimate voltage reading,
    # so voltage inputs get none, and Custom can't know what "broken" looks like.
    assert (loop.signal_min, loop.signal_max, loop.fault_below) == (4.0, 20.0, 3.5)
    assert (volts.signal_min, volts.signal_max, volts.fault_below) == (0.0, 10.0, None)
    assert (odd.signal_min, odd.signal_max, odd.fault_below) == (0.0, 5.0, None)
    assert (loop.signal_units, volts.signal_units) == ("mA", "V")


def test_ui_builds_analog_gauges_pickers_and_sliders():
    config = _FakeConfig(
        [_FakeSlot(app="a-1", tag="level", label="Dam Level", units="%")],
        analog=[_FakeAnalogSlot(pin=2, label="Inlet Pressure", units="kPa",
                                setpoint_min=100.0, setpoint_max=1500.0)],
    )
    ui = _built_ui(config)

    # Native inputs get the same top-level gauge as remote sensors...
    assert {n for n in ui._elements if n.startswith("sensor_")} == {
        "sensor_dam_level",
        "sensor_inlet_pressure",
    }
    # ...an option in every trigger picker...
    for picker in ("start_variable", "stop_variable"):
        options = ui._find_element(picker).options
        names = options.keys() if isinstance(options, dict) else {o.name for o in options}
        assert set(names) == {"dam_level", "inlet_pressure"}
    # ...and a setpoint slider named for them, with their own bounds.
    for prefix in ("start_setpoint", "stop_setpoint"):
        slider = ui._find_element(f"{prefix}_inlet_pressure")
        assert (slider.min_val, slider.max_val) == (100.0, 1500.0)
        assert slider.units == "kPa"


def test_regulate_plot_lives_in_a_collapsed_submodule():
    """The plot is history, not a control, and it's the tallest thing in the tab —
    so it shouldn't push the controls off the screen."""
    ui = _built_ui(_FakeConfig([], analog=[_FakeAnalogSlot(pin=2, label="Inlet")]))

    group = ui._find_element("regulate_plot_group")
    emitted = group.to_dict()
    assert emitted["type"] == "uiSubmodule"
    assert emitted["defaultOpen"] is False  # collapsed until asked for
    assert "regulate_plot" in emitted["children"]
    assert len(emitted["children"]["regulate_plot"]["series"]) == 4


def test_trigger_pickers_still_offer_every_sensor():
    """Driving engine speed from a reading is opt-in per sensor, and is now
    enforced in config validation rather than by filtering a picker. The Start /
    Stop triggers were never restricted and still aren't."""
    config = _FakeConfig(
        [_FakeSlot(app="a-1", tag="level", label="Dam Level", allow_rpm_control=False)],
        analog=[
            _FakeAnalogSlot(pin=2, label="Inlet Pressure", allow_rpm_control=True),
            _FakeAnalogSlot(pin=3, label="Tank Level", allow_rpm_control=False),
        ],
    )
    ui = _built_ui(config)

    for picker in ("start_variable", "stop_variable"):
        options = ui._find_element(picker).options
        names = set(
            options.keys() if isinstance(options, dict) else {o.name for o in options}
        )
        assert names == {"dam_level", "inlet_pressure", "tank_level"}

    ui._find_element("start_setpoint_tank_level")
    ui._find_element("stop_setpoint_dam_level")


def test_regulate_refuses_a_sensor_not_allowed_to_control_rpm():
    """A selection can outlive the permission — a config change, or a config that
    predates the setting. The picker won't offer it; neither should the loop."""
    sensor = _slot("inlet", 500.0, sp_max=1600.0, allow_rpm_control=False)
    app = _regulate_app(sensor, None, setpoint=800.0)

    assert _run(app._regulate_target_rpm(app.mode)) is None
    status = app.tags.regulate_status.get()
    assert "not allowed to control RPM" in status
    assert app.tags.regulate_setpoint_active.get() is None
    assert app.tags.regulate_output_rpm.get() is None


def test_analog_gauge_spans_the_sensor_range_not_the_setpoint_bounds():
    config = _FakeConfig(
        # Remote slot keeps the old behaviour (setpoint bounds), so existing
        # dashboards don't move.
        [_FakeSlot(app="a-1", tag="level", label="Dam Level",
                   setpoint_min=0.0, setpoint_max=90.0)],
        analog=[
            # 0-1600 kPa sensor with the default 0-100 setpoint bounds: the gauge
            # has to follow the sensor, or it reads pegged full at 100.
            _FakeAnalogSlot(pin=2, label="Inlet Pressure", value_max=1600.0),
            # Inverse-scaled (ullage) sensor still renders low-to-high.
            _FakeAnalogSlot(pin=3, label="Ullage", value_min=1000.0, value_max=0.0),
        ],
    )
    ui = _built_ui(config)

    def gauge(name):
        return ui._find_element(name).ranges[0]

    level = gauge("sensor_dam_level")
    assert (level.min, level.max) == (0.0, 90.0)
    inlet = gauge("sensor_inlet_pressure")
    assert (inlet.min, inlet.max) == (0.0, 1600.0)
    ullage = gauge("sensor_ullage")
    assert (ullage.min, ullage.max) == (0.0, 1000.0)


def test_configured_display_ranges_become_gauge_bands():
    config = _FakeConfig(
        [],
        analog=[
            _FakeAnalogSlot(
                pin=2, label="Inlet Pressure", units="kPa", value_max=1600.0,
                display_ranges=[
                    # Deliberately out of order and back-to-front, as an operator
                    # might enter them.
                    _FakeRange(1400.0, 1600.0, "yellow", "High"),
                    _FakeRange(200.0, 0.0, "red", "Low"),
                    _FakeRange(200.0, 1400.0, "green", "Normal"),
                ],
            )
        ],
    )
    ui = _built_ui(config)

    bands = ui._find_element("sensor_inlet_pressure").ranges
    assert [(b.min, b.max, b.colour, b.label) for b in bands] == [
        (0.0, 200.0, "red", "Low"),
        (200.0, 1400.0, "green", "Normal"),
        (1400.0, 1600.0, "yellow", "High"),
    ]


def test_display_ranges_skip_unfilled_rows():
    from dse_engine_controller.app_tags import DseEngineControllerTags
    from dse_engine_controller.sensors import build_sensors

    # "Add range" clicked and left blank: inventing a band from the defaults would
    # silently re-scale the gauge, since configured bands define its extent.
    config = _FakeConfig(
        [],
        analog=[
            _FakeAnalogSlot(
                pin=2, label="Inlet", value_max=1600.0,
                display_ranges=[_FakeRange(), _FakeRange(0.0, None), _FakeRange(None, 500.0)],
            )
        ],
    )
    assert build_sensors(config, DseEngineControllerTags)[0].ranges == ()


def test_display_ranges_drop_zero_width_bands():
    from dse_engine_controller.app_tags import DseEngineControllerTags
    from dse_engine_controller.sensors import build_sensors

    config = _FakeConfig(
        [],
        analog=[
            _FakeAnalogSlot(
                pin=2, label="Inlet", value_max=1600.0,
                display_ranges=[
                    _FakeRange(500.0, 500.0, "red", "Nothing"),
                    _FakeRange(0.0, 1600.0, "green", "All"),
                ],
            )
        ],
    )
    sensor = build_sensors(config, DseEngineControllerTags)[0]
    assert [(b.min_val, b.max_val, b.label) for b in sensor.ranges] == [
        (0.0, 1600.0, "All")
    ]


def test_sensor_without_configured_ranges_keeps_the_plain_band():
    config = _FakeConfig(
        [], analog=[_FakeAnalogSlot(pin=2, label="Inlet", value_max=1600.0)]
    )
    ui = _built_ui(config)

    bands = ui._find_element("sensor_inlet").ranges
    assert len(bands) == 1
    assert (bands[0].min, bands[0].max, bands[0].colour) == (0.0, 1600.0, "green")


def test_auto_start_fault_latches_until_the_pump_runs_again():
    """Shown while faulted, kept after acknowledgement, cleared by a start.

    A fault that disappears the moment it's reset tells an operator arriving five
    minutes later nothing at all.
    """
    app = _readout_app(state="fault")
    _run(app._publish_autostart_tags(run_request=False, lost_comms=False))
    assert app.tags.auto_start_fault.get() == "Engine stopped unexpectedly"
    assert app.tags.auto_start_fault_hidden.get() is False

    # Fault cleared but nothing started yet: still shown.
    app.autostart.state = "idle"
    _run(app._publish_autostart_tags(run_request=False, lost_comms=False))
    assert app.tags.auto_start_fault.get() == "Engine stopped unexpectedly"
    assert app.tags.auto_start_fault_hidden.get() is False

    # Started: it stops being news.
    app.autostart.state = "prestart_warning"
    _run(app._publish_autostart_tags(run_request=True, lost_comms=False))
    assert app.tags.auto_start_fault.get() == ""
    assert app.tags.auto_start_fault_hidden.get() is True


def test_auto_start_fault_cleared_by_running_normally():
    app = _readout_app(state="fault")
    _run(app._publish_autostart_tags(run_request=True, lost_comms=False))
    assert app.tags.auto_start_fault_hidden.get() is False

    # Auto-recovery puts it back into running without any button press.
    app.autostart.state = "running"
    app.autostart.fault_reason = ""
    _run(app._publish_autostart_tags(run_request=True, lost_comms=False))
    assert app.tags.auto_start_fault.get() == ""
    assert app.tags.auto_start_fault_hidden.get() is True


def test_auto_start_fault_stays_hidden_when_nothing_has_faulted():
    app = _readout_app(state="idle", display="Ready")
    app.tags.auto_start_fault._v = ""
    _run(app._publish_autostart_tags(run_request=False, lost_comms=False))
    assert app.tags.auto_start_fault_hidden.get() is True


def test_auto_start_fault_is_a_warning_indicator_not_a_text_line():
    """The app's own control state — including why it faulted — has to be visible.

    Station 1 (2026-08-18) sat in "Fault: Engine stopped unexpectedly" for half an
    hour while the panel ran happily in Manual. The reason was published the whole
    time; nothing displayed it.
    """
    ui = _built_ui(_FakeConfig([]))

    # No always-on state text — only a warning when there's something wrong.
    assert "auto_start_state" not in ui._elements
    warning = ui._find_element("auto_start_fault")
    emitted = warning.to_dict()
    assert emitted["type"] == "uiWarningIndicator"
    # The message *is* the fault reason, and it hides itself when there isn't one.
    assert emitted["displayString"].startswith("$tag.app().auto_start_fault:")
    assert emitted["hidden"].startswith("$tag.app().auto_start_fault_hidden")
    # Nothing here handles a dismiss command, so don't offer one.
    assert emitted["can_cancel"] is False

    reason = ui._find_element("run_reason")
    assert reason.to_dict()["currentValue"].startswith("$tag.app().run_request_reason")
    assert reason.to_dict()["hidden"].startswith("$tag.app().run_reason_hidden")


def test_no_sensor_fault_element_on_the_dashboard():
    from dse_engine_controller.app_tags import DseEngineControllerTags

    config = _FakeConfig(
        [], analog=[_FakeAnalogSlot(pin=2, label="Inlet Pressure")]
    )
    ui = _built_ui(config)

    # Health is published as tags for other apps, not surfaced on the dashboard.
    assert "analog_fault" not in ui._elements
    assert not hasattr(DseEngineControllerTags, "sensor_fault_hidden")


def test_rpm_gauge_scales_to_the_configured_maximum():
    """Full scale is Target RPM Maximum + the margin, with the margin as the yellow
    band — not a fixed 4000-RPM sweep the needle never leaves the bottom half of."""
    config = _FakeConfig([])
    config.target_rpm_max = _FakeElem(2000)
    bands = [
        (r.label, r.min, r.max, r.colour)
        for r in _built_ui(config)._find_element("rpm").ranges
    ]
    assert bands == [
        ("Idle", 0, 800.0, "blue"),
        ("Run", 800.0, 2000.0, "green"),
        ("High", 2000.0, 2100.0, "yellow"),
    ]


def test_rpm_gauge_bands_stay_ordered_for_a_low_maximum():
    # A maximum below the engine's nominal idle would otherwise invert the bands.
    config = _FakeConfig([])
    config.target_rpm_max = _FakeElem(700)
    bands = [
        (r.min, r.max) for r in _built_ui(config)._find_element("rpm").ranges
    ]
    assert bands == [(0, 600.0), (600.0, 700.0), (700.0, 800.0)]
    assert all(lo < hi for lo, hi in bands)


def test_commands_read_from_the_cmds_aggregate_not_tags():
    """Every interaction displays from ui_cmds, never from a tag.

    A tag-backed ``currentValue`` puts the widget's displayed value on a
    different channel from the command that set it, which the site's command
    transaction flow can't reconcile. The app keeps the aggregate honest instead
    by mirroring each value it settles on (see ``_set_command_value``).
    """
    from pydoover.ui import Interaction

    ui = _built_ui(
        _FakeConfig(
            [],
            analog=[
                _FakeAnalogSlot(pin=2, label="Discharge Pressure", units="bar"),
                _FakeAnalogSlot(pin=3, label="Dam Level", units="mm"),
            ],
        )
    )
    tag_backed = [
        (name, elem.to_dict()["currentValue"])
        for name, elem in ui.get_interactions().items()
        if str(elem.to_dict().get("currentValue", "")).startswith("$tag")
    ]
    assert tag_backed == []
    # And each one points at its own name in the aggregate.
    for name, elem in ui.get_interactions().items():
        assert isinstance(elem, Interaction)
        assert elem.to_dict()["currentValue"].startswith(f"$cmds.app().{name}"), name


def test_fixed_mode_target_rpm_slider_bounds_come_from_config():
    # Nothing configured, so the app synthesises its single Fixed mode.
    ui = _built_ui(_FakeConfig([]))
    slider = ui._find_element("mode_fixed_rpm_target_rpm")
    assert (
        slider.to_dict()["currentValue"] == "$cmds.app().mode_fixed_rpm_target_rpm"
    )
    # The always-available mode is already named for its RPM, so its slider says
    # "Fixed RPM" rather than stuttering into "Fixed RPM RPM".
    assert slider.display_name == "Fixed RPM"
    # Bounds come from config, so the slider can't offer an RPM that gets clamped.
    assert (slider.min_val, slider.max_val) == (900, 2000)


def test_each_mode_gets_its_own_panel_and_controls():
    """One panel per configured mode, each owning its own numbers — so Warm Up's
    800 rpm and Line Fill's 1300 are separate settings, not one shared slider."""
    ui = _built_ui(
        _FakeConfig(
            [],
            run_modes=[
                _FakeModeSlot("Warm Up", "Fixed", target_rpm=800),
                _FakeModeSlot("Line Fill", "Fixed", target_rpm=1300),
            ],
        )
    )
    for name in (
        "mode_warm_up_panel", "mode_warm_up_target_rpm",
        "mode_line_fill_panel", "mode_line_fill_target_rpm",
    ):
        ui._find_element(name)
    picker = ui._find_element("run_mode")
    options = picker.options
    assert set(
        options.keys() if isinstance(options, dict) else {o.name for o in options}
    ) == {"warm_up", "line_fill", "fixed_rpm"}  # plus the always-available one


def test_ui_adds_raw_signal_diagnostics_for_analog_inputs_only():
    config = _FakeConfig(
        [_FakeSlot(app="a-1", tag="level", label="Dam Level")],
        analog=[_FakeAnalogSlot(pin=2, label="Inlet Pressure", units="kPa")],
    )
    ui = _built_ui(config)

    raw = ui._find_element("analog_raw_inlet_pressure")
    assert raw.units == "mA"  # raw signal, not the scaled engineering value
    assert "AI 2" in raw.display_name
    assert ui._find_element("analog_fault_inlet_pressure") is not None


# ---------------------------------------------------------------------------
# Loop failsafe
# ---------------------------------------------------------------------------


def _readout_app(state="fault", display="Fault — engine stopped unexpectedly"):
    """Just enough app to exercise _publish_autostart_tags' readout handling."""
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication

    app = SimpleNamespace()
    app.tags = SimpleNamespace(
        # The regulate readouts, left behind by the last step that ran.
        regulate_status=_TagStub("Holding 3 bar (now 2.971 bar)"),
        regulate_error=_TagStub(-0.03),
        regulate_output_rpm=_TagStub(1661),
        regulate_measured=_TagStub(2.971),
        regulate_setpoint_active=_TagStub(3.0),
        # Everything else _publish_autostart_tags writes.
        auto_start_state=_TagStub(""),
        auto_start_fault=_TagStub(""),
        auto_start_fault_hidden=_TagStub(True),
        auto_start_fault_at=_TagStub(None),
        auto_start_armed=_TagStub(False),
        prestart_remaining_s=_TagStub(None),
        cancel_btn_hidden=_TagStub(True),
        run_reason_hidden=_TagStub(True),
        start_now_disabled=_TagStub(True),
        stop_now_disabled=_TagStub(True),
        run_request_reason=_TagStub(""),
    )
    app.autostart = SimpleNamespace(
        state=state,
        display=display,
        warning_remaining=None,
        fault_reason="Engine stopped unexpectedly" if state == "fault" else "",
    )
    app._regulate_key = ("discharge_pressure", 3.0)
    for name in (
        "_publish_autostart_tags", "_clear_regulate_readouts",
        "_publish_autostart_fault", "_fault_gone_stale", "_clear_autostart_fault",
    ):
        setattr(
            app, name, types.MethodType(getattr(DseEngineControllerApplication, name), app)
        )
    return app


def test_regulate_readouts_are_dropped_when_not_regulating():
    """Station 1 (2026-08-18): the dashboard showed 'now 2.971 bar' beside a gauge
    reading 5.73 bar. Same tag underneath — the status line was the last thing a
    regulate step wrote before the app faulted 30 minutes earlier, and because the
    tag is live it was republished every poll and looked current.
    """
    app = _readout_app()
    _run(app._publish_autostart_tags(run_request=False, lost_comms=False))

    assert app.tags.regulate_status.get() == (
        "Not regulating — Fault — engine stopped unexpectedly"
    )
    assert app.tags.regulate_measured.get() is None
    assert app.tags.regulate_error.get() is None
    assert app.tags.regulate_output_rpm.get() is None
    assert app.tags.regulate_setpoint_active.get() is None
    assert app._regulate_key is None  # and the loop re-seeds when it resumes


def test_regulate_readouts_are_left_alone_while_running():
    app = _readout_app(state="running", display="Running")
    _run(app._publish_autostart_tags(run_request=True, lost_comms=False))

    # The loop owns these while it's running — don't fight it.
    assert app.tags.regulate_status.get() == "Holding 3 bar (now 2.971 bar)"
    assert app.tags.regulate_measured.get() == 2.971
    assert app._regulate_key == ("discharge_pressure", 3.0)


# ---------------------------------------------------------------------------
# Analog polling (the platform-interface plumbing)
# ---------------------------------------------------------------------------


def _analog_slot(label="Inlet Pressure", *, pin=2, **kw):
    slot = _slot(label, None, analog=True, pin=pin, **kw)
    # Writable tags — the poll publishes value / raw / fault.
    slot.tag = _LoggingTagStub(None)
    slot.raw_tag = _TagStub(None)
    slot.fault_tag = _TagStub(False)
    return slot


def _poll_app(slots, readings=None, *, fail=False):
    import types
    from types import SimpleNamespace

    from dse_engine_controller.analog import AnalogInput
    from dse_engine_controller.application import DseEngineControllerApplication

    app = SimpleNamespace()
    app._analog_slots = list(slots)
    app._analog = {s.sanitised: AnalogInput.from_slot(s) for s in app._analog_slots}
    app._last_analog_time = 0.0
    app._analog_config_problems = []
    app._analog_failed_pins = set()
    app._analog_logged = {}
    app._analog_logged_at = {}
    app._sensor_fault_stopped = False
    app.tags = SimpleNamespace(
        analog_fault_string=_TagStub(""),
        sensor_fault_active=_TagStub(False),
    )

    async def fetch_ai(*pins):
        if fail:
            raise RuntimeError("platform interface down")
        # Mirrors the real API: a scalar for one pin, a list for several.
        return readings[0] if len(pins) == 1 else list(readings)

    app.platform_iface = SimpleNamespace(fetch_ai=fetch_ai)
    for name in (
        "_poll_analog_inputs", "_read_analog_pins", "_fetch_ai", "_log_read_failures",
        "_should_log_analog", "_publish_analog_value",
    ):
        setattr(
            app, name, types.MethodType(getattr(DseEngineControllerApplication, name), app)
        )
    return app


def test_poll_publishes_scaled_and_raw_values():
    slots = [
        _analog_slot("Inlet Pressure", pin=2, value_max=1600.0),
        _analog_slot("Tank Level", pin=3, value_max=100.0),
    ]
    app = _poll_app(slots, readings=[12.0, 20.0])
    _run(app._poll_analog_inputs())

    assert slots[0].tag.get() == 800.0
    assert slots[0].raw_tag.get() == 12.0
    assert slots[1].tag.get() == 100.0
    assert all(s.fault_tag.get() is False for s in slots)
    assert app.tags.analog_fault_string.get() == ""
    assert app.tags.sensor_fault_active.get() is False


def test_poll_logs_the_first_reading_then_only_significant_moves():
    slot = _analog_slot(value_max=1600.0)  # span 1600 -> 0.5% deadband = 8 kPa
    app = _poll_app([slot], readings=[12.0])
    _run(app._poll_analog_inputs())
    assert slot.tag.logged == [800.0]  # first reading always anchors the plot

    # Noise inside the deadband is published live but not stored.
    app.platform_iface.fetch_ai = _returning(12.02)
    _run(app._poll_analog_inputs())
    assert slot.tag.logged == [800.0]
    assert slot.tag.get() == 802.0  # the tag itself still tracks every reading

    # A real move is stored.
    app.platform_iface.fetch_ai = _returning(13.0)
    _run(app._poll_analog_inputs())
    assert slot.tag.logged == [800.0, 900.0]


def test_analog_logging_deadband_scales_with_the_configured_span():
    from dse_engine_controller.application import DseEngineControllerApplication

    should_log = DseEngineControllerApplication._should_log_analog
    app = _poll_app([_analog_slot()], readings=[])

    # A 0-1 sensor and a 0-1600 sensor can't share an absolute threshold.
    small = _slot("small", None, analog=True, pin=2, value_min=0.0, value_max=1.0)
    big = _slot("big", None, analog=True, pin=3, value_min=0.0, value_max=1600.0)
    assert should_log(app, small, 0.5, 0.0) is True  # first reading
    assert should_log(app, small, 0.502, 1.0) is False  # inside 0.5% of 1.0
    assert should_log(app, small, 0.52, 2.0) is True
    assert should_log(app, big, 800.0, 0.0) is True
    assert should_log(app, big, 802.0, 1.0) is False  # inside 0.5% of 1600
    assert should_log(app, big, 830.0, 2.0) is True


def test_analog_logging_heartbeats_through_a_steady_reading():
    from dse_engine_controller.application import (
        ANALOG_LOG_HEARTBEAT_S,
        DseEngineControllerApplication,
    )

    should_log = DseEngineControllerApplication._should_log_analog
    app = _poll_app([_analog_slot()], readings=[])
    slot = _slot("steady", None, analog=True, pin=2, value_max=1600.0)

    assert should_log(app, slot, 800.0, 0.0) is True
    assert should_log(app, slot, 800.0, 60.0) is False
    # Plots need an anchor point even when nothing changes.
    assert should_log(app, slot, 800.0, ANALOG_LOG_HEARTBEAT_S + 1) is True


def test_poll_blanks_the_tag_once_a_reading_goes_stale():
    slot = _analog_slot(value_max=1600.0)
    app = _poll_app([slot], readings=[12.0])
    _run(app._poll_analog_inputs())
    assert slot.tag.get() == 800.0

    # Force the hold window shut, then poll a dead loop. The first faulted poll
    # starts the window; the next one is past it.
    app._analog[slot.sanitised].hold_seconds = 0.0
    app.platform_iface.fetch_ai = _raising()
    _run(app._poll_analog_inputs())
    assert slot.tag.get() == 800.0  # still held on the first bad poll
    _run(app._poll_analog_inputs())

    assert slot.tag.get() is None  # gauge blanks instead of flat-lining
    assert slot.fault_tag.get() is True
    # The None is logged once, so the plot breaks where the reading stopped
    # being trustworthy.
    assert slot.tag.logged == [800.0, None]

    _run(app._poll_analog_inputs())
    assert slot.tag.logged == [800.0, None]  # not re-logged every poll


def test_poll_logs_the_recovery_after_a_stale_gap():
    slot = _analog_slot(value_max=1600.0)
    app = _poll_app([slot], readings=[12.0])
    _run(app._poll_analog_inputs())
    app._analog[slot.sanitised].hold_seconds = 0.0
    app.platform_iface.fetch_ai = _raising()
    _run(app._poll_analog_inputs())
    _run(app._poll_analog_inputs())

    app.platform_iface.fetch_ai = _returning(12.0)
    _run(app._poll_analog_inputs())
    # Logged even though it matches the pre-gap value — it anchors the restart.
    assert slot.tag.logged == [800.0, None, 800.0]


def test_poll_handles_a_single_pin_scalar_response():
    slot = _analog_slot(value_max=1000.0)
    app = _poll_app([slot], readings=[4.0])
    _run(app._poll_analog_inputs())
    assert slot.tag.get() == 0.0


def test_poll_flags_an_open_circuit_and_holds_the_value():
    slot = _analog_slot(value_max=1600.0)
    app = _poll_app([slot], readings=[12.0])
    _run(app._poll_analog_inputs())

    app.platform_iface.fetch_ai = _returning(2.0)
    _run(app._poll_analog_inputs())

    assert slot.tag.get() == 800.0  # last good value held, not a scaled 2 mA
    assert slot.raw_tag.get() == 2.0
    assert slot.fault_tag.get() is True
    assert app.tags.analog_fault_string.get() == "Inlet Pressure: open circuit"
    assert app.tags.sensor_fault_active.get() is True


def test_poll_survives_a_platform_interface_failure():
    slot = _analog_slot(value_max=1600.0)
    app = _poll_app([slot], readings=[12.0])
    _run(app._poll_analog_inputs())

    app.platform_iface.fetch_ai = _raising()
    _run(app._poll_analog_inputs())

    assert slot.tag.get() == 800.0  # held
    assert slot.fault_tag.get() is True
    assert "no reading" in app.tags.analog_fault_string.get()


def test_poll_says_so_when_a_fault_stopped_the_engine():
    slot = _analog_slot()
    app = _poll_app([slot], readings=[2.0])
    app._sensor_fault_stopped = True
    _run(app._poll_analog_inputs())
    assert app.tags.analog_fault_string.get().endswith("engine stopped")

    # Fault clears → so does the "stopped" note.
    app.platform_iface.fetch_ai = _returning(12.0)
    _run(app._poll_analog_inputs())
    assert app.tags.analog_fault_string.get() == ""
    assert app._sensor_fault_stopped is False


def test_poll_keeps_startup_config_problems_visible():
    slot = _analog_slot()
    app = _poll_app([slot], readings=[12.0])
    app._analog_config_problems = ["Inlet Pressure: no analog input 9 on this device"]
    _run(app._poll_analog_inputs())
    # A healthy read must not scrub the config warning off the dashboard.
    assert "no analog input 9" in app.tags.analog_fault_string.get()


def test_poll_is_a_noop_without_analog_slots():
    app = _poll_app([], readings=[])
    _run(app._poll_analog_inputs())


def _returning(*values):
    async def fetch_ai(*pins):
        return values[0] if len(pins) == 1 else list(values)

    return fetch_ai


def _raising():
    async def fetch_ai(*pins):
        raise RuntimeError("platform interface down")

    return fetch_ai


def _reader_app(fetch_ai):
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication

    app = SimpleNamespace()
    app.platform_iface = SimpleNamespace(fetch_ai=fetch_ai)
    app._analog_failed_pins = set()
    for name in ("_read_analog_pins", "_fetch_ai", "_log_read_failures"):
        setattr(
            app, name, types.MethodType(getattr(DseEngineControllerApplication, name), app)
        )
    return app


def test_one_bad_channel_does_not_blind_the_others():
    # The platform interface errors a whole batched read if any single pin
    # raises, so a bad channel must be isolated by re-reading pins one by one.
    values = {2: 12.0, 3: 8.0}

    async def fetch_ai(*pins):
        if any(pin not in values for pin in pins):
            raise RuntimeError("AI 9 read failed")
        return values[pins[0]] if len(pins) == 1 else [values[p] for p in pins]

    app = _reader_app(fetch_ai)
    assert _run(app._read_analog_pins([2, 9, 3])) == [12.0, None, 8.0]
    assert app._analog_failed_pins == {9}


def test_short_analog_response_falls_back_to_per_pin_reads():
    async def fetch_ai(*pins):
        if len(pins) > 1:
            return [12.0]  # two pins asked for, one value back
        return 12.0

    app = _reader_app(fetch_ai)
    assert _run(app._read_analog_pins([2, 3])) == [12.0, 12.0]


def test_total_read_failure_yields_no_readings():
    async def fetch_ai(*pins):
        raise RuntimeError("platform interface down")

    app = _reader_app(fetch_ai)
    assert _run(app._read_analog_pins([2, 3])) == [None, None]
    assert app._analog_failed_pins == {2, 3}


def test_read_failures_are_logged_only_on_change(caplog):
    import logging

    state = {"fail": True}

    async def fetch_ai(*pins):
        if state["fail"]:
            raise RuntimeError("down")
        return 12.0 if len(pins) == 1 else [12.0] * len(pins)

    app = _reader_app(fetch_ai)
    with caplog.at_level(logging.WARNING, logger="dse_engine_controller.application"):
        _run(app._read_analog_pins([2]))
        _run(app._read_analog_pins([2]))  # same failure — must stay quiet
        assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1
        state["fail"] = False
        _run(app._read_analog_pins([2]))
        assert app._analog_failed_pins == set()


# ---------------------------------------------------------------------------
# Panel in Manual
# ---------------------------------------------------------------------------


def _mode_app(*, remote_control=True):
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication

    from dse_engine_controller.run_modes import (
        CONTROL_FIXED, CONTROL_REGULATE, ModeSequencer, RunMode,
    )

    fixed = RunMode("Fixed", "fixed", CONTROL_FIXED)
    regulate = RunMode("Run", "run", CONTROL_REGULATE, variable="level")
    app = SimpleNamespace()
    app._selected_mode = None
    app.config = SimpleNamespace(enable_remote_control=_FakeElem(remote_control))
    app.sequencer = ModeSequencer([fixed, regulate], [])
    app.tags = SimpleNamespace(
        manual_mode_hidden=_TagStub(True),
        rpm_status=_TagStub(""),
        run_mode_hidden=_TagStub(False),
        # One visibility flag per configured mode; only the active one shows.
        mode_fixed_hidden=_TagStub(False),
        mode_run_hidden=_TagStub(True),
        regulate_plot_hidden=_TagStub(True),
    )
    app.ui_manager = _UIManagerStub()
    app.ui_manager.values["run_mode"] = "fixed"
    app._manual_while_running = False
    for name in (
        "_publish_panel_mode", "_apply_rpm_mode_visibility", "_command_value",
        "_active_mode",
    ):
        setattr(
            app, name, types.MethodType(getattr(DseEngineControllerApplication, name), app)
        )
    return app


def test_manual_mode_is_flagged_with_a_reason():
    """Station 2 (2026-08-18): the panel ran at its own manual speed while the app
    held a setpoint it was ignoring, and nothing said why."""
    from types import SimpleNamespace

    app = _mode_app()
    _run(app._publish_panel_mode(SimpleNamespace(is_auto=False, is_running=True)))

    assert app.tags.manual_mode_hidden.get() is False
    assert app.tags.rpm_status.get() == "No RPM control in Manual mode"


def test_manual_mode_hides_the_rpm_controls():
    from types import SimpleNamespace

    app = _mode_app()
    _run(app._publish_panel_mode(SimpleNamespace(is_auto=False, is_running=True)))

    assert app.tags.run_mode_hidden.get() is True
    assert app.tags.mode_fixed_hidden.get() is True
    assert app.tags.mode_run_hidden.get() is True
    # ...but the explanation stays.
    assert app.tags.manual_mode_hidden.get() is False
    assert "Manual" in app.tags.rpm_status.get()


def test_auto_mode_restores_the_rpm_controls():
    from types import SimpleNamespace

    app = _mode_app()
    _run(app._publish_panel_mode(SimpleNamespace(is_auto=False, is_running=True)))
    _run(app._publish_panel_mode(SimpleNamespace(is_auto=True, is_running=True)))

    assert app.tags.run_mode_hidden.get() is False
    assert app.tags.mode_fixed_hidden.get() is False  # the active mode's panel
    assert app.tags.mode_run_hidden.get() is True


def test_manual_but_stopped_leaves_everything_alone():
    """Manual only matters while the engine is running: a stopped panel in Manual
    is no obstacle, because the start sequence puts it into Auto itself."""
    from types import SimpleNamespace

    app = _mode_app()
    _run(app._publish_panel_mode(SimpleNamespace(is_auto=False, is_running=False)))

    assert app.tags.manual_mode_hidden.get() is True  # no warning chip
    assert app.tags.rpm_status.get() == ""
    assert app.tags.run_mode_hidden.get() is False  # controls stay put
    assert app.tags.mode_fixed_hidden.get() is False


def test_auto_mode_says_nothing():
    from types import SimpleNamespace

    app = _mode_app()
    _run(app._publish_panel_mode(SimpleNamespace(is_auto=True, is_running=True)))

    assert app.tags.manual_mode_hidden.get() is True
    assert app.tags.rpm_status.get() == ""


def test_manual_mode_not_flagged_on_a_monitor_only_install():
    # No remote control configured — the panel being in Manual is just normal.
    from types import SimpleNamespace

    app = _mode_app(remote_control=False)
    _run(app._publish_panel_mode(SimpleNamespace(is_auto=False, is_running=True)))

    assert app.tags.manual_mode_hidden.get() is True
    assert app.tags.rpm_status.get() == ""


def test_manual_mode_not_claimed_without_a_snapshot():
    app = _mode_app()
    _run(app._publish_panel_mode(None))
    assert app.tags.manual_mode_hidden.get() is True


def test_manual_warning_and_rpm_status_are_on_the_dashboard():
    ui = _built_ui(_FakeConfig([]))

    warning = ui._find_element("manual_mode_warning").to_dict()
    assert warning["type"] == "uiWarningIndicator"
    assert "Manual" in warning["displayString"]
    assert warning["hidden"].startswith("$tag.app().manual_mode_hidden")
    assert warning["can_cancel"] is False

    # And the same condition explains the RPM controls, inside the RPM tab.
    status = ui._find_element("rpm_status").to_dict()
    assert status["currentValue"].startswith("$tag.app().rpm_status")
    assert status["hidden"].startswith("$tag.app().manual_mode_hidden")


# ---------------------------------------------------------------------------
# Target RPM assertion
# ---------------------------------------------------------------------------


def _rpm_app(*, last_written=None, echo=None, last_assert=0.0):
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication

    app = SimpleNamespace()
    app._last_rpm_written = last_written
    app._last_rpm_assert = last_assert
    app.store = SimpleNamespace(
        latest=SimpleNamespace(remote_rpm_setpoint=echo) if echo is not None else None
    )
    if echo is None:
        # A panel that reports nothing at all for 40048.
        app.store = SimpleNamespace(latest=SimpleNamespace(remote_rpm_setpoint=None))
    app._rpm_write_needed = types.MethodType(
        DseEngineControllerApplication._rpm_write_needed, app
    )
    return app


def test_rpm_written_when_the_target_changes():
    assert _rpm_app(last_written=1400, echo=1400)._rpm_write_needed(1500) is True


def test_rpm_not_rewritten_when_the_panel_agrees():
    assert _rpm_app(last_written=1500, echo=1500)._rpm_write_needed(1500) is False
    # Within tolerance of the commanded value.
    assert _rpm_app(last_written=1500, echo=1501)._rpm_write_needed(1500) is False


def test_rpm_reasserted_when_the_panel_has_lost_it():
    """Station 2 (2026-08-18): the app wrote 1961 once, the panel restarted and
    cleared 40048 to 0, and the cache-only check meant it never wrote again — the
    engine ran at its own manual speed while the app believed it held 1961.
    """
    import time

    app = _rpm_app(last_written=1961, echo=0, last_assert=time.monotonic() - 30)
    assert app._rpm_write_needed(1961) is True


def test_rpm_reassert_is_rate_limited():
    import time

    from dse_engine_controller.application import RPM_REASSERT_INTERVAL_S

    # A panel in Manual ignores the setpoint; don't write to it every poll.
    app = _rpm_app(last_written=1961, echo=0, last_assert=time.monotonic())
    assert app._rpm_write_needed(1961) is False

    app._last_rpm_assert = time.monotonic() - (RPM_REASSERT_INTERVAL_S + 1)
    assert app._rpm_write_needed(1961) is True


def test_rpm_not_reasserted_when_the_panel_reports_nothing():
    # Some panels/models don't populate 40048 — trust the cache rather than
    # writing on every single poll forever.
    app = _rpm_app(last_written=1500, echo=None)
    assert app._rpm_write_needed(1500) is False


# ---------------------------------------------------------------------------
# RPM Regulate on a native analog sensor
# ---------------------------------------------------------------------------


def _regulate_app(sensor, reading=None, *, setpoint=800.0, rpm=1500.0):
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication
    from dse_engine_controller.regulator import RpmRegulator

    app = SimpleNamespace()
    # Only the loop's readouts are tags; the variable, setpoint and sensitivity
    # are operator settings and come from the ui_cmds aggregate.
    app.tags = SimpleNamespace(
        regulate_setpoint_active=_TagStub(None),
        regulate_status=_TagStub(""),
        regulate_error=_TagStub(None),
        regulate_output_rpm=_TagStub(None),
        regulate_measured=_TagStub(None),
        engine_rpm=_TagStub(rpm),
    )
    from dse_engine_controller.run_modes import CONTROL_REGULATE, RunMode

    # A Regulate mode names its sensor in config; the setpoint and sensitivity
    # are that mode's own controls in the ui_cmds aggregate.
    app.mode = RunMode(
        "Run", "run", CONTROL_REGULATE,
        variable=sensor.sanitised, variable_label=sensor.label,
    )
    app.ui_manager = _UIManagerStub()
    app.ui_manager.values.update({
        app.mode.element_name("setpoint"): setpoint,
        app.mode.element_name("sensitivity"): 5.0,
    })
    app._sensor_by_name = {sensor.sanitised: sensor}
    app._analog = {sensor.sanitised: reading} if reading is not None else {}
    app.regulator = RpmRegulator(rpm_min=900, rpm_max=2000)
    app._regulate_key = None
    app._last_regulate_time = 0.0
    app.loop_target_period = 2.0
    app._regulate_logged = None
    app._regulate_logged_at = 0.0
    for name in ("_regulate_target_rpm", "_command_value", "_selected_setpoint",
                 "_should_log_regulate"):
        setattr(
            app, name,
            types.MethodType(getattr(DseEngineControllerApplication, name), app),
        )
    return app


def test_regulate_runs_on_a_native_analog_sensor():
    from dse_engine_controller.analog import AnalogInput

    sensor = _slot("inlet", 500.0, analog=True, pin=2, units="kPa",
                   sp_max=1600.0, value_max=1600.0)
    reading = AnalogInput.from_slot(sensor)
    reading.update(12.0, 1.0)  # healthy

    app = _regulate_app(sensor, reading)
    target = _run(app._regulate_target_rpm(app.mode))

    assert isinstance(target, int)
    assert 900 <= target <= 2000
    assert app.tags.regulate_measured.get() == 500.0
    assert "Holding 800" in app.tags.regulate_status.get()


def test_regulate_does_not_overwrite_the_operators_setpoint():
    """The loop must not write back to the slider's own command value.

    It reads the setpoint at the top of an iteration; an edit landing mid-loop
    would be stomped by that stale copy and the value would visibly flip back for
    a poll period.
    """
    from dse_engine_controller.analog import AnalogInput

    sensor = _slot("inlet", 500.0, analog=True, pin=2, units="kPa",
                   sp_max=1600.0, value_max=1600.0)
    reading = AnalogInput.from_slot(sensor)
    reading.update(12.0, 1.0)

    app = _regulate_app(sensor, reading, setpoint=800.0)

    # Land the operator's edit mid-iteration: the loop has already read 800, and
    # writes its outputs afterwards. Before the fix, one of those writes went to
    # regulate_setpoint and reverted the slider to 800 for a poll period.
    measured_set = app.tags.regulate_measured.set
    key = f"regulate_setpoint_{sensor.sanitised}"

    async def set_measured(value, log=False):
        app.ui_manager.values[key] = 900.0  # operator moves the slider
        await measured_set(value, log)

    app.tags.regulate_measured.set = set_measured
    _run(app._regulate_target_rpm(app.mode))

    assert app.ui_manager.values[key] == 900.0  # operator's edit survives
    # The loop publishes what it was holding on its own tag instead.
    assert app.tags.regulate_setpoint_active.get() == 800.0


def test_settled_regulate_loop_stops_logging_every_poll():
    """Agent 213808101676069636 went from ~460 messages a day to 30,697 on
    2026-08-18.

    The four plot tags carried log_on=Delta(percent=2), whose threshold is a
    percentage of the last logged value. regulate_error is a signed error that
    converges on zero, so as the loop settled the threshold fell below the
    sensor's own noise and every poll became a data point (and each firing
    flushes its own channel message at the end of the loop).
    """
    from dse_engine_controller.application import (
        ANALOG_LOG_DELTA_FRACTION,
        DseEngineControllerApplication as App,
    )

    app = types_ns(_regulate_logged=None, _regulate_logged_at=0.0)
    app._should_log_regulate = types_method(App._should_log_regulate, app)

    span = 10.0                      # 0..10 bar sensor
    noise = span * ANALOG_LOG_DELTA_FRACTION / 10   # well under the threshold

    # First step always plants a point.
    assert app._should_log_regulate(5.0, 5.0, span, 0.0) is True

    # A converged loop jittering by sensor noise logs nothing for a long while,
    # however close to zero the error gets.
    measured, t = 5.0, 0.0
    for i in range(100):
        t += 2.0                     # the 2s poll period
        measured += noise if i % 2 else -noise
        assert app._should_log_regulate(measured, 5.0, span, t) is False, i

    # A real move still lands immediately.
    assert app._should_log_regulate(5.0 + span * ANALOG_LOG_DELTA_FRACTION * 2, 5.0, span, t) is True

    # And a dead-steady loop still anchors its plot on the heartbeat.
    from dse_engine_controller.application import ANALOG_LOG_HEARTBEAT_S

    steady = app._regulate_logged[0]
    assert app._should_log_regulate(steady, 5.0, span, t + 1) is False
    assert app._should_log_regulate(steady, 5.0, span, t + ANALOG_LOG_HEARTBEAT_S) is True


def test_regulate_series_log_together_or_not_at_all():
    """All four feed one plot, so they share a single logging decision — the
    series line up on the same x-points and cost one message between them."""
    from dse_engine_controller.analog import AnalogInput

    sensor = _slot("inlet", 500.0, analog=True, pin=2, units="kPa",
                   sp_max=1600.0, value_max=1600.0)
    reading = AnalogInput.from_slot(sensor)
    reading.update(12.0, 1.0)
    app = _regulate_app(sensor, reading, setpoint=800.0)

    logged = {}
    for name in ("regulate_measured", "regulate_setpoint_active",
                 "regulate_error", "regulate_output_rpm"):
        async def recorder(value, log=False, _name=name):
            logged[_name] = log

        setattr(app.tags, name, types_ns(set=recorder))

    _run(app._regulate_target_rpm(app.mode))
    assert set(logged) == {"regulate_measured", "regulate_setpoint_active",
                           "regulate_error", "regulate_output_rpm"}
    assert len(set(logged.values())) == 1, logged   # all four agree
    assert logged["regulate_error"] is True         # first step plants a point


def test_a_stopped_loop_resets_the_logging_baseline():
    """Coming back after a stop should plant a fresh first point, not measure
    against a reading from before the gap."""
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App

    app = SimpleNamespace(
        tags=SimpleNamespace(
            regulate_status=_TagStub(""), regulate_error=_TagStub(1.0),
            regulate_output_rpm=_TagStub(1500), regulate_measured=_TagStub(5.0),
            regulate_setpoint_active=_TagStub(5.0),
        ),
        _regulate_logged=(5.0, 5.0),
        _regulate_logged_at=100.0,
    )
    app._clear_regulate_readouts = types.MethodType(App._clear_regulate_readouts, app)
    _run(app._clear_regulate_readouts("engine stopped"))
    assert app._regulate_logged is None
    assert app.tags.regulate_error.get() is None


def types_ns(**kwargs):
    from types import SimpleNamespace

    return SimpleNamespace(**kwargs)


def test_regulate_clears_the_plotted_setpoint_when_it_cannot_run():
    app = _regulate_app(_slot("inlet", 500.0), None, setpoint=None)
    assert _run(app._regulate_target_rpm(app.mode)) is None
    assert app.tags.regulate_setpoint_active.get() is None


def _recording_regulator(app):
    """Capture the timestep the PI loop is handed on each step."""
    steps = []
    real_update = app.regulator.update

    def update(measured, setpoint, sensitivity, dt, **kw):
        steps.append(dt)
        return real_update(measured, setpoint, sensitivity, dt, **kw)

    app.regulator.update = update
    return steps


def test_regulate_caps_the_timestep_after_skipped_steps():
    """A short outage must not hand the integrator the whole gap.

    Station 2 (2026-08-18): a simulator restart took the sensor and the panel away
    for ~13 s, and the first steps back integrated the gap in one hit — 1704 RPM
    became 1808 then 1958, two polls apart, heading for the 2000 rail.
    """
    import time

    from dse_engine_controller.application import MAX_REGULATE_DT_PERIODS

    sensor = _slot("inlet", 500.0, sp_max=1600.0)
    app = _regulate_app(sensor, None, setpoint=800.0)
    steps = _recording_regulator(app)

    _run(app._regulate_target_rpm(app.mode))  # first step seeds the loop (dt 0)
    # 5 s of held steps, then a good reading — just under the re-seed threshold.
    app._last_regulate_time = time.monotonic() - 5.0
    _run(app._regulate_target_rpm(app.mode))

    assert steps[0] == 0.0
    assert steps[1] == MAX_REGULATE_DT_PERIODS * app.loop_target_period == 4.0


def test_regulate_reseeds_from_engine_rpm_after_a_long_gap():
    import time

    from dse_engine_controller.application import REGULATE_RESEED_PERIODS

    sensor = _slot("inlet", 500.0, sp_max=1600.0)
    app = _regulate_app(sensor, None, setpoint=800.0, rpm=1500.0)
    _run(app._regulate_target_rpm(app.mode))

    # Long outage; meanwhile the engine ends up somewhere else entirely.
    app.tags.engine_rpm._v = 1700.0
    gap = REGULATE_RESEED_PERIODS * app.loop_target_period + 1
    app._last_regulate_time = time.monotonic() - gap
    steps = _recording_regulator(app)
    target = _run(app._regulate_target_rpm(app.mode))

    # Resumes from where the engine actually is, with no integrated backlog.
    assert target == 1700
    assert steps == [0.0]


def test_regulate_uses_a_normal_timestep_when_stepping_each_poll():
    import time

    sensor = _slot("inlet", 500.0, sp_max=1600.0)
    app = _regulate_app(sensor, None, setpoint=800.0)
    _run(app._regulate_target_rpm(app.mode))

    app._last_regulate_time = time.monotonic() - 2.0
    steps = _recording_regulator(app)
    _run(app._regulate_target_rpm(app.mode))

    assert 1.9 <= steps[0] <= 2.1  # the real elapsed time, not the cap


def test_regulate_holds_rpm_when_the_analog_loop_fails():
    from dse_engine_controller.analog import AnalogInput

    sensor = _slot("inlet", 500.0, analog=True, pin=2, units="kPa",
                   sp_max=1600.0, value_max=1600.0)
    reading = AnalogInput.from_slot(sensor)
    reading.update(12.0, 1.0)
    reading.update(2.0, 1.0)  # open circuit

    app = _regulate_app(sensor, reading)
    assert _run(app._regulate_target_rpm(app.mode)) is None  # hold, don't chase
    status = app.tags.regulate_status.get()
    assert "open circuit" in status and "holding RPM" in status
    assert app.tags.regulate_error.get() is None


# ---------------------------------------------------------------------------
# Sensor fault action
# ---------------------------------------------------------------------------


class _FaultyReading:
    """Stands in for an AnalogInput that has been faulted for *seconds*."""

    def __init__(self, reason="open circuit", seconds=60.0, has_reading=True):
        self.fault = bool(reason)
        self.fault_reason = reason
        self.has_reading = has_reading
        self._seconds = seconds

    def faulted_for(self, now):
        return self._seconds


def _fault_app(**kw):
    """An auto-control app whose analog sensor at index 6 has failed."""
    sensors = {"level": _slot("level", 500.0, analog=True, pin=2, faulted=True)}
    defaults = dict(sensors=sensors, analog={"level": _FaultyReading()}, latch=True)
    defaults.update(kw)
    return _auto_app(**defaults)


def test_sensor_fault_hold_leaves_the_engine_running():
    from dse_engine_controller.application import DseEngineControllerApplication

    app = _fault_app(rpm_mode="regulate", regulate_var="level", fault_action="Hold")
    _run(DseEngineControllerApplication._evaluate_auto_control(app))
    assert app._operator_run_latch is True
    assert app._sensor_fault_stopped is False


def test_sensor_fault_does_not_claim_a_stop_when_already_stopped():
    from dse_engine_controller.application import DseEngineControllerApplication

    app = _fault_app(
        rpm_mode="regulate", regulate_var="level", fault_action="Stop Engine",
        latch=False,
    )
    _run(DseEngineControllerApplication._evaluate_auto_control(app))
    assert app._sensor_fault_stopped is False


def test_sensor_fault_stops_the_engine_when_configured():
    from dse_engine_controller.application import DseEngineControllerApplication

    app = _fault_app(
        rpm_mode="regulate", regulate_var="level", fault_action="Stop Engine"
    )
    _run(DseEngineControllerApplication._evaluate_auto_control(app))
    assert app._operator_run_latch is False
    assert app._suppress_run_tag is True  # a stuck run tag can't re-start it
    assert app._sensor_fault_stopped is True


def test_sensor_fault_never_stops_on_a_sensor_that_has_never_read():
    from dse_engine_controller.application import DseEngineControllerApplication

    # At boot every input reads "no reading" — a slow platform interface must not
    # look like a dead sensor and shut a running engine down.
    app = _fault_app(
        rpm_mode="regulate", regulate_var="level", fault_action="Stop Engine",
        analog={"level": _FaultyReading(reason="no reading", has_reading=False)},
    )
    _run(DseEngineControllerApplication._evaluate_auto_control(app))
    assert app._operator_run_latch is True
    assert app._sensor_fault_stopped is False


def test_sensor_fault_stop_waits_out_the_grace_period():
    from dse_engine_controller.application import DseEngineControllerApplication

    app = _fault_app(
        rpm_mode="regulate", regulate_var="level", fault_action="Stop Engine",
        analog={"level": _FaultyReading(seconds=10.0)}, fault_grace=30,
    )
    _run(DseEngineControllerApplication._evaluate_auto_control(app))
    assert app._operator_run_latch is True  # glitch, not a shutdown


def test_sensor_fault_on_the_stop_trigger_stops_the_engine():
    from dse_engine_controller.application import DseEngineControllerApplication

    # The stop trigger is what shuts the engine down, so losing it matters.
    app = _fault_app(
        stop_mode="sensor", stop_var="level", stop_sp=900, stop_dir="Above",
        fault_action="Stop Engine",
    )
    _run(DseEngineControllerApplication._evaluate_auto_control(app))
    assert app._operator_run_latch is False


def test_sensor_fault_on_the_start_trigger_does_not_stop_the_engine():
    from dse_engine_controller.application import DseEngineControllerApplication

    # A broken start sensor can only fail to *request* a run — already safe.
    app = _fault_app(
        start_mode="sensor", start_var="level", start_sp=100, start_dir="Below",
        fault_action="Stop Engine",
    )
    _run(DseEngineControllerApplication._evaluate_auto_control(app))
    assert app._operator_run_latch is True


def test_healthy_sensor_never_triggers_the_fault_action():
    from dse_engine_controller.application import DseEngineControllerApplication

    app = _fault_app(
        rpm_mode="regulate", regulate_var="level", fault_action="Stop Engine",
        analog={"level": _FaultyReading(reason="")},
    )
    _run(DseEngineControllerApplication._evaluate_auto_control(app))
    assert app._operator_run_latch is True


def test_horn_pulse_three_on_ten_off():
    from dse_engine_controller.application import DseEngineControllerApplication

    pulse = DseEngineControllerApplication._horn_pulse_on  # uses only `now`
    # On for the first 3s of each 13s cycle, off for the remaining 10s.
    assert pulse(None, 0.0) is True
    assert pulse(None, 2.9) is True
    assert pulse(None, 3.0) is False
    assert pulse(None, 12.0) is False
    assert pulse(None, 13.0) is True  # next cycle starts
    on_seconds = sum(1 for s in range(13) if pulse(None, float(s)))
    assert on_seconds == 3


def test_slider_step_picks_tidy_increments():
    from dse_engine_controller.app_ui import _slider_step

    assert _slider_step(0, 1000) == 10.0
    assert _slider_step(1, 10) == 0.1
    assert _slider_step(0, 0) == 1.0  # degenerate span


# ---------------------------------------------------------------------------
# RPM Control mode toggles which sub-panel is shown
# ---------------------------------------------------------------------------


class _TagStub:
    def __init__(self, value=None):
        self._v = value

    def get(self):
        return self._v

    async def set(self, value, log=False):
        self._v = value


class _LoggingTagStub(_TagStub):
    """Remembers which sets asked for the value to be logged."""

    def __init__(self, value=None):
        super().__init__(value)
        self.logged = []

    async def set(self, value, log=False):
        await super().set(value)
        if log:
            self.logged.append(value)


def _visibility_app(mode, *, in_manual=False):
    """An app stub carrying just what the RPM panel visibility reads."""
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication
    from dse_engine_controller.run_modes import (
        CONTROL_FIXED, CONTROL_REGULATE, ModeSequencer, RunMode,
    )

    app = SimpleNamespace()
    app.tags = _TagsStub()
    app._manual_while_running = in_manual
    app._selected_mode = None
    app.sequencer = ModeSequencer(
        [
            RunMode("Fixed", "fixed", CONTROL_FIXED),
            RunMode("Run", "run", CONTROL_REGULATE, variable="level"),
        ],
        [],
    )
    app.ui_manager = _UIManagerStub()
    app.ui_manager.values["run_mode"] = mode
    for name in ("_command_value", "_active_mode"):
        app.__dict__[name] = types_method(
            getattr(DseEngineControllerApplication, name), app
        )
    return app


class _TagsStub:
    """Tags an RPM-visibility check touches: the picker, and one flag per mode."""

    def __init__(self, mode=None):
        self.run_mode_hidden = _TagStub(None)
        self.regulate_plot_hidden = _TagStub(None)
        self.mode_fixed_hidden = _TagStub(None)
        self.mode_run_hidden = _TagStub(None)


# ---------------------------------------------------------------------------
# Auto start/stop control (Sensor / Timer trigger logic)
# ---------------------------------------------------------------------------


class _SensorTag:
    def __init__(self, value):
        self._v = value

    def get(self):
        return self._v


def _slot(label, value, *, analog=False, faulted=False, tag_key=None,
          allow_rpm_control=True, **kw):
    """A SensorSlot as sensor_specs would produce, with a canned reading."""
    from dse_engine_controller.sensors import (
        HIDDEN_TAG_PREFIXES,
        SENSOR_KIND_ANALOG,
        SENSOR_KIND_REMOTE,
        SensorSlot,
    )

    slot = SensorSlot(
        kind=SENSOR_KIND_ANALOG if analog else SENSOR_KIND_REMOTE,
        label=label,
        sanitised=label,
        tag_key=tag_key or (f"pin{kw.get('pin', 0)}" if analog else "0"),
        units=kw.pop("units", ""),
        invert=kw.pop("invert", False),
        sp_min=kw.pop("sp_min", 0.0),
        sp_max=kw.pop("sp_max", 100.0),
        allow_rpm_control=allow_rpm_control,
        **kw,
    )
    slot.tag = _SensorTag(value)
    if analog:
        slot.fault_tag = _TagStub(faulted)
    slot.hidden_tags = {prefix: _TagStub(True) for prefix in HIDDEN_TAG_PREFIXES}
    return slot



class _AutoStub:
    def __init__(self, state="idle", time_in_state=0.0):
        self.state = state
        self.time_in_state = time_in_state


def _auto_app(**kw):
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication

    defaults = dict(
        start_mode="manual", stop_mode="manual", start_var="", stop_var="",
        start_sp=None, stop_sp=None, start_at=None, stop_at=None,
        start_dir="Below", stop_dir="Above", sensors=None, latch=False, auto=None,
        rpm_mode="fixed", regulate_var="", analog=None,
        fault_action="Hold", fault_grace=30, transitions=None,
    )
    defaults.update(kw)
    app = SimpleNamespace()
    app.tags = SimpleNamespace(
        start_sensor_hidden=_TagStub(None),
        start_timer_hidden=_TagStub(None),
        stop_sensor_hidden=_TagStub(None),
        stop_timer_hidden=_TagStub(None),
    )
    # Modes, trigger variables, setpoints and the scheduled times are all
    # operator settings, so they live in the ui_cmds aggregate keyed by element
    # name — the per-sensor setpoint sliders included.
    cmds = _UIManagerStub()
    cmds.values.update({
        "start_control_mode": defaults["start_mode"],
        "stop_control_mode": defaults["stop_mode"],
        "start_variable": defaults["start_var"],
        "stop_variable": defaults["stop_var"],
        "start_timer_at": defaults["start_at"],
        "stop_timer_at": defaults["stop_at"],
    })
    # The RPM mode is now one of the configured run modes rather than a
    # hardcoded pair, so the stub builds the one this test needs.
    from dse_engine_controller.run_modes import (
        CONTROL_FIXED, CONTROL_REGULATE, ModeSequencer, RunMode,
    )

    if defaults["rpm_mode"] == "regulate":
        mode = RunMode(
            "Run", "run", CONTROL_REGULATE,
            variable=defaults["regulate_var"],
            variable_label=defaults["regulate_var"],
        )
    else:
        mode = RunMode("Fixed", "fixed", CONTROL_FIXED)
    app._selected_mode = None
    app.sequencer = ModeSequencer([mode], list(defaults.get("transitions") or []))
    cmds.values["run_mode"] = mode.sanitised
    if defaults["start_var"]:
        cmds.values[f"start_setpoint_{defaults['start_var']}"] = defaults["start_sp"]
    if defaults["stop_var"]:
        cmds.values[f"stop_setpoint_{defaults['stop_var']}"] = defaults["stop_sp"]
    app.ui_manager = cmds
    app._ui_values = cmds.values  # exposed so tests can re-pick a time
    app._start_timer_fired_at = None
    app._stop_timer_fired_at = None
    app.config = SimpleNamespace(
        start_sensor_direction=_FakeElem(defaults["start_dir"]),
        stop_sensor_direction=_FakeElem(defaults["stop_dir"]),
        sensor_fault_action=_FakeElem(defaults["fault_action"]),
        sensor_fault_grace_seconds=_FakeElem(defaults["fault_grace"]),
    )
    app._sensor_by_name = defaults["sensors"] or {}
    app._analog = defaults["analog"] or {}
    app._operator_run_latch = defaults["latch"]
    app._suppress_run_tag = False
    app._sensor_fault_stopped = False
    app.autostart = defaults["auto"] or _AutoStub()
    # Bind the real (pure) methods the orchestration calls on self.
    for name in (
        "_auto_start_demanded", "_auto_stop_demanded", "_sensor_crossed",
        "_timer_reached", "_controlling_sensors", "_sensor_fault_stop_reason",
        "_active_mode",
        "_command_value", "_selected_setpoint", "_apply_control_mode_visibility",
    ):
        setattr(app, name, types.MethodType(getattr(DseEngineControllerApplication, name), app))
    return app




def _snap(running=True, warming=False):
    from types import SimpleNamespace

    return SimpleNamespace(is_running=running, is_warming_up=warming)




















def test_control_mode_normalises():
    from dse_engine_controller.application import _control_mode

    assert _control_mode("Sensor") == "sensor"
    assert _control_mode("TIMER") == "timer"
    assert _control_mode("Manual") == "manual"
    assert _control_mode("garbage") == "manual"
    assert _control_mode(None) == "manual"


def test_sensor_crossed_directions():
    from dse_engine_controller.application import DseEngineControllerApplication

    crossed = DseEngineControllerApplication._sensor_crossed
    app = _auto_app(sensors={"level": _slot("level", 80.0)})

    assert crossed(app, "level", 50, "Above") is True
    assert crossed(app, "level", 90, "Above") is False
    assert crossed(app, "level", 90, "Below") is True
    assert crossed(app, "level", 50, "Below") is False
    # Missing sensor / setpoint / reading → never fires.
    assert crossed(app, "missing", 50, "Above") is False
    assert crossed(app, "level", None, "Above") is False
    assert crossed(_auto_app(sensors={"level": _slot("level", None)}),
                   "level", 50, "Above") is False
    # A faulted native input never fires a trigger, whatever it last read.
    assert crossed(
        _auto_app(sensors={"level": _slot("level", 80.0, analog=True, faulted=True)}),
        "level", 50, "Above",
    ) is False


def test_sensor_start_demand_sets_latch():
    from dse_engine_controller.application import DseEngineControllerApplication

    # Start when level is at/below 30; level=20 → demand a start.
    app = _auto_app(
        start_mode="sensor", start_var="level", start_sp=30, start_dir="Below",
        sensors={"level": _slot("level", 20.0)},
    )
    _run(DseEngineControllerApplication._evaluate_auto_control(app))
    assert app._operator_run_latch is True


def test_sensor_stop_takes_precedence_over_start():
    from dse_engine_controller.application import DseEngineControllerApplication

    # Start demand active (level low) AND stop demand active (level high) can't
    # both be true for one sensor, but verify stop wins when both fire.
    app = _auto_app(
        start_mode="sensor", start_var="lo", start_sp=100, start_dir="Below",
        stop_mode="sensor", stop_var="hi", stop_sp=90, stop_dir="Above",
        sensors={"lo": _slot("lo", 50.0), "hi": _slot("hi", 95.0, tag_key="1")},
        latch=True,
    )
    _run(DseEngineControllerApplication._evaluate_auto_control(app))
    assert app._operator_run_latch is False
    assert app._suppress_run_tag is True


def test_timer_start_and_stop_demands():
    import time

    from dse_engine_controller.application import DseEngineControllerApplication
    ev = DseEngineControllerApplication._evaluate_auto_control

    now_ms = time.time() * 1000.0
    past = now_ms - 60_000.0    # scheduled 1 min ago → reached
    future = now_ms + 3_600_000.0  # scheduled 1 hr out → not reached

    # Scheduled start time reached while idle → start.
    app = _auto_app(start_mode="timer", start_at=past, auto=_AutoStub("idle"))
    _run(ev(app))
    assert app._operator_run_latch is True

    # Scheduled start time still in the future → no start.
    app = _auto_app(start_mode="timer", start_at=future, auto=_AutoStub("idle"))
    _run(ev(app))
    assert app._operator_run_latch is False

    # Scheduled stop time reached while running → stop.
    app = _auto_app(stop_mode="timer", stop_at=past, latch=True,
                    auto=_AutoStub("running"))
    _run(ev(app))
    assert app._operator_run_latch is False


def test_timer_start_fires_once_per_scheduled_time():
    import time

    from dse_engine_controller.application import DseEngineControllerApplication
    ev = DseEngineControllerApplication._evaluate_auto_control

    past = time.time() * 1000.0 - 60_000.0
    app = _auto_app(start_mode="timer", start_at=past, auto=_AutoStub("idle"))

    # First evaluation fires the scheduled start.
    _run(ev(app))
    assert app._operator_run_latch is True

    # Simulate the engine having run and returned to idle without a new time
    # being picked — the already-actioned time must not re-trigger a start.
    app._operator_run_latch = False
    _run(ev(app))
    assert app._operator_run_latch is False

    # Operator picks a new (later, but already-past) time → re-arms and fires.
    app._ui_values["start_timer_at"] = past + 1_000.0
    _run(ev(app))
    assert app._operator_run_latch is True


def test_manual_mode_does_not_touch_latch():
    from dse_engine_controller.application import DseEngineControllerApplication

    app = _auto_app(start_mode="manual", stop_mode="manual", latch=True)
    _run(DseEngineControllerApplication._evaluate_auto_control(app))
    assert app._operator_run_latch is True  # untouched


def test_control_mode_visibility():

    app = _auto_app(start_mode="sensor", stop_mode="timer")
    _run(app._apply_control_mode_visibility())
    assert app.tags.start_sensor_hidden.get() is False
    assert app.tags.start_timer_hidden.get() is True
    assert app.tags.stop_sensor_hidden.get() is True
    assert app.tags.stop_timer_hidden.get() is False  # stop mode = timer


def test_clamping_handlers_do_not_echo_the_raw_value():
    """Interactions that clamp (or normalise) must not auto-update.

    pydoover echoes the raw payload back into the ui_cmds aggregate after a
    successful handler. The aggregate is where these controls keep their value,
    so an echo would overwrite the clamped value the handler just settled on with
    the operator's raw one.
    """
    from dse_engine_controller.application import DseEngineControllerApplication as App

    for name in (
        "on_mode_target_rpm", "on_mode_setpoint", "on_mode_sensitivity",
        "on_start_setpoint", "on_stop_setpoint", "on_run_mode",
        "on_start_control_mode", "on_stop_control_mode",
        "on_start_variable", "on_stop_variable",
    ):
        handler = getattr(App, name)
        assert handler._ui_auto_update is False, name


def _seed_app(cmds, modes, *, entered_at=None, rpm_min=950):
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App
    from dse_engine_controller.run_modes import ModeSequencer

    app = _command_app(
        tags=SimpleNamespace(
            run_mode_active=_TagStub(""),
            run_mode_variable=_TagStub(""),
            run_mode_status=_TagStub(""),
            run_mode_status_hidden=_TagStub(False),
            run_mode_entered_at=_TagStub(entered_at),
        ),
        config=SimpleNamespace(
            target_rpm_min=_FakeElem(rpm_min),
            target_rpm_max=_FakeElem(2000),
            regulate_min_sensitivity=_FakeElem(0.1),
            regulate_max_sensitivity=_FakeElem(10.0),
        ),
        cmds=cmds,
    )
    app.sequencer = ModeSequencer(list(modes), [])
    app._run_mode_problems = []
    app._selected_mode = None
    app.autostart = None
    for name in ("_seed_run_mode", "_seed_element", "_active_mode",
                 "_publish_run_mode", "_tuned_transitions"):
        app.__dict__[name] = types_method(getattr(App, name), app)
    return app


def test_each_mode_is_seeded_from_its_own_config():
    """An unset slider renders empty and gives the loop nothing to hold, so every
    mode's controls get its configured starting values."""
    from dse_engine_controller.run_modes import CONTROL_FIXED, CONTROL_REGULATE, RunMode

    cmds = _UIManagerStub()
    app = _seed_app(
        cmds,
        [
            RunMode("Warm Up", "warm_up", CONTROL_FIXED, target_rpm=800),
            RunMode("Run", "run", CONTROL_REGULATE, variable="level",
                    setpoint=250.0, sensitivity=4.0),
        ],
        rpm_min=700,
    )
    _run(app._seed_run_mode())

    assert cmds.values["mode_warm_up_target_rpm"] == 800
    assert cmds.values["mode_run_setpoint"] == 250.0
    assert cmds.values["mode_run_sensitivity"] == 4.0
    # The first mode is where a fresh install starts.
    assert cmds.values["run_mode"] == "warm_up"


def test_a_fixed_mode_without_a_configured_rpm_falls_back_to_the_minimum():
    from dse_engine_controller.run_modes import CONTROL_FIXED, RunMode

    cmds = _UIManagerStub()
    app = _seed_app(cmds, [RunMode("Fixed", "fixed", CONTROL_FIXED)])
    _run(app._seed_run_mode())
    assert cmds.values["mode_fixed_target_rpm"] == 950  # the slowest the panel takes


def test_seeding_leaves_an_operators_value_alone():
    from dse_engine_controller.run_modes import CONTROL_FIXED, RunMode

    cmds = _UIManagerStub()
    app = _seed_app(cmds, [RunMode("Fixed", "fixed", CONTROL_FIXED, target_rpm=800)])
    cmds.values["mode_fixed_target_rpm"] = 1400
    _run(app._seed_run_mode())
    assert cmds.values["mode_fixed_target_rpm"] == 1400


def test_the_active_mode_and_its_clock_survive_a_restart():
    """The whole point of keeping the selection in rpm_mode: an app restart
    mid-run picks the sequence up where it left off, and ideally nobody notices."""
    from dse_engine_controller.run_modes import CONTROL_FIXED, RunMode

    cmds = _UIManagerStub()
    modes = [
        RunMode("Warm Up", "warm_up", CONTROL_FIXED, target_rpm=800),
        RunMode("Line Fill", "line_fill", CONTROL_FIXED, target_rpm=1300),
    ]
    cmds.values["run_mode"] = "line_fill"  # what the last run left behind
    app = _seed_app(cmds, modes, entered_at=1234.0)
    _run(app._seed_run_mode())

    assert cmds.values["run_mode"] == "line_fill"
    assert app.tags.run_mode_entered_at.get() == 1234.0  # timers don't restart
    assert app.tags.run_mode_active.get() == "Line Fill"


def test_a_stored_mode_that_no_longer_exists_falls_back():
    from dse_engine_controller.run_modes import CONTROL_FIXED, RunMode

    cmds = _UIManagerStub()
    cmds.values["run_mode"] = "deleted_mode"
    app = _seed_app(cmds, [RunMode("Warm Up", "warm_up", CONTROL_FIXED)])
    _run(app._seed_run_mode())
    assert cmds.values["run_mode"] == "warm_up"


class _UIManagerStub:
    """Records what the app mirrors into the ui_cmds aggregate."""

    def __init__(self):
        self.values = {}
        self.logged = []

    async def set_value(self, key, value, log_update=True):
        self.values[key] = value
        if log_update:
            self.logged.append(key)

    def get_value(self, key):
        return self.values.get(key)


def _command_app(*, tags, config=None, cmds=None, **extra):
    """A stub app wired for the ui_cmds mirroring helper."""
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App

    app = SimpleNamespace(tags=tags, ui_manager=cmds or _UIManagerStub(), **extra)
    if config is not None:
        app.config = config
    for name in ("_set_command_value", "_command_value", "_selected_setpoint"):
        setattr(app, name, types.MethodType(getattr(App, name), app))
    return app


def _dse_snapshot(image=None):
    """A real DseSnapshot over a sparse image, so absent pages decode to None."""
    from dse_engine_controller.dse_state import DseSnapshot

    return DseSnapshot(dict(image or {}), 0.0)


def test_unread_registers_publish_as_absent_not_zero():
    """The slow tier hasn't run yet, or the module doesn't implement a page:
    either way the tag reads None, never an invented zero."""
    import types
    from types import SimpleNamespace

    from dse_engine_controller import gencomm as gc
    from dse_engine_controller.application import DseEngineControllerApplication as App

    names = (
        "engine_rpm", "engine_load_pct", "coolant_temp_c", "oil_temp_c",
        "oil_pressure_kpa", "coolant_level_pct", "oil_level_pct", "fuel_level_pct",
        "fuel_rate_lph", "fuel_used_l", "boost_pressure_kpa",
        "intake_manifold_temp_c", "exhaust_temp_c", "alternator_voltage",
        "battery_voltage", "total_engine_hours", "number_of_starts",
        "manual_run_time_remaining_s",
    )
    tags = SimpleNamespace(**{n: _TagStub(None) for n in names})
    app = SimpleNamespace(tags=tags)
    app._publish_engine_telemetry = types.MethodType(App._publish_engine_telemetry, app)

    _run(app._publish_engine_telemetry(_dse_snapshot()))
    assert all(getattr(tags, n).get() is None for n in names)

    image = {
        gc.address(gc.PAGE_BASIC, gc.P4_ENGINE_SPEED): 1500,
        gc.address(gc.PAGE_BASIC, gc.P4_BATTERY_VOLTS): 128,
        gc.address(gc.PAGE_BASIC, gc.P4_COOLANT_TEMP): 0xFFCE,  # -50 °C, signed
        gc.address(gc.PAGE_BASIC, gc.P4_OIL_TEMP): 0x7FFF,  # unimplemented
        gc.address(gc.PAGE_ACCUMULATED, gc.P7_ENGINE_RUN_TIME): 0,
        gc.address(gc.PAGE_ACCUMULATED, gc.P7_ENGINE_RUN_TIME + 1): 7200,
    }
    _run(app._publish_engine_telemetry(_dse_snapshot(image)))
    assert tags.engine_rpm.get() == 1500
    assert tags.battery_voltage.get() == 12.8
    assert tags.coolant_temp_c.get() == -50
    assert tags.oil_temp_c.get() is None
    assert tags.total_engine_hours.get() == 2.0
    assert tags.fuel_rate_lph.get() is None  # page 5 never read


def test_flex_sensors_are_hidden_until_the_module_says_they_are_configured():
    import types
    from types import SimpleNamespace

    from dse_engine_controller import gencomm as gc
    from dse_engine_controller.app_tags import flex_hidden_tag, flex_value_tag
    from dse_engine_controller.application import DseEngineControllerApplication as App

    tags = SimpleNamespace()
    for letter in "ABCDEFG":
        setattr(tags, flex_value_tag(letter), _TagStub(None))
        setattr(tags, flex_hidden_tag(letter), _TagStub(True))
    app = SimpleNamespace(tags=tags)
    app._publish_flex_sensors = types.MethodType(App._publish_flex_sensors, app)

    base = gc.flex_unit_info_offset(0)
    image = {
        gc.address(gc.PAGE_FLEX_SENSORS, 0): 350,
        gc.address(gc.PAGE_FLEX_SENSORS, base + gc.P178_UNIT_ID): gc.UnitId.PRESSURE,
        gc.address(gc.PAGE_FLEX_SENSORS, base + gc.P178_MULTIPLIER): 0,
        gc.address(gc.PAGE_FLEX_SENSORS, base + gc.P178_PRECISION): 0,
        # B is wired as a digital input: a value makes no sense on a gauge.
        gc.address(gc.PAGE_FLEX_SENSORS, 1): 1,
        gc.address(gc.PAGE_FLEX_SENSORS, gc.flex_unit_info_offset(1) + gc.P178_UNIT_ID): gc.UnitId.DIGITAL_INPUT,
    }
    _run(app._publish_flex_sensors(_dse_snapshot(image)))
    assert tags.flex_a_hidden.get() is False and tags.flex_a_value.get() == 350
    assert tags.flex_b_hidden.get() is True
    assert tags.flex_c_hidden.get() is True and tags.flex_c_value.get() is None


def test_maintain_value_readouts_hide_when_the_module_has_no_loop():
    import types
    from types import SimpleNamespace

    from dse_engine_controller import gencomm as gc
    from dse_engine_controller.application import DseEngineControllerApplication as App

    names = (
        "target_speed_rpm", "maintain_available", "maintain_hidden", "maintain_setpoint",
        "maintain_setpoint_min", "maintain_setpoint_max", "maintain_value", "maintain_units",
        "maintain_source", "maintain_status", "maintain_p_gain", "maintain_i_gain",
        "maintain_d_gain",
    )
    tags = SimpleNamespace(**{n: _TagStub(None) for n in names})
    app = SimpleNamespace(tags=tags)
    app._publish_speed_control = types.MethodType(App._publish_speed_control, app)

    p = gc.PAGE_SPEED_CONTROL
    _run(app._publish_speed_control(_dse_snapshot({gc.address(p, gc.P137_TARGET_SPEED): 1500})))
    assert tags.target_speed_rpm.get() == 1500
    assert tags.maintain_available.get() is False and tags.maintain_hidden.get() is True

    image = {
        gc.address(p, gc.P137_TARGET_SPEED): 1500,
        gc.address(p, gc.P137_SET_POINT): 250,
        gc.address(p, gc.P137_SET_POINT_MIN): 0,
        gc.address(p, gc.P137_SET_POINT_MAX): 1000,
        gc.address(p, gc.P137_CONTROL_SOURCE_VALUE): 150,
        gc.address(p, gc.P137_CONTROL_SOURCE_UNIT_ID): gc.UnitId.PRESSURE,
        gc.address(p, gc.P137_CONTROL_SOURCE_MULTIPLIER): gc.to_s16(-2),
        gc.address(p, gc.P137_CONTROL_SOURCE_PRECISION): 2,
        gc.address(p, gc.P137_DISPLAY_UNITS): 1,
        gc.address(p, gc.P137_SET_POINT_TARGET_SOURCE): 0,
    }
    _run(app._publish_speed_control(_dse_snapshot(image)))
    assert tags.maintain_hidden.get() is False
    assert tags.maintain_setpoint.get() == 2.5
    assert tags.maintain_value.get() == 1.5
    assert tags.maintain_units.get() == "bar"
    assert tags.maintain_status.get() == "Holding 2.5 bar (now 1.5 bar)"
    assert tags.maintain_source.get() == "Specified value"


def test_stale_tag_purge_only_touches_keys_that_are_present():
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import (
        STALE_TAG_KEYS,
        DseEngineControllerApplication as App,
    )

    # Namespaced the way the real aggregate is: tag_values holds
    # {app_key: {tag: value}}, and pydoover reads app_key=None as the root of
    # it rather than as this app. A purge that doesn't name the app therefore
    # reads an empty namespace, finds nothing stale and clears nothing — which
    # is what an earlier version did until this test could see it.
    app_key = "dse_engine_controller_1"
    stored = {
        app_key: {
            "target_rpm_value": 1663,
            "regulate_var_hidden_7": True,
            "engine_rpm": 1500,
        }
    }
    cleared = []

    def get_tag(key, default=None, app_key=None, raise_key_error=False):
        return stored.get(app_key, {}).get(key, default)

    async def set_tag(key, value, app_key=None, **kwargs):
        # A write that names no app lands somewhere nothing reads; recording it
        # as a clear would hide exactly the bug this test exists for.
        if app_key not in stored:
            return
        cleared.append((key, value))
        stored[app_key].pop(key, None)

    app = SimpleNamespace(
        app_key=app_key,
        tag_manager=SimpleNamespace(get_tag=get_tag, set_tag=set_tag),
    )
    app._purge_stale_tags = types.MethodType(App._purge_stale_tags, app)
    _run(app._purge_stale_tags())

    assert sorted(cleared) == [("regulate_var_hidden_7", None), ("target_rpm_value", None)]
    assert stored[app_key] == {"engine_rpm": 1500}  # a live tag is never touched

    # Running again on a clean device is a no-op.
    cleared.clear()
    _run(app._purge_stale_tags())
    assert cleared == []

    # The list covers the keys earlier versions of the Kensho app wrote.
    for key in ("analog_value_0", "analog_fault_1", "reset_btn_hidden",
                "sensor_fault_hidden", "start_btn_hidden", "control_hidden",
                "stop_var_hidden_11", "regulate_setpoint"):
        assert key in STALE_TAG_KEYS, key


def test_stale_command_purge_clears_the_controls_the_run_modes_replaced():
    """Nothing overwrites a ui_cmd once the app stops reading it — it sits in
    the aggregate holding its last value beside the control that replaced it.
    The wall reads ui_cmds directly, so `rpm_mode: regulate` and a setpoint of
    5 bar looked live next to a pump running Line Fill at a fixed RPM."""
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App

    manager = _UIManagerStub()
    manager.values.update(
        {
            # Dead: the Fixed/Regulate pair and its settings.
            "rpm_mode": "regulate",
            "target_rpm": 1354,
            "regulate_variable": "discharge_pressure",
            "regulate_sensitivity": 7,
            # Dead: per-sensor Regulate setpoints, in both generations.
            "regulate_setpoint_discharge_pressure": 5,
            "regulate_setpoint_7": 4.3,
            # Dead: index-keyed start/stop trigger setpoints.
            "stop_setpoint_3": 2.0,
            # Live, and must survive all of it.
            "run_mode": "line_fill",
            "mode_line_fill_target_rpm": 1300,
            "mode_regulate_output_setpoint": 4,
            "start_setpoint_discharge_pressure": 1.5,
            "start_control_mode": "manual",
            "start_now": 1787799960940,
            "v2": {"run_mode": {"message_id": "218544558538028586"}},
        }
    )
    app = SimpleNamespace(ui_manager=manager)
    app._purge_stale_commands = types.MethodType(App._purge_stale_commands, app)
    _run(app._purge_stale_commands())

    assert manager.values["rpm_mode"] is None
    assert manager.values["regulate_setpoint_discharge_pressure"] is None
    assert manager.values["regulate_setpoint_7"] is None
    assert manager.values["stop_setpoint_3"] is None
    assert manager.values["run_mode"] == "line_fill"
    assert manager.values["mode_regulate_output_setpoint"] == 4
    # A sanitised name is not an index — this one is the live control.
    assert manager.values["start_setpoint_discharge_pressure"] == 1.5
    assert manager.values["start_control_mode"] == "manual"
    assert manager.values["v2"] == {"run_mode": {"message_id": "218544558538028586"}}
    # Clearing is the app tidying up after itself, not an operator action.
    assert manager.logged == []

    # Running again on a clean device is a no-op: everything stale now reads
    # None, and a None is what a cleared key already is.
    before = dict(manager.values)
    _run(app._purge_stale_commands())
    assert manager.values == before


def test_stale_command_purge_survives_an_app_with_no_ui_manager():
    """Schema export and unit construction have no aggregate to tidy."""
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App

    app = SimpleNamespace()
    app._purge_stale_commands = types.MethodType(App._purge_stale_commands, app)
    _run(app._purge_stale_commands())  # must not raise


def test_correctly_wired_current_loop_is_not_a_config_problem():
    """The field device reported 'AI 2 is a 4-20mA input (mA), configured as
    4-20 mA' — agreement, flagged as a fault, latching sensor_fault_active true
    forever. kind is compared against "current"; the doovit says "4-20mA"."""
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App

    slot = _slot("inlet", 5.0, analog=True, pin=2)
    channel = SimpleNamespace(channel=2, kind="4-20mA", units="mA")
    mismatched = SimpleNamespace(channel=3, kind="voltage", units="V")

    fault_string, fault_active = _TagStub(""), _TagStub(False)
    app = SimpleNamespace(
        _analog_slots=[slot],
        tags=SimpleNamespace(analog_fault_string=fault_string,
                             sensor_fault_active=fault_active),
        platform_iface=SimpleNamespace(
            fetch_io_details=_async_return(
                SimpleNamespace(channels=lambda io_type: [channel, mismatched])
            )
        ),
    )
    app._validate_analog_pins = types.MethodType(App._validate_analog_pins, app)
    _run(app._validate_analog_pins())

    assert app._analog_config_problems == []
    assert fault_active.get() is False

    # A genuine mismatch — a mA sensor wired to the voltage input — still trips.
    slot.pin = 3
    _run(app._validate_analog_pins())
    assert len(app._analog_config_problems) == 1
    assert "reads in V" in app._analog_config_problems[0]
    assert fault_active.get() is True


def _async_return(value):
    async def _call():
        return value

    return _call


def test_command_defaults_seed_the_controls_a_tag_default_used_to_fill():
    """The selects and the sensitivity slider used to get their opening value
    from a tag default. With the aggregate as the store there is nothing to
    default from, so setup seeds them — otherwise they render blank on a fresh
    install while the loop quietly runs on the fallbacks."""
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App

    cmds = _UIManagerStub()
    app = _command_app(tags=SimpleNamespace(), cmds=cmds)
    app._COMMAND_DEFAULTS = App._COMMAND_DEFAULTS
    app._seed_command_defaults = types_method(App._seed_command_defaults, app)
    _run(app._seed_command_defaults())
    # (The RPM mode and every per-mode control are seeded from the configured run
    # modes instead — see _seed_run_mode.)
    assert cmds.values == {
        "start_control_mode": "manual",
        "stop_control_mode": "manual",
    }

    # An operator's choices survive a restart.
    cmds.values["start_control_mode"] = "sensor"
    _run(app._seed_command_defaults())
    assert cmds.values["start_control_mode"] == "sensor"


def test_switching_variable_hands_the_setpoint_to_the_new_sensor():
    """Each sensor's slider carries its own setpoint. A sensor that has never
    been given one inherits the outgoing sensor's, clamped into its own bounds,
    so switching variable never leaves the loop without a target — the hand-over
    the single shared setpoint used to provide."""
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App

    pressure = _slot("pressure", 5.0, analog=True, pin=2, sp_max=1600.0,
                     value_max=1600.0)
    level = _slot("level", 50.0, analog=True, pin=3, sp_max=100.0, value_max=100.0)

    cmds = _UIManagerStub()
    app = _command_app(tags=SimpleNamespace(), cmds=cmds)
    app._sensors = [pressure, level]
    app._sensor_by_name = {s.sanitised: s for s in app._sensors}
    app._select_sensor_variable = types_method(App._select_sensor_variable, app)

    select = dict(
        select_name="start_variable",
        hidden_prefix="start_var_hidden",
        setpoint_prefix="start_setpoint",
    )
    _run(app._select_sensor_variable("pressure", **select))
    cmds.values["start_setpoint_pressure"] = 900.0

    # Level's slider tops out at 100, so the carried-over 900 is clamped in.
    _run(app._select_sensor_variable("level", **select))
    assert cmds.values["start_setpoint_level"] == 100.0
    assert cmds.values["start_variable"] == "level"
    # Only the selected sensor's slider is shown.
    assert level.hidden_tags["start_var_hidden"].get() is False
    assert pressure.hidden_tags["start_var_hidden"].get() is True

    # Going back finds pressure's own setpoint untouched, not level's.
    _run(app._select_sensor_variable("pressure", **select))
    assert cmds.values["start_setpoint_pressure"] == 900.0


def test_command_mirror_survives_a_missing_or_broken_ui_manager():
    """A failed UI echo must not take the command down — the tag write that
    actually drives the control loop has already happened by then."""
    from types import SimpleNamespace


    class _Exploding:
        async def set_value(self, key, value, log_update=True):
            raise RuntimeError("aggregate update failed")

    app = _command_app(tags=SimpleNamespace(), cmds=_Exploding())
    _run(app._set_command_value("target_rpm", 1200))  # must not raise

    # Standalone construction (schema export, unit tests) has no manager at all.
    app = _command_app(tags=SimpleNamespace())
    app.ui_manager = None
    _run(app._set_command_value("target_rpm", 1200))


def test_clamped_command_values_are_mirrored_back_to_their_element():
    """The operator's raw value is not what the app acts on, so it's not what the
    control should show either."""
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App

    cmds = _UIManagerStub()
    app = _command_app(
        tags=SimpleNamespace(),
        config=SimpleNamespace(
            regulate_min_sensitivity=_FakeElem(1.0),
            regulate_max_sensitivity=_FakeElem(10.0),
        ),
        cmds=cmds,
    )
    ctx = SimpleNamespace(element=SimpleNamespace(name="mode_run_sensitivity"))
    _run(App.on_mode_sensitivity(app, ctx, 99.0))
    assert cmds.values["mode_run_sensitivity"] == 10.0
    # Echoes of an already-logged command don't get logged a second time.
    assert cmds.logged == []


def test_control_mode_select_mirrors_the_normalised_mode():
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App

    cmds = _UIManagerStub()
    app = _command_app(
        tags=SimpleNamespace(
            start_sensor_hidden=_TagStub(None),
            start_timer_hidden=_TagStub(None),
            stop_sensor_hidden=_TagStub(None),
            stop_timer_hidden=_TagStub(None),
        ),
        cmds=cmds,
    )
    app._apply_control_mode_visibility = types_method(
        App._apply_control_mode_visibility, app
    )
    _run(App.on_start_control_mode(app, None, "Sensor"))
    assert cmds.values["start_control_mode"] == "sensor"
    assert app.tags.start_sensor_hidden.get() is False

    # An unrecognised payload falls back to manual — and the select says so.
    _run(App.on_start_control_mode(app, None, "gibberish"))
    assert cmds.values["start_control_mode"] == "manual"
    assert app.tags.start_sensor_hidden.get() is True


def types_method(func, obj):
    import types

    return types.MethodType(func, obj)


def test_rpm_mode_visibility_follows_mode():
    from dse_engine_controller.application import DseEngineControllerApplication

    apply = DseEngineControllerApplication._apply_rpm_mode_visibility

    async def scenario():
        fixed = _visibility_app("fixed")
        await apply(fixed)
        assert fixed.tags.mode_fixed_hidden.get() is False
        assert fixed.tags.mode_run_hidden.get() is True
        assert fixed.tags.run_mode_hidden.get() is False

        reg = _visibility_app("run")
        await apply(reg)
        assert reg.tags.mode_fixed_hidden.get() is True
        assert reg.tags.mode_run_hidden.get() is False
        assert reg.tags.run_mode_hidden.get() is False

    _run(scenario())


def test_rpm_controls_hidden_in_manual_whatever_the_mode():
    """While running in Manual the panel owns engine speed, so none of the RPM
    controls can do anything — the RPM Control Status line is left to explain it."""
    from dse_engine_controller.application import DseEngineControllerApplication

    apply = DseEngineControllerApplication._apply_rpm_mode_visibility

    async def scenario():
        for mode in ("fixed", "run"):
            app = _visibility_app(mode, in_manual=True)
            await apply(app)
            assert app.tags.run_mode_hidden.get() is True, mode
            assert app.tags.mode_fixed_hidden.get() is True, mode
            assert app.tags.mode_run_hidden.get() is True, mode

    _run(scenario())


# ---------------------------------------------------------------------------
# Panel Manual→Auto prep handshake (the reset/auto reboot race)
# ---------------------------------------------------------------------------


class _PanelStub:
    def __init__(self):
        self._reset_issued = False
        self._last_reset_cmd = 0.0
        self._last_auto_cmd = 0.0
        self.resets = 0
        self.autos = 0

    async def write_force_reset(self):
        self.resets += 1
        return True

    async def write_set_auto(self):
        self.autos += 1
        return True


class _Snap:
    def __init__(self, auto=False, faulted=False, running=False, locked=False):
        self.is_auto = auto
        self.is_faulted = faulted
        self.is_running = running
        self.panel_locked = locked
        self.fault_string = "Low oil pressure" if faulted else ""
        self.control_mode_label = "Auto" if auto else "Stop"


def _prep_fn():
    from dse_engine_controller.application import DseEngineControllerApplication

    return DseEngineControllerApplication._prep_panel


def test_prep_panel_nudges_auto_until_it_latches(monkeypatch):
    """An unfaulted panel sitting in Stop or Manual needs no reset — just the
    Auto key, retried every few seconds until the mode register echoes it."""
    from dse_engine_controller import application as appmod

    # Base offset mimics real time.monotonic() (always a large value), so the
    # first nudge isn't blocked by the rate-limit window.
    base = 1000.0
    clock = {"t": base}
    monkeypatch.setattr(appmod.time, "monotonic", lambda: clock["t"])
    prep, stub = _prep_fn(), _PanelStub()

    async def scenario():
        await prep(stub, _Snap())
        assert (stub.resets, stub.autos) == (0, 1)

        for dt in (1.0, 2.0):
            clock["t"] = base + dt
            await prep(stub, _Snap())
        assert (stub.resets, stub.autos) == (0, 1)

        clock["t"] = base + 3.0
        await prep(stub, _Snap())
        assert (stub.resets, stub.autos) == (0, 2)

        # Panel latches Auto → no-op and re-armed for a future fault.
        clock["t"] = base + 4.0
        await prep(stub, _Snap(auto=True))
        assert (stub.resets, stub.autos) == (0, 2)
        assert stub._reset_issued is False

    _run(scenario())


def test_prep_panel_resets_a_latched_alarm_then_moves_to_auto(monkeypatch):
    """A lockout is reset every PANEL_RESET_INTERVAL_S while it persists (the
    cause may still be there), and Auto is only asked for once it has cleared."""
    from dse_engine_controller import application as appmod

    base = 1000.0
    clock = {"t": base}
    monkeypatch.setattr(appmod.time, "monotonic", lambda: clock["t"])
    prep, stub = _prep_fn(), _PanelStub()

    async def scenario():
        await prep(stub, _Snap(faulted=True))
        assert (stub.resets, stub.autos, stub._reset_issued) == (1, 0, True)

        # Still faulted inside the interval: no spam.
        for dt in (1.0, 4.0):
            clock["t"] = base + dt
            await prep(stub, _Snap(faulted=True))
        assert (stub.resets, stub.autos) == (1, 0)

        # ...and a fresh reset once it has passed.
        clock["t"] = base + appmod.PANEL_RESET_INTERVAL_S
        await prep(stub, _Snap(faulted=True))
        assert (stub.resets, stub.autos) == (2, 0)

        # Alarm cleared but the panel is still in Stop: Auto, after one retry gap.
        clock["t"] = base + appmod.PANEL_RESET_INTERVAL_S + 1.0
        await prep(stub, _Snap())
        assert (stub.resets, stub.autos) == (2, 0)
        clock["t"] = base + appmod.PANEL_RESET_INTERVAL_S + appmod.PANEL_AUTO_RETRY_S
        await prep(stub, _Snap())
        assert (stub.resets, stub.autos) == (2, 1)

    _run(scenario())


def test_prep_panel_does_nothing_against_a_panel_lock(monkeypatch):
    """With Panel Lock active nothing we send lands; say so once and wait."""
    from dse_engine_controller import application as appmod

    monkeypatch.setattr(appmod.time, "monotonic", lambda: 1000.0)
    prep, stub = _prep_fn(), _PanelStub()

    async def scenario():
        await prep(stub, _Snap(faulted=True, locked=True))
        await prep(stub, _Snap(locked=True))
        assert (stub.resets, stub.autos) == (0, 0)

    _run(scenario())


def test_prep_panel_skips_running_engine(monkeypatch):
    from dse_engine_controller import application as appmod

    monkeypatch.setattr(appmod.time, "monotonic", lambda: 100.0)
    prep, stub = _prep_fn(), _PanelStub()

    async def scenario():
        # A running engine (even in Manual) is never force-reset.
        await prep(stub, _Snap(running=True))
        assert (stub.resets, stub.autos) == (0, 0)

    _run(scenario())


# ---------------------------------------------------------------------------
# In-flight command progress (ui_cmd `pending` status updates)
# ---------------------------------------------------------------------------


class _ProgressAuto:
    """Stand-in for AutoStartController, for the progress-text helpers."""

    def __init__(self, state="idle", remaining=None, display="Ready"):
        self.state = state
        self.warning_remaining = remaining
        self.display = display


class _CtxStub:
    """Stands in for pydoover's RPCContext, capturing what the handler reports."""

    def __init__(self, fail=False):
        self.acks = 0
        self.texts = []
        self._fail = fail

    async def acknowledge(self):
        if self._fail:
            raise RuntimeError("offline")
        self.acks += 1

    async def progress(self, text=None, **fields):
        if self._fail:
            raise RuntimeError("offline")
        self.texts.append(text)


def _progress_app(state, *, remaining=None, display="Ready", snap=None):
    from types import SimpleNamespace

    app = SimpleNamespace()
    app.autostart = _ProgressAuto(state, remaining, display)
    app.store = SimpleNamespace(latest=snap)
    return app


def _start_text(state, **kw):
    from dse_engine_controller.application import DseEngineControllerApplication

    return DseEngineControllerApplication._start_progress_text(_progress_app(state, **kw))


def _stop_text(state, **kw):
    from dse_engine_controller.application import DseEngineControllerApplication

    return DseEngineControllerApplication._stop_progress_text(_progress_app(state, **kw))


def _panel(auto=True, faulted=False, cooling=False, locked=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        is_auto=auto,
        is_faulted=faulted,
        is_cooling_down=cooling,
        panel_locked=locked,
        fault_string="Low oil pressure" if faulted else "",
    )


def test_start_progress_counts_down_in_steps():
    # Rounded UP to the next 5s step, so it reads 30 → 25 → … → 5, never 0. It is
    # "at least": the countdown is a floor, and the warning runs on past it until
    # the panel can take a start.
    assert _start_text("prestart_warning", remaining=30.0) == "Horn sounding — at least 30s to crank"
    assert _start_text("prestart_warning", remaining=26.4) == "Horn sounding — at least 30s to crank"
    assert _start_text("prestart_warning", remaining=21.0) == "Horn sounding — at least 25s to crank"
    assert _start_text("prestart_warning", remaining=1.2) == "Horn sounding — at least 5s to crank"


def test_start_progress_says_so_while_the_warning_is_held_open():
    # Countdown spent, panel not ready yet — no number to give.
    assert (
        _start_text("prestart_warning", remaining=None)
        == "Horn sounding — preparing panel…"
    )


def test_start_progress_distinguishes_panel_prep_from_cranking():
    # Panel not yet in Auto (or latched faulted) → the app is still doing the
    # reset → Auto handshake, not cranking, and says which.
    assert _start_text("starting", snap=_panel(auto=False)) == "Putting panel in Auto…"
    assert (
        _start_text("starting", snap=_panel(faulted=True))
        == "Resetting panel alarm (Low oil pressure)…"
    )
    assert (
        _start_text("starting", snap=_panel(auto=False, locked=True))
        == "Panel is locked — release Panel Lock to start"
    )
    assert _start_text("starting", snap=_panel()) == "Cranking…"
    # No panel data at all yet.
    assert _start_text("starting", snap=None) == "Waiting for the panel…"


def test_start_progress_terminal_states():
    assert _start_text("running", display="Running") == "Running"
    assert _start_text("fault", display="Fault: no crank") == "Fault: no crank"


def test_start_progress_before_state_machine_advances():
    # The handler sets the run latch, but the machine only leaves idle on the
    # next main loop — the first update must not read as a cancellation.
    assert _start_text("idle") == "Preparing to start…"


def test_stop_progress_reports_cooldown():
    assert _stop_text("stopping", snap=_panel(cooling=True)) == "Cooling down…"
    assert _stop_text("stopping", snap=_panel()) == "Stopping…"
    assert _stop_text("stopping", snap=None) == "Stopping…"
    assert _stop_text("idle") == "Stopped"
    assert _stop_text("fault", display="Fault: stalled") == "Fault: stalled"


def test_stop_progress_before_state_machine_advances():
    # Same window on the way down: still "running" for one loop after the press.
    assert _stop_text("running", snap=_panel()) == "Stopping…"
    assert _stop_text("starting", snap=_panel()) == "Stopping…"


def _await_app():
    from types import SimpleNamespace
    import types

    from dse_engine_controller.application import DseEngineControllerApplication

    app = SimpleNamespace()
    for name in ("_await_until", "_command_progress", "_acknowledge"):
        setattr(app, name, types.MethodType(getattr(DseEngineControllerApplication, name), app))
    return app


def test_await_until_publishes_progress_only_on_change():
    app, ctx = _await_app(), _CtxStub()
    phases = ["Horn sounding — 10s to crank"] * 3 + ["Cranking…"] * 2 + ["Running"]
    calls = {"n": 0}

    def predicate():
        # Resolve once the sequence has played out.
        return calls["n"] >= len(phases)

    def progress():
        text = phases[min(calls["n"], len(phases) - 1)]
        calls["n"] += 1
        return text

    async def scenario():
        assert await app._await_until(
            predicate, timeout=5, poll=0, ctx=ctx, progress=progress
        )

    _run(scenario())
    # Three identical countdown ticks collapse into one channel write.
    assert ctx.texts == ["Horn sounding — 10s to crank", "Cranking…", "Running"]


def test_await_until_heartbeats_unchanged_progress(monkeypatch):
    # A phase like "Cranking…" can sit unchanged for minutes; the site reads
    # progress as proof of life, so it must keep reporting anyway.
    from dse_engine_controller import application as appmod

    app, ctx = _await_app(), _CtxStub()
    clock = {"t": 0.0}
    monkeypatch.setattr(appmod.time, "monotonic", lambda: clock["t"])
    ticks = {"n": 0}

    def predicate():
        ticks["n"] += 1
        # Advance well past one heartbeat interval per poll.
        clock["t"] += appmod.PROGRESS_HEARTBEAT_S + 1
        return ticks["n"] > 3

    async def scenario():
        assert await app._await_until(
            predicate, timeout=1e6, poll=0, ctx=ctx, progress=lambda: "Cranking…"
        )

    _run(scenario())
    assert ctx.texts == ["Cranking…"] * 3


def test_await_until_without_ctx_publishes_nothing():
    app, ctx = _await_app(), _CtxStub()
    done = {"v": False}

    def predicate():
        was, done["v"] = done["v"], True
        return was

    async def scenario():
        assert await app._await_until(predicate, timeout=5, poll=0)

    _run(scenario())
    assert ctx.texts == []


def test_progress_and_ack_failures_are_non_fatal():
    # A device that can't reach the channel must still run the start sequence.
    app, ctx = _await_app(), _CtxStub(fail=True)

    async def scenario():
        await app._acknowledge(ctx)
        await app._command_progress(ctx, "Cranking…")
        assert await app._await_until(
            lambda: True, timeout=1, poll=0, ctx=ctx, progress=lambda: "x"
        )

    _run(scenario())
    assert (ctx.acks, ctx.texts) == (0, [])


def test_acknowledge_marks_the_command_received():
    app, ctx = _await_app(), _CtxStub()
    _run(app._acknowledge(ctx))
    assert ctx.acks == 1


# ---------------------------------------------------------------------------
# App tile status string
# ---------------------------------------------------------------------------


def _display_state(auto_state, panel_display="Stopped", *, modes=1, running=False,
                   active="Warm Up", phase=None):
    """The tile's text. `modes` is how many run modes the install has — the
    active one is named alongside the state only where there is a choice."""
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication
    from dse_engine_controller.run_modes import CONTROL_FIXED, RunMode

    app = SimpleNamespace()
    app.autostart = SimpleNamespace(state=auto_state) if auto_state else None
    app.sequencer = SimpleNamespace(
        modes=[RunMode(f"Mode {i}", f"mode_{i}", CONTROL_FIXED) for i in range(modes)]
    )
    app._active_mode = lambda: RunMode(active, "active", CONTROL_FIXED)
    for name in ("_display_state", "_with_run_mode"):
        setattr(
            app, name, types.MethodType(getattr(DseEngineControllerApplication, name), app)
        )
    return app._display_state(
        SimpleNamespace(
            display_state=panel_display,
            is_running=running,
            is_warming_up=phase == "warming_up",
            is_cooling_down=phase == "cooling_down",
        )
    )


def test_tile_says_starting_through_the_whole_start_sequence():
    """The panel reports stopped for the pre-start warning and the cranking that
    follows, so the tile would otherwise read "Stopped" for a minute after Start."""
    assert _display_state("prestart_warning") == "Starting"
    assert _display_state("starting") == "Starting"
    # Even while the panel reports the fault this app is in the middle of clearing.
    assert _display_state("starting", "Fault: Low Oil Pressure") == "Starting"


def test_the_tile_names_the_run_mode_where_there_is_a_choice(rig_unused=None):
    """"Running" says nothing about which of a site's modes is running — at
    900 rpm warming up, or holding a discharge pressure."""
    assert _display_state(
        "running", "Running", modes=3, running=True, active="Warmup"
    ) == "Running (Warmup)"
    # Not while starting: nothing is running yet for a mode to be a mode of.
    assert _display_state("starting", modes=3, active="Warmup") == "Starting"
    assert _display_state("prestart_warning", modes=3, active="Warmup") == "Starting"
    # One mode is no choice, so the suffix would be noise on every tile.
    assert _display_state("running", "Running", modes=1, running=True) == "Running"
    # A stopped pump's mode is a plan rather than a state, and the panel's own
    # stopped text already carries brackets of its own.
    assert _display_state("idle", "Stopped", modes=3) == "Stopped"


def test_tile_otherwise_shows_the_panel_state():
    assert _display_state("running", "Manual Running") == "Manual Running"
    assert _display_state("idle", "Stopped") == "Stopped"
    assert _display_state("stopping", "Cooling Down") == "Cooling Down"
    assert _display_state("fault", "Fault: Overspeed") == "Fault: Overspeed"
    # Monitor-only install: no auto-start machine at all.
    assert _display_state(None, "Running") == "Running"


# ---------------------------------------------------------------------------
# Getting out of a fault
# ---------------------------------------------------------------------------


def _button_app(auto_state="fault", fault_reason="Engine stopped unexpectedly"):
    """The Start/Stop Now handlers with the state machine and panel stubbed."""
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication

    app = SimpleNamespace()
    app.resets = []
    app.stops = []

    async def reset():
        app.resets.append(True)
        app.autostart.state = "idle"
        app.autostart.fault_reason = ""

    app.autostart = SimpleNamespace(
        state=auto_state,
        fault_reason=fault_reason,
        reset=reset,
        warning_seconds=0,
        start_timeout=5,
        stop_timeout=5,
    )
    app._operator_run_latch = False
    app._suppress_run_tag = False

    async def acknowledge(_ctx):
        return None

    async def await_until(*a, **kw):
        return True

    async def write_stop():
        app.stops.append(True)
        return True

    app._acknowledge = acknowledge
    app._await_until = await_until
    app.write_stop = write_stop
    app._start_progress_text = lambda: ""
    app._stop_progress_text = lambda: ""
    for name in ("on_start_now", "on_stop_now", "_on_start_command_cancelled"):
        setattr(
            app, name, types.MethodType(getattr(DseEngineControllerApplication, name), app)
        )
    return app


def test_start_now_clears_a_fault_and_arms_the_run():
    """Station 1 (2026-08-18): Start Now was pressed four times while the machine
    sat in `fault`, which has no run-request transition — so nothing happened."""
    app = _button_app()
    _run(app.on_start_now(None, None))

    assert app.resets == [True]  # the press is the acknowledgement
    assert app.autostart.state == "idle"  # ...so the sequence can begin
    assert app._operator_run_latch is True
    assert app._suppress_run_tag is False


def test_start_now_from_idle_does_not_reset_anything():
    app = _button_app(auto_state="idle", fault_reason="")
    _run(app.on_start_now(None, None))

    assert app.resets == []
    assert app._operator_run_latch is True


def test_stop_now_commands_the_panel_from_a_fault():
    """The machine sends nothing from `fault`, so clearing the latch alone would
    leave a running engine running."""
    app = _button_app()
    _run(app.on_stop_now(None, None))

    assert app.stops == [True]
    assert app._operator_run_latch is False
    assert app._suppress_run_tag is True
    # The fault stays: stopping doesn't explain what went wrong.
    assert app.autostart.state == "fault"


def test_stop_now_from_running_leaves_the_panel_to_the_state_machine():
    app = _button_app(auto_state="running", fault_reason="")
    _run(app.on_stop_now(None, None))

    assert app.stops == []  # `stopping` is what sends it, after the cooldown
    assert app._operator_run_latch is False


def _restart_probe(*, seconds_since_outage=None):
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication

    app = SimpleNamespace()
    app._last_comms_loss = (
        None if seconds_since_outage is None else 1000.0 - seconds_since_outage
    )
    app._panel_looks_restarted = types.MethodType(
        DseEngineControllerApplication._panel_looks_restarted, app
    )
    return app


def _restart_snap(is_auto=False):
    from types import SimpleNamespace

    return SimpleNamespace(is_auto=is_auto)


def test_panel_restart_detected_after_a_short_outage():
    """The outage before a panel restart is shorter than the comms-loss debounce
    (15s / 3 polls) — which is why the fault path is reached at all.

    Station 1 (2026-08-18): an 8s outage, so `lost_comms` never went true. Keying
    the detection off that flag meant a healthy running engine faulted anyway.
    """
    app = _restart_probe(seconds_since_outage=8.0)
    assert app._panel_looks_restarted(_restart_snap(), raw_running=False, now=1000.0) is True


def test_panel_restart_not_claimed_long_after_an_outage():
    from dse_engine_controller.application import PANEL_RESTART_WINDOW_S

    app = _restart_probe(seconds_since_outage=PANEL_RESTART_WINDOW_S + 1)
    assert app._panel_looks_restarted(_restart_snap(), raw_running=False, now=1000.0) is False


def test_panel_restart_not_claimed_without_an_outage():
    # An engine that trips while comms are healthy is a real fault.
    app = _restart_probe()
    assert app._panel_looks_restarted(_restart_snap(), raw_running=False, now=1000.0) is False


def test_panel_restart_not_claimed_in_auto_or_while_running():
    app = _restart_probe(seconds_since_outage=8.0)
    # A panel that came back in Auto hasn't lost its remote setup.
    assert app._panel_looks_restarted(_restart_snap(is_auto=True), False, 1000.0) is False
    # And a running engine isn't a stopped one.
    assert app._panel_looks_restarted(_restart_snap(), raw_running=True, now=1000.0) is False


# ---------------------------------------------------------------------------
# Pump power output + the no-comms message
# ---------------------------------------------------------------------------


def _power_app(
    *,
    pin=7,
    autostart_state="idle",
    remote_control=True,
    run_request=False,
    panel_running=False,
    contactable=True,
    seen_panel=True,
    power_off_message="Power Off",
    controller_state="no_comms",
    hold_elapsed=0.0,
    start_failed=False,
):
    """An application stub carrying just what the power output reads."""
    import time
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication

    app = SimpleNamespace()
    app.config = SimpleNamespace(
        pump_power_do_pin=SimpleNamespace(value=pin),
        power_off_message=SimpleNamespace(value=power_off_message),
    )
    app.tags = SimpleNamespace(pump_power_on=_TagStub(False))
    app.autostart = (
        SimpleNamespace(
            state=autostart_state,
            is_active=autostart_state
            in ("prestart_warning", "starting", "running", "stopping"),
            start_failed=(autostart_state == "fault" and start_failed),
        )
        if remote_control
        else None
    )
    snap = SimpleNamespace(is_running=panel_running)
    app.store = SimpleNamespace(
        latest=snap,
        is_contactable=contactable,
        snapshots=[snap] if seen_panel else [],
    )
    app.controller_state = SimpleNamespace(state=controller_state)
    app._run_request_active = run_request
    app._pump_power_on = None
    app._last_pump_power_out = None
    app._power_startup_hold = True
    app._started_at = time.monotonic() - hold_elapsed
    app._power_on_since = None
    app.written = []

    async def set_do(pin, state):
        app.written.append((pin, state))

    app.platform_iface = SimpleNamespace(set_do=set_do)
    for name in (
        "_drive_pump_power", "_wants_pump_power", "_power_hold_active",
        "_set_output_pin", "_no_comms_display", "_holding_panel_power",
        "_panel_powering_up", "_engine_may_be_turning",
    ):
        setattr(
            app, name, types.MethodType(getattr(DseEngineControllerApplication, name), app)
        )
    return app


def test_pump_power_config_elements():
    from dse_engine_controller.app_config import DseEngineControllerConfig

    # Optional: an install whose panel is always powered leaves it empty.
    assert DseEngineControllerConfig.pump_power_do_pin.default is None
    assert DseEngineControllerConfig.power_off_message.default == "Power Off"


def test_pump_power_off_when_idle_and_nothing_wants_the_pump():
    app = _power_app()
    _run(app._drive_pump_power())

    assert app.written == [(7, False)]
    assert app.tags.pump_power_on.get() is False


def test_pump_power_on_for_a_run_request_before_the_panel_is_reachable():
    # The point of the pin: the panel is dark, so there are no comms to start
    # through — power it up and let the state machine pick it up when it boots.
    app = _power_app(run_request=True, contactable=False, seen_panel=False)
    _run(app._drive_pump_power())

    assert app.written == [(7, True)]
    assert app.tags.pump_power_on.get() is True


def test_pump_power_held_on_through_the_whole_sequence():
    for state in ("prestart_warning", "starting", "running", "stopping"):
        app = _power_app(autostart_state=state)
        _run(app._drive_pump_power())
        assert app.written == [(7, True)], state


def test_pump_power_held_on_for_an_engine_someone_started_by_hand():
    # Powered up at the panel and started manually: the app must not switch the
    # power out from under it just because it has no run demand of its own.
    app = _power_app(panel_running=True)
    assert app._wants_pump_power() is True


def test_pump_power_released_once_the_run_demand_clears():
    app = _power_app(autostart_state="running", run_request=True)
    _run(app._drive_pump_power())
    assert app.written == [(7, True)]

    # Operator pressed Stop, cooldown finished, panel reports stopped.
    app.autostart.state, app.autostart.is_active = "idle", False
    app._run_request_active = False
    _run(app._drive_pump_power())
    assert app.written == [(7, True), (7, False)]


def test_pump_power_stays_on_through_a_latched_fault():
    # A fault keeps the operator's run demand, so the panel stays powered for
    # them to look at rather than going dark with the fault on it.
    app = _power_app(autostart_state="fault", run_request=True)
    assert app._wants_pump_power() is True


def test_pump_power_untouched_without_a_pin():
    app = _power_app(pin=None, run_request=True)
    _run(app._drive_pump_power())
    assert app.written == []


def test_pump_power_held_on_in_monitor_only_mode():
    # No remote control: the app never runs the pump, but it still needs the
    # panel awake to read it.
    app = _power_app(remote_control=False)
    assert app._wants_pump_power() is True


def test_pump_power_left_alone_until_the_panel_has_been_seen():
    # A restart that lands mid-run can't read the output back, and computing it
    # from an empty picture would cut power to a pump it can't see yet.
    app = _power_app(contactable=False, seen_panel=False)
    assert app._wants_pump_power() is None
    _run(app._drive_pump_power())
    assert app.written == []


def test_pump_power_hold_lifts_once_the_panel_answers():
    app = _power_app(contactable=False, seen_panel=True)  # answered, now quiet
    _run(app._drive_pump_power())
    assert app.written == [(7, False)]
    assert app._power_startup_hold is False


def test_pump_power_hold_expires_if_the_panel_never_answers():
    from dse_engine_controller.application import POWER_STARTUP_HOLD_S

    app = _power_app(
        contactable=False, seen_panel=False, hold_elapsed=POWER_STARTUP_HOLD_S + 1
    )
    _run(app._drive_pump_power())
    assert app.written == [(7, False)]


def test_no_comms_says_power_off_by_default():
    app = _power_app(pin=None, controller_state="initialising")
    assert app._no_comms_display() == "Power Off"
    app.controller_state.state = "no_comms"
    assert app._no_comms_display() == "Power Off"


def test_no_comms_message_is_configurable():
    app = _power_app(power_off_message="Panel asleep")
    assert app._no_comms_display() == "Panel asleep"


def test_no_comms_says_no_comms_while_we_are_powering_the_panel():
    # We're holding its power on and still can't reach it — that's a real fault,
    # not a panel someone switched off.
    app = _power_app(autostart_state="running", panel_running=True, contactable=False)
    _run(app._drive_pump_power())
    assert app._no_comms_display() == "no comms"


def test_no_comms_says_starting_while_the_panel_is_still_booting():
    # Start pressed on a dark panel: the machine can't leave idle until comms
    # arrive, so without this the tile reads "no comms" for the whole boot.
    app = _power_app(run_request=True, contactable=False, seen_panel=False)
    _run(app._drive_pump_power())
    assert app.written == [(7, True)]
    assert app._no_comms_display() == "Starting"


def test_no_comms_says_starting_while_the_panel_boots():
    # Powering up (or rebooting out of a Force-Reset) mid-start: unreachable on
    # purpose, and the tile already says "Starting" either side of it.
    app = _power_app(autostart_state="prestart_warning", contactable=False)
    assert app._no_comms_display() == "Starting"


def test_blank_no_comms_message_falls_back_to_the_original_wording():
    app = _power_app(pin=None, power_off_message="", controller_state="initialising")
    assert app._no_comms_display() == "connecting…"
    app.controller_state.state = "no_comms"
    assert app._no_comms_display() == "no comms"


def test_pump_power_can_be_released_again_during_the_startup_hold():
    """Start then Stop, both inside the startup hold, on a panel that never
    answered. The hold exists because a DO can't be read back — once we've
    written the pin ourselves that no longer applies, and holding on to it would
    leave the panel powered for the rest of the hold after an explicit stop."""
    app = _power_app(run_request=True, contactable=False, seen_panel=False)
    _run(app._drive_pump_power())
    assert app.written == [(7, True)]

    app._run_request_active = False  # operator pressed Stop / Cancel
    _run(app._drive_pump_power())
    assert app.written == [(7, True), (7, False)]


def test_no_comms_never_says_power_off_while_a_run_is_believed_under_way():
    # No power pin at all, engine running, RS485 falls out: "Power Off" beside a
    # running engine would be a lie. A fault out of a run is the same — the
    # engine may still be turning.
    for state in ("running", "stopping", "fault"):
        app = _power_app(pin=None, autostart_state=state, contactable=False)
        assert app._no_comms_display() == "no comms", state


def test_no_comms_says_power_off_after_a_start_gave_up():
    """The start timed out on a panel that never answered. Nothing ever ran, so
    the silence means what silence usually means — and repeating "no comms" adds
    nothing to the fault already on the dashboard."""
    app = _power_app(
        pin=None, autostart_state="fault", start_failed=True, contactable=False
    )
    assert app._no_comms_display() == "Power Off"


def test_pump_power_released_once_a_start_has_given_up():
    """The run latch outlives the fault — it is what a later Start press clears —
    so without this the panel is powered indefinitely in front of a pump that
    isn't going anywhere."""
    app = _power_app(
        autostart_state="fault", start_failed=True, run_request=True,
        contactable=False,
    )
    assert app._wants_pump_power() is False
    _run(app._drive_pump_power())
    assert app.written == [(7, False)]
    # ...and the tile follows the power.
    assert app._no_comms_display() == "Power Off"


def test_pump_power_kept_if_the_engine_turns_out_to_be_running():
    # "Start failed" but the panel says it is running after all — recover_running
    # is about to pick it up, so don't cut its power on the way.
    app = _power_app(
        autostart_state="fault", start_failed=True, run_request=True,
        panel_running=True,
    )
    assert app._wants_pump_power() is True


def test_pump_power_kept_through_a_fault_out_of_a_run():
    # Engine stopped unexpectedly / lost comms while running: it may still be
    # turning, and the operator needs the panel up to see why.
    app = _power_app(autostart_state="fault", run_request=True, contactable=False)
    assert app._wants_pump_power() is True


def test_no_comms_stops_saying_starting_once_the_boot_window_passes():
    # A panel that never comes up shouldn't leave the tile claiming "Starting"
    # forever while the Auto Start State beside it reads "Ready".
    import time

    from dse_engine_controller.application import PANEL_POWER_UP_S

    app = _power_app(run_request=True, contactable=False, seen_panel=False)
    _run(app._drive_pump_power())
    assert app._no_comms_display() == "Starting"

    app._power_on_since = time.monotonic() - (PANEL_POWER_UP_S + 1)
    assert app._no_comms_display() == "no comms"


# ---------------------------------------------------------------------------
# Announcements dodging the horn
# ---------------------------------------------------------------------------


def _announce_app(*, horn_pin=3, episode_start=None, warning_remaining=25.0):
    """An app stub carrying just what the announcement timing reads.

    `episode_start` is the wall-clock instant the horn's warning episode began,
    the same value _coordinated_output shares between pumps on that pin.
    """
    import time
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication

    now = time.time()
    app = SimpleNamespace()
    app.config = SimpleNamespace(
        horn_do_pin=SimpleNamespace(value=horn_pin),
        enable_announcements=SimpleNamespace(value=True),
        prestart_announcement=SimpleNamespace(value="{name} starting shortly."),
        start_announcement=SimpleNamespace(value="{name} starting now."),
        start_failed_announcement=SimpleNamespace(value="{name} startup failed."),
    )
    app._pending_announcement = None
    app.spoken = []
    app.globals = {}
    if episode_start is not None and horn_pin is not None:
        pin = int(horn_pin)
        app.globals[f"kensho_horn_warning_start_p{pin}"] = episode_start
        app.globals[f"kensho_horn_warning_until_p{pin}"] = now + 5

    def get_global_tag(key, default=None):
        return app.globals.get(key, default)

    app.get_global_tag = get_global_tag

    async def speak(template, *, expires_ms):
        app.spoken.append((template, expires_ms))

    app._speak_announcement = speak
    app._last_prestart_spoken = 0.0
    app.autostart = SimpleNamespace(
        state="prestart_warning", warning_remaining=warning_remaining,
        start_failed=False,
    )
    for name in (
        "_arm_announcement", "_speak_pending", "_horn_quiet_for",
        "_announce_state_change",
    ):
        setattr(
            app, name, types.MethodType(getattr(DseEngineControllerApplication, name), app)
        )
    return app


def test_horn_quiet_for_reports_the_gap():
    import math
    import time

    from dse_engine_controller.application import HORN_CYCLE_S

    now = time.time()
    # Blast just started → no room at all.
    app = _announce_app(episode_start=now)
    assert app._horn_quiet_for(now) == 0.0
    # 4s in: 3s of blast done, 9s of the 10s silence left.
    app = _announce_app(episode_start=now - 4)
    assert app._horn_quiet_for(now) == HORN_CYCLE_S - 4
    # No horn wired — the tag still pulses but nothing makes a noise.
    app = _announce_app(horn_pin=None, episode_start=now)
    assert app._horn_quiet_for(now) == math.inf
    # No warning episode running on the pin.
    app = _announce_app(episode_start=None)
    assert app._horn_quiet_for(now) == math.inf


def test_announcement_waits_for_the_horn_to_stop():
    """The collision this exists for: the horn is phased to blast the instant the
    warning starts, which is exactly when the opening announcement is due."""
    import time

    app = _announce_app(episode_start=time.time())  # blast under way
    app._arm_announcement(
        app.config.prestart_announcement.value, prestart=True, hold_for=20.0
    )
    _run(app._speak_pending(app.autostart))
    assert app.spoken == []  # held, not lost
    assert app._pending_announcement is not None

    # Four seconds later the blast is over and there is room to speak.
    app.globals["kensho_horn_warning_start_p3"] = time.time() - 4
    _run(app._speak_pending(app.autostart))
    assert len(app.spoken) == 1
    assert app._pending_announcement is None


def test_announcement_not_delayed_when_no_horn_is_wired():
    app = _announce_app(horn_pin=None)
    app._arm_announcement(
        app.config.prestart_announcement.value, prestart=True, hold_for=20.0
    )
    _run(app._speak_pending(app.autostart))
    assert len(app.spoken) == 1


def test_announcement_waits_out_the_tail_of_the_silence():
    """Speaking with a second of silence left just feeds the phrase to the next
    blast — wait for the following gap instead."""
    import time

    from dse_engine_controller.application import HORN_CYCLE_S

    app = _announce_app(episode_start=time.time() - (HORN_CYCLE_S - 1))
    app._arm_announcement(
        app.config.prestart_announcement.value, prestart=True, hold_for=20.0
    )
    _run(app._speak_pending(app.autostart))
    assert app.spoken == []


def test_announcement_goes_out_over_the_horn_past_its_deadline():
    import time

    app = _announce_app(episode_start=time.time())  # blast under way
    app._arm_announcement(
        app.config.prestart_announcement.value, prestart=True, hold_for=0.0
    )
    _run(app._speak_pending(app.autostart))
    assert len(app.spoken) == 1  # late and audible beats never


def test_pending_prestart_phrase_dropped_once_the_warning_ends():
    import time

    app = _announce_app(episode_start=time.time())
    app._arm_announcement(
        app.config.prestart_announcement.value, prestart=True, hold_for=99.0
    )
    app.autostart.state = "starting"  # warning over while it waited its turn
    _run(app._speak_pending(app.autostart))
    assert app.spoken == []  # it was about a moment that has passed
    assert app._pending_announcement is None


def test_start_announcement_survives_the_end_of_the_warning():
    import time

    from dse_engine_controller.application import ANNOUNCEMENT_EXPIRY_S

    app = _announce_app(episode_start=time.time() - 4)
    app.autostart.state = "starting"  # engine cranking
    app._arm_announcement(
        app.config.start_announcement.value, prestart=False, hold_for=3.0
    )
    _run(app._speak_pending(app.autostart))
    assert app.spoken == [
        ("{name} starting now.", int(ANNOUNCEMENT_EXPIRY_S * 1000))
    ]


def test_speaking_a_prestart_phrase_paces_the_next_repeat():
    import time

    app = _announce_app(episode_start=time.time() - 4)
    app._arm_announcement(
        app.config.prestart_announcement.value, prestart=True, hold_for=20.0
    )
    _run(app._speak_pending(app.autostart))
    # The repeat clock runs from the utterance, not from the loop that queued it,
    # so holding out for a gap in the horn doesn't shorten the next gap.
    assert app._last_prestart_spoken > 0.0


def test_nothing_queued_when_announcements_are_disabled():
    app = _announce_app(horn_pin=None)
    app.config.enable_announcements.value = False
    app._arm_announcement("{name} starting.", prestart=False, hold_for=0.0)
    assert app._pending_announcement is None


def test_panel_ready_mirrors_the_start_gate():
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication

    ready = types.MethodType(
        DseEngineControllerApplication._panel_ready_to_start, SimpleNamespace()
    )
    assert ready(SimpleNamespace(is_auto=True, is_faulted=False)) is True
    # Mid Force-Reset the last thing the panel said was "Manual" — a stale
    # snapshot is used as-is, which is exactly when the warning must keep going.
    assert ready(SimpleNamespace(is_auto=False, is_faulted=False)) is False
    assert ready(SimpleNamespace(is_auto=True, is_faulted=True)) is False
    # Never heard from is the least ready thing there is — writing STARTs blind
    # at a bus with nobody on it, with the warning already over, is what this
    # used to do.
    assert ready(None) is False


def test_legacy_countdown_template_is_reworded():
    from dse_engine_controller.application import _drop_countdown

    # The old default, and the shapes around it.
    assert _drop_countdown("{name} starting in {seconds} seconds.") == (
        "{name} starting shortly."
    )
    assert _drop_countdown("{name} starting in {seconds}s.") == "{name} starting shortly."
    assert _drop_countdown("Starting in {seconds} SECONDS") == "Starting shortly"
    # A placeholder used some other way still loses the figure rather than
    # speaking a literal brace.
    assert _drop_countdown("{seconds} to go") == "shortly to go"
    # Templates without one are untouched.
    assert _drop_countdown("{name} starting shortly.") == "{name} starting shortly."


def test_start_budget_config_problems_are_flagged(caplog):
    import logging
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication

    def _app(warning, timeout):
        app = SimpleNamespace()
        app.autostart = SimpleNamespace(
            warning_seconds=float(warning), start_timeout=float(timeout)
        )
        app._check_start_budget = types.MethodType(
            DseEngineControllerApplication._check_start_budget, app
        )
        return app

    # A budget the countdown alone exhausts can never start an engine.
    with caplog.at_level(logging.WARNING):
        _app(30, 30)._check_start_budget()
    assert "will fault the moment it ends" in caplog.text

    # Long enough for the countdown, too short for the panel handshake that has
    # been running alongside it.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _app(10, 12)._check_start_budget()
    assert "reach Auto" in caplog.text

    # The shipped defaults are quiet: the handshake runs during the countdown,
    # so 60s is measured from the press and not 30s + a handshake.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _app(30, 60)._check_start_budget()
    assert caplog.text == ""

    # Monitor-only has no autostart at all.
    quiet = _app(30, 30)
    quiet.autostart = None
    quiet._check_start_budget()


def test_prestart_announcement_default_has_no_countdown():
    from dse_engine_controller.app_config import DseEngineControllerConfig

    assert "{seconds}" not in DseEngineControllerConfig.prestart_announcement.default


def _at_state(app, state, *, start_failed=False):
    """Run the transition-announcement decision for a move into `state`."""
    from types import SimpleNamespace

    app.autostart = SimpleNamespace(
        state=state,
        warning_remaining=None,
        start_failed=(state == "fault" and start_failed),
    )
    app._announce_state_change(app.autostart)
    return app._pending_announcement


def test_giving_up_on_a_start_announces_it():
    """From a distance a start that gave up sounds exactly like one that worked:
    the horn stops either way. Say which it was."""
    for reason in ("panel never ready", "engine never ran"):
        app = _announce_app(horn_pin=None)
        queued = _at_state(app, "fault", start_failed=True)
        assert queued is not None, reason
        assert queued.template == "{name} startup failed."
        assert queued.prestart is False  # survives the state it was queued in
        _run(app._speak_pending(app.autostart))
        assert app.spoken[0][0] == "{name} startup failed."


def test_failed_start_announcement_replaces_the_queued_warning_phrase():
    # The repeat that was waiting for a gap in the horn is no longer true.
    import time

    app = _announce_app(episode_start=time.time())
    app._arm_announcement(
        app.config.prestart_announcement.value, prestart=True, hold_for=99.0
    )
    queued = _at_state(app, "fault", start_failed=True)
    assert queued.template == "{name} startup failed."


def test_faults_that_are_not_failed_starts_stay_quiet():
    # Engine stopped unexpectedly / panel fault / lost comms while running, and a
    # stop that timed out. The engine going quiet is its own announcement.
    app = _announce_app(horn_pin=None)
    assert _at_state(app, "fault", start_failed=False) is None
    # So is an operator cancelling their own start.
    app = _announce_app(horn_pin=None)
    assert _at_state(app, "idle") is None


def test_failed_start_announcement_waits_for_the_horn_too():
    # The cross-pump hold margin can leave the horn finishing a blast as the
    # fault lands.
    import time

    app = _announce_app(episode_start=time.time())  # blast under way
    _at_state(app, "fault", start_failed=True)
    _run(app._speak_pending(app.autostart))
    assert app.spoken == []

    app.globals["kensho_horn_warning_start_p3"] = time.time() - 4
    _run(app._speak_pending(app.autostart))
    assert app.spoken[0][0] == "{name} startup failed."


def test_start_failed_announcement_can_be_turned_off():
    app = _announce_app(horn_pin=None)
    app.config.start_failed_announcement.value = ""
    assert _at_state(app, "fault", start_failed=True) is None


def test_panel_prep_runs_during_the_warning_not_after_it():
    """The Force-Reset reboots the panel for ~30s. Deferring the handshake until
    the countdown finished would bolt that reboot onto the end of every start
    from Manual; only the START itself waits for the countdown."""
    import types
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication

    def _app(state):
        app = SimpleNamespace(calls=[])

        async def record(name):
            async def fn(*a):
                app.calls.append(name)

            return fn

        for name in ("_prep_panel", "_maybe_send_start", "_write_running_rpm",
                     "_maybe_send_stop"):
            def make(n):
                async def fn(*a):
                    app.calls.append(n)
                return fn
            setattr(app, name, make(name))
        app._drive_panel_commands = types.MethodType(
            DseEngineControllerApplication._drive_panel_commands, app
        )
        _run(app._drive_panel_commands(SimpleNamespace(state=state), object()))
        return app.calls

    # The whole warning, countdown included: prep runs, START does not.
    assert _app("prestart_warning") == ["_prep_panel"]
    # Once the countdown is done, both.
    assert _app("starting") == ["_prep_panel", "_maybe_send_start"]
    # And nothing panel-side leaks into the other states.
    assert _app("running") == ["_write_running_rpm"]
    assert _app("stopping") == ["_maybe_send_stop"]
    assert _app("idle") == []
    assert _app("fault") == []


def test_a_mode_configured_outside_the_rpm_band_is_clamped_and_says_so(caplog):
    """Running 150 rpm from where the config says is not something to discover
    from a flow meter."""
    import logging

    from dse_engine_controller.run_modes import CONTROL_FIXED, RunMode

    cmds = _UIManagerStub()
    app = _seed_app(
        cmds, [RunMode("Warm Up", "warm_up", CONTROL_FIXED, target_rpm=400)]
    )
    with caplog.at_level(logging.WARNING):
        _run(app._seed_run_mode())
    assert cmds.values["mode_warm_up_target_rpm"] == 950
    assert "outside the Target RPM band" in caplog.text


# ---------------------------------------------------------------------------
# Run modes: the wiring between the sequencer and the loop
# ---------------------------------------------------------------------------


def _mode_runtime_app(modes, transitions=(), *, active=None, entered_at=0.0,
                      readings=None, snap=None, contactable=True):
    """An app stub carrying just what the run-mode wiring reads."""
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App
    from dse_engine_controller.run_modes import ModeSequencer

    app = SimpleNamespace()
    app.sequencer = ModeSequencer(list(modes), list(transitions))
    app._run_mode_problems = []
    app._selected_mode = None
    app.app_display_name = "Pump 1"
    # Stopped unless a test says otherwise: transitions are watched for a running
    # engine and nothing else.
    app.autostart = SimpleNamespace(state="idle")
    app.tags = SimpleNamespace(
        run_mode_active=_TagStub(""),
        run_mode_variable=_TagStub(""),
        run_mode_status=_TagStub(""),
        run_mode_status_hidden=_TagStub(False),
        run_mode_entered_at=_TagStub(entered_at),
        run_mode_hidden=_TagStub(False),
        regulate_plot_hidden=_TagStub(True),
        **{
            m.element_name("hidden"): _TagStub(True) for m in app.sequencer.modes
        },
    )
    app._manual_while_running = False
    app._regulate_key = "carried over"
    app.messages = []
    app.ui_manager = _UIManagerStub()
    app.ui_manager.values["run_mode"] = active or app.sequencer.fallback_mode.sanitised
    app._sensor_by_name = {s.sanitised: s for s in (readings or {})}
    app._analog = dict(readings or {})
    app.store = SimpleNamespace(latest=snap, is_contactable=contactable)

    async def create_message(channel, payload):
        app.messages.append((channel, payload))

    app.create_message = create_message
    for name in (
        "_active_mode", "_select_run_mode", "_evaluate_run_mode",
        "_transition_crossed", "_panel_flag", "_publish_run_mode",
        "_command_value", "_set_command_value", "_apply_rpm_mode_visibility",
        "_tuned_transitions",
    ):
        app.__dict__[name] = types_method(getattr(App, name), app)
    return app


def _fixed(label, sanitised, rpm=1000):
    from dse_engine_controller.run_modes import CONTROL_FIXED, RunMode

    return RunMode(label, sanitised, CONTROL_FIXED, target_rpm=rpm)


def _regulate(label, sanitised, variable, variable_label=""):
    from dse_engine_controller.run_modes import CONTROL_REGULATE, RunMode

    return RunMode(
        label, sanitised, CONTROL_REGULATE,
        variable=variable, variable_label=variable_label or variable,
    )


def test_the_active_modes_regulate_variable_is_published():
    """Which sensor a Regulate mode holds lives in config, per mode, so nothing
    else published says it — and a display driving off these tags has to know
    in order to put the setpoint against the right reading rather than against
    engine speed."""
    modes = [
        _fixed("Warmup", "warmup", 900),
        _regulate("Regulate Output", "regulate_output", "discharge_pressure"),
    ]
    app = _mode_runtime_app(modes, active="regulate_output")

    _run(app._publish_run_mode())
    assert app.tags.run_mode_variable.get() == "discharge_pressure"

    # A Fixed mode holds no variable, and must clear the one before it rather
    # than leaving the setpoint hanging off the previous mode's sensor.
    app.ui_manager.values["run_mode"] = "warmup"
    _run(app._publish_run_mode())
    assert app.tags.run_mode_variable.get() == ""


def test_a_misconfigured_mode_still_names_its_variable():
    """The problems bail-out returns early, and a display that stopped being
    told the variable would keep marking the last mode's sensor."""
    modes = [_regulate("Regulate Output", "regulate_output", "discharge_pressure")]
    app = _mode_runtime_app(modes, active="regulate_output")
    app._run_mode_problems = ["Regulate Output: no sensor called Discharge Pressure"]

    _run(app._publish_run_mode())

    assert app.tags.run_mode_variable.get() == "discharge_pressure"
    assert app.tags.run_mode_status.get().startswith("Regulate Output:")


def _timer_to(to, frm="", seconds=30.0, index=0):
    from dse_engine_controller.run_modes import WHEN_TIMER, RunTransition

    return RunTransition(
        index=index, to_mode=to, to_label=to.replace("_", " ").title(),
        when=WHEN_TIMER, from_mode=frm, from_label=frm, after_seconds=seconds,
    )


def test_a_transition_moves_the_mode_and_restarts_everything_keyed_to_it():
    import time

    modes = [_fixed("Warm Up", "warm_up", 800), _fixed("Line Fill", "line_fill", 1300)]
    app = _mode_runtime_app(
        modes, [_timer_to("line_fill", "warm_up", seconds=30.0)],
        entered_at=time.time() - 31,
    )
    _run(app._evaluate_run_mode())

    assert app.ui_manager.values["run_mode"] == "line_fill"
    # The clock restarts, so the next transition's timer starts from here...
    assert app.tags.run_mode_entered_at.get() > time.time() - 1
    # ...and the regulate loop re-seeds rather than unwinding the last mode's
    # integrator through the first seconds of this one.
    assert app._regulate_key is None
    # Only the new mode's controls are on screen.
    assert app.tags.mode_line_fill_hidden.get() is False
    assert app.tags.mode_warm_up_hidden.get() is True
    assert app.messages and "Line Fill" in app.messages[0][1]["message"]


def test_the_mode_holds_while_no_transition_is_met():
    import time

    modes = [_fixed("Warm Up", "warm_up"), _fixed("Line Fill", "line_fill")]
    app = _mode_runtime_app(
        modes, [_timer_to("line_fill", "warm_up", seconds=30.0)],
        entered_at=time.time() - 5,
    )
    _run(app._evaluate_run_mode())
    assert app.ui_manager.values["run_mode"] == "warm_up"
    assert app.messages == []


def test_the_status_line_says_what_would_move_the_mode_on():
    modes = [_fixed("Warm Up", "warm_up"), _fixed("Line Fill", "line_fill")]
    app = _mode_runtime_app(modes, [_timer_to("line_fill", "warm_up", seconds=30.0)])
    _run(app._publish_run_mode())
    assert app.tags.run_mode_active.get() == "Warm Up"
    assert app.tags.run_mode_status.get() == "Warm Up → Line Fill after 30s"


def test_a_terminal_mode_says_so():
    app = _mode_runtime_app([_fixed("Run", "run")])
    _run(app._publish_run_mode())
    assert "no automatic change from here" in app.tags.run_mode_status.get()


def test_config_problems_take_over_the_status_line():
    """A misconfigured mode has to say so where the operator is looking, not only
    in a log line nobody reads until something has gone wrong."""
    app = _mode_runtime_app([_fixed("Run", "run")])
    app._run_mode_problems = ["Run mode 'Run' regulates but names no variable."]
    _run(app._publish_run_mode())
    assert "names no variable" in app.tags.run_mode_status.get()


def test_transition_crossed_answers_from_the_live_reading():
    sensor = _slot("level", 500.0, analog=True, pin=2, sp_max=1600.0,
                   value_max=1600.0)
    app = _mode_runtime_app([_fixed("Run", "run")])
    app._sensor_by_name = {"level": sensor}
    app._analog = {}
    assert app._transition_crossed("level", 400.0, "Above") is True
    assert app._transition_crossed("level", 600.0, "Above") is False
    assert app._transition_crossed("level", 600.0, "Below") is True


def test_transition_crossed_says_it_cannot_tell_rather_than_guessing():
    """A dead sensor must not read as 'condition not met' — the sequencer treats
    'can't tell' as a reason to hold, and as grounds to drop any partial hold."""
    sensor = _slot("level", None, analog=True, pin=2)
    app = _mode_runtime_app([_fixed("Run", "run")])
    app._sensor_by_name = {"level": sensor}
    # No reading at all.
    assert app._transition_crossed("level", 400.0, "Above") is None
    # No such sensor, and no setpoint.
    assert app._transition_crossed("nope", 400.0, "Above") is None
    assert app._transition_crossed("level", None, "Above") is None
    # A faulted analog loop, even though the tag still holds its last value.
    sensor.tag = _TagStub(500.0)
    app._analog = {"level": _FaultyReading()}
    assert app._transition_crossed("level", 400.0, "Above") is None


def test_panel_flag_reads_the_snapshot_and_admits_when_it_cannot():
    from types import SimpleNamespace

    snap = SimpleNamespace(is_warming_up=True, has_warning=False)
    app = _mode_runtime_app([_fixed("Run", "run")], snap=snap)
    assert app._panel_flag("warming_up") is True
    assert app._panel_flag("warning") is False
    assert app._panel_flag("not_a_flag") is None
    # No panel data, or comms down: unanswerable rather than False.
    assert _mode_runtime_app([_fixed("Run", "run")])._panel_flag("warming_up") is None
    assert _mode_runtime_app(
        [_fixed("Run", "run")], snap=snap, contactable=False
    )._panel_flag("warming_up") is None


def test_picking_a_mode_by_hand_goes_through_the_same_path_as_a_transition():
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App

    modes = [_fixed("Warm Up", "warm_up"), _fixed("Line Fill", "line_fill")]
    app = _mode_runtime_app(modes)
    _run(App.on_run_mode(app, SimpleNamespace(), "Line Fill"))

    assert app.ui_manager.values["run_mode"] == "line_fill"
    assert app.tags.mode_line_fill_hidden.get() is False


def test_reselecting_the_active_mode_changes_nothing():
    # Otherwise a blank From Mode re-entering its own target would reset
    # time-in-mode every loop, and every timer downstream depends on it.
    app = _mode_runtime_app([_fixed("Run", "run")], entered_at=1234.0)
    _run(app._select_run_mode(app.sequencer.fallback_mode, reason="test"))
    assert app.tags.run_mode_entered_at.get() == 1234.0
    assert app.messages == []


def test_a_transition_sensor_counts_as_a_controlling_sensor():
    """A dead reading that should have ended warm-up leaves the pump at warm-up
    RPM indefinitely — the same category as the Stop trigger, which is already
    included, and unlike the Start trigger, which can only fail safe."""
    from dse_engine_controller.application import DseEngineControllerApplication
    from dse_engine_controller.run_modes import WHEN_SENSOR, RunTransition

    level = _slot("level", 500.0, analog=True, pin=2)
    app = _auto_app(
        sensors={"level": level},
        transitions=[
            RunTransition(
                index=0, to_mode="line_fill", to_label="Line Fill",
                when=WHEN_SENSOR, from_mode="fixed", from_label="Fixed",
                variable="level", variable_label="level",
                direction="Above", setpoint=200.0,
            )
        ],
    )
    assert DseEngineControllerApplication._controlling_sensors(app) == [level]


def test_a_transition_reports_why_it_moved_the_mode():
    """Without this the operator sees the pump change behaviour with no cause —
    the activity log just named the new mode."""
    import time

    modes = [_fixed("Warm Up", "warm_up", 800), _fixed("Line Fill", "line_fill", 1300)]
    app = _mode_runtime_app(
        modes, [_timer_to("line_fill", "warm_up", seconds=30.0)],
        entered_at=time.time() - 31,
    )
    _run(app._evaluate_run_mode())
    assert app.messages[0][1]["message"] == "Pump 1: run mode Line Fill (after 30s)"


def test_an_operator_pick_says_it_was_an_operator():
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App

    modes = [_fixed("Warm Up", "warm_up"), _fixed("Line Fill", "line_fill")]
    app = _mode_runtime_app(modes)
    _run(App.on_run_mode(app, SimpleNamespace(), "Line Fill"))
    assert app.messages[0][1]["message"].endswith("(operator)")


def test_only_the_active_modes_controls_are_on_screen():
    """Warrego (2026-08-27): two Fixed modes put two Target RPM sliders on the
    dashboard at once. The per-mode visibility flags are created at runtime, so
    they exist only on the tags *instance* — looking them up on the class yields
    None, which binds each panel to no flag and shows every mode's controls."""
    config = _FakeConfig(
        [],
        run_modes=[
            _FakeModeSlot("Warmup", "Fixed", target_rpm=900),
            _FakeModeSlot("Line Fill", "Fixed", target_rpm=1300),
        ],
    )
    ui = _built_ui(config)

    # Each panel emits a binding to its own flag. Asserting the binding rather
    # than the element's existence is the point: the broken version built both
    # panels correctly and simply wired neither of them to anything.
    for mode in ("warmup", "line_fill"):
        emitted = ui._find_element(f"mode_{mode}_panel").to_dict()["hidden"]
        assert emitted.startswith(f"$tag.app().mode_{mode}_hidden"), emitted

    # And each Fixed mode keeps its own Target RPM, so hiding is what stops the
    # dashboard showing two identically-labelled sliders.
    assert (
        ui._find_element("mode_warmup_target_rpm").to_dict()["currentValue"]
        == "$cmds.app().mode_warmup_target_rpm"
    )


def test_each_fixed_modes_slider_is_named_for_its_mode():
    """Only the active mode's panel is on screen, so two sliders are never side
    by side — but the label saying which mode you are editing is the difference
    between confidence and guessing."""
    ui = _built_ui(
        _FakeConfig(
            [],
            run_modes=[
                _FakeModeSlot("Warmup", "Fixed", target_rpm=900),
                _FakeModeSlot("Line Fill", "Fixed", target_rpm=1300),
            ],
        )
    )
    assert ui._find_element("mode_warmup_target_rpm").display_name == "Warmup RPM"
    assert ui._find_element("mode_line_fill_target_rpm").display_name == (
        "Line Fill RPM"
    )


def test_the_regulate_plot_is_bound_to_its_own_visibility_flag():
    ui = _built_ui(_FakeConfig([]))
    emitted = ui._find_element("regulate_plot_group").to_dict()["hidden"]
    assert emitted.startswith("$tag.app().regulate_plot_hidden")


def test_the_regulate_plot_shows_only_while_a_regulate_mode_runs():
    """It is the loop's own history, so it has nothing to show from a Fixed mode
    — or from Manual, where the panel owns engine speed and the loop is idle."""
    from dse_engine_controller.application import DseEngineControllerApplication

    apply = DseEngineControllerApplication._apply_rpm_mode_visibility

    async def scenario():
        fixed = _visibility_app("fixed")
        await apply(fixed)
        assert fixed.tags.regulate_plot_hidden.get() is True

        regulating = _visibility_app("run")
        await apply(regulating)
        assert regulating.tags.regulate_plot_hidden.get() is False

        manual = _visibility_app("run", in_manual=True)
        await apply(manual)
        assert manual.tags.regulate_plot_hidden.get() is True

    _run(scenario())


class _LaggingUIManagerStub(_UIManagerStub):
    """A ui_cmds manager that behaves like the real one: set_value posts the
    update and does *not* refresh the local copy, so a read straight after a
    write still returns the previous value until the aggregate echoes back."""

    def __init__(self):
        super().__init__()
        self.sent = {}

    async def set_value(self, key, value, log_update=True):
        self.sent[key] = value  # in flight; self.values is deliberately untouched

    def echo(self):
        """Simulate the aggregate coming back from the platform."""
        self.values.update(self.sent)


def test_selecting_a_mode_publishes_the_mode_that_was_picked():
    """Warrego (2026-08-27): Mode Status lagged a change behind, inconsistently.
    _select_run_mode wrote the selection and then re-read it, but the ui_cmds
    manager only refreshes its local copy when the aggregate echoes back."""
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App

    modes = [_fixed("Warmup", "warmup", 900), _fixed("Line Fill", "line_fill", 1300)]
    app = _mode_runtime_app(modes)
    app.ui_manager = _LaggingUIManagerStub()
    app.ui_manager.values["run_mode"] = "warmup"

    _run(App.on_run_mode(app, SimpleNamespace(), "Line Fill"))

    # Published from the mode that was picked, not from the stale element.
    assert app.tags.run_mode_active.get() == "Line Fill"
    assert app.tags.run_mode_status.get().startswith("Line Fill")
    assert app.tags.mode_line_fill_hidden.get() is False
    assert app.tags.mode_warmup_hidden.get() is True
    # ...and the selection really was sent.
    assert app.ui_manager.sent["run_mode"] == "line_fill"


def test_the_readouts_are_refreshed_while_the_pump_is_stopped():
    """Nothing else republishes them when idle, so a value that ever did go
    stale would stay stale until the next run."""

    from dse_engine_controller.application import DseEngineControllerApplication as App

    modes = [_fixed("Warmup", "warmup"), _fixed("Line Fill", "line_fill")]
    app = _mode_runtime_app(modes)
    app.ui_manager.values["run_mode"] = "line_fill"
    app.tags.run_mode_active._v = "stale"

    app.__dict__["_publish_run_mode"] = types_method(App._publish_run_mode, app)
    _run(app._publish_run_mode())
    assert app.tags.run_mode_active.get() == "Line Fill"


def test_the_fault_indicator_goes_quiet_once_it_is_a_day_old():
    """Warrego (2026-08-27): a warning from the day before was still on the
    dashboard. The machine sits in `fault` until a human acts, so the indicator
    re-asserted itself every loop for as long as nobody did."""
    import time

    from dse_engine_controller.application import AUTOSTART_FAULT_LATCH_S

    app = _readout_app(state="fault")
    _run(app._publish_autostart_tags(run_request=False, lost_comms=False))
    assert app.tags.auto_start_fault_hidden.get() is False
    stamped = app.tags.auto_start_fault_at.get()
    assert stamped is not None

    # Still the same fault a day later, still unattended.
    app.tags.auto_start_fault_at._v = time.time() - AUTOSTART_FAULT_LATCH_S - 1
    _run(app._publish_autostart_tags(run_request=False, lost_comms=False))
    assert app.tags.auto_start_fault_hidden.get() is True
    # The reason and the machine's own state are untouched — only the shouting
    # stops. auto_start_state still reads "Fault: …" until someone acts.
    assert app.tags.auto_start_fault.get() == "Engine stopped unexpectedly"


def test_a_different_fault_raises_the_indicator_again():
    import time

    from dse_engine_controller.application import AUTOSTART_FAULT_LATCH_S

    app = _readout_app(state="fault")
    _run(app._publish_autostart_tags(run_request=False, lost_comms=False))
    app.tags.auto_start_fault_at._v = time.time() - AUTOSTART_FAULT_LATCH_S - 1
    _run(app._publish_autostart_tags(run_request=False, lost_comms=False))
    assert app.tags.auto_start_fault_hidden.get() is True

    app.autostart.fault_reason = "Auto Start Failed"
    _run(app._publish_autostart_tags(run_request=False, lost_comms=False))
    assert app.tags.auto_start_fault_hidden.get() is False
    assert app.tags.auto_start_fault.get() == "Auto Start Failed"
    # ...and its clock starts from now, not from the one it replaced.
    assert time.time() - app.tags.auto_start_fault_at.get() < 5


def test_a_stale_fault_stays_quiet_while_the_pump_sits_idle():
    import time

    from dse_engine_controller.application import AUTOSTART_FAULT_LATCH_S

    app = _readout_app(state="fault")
    _run(app._publish_autostart_tags(run_request=False, lost_comms=False))
    app.tags.auto_start_fault_at._v = time.time() - AUTOSTART_FAULT_LATCH_S - 1
    app.autostart.state = "idle"
    _run(app._publish_autostart_tags(run_request=False, lost_comms=False))
    assert app.tags.auto_start_fault_hidden.get() is True


def test_a_restart_drops_a_latched_fault():
    """The auto-start machine comes up in `idle` with no memory of it, so the app
    can no longer vouch for the warning. The event stays in the activity log."""
    from types import SimpleNamespace

    from dse_engine_controller.application import DseEngineControllerApplication as App

    app = SimpleNamespace(
        tags=SimpleNamespace(
            auto_start_fault=_TagStub("Auto Start Failed"),
            auto_start_fault_hidden=_TagStub(False),
            auto_start_fault_at=_TagStub(1000.0),
        )
    )
    _run(App._clear_autostart_fault(app))
    assert app.tags.auto_start_fault.get() == ""
    assert app.tags.auto_start_fault_hidden.get() is True
    assert app.tags.auto_start_fault_at.get() is None


def test_the_tile_leaves_the_mode_off_a_panel_reporting_its_own_phase():
    """The panel's warm-up and a run mode named Warmup are different things, and
    reading them together is nonsense."""
    for phase, shown in (
        ("warming_up", "Warming Up"),
        ("cooling_down", "Cooling Down"),
    ):
        assert _display_state(
            "running", shown, modes=3, running=True, active="Warmup", phase=phase
        ) == shown, phase

    # A plain running engine still names its mode.
    assert _display_state(
        "running", "Running", modes=3, running=True, active="Warmup"
    ) == "Running (Warmup)"


class TestAwaitUntilCancellation:
    """The real `_await_until`, which the rig harness stubs out.

    Cancelling mid-wait is the case that matters: the operator hits cancel while
    the handler is still spinning, and the abort has to happen there rather than
    after the timeout.
    """

    @staticmethod
    def _app():
        from types import SimpleNamespace

        from dse_engine_controller.application import DseEngineControllerApplication

        app = SimpleNamespace()
        app._await_until = types.MethodType(
            DseEngineControllerApplication._await_until, app
        )

        async def command_progress(_ctx, _text):
            return None

        app._command_progress = command_progress
        return app

    class _Ctx:
        def __init__(self):
            self.cancelled = False
            self.cancelled_by = None
            self.progress = []

    def test_cancelling_mid_wait_runs_on_cancel_once(self):
        app = self._app()
        ctx = self._Ctx()
        polls = []
        aborted = []

        def predicate():
            polls.append(1)
            if len(polls) == 3:
                ctx.cancelled = True
            # Only the abort ends this wait; a timeout would take 60s.
            return bool(aborted)

        async def on_cancel(c):
            aborted.append(c)

        assert _run(
            app._await_until(
                predicate, timeout=60, poll=0, ctx=ctx, on_cancel=on_cancel
            )
        )
        assert len(aborted) == 1, "on_cancel must fire exactly once"

    def test_on_cancel_is_not_called_when_nothing_cancels(self):
        app = self._app()
        ctx = self._Ctx()
        aborted = []

        async def on_cancel(c):
            aborted.append(c)

        assert _run(
            app._await_until(
                lambda: True, timeout=1, poll=0, ctx=ctx, on_cancel=on_cancel
            )
        )
        assert aborted == []

    def test_progress_stops_being_reported_once_cancelled(self):
        """Reporting `pending` over a cancelled command would un-cancel it."""
        app = self._app()
        ctx = self._Ctx()
        sent = []

        async def command_progress(_ctx, text):
            sent.append(text)

        app._command_progress = command_progress
        polls = []

        def predicate():
            polls.append(1)
            if len(polls) == 2:
                ctx.cancelled = True
            return len(polls) >= 5

        _run(
            app._await_until(
                predicate,
                timeout=60,
                poll=0,
                ctx=ctx,
                progress=lambda: f"step {len(polls)}",
            )
        )
        assert sent, "progress should be reported before the cancellation"
        assert len(sent) == 1, f"nothing may be sent after cancelling, got {sent}"

    def test_a_wait_with_no_ctx_is_unaffected(self):
        app = self._app()
        polls = []

        def predicate():
            polls.append(1)
            return len(polls) >= 3

        assert _run(app._await_until(predicate, timeout=60, poll=0))
