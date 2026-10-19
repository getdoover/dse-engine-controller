"""Whole-sequence tests: the pump from Start pressed to idle again.

Every method here is the production one — see ``tests/harness.py``. These cover
the composition the unit tests can't: that the warning really does sound before
anything is commanded, that the panel is prepared while it sounds, that the
engine is only commanded once the panel can take it, that the run modes drive the
speed, and that a stop cools down and lands back in idle.
"""

import pytest

from .harness import (
    CMD_AUTO,
    CMD_RESET,
    CMD_START,
    CMD_STOP,
    REG_REMOTE_RPM,
    Panel,
    Rig,
    build_config,
)
from .harness import run as drive


def _built_ui_for(config):
    """The UI as the framework builds it, tags first."""

    from dse_engine_controller.app_tags import DseEngineControllerTags
    from dse_engine_controller.app_ui import DseEngineControllerUI

    tags = DseEngineControllerTags("k", None, config)
    drive(tags.setup())
    ui = DseEngineControllerUI(config, tags, "k")
    drive(ui.setup())
    return ui


def run_transition(rig_, element: str, value):
    """Move a transition's threshold the way the site would."""
    from .harness import _Ctx

    drive(rig_.app.on_transition_threshold(_Ctx(element), float(value)))
    rig_.tick(1)


def run_handler(rig_, element: str, value, parser=int):
    """Fire a ui_cmd handler the way the site would, by element name."""
    from .harness import _Ctx

    handler = {
        "target_rpm": rig_.app.on_mode_target_rpm,
        "setpoint": rig_.app.on_mode_setpoint,
        "sensitivity": rig_.app.on_mode_sensitivity,
    }[element.rsplit("_", 1)[-1] if element.endswith(("setpoint", "sensitivity"))
      else "target_rpm"]
    drive(handler(_Ctx(element), parser(value)))
    rig_.tick(1)


@pytest.fixture
def rig():
    """A pump with a healthy panel, stopped in Auto, nothing configured beyond
    the defaults. The clock is always restored, even when a test fails."""
    made = []

    def make(**kwargs):
        made.append(Rig(**kwargs))
        return made[-1]

    yield make
    for r in made:
        r.close()


# ---------------------------------------------------------------------------
# The pre-start warning
# ---------------------------------------------------------------------------


def test_nothing_is_commanded_before_the_warning_has_run(rig):
    """The one guarantee the whole design rests on: no path to a cranking engine
    that skips the warning."""
    r = rig()
    r.press_start()
    r.run_for(20)  # inside the 30s countdown

    assert r.state == "prestart_warning"
    assert CMD_START not in r.panel.commanded()
    assert r.beacon_on()


def test_the_countdown_runs_in_full_even_on_a_ready_panel(rig):
    """A panel already in Auto needs no preparing, but the warning is for the
    people near the pump, not for the panel."""
    r = rig()
    r.press_start()
    r.run_for(28)
    assert r.state == "prestart_warning"
    r.run_for(6)
    assert r.state == "starting"


def test_the_horn_pulses_and_the_beacon_holds(rig):
    """Beacon steady, horn in bursts — the pattern that carries across a yard."""
    r = rig()
    r.press_start()
    seen_horn_on = seen_horn_off = False
    for _ in range(14):
        r.tick(1)
        assert r.beacon_on(), "beacon should be steady for the whole warning"
        seen_horn_on |= r.horn_on()
        seen_horn_off |= not r.horn_on()
    assert seen_horn_on and seen_horn_off


def test_the_warning_sounds_for_the_whole_start_attempt(rig):
    """Station test 2026-08-25: the horn stopped at the end of the countdown and
    the engine could crank a minute later in silence. The warning runs until the
    attempt is over, not until the countdown is."""
    r = rig()
    r.press_start()
    r.run_for(34)
    assert r.state == "starting", "the engine has not caught yet"
    # `starting` is where STARTs are written and the engine can begin turning at
    # any moment, so the horn has to still be sounding through it.
    assert r.app.autostart.horn_active
    assert r.beacon_on()


def test_cancelling_during_the_warning_commands_nothing(rig):
    r = rig()
    r.press_start()
    r.run_for(10)
    r.press_cancel()
    r.tick(2)

    assert r.state == "idle"
    assert r.panel.commanded() == []
    assert not r.beacon_on() and not r.horn_on()


def test_the_warning_outputs_drop_when_the_engine_is_running(rig):
    r = rig()
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(10)

    assert r.state == "running"
    assert not r.beacon_on() and not r.horn_on()
    # ...and the pins really were driven low, not merely reported as off.
    assert r.pin_history(3)[-1] is False  # beacon
    assert r.pin_history(2)[-1] is False  # horn


# ---------------------------------------------------------------------------
# Preparing the panel
# ---------------------------------------------------------------------------


def test_a_panel_in_stop_mode_is_prepared_while_the_warning_sounds(rig):
    """The handshake runs alongside the countdown, so the panel is ready the
    moment the countdown is. An unfaulted panel needs no reset — just Auto."""
    r = rig(panel=Panel(is_auto=False))
    r.press_start()
    r.run_for(10)  # still inside the countdown

    assert r.state == "prestart_warning"
    assert CMD_AUTO in r.panel.commanded()
    assert CMD_RESET not in r.panel.commanded()
    assert CMD_START not in r.panel.commanded()


def test_a_latched_alarm_is_reset_before_auto_is_asked_for(rig):
    """A DSE holds a shutdown latched until it is reset, and won't take a remote
    start until it is both clear and in Auto."""
    r = rig(panel=Panel(is_auto=False, is_faulted=True))
    r.press_start()
    r.run_for(10)
    assert CMD_RESET in r.panel.commanded()
    assert CMD_AUTO not in r.panel.commanded(), "no point asking for Auto while locked out"

    r.panel.is_faulted = False
    r.run_for(10)
    assert CMD_AUTO in r.panel.commanded()


def test_the_start_waits_for_the_panel_to_reach_auto(rig):
    """The countdown is a floor. The engine is only commanded once a START would
    actually be acted on."""
    r = rig(panel=Panel(is_auto=False))
    r.press_start()
    r.run_for(60)  # well past the countdown

    assert r.state == "prestart_warning"
    assert r.app.autostart.awaiting_panel_ready
    assert CMD_START not in r.panel.commanded()

    r.panel.is_auto = True
    r.run_for(6)
    assert r.state == "starting"
    assert CMD_START in r.panel.commanded()


def test_a_persisting_alarm_is_reset_every_few_seconds_not_every_poll(rig):
    """The alarm's cause may still be present, so the reset is retried — but at
    PANEL_RESET_INTERVAL_S, not on every poll."""
    r = rig(panel=Panel(is_auto=False, is_faulted=True))
    r.press_start()
    r.run_for(20)  # ten polls
    resets = r.panel.commanded().count(CMD_RESET)
    assert 2 <= resets <= 5, resets


def test_auto_is_nudged_until_the_panel_latches_it(rig):
    """The panel ignores commands while it reboots, so AUTO is retried rather
    than fired once into the void."""
    r = rig(panel=Panel(is_auto=False))
    r.press_start()
    r.run_for(60)
    assert r.panel.commanded().count(CMD_AUTO) > 1


def test_a_running_engine_is_never_force_reset(rig):
    """Rebooting a panel that is driving a running engine is the one thing the
    prep must never do."""
    r = rig(panel=Panel(is_auto=False, is_running=True))
    r.press_start()
    r.run_for(40)
    assert CMD_RESET not in r.panel.commanded()


# ---------------------------------------------------------------------------
# Starting, and giving up
# ---------------------------------------------------------------------------


def test_a_full_start_reaches_running(rig):
    r = rig()
    r.press_start()
    r.run_for(32)
    assert r.state == "starting"
    r.panel.is_running = True
    r.run_for(10)
    assert r.state == "running"
    assert r.tags.auto_start_state.get() == "Running"


def test_an_engine_that_never_runs_gives_up_and_says_so(rig):
    from dse_engine_controller.autostart import FAULT_START_FAILED

    r = rig(start_timeout_seconds=40)
    r.press_start()
    r.run_for(60)  # countdown 30 + budget 40, measured from the press

    assert r.state == "fault"
    assert r.tags.auto_start_fault.get() == FAULT_START_FAILED
    assert r.tags.auto_start_fault_hidden.get() is False


def test_a_panel_that_never_becomes_ready_gives_up_the_same_way(rig):
    from dse_engine_controller.autostart import FAULT_START_FAILED

    r = rig(panel=Panel(is_auto=False, reachable=False), start_timeout_seconds=40)
    r.press_start()
    r.run_for(60)
    assert r.state == "fault"
    assert r.tags.auto_start_fault.get() == FAULT_START_FAILED


