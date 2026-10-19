"""Smoke tests for the DSE engine controller application.

Validate modules are importable, the config schema is well-formed, the
register decoder produces sane values from synthetic register frames, and
that the config / UI exporters produce a valid doover_config.json.
"""

import json

from pydoover.config import Schema
from pydoover.tags import Tags
from pydoover.ui import UI

# Imported here rather than per-test: several scenarios assert on it, and the
# point of the constant is that every start failure reports the same thing.
from dse_engine_controller.autostart import FAULT_START_FAILED


def test_import_app():
    from dse_engine_controller.application import DseEngineControllerApplication
    assert DseEngineControllerApplication.config_cls is not None
    assert DseEngineControllerApplication.tags_cls is not None
    assert DseEngineControllerApplication.ui_cls is not None


def test_config_schema():
    from dse_engine_controller.app_config import DseEngineControllerConfig
    assert issubclass(DseEngineControllerConfig, Schema)

    schema = DseEngineControllerConfig.to_schema()
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    assert len(schema["properties"]) > 0


def test_tags():
    from dse_engine_controller.app_tags import DseEngineControllerTags
    assert issubclass(DseEngineControllerTags, Tags)


def test_ui():
    from dse_engine_controller.app_ui import DseEngineControllerUI
    assert issubclass(DseEngineControllerUI, UI)


def _image(overrides=None):
    """A sparse GenComm register image for an E400: stopped, Auto, no alarms."""
    from dse_engine_controller import gencomm as gc

    words = {
        (gc.PAGE_STATUS, gc.P3_CONTROL_MODE): gc.ControlMode.AUTO,
        (gc.PAGE_STATUS, gc.P3_ALARM_FLAGS): 0,
        (gc.PAGE_BASIC, gc.P4_ENGINE_SPEED): 0,
        (gc.PAGE_EXTENDED, gc.P5_ENGINE_OPERATING_STATE): gc.EngineOperatingState.STOPPED,
        **{(gc.PAGE_NAMED_ALARMS, i): 0x1111 for i in range(1, gc.NAMED_ALARM_REGISTERS + 1)},
    }
    words.update(overrides or {})
    return {gc.address(p, o): v for (p, o), v in words.items()}


def test_app_state_derivation():
    from dse_engine_controller import gencomm as gc
    from dse_engine_controller.app_state import DseEngineControllerState
    from dse_engine_controller.dse_state import DseStateStore

    state = DseEngineControllerState()
    store = DseStateStore()
    assert state.update(store) == "initialising"

    # Empty / failed read → no_comms once we've had a record
    store.update(None)
    assert state.update(store) == "initialising"  # still no records

    store.update(_image({(gc.PAGE_STATUS, gc.P3_CONTROL_MODE): gc.ControlMode.MANUAL}))
    assert state.update(store) == "stopped_manual"

    store.update(_image())
    assert state.update(store) == "stopped_auto"

    store.update(_image({(gc.PAGE_EXTENDED, gc.P5_ENGINE_OPERATING_STATE): gc.EngineOperatingState.PRE_START}))
    assert state.update(store) == "pre_start"

    store.update(
        _image({
                (gc.PAGE_BASIC, gc.P4_ENGINE_SPEED): 1500,
                (gc.PAGE_EXTENDED, gc.P5_ENGINE_OPERATING_STATE): gc.EngineOperatingState.WARMING_UP,
            }
        )
    )
    assert state.update(store) == "warming_up"

    store.update(_image({(gc.PAGE_BASIC, gc.P4_ENGINE_SPEED): 1500}))
    assert state.update(store) == "running"

    store.update(_image({(gc.PAGE_EXTENDED, gc.P5_ENGINE_OPERATING_STATE): gc.EngineOperatingState.COOLING_DOWN}))
    assert state.update(store) == "cooling_down"

    # A shutdown alarm wins over everything.
    store.update(_image({(gc.PAGE_NAMED_ALARMS, 1): 0x1311}))
    assert state.update(store) == "fault"

    # Comms lost → no_comms, and the last snapshot stays available
    store.update(None)
    assert state.update(store) == "no_comms"
    assert store.latest is not None


