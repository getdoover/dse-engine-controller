"""Tests for the configurable run modes and the transitions between them.

Phase 1: parsing config, validating it, and the sequencer's decision logic.
Nothing here touches the main loop — the sequencer is pure by design, so all of
it is exercisable without a panel, a clock or an app.
"""

from types import SimpleNamespace

from dse_engine_controller.run_modes import (
    CONTROL_FIXED,
    DEFAULT_MODE_LABEL,
    WHEN_PANEL,
    WHEN_SENSOR,
    WHEN_TIMER,
    ModeSequencer,
    RunMode,
    RunTransition,
    build_modes,
    build_sequencer,
    build_transitions,
    validate,
)


# ---------------------------------------------------------------------------
# Config stubs, shaped the way pydoover presents an Array of Objects
# ---------------------------------------------------------------------------


class _Elem:
    def __init__(self, value=None):
        self._v = value

    @property
    def value(self):
        return self._v


def _mode_slot(label="", control="Fixed", **kw):
    slot = SimpleNamespace(label=_Elem(label), control=_Elem(control))
    for field in ("target_rpm", "variable", "setpoint", "sensitivity"):
        setattr(slot, field, _Elem(kw.get(field)))
    slot.minimum_seconds = _Elem(kw.get("minimum_seconds", 0))
    return slot


def _transition_slot(to_mode="", **kw):
    slot = SimpleNamespace(to_mode=_Elem(to_mode))
    defaults = {
        "from_mode": "",
        "when": "Sensor",
        "after_seconds": 60,
        "variable": "",
        "direction": "Above",
        "setpoint": None,
        "panel_flag": "Warming Up",
        "panel_state": "Off",
        "sensor_hold_seconds": 0,
        "panel_hold_seconds": 0,
    }
    for field, default in defaults.items():
        setattr(slot, field, _Elem(kw.get(field, default)))
    return slot


def _config(modes=(), transitions=(), start_in="", include_default=True):
    return SimpleNamespace(
        run_modes=_Elem(list(modes)),
        run_mode_transitions=_Elem(list(transitions)),
        start_in_mode=_Elem(start_in),
        include_default_fixed_run_mode=_Elem(include_default),
    )


def _sensor(label="Discharge Pressure", allow=True):
    from pydoover.utils.utils import sanitize_display_name as sanitise

    return SimpleNamespace(
        label=label, sanitised=sanitise(label), allow_rpm_control=allow
    )


# ---------------------------------------------------------------------------
# Parsing modes
# ---------------------------------------------------------------------------


def test_no_configured_modes_gives_one_fixed_mode():
    """Deleting every row must not leave the pump with no way to set a speed —
    and an install that configures nothing behaves as the app always did."""
    modes = build_modes(_config())
    assert len(modes) == 1
    assert modes[0].label == DEFAULT_MODE_LABEL == "Fixed RPM"
    assert modes[0].control == CONTROL_FIXED
    assert modes[0].sanitised == "fixed_rpm"


def test_modes_parse_in_order_with_their_parameters():
    modes = build_modes(
        _config(
            modes=[
                _mode_slot("Warm Up", "Fixed", target_rpm=800),
                _mode_slot(
                    "Run", "Regulate", variable="Discharge Pressure",
                    setpoint=200.0, sensitivity=3.0,
                ),
            ],
            include_default=False,
        )
    )
    assert [m.label for m in modes] == ["Warm Up", "Run"]
    assert [m.sanitised for m in modes] == ["warm_up", "run"]
    assert modes[0].control == CONTROL_FIXED and modes[0].target_rpm == 800
    assert modes[1].is_regulate
    # Both forms of the sensor name are kept: one to match on, one to read.
    assert modes[1].variable == "discharge_pressure"
    assert modes[1].variable_label == "Discharge Pressure"
    assert modes[1].setpoint == 200.0 and modes[1].sensitivity == 3.0


def test_unnamed_mode_rows_are_skipped():
    # A row added in the form and left blank, exactly like an analog input with
    # no pin — skipped rather than taking the app down at config load.
    modes = build_modes(
        _config(modes=[_mode_slot(""), _mode_slot("Run")], include_default=False)
    )
    assert [m.label for m in modes] == ["Run"]