def test_the_budget_covers_the_countdown_too(rig):
    """One budget from the press, so a long countdown eats into it rather than
    being added on top."""
    r = rig(prestart_warning_seconds=30, start_timeout_seconds=40)
    r.press_start()
    r.run_for(36)
    assert r.state == "starting"  # inside the budget
    r.run_for(8)
    assert r.state == "fault"  # 44s from the press, past 40


def test_an_already_running_engine_is_adopted_not_re_warned(rig):
    """Someone started it at the panel, or the app restarted mid-run. Warning
    about a start that already happened, and force-resetting the panel driving
    it, are both wrong."""
    r = rig(panel=Panel(is_running=True))
    r.tick(3)

    assert r.state == "running"
    assert r.panel.commanded() == []
    assert not r.beacon_on()


# ---------------------------------------------------------------------------
# Run modes and transitions
# ---------------------------------------------------------------------------


TWO_MODE_CONFIG = dict(
    run_modes=[
        {"label": "Warm Up", "control": "Fixed", "target_rpm": 900,
         "minimum_seconds": 0},
        {"label": "Line Fill", "control": "Fixed", "target_rpm": 1300,
         "minimum_seconds": 0},
    ],
    start_in_mode="Warm Up",
    include_default_fixed_run_mode=False,
)


def _running(rig_factory, **overrides):
    """A pump already running, so a test can get to the modes."""
    settings = dict(TWO_MODE_CONFIG)
    settings.update(overrides)
    r = rig_factory(config=build_config(**settings))
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(6)
    assert r.state == "running"
    # One more, so the tile has been published since the machine reached
    # `running`: the status comes from the panel data at the top of a loop, and
    # the machine advances below it.
    r.tick(1)
    return r


def test_the_run_starts_in_the_configured_mode_and_holds_its_rpm(rig):
    r = _running(rig)
    assert r.mode == "Warm Up"
    assert r.panel.written(REG_REMOTE_RPM)[-1] == 900


def test_a_timer_transition_moves_the_mode_and_the_rpm(rig):
    r = _running(
        rig,
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
             "after_seconds": 30},
        ],
    )
    assert r.mode == "Warm Up"
    r.run_for(40)
    assert r.mode == "Line Fill"
    assert r.panel.written(REG_REMOTE_RPM)[-1] == 1300


def test_a_panel_flag_transition_fires_on_the_panels_own_bit(rig):
    r = _running(
        rig,
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill",
             "when": "Panel Flag", "panel_flag": "Warming Up",
             "panel_state": "Off", "panel_hold_seconds": 5},
        ],
        run_modes=[
            {"label": "Warm Up", "control": "Fixed", "target_rpm": 900,
             "minimum_seconds": 20},
            {"label": "Line Fill", "control": "Fixed", "target_rpm": 1300,
             "minimum_seconds": 0},
        ],
    )
    r.panel.is_warming_up = True
    r.run_for(30)
    assert r.mode == "Warm Up", "the panel says it is still warming up"

    r.panel.is_warming_up = False
    r.run_for(10)
    assert r.mode == "Line Fill"


def test_a_minimum_holds_a_mode_against_a_condition_true_on_entry(rig):
    """Without it, a rule already satisfied fires on the loop the mode is
    entered — including out of a mode an operator has just picked."""
    r = _running(
        rig,
        run_modes=[
            {"label": "Warm Up", "control": "Fixed", "target_rpm": 900,
             "minimum_seconds": 60},
            {"label": "Line Fill", "control": "Fixed", "target_rpm": 1300,
             "minimum_seconds": 0},
        ],
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
             "after_seconds": 1},
        ],
    )
    r.run_for(30)
    assert r.mode == "Warm Up"
    r.run_for(40)
    assert r.mode == "Line Fill"


def test_an_operator_can_change_mode_mid_run(rig):
    r = _running(rig)
    r.pick_mode("Line Fill")
    r.tick(2)

    assert r.mode == "Line Fill"
    assert r.panel.written(REG_REMOTE_RPM)[-1] == 1300
    assert any("run mode Line Fill (operator)" in m for m in r.messages)


def test_a_transition_says_why_it_moved(rig):
    r = _running(
        rig,
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
             "after_seconds": 30},
        ],
    )
    r.run_for(40)
    assert any("run mode Line Fill (after 30s)" in m for m in r.messages)


def test_modes_do_not_change_while_the_pump_is_stopped(rig):
    """Transitions run the engine; there is no speed to set until one is
    turning, and a timer must not tick down while the pump sits idle."""
    settings = dict(TWO_MODE_CONFIG)
    settings["run_mode_transitions"] = [
        {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
         "after_seconds": 5},
    ]
    r = rig(config=build_config(**settings))
    r.run_for(60)
    assert r.state == "idle"
    assert r.mode == "Warm Up"


def test_every_start_returns_to_the_configured_start_mode(rig):
    r = _running(
        rig,
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
             "after_seconds": 20},
        ],
    )
    r.run_for(30)
    assert r.mode == "Line Fill"

    r.press_stop()
    r.panel.is_running = False
    r.run_for(10)
    assert r.state == "idle"

    r.press_start()
    r.run_for(4)
    assert r.mode == "Warm Up"


# ---------------------------------------------------------------------------
# Stopping
# ---------------------------------------------------------------------------


def test_a_stop_commands_the_panel_and_lands_back_in_idle(rig):
    r = _running(rig)
    r.press_stop()
    r.tick(2)
    assert r.state == "stopping"
    assert CMD_STOP in r.panel.commanded()

    r.panel.is_running = False
    r.run_for(6)
    assert r.state == "idle"


def test_a_stop_is_re_sent_until_the_engine_stops(rig):
    """One dropped command must not leave a pump running with the app believing
    it asked."""
    r = _running(rig)
    r.press_stop()
    r.run_for(30)  # engine keeps running
    assert r.panel.commanded().count(CMD_STOP) > 1


def test_an_engine_that_will_not_stop_faults(rig):
    r = _running(rig, stop_timeout_seconds=30)
    r.press_stop()
    r.run_for(45)
    assert r.state == "fault"
    assert "did not stop" in r.app.autostart.fault_reason


def test_a_stop_is_not_a_failed_start(rig):
    """The distinction the power output and the tile both hang off: a start that
    gave up leaves an engine that never ran; this one may still be turning."""
    r = _running(rig, stop_timeout_seconds=30)
    r.press_stop()
    r.run_for(45)
    assert r.state == "fault"
    assert r.app.autostart.start_failed is False


def test_a_latched_cross_app_request_cannot_undo_an_operator_stop(rig):
    """An automated request that is still high when someone stops the pump must
    not restart it on the next loop. It is ignored until it goes low again."""
    r = _running(rig)
    r.app.tags.automation_enabled._value = True
    r.app.tags.run_request._value = True

    r.press_stop()
    r.panel.is_running = False
    r.run_for(20)
    assert r.state == "idle", "the stuck request must not have re-started it"

    # Once it drops and is raised again, it counts.
    r.app.tags.run_request._value = False
    r.tick(2)
    r.app.tags.run_request._value = True
    r.tick(2)
    assert r.state == "prestart_warning"


# ---------------------------------------------------------------------------
# What the dashboard says
# ---------------------------------------------------------------------------


def test_the_tile_says_starting_through_the_whole_attempt(rig):
    """The panel reports stopped for the countdown and the cranking, so its own
    state would read 'Stopped' for a minute after the operator pressed Start."""
    r = rig()
    r.press_start()
    r.run_for(10)
    assert r.status == "Pump 1: Starting"
    r.run_for(26)
    assert r.status == "Pump 1: Starting"


def test_the_tile_says_power_off_when_the_panel_is_dark_and_idle(rig):
    r = rig(panel=Panel(reachable=False))
    r.tick(3)
    assert r.status == "Pump 1: Power Off"


def test_the_mode_status_names_what_would_move_the_pump_on(rig):
    r = _running(
        rig,
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
             "after_seconds": 30},
        ],
    )
    # Running, so the wait is counted out rather than merely described.
    assert r.tags.run_mode_status.get().startswith("Warm Up → Line Fill after 30s (")
    r.run_for(40)
    assert r.tags.run_mode_status.get() == "Line Fill — no automatic change from here"


def test_the_run_is_narrated_into_the_activity_log(rig):
    r = _running(rig)
    r.press_stop()
    r.panel.is_running = False
    r.run_for(10)
    joined = " | ".join(r.messages)
    for expected in ("Pre-start warning", "Starting", "Running", "Stopping", "Ready"):
        assert expected in joined, expected