def test_config_export(tmp_path):
    from dse_engine_controller.app_config import DseEngineControllerConfig

    fp = tmp_path / "doover_config.json"
    DseEngineControllerConfig.export(fp, "dse_engine_controller")

    data = json.loads(fp.read_text())
    assert "dse_engine_controller" in data
    schema = data["dse_engine_controller"]["config_schema"]
    assert "properties" in schema
    assert "modbus_server_id" in schema["properties"]
    assert "dse_model" in schema["properties"]
    assert "slow_poll_period_seconds" in schema["properties"]

    # Analog inputs are a list, so they need no enabling switch and sit at the
    # top level alongside the settings shared by both sensor kinds.
    props = schema["properties"]
    assert "sensor_fault_action" in props
    assert "start_trigger_direction" in props

    # Only the remote tag slots are gated, in an allOf branch.
    sensors = next(
        group["then"]["properties"]
        for group in schema["allOf"]
        if "enable_remote_tags" in group["if"]["properties"]
    )
    assert "remote_sensor_1" in sensors
    assert "analog_inputs" not in sensors

    # The sensitivity slider reaches right down to the gentle end by default: on a
    # geometric knob, 0.1 is ~3000x below the top of the scale.
    assert props["regulate_sensitivity_minimum"]["default"] == 0.1
    assert props["regulate_sensitivity_maximum"]["default"] == 10.0

    analog_inputs = props["analog_inputs"]
    assert analog_inputs["type"] == ["array", "null"]
    analog = analog_inputs["items"]["properties"]
    for field in ("ai_pin", "signal_type", "value_min", "value_max", "setpoint_max"):
        assert field in analog, field

    # Using a reading to drive engine speed is opt-in, on both sensor kinds.
    for props in (analog, sensors["remote_sensor_1"]["properties"]):
        assert props["allow_rpm_control"]["default"] is False
    # Custom signal bounds only appear once Signal Type is Custom.
    custom = next(
        group["then"]["properties"]
        for group in analog_inputs["items"]["allOf"]
        if group["if"]["properties"]["signal_type"]["const"] == "Custom"
    )
    assert set(custom) == {"signal_min", "signal_max"}

    # Three remote slots, each collapsed so the unused ones cost a row, not a form.
    assert "remote_sensor_4" not in sensors
    assert sensors["remote_sensor_1"]["x-defaultCollapsed"] is True


def _sample_deployment_config():
    """The shipped simulator config — a realistic, complete deployment config."""
    import pathlib

    fp = pathlib.Path(__file__).parents[1] / "simulators" / "app_config.json"
    return json.loads(fp.read_text())


def test_sample_deployment_config_loads():
    from dse_engine_controller.app_config import DseEngineControllerConfig

    config = DseEngineControllerConfig()
    config._inject_deployment_config(_sample_deployment_config())
    assert config.analog_inputs.value[0].ai_pin.value == 2


def test_null_display_ranges_do_not_break_config_load():
    """A cleared list arrives from the site as `null`, and pydoover's Array raises
    on it while the deployment config is loading — i.e. before the app starts."""
    from dse_engine_controller.app_config import DseEngineControllerConfig

    for value in (None, [], [{"label": "All", "min_val": 0, "max_val": 100}]):
        data = _sample_deployment_config()
        data["analog_inputs"][0]["display_ranges"] = value
        config = DseEngineControllerConfig()
        config._inject_deployment_config(data)
        ranges = config.analog_inputs.value[0].display_ranges.value
        assert len(ranges) == (1 if value else 0)