def test_colliding_mode_names_are_made_unique(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        modes = build_modes(
            _config(
                modes=[_mode_slot("Line Fill"), _mode_slot("line fill")],
                include_default=False,
            )
        )
    assert [m.sanitised for m in modes] == ["line_fill", "line_fill_2"]
    assert "collides" in caplog.text


def test_mode_element_names_follow_the_mode_name():
    mode = build_modes(_config(modes=[_mode_slot("Line Fill")]))[0]
    assert mode.element_name("target_rpm") == "mode_line_fill_target_rpm"


# ---------------------------------------------------------------------------
# Parsing transitions
# ---------------------------------------------------------------------------


def test_transitions_resolve_mode_names_leniently():
    """Config can't offer a picker, so names are typed. 'line fill' and
    'Line Fill' have to be the same thing or every install is a typo away from
    a rule that silently never fires."""
    modes = build_modes(
        _config(modes=[_mode_slot("Warm Up"), _mode_slot("Line Fill")])
    )
    transitions = build_transitions(
        _config(transitions=[_transition_slot("line fill", from_mode="warm up")]),
        modes,
    )
    assert transitions[0].to_mode == "line_fill"
    assert transitions[0].from_mode == "warm_up"
    # ...and the operator's own capitalisation comes back for the dashboard.
    assert transitions[0].to_label == "Line Fill"
    assert transitions[0].from_label == "Warm Up"


def test_transition_when_kinds_parse():
    modes = build_modes(_config(modes=[_mode_slot("A"), _mode_slot("B")]))
    for configured, expected in (
        ("Timer", WHEN_TIMER),
        ("Sensor", WHEN_SENSOR),
        ("Panel Flag", WHEN_PANEL),
    ):
        t = build_transitions(
            _config(transitions=[_transition_slot("B", when=configured)]), modes
        )[0]
        assert t.when == expected, configured


def test_transition_without_a_target_is_skipped():
    modes = build_modes(_config(modes=[_mode_slot("A")]))
    assert build_transitions(_config(transitions=[_transition_slot("")]), modes) == []


def test_unknown_transition_target_is_kept_as_typed():
    # Dropping it would leave the operator with a rule that vanished silently;
    # validate() is what tells them about it.
    modes = build_modes(_config(modes=[_mode_slot("A")]))
    t = build_transitions(_config(transitions=[_transition_slot("Typo")]), modes)[0]
    assert t.to_mode == "typo" and t.to_label == "Typo"


def test_panel_flag_state_parses_on_and_off():
    modes = build_modes(_config(modes=[_mode_slot("A"), _mode_slot("B")]))
    off, on = (
        build_transitions(
            _config(
                transitions=[
                    _transition_slot("B", when="Panel Flag", panel_state=state)
                ]
            ),
            modes,
        )[0]
        for state in ("Off", "On")
    )
    assert off.panel_on is False and on.panel_on is True
    assert off.panel_flag == "warming_up"


# ---------------------------------------------------------------------------
# Sequencer: which mode, and when to leave it
# ---------------------------------------------------------------------------


def _never(*_a, **_k):
    return False


def _seq(modes=None, transitions=None):
    modes = modes or [
        RunMode("Warm Up", "warm_up", CONTROL_FIXED, target_rpm=800),
        RunMode("Line Fill", "line_fill", CONTROL_FIXED, target_rpm=1300),
        RunMode("Run", "run", CONTROL_FIXED, target_rpm=1600),
    ]
    return ModeSequencer(modes, list(transitions or []))


def _sensor_transition(index=0, to="line_fill", frm="warm_up", hold=0.0, **kw):
    return RunTransition(
        index=index,
        to_mode=to,
        to_label=to,
        when=WHEN_SENSOR,
        from_mode=frm,
        from_label=frm,
        variable="discharge_pressure",
        variable_label="Discharge Pressure",
        direction="Above",
        setpoint=200.0,
        hold_seconds=hold,
        **kw,
    )


def test_no_start_in_mode_means_resume_whatever_was_left():
    """Blank Start In Mode: the pump carries on in the mode it was left in, and
    the list's first entry is only where an unusable selection lands."""
    seq = _seq()
    assert seq.start_mode is None
    assert seq.fallback_mode.label == "Warm Up"


def test_start_in_mode_is_independent_of_list_order():
    """List order is how the picker reads. Making it decide what every start does
    would turn re-ordering into a behaviour change."""
    seq = _seq()
    seq.start_mode_name = "run"
    assert seq.start_mode.label == "Run"
    assert seq.fallback_mode.label == "Run"  # and unusable selections land there


def test_resolve_falls_back_when_a_mode_is_gone(caplog):
    import logging

    seq = _seq()
    assert seq.resolve("run").label == "Run"
    with caplog.at_level(logging.WARNING):
        # A mode the operator renamed or deleted out from under a stored value.
        assert seq.resolve("gone").label == "Warm Up"
        assert seq.resolve("gone").label == "Warm Up"
    # Warned once, not once per main loop.
    assert caplog.text.count("no longer configured") == 1


def test_timer_transition_fires_on_time_in_mode():
    t = RunTransition(
        index=0, to_mode="line_fill", to_label="Line Fill", when=WHEN_TIMER,
        from_mode="warm_up", from_label="Warm Up", after_seconds=30.0,
    )
    seq = _seq(transitions=[t])
    warm = seq.fallback_mode
    fired = seq.evaluate(
        active=warm, entered_at=0.0, now=29.0, crossed=_never, flag=_never
    )
    assert fired is None
    fired = seq.evaluate(
        active=warm, entered_at=0.0, now=30.0, crossed=_never, flag=_never
    )
    assert fired is t


def test_sensor_transition_fires_when_the_reading_crosses():
    seq = _seq(transitions=[_sensor_transition()])
    warm = seq.fallback_mode
    assert seq.evaluate(
        active=warm, entered_at=0.0, now=1.0,
        crossed=lambda *_: False, flag=_never,
    ) is None
    assert seq.evaluate(
        active=warm, entered_at=0.0, now=2.0,
        crossed=lambda *_: True, flag=_never,
    ) is not None


def test_sensor_transition_holds_the_condition_first():
    """The reported shape: move on once pressure has been seen for x seconds,
    not the instant it blips past the setpoint."""
    seq = _seq(transitions=[_sensor_transition(hold=10.0)])
    warm = seq.fallback_mode
    kw = dict(active=warm, entered_at=0.0, crossed=lambda *_: True, flag=_never)
    assert seq.evaluate(now=100.0, **kw) is None  # condition just went true
    assert seq.evaluate(now=109.0, **kw) is None
    assert seq.evaluate(now=110.0, **kw) is not None


def test_a_lapsed_condition_restarts_its_hold():
    seq = _seq(transitions=[_sensor_transition(hold=10.0)])
    warm = seq.fallback_mode
    truth = {"value": True}
    kw = dict(
        active=warm, entered_at=0.0,
        crossed=lambda *_: truth["value"], flag=_never,
    )
    assert seq.evaluate(now=100.0, **kw) is None
    truth["value"] = False
    assert seq.evaluate(now=105.0, **kw) is None
    truth["value"] = True
    # The hold restarts from the first observation that it is true again (114),
    # not from where it left off — nine more seconds of truth is not ten.
    assert seq.evaluate(now=114.0, **kw) is None
    assert seq.evaluate(now=123.0, **kw) is None
    assert seq.evaluate(now=124.0, **kw) is not None


def test_an_unanswerable_condition_is_not_met_and_drops_its_hold():
    """A dead sensor reads None. Not knowing is a reason to stay put, and a hold
    that survived the outage would fire on stale evidence the moment it came
    back."""
    seq = _seq(transitions=[_sensor_transition(hold=10.0)])
    warm = seq.fallback_mode
    answer = {"value": True}
    kw = dict(
        active=warm, entered_at=0.0,
        crossed=lambda *_: answer["value"], flag=_never,
    )
    assert seq.evaluate(now=100.0, **kw) is None
    answer["value"] = None  # sensor drops out mid-hold
    assert seq.evaluate(now=109.0, **kw) is None
    answer["value"] = True
    assert seq.evaluate(now=111.0, **kw) is None  # hold restarted, not resumed
    assert seq.evaluate(now=121.0, **kw) is not None


def test_panel_flag_transition_fires_on_the_configured_state():
    t = RunTransition(
        index=0, to_mode="line_fill", to_label="Line Fill", when=WHEN_PANEL,
        from_mode="warm_up", from_label="Warm Up",
        panel_flag="warming_up", panel_flag_label="Warming Up", panel_on=False,
    )
    seq = _seq(transitions=[t])
    warm = seq.fallback_mode
    kw = dict(active=warm, entered_at=0.0, now=1.0, crossed=_never)
    # Panel still warming up -> stay.
    assert seq.evaluate(flag=lambda _n: True, **kw) is None
    # Warm-up finished -> move on.
    assert seq.evaluate(flag=lambda _n: False, **kw) is t
    # No panel data at all -> can't tell, so stay.
    assert seq.evaluate(flag=lambda _n: None, **kw) is None


def test_transitions_are_scoped_to_their_from_mode():
    seq = _seq(transitions=[_sensor_transition(frm="warm_up")])
    line_fill = seq.resolve("line_fill")
    assert seq.evaluate(
        active=line_fill, entered_at=0.0, now=1.0,
        crossed=lambda *_: True, flag=_never,
    ) is None


def test_a_blank_from_mode_applies_everywhere():
    seq = _seq(transitions=[_sensor_transition(to="run", frm="")])
    for mode in ("warm_up", "line_fill"):
        assert seq.evaluate(
            active=seq.resolve(mode), entered_at=0.0, now=1.0,
            crossed=lambda *_: True, flag=_never,
        ) is not None


def test_a_transition_never_fires_into_the_mode_it_is_already_in():
    # Otherwise a blank From Mode would re-enter its target every loop and keep
    # resetting time-in-mode, which every timer downstream depends on.
    seq = _seq(transitions=[_sensor_transition(to="run", frm="")])
    assert seq.evaluate(
        active=seq.resolve("run"), entered_at=0.0, now=1.0,
        crossed=lambda *_: True, flag=_never,
    ) is None


def test_first_matching_transition_wins():
    seq = _seq(
        transitions=[
            _sensor_transition(index=0, to="line_fill"),
            _sensor_transition(index=1, to="run"),
        ]
    )
    fired = seq.evaluate(
        active=seq.fallback_mode, entered_at=0.0, now=1.0,
        crossed=lambda *_: True, flag=_never,
    )
    assert fired.to_mode == "line_fill"


def test_holds_re_arm_on_a_mode_change():
    """A condition true throughout the previous mode shouldn't fire the instant
    the next one is entered, having never been observed while in it."""
    seq = _seq(transitions=[_sensor_transition(to="run", frm="", hold=10.0)])
    kw = dict(entered_at=0.0, crossed=lambda *_: True, flag=_never)
    assert seq.evaluate(active=seq.resolve("warm_up"), now=100.0, **kw) is None
    seq.mode_changed()
    assert seq.evaluate(active=seq.resolve("line_fill"), now=109.0, **kw) is None
    assert seq.evaluate(active=seq.resolve("line_fill"), now=119.0, **kw) is not None


def test_outgoing_lists_what_could_move_us_on():
    seq = _seq(
        transitions=[
            _sensor_transition(index=0, to="line_fill", frm="warm_up"),
            _sensor_transition(index=1, to="run", frm="line_fill"),
        ]
    )
    assert [t.to_mode for t in seq.outgoing(seq.fallback_mode)] == ["line_fill"]
    assert seq.outgoing(seq.resolve("run")) == []


def test_transition_summary_reads_as_a_sentence():
    seq = _seq(transitions=[_sensor_transition(hold=10.0)])
    seq.transitions[0].to_label = "Line Fill"
    assert seq.transitions[0].summary == (
        "→ Line Fill when Discharge Pressure above 200 for 10s"
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _problems(modes=(), transitions=(), sensors=(), start_in=""):
    return validate(
        build_sequencer(_config(modes, transitions, start_in)), list(sensors)
    )


def test_default_config_has_nothing_to_report():
    assert _problems() == []


def test_regulate_mode_must_name_a_real_sensor_allowed_to_control_rpm():
    # Names nothing.
    assert any(
        "names no variable" in p
        for p in _problems(modes=[_mode_slot("Run", "Regulate", setpoint=1)])
    )
    # Names a sensor that doesn't exist — and is told what does.
    problems = _problems(
        modes=[_mode_slot("Run", "Regulate", variable="Typo", setpoint=1)],
        sensors=[_sensor("Discharge Pressure")],
    )
    assert any("not a configured sensor" in p for p in problems)
    assert any("Discharge Pressure" in p for p in problems)
    # Names one that isn't allowed to drive engine speed.
    assert any(
        "not allowed to control RPM" in p
        for p in _problems(
            modes=[
                _mode_slot("Run", "Regulate", variable="Discharge Pressure", setpoint=1)
            ],
            sensors=[_sensor("Discharge Pressure", allow=False)],
        )
    )


def test_regulate_mode_without_a_setpoint_is_reported():
    assert any(
        "no starting setpoint" in p
        for p in _problems(
            modes=[_mode_slot("Run", "Regulate", variable="Discharge Pressure")],
            sensors=[_sensor("Discharge Pressure")],
        )
    )


def test_transition_naming_an_unknown_mode_is_reported_with_the_real_ones():
    problems = _problems(
        modes=[_mode_slot("Warm Up"), _mode_slot("Run")],
        transitions=[_transition_slot("Typo", from_mode="Warm Up", when="Timer")],
    )
    assert any("not a run mode" in p and "Warm Up, Run" in p for p in problems)


def test_transition_to_its_own_from_mode_is_reported():
    assert any(
        "to itself" in p
        for p in _problems(
            modes=[_mode_slot("Run")],
            transitions=[_transition_slot("Run", from_mode="Run", when="Timer")],
        )
    )


def test_sensor_transition_needs_a_sensor_and_a_setpoint():
    problems = _problems(
        modes=[_mode_slot("A"), _mode_slot("B")],
        transitions=[_transition_slot("B", from_mode="A", when="Sensor")],
    )
    assert any("names none" in p for p in problems)
    assert any("no setpoint" in p for p in problems)


def _notes(modes=(), transitions=(), start_in=""):
    from dse_engine_controller.run_modes import advisories

    return advisories(build_sequencer(_config(modes, transitions, start_in)))


def test_a_hand_picked_only_mode_is_an_observation_not_a_problem():
    """It works exactly as written — a mode you only ever pick by hand is a
    perfectly good mode, and is the whole point of the default Fixed one. Putting
    it on the dashboard next to a mode naming a sensor that doesn't exist would
    teach operators that the status line cries wolf."""
    config = dict(
        modes=[
            _mode_slot("Warm Up", target_rpm=900),
            _mode_slot("Orphan", target_rpm=900),
        ],
        start_in="Warm Up",
    )
    assert _problems(**config) == []

    notes = _notes(**config)
    # Where starts begin is reachable by definition; only the orphan is noted.
    assert len(notes) == 1 and "Orphan" in notes[0]


def test_nothing_is_noted_when_starts_resume():
    assert _notes(
        modes=[_mode_slot("Warm Up", target_rpm=900), _mode_slot("Orphan", target_rpm=900)]
    ) == []


def test_the_default_fixed_mode_is_never_noted():
    """Nothing transitioning into it is the point of it."""
    assert _notes(
        modes=[_mode_slot("Warm Up", target_rpm=900)], start_in="Warm Up"
    ) == []


def test_an_unresolvable_start_in_mode_is_reported():
    problems = _problems(
        modes=[_mode_slot("Warm Up", target_rpm=900)], start_in="Typo"
    )
    assert any("Start In Mode" in p and "Warm Up" in p for p in problems)


def test_a_fully_wired_setup_is_clean():
    problems = _problems(
        modes=[
            _mode_slot("Warm Up", "Fixed", target_rpm=800),
            _mode_slot("Line Fill", "Fixed", target_rpm=1300),
            _mode_slot(
                "Run", "Regulate", variable="Discharge Pressure", setpoint=200.0
            ),
        ],
        transitions=[
            _transition_slot(
                "Line Fill", from_mode="Warm Up", when="Sensor",
                variable="Discharge Pressure", setpoint=50.0, hold_seconds=10,
            ),
            _transition_slot("Run", from_mode="Line Fill", when="Timer"),
        ],
        sensors=[_sensor("Discharge Pressure")],
    )
    assert problems == []


# ---------------------------------------------------------------------------
# Minimum dwell
# ---------------------------------------------------------------------------


def _dwell_seq(minimum, transitions):
    modes = [
        RunMode("Warm Up", "warm_up", CONTROL_FIXED, minimum_seconds=minimum),
        RunMode("Line Fill", "line_fill", CONTROL_FIXED),
    ]
    return ModeSequencer(modes, list(transitions))


def test_a_condition_true_on_entry_is_held_off_by_the_minimum():
    """The trap this exists for: level-triggered conditions. An operator picks a
    mode whose exit rule is already satisfied and gets bounced out on the next
    poll, with nothing on screen to explain it."""
    seq = _dwell_seq(30.0, [_sensor_transition(hold=0.0)])
    warm = seq.fallback_mode
    kw = dict(active=warm, entered_at=0.0, crossed=lambda *_: True, flag=_never)
    assert seq.evaluate(now=1.0, **kw) is None
    assert seq.evaluate(now=29.0, **kw) is None
    assert seq.evaluate(now=30.0, **kw) is not None


def test_holds_keep_running_through_the_minimum():
    """The floor is on leaving, not on watching — a rule satisfied throughout
    fires the moment the floor is up, rather than starting its hold from there."""
    seq = _dwell_seq(30.0, [_sensor_transition(hold=10.0)])
    warm = seq.fallback_mode
    kw = dict(active=warm, entered_at=0.0, crossed=lambda *_: True, flag=_never)
    seq.evaluate(now=1.0, **kw)     # hold starts here, well inside the minimum
    assert seq.evaluate(now=29.0, **kw) is None
    assert seq.evaluate(now=30.0, **kw) is not None  # not 30 + another 10


def test_a_zero_minimum_is_the_old_behaviour():
    seq = _dwell_seq(0.0, [_sensor_transition(hold=0.0)])
    assert seq.evaluate(
        active=seq.fallback_mode, entered_at=0.0, now=0.0,
        crossed=lambda *_: True, flag=_never,
    ) is not None


def test_the_minimum_belongs_to_the_mode_being_left():
    # Line Fill has no minimum, so a rule out of it is unaffected by Warm Up's.
    seq = _dwell_seq(30.0, [_sensor_transition(to="run", frm="line_fill")])
    assert seq.evaluate(
        active=seq.resolve("line_fill"), entered_at=0.0, now=1.0,
        crossed=lambda *_: True, flag=_never,
    ) is not None


def test_minimum_seconds_parses_from_config():
    modes = build_modes(
        _config(modes=[_mode_slot("Warm Up", minimum_seconds=45)])
    )
    assert modes[0].minimum_seconds == 45.0


def test_transition_cause_reads_without_its_destination():
    """What gets reported when a transition moves the mode — the destination is
    already in the message, so the reason shouldn't repeat it."""
    t = _sensor_transition(hold=10.0)
    t.to_label = "Line Fill"
    assert t.cause == "when Discharge Pressure above 200 for 10s"
    assert t.summary == "→ Line Fill when Discharge Pressure above 200 for 10s"


def test_a_fixed_mode_without_a_target_rpm_is_reported():
    """Required in config now, but a config saved before that — or a row left
    half-filled — still reaches the app."""
    assert any(
        "has no Target RPM" in p
        for p in _problems(modes=[_mode_slot("Warm Up", "Fixed")])
    )


def test_the_synthesised_default_mode_is_not_reported():
    """It has no Target RPM by definition; complaining about the one blank the
    app puts there itself would flag every unconfigured install."""
    assert _problems() == []
    assert build_modes(_config())[0].synthesised is True


def test_target_rpm_is_required_only_for_fixed_modes():
    from dse_engine_controller.app_config import RunModeSlot

    assert RunModeSlot.target_rpm.required is True
    # A Regulate mode has nothing to say about a fixed RPM.
    assert not any(
        "Target RPM" in p
        for p in _problems(
            modes=[
                _mode_slot("Run", "Regulate", variable="Discharge Pressure",
                           setpoint=200.0)
            ],
            sensors=[_sensor("Discharge Pressure")],
        )
    )


def test_each_condition_kind_reads_its_own_hold_field():
    """A hold means nothing to a Timer, which is already a duration, so the form
    shows one field per condition kind rather than one that appears where it does
    nothing. They feed the same runtime hold."""
    modes = build_modes(_config(modes=[_mode_slot("A"), _mode_slot("B")]))

    sensor = build_transitions(
        _config(transitions=[
            _transition_slot("B", when="Sensor", sensor_hold_seconds=10,
                             panel_hold_seconds=99)
        ]),
        modes,
    )[0]
    assert sensor.hold_seconds == 10.0  # not the panel field

    panel = build_transitions(
        _config(transitions=[
            _transition_slot("B", when="Panel Flag", panel_hold_seconds=7,
                             sensor_hold_seconds=99)
        ]),
        modes,
    )[0]
    assert panel.hold_seconds == 7.0

    timer = build_transitions(
        _config(transitions=[
            _transition_slot("B", when="Timer", sensor_hold_seconds=99,
                             panel_hold_seconds=99)
        ]),
        modes,
    )[0]
    assert timer.hold_seconds == 0.0  # neither field reaches a Timer


# ---------------------------------------------------------------------------
# The always-available manual Fixed mode
# ---------------------------------------------------------------------------


def test_the_default_fixed_mode_is_offered_alongside_a_programme():
    """Somewhere to take manual control of engine speed whatever the programme
    is doing. It goes last, so the first *configured* mode stays the one an
    unusable selection falls back to."""
    modes = build_modes(
        _config(modes=[_mode_slot("Warm Up", target_rpm=900), _mode_slot("Run", target_rpm=1600)])
    )
    assert [m.label for m in modes] == ["Warm Up", "Run", "Fixed RPM"]
    assert modes[-1].synthesised is True
    assert modes[0].synthesised is False


def test_the_default_fixed_mode_can_be_turned_off():
    modes = build_modes(
        _config(modes=[_mode_slot("Warm Up", target_rpm=900)], include_default=False)
    )
    assert [m.label for m in modes] == ["Warm Up"]


def test_the_default_fixed_mode_is_the_only_one_when_nothing_is_configured():
    """On or off — the app still has to be able to run the pump."""
    for include in (True, False):
        modes = build_modes(_config(include_default=include))
        assert [m.label for m in modes] == ["Fixed RPM"], include


def test_a_site_naming_its_own_mode_fixed_rpm_gets_no_duplicate():
    modes = build_modes(_config(modes=[_mode_slot("Fixed RPM", target_rpm=1200)]))
    assert [m.label for m in modes] == ["Fixed RPM"]
    # ...and it is the site's own, not the synthesised one.
    assert modes[0].synthesised is False and modes[0].target_rpm == 1200


def test_the_manual_mode_is_not_a_problem():
    """Nothing transitions into it — that is the point of it."""
    assert _problems(
        modes=[_mode_slot("Warm Up", target_rpm=900)], start_in="Warm Up"
    ) == []


# ---------------------------------------------------------------------------
# Forgiving name resolution
# ---------------------------------------------------------------------------


def test_names_resolve_across_spacing_case_and_punctuation():
    """Maxitool (2026-08-27): a transition to "linefill" reported "not a run
    mode" because sanitising it gives `linefill` while "Line Fill" gives
    `line_fill`. Config can't offer a picker, so a typed name has to forgive the
    ways a person will reasonably write it."""
    from dse_engine_controller.run_modes import resolve_name

    keys = ["warmup", "line_fill", "regulate_output"]
    for typed in ("line_fill", "linefill", "Line Fill", "line fill", "LINE-FILL",
                  "  Line   Fill  "):
        assert resolve_name(typed, keys) == "line_fill", typed
    assert resolve_name("nope", keys) == ""
    assert resolve_name("", keys) == ""
    assert resolve_name(None, keys) == ""


def test_an_exact_match_always_beats_a_loose_one():
    """A site with two names that only differ by punctuation keeps them apart."""
    from dse_engine_controller.run_modes import resolve_name

    keys = ["line_fill", "linefill"]
    assert resolve_name("line_fill", keys) == "line_fill"
    assert resolve_name("linefill", keys) == "linefill"


def test_a_transition_finds_a_mode_written_loosely():
    modes = build_modes(
        _config(
            modes=[_mode_slot("Warmup", target_rpm=900),
                   _mode_slot("Line Fill", target_rpm=1300)],
            include_default=False,
        )
    )
    t = build_transitions(
        _config(transitions=[
            _transition_slot("linefill", from_mode="WARMUP", when="Timer")
        ]),
        modes,
    )[0]
    assert t.to_mode == "line_fill" and t.from_mode == "warmup"
    # ...and reports itself using the mode's real label, not what was typed.
    assert t.summary == "→ Line Fill after 60s"


def test_start_in_mode_is_matched_loosely_too():
    seq = build_sequencer(
        _config(modes=[_mode_slot("Warm Up", target_rpm=900)],
                start_in="warmup", include_default=False)
    )
    assert seq.start_mode.label == "Warm Up"


def test_a_regulate_mode_finds_a_loosely_named_sensor():
    sensors = [_sensor("Discharge Pressure")]
    modes = build_modes(
        _config(
            modes=[_mode_slot("Run", "Regulate", variable="dischargepressure",
                              setpoint=4)],
            include_default=False,
        ),
        sensors,
    )
    assert modes[0].variable == "discharge_pressure"
    # The sensor's own label comes back for the dashboard.
    assert modes[0].variable_label == "Discharge Pressure"
    assert validate(ModeSequencer(modes, []), sensors) == []


def test_a_sensor_transition_finds_a_loosely_named_sensor():
    sensors = [_sensor("Inlet Pressure")]
    modes = build_modes(
        _config(modes=[_mode_slot("A", target_rpm=900), _mode_slot("B", target_rpm=900)],
                include_default=False)
    )
    t = build_transitions(
        _config(transitions=[
            _transition_slot("B", from_mode="A", when="Sensor",
                             variable="inlet pressure", setpoint=0.5)
        ]),
        modes,
        sensors,
    )[0]
    assert t.variable == "inlet_pressure"
    assert "Inlet Pressure" in t.summary


def test_a_name_that_matches_nothing_is_still_reported():
    """Forgiving is not the same as silent — a real typo must still say so."""
    problems = _problems(
        modes=[_mode_slot("Warm Up", target_rpm=900)],
        transitions=[_transition_slot("Completely Different", from_mode="Warm Up",
                                      when="Timer")],
    )
    assert any("not a run mode" in p for p in problems)


def test_a_stored_selection_survives_a_loose_rename():
    seq = _seq()
    assert seq.resolve("linefill").label == "Line Fill"
    assert seq.resolve("LINE FILL").label == "Line Fill"


# ---------------------------------------------------------------------------
# The reserved Shutdown destination
# ---------------------------------------------------------------------------


def test_shutdown_is_recognised_however_it_is_written():
    from dse_engine_controller.run_modes import SHUTDOWN

    modes = build_modes(_config(modes=[_mode_slot("Run", target_rpm=900)],
                                include_default=False))
    for typed in ("shutdown", "Shutdown", "SHUT DOWN", "shut_down", "Shut-Down"):
        t = build_transitions(
            _config(transitions=[_transition_slot(typed, from_mode="Run",
                                                  when="Timer")]),
            modes,
        )[0]
        assert t.to_mode == SHUTDOWN, typed
        assert t.is_shutdown is True
        assert t.to_label == "Shutdown"
        assert t.summary == "→ Shutdown after 60s"


def test_a_normal_transition_is_not_a_shutdown():
    modes = build_modes(
        _config(modes=[_mode_slot("A", target_rpm=900), _mode_slot("B", target_rpm=900)],
                include_default=False)
    )
    t = build_transitions(
        _config(transitions=[_transition_slot("B", from_mode="A", when="Timer")]),
        modes,
    )[0]
    assert t.is_shutdown is False


def test_shutdown_is_not_reported_as_a_missing_mode():
    assert _problems(
        modes=[_mode_slot("Run", target_rpm=900)],
        transitions=[_transition_slot("Shutdown", from_mode="Run", when="Timer")],
    ) == []


def test_the_missing_mode_message_mentions_shutdown_as_an_option():
    problems = _problems(
        modes=[_mode_slot("Run", target_rpm=900)],
        transitions=[_transition_slot("Typo", from_mode="Run", when="Timer")],
    )
    assert any("'Shutdown' to stop the engine" in p for p in problems)


def test_a_mode_named_shutdown_is_reported_as_reserved():
    """The reserved destination wins, so a mode with that name could never be
    selected by a transition — better to say so than to let it look wired up."""
    problems = _problems(modes=[_mode_slot("Shutdown", target_rpm=900)])
    assert any("reserved name" in p for p in problems)