def test_a_warm_up_mode_gets_its_time_with_the_engine_actually_turning(rig):
    """Found by these tests (2026-08-27): the mode clock started when the *mode*
    was selected, at the pre-start warning. So "Warm Up for 30s" was satisfied by
    30s of countdown with the engine off, and the pump left warm-up the instant
    it caught — never warming up at all."""
    r = _running(
        rig,
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
             "after_seconds": 30},
        ],
    )
    # _running already spent ~38s getting here, all of it before the engine ran.
    assert r.mode == "Warm Up"
    assert r.panel.written(REG_REMOTE_RPM)[-1] == 900

    r.run_for(20)
    assert r.mode == "Warm Up", "still inside its 30s of running"
    r.run_for(14)
    assert r.mode == "Line Fill"


def test_the_mode_shown_during_the_warning_is_the_one_about_to_run(rig):
    """The clock restarts at `running`, but the selection still happens at the
    warning — so the dashboard says what the pump is about to do, rather than
    showing the last run's mode until the engine catches."""
    settings = dict(TWO_MODE_CONFIG)
    r = rig(config=build_config(**settings))
    r.pick_mode("Line Fill")
    r.tick(1)
    assert r.mode == "Line Fill"

    r.press_start()
    r.run_for(6)
    assert r.state == "prestart_warning"
    assert r.mode == "Warm Up"


def test_adoption_keeps_the_mode_and_clock_it_was_left_in(rig):
    """An app restart mid-run comes back as an adoption, which skips the warning
    entirely — so it must not reset to the start mode and drop a hot pump back
    to warm-up RPM."""
    settings = dict(TWO_MODE_CONFIG)
    r = rig(
        config=build_config(**settings),
        panel=Panel(is_running=True),
        stored={"run_mode": "line_fill"},  # what the process before it left
    )
    r.tick(3)

    assert r.state == "running"
    assert r.mode == "Line Fill"
    assert r.panel.written(REG_REMOTE_RPM)[-1] == 1300


# ---------------------------------------------------------------------------
# A Regulate run mode, driving speed from a real reading
# ---------------------------------------------------------------------------


PRESSURE_INPUT = {
    "ai_pin": 2,
    "sensor_label": "Discharge",
    "sensor_units": "kPa",
    "signal_type": "4-20 mA",
    "value_min": 0,
    "value_max": 1600,
    "setpoint_min": 0,
    "setpoint_max": 1500,
    "filter_seconds": 0,
    "allow_rpm_control": True,
}


def _regulating(rig_factory, setpoint=800, **overrides):
    settings = dict(
        analog_inputs=[PRESSURE_INPUT],
        run_modes=[
            {"label": "Pressure", "control": "Regulate", "variable": "Discharge",
             "setpoint": setpoint, "sensitivity": 8, "minimum_seconds": 0},
        ],
        start_in_mode="Pressure",
        include_default_fixed_run_mode=False,
    )
    settings.update(overrides)
    r = rig_factory(config=build_config(**settings))
    r.set_analog(2, 12.0)  # mid-scale, 800 kPa
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(6)
    assert r.state == "running"
    r.tick(1)
    return r


def test_a_regulate_mode_holds_its_setpoint_by_trimming_rpm(rig):
    """The loop only has RPM to work with, so 'holding' means the commanded
    speed moves in the direction that corrects the reading."""
    r = _regulating(rig, setpoint=1200)  # asking for more than the 800 it reads
    r.set_analog(2, 12.0)
    r.run_for(30)

    written = r.panel.written(REG_REMOTE_RPM)
    assert written, "the regulate loop should be commanding a speed"
    assert written[-1] > written[0], "reading below setpoint should speed it up"
    assert 900 <= written[-1] <= 2000


def test_a_regulate_mode_slows_down_when_the_reading_is_above_setpoint(rig):
    r = _regulating(rig, setpoint=400)
    r.set_analog(2, 12.0)  # 800 kPa, well above
    r.run_for(30)

    written = r.panel.written(REG_REMOTE_RPM)
    assert written[-1] < written[0]


def test_a_regulate_mode_names_what_it_is_holding(rig):
    r = _regulating(rig, setpoint=800)
    r.run_for(10)
    assert "Holding 800" in r.tags.regulate_status.get()
    assert r.tags.regulate_measured.get() == 800.0


def test_a_broken_loop_holds_the_rpm_rather_than_regulating_on_nonsense(rig):
    """An open-circuit 4-20 mA input reads below 4 mA. Regulating on that would
    drive the engine from a number that means 'the wire fell off'."""
    r = _regulating(rig, setpoint=1200)
    r.run_for(20)
    held = r.panel.written(REG_REMOTE_RPM)[-1]

    r.set_analog(2, 0.5)  # open circuit
    r.run_for(30)
    assert r.panel.written(REG_REMOTE_RPM)[-1] == held
    assert "sensor fault" in r.tags.regulate_status.get()


def test_a_sensor_fault_can_stop_the_engine_when_configured(rig):
    r = _regulating(
        rig, setpoint=1200,
        sensor_fault_action="Stop Engine", sensor_fault_grace_seconds=10,
    )
    r.set_analog(2, 0.5)
    r.run_for(40)

    assert CMD_STOP in r.panel.commanded()
    assert r.app._sensor_fault_stopped is True


def test_moving_between_regulate_modes_re_seeds_the_loop(rig):
    """Carrying the integrator across would spend the first seconds of the new
    mode unwinding the last one's commitment to a setpoint it no longer has."""
    r = _regulating(
        rig,
        run_modes=[
            {"label": "Low", "control": "Regulate", "variable": "Discharge",
             "setpoint": 400, "sensitivity": 8, "minimum_seconds": 0},
            {"label": "High", "control": "Regulate", "variable": "Discharge",
             "setpoint": 1200, "sensitivity": 8, "minimum_seconds": 0},
        ],
        start_in_mode="Low",
    )
    r.run_for(20)
    r.pick_mode("High")
    assert r.app._regulate_key is None
    r.run_for(20)
    assert r.mode == "High"
    assert "Holding 1200" in r.tags.regulate_status.get()


def test_a_sensor_transition_moves_a_mode_on_a_real_reading(rig):
    r = _regulating(
        rig,
        run_modes=[
            {"label": "Warm Up", "control": "Fixed", "target_rpm": 900,
             "minimum_seconds": 0},
            {"label": "Pressure", "control": "Regulate", "variable": "Discharge",
             "setpoint": 800, "sensitivity": 8, "minimum_seconds": 0},
        ],
        start_in_mode="Warm Up",
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Pressure", "when": "Sensor",
             "variable": "Discharge", "direction": "Above", "setpoint": 500,
             "sensor_hold_seconds": 10},
        ],
    )
    r.set_analog(2, 6.0)  # 200 kPa — below the trigger
    r.run_for(20)
    assert r.mode == "Warm Up"

    r.set_analog(2, 12.0)  # 800 kPa — above it
    r.run_for(6)
    assert r.mode == "Warm Up", "the hold has not elapsed yet"
    r.run_for(12)
    assert r.mode == "Pressure"


# ---------------------------------------------------------------------------
# Spoken announcements
# ---------------------------------------------------------------------------


def _speaking(rig_factory, **overrides):
    settings = dict(
        enable_announcements=True,
        announcement_speaker="ip_speaker_1",
        prestart_announcement="{name} starting shortly.",
        start_announcement="{name} starting now.",
        start_failed_announcement="{name} startup failed.",
        horn_output_pin=None,  # nothing to dodge, so timing is about the sequence
    )
    settings.update(overrides)
    return rig_factory(config=build_config(**settings))


def _said(rig_):
    return [s["text"] for s in rig_.spoken]


def test_the_warning_is_announced_and_repeats_while_it_runs(rig):
    r = _speaking(rig, prestart_warning_seconds=60)
    r.press_start()
    r.run_for(50)

    said = _said(r)
    assert said[0] == "Pump 1 starting shortly."
    assert len(said) >= 2, "a warning of unknown length repeats"
    assert all(s == "Pump 1 starting shortly." for s in said)


def test_the_crank_and_the_giving_up_are_both_announced(rig):
    r = _speaking(rig, start_timeout_seconds=40)
    r.press_start()
    r.run_for(34)
    assert "Pump 1 starting now." in _said(r)

    r.run_for(20)  # engine never runs
    assert r.state == "fault"
    assert _said(r)[-1] == "Pump 1 startup failed."


def test_a_successful_start_never_says_it_failed(rig):
    r = _speaking(rig)
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(20)

    assert r.state == "running"
    assert not any("failed" in s for s in _said(r))


def test_a_legacy_countdown_template_is_reworded_not_spoken_raw(rig):
    r = _speaking(rig, prestart_announcement="{name} starting in {seconds} seconds.")
    r.press_start()
    r.run_for(10)
    assert _said(r)[0] == "Pump 1 starting shortly."


def test_announcements_stay_silent_when_switched_off(rig):
    r = rig()  # announcements default off
    r.press_start()
    r.run_for(40)
    assert r.spoken == []