def test_ui_export(tmp_path):
    from dse_engine_controller.app_ui import DseEngineControllerUI

    fp = tmp_path / "doover_config.json"
    DseEngineControllerUI(None, None, None).export(fp, "dse_engine_controller")

    data = json.loads(fp.read_text())
    ui_schema = data["dse_engine_controller"]["ui_schema"]
    assert ui_schema["type"] == "uiApplication"
    children = ui_schema["children"]
    assert "control" in children
    # Engine / Panel Sensors / Speed Control / Diagnostics live under "Details".
    assert "details" in children
    details = children["details"]["children"]
    assert "engine" in details
    assert "flex_sensors" in details
    assert "speed_control" in details
    assert "diagnostics" in details
    assert "active_alarms" in children


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _make_controller(**kw):
    from dse_engine_controller.autostart import AutoStartController

    defaults = dict(warning_seconds=0, start_timeout=5, stop_timeout=5)
    defaults.update(kw)
    return AutoStartController(**defaults)


def test_autostart_happy_path():
    """idle -> warning -> starting -> running -> stopping -> idle."""

    async def scenario():
        c = _make_controller()
        assert c.state == "idle"

        # Operator requests a run -> pre-start warning, beacon + horn on.
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "prestart_warning"
        assert c.beacon_active and c.horn_active

        # warning_seconds=0, so the next tick completes the warning -> starting.
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "starting"
        # Horn keeps sounding: this is where STARTs are written and the engine
        # can begin turning at any moment.
        assert c.beacon_active and c.horn_active

        # Panel reports the engine running -> running, outputs off.
        await c.evaluate(lost_comms=False, is_running=True, is_faulted=False, run_request=True)
        assert c.state == "running"
        assert not c.beacon_active and not c.horn_active

        # Run request cleared -> stopping -> idle once stopped.
        await c.evaluate(lost_comms=False, is_running=True, is_faulted=False, run_request=False)
        assert c.state == "stopping"
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=False)
        assert c.state == "idle"

    _run(scenario())


def test_autostart_cannot_skip_warning():
    """There is no transition straight from idle to starting/running."""

    async def scenario():
        c = _make_controller(warning_seconds=999)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "prestart_warning"
        # Even if the engine somehow already reads running, we stay in warning
        # until the countdown elapses or the request is cancelled.
        await c.evaluate(lost_comms=False, is_running=True, is_faulted=False, run_request=True)
        assert c.state == "prestart_warning"

    _run(scenario())


def test_autostart_cancel_during_warning():
    async def scenario():
        c = _make_controller(warning_seconds=999)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "prestart_warning"
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=False)
        assert c.state == "idle"

    _run(scenario())


def test_autostart_comms_tolerated_until_running():
    """Comms loss during warning/starting is expected (panel reboot), not a fault."""

    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=999)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "prestart_warning"
        await c.evaluate(lost_comms=True, is_running=False, is_faulted=True, run_request=True)
        assert c.state == "starting"  # reboot drops comms + shows fault: tolerated
        await c.evaluate(lost_comms=True, is_running=False, is_faulted=True, run_request=True)
        assert c.state == "starting"

    _run(scenario())