# ---------------------------------------------------------------------------
# Comms, and the panel disappearing
# ---------------------------------------------------------------------------


def test_losing_the_panel_mid_run_faults_rather_than_pretending(rig):
    r = rig()
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(6)
    assert r.state == "running"

    r.panel.reachable = False
    r.run_for(30)
    assert r.state == "fault"
    assert r.app.autostart.start_failed is False  # it ran; this is not a failed start


def test_a_run_recovers_when_the_panel_comes_back_still_running(rig):
    """A fault the engine disproves. Leaving a running pump unregulated pending a
    button press is worse than resuming control."""
    r = rig()
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(6)
    r.panel.reachable = False
    r.run_for(30)
    assert r.state == "fault"

    r.panel.reachable = True
    r.run_for(10)
    assert r.state == "running"


def test_a_start_can_begin_on_a_panel_that_has_never_answered(rig):
    """The warning is entirely app-side, and a dark panel is exactly when a start
    has to be able to begin — otherwise pressing Start does nothing at all."""
    r = rig(panel=Panel(reachable=False))
    r.press_start()
    r.run_for(10)

    assert r.state == "prestart_warning"
    assert r.beacon_on()
    assert r.status == "Pump 1: Starting"


# ---------------------------------------------------------------------------
# Digital outputs and pump power, around a run
# ---------------------------------------------------------------------------


def test_the_pump_power_output_is_held_for_the_run_and_released_after(rig):
    r = rig(panel_power_output_pin=7)
    r.tick(2)
    assert r.tags.pump_power_on.get() is False

    r.press_start()
    r.run_for(6)
    assert r.tags.pump_power_on.get() is True, "powered up to be started"

    r.run_for(28)
    r.panel.is_running = True
    r.run_for(6)
    assert r.tags.pump_power_on.get() is True

    r.press_stop()
    r.panel.is_running = False
    r.run_for(10)
    assert r.state == "idle"
    assert r.tags.pump_power_on.get() is False
    assert r.pin_history(7)[-1] is False


# ---------------------------------------------------------------------------
# Automatic starts and stops
# ---------------------------------------------------------------------------


def test_a_sensor_can_start_the_pump_and_it_still_gets_the_full_warning(rig):
    """An automated start feeds the same latch the button does, so it flows
    through the same warning. There is no quiet start."""
    r = rig(config=build_config(analog_inputs=[PRESSURE_INPUT]))
    r.cmds.values.update(
        {"start_control_mode": "sensor", "start_variable": "discharge",
         "start_setpoint_discharge": 400.0}
    )
    r.set_analog(2, 12.0)  # 800 kPa, above the 400 trigger (direction Below)
    r.run_for(10)
    assert r.state == "idle", "above the setpoint, so no demand"

    r.set_analog(2, 5.0)  # 100 kPa, below it
    r.run_for(6)
    assert r.state == "prestart_warning"
    assert r.beacon_on()
    assert CMD_START not in r.panel.commanded()


def test_a_sensor_can_stop_the_pump(rig):
    r = rig(config=build_config(analog_inputs=[PRESSURE_INPUT]))
    r.cmds.values.update(
        {"stop_control_mode": "sensor", "stop_variable": "discharge",
         "stop_setpoint_discharge": 600.0}
    )
    r.set_analog(2, 5.0)  # 100 kPa, below the stop trigger
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(6)
    assert r.state == "running"

    r.set_analog(2, 14.0)  # 1000 kPa, above it
    r.run_for(6)
    assert r.state == "stopping"
    assert CMD_STOP in r.panel.commanded()


def test_a_stop_trigger_beats_a_start_trigger(rig):
    """Both true at once is the ambiguous case, and the safe reading of it is to
    stop."""
    r = rig(config=build_config(analog_inputs=[PRESSURE_INPUT]))
    r.cmds.values.update(
        {"start_control_mode": "sensor", "start_variable": "discharge",
         "start_setpoint_discharge": 1400.0,  # below 1400 -> start
         "stop_control_mode": "sensor", "stop_variable": "discharge",
         "stop_setpoint_discharge": 100.0}   # above 100 -> stop
    )
    r.set_analog(2, 12.0)  # 800 kPa satisfies both
    r.run_for(10)
    assert r.state == "idle"


# ---------------------------------------------------------------------------
# The operator's own controls, mid-run
# ---------------------------------------------------------------------------


def test_changing_the_active_modes_rpm_applies_immediately(rig):
    r = _running(rig)
    before = r.panel.written(REG_REMOTE_RPM)[-1]
    assert before == 900

    run_handler(r, "mode_warm_up_target_rpm", 1500)
    assert r.cmds.values["mode_warm_up_target_rpm"] == 1500
    assert r.panel.written(REG_REMOTE_RPM)[-1] == 1500


def test_changing_an_inactive_modes_rpm_is_held_until_that_mode_runs(rig):
    """Every other mode's slider is a setting for later, not a command."""
    r = _running(rig)
    run_handler(r, "mode_line_fill_target_rpm", 1700)
    assert r.panel.written(REG_REMOTE_RPM)[-1] == 900, "still in Warm Up"

    r.pick_mode("Line Fill")
    r.tick(2)
    assert r.panel.written(REG_REMOTE_RPM)[-1] == 1700


def test_an_rpm_outside_the_configured_band_is_clamped(rig):
    """The panel ignores values outside its idle/overspeed band, so a slider that
    let one through would just make the engine disagree with the dashboard."""
    r = _running(rig)
    run_handler(r, "mode_warm_up_target_rpm", 4000)
    assert r.cmds.values["mode_warm_up_target_rpm"] == 2000
    run_handler(r, "mode_warm_up_target_rpm", 10)
    assert r.cmds.values["mode_warm_up_target_rpm"] == 900


def test_a_regulate_setpoint_is_clamped_to_its_own_sensors_bounds(rig):
    r = _regulating(rig)
    run_handler(r, "mode_pressure_setpoint", 9999.0, parser=float)
    assert r.cmds.values["mode_pressure_setpoint"] == 1500.0  # the sensor's max


def test_sensitivity_is_clamped_to_the_configured_scale(rig):
    r = _regulating(rig)
    run_handler(r, "mode_pressure_sensitivity", 99.0, parser=float)
    assert r.cmds.values["mode_pressure_sensitivity"] == 10.0


# ---------------------------------------------------------------------------
# Model-driven display
# ---------------------------------------------------------------------------


def test_the_panel_model_decides_how_many_flexible_sensors_exist(rig):
    """An E400 has seven flexible inputs, an E800 twelve. The tags, the UI and
    the read plan all size themselves from the configured model."""
    from dse_engine_controller.app_tags import flex_sensor_letters

    # One rig at a time: config elements are class-level declarations, so a
    # second config built in the same process overwrites the first's values.
    e400 = rig(dse_model="E400")
    assert flex_sensor_letters(e400.config) == "ABCDEFG"
    e400_alarm_registers = e400.app.model.unnamed_alarm_registers
    e800 = rig(dse_model="E800")
    assert flex_sensor_letters(e800.config) == "ABCDEFGHIJKL"
    assert e400_alarm_registers < e800.app.model.unnamed_alarm_registers


# ---------------------------------------------------------------------------
# Startup: the real setup(), on configs a site might actually save
# ---------------------------------------------------------------------------


def test_setup_runs_and_leaves_the_pump_ready(rig):
    """The production setup(), not the harness's reconstruction of it. Its
    ordering is load-bearing — the sequencer has to exist before the modes are
    seeded, and the seeding publishes a status that reports config problems."""
    r = rig(config=build_config(**TWO_MODE_CONFIG)).real_setup()
    r.tick(2)

    assert r.state == "idle"
    assert r.mode == "Warm Up"
    assert r.cmds.values["mode_warm_up_target_rpm"] == 900
    assert r.cmds.values["mode_line_fill_target_rpm"] == 1300
    assert r.app._run_mode_problems == []


def test_setup_survives_a_config_with_nothing_configured(rig):
    r = rig().real_setup()
    r.tick(2)
    assert [m.label for m in r.app.sequencer.modes] == ["Fixed RPM"]
    assert r.state == "idle"


def test_setup_reports_a_broken_run_mode_rather_than_refusing_to_start(rig):
    """A pump that won't come up because one mode names a missing sensor is worse
    than a pump that comes up and says so."""
    r = rig(
        config=build_config(
            run_modes=[
                {"label": "Bad", "control": "Regulate", "variable": "Nope",
                 "setpoint": 100, "minimum_seconds": 0},
            ],
            include_default_fixed_run_mode=False,
        )
    ).real_setup()
    r.tick(2)

    assert r.state == "idle", "still running"
    assert any("not a configured sensor" in p for p in r.app._run_mode_problems)
    assert "not a configured sensor" in r.tags.run_mode_status.get()