def test_autostart_starts_without_comms():
    """A start must begin on a panel we can't hear.

    The pre-start warning is entirely app-side, and a Kensho that is switched off
    — or booting on the app's own Pump Power output — is exactly the case the
    warning has to cover. Refusing to leave `idle` meant pressing Start on a dark
    panel did nothing at all: no beacon, no countdown, no fault.
    """

    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=999)
        await c.evaluate(lost_comms=True, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "prestart_warning"
        assert c.beacon_active and c.horn_active  # the warning runs regardless
        # The panel comes up part-way through and the sequence carries on.
        await c.evaluate(lost_comms=True, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "starting"
        await c.evaluate(lost_comms=False, is_running=True, is_faulted=False, run_request=True)
        assert c.state == "running"

    _run(scenario())


def test_autostart_start_without_comms_faults_on_the_start_timeout():
    """A panel that never comes up aborts to a visible fault, not silence."""

    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=0)
        await c.evaluate(lost_comms=True, is_running=False, is_faulted=False, run_request=True)
        await c.evaluate(lost_comms=True, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "starting"
        await c.evaluate(lost_comms=True, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "fault"
        assert c.fault_reason == FAULT_START_FAILED

    _run(scenario())


def test_autostart_stop_without_comms_when_it_never_ran():
    """Stopping an attempt on a dead panel returns to Ready, not a stop fault.

    Now that a start can begin without comms, the operator stopping it is an
    ordinary path — and timing out into "Engine did not stop" would fault a pump
    that never turned over.
    """

    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=999, stop_timeout=999)
        await c.evaluate(lost_comms=True, is_running=False, is_faulted=False, run_request=True)
        await c.evaluate(lost_comms=True, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "starting"
        # Operator presses Stop Now. evaluate() fires at most one transition per
        # call, so `starting` hands over to `stopping` and the next loop settles
        # it: nothing ever ran, so there is nothing to wait for, comms or no comms.
        await c.evaluate(lost_comms=True, is_running=False, is_faulted=False, run_request=False)
        assert c.state == "stopping"
        await c.evaluate(lost_comms=True, is_running=False, is_faulted=False, run_request=False)
        assert c.state == "idle"

    _run(scenario())


def test_autostart_stop_without_comms_still_waits_on_a_running_engine():
    """The opposite case: it *was* running, so a silent panel isn't proof it stopped."""

    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=999, stop_timeout=999)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        await c.evaluate(lost_comms=False, is_running=True, is_faulted=False, run_request=True)
        assert c.state == "running"
        await c.evaluate(lost_comms=False, is_running=True, is_faulted=False, run_request=False)
        assert c.state == "stopping"
        # Comms drop mid-cooldown: hold `stopping` rather than declare it stopped.
        await c.evaluate(lost_comms=True, is_running=False, is_faulted=False, run_request=False)
        assert c.state == "stopping"

    _run(scenario())


def test_autostart_faults_and_resets():
    async def scenario():
        c = _make_controller(warning_seconds=0)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        await c.evaluate(lost_comms=False, is_running=True, is_faulted=False, run_request=True)
        assert c.state == "running"
        # Engine drops out unexpectedly -> fault.
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "fault"
        assert "unexpectedly" in c.fault_reason
        # Reset returns to idle and clears the reason.
        await c.reset()
        assert c.state == "idle" and c.fault_reason == ""

    _run(scenario())


def test_autostart_adopts_running_engine():
    """A running engine can be adopted straight into `running` from idle."""

    async def scenario():
        c = _make_controller()
        assert c.state == "idle"
        await c.adopt_running()
        assert c.state == "running"
        # No beacon/horn when adopting an already-running engine.
        assert not c.beacon_active and not c.horn_active
        # And it stops normally from there.
        await c.evaluate(lost_comms=False, is_running=True, is_faulted=False, run_request=False)
        assert c.state == "stopping"

    _run(scenario())


def test_warning_seconds_has_minimum_floor():
    """Config cannot be saved with a warning shorter than the safety floor."""
    from dse_engine_controller.app_config import DseEngineControllerConfig

    inst = DseEngineControllerConfig()
    assert inst.prestart_warning_seconds.minimum >= 5


def test_autostart_start_timeout_faults():
    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=0)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "starting"
        # start_timeout=0 means the next tick without a running engine faults.
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "fault"
        assert c.fault_reason == FAULT_START_FAILED

    _run(scenario())


# ---------------------------------------------------------------------------
# Panel-restart tolerance + fault recovery
# ---------------------------------------------------------------------------


def test_panel_restart_does_not_fault_a_running_engine():
    """Stations 1 and 2 (2026-08-18): a simulator/panel restart took modbus away
    for ~8s; the panel came back reporting stopped in Manual, the app called it
    "Engine stopped unexpectedly", and it sat in fault while the engine ran for the
    next half hour. The panel reported running again ~85s after the outage.
    """

    async def scenario():
        c = _make_controller(warning_seconds=0)
        await c.adopt_running()
        assert c.state == "running"

        # The outage itself never reaches the state machine: has_lost_comms needs
        # 15s and 3 polls, and the panel was only away for ~8s. What arrives is the
        # freshly-booted panel reporting stopped, in Manual.
        for _ in range(5):
            await c.evaluate(
                lost_comms=False,
                is_running=False,
                is_faulted=False,
                run_request=True,
                panel_restarting=True,
            )
            assert c.state == "running"
        # And it says so, rather than claiming everything is fine.
        assert c.awaiting_panel is True
        assert c.display == "Running (waiting for the panel)"

        # The panel comes back running: no fault ever happened.
        await c.evaluate(
            lost_comms=False, is_running=True, is_faulted=False, run_request=True
        )
        assert c.state == "running"
        assert c.awaiting_panel is False
        assert c.display == "Running"

    _run(scenario())


def test_panel_restart_grace_expires_into_a_fault():
    """Waiting must not mean waiting forever — an engine that really has stopped
    still faults, just a little later."""

    async def scenario():
        c = _make_controller(warning_seconds=0, panel_restart_grace=0.0)
        await c.adopt_running()
        await c.evaluate(
            lost_comms=False,
            is_running=False,
            is_faulted=False,
            run_request=True,
            panel_restarting=True,
        )
        assert c.state == "fault"
        assert "stopped unexpectedly" in c.fault_reason

    _run(scenario())


def test_engine_stopping_with_a_healthy_panel_still_faults_immediately():
    """The tolerance is only for a panel that looks restarted. An engine that trips
    while comms are fine and the panel is in Auto is a real fault."""

    async def scenario():
        c = _make_controller(warning_seconds=0)
        await c.adopt_running()
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False, run_request=True
        )
        assert c.state == "fault"

    _run(scenario())


def test_fault_recovers_when_the_engine_turns_up_running():
    async def scenario():
        c = _make_controller(warning_seconds=0)
        await c.adopt_running()
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False, run_request=True
        )
        assert c.state == "fault"

        # The engine is running again, comms are healthy, and a run is still wanted.
        await c.evaluate(
            lost_comms=False, is_running=True, is_faulted=False, run_request=True
        )
        assert c.state == "running"
        assert c.fault_reason == ""  # and the stale reason is gone

    _run(scenario())


def test_fault_recovery_needs_the_run_to_still_be_wanted():
    """If the operator has stopped it, an engine running in Manual is someone
    else's business — adopting it would immediately command a stop."""

    async def scenario():
        c = _make_controller(warning_seconds=0)
        await c.adopt_running()
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False, run_request=True
        )
        assert c.state == "fault"

        await c.evaluate(
            lost_comms=False, is_running=True, is_faulted=False, run_request=False
        )
        assert c.state == "fault"

    _run(scenario())


def test_a_failed_start_does_not_auto_recover():
    """A start that timed out wants a human, even if the engine later turns up
    running — something is wrong with the sequence."""

    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=0)
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False, run_request=True
        )
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False, run_request=True
        )
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False, run_request=True
        )
        assert c.state == "fault"
        assert c.fault_reason == FAULT_START_FAILED

        await c.evaluate(
            lost_comms=False, is_running=True, is_faulted=False, run_request=True
        )
        assert c.state == "fault"  # still needs an operator to press Start

    _run(scenario())


def test_a_panel_fault_does_not_auto_recover():
    async def scenario():
        c = _make_controller(warning_seconds=0)
        await c.adopt_running()
        await c.evaluate(
            lost_comms=False, is_running=True, is_faulted=True, run_request=True
        )
        assert c.state == "fault"
        assert c.fault_reason == "Panel fault while running"

        await c.evaluate(
            lost_comms=False, is_running=True, is_faulted=False, run_request=True
        )
        assert c.state == "fault"

    _run(scenario())