def test_setup_clears_a_fault_latched_by_a_previous_process(rig):
    r = rig()
    r.app.tags.auto_start_fault._value = "Auto Start Failed"
    r.app.tags.auto_start_fault_hidden._value = False
    r.real_setup()
    assert r.tags.auto_start_fault.get() == ""
    assert r.tags.auto_start_fault_hidden.get() is True


def test_setup_drives_the_warning_outputs_to_a_known_state(rig):
    """A beacon left energised by a crash mid-warning would otherwise stay on
    until the next start."""
    r = rig().real_setup()
    assert (3, False) in r.outputs  # beacon
    assert (2, False) in r.outputs  # horn


def test_setup_flags_a_start_budget_that_can_never_start_anything(caplog, rig):
    import logging

    with caplog.at_level(logging.ERROR):
        rig(prestart_warning_seconds=60, start_timeout_seconds=30).real_setup()
    assert "will fault the moment it ends" in caplog.text


def test_setup_flags_an_analog_pin_wired_to_the_wrong_kind_of_input(rig):
    """A 4-20 mA sensor on a 0-10 V input reads plausible nonsense rather than
    failing, which is exactly the kind of mistake worth naming at boot."""
    voltage_pin = dict(PRESSURE_INPUT, ai_pin=0)
    r = rig(config=build_config(analog_inputs=[voltage_pin])).real_setup()
    assert any("mA" in p or "V" in p for p in r.app._analog_config_problems)
    assert r.tags.analog_fault_string.get() != ""


def test_setup_clears_tag_keys_left_by_older_versions(rig):
    """Renamed and removed elements linger in the aggregate otherwise, and a
    stale `rpm_mode` would sit alongside the `run_mode` that replaced it."""
    from dse_engine_controller.application import STALE_TAG_KEYS

    r = rig()
    r.app.tag_manager.tags.update({"rpm_mode": "fixed", "target_rpm": 1200})
    r.real_setup()

    assert set(r.app.tag_manager.cleared) <= set(STALE_TAG_KEYS)
    assert "rpm_mode" in r.app.tag_manager.cleared
    assert "target_rpm" in r.app.tag_manager.cleared


def test_a_full_run_works_after_the_real_setup(rig):
    """End to end on the real startup path: press Start, warn, prepare, crank,
    hold the mode's RPM, stop, back to idle."""
    r = rig(config=build_config(**TWO_MODE_CONFIG)).real_setup()
    r.press_start()
    r.run_for(32)
    assert r.state == "starting"
    r.panel.is_running = True
    r.run_for(6)

    assert r.state == "running"
    assert r.mode == "Warm Up"
    assert r.panel.written(REG_REMOTE_RPM)[-1] == 900

    r.press_stop()
    r.panel.is_running = False
    r.run_for(10)
    assert r.state == "idle"


def test_the_mode_status_counts_out_a_wait_in_progress(rig):
    """Maxitool (2026-08-27): the status never changed from its opening message.
    "when Inlet Pressure above 0.5 for 120s" reads identically whether the
    pressure has been there for two minutes or has never been seen — which is
    exactly the moment somebody wants to know which of those it is."""
    r = _regulating(
        rig,
        run_modes=[
            {"label": "Warm Up", "control": "Fixed", "target_rpm": 900,
             "minimum_seconds": 0},
            {"label": "Line Fill", "control": "Fixed", "target_rpm": 1300,
             "minimum_seconds": 0},
        ],
        start_in_mode="Warm Up",
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Sensor",
             "variable": "Discharge", "direction": "Above", "setpoint": 500,
             "sensor_hold_seconds": 60},
        ],
    )
    r.set_analog(2, 6.0)  # 200 kPa — the condition is not met
    r.run_for(10)
    assert r.tags.run_mode_status.get() == (
        "Warm Up → Line Fill when Discharge above 500 for 60s"
    )

    r.set_analog(2, 12.0)  # 800 kPa — now it is, and the hold starts counting
    r.run_for(20)
    status = r.tags.run_mode_status.get()
    assert "met for" in status and "of 60s" in status, status

    # And it falls back to describing the condition when the reading drops away.
    r.set_analog(2, 6.0)
    r.run_for(6)
    assert "met for" not in r.tags.run_mode_status.get()


def test_a_timer_transition_shows_how_far_through_it_is(rig):
    r = _running(
        rig,
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
             "after_seconds": 60},
        ],
    )
    r.run_for(20)
    status = r.tags.run_mode_status.get()
    assert "after 60s (" in status and "s so far)" in status, status


def test_a_stopped_pump_describes_rather_than_counts(rig):
    """Nothing is being watched, so a countdown would be describing something
    that is not happening."""
    settings = dict(TWO_MODE_CONFIG)
    settings["run_mode_transitions"] = [
        {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
         "after_seconds": 60},
    ]
    r = rig(config=build_config(**settings))
    r.run_for(30)
    assert r.state == "idle"
    assert r.tags.run_mode_status.get() == "Warm Up → Line Fill after 60s"


def test_the_tile_names_the_mode_through_a_run(rig):
    r = _running(rig)
    assert r.status == "Pump 1: Running (Warm Up)"
    r.pick_mode("Line Fill")
    r.tick(2)
    assert r.status == "Pump 1: Running (Line Fill)"


def test_the_tile_leaves_the_mode_off_a_single_mode_install(rig):
    r = rig()  # nothing configured, so just the one Fixed RPM mode
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(8)
    assert r.state == "running"
    assert r.status == "Pump 1: Running"


# ---------------------------------------------------------------------------
# Transition thresholds an operator may move
# ---------------------------------------------------------------------------


def test_a_timer_threshold_can_be_moved_from_the_dashboard(rig):
    """Config says the rule is a timer; the operator says how long."""
    r = _running(
        rig,
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
             "after_seconds": 300, "adjustable": True},
        ],
    )
    assert r.cmds.values["transition_warm_up_to_line_fill_after"] == 300.0

    run_transition(r, "transition_warm_up_to_line_fill_after", 30)
    r.run_for(20)
    assert r.mode == "Warm Up"
    r.run_for(20)
    assert r.mode == "Line Fill", "moved on at the operator's 30s, not the configured 300"


def test_a_sensor_threshold_can_be_moved_from_the_dashboard(rig):
    r = _regulating(
        rig,
        run_modes=[
            {"label": "Warm Up", "control": "Fixed", "target_rpm": 900,
             "minimum_seconds": 0},
            {"label": "Line Fill", "control": "Fixed", "target_rpm": 1300,
             "minimum_seconds": 0},
        ],
        start_in_mode="Warm Up",
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Sensor",
             "variable": "Discharge", "direction": "Above", "setpoint": 1400,
             "adjustable": True},
        ],
    )
    r.set_analog(2, 12.0)  # 800 kPa — under the configured 1400
    r.run_for(20)
    assert r.mode == "Warm Up"

    run_transition(r, "transition_warm_up_to_line_fill_setpoint", 500.0)
    r.run_for(10)
    assert r.mode == "Line Fill"


def test_the_status_line_quotes_the_operators_number(rig):
    """Otherwise it reads as contradicting the slider sitting next to it."""
    r = _running(
        rig,
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
             "after_seconds": 300, "adjustable": True},
        ],
    )
    assert "after 300s" in r.tags.run_mode_status.get()
    run_transition(r, "transition_warm_up_to_line_fill_after", 45)
    assert "after 45s" in r.tags.run_mode_status.get()


def test_a_threshold_is_clamped_to_its_sensors_bounds(rig):
    r = _regulating(
        rig,
        run_modes=[
            {"label": "Warm Up", "control": "Fixed", "target_rpm": 900,
             "minimum_seconds": 0},
            {"label": "Line Fill", "control": "Fixed", "target_rpm": 1300,
             "minimum_seconds": 0},
        ],
        start_in_mode="Warm Up",
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Sensor",
             "variable": "Discharge", "direction": "Above", "setpoint": 500,
             "adjustable": True},
        ],
    )
    run_transition(r, "transition_warm_up_to_line_fill_setpoint", 99999.0)
    assert r.cmds.values["transition_warm_up_to_line_fill_setpoint"] == 1500.0


def test_a_rule_left_unadjustable_offers_no_control(rig):
    r = _running(
        rig,
        run_mode_transitions=[
            {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
             "after_seconds": 30},
        ],
    )
    assert not any(k.startswith("transition_") for k in r.cmds.values)