def test_autostart_warning_held_open_until_the_panel_can_take_a_start():
    """The hole this closes: the countdown expires, the horn stops, and the engine
    cranks 30s later when the panel finishes rebooting out of the Force-Reset."""

    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=999)
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False,
            run_request=True, panel_ready=False,
        )
        assert c.state == "prestart_warning"
        # Countdown spent, panel still being prepared: stay in the warning, with
        # the horn still sounding, and say so.
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False,
            run_request=True, panel_ready=False,
        )
        assert c.state == "prestart_warning"
        assert c.horn_active and c.beacon_active
        assert c.awaiting_panel_ready
        assert c.warning_remaining is None  # no honest number to give
        assert c.display == "Pre-start warning (preparing panel)"
        # Panel reaches Auto — now, and only now, does the start go.
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False,
            run_request=True, panel_ready=True,
        )
        assert c.state == "starting"

    _run(scenario())


def test_autostart_held_warning_faults_on_the_start_timeout():
    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=0)
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False,
            run_request=True, panel_ready=False,
        )
        assert c.state == "prestart_warning"
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False,
            run_request=True, panel_ready=False,
        )
        assert c.state == "fault"
        assert c.fault_reason == FAULT_START_FAILED

    _run(scenario())


def test_autostart_held_warning_can_still_be_cancelled():
    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=999)
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False,
            run_request=True, panel_ready=False,
        )
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False,
            run_request=False, panel_ready=False,
        )
        assert c.state == "idle"

    _run(scenario())


def test_autostart_held_warning_ends_if_the_engine_turns_up_running():
    """Started at the panel while we warned about starting it — nothing left to
    warn of, and the panel will never read Auto-ready while it runs in Manual."""

    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=999)
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False,
            run_request=True, panel_ready=False,
        )
        await c.evaluate(
            lost_comms=False, is_running=True, is_faulted=False,
            run_request=True, panel_ready=False,
        )
        assert c.state == "starting"

    _run(scenario())


def test_autostart_countdown_is_a_floor_not_shortened_by_a_ready_panel():
    async def scenario():
        c = _make_controller(warning_seconds=999, start_timeout=999)
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False,
            run_request=True, panel_ready=True,
        )
        assert c.state == "prestart_warning"
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False,
            run_request=True, panel_ready=True,
        )
        assert c.state == "prestart_warning"
        assert not c.awaiting_panel_ready
        assert c.warning_remaining is not None

    _run(scenario())


def test_autostart_marks_which_faults_were_failed_starts():
    """A start that gave up leaves an engine that never ran; a fault out of a run
    leaves one that may still be turning. The app says different things about the
    pump — and keeps the panel powered or not — depending on which it was."""

    async def scenario():
        # Engine never cranked.
        c = _make_controller(warning_seconds=0, start_timeout=0)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "fault" and c.start_failed

        # Panel never became ready.
        c = _make_controller(warning_seconds=0, start_timeout=0)
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False,
            run_request=True, panel_ready=False,
        )
        await c.evaluate(
            lost_comms=False, is_running=False, is_faulted=False,
            run_request=True, panel_ready=False,
        )
        assert c.state == "fault" and c.start_failed

        # Ran, then dropped out: not a failed start.
        c = _make_controller(warning_seconds=0, start_timeout=999)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        await c.evaluate(lost_comms=False, is_running=True, is_faulted=False, run_request=True)
        assert c.state == "running"
        assert not c.start_failed
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "fault" and not c.start_failed

    _run(scenario())


def test_start_failed_clears_when_the_pump_runs_again():
    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=0)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        assert c.start_failed
        await c.reset()
        assert not c.start_failed  # only ever true while that fault stands

    _run(scenario())


def test_autostart_horn_sounds_for_the_whole_start_attempt():
    """Station test 2026-08-25: 30s of horn, then ~150s of silence, then "startup
    failed" out of nowhere. The silence was `starting` — the window where STARTs
    are actually written and the engine can crank at any moment."""

    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=999)
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "prestart_warning"
        assert c.horn_active
        await c.evaluate(lost_comms=False, is_running=False, is_faulted=False, run_request=True)
        assert c.state == "starting"
        assert c.horn_active and c.beacon_active
        # It stops when the attempt does, not before.
        await c.evaluate(lost_comms=False, is_running=True, is_faulted=False, run_request=True)
        assert c.state == "running"
        assert not c.horn_active and not c.beacon_active

    _run(scenario())


def test_autostart_warning_runs_the_whole_way_on_a_panel_that_never_answers():
    """The reported case end to end: no comms for the entire attempt. The warning
    must run from the press to the fault, with no silent gap in the middle."""

    async def scenario():
        c = _make_controller(warning_seconds=0, start_timeout=0)
        # panel_ready is False throughout — a panel we have never heard from.
        await c.evaluate(
            lost_comms=True, is_running=False, is_faulted=False,
            run_request=True, panel_ready=False,
        )
        assert c.state == "prestart_warning" and c.horn_active
        await c.evaluate(
            lost_comms=True, is_running=False, is_faulted=False,
            run_request=True, panel_ready=False,
        )
        # Straight to the fault — never a state where the horn is off but the
        # app is still trying.
        assert c.state == "fault"
        assert c.start_failed
        assert c.fault_reason == FAULT_START_FAILED
        assert not c.horn_active

    _run(scenario())


def test_start_timeout_is_one_budget_across_the_whole_attempt():
    """One budget from the operator's press: countdown, panel prep and cranking
    all draw on it. It has to survive prestart_warning -> starting, where
    time_in_state resets and would otherwise hand each phase its own full
    timeout — and multiply how long the horn sounds on a failed start."""
    import dse_engine_controller.autostart as autostart_mod

    clock = [1000.0]
    real_time = autostart_mod.time

    class _FakeTime:
        @staticmethod
        def time():
            return clock[0]

    autostart_mod.time = _FakeTime()
    try:

        async def scenario():
            c = _make_controller(warning_seconds=10, start_timeout=60)
            await c.evaluate(lost_comms=False, is_running=False, is_faulted=False,
                             run_request=True, panel_ready=False)
            assert c.state == "prestart_warning"
            assert c.attempt_elapsed < 1  # the clock starts at the press

            # The countdown is on the budget too.
            clock[0] += 10
            await c.evaluate(lost_comms=False, is_running=False, is_faulted=False,
                             run_request=True, panel_ready=False)
            assert c.state == "prestart_warning" and c.awaiting_panel_ready
            assert 9 < c.attempt_elapsed < 11

            # 30s more holding the warning open for the panel, then it comes good.
            clock[0] += 30
            await c.evaluate(lost_comms=False, is_running=False, is_faulted=False,
                             run_request=True, panel_ready=True)
            assert c.state == "starting"
            assert 39 < c.attempt_elapsed < 41  # carried across the transition

            # 15s of cranking: 55s in total, still inside the budget.
            clock[0] += 15
            await c.evaluate(lost_comms=False, is_running=False, is_faulted=False,
                             run_request=True, panel_ready=True)
            assert c.state == "starting"
            # 6s more takes the whole attempt past 60s.
            clock[0] += 6
            await c.evaluate(lost_comms=False, is_running=False, is_faulted=False,
                             run_request=True, panel_ready=True)
            assert c.state == "fault" and c.start_failed

        _run(scenario())
    finally:
        autostart_mod.time = real_time