def test_the_slider_lives_with_the_mode_the_rule_applies_in(rig):
    """It matters while you are in that mode, so that is where it belongs."""
    settings = dict(TWO_MODE_CONFIG)
    settings["run_mode_transitions"] = [
        {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
         "after_seconds": 60, "adjustable": True},
    ]
    ui = _built_ui_for(build_config(**settings))
    panel = ui._find_element("mode_warm_up_panel")
    assert "transition_warm_up_to_line_fill_after" in panel._children
    slider = ui._find_element("transition_warm_up_to_line_fill_after")
    assert slider.display_name == "Line Fill after"
    assert (slider.min_val, slider.max_val) == (0.0, 120.0)  # a band around 60
    assert slider.units == "s"


def test_a_rule_that_applies_everywhere_sits_outside_any_mode(rig):
    """A blank From Mode has no one mode to belong to."""
    settings = dict(TWO_MODE_CONFIG)
    settings["run_mode_transitions"] = [
        {"from_mode": "", "to_mode": "Line Fill", "when": "Timer",
         "after_seconds": 60, "adjustable": True},
    ]
    ui = _built_ui_for(build_config(**settings))
    assert "transition_any_to_line_fill_after" in ui._find_element("rpm_tab")._children


def test_two_rules_between_the_same_modes_get_their_own_controls(rig):
    """Unusual but legal — a sensor and a timer racing each other. The second
    must not silently share the first one's dialled-in number."""
    settings = dict(TWO_MODE_CONFIG)
    settings["run_mode_transitions"] = [
        {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
         "after_seconds": 60, "adjustable": True},
        {"from_mode": "Warm Up", "to_mode": "Line Fill", "when": "Timer",
         "after_seconds": 90, "adjustable": True},
    ]
    r = rig(config=build_config(**settings))
    assert r.cmds.values["transition_warm_up_to_line_fill_after"] == 60.0
    assert r.cmds.values["transition_warm_up_to_line_fill_2_after"] == 90.0


# ---------------------------------------------------------------------------
# Configurable digital outputs
# ---------------------------------------------------------------------------


OUTPUT_CONFIG = dict(
    digital_outputs=[
        {"do_pin": 5, "label": "Clutch", "default_off_state": False,
         "default_running_state": True},
        {"do_pin": 6, "label": "Discharge Valve", "default_off_state": True,
         "default_running_state": True},
    ],
    run_modes=[
        {"label": "Warm Up", "control": "Fixed", "target_rpm": 900,
         "minimum_seconds": 0,
         "outputs": [{"output": "Clutch", "state": False}]},
        {"label": "Line Fill", "control": "Fixed", "target_rpm": 1300,
         "minimum_seconds": 0,
         "outputs": [{"output": "Clutch", "state": True},
                     {"output": "Discharge Valve", "state": False}]},
    ],
    start_in_mode="Warm Up",
    include_default_fixed_run_mode=False,
)


def _with_outputs(rig_factory, **overrides):
    settings = dict(OUTPUT_CONFIG)
    settings.update(overrides)
    return rig_factory(config=build_config(**settings))


def test_outputs_rest_at_their_configured_default_when_nothing_runs(rig):
    """A resting state is only meaningful if something actually returns to it."""
    r = _with_outputs(rig)
    r.tick(2)
    assert r.output("Clutch") is False
    assert r.output("Discharge Valve") is True
    assert (5, False) in r.outputs and (6, True) in r.outputs


def test_a_run_mode_drives_the_outputs_it_names(rig):
    r = _with_outputs(rig)
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(8)
    r.tick(1)
    assert r.output("Clutch") is False, "Warm Up asks for it off"

    r.pick_mode("Line Fill")
    r.tick(2)
    assert r.output("Clutch") is True
    assert r.output("Discharge Valve") is False
    assert r.pin_history(5)[-1] is True


def test_an_output_a_mode_leaves_out_returns_to_its_default(rig):
    """A mode says what the outputs are, rather than changing some and inheriting
    the rest from whichever mode happened to run before it."""
    r = _with_outputs(rig)
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(8)
    r.pick_mode("Line Fill")
    r.tick(2)
    assert r.output("Discharge Valve") is False  # Line Fill names it

    r.pick_mode("Warm Up")  # which doesn't
    r.tick(2)
    assert r.output("Discharge Valve") is True, "back to its default, not Line Fill's"


def test_outputs_return_to_rest_when_the_pump_stops(rig):
    r = _with_outputs(rig)
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(8)
    r.pick_mode("Line Fill")
    r.tick(2)
    assert r.output("Clutch") is True

    r.press_stop()
    r.panel.is_running = False
    r.run_for(10)
    assert r.state == "idle"
    assert r.output("Clutch") is False
    assert r.output("Discharge Valve") is True


def test_outputs_stay_at_rest_through_the_warning(rig):
    """A mode only drives outputs with the engine turning. Energising a clutch
    through the pre-start warning would be a surprise nobody asked for."""
    r = _with_outputs(
        rig,
        run_modes=[
            {"label": "Warm Up", "control": "Fixed", "target_rpm": 900,
             "minimum_seconds": 0,
             "outputs": [{"output": "Clutch", "state": True}]},
        ],
        start_in_mode="Warm Up",
    )
    r.press_start()
    r.run_for(20)
    assert r.state == "prestart_warning"
    assert r.output("Clutch") is False

    r.run_for(14)
    r.panel.is_running = True
    r.run_for(8)
    r.tick(1)
    assert r.output("Clutch") is True


def test_an_output_state_is_not_rewritten_every_poll(rig):
    """Writing the same value at every poll is noise on the IO bus and in the
    log, and would bury the changes that matter."""
    r = _with_outputs(rig)
    r.tick(10)
    assert r.pin_history(5) == [False], "written once, then left alone"


def test_a_mode_naming_an_output_that_does_not_exist_is_ignored_loudly(rig, caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        _with_outputs(
            rig,
            run_modes=[
                {"label": "Warm Up", "control": "Fixed", "target_rpm": 900,
                 "minimum_seconds": 0,
                 "outputs": [{"output": "Nonexistent", "state": True}]},
            ],
            start_in_mode="Warm Up",
        )
    assert "not a configured digital output" in caplog.text


def test_output_names_are_matched_as_forgivingly_as_everything_else(rig):
    r = _with_outputs(
        rig,
        run_modes=[
            {"label": "Warm Up", "control": "Fixed", "target_rpm": 900,
             "minimum_seconds": 0,
             "outputs": [{"output": "dischargevalve", "state": False}]},
        ],
        start_in_mode="Warm Up",
    )
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(8)
    r.tick(1)
    assert r.output("Discharge Valve") is False


def test_an_output_sharing_a_warning_pin_is_flagged(rig, caplog):
    """Both would drive it, on different schedules, and the pump would flicker
    its beacon in time with somebody's clutch."""
    import logging

    with caplog.at_level(logging.ERROR):
        _with_outputs(
            rig,
            digital_outputs=[{"do_pin": 3, "label": "Clutch"}],  # the beacon pin
        )
    assert "also the Beacon Output Pin" in caplog.text


def test_the_outputs_appear_between_the_sensors_and_the_control_tabs(rig):
    settings = dict(OUTPUT_CONFIG)
    settings["analog_inputs"] = [PRESSURE_INPUT]
    ui = _built_ui_for(build_config(**settings))

    sensor = ui._find_element("sensor_discharge")
    clutch = ui._find_element("output_clutch")
    valve = ui._find_element("output_discharge_valve")
    control = ui._find_element("control")

    assert clutch.to_dict()["currentValue"] == "$tag.app().output_pin5:boolean:false"
    # Straight after the sensors, in configured order, and above Pump Control.
    assert sensor.position < clutch.position < valve.position < control.position


def test_output_names_are_capitalised_for_display(rig):
    """Only where a word has no capitals of its own, so an acronym survives."""
    from dse_engine_controller.outputs import output_specs

    outputs = output_specs(
        build_config(
            digital_outputs=[
                {"do_pin": 5, "label": "clutch"},
                {"do_pin": 6, "label": "discharge valve"},
                {"do_pin": 7, "label": "PTO valve"},
                {"do_pin": 8, "label": "Already Fine"},
            ]
        )
    )
    assert [o.label for o in outputs] == [
        "Clutch", "Discharge Valve", "PTO Valve", "Already Fine",
    ]
    # The name a run mode refers to it by is unchanged by any of that.
    assert [o.sanitised for o in outputs][:2] == ["clutch", "discharge_valve"]


def test_an_output_keeps_its_tag_when_the_list_is_reordered(rig):
    """Keyed by pin, so re-ordering the config can't move one output's history
    onto another — the same rule the analog inputs follow."""
    from dse_engine_controller.outputs import output_specs

    forward = output_specs(build_config(**OUTPUT_CONFIG))
    reversed_cfg = dict(OUTPUT_CONFIG)
    reversed_cfg["digital_outputs"] = list(reversed(OUTPUT_CONFIG["digital_outputs"]))
    backward = output_specs(build_config(**reversed_cfg))

    assert {o.label: o.tag_name for o in forward} == {
        o.label: o.tag_name for o in backward
    }


def test_an_output_follows_a_mode_change_on_the_same_loop(rig):
    """The selection is written to a ui_cmd whose local copy only refreshes when
    the aggregate echoes back, so anything re-reading it gets the previous mode.
    The outputs are driven from the main loop and cannot be handed the new mode,
    so they would lag it by at least a poll — a clutch a couple of seconds late."""
    r = _with_outputs(rig)
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(8)
    r.tick(1)
    assert r.output("Clutch") is False

    # A ui_cmds manager that behaves like the real one: writes go out, the local
    # copy stays stale until echo().
    class _Lagging:
        def __init__(self, inner):
            self.values, self.pending = inner.values, {}

        async def set_value(self, key, value, log_update=True):
            self.pending[key] = value

        def get_value(self, key):
            return self.values.get(key)

    r.app.ui_manager = _Lagging(r.cmds)
    r.pick_mode("Line Fill")
    r.tick(1)
    assert r.output("Clutch") is True, "must not wait for the aggregate to echo"


def test_two_outputs_on_one_pin_is_reported(rig, caplog):
    """One pin has one state, so the second definition wins outright and the
    first's default and per-mode settings do nothing — silently, since both still
    publish a tag of their own."""
    import logging

    with caplog.at_level(logging.ERROR):
        _with_outputs(
            rig,
            digital_outputs=[
                {"do_pin": 5, "label": "Clutch", "default_off_state": False},
                {"do_pin": 5, "label": "Valve", "default_off_state": True},
            ],
        )
    assert "both on pin 5" in caplog.text
    assert "Only 'Valve' will have any effect" in caplog.text


def test_a_mode_only_names_outputs_it_wants_differently(rig):
    """The running default is what a mode falls back to, so a clutch engaged for
    most of a run is configured once rather than repeated in every mode."""
    r = _with_outputs(
        rig,
        run_modes=[
            {"label": "Run", "control": "Fixed", "target_rpm": 1300,
             "minimum_seconds": 0},  # names nothing at all
        ],
        start_in_mode="Run",
        include_default_fixed_run_mode=False,
    )
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(8)
    r.tick(1)
    assert r.output("Clutch") is True, "the running default, not the off one"
    assert r.output("Discharge Valve") is True


def test_the_off_state_holds_until_the_engine_actually_turns(rig):
    """Run modes have no say before then — nothing is running for them to be a
    mode of — so the warning and the cranking sit at the off state."""
    r = _with_outputs(
        rig,
        run_modes=[
            {"label": "Run", "control": "Fixed", "target_rpm": 1300,
             "minimum_seconds": 0, "outputs": [{"output": "Clutch", "state": True}]},
        ],
        start_in_mode="Run",
        include_default_fixed_run_mode=False,
    )
    r.press_start()
    r.run_for(20)
    assert r.state == "prestart_warning" and r.output("Clutch") is False
    r.run_for(14)
    assert r.state == "starting" and r.output("Clutch") is False


def test_outputs_hold_through_cooldown_and_drop_when_the_engine_does(rig):
    """The engine is still turning during cooldown. Dropping a clutch the instant
    Stop is pressed would dump the load at speed — which is exactly what the old
    built-in clutch's disengage delay existed to avoid."""
    r = _with_outputs(rig)
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(8)
    r.pick_mode("Line Fill")
    r.tick(2)
    assert r.output("Clutch") is True

    r.press_stop()
    r.tick(2)
    assert r.state == "stopping"
    assert r.output("Clutch") is True, "still turning, so still engaged"

    r.panel.is_running = False
    r.run_for(8)
    assert r.state == "idle"
    assert r.output("Clutch") is False


def test_the_manual_fixed_rpm_mode_leaves_the_outputs_alone(rig):
    """Taking manual control of engine speed is not asking for the clutch to
    drop. The mode names nothing, so everything sits at its running default."""
    r = _with_outputs(
        rig,
        run_modes=[
            {"label": "Line Fill", "control": "Fixed", "target_rpm": 1300,
             "minimum_seconds": 0, "outputs": [{"output": "Clutch", "state": True}]},
        ],
        start_in_mode="Line Fill",
        include_default_fixed_run_mode=True,
    )
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(8)
    r.tick(1)
    assert r.output("Clutch") is True

    r.pick_mode("Fixed RPM")
    r.tick(2)
    assert r.mode == "Fixed RPM"
    assert r.output("Clutch") is True, "manual speed control must not dump the load"


def test_a_mode_can_still_turn_a_running_default_off(rig):
    r = _with_outputs(
        rig,
        run_modes=[
            {"label": "Run", "control": "Fixed", "target_rpm": 1300,
             "minimum_seconds": 0, "outputs": [{"output": "Clutch", "state": False}]},
        ],
        start_in_mode="Run",
        include_default_fixed_run_mode=False,
    )
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(8)
    r.tick(1)
    assert r.output("Clutch") is False, "the mode overrides the running default"


# ---------------------------------------------------------------------------
# Transitions that stop the engine
# ---------------------------------------------------------------------------


def _shutdown_rig(rig_factory, **transition):
    rule = {"from_mode": "Run", "to_mode": "shutdown", "when": "Timer",
            "after_seconds": 30}
    rule.update(transition)
    return rig_factory(
        config=build_config(
            analog_inputs=[PRESSURE_INPUT],
            run_modes=[{"label": "Run", "control": "Fixed", "target_rpm": 1300,
                        "minimum_seconds": 0}],
            start_in_mode="Run",
            include_default_fixed_run_mode=False,
            run_mode_transitions=[rule],
        )
    )


def _run_until_running(r):
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(8)
    r.tick(1)
    assert r.state == "running"
    return r


def test_a_transition_to_shutdown_stops_the_engine(rig):
    r = _run_until_running(_shutdown_rig(rig))
    assert r.state == "running"

    r.run_for(34)
    assert r.state == "stopping"
    assert CMD_STOP in r.panel.commanded()

    r.panel.is_running = False
    r.run_for(8)
    assert r.state == "idle"


def test_a_shutdown_gets_the_ordinary_cooldown_not_a_direct_command(rig):
    """Going through the run latch rather than commanding the panel means it
    gets the same cooldown, timeout and path back to idle as any other stop."""
    r = _run_until_running(_shutdown_rig(rig))
    r.run_for(34)
    assert r.state == "stopping", "cooldown, not straight to idle"
    assert r.app._operator_run_latch is False


def test_a_shutdown_says_why_in_the_activity_log(rig):
    r = _run_until_running(_shutdown_rig(rig))
    r.run_for(34)
    assert "Pump 1: shutting down (after 30s)" in r.messages


def test_a_stuck_run_request_cannot_undo_a_shutdown(rig):
    """Exactly as when an operator presses Stop: a cross-app request still high
    must not restart the pump on the next loop."""
    r = _run_until_running(_shutdown_rig(rig))
    r.app.tags.automation_enabled._value = True
    r.app.tags.run_request._value = True

    r.run_for(34)
    r.panel.is_running = False
    r.run_for(20)
    assert r.state == "idle"


def test_a_sensor_can_shut_the_engine_down(rig):
    """Any condition can, since shutdown is a destination rather than a fourth
    kind of rule."""
    r = _shutdown_rig(
        rig, when="Sensor", variable="Discharge", direction="Below",
        setpoint=300, sensor_hold_seconds=10,
    )
    r.set_analog(2, 12.0)  # 800 kPa, above the trigger
    _run_until_running(r)
    r.run_for(20)
    assert r.state == "running"

    r.set_analog(2, 5.0)  # 100 kPa — the discharge has collapsed
    r.run_for(6)
    assert r.state == "running", "the hold has not elapsed"
    r.run_for(12)
    assert r.state == "stopping"


def test_a_shutdown_rule_can_apply_in_every_mode(rig):
    r = _shutdown_rig(rig, from_mode="")
    _run_until_running(r)
    r.run_for(34)
    assert r.state == "stopping"


def test_the_status_line_names_the_shutdown(rig):
    r = _run_until_running(_shutdown_rig(rig))
    assert r.tags.run_mode_status.get().startswith("Run → Shutdown after 30s (")


def test_a_shutdown_threshold_can_be_adjustable(rig):
    r = _run_until_running(_shutdown_rig(rig, after_seconds=600, adjustable=True))
    assert r.cmds.values["transition_run_to_shutdown_after"] == 600.0
    run_transition(r, "transition_run_to_shutdown_after", 30)
    r.run_for(34)
    assert r.state == "stopping"


def test_the_mode_is_left_alone_by_a_shutdown(rig):
    """The pump is stopping, not changing how it runs."""
    r = _run_until_running(_shutdown_rig(rig))
    r.run_for(34)
    assert r.mode == "Run"


def test_the_mode_status_is_hidden_when_it_could_only_say_one_thing(rig):
    """A single mode with nothing that could move it makes the line furniture —
    "Fixed RPM — no automatic change from here", forever."""
    r = rig()  # nothing configured, so just the one Fixed RPM mode
    r.tick(2)
    assert r.tags.run_mode_status_hidden.get() is True


def test_the_mode_status_is_shown_as_soon_as_there_is_a_choice(rig):
    r = rig(config=build_config(**TWO_MODE_CONFIG))
    r.tick(2)
    assert r.tags.run_mode_status_hidden.get() is False


def test_the_mode_status_is_shown_for_a_lone_mode_that_can_shut_down(rig):
    """One mode, but a rule that can stop the engine — so there is something to
    report after all."""
    r = rig(
        config=build_config(
            run_modes=[{"label": "Run", "control": "Fixed", "target_rpm": 1300,
                        "minimum_seconds": 0}],
            start_in_mode="Run",
            include_default_fixed_run_mode=False,
            run_mode_transitions=[{"from_mode": "Run", "to_mode": "shutdown",
                                   "when": "Timer", "after_seconds": 600}],
        )
    )
    r.tick(2)
    assert r.tags.run_mode_status_hidden.get() is False
    assert "Shutdown" in r.tags.run_mode_status.get()


def test_a_config_problem_is_never_hidden(rig):
    """The one case where a single-mode install has something worth reading."""
    r = rig(
        config=build_config(
            run_modes=[{"label": "Bad", "control": "Regulate", "variable": "Nope",
                        "setpoint": 1, "minimum_seconds": 0}],
            include_default_fixed_run_mode=False,
        )
    )
    r.tick(2)
    assert r.app._run_mode_problems
    assert r.tags.run_mode_status_hidden.get() is False
    assert "not a configured sensor" in r.tags.run_mode_status.get()


def test_the_status_element_is_bound_to_the_hidden_flag(rig):
    ui = _built_ui_for(build_config())
    emitted = ui._find_element("run_mode_status_text").to_dict()["hidden"]
    assert emitted.startswith("$tag.app().run_mode_status_hidden")


def test_the_mode_picker_is_hidden_when_there_is_nothing_to_pick(rig):
    """One mode means a dropdown with one option — a control with nothing to
    control."""
    r = rig()  # nothing configured, so just the one Fixed RPM mode
    r.tick(2)
    assert r.tags.run_mode_hidden.get() is True


def test_the_mode_picker_appears_as_soon_as_there_is_a_choice(rig):
    r = rig(config=build_config(**TWO_MODE_CONFIG))
    r.tick(2)
    assert r.tags.run_mode_hidden.get() is False


def test_the_mode_picker_stays_hidden_in_manual_however_many_modes(rig):
    """The panel owns engine speed there, so none of these controls can act."""
    r = rig(config=build_config(**TWO_MODE_CONFIG))
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.panel.is_auto = False  # running under the panel's own control
    r.run_for(8)
    r.tick(1)
    assert r.tags.run_mode_hidden.get() is True


def test_a_default_install_shows_neither_picker_nor_mode_status(rig):
    """With one mode and nothing that could move it, the RPM tab is just the
    speed control — no picker offering one option, no line that never changes."""
    r = rig()
    r.tick(2)
    assert r.tags.run_mode_hidden.get() is True
    assert r.tags.run_mode_status_hidden.get() is True


# --- Cancelling the Start Now command itself -----------------------------
#
# The site offers two ways to abort a start: the Cancel Start button, and the
# cancel on the in-flight Start Now command. The second is a patch to that
# command's own message rather than a new command, so it reaches the app
# through the handler's context — but to the operator both are the same act,
# and these pin them to the same behaviour.


def test_cancelling_the_start_command_aborts_the_warning(rig):
    r = rig()
    r.press_start()
    r.run_for(10)
    assert r.state == "prestart_warning"

    r.cancel_start_command()
    r.tick(2)

    assert r.state == "idle"
    assert r.panel.commanded() == []
    assert not r.beacon_on() and not r.horn_on()


def test_cancelling_the_start_command_matches_the_cancel_button(rig):
    """Both affordances must leave the pump in the same place."""
    button = rig()
    button.press_start()
    button.run_for(10)
    button.press_cancel()
    button.tick(2)

    command = rig()
    command.press_start()
    command.run_for(10)
    command.cancel_start_command()
    command.tick(2)

    assert command.state == button.state == "idle"
    assert command.panel.commanded() == button.panel.commanded() == []


def test_cancelling_after_the_engine_is_running_does_not_stop_it(rig):
    """A dismissed spinner must not become a shutdown.

    Past the warning the START has gone to the panel and the engine is turning.
    Stopping it is Stop Now's job, behind its own confirmation.
    """
    r = rig()
    r.press_start()
    r.run_for(32)
    r.panel.is_running = True
    r.run_for(10)
    assert r.state == "running"

    r.cancel_start_command()
    r.run_for(4)

    assert r.state == "running"
    assert r.app._operator_run_latch


def test_a_cancelled_start_says_so_in_the_activity_log(rig):
    r = rig()
    r.press_start()
    r.run_for(10)
    r.cancel_start_command(by={"name": "Tom"})

    logged = " ".join(
        str(m) for m in r.messages_on("activity_logs")
    )
    assert "start cancelled" in logged
    assert "Tom" in logged


def test_the_cancel_button_still_aborts_once_the_engine_is_cranking(rig):
    """The button and the command-cancel are deliberately not symmetric.

    Cancel Start is only *shown* during the warning, so pressing it is an
    explicit "abort this start" and is honoured wherever it lands — including a
    press that arrives just after the state moved on. The cancel on the pending
    command is offered for the whole attempt, so it is the one that has to
    refuse (see the test below).
    """
    r = rig()
    r.press_start()
    r.run_for(34)
    assert r.state == "starting"

    r.press_cancel()
    r.run_for(2)

    assert not r.app._operator_run_latch


def test_the_command_cancel_refuses_once_the_engine_is_cranking(rig):
    """Dismissing a spinner must not become a shutdown."""
    r = rig()
    r.press_start()
    r.run_for(34)
    assert r.state == "starting"

    r.cancel_start_command()
    r.run_for(2)

    assert r.app._operator_run_latch
    assert r.state in ("starting", "running")


def test_the_real_site_cancel_payload_aborts_the_start(rig):
    """End to end through pydoover's own RPC layer, not a stand-in context.

    Every other test here fakes the context. This one builds a real
    ``RPCContext``, routes the exact payload the site writes when an operator
    cancels a pending command (``handleCancelCommand`` — a terminal ``error``
    carrying ``cancelled_at`` / ``cancelled_by``, not a status code of its own),
    and checks the pump actually stands down. That seam is the whole reason the
    cancel never worked, so it is worth exercising for real.
    """
    from types import SimpleNamespace

    from pydoover.models.data import ChannelID, Message, MessageUpdateEvent
    from pydoover.rpc import RPCContext, RPCManager

    r = rig()
    r.press_start()
    r.run_for(10)
    assert r.state == "prestart_warning"

    channel = ChannelID(agent_id=1, name="ui_cmds")
    command = Message(
        id=100,
        author_id=7,
        channel=channel,
        data={
            "type": "rpc",
            "method": "start_now",
            "request": {},
            "status": {"code": "sent"},
            "response": {},
        },
        attachments=[],
    )
    ctx = RPCContext(
        method="start_now",
        message=command,
        _update_fn=None,
        _handler=None,
    )

    manager = RPCManager(SimpleNamespace(app_key="dse_engine_controller_1"))
    # What RPCManager._handle_request does while a handler is running.
    manager._inflight[command.id] = ctx

    actor = {"id": 7, "name": "Tom Wyatt", "email": "tom@span-eng.com"}
    manager._handle_response(
        MessageUpdateEvent(
            channel=channel,
            author_id=7,
            organisation_id=1,
            message=Message(
                id=command.id,
                author_id=7,
                channel=channel,
                data={
                    "status": {
                        "code": "error",
                        "message": {
                            "info": "Command cancelled",
                            "cancelled_at": 1787812081613,
                            "cancelled_by": actor,
                        },
                    },
                    "response": {},
                },
                attachments=[],
            ),
            request_data={},
        )
    )

    assert ctx.cancelled, "the site's payload must read as a cancellation"
    assert ctx.cancelled_by == actor

    drive(r.app._on_start_command_cancelled(ctx))
    r.tick(2)

    assert r.state == "idle"
    assert r.panel.commanded() == []
    assert not r.beacon_on() and not r.horn_on()
    assert "Tom Wyatt" in " ".join(r.messages_on("activity_logs"))