def test_start_budget_resets_between_attempts():
    import dse_engine_controller.autostart as autostart_mod

    clock = [1000.0]
    real_time = autostart_mod.time

    class _FakeTime:
        @staticmethod
        def time():
            return clock[0]

    autostart_mod.time = _FakeTime()
    try:

        async def scenario():
            c = _make_controller(warning_seconds=0, start_timeout=60)
            await c.evaluate(lost_comms=False, is_running=False, is_faulted=False,
                             run_request=True, panel_ready=True)
            clock[0] += 5
            await c.evaluate(lost_comms=False, is_running=False, is_faulted=False,
                             run_request=True, panel_ready=True)
            assert c.state == "starting"
            # Engine runs, then the operator stops it and starts again much later.
            await c.evaluate(lost_comms=False, is_running=True, is_faulted=False,
                             run_request=True, panel_ready=True)
            assert c.state == "running"
            clock[0] += 3600
            await c.evaluate(lost_comms=False, is_running=True, is_faulted=False,
                             run_request=False, panel_ready=True)
            await c.evaluate(lost_comms=False, is_running=False, is_faulted=False,
                             run_request=False, panel_ready=True)
            assert c.state == "idle"
            await c.evaluate(lost_comms=False, is_running=False, is_faulted=False,
                             run_request=True, panel_ready=True)
            await c.evaluate(lost_comms=False, is_running=False, is_faulted=False,
                             run_request=True, panel_ready=True)
            # A fresh budget, not an hour-old one.
            assert c.state == "starting" and c.attempt_elapsed < 1

        _run(scenario())
    finally:
        autostart_mod.time = real_time


def test_shipped_start_defaults_are_workable():
    """The two settings are coupled — one budget covers the countdown and the
    panel handshake, which run concurrently — so the shipped pair has to clear
    the longer of them. Asserts the relationship rather than the numbers, so
    tuning either default doesn't need a test edit to go with it."""
    from dse_engine_controller.app_config import DseEngineControllerConfig
    from dse_engine_controller.application import PANEL_PREP_BUDGET_S

    budget = DseEngineControllerConfig.start_timeout_seconds.default
    countdown = DseEngineControllerConfig.prestart_warning_seconds.default
    assert budget > countdown
    assert budget >= PANEL_PREP_BUDGET_S


def test_countdown_still_runs_in_full_on_a_budget_too_short_for_it():
    """A budget at or below the countdown can't start an engine — but it must not
    cut the warning short either. The countdown stays a floor; the fault lands
    the moment it ends."""
    import dse_engine_controller.autostart as autostart_mod

    clock = [1000.0]
    real_time = autostart_mod.time

    class _FakeTime:
        @staticmethod
        def time():
            return clock[0]

    autostart_mod.time = _FakeTime()
    try:

        async def scenario():
            c = _make_controller(warning_seconds=30, start_timeout=10)
            await c.evaluate(lost_comms=False, is_running=False, is_faulted=False,
                             run_request=True, panel_ready=False)
            assert c.state == "prestart_warning"
            # Well past the budget, still inside the countdown: warning holds.
            clock[0] += 20
            await c.evaluate(lost_comms=False, is_running=False, is_faulted=False,
                             run_request=True, panel_ready=False)
            assert c.state == "prestart_warning" and c.horn_active
            clock[0] += 11
            await c.evaluate(lost_comms=False, is_running=False, is_faulted=False,
                             run_request=True, panel_ready=False)
            assert c.state == "fault" and c.start_failed

        _run(scenario())
    finally:
        autostart_mod.time = real_time


def test_both_kinds_of_failed_start_report_the_same_thing():
    """An operator reading a dashboard warning wants to know the pump didn't
    start. Which of the two ways it failed, and the timeout it hit, are in the
    log — that is the difference between a warning and a diagnosis."""

    async def scenario():
        # The panel never became ready to take a start.
        panel = _make_controller(warning_seconds=0, start_timeout=0)
        for _ in range(2):
            await panel.evaluate(
                lost_comms=False, is_running=False, is_faulted=False,
                run_request=True, panel_ready=False,
            )
        # The panel was ready; the engine never ran.
        engine = _make_controller(warning_seconds=0, start_timeout=0)
        for _ in range(3):
            await engine.evaluate(
                lost_comms=False, is_running=False, is_faulted=False,
                run_request=True, panel_ready=True,
            )

        assert panel.state == engine.state == "fault"
        assert panel.fault_reason == engine.fault_reason == FAULT_START_FAILED
        assert panel.start_failed and engine.start_failed

    _run(scenario())
