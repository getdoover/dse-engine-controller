"""Configurable run modes for the DSE engine controller, and the moves between them.

A *run mode* is one way of deciding the engine's target RPM while the pump runs,
and the two ways are the ones this app already had: hold a **Fixed** RPM, or
**Regulate** a sensor to a setpoint. Naming them and listing them is the whole
idea — it turns "the RPM mode" from a hardcoded pair into something a site can
shape (Warm Up at 800, Line Fill at 1300, Run regulating discharge pressure)
without any of those particular stages being built into this app.

Design
------
* **No new control behaviour.** A mode selects one of the two existing
  primitives and supplies its parameters. :class:`~.regulator.RpmRegulator` and
  the fixed path are untouched, which is also why the whole feature degrades to
  today's behaviour when nothing is configured.
* **Two tables, like the state machine next door.** Modes and transitions are
  separate lists, exactly the shape ``autostart.py`` uses for ``STATES`` and
  ``TRANSITIONS`` — just read from config instead of module constants. Several
  rules can move between the same pair of modes, and a rule can apply in every
  mode at once.
* **Switching by hand and switching automatically are the same operation.** A
  transition sets the active mode; so does the operator's picker. Same
  convention ``_evaluate_auto_control`` already follows for starts, where Sensor
  and Timer modes feed the same run latch the buttons do.
* **No hardware, no timers, no config reads at runtime.** :class:`ModeSequencer`
  is handed the readings it needs through callables, so it stays pure and
  unit-testable — the same rule :class:`~.regulator.RpmRegulator` and
  :class:`~.analog.AnalogInput` follow.

Identity works the way it does for sensors (see ``sensors.py``): every mode
carries both the operator's ``label`` and its ``sanitised`` form. The sanitised
name is what gets stored, what transitions match on, and what names UI elements;
the label is what people read. Config can't offer a dynamic picker — only the UI
can — so modes and sensors are referenced by typed name, matched leniently on the
sanitised form so "Line Fill" and "line_fill" are the same thing, and anything
that doesn't resolve is reported by :func:`validate` at startup rather than left
to fail quietly at 3am.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from pydoover.utils.utils import sanitize_display_name as sanitise

from .app_config import (
    PANEL_FLAG_ON,
    RUN_MODE_PANEL,
    RUN_MODE_REGULATE,
    TRANSITION_PANEL,
    TRANSITION_SENSOR,
    TRANSITION_TIMER,
)
from .sensors import cfg_value

log = logging.getLogger(__name__)


CONTROL_FIXED = "fixed"
CONTROL_REGULATE = "regulate"
# The DSE's own Maintain Value loop; this app only moves its set point.
CONTROL_PANEL = "panel"

# A reserved To Mode. A rule pointing here doesn't change mode — it stops the
# engine, through the same run latch the Stop button uses. Modelled as a
# destination rather than a fourth condition kind so every condition can shut the
# pump down: after twenty minutes, on a pressure, on a panel flag, all of it.
SHUTDOWN = "shutdown"
SHUTDOWN_LABEL = "Shutdown"

WHEN_TIMER = "timer"
WHEN_SENSOR = "sensor"
WHEN_PANEL = "panel"

# The plain "hold whatever RPM the operator has dialled in" mode. Synthesised
# rather than defaulted into the config array, so that deleting every row can't
# leave the pump with no way to set a speed — and so it can be offered alongside
# a configured programme as a manual escape hatch (see build_modes).
DEFAULT_MODE_LABEL = "Fixed RPM"

# Panel states a transition can watch, keyed by their sanitised config name
# and mapped to the attribute on a DseSnapshot.
PANEL_FLAG_ATTRS = {
    "warming_up": "is_warming_up",
    "cooling_down": "is_cooling_down",
    "common_alarm": "has_common_alarm",
    "warning": "has_warning",
}


@dataclass
class RunMode:
    """One named way of driving engine speed."""

    label: str
    sanitised: str
    control: str
    # Fixed: where this mode's Target RPM control starts. None → the middle of
    # the configured band, decided by the application (it owns the bounds).
    target_rpm: int | None = None
    # Regulate: which sensor, and where its controls start. `variable` is a
    # sanitised sensor name; `variable_label` is the same sensor as written.
    variable: str = ""
    variable_label: str = ""
    setpoint: float | None = None
    sensitivity: float | None = None
    # Maintain Value: where the dashboard set point slider starts and its bounds.
    # The panel clamps to its own Set Point Min / Max on top of these.
    panel_setpoint: float | None = None
    panel_setpoint_min: float = 0.0
    panel_setpoint_max: float = 100.0
    # Digital outputs this mode drives, by sanitised output name. Anything absent
    # rests at that output's own default — see outputs.desired_states.
    outputs: dict = field(default_factory=dict)
    # Floor on how long this mode runs before any transition may leave it. See
    # ModeSequencer.evaluate — conditions are level-triggered, so without a floor
    # a rule whose condition is already true fires on the loop the mode is
    # entered, including a mode an operator has just picked by hand.
    minimum_seconds: float = 0.0
    # True only for the mode the app invents when nothing is configured. It has
    # no Target RPM by definition, so validation has to know not to complain
    # about the one blank it puts there itself.
    synthesised: bool = False

    @property
    def is_regulate(self) -> bool:
        return self.control == CONTROL_REGULATE

    @property
    def is_panel(self) -> bool:
        """Whether the DSE's own Maintain Value loop drives speed in this mode."""
        return self.control == CONTROL_PANEL

    @property
    def is_fixed(self) -> bool:
        return self.control == CONTROL_FIXED

    def element_name(self, suffix: str) -> str:
        """Name of one of this mode's UI controls / per-mode tags.

        Keyed on the sanitised label like the per-sensor elements are, so
        renaming a mode renames its controls rather than orphaning them.
        """
        return f"mode_{self.sanitised}_{suffix}"


@dataclass
class RunTransition:
    """One rule for moving out of a run mode."""

    index: int  # position in config, and the tie-break when several could fire
    to_mode: str
    to_label: str
    when: str
    # Blank applies in every mode. Kept as a name rather than a resolved RunMode
    # so a rule can outlive a mode being renamed without silently re-targeting.
    from_mode: str = ""
    from_label: str = ""
    after_seconds: float = 60.0
    variable: str = ""
    variable_label: str = ""
    direction: str = "Above"
    setpoint: float | None = None
    panel_flag: str = ""
    panel_flag_label: str = ""
    panel_on: bool = False
    hold_seconds: float = 0.0
    # Whether the operator may move this rule's number from the dashboard. What
    # the rule *is* stays in config; only the threshold it fires at is tunable.
    adjustable: bool = False
    # Stable identity for that control, from the pair of modes it moves between.
    key: str = ""

    @property
    def tunable_name(self) -> str | None:
        """The UI element holding this rule's adjustable number, or ``None``.

        A Timer's number is how long to wait; a Sensor's is the level it fires
        at. A Panel Flag has neither — its flag and state are choices, not
        quantities — so it has nothing to offer a slider.
        """
        if not self.adjustable:
            return None
        if self.when == WHEN_TIMER:
            return f"transition_{self.key}_after"
        if self.when == WHEN_SENSOR:
            return f"transition_{self.key}_setpoint"
        return None

    @property
    def tunable_default(self) -> float | None:
        """What that control starts at — whatever config asked for."""
        if self.when == WHEN_TIMER:
            return float(self.after_seconds)
        if self.when == WHEN_SENSOR:
            return None if self.setpoint is None else float(self.setpoint)
        return None

    @property
    def is_shutdown(self) -> bool:
        """Whether this rule stops the engine rather than changing mode."""
        return self.to_mode == SHUTDOWN

    def applies_in(self, mode: RunMode) -> bool:
        """Whether this rule is live while ``mode`` is the active one."""
        if self.to_mode == mode.sanitised:
            return False  # already there; firing would only reset the clock
        return not self.from_mode or self.from_mode == mode.sanitised

    @property
    def condition(self) -> str:
        """The test this rule applies, without how long it has to hold."""
        return self.condition_at(None)

    def condition_at(self, value) -> str:
        """The same, stated at ``value`` — what the operator has dialled in.

        The status line has to quote the number actually in force, or it reads as
        contradicting the slider sitting next to it.
        """
        if self.when == WHEN_TIMER:
            after = self.after_seconds if value is None else value
            return f"after {int(after)}s"
        if self.when == WHEN_SENSOR:
            setpoint = self.setpoint if value is None else value
            sp = "?" if setpoint is None else f"{setpoint:g}"
            return f"when {self.variable_label} {self.direction.lower()} {sp}"
        if self.when == WHEN_PANEL:
            state = "on" if self.panel_on else "off"
            return f"when panel {self.panel_flag_label} is {state}"
        return "never"

    @property
    def cause(self) -> str:
        """What this rule is waiting for, on its own — no destination."""
        if self.hold_seconds and self.when != WHEN_TIMER:
            return f"{self.condition} for {int(self.hold_seconds)}s"
        return self.condition

    @property
    def summary(self) -> str:
        """One line for the dashboard: where this rule goes, and what it awaits."""
        return f"→ {self.to_label} {self.cause}"

    def progress(
        self, held: float | None, elapsed: float | None, tuned=None
    ) -> str:
        """The same line, counting out a wait that is already under way.

        A rule that reports only what it is waiting for tells an operator nothing
        about whether it is close. "when Inlet Pressure above 0.5 for 120s" reads
        identically whether the pressure has been there for two minutes or has
        never been seen at all — which is exactly the moment somebody wants to
        know which of those it is.
        """
        condition = self.condition if tuned is None else self.condition_at(tuned)
        if self.when == WHEN_TIMER:
            if elapsed is None:
                return f"→ {self.to_label} {condition}"
            after = self.after_seconds if tuned is None else tuned
            return (
                f"→ {self.to_label} after {int(after)}s ({int(elapsed)}s so far)"
            )
        if held is None:
            if self.hold_seconds:
                return f"→ {self.to_label} {condition} for {int(self.hold_seconds)}s"
            return f"→ {self.to_label} {condition}"
        if not self.hold_seconds:
            return f"→ {self.to_label} {condition} — met"
        return (
            f"→ {self.to_label} {condition} — met for "
            f"{int(held)}s of {int(self.hold_seconds)}s"
        )


class ModeSequencer:
    """Holds the configured modes and decides when to leave the active one.

    Pure: it never reads config, touches hardware or looks at the clock on its
    own. ``evaluate`` is handed the time and two callables that answer questions
    about the world, so every rule in here is exercisable from a test.
    """

    def __init__(
        self,
        modes: list[RunMode],
        transitions: list[RunTransition],
        start_mode: str = "",
    ):
        self.modes = list(modes)
        self.transitions = list(transitions)
        self._by_name = {mode.sanitised: mode for mode in self.modes}
        # Sanitised name of the mode every deliberate start resets to. Blank means
        # "resume" — carry on in whatever mode the pump was left in.
        self.start_mode_name = str(start_mode or "")
        # Transition index -> when its condition first went true. Cleared when the
        # condition lapses, and re-armed wholesale on a mode change: a condition
        # that was true throughout the previous mode shouldn't fire the instant
        # the next one is entered, having never been observed *in* it.
        self._held_since: dict[int, float] = {}
        self._warned_unknown: set[str] = set()

    @property
    def start_mode(self) -> RunMode | None:
        """The mode every deliberate start resets to, or ``None`` to resume.

        ``None`` is the unconfigured case and means the pump carries on in
        whatever mode it was last left in — including one an operator picked
        before shutting down. Deliberately not "the first mode in the list": list
        order is how the picker is ordered, and making it decide what every start
        does turns a cosmetic drag into a behaviour change.
        """
        name = self.start_mode_name
        return self._by_name.get(name) if name else None

    @property
    def fallback_mode(self) -> RunMode:
        """Where an unusable selection lands. Never ``None``; there is always one mode."""
        return self.start_mode or self.modes[0]

    def resolve(self, sanitised) -> RunMode:
        """The mode a stored selection names, falling back to the entry mode.

        A stored name can outlive its mode — the operator renames it, or deletes
        it — so this never raises. Warned once per unknown name rather than every
        loop, since this is called from the main loop.
        """
        name = resolve_name(sanitised, self._by_name)
        mode = self._by_name.get(name)
        if mode is not None:
            return mode
        name = str(sanitised or "")
        if name and name not in self._warned_unknown:
            self._warned_unknown.add(name)
            log.warning(
                "Run mode %r is no longer configured — falling back to %r.",
                name,
                self.fallback_mode.label,
            )
        return self.fallback_mode

    def outgoing(self, mode: RunMode) -> list[RunTransition]:
        """Every rule that could move the pump out of ``mode``, in order."""
        return [t for t in self.transitions if t.applies_in(mode)]

    def held_for(self, transition: RunTransition, now: float) -> float | None:
        """How long this rule's condition has been continuously true, or None.

        ``None`` covers both "not true" and "not being evaluated" — while the
        pump is stopped nothing is watched at all.
        """
        since = self._held_since.get(transition.index)
        return None if since is None else max(0.0, now - since)

    def describe(
        self, mode: RunMode, *, now: float | None = None,
        entered_at: float | None = None, tuned: dict | None = None,
    ) -> str:
        """What this mode is, and how close anything is to moving it on.

        With ``now`` given, waits already under way are counted out. Without it —
        the pump stopped, nothing being evaluated — each rule just states what it
        watches for, because a countdown that isn't running would be a lie.
        """
        lines = []
        for transition in self.outgoing(mode):
            live = _tuned(transition, tuned, None)
            if now is None:
                lines.append(transition.progress(None, None, live))
                continue
            elapsed = None if entered_at is None else max(0.0, now - entered_at)
            lines.append(
                transition.progress(self.held_for(transition, now), elapsed, live)
            )
        if lines:
            # Each line already opens with an arrow, so the mode name runs
            # straight into it: "Warm Up → Line Fill after 30s".
            return f"{mode.label} {'; '.join(lines)}"
        return f"{mode.label} — no automatic change from here"

    def mode_changed(self) -> None:
        """Re-arm every hold timer. Call whenever the active mode changes."""
        self._held_since.clear()

    def evaluate(
        self,
        *,
        active: RunMode,
        entered_at: float,
        now: float,
        crossed,
        flag,
        tuned: dict | None = None,
    ) -> RunTransition | None:
        """The first rule whose condition is met, or None to stay put.

        Parameters
        ----------
        active / entered_at:
            The mode being run and when it was entered (same clock as ``now``).
        crossed:
            ``crossed(variable, setpoint, direction) -> bool | None`` — whether a
            sensor reading is past a setpoint. ``None`` means it can't be
            answered (no such sensor, no reading yet, a faulted input).
        flag:
            ``flag(name) -> bool | None`` — a panel status bit, by its sanitised
            config name. ``None`` when the panel hasn't been heard from.

        An unanswerable condition counts as not met, and clears any hold it had
        accumulated: not knowing is a reason to stay where you are, and a reading
        that drops out mid-hold shouldn't have its hold survive the gap.

        A mode's ``minimum_seconds`` is a floor on *leaving*, not on watching:
        conditions are still evaluated and holds still accumulate through it, so
        a rule that has been satisfied throughout fires the moment the floor is
        up rather than starting its hold from there.
        """
        dwelling = now - entered_at < active.minimum_seconds
        for transition in self.transitions:
            if not transition.applies_in(active):
                self._held_since.pop(transition.index, None)
                continue
            met = self._met(transition, entered_at, now, crossed, flag, tuned)
            if met and not dwelling:
                return transition
        return None

    def _met(self, transition, entered_at, now, crossed, flag, tuned) -> bool:
        if transition.when == WHEN_TIMER:
            # Already a duration; a hold on top would just be a second timer.
            return now - entered_at >= _tuned(transition, tuned, transition.after_seconds)

        if transition.when == WHEN_SENSOR:
            truth = crossed(
                transition.variable,
                _tuned(transition, tuned, transition.setpoint),
                transition.direction,
            )
        elif transition.when == WHEN_PANEL:
            raised = flag(transition.panel_flag)
            truth = None if raised is None else (bool(raised) is transition.panel_on)
        else:
            return False

        if not truth:  # False, or None for "can't tell"
            self._held_since.pop(transition.index, None)
            return False
        since = self._held_since.setdefault(transition.index, now)
        return now - since >= transition.hold_seconds


# ---------------------------------------------------------------------------
# Parsing config
# ---------------------------------------------------------------------------


def _tuned(transition, tuned: dict | None, configured):
    """This rule's number: the operator's if they have one, else the configured.

    Keyed by position in the config list, which is also how the rules are
    ordered, so it survives anything that doesn't reorder them.
    """
    if not tuned:
        return configured
    value = tuned.get(transition.index)
    return configured if value is None else value


def _loose(name) -> str:
    """A name with everything but letters and digits stripped, folded to lower.

    ``Line Fill``, ``line fill``, ``line_fill``, ``LineFill`` and ``LINE-FILL``
    all land on ``linefill``.
    """
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def resolve_name(name, keys) -> str:
    """The key a typed name refers to, or ``""`` when nothing matches.

    Config can't offer a picker — only the UI can — so modes and sensors are
    referred to by typed name, and a typed name is a typo waiting to happen.
    Exact match first, then a loose one that forgives spacing, punctuation and
    capitalisation, because "linefill" and "Line Fill" are obviously the same
    thing to everyone except a string comparison.

    Exact always wins, so a site with two genuinely distinct names keeps them;
    only if nothing matches exactly does the loose pass run, and there first
    definition wins.
    """
    if not str(name or "").strip():
        return ""
    keys = list(keys)
    exact = sanitise(name)
    if exact in keys:
        return exact
    wanted = _loose(name)
    for key in keys:
        if _loose(key) == wanted:
            return key
    return ""


def _unique_key(value: str, taken: set[str]) -> str:
    """A transition key nothing else has claimed.

    Two rules between the same pair of modes are unusual but legal — a sensor
    and a timer racing each other, say — so the second gets a suffix rather than
    silently sharing the first one's dashboard control.
    """
    key, suffix = value, 2
    while key in taken:
        key = f"{value}_{suffix}"
        suffix += 1
    taken.add(key)
    return key


def _unique(value: str, taken: set[str], label: str) -> str:
    """A sanitised name nothing else has claimed, complaining if it collided."""
    name = value
    suffix = 2
    while name in taken:
        name = f"{value}_{suffix}"
        suffix += 1
    if name != value:
        log.warning(
            "Run mode name for %r collides with another mode; using %r instead. "
            "Give the modes distinct names.",
            label,
            name,
        )
    taken.add(name)
    return name


def _default_mode() -> RunMode:
    """The plain Fixed mode the app can always fall back to."""
    return RunMode(
        label=DEFAULT_MODE_LABEL,
        sanitised=sanitise(DEFAULT_MODE_LABEL),
        control=CONTROL_FIXED,
        synthesised=True,
    )


def _mode_outputs(slot, by_output: dict, mode_label: str) -> dict:
    """Which outputs this mode drives, resolved to the configured ones.

    A row naming an output that doesn't exist is dropped with a warning rather
    than silently doing nothing: the operator meant to switch something.
    """
    states: dict = {}
    for index, row in enumerate(
        cfg_value(getattr(slot, "outputs", None), []) or []
    ):
        typed = str(cfg_value(getattr(row, "output", None), "") or "")
        if not typed.strip():
            continue
        key = resolve_name(typed, by_output)
        if not key:
            log.warning(
                "Run mode %r sets output %d to %r, which is not a configured "
                "digital output — ignoring it.",
                mode_label,
                index + 1,
                typed,
            )
            continue
        states[key] = bool(cfg_value(getattr(row, "state", None), False))
    return states


def build_modes(config, sensors=(), outputs=()) -> list[RunMode]:
    """Parse the configured run modes, in order.

    Always returns at least one: with nothing configured — or nothing usable —
    the app still has to be able to run the pump, so the plain Fixed mode is
    synthesised. That is exactly how this app behaved before modes existed.

    That same mode is also appended to a configured programme unless the site
    turns it off, so there is always somewhere to take manual control of engine
    speed. It goes last, leaving the first *configured* mode as the one an
    unusable selection falls back to. A site that names a mode "Fixed" itself
    keeps its own and gets no duplicate.
    """
    modes: list[RunMode] = []
    taken: set[str] = set()
    # Callers that only need labels and element names (the tag factory) pass no
    # sensors; the variable then keeps its typed form, which they never read.
    by_sensor = {s.sanitised: s for s in sensors}
    by_output = {o.sanitised: o for o in outputs}

    for index, slot in enumerate(
        cfg_value(getattr(config, "run_modes", None), []) or []
    ):
        label = str(cfg_value(getattr(slot, "label", None), "") or "")
        if not label.strip():
            log.warning("Ignoring run mode %d: no name set.", index + 1)
            continue
        control_raw = str(cfg_value(getattr(slot, "control", None), "") or "")
        is_regulate = sanitise(control_raw) == sanitise(RUN_MODE_REGULATE)
        is_panel = sanitise(control_raw) == sanitise(RUN_MODE_PANEL)
        panel_setpoint = cfg_value(getattr(slot, "panel_setpoint", None))
        panel_min = cfg_value(getattr(slot, "panel_setpoint_min", None), 0) or 0
        panel_max = cfg_value(getattr(slot, "panel_setpoint_max", None), 100)
        if panel_max is None:
            panel_max = 100
        variable_label = str(cfg_value(getattr(slot, "variable", None), "") or "")
        setpoint = cfg_value(getattr(slot, "setpoint", None))
        sensitivity = cfg_value(getattr(slot, "sensitivity", None))
        target_rpm = cfg_value(getattr(slot, "target_rpm", None))
        minimum = cfg_value(getattr(slot, "minimum_seconds", None), 0) or 0
        variable = resolve_name(variable_label, by_sensor) or (
            sanitise(variable_label) if variable_label else ""
        )
        sensor = by_sensor.get(variable)
        modes.append(
            RunMode(
                label=label,
                sanitised=_unique(sanitise(label) or f"mode_{index}", taken, label),
                control=(
                    CONTROL_REGULATE
                    if is_regulate
                    else CONTROL_PANEL if is_panel else CONTROL_FIXED
                ),
                target_rpm=None if target_rpm is None else int(target_rpm),
                variable=variable,
                # The sensor's own label once it resolves, so the dashboard reads
                # in the operator's words rather than whatever they typed here.
                variable_label=sensor.label if sensor is not None else variable_label,
                setpoint=None if setpoint is None else float(setpoint),
                sensitivity=None if sensitivity is None else float(sensitivity),
                panel_setpoint=None if panel_setpoint is None else float(panel_setpoint),
                panel_setpoint_min=float(panel_min),
                panel_setpoint_max=float(panel_max),
                minimum_seconds=float(minimum),
                outputs=_mode_outputs(slot, by_output, label),
            )
        )

    include_default = bool(
        cfg_value(getattr(config, "include_default_fixed_run_mode", None), True)
    )
    if not modes or (include_default and sanitise(DEFAULT_MODE_LABEL) not in taken):
        modes.append(_default_mode())
    return modes


# Which config field feeds the runtime hold, per condition kind. Timer is absent
# on purpose: it is already a duration, so a hold on top would just be a second
# timer, and the form doesn't offer one.
_HOLD_FIELDS = {
    WHEN_SENSOR: "sensor_hold_seconds",
    WHEN_PANEL: "panel_hold_seconds",
}


def _hold_seconds(slot, when: str) -> float:
    """How long this transition's condition must hold before it fires."""
    field = _HOLD_FIELDS.get(when)
    if field is None:
        return 0.0
    return float(cfg_value(getattr(slot, field, None), 0) or 0)


def build_transitions(config, modes: list[RunMode], sensors=()) -> list[RunTransition]:
    """Parse the configured transitions, in the order they will be checked.

    Names are resolved to the labels of the modes they matched, so the dashboard
    can describe a rule using the operator's own words. A name that matches
    nothing is kept as typed — :func:`validate` is what reports it, and dropping
    it here would leave the operator with a rule that silently vanished.
    """
    by_name = {mode.sanitised: mode for mode in modes}
    by_sensor = {s.sanitised: s for s in sensors}
    transitions: list[RunTransition] = []
    keys: set[str] = set()

    for index, slot in enumerate(
        cfg_value(getattr(config, "run_mode_transitions", None), []) or []
    ):
        to_label = str(cfg_value(getattr(slot, "to_mode", None), "") or "")
        if not to_label.strip():
            log.warning("Ignoring transition %d: no To Mode set.", index + 1)
            continue
        from_label = str(cfg_value(getattr(slot, "from_mode", None), "") or "")
        when_raw = sanitise(str(cfg_value(getattr(slot, "when", None), "") or ""))
        when = {
            sanitise(TRANSITION_TIMER): WHEN_TIMER,
            sanitise(TRANSITION_SENSOR): WHEN_SENSOR,
            sanitise(TRANSITION_PANEL): WHEN_PANEL,
        }.get(when_raw, WHEN_SENSOR)

        # Names are matched forgivingly — see resolve_name. An unresolvable one
        # is kept as typed so validate() can name it back to the operator rather
        # than the rule silently vanishing.
        # The reserved destination is checked before the modes, so a rule aiming
        # at it can't be quietly captured by a mode that happens to share the
        # name — validate() reports that clash rather than letting it decide.
        if _loose(to_label) == SHUTDOWN:
            to_key = SHUTDOWN
        else:
            to_key = resolve_name(to_label, by_name) or sanitise(to_label)
        from_key = (
            (resolve_name(from_label, by_name) or sanitise(from_label))
            if from_label.strip()
            else ""
        )
        variable_label = str(cfg_value(getattr(slot, "variable", None), "") or "")
        setpoint = cfg_value(getattr(slot, "setpoint", None))
        panel_label = str(cfg_value(getattr(slot, "panel_flag", None), "") or "")
        panel_state = str(cfg_value(getattr(slot, "panel_state", None), "") or "")
        variable = resolve_name(variable_label, by_sensor) or (
            sanitise(variable_label) if variable_label else ""
        )
        watched = by_sensor.get(variable)

        transitions.append(
            RunTransition(
                index=index,
                to_mode=to_key,
                to_label=(
                    SHUTDOWN_LABEL
                    if to_key == SHUTDOWN
                    else (by_name[to_key].label if to_key in by_name else to_label)
                ),
                when=when,
                from_mode=from_key,
                from_label=(
                    by_name[from_key].label if from_key in by_name else from_label
                ),
                after_seconds=float(
                    cfg_value(getattr(slot, "after_seconds", None), 60) or 0
                ),
                variable=variable,
                variable_label=(
                    watched.label if watched is not None else variable_label
                ),
                direction=str(cfg_value(getattr(slot, "direction", None), "Above")),
                setpoint=None if setpoint is None else float(setpoint),
                panel_flag=sanitise(panel_label) if panel_label else "",
                panel_flag_label=panel_label,
                panel_on=sanitise(panel_state) == sanitise(PANEL_FLAG_ON),
                hold_seconds=_hold_seconds(slot, when),
                adjustable=bool(cfg_value(getattr(slot, "adjustable", None), False)),
                # Named for the pair it moves between rather than its position,
                # so re-ordering the rules can't move one rule's dialled-in
                # number onto another.
                key=_unique_key(f"{from_key or 'any'}_to_{to_key}", keys),
            )
        )
    return transitions


def build_sequencer(config, sensors=(), outputs=()) -> ModeSequencer:
    """Parse config into a ready-to-run sequencer.

    ``sensors`` lets the typed sensor names in Regulate modes and Sensor
    transitions be resolved to real ones here, once, so nothing downstream has to
    match names again.
    """
    modes = build_modes(config, sensors, outputs)
    start = str(cfg_value(getattr(config, "start_in_mode", None), "") or "")
    by_name = {mode.sanitised: mode for mode in modes}
    return ModeSequencer(
        modes,
        build_transitions(config, modes, sensors),
        start_mode=(
            (resolve_name(start, by_name) or sanitise(start))
            if start.strip()
            else ""
        ),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(sequencer: ModeSequencer, sensors) -> list[str]:
    """Config problems worth an operator's attention, as human-readable lines.

    Returned rather than logged so the application can do both — log them and
    put them on the dashboard, the way startup analog pin problems already ride
    along in ``analog_fault_string``. A name that can't be resolved is reported
    with the names that *are* available, because the whole reason these are typed
    rather than picked is that config can't offer a list.
    """
    problems: list[str] = []
    by_sensor = {s.sanitised: s for s in sensors}
    mode_names = ", ".join(m.label for m in sequencer.modes) or "none"
    sensor_names = ", ".join(s.label for s in sensors) or "none configured"

    for mode in sequencer.modes:
        if mode.sanitised == SHUTDOWN:
            problems.append(
                f"Run mode '{mode.label}' uses a reserved name: a transition "
                f"pointing at '{SHUTDOWN_LABEL}' stops the engine rather than "
                f"selecting a mode. Rename it."
            )
        if mode.is_panel:
            if mode.panel_setpoint_max <= mode.panel_setpoint_min:
                problems.append(
                    f"Run mode '{mode.label}' has a Maintain Value slider maximum "
                    f"at or below its minimum."
                )
            continue
        if not mode.is_regulate:
            # Required in config, but a config saved before it was — or a row
            # half-filled — still reaches here, and running at some app-chosen
            # speed nobody picked is worse than saying so.
            if mode.target_rpm is None and not mode.synthesised:
                problems.append(f"Run mode '{mode.label}' has no Target RPM.")
            continue
        if not mode.variable:
            problems.append(
                f"Run mode '{mode.label}' regulates but names no variable."
            )
            continue
        sensor = by_sensor.get(mode.variable)
        if sensor is None:
            problems.append(
                f"Run mode '{mode.label}' regulates '{mode.variable_label}', "
                f"which is not a configured sensor. Sensors: {sensor_names}."
            )
        elif not sensor.allow_rpm_control:
            problems.append(
                f"Run mode '{mode.label}' regulates '{sensor.label}', which is "
                f"not allowed to control RPM. Turn on Allow RPM Control on that "
                f"sensor, or pick another."
            )
        if mode.setpoint is None:
            problems.append(
                f"Run mode '{mode.label}' regulates but has no starting setpoint."
            )

    if sequencer.start_mode_name and sequencer.start_mode is None:
        problems.append(
            f"Start In Mode names '{sequencer.start_mode_name}', which is not a run "
            f"mode. Modes: {mode_names}."
        )

    for transition in sequencer.transitions:
        where = f"Transition {transition.index + 1}"
        known = {m.sanitised for m in sequencer.modes} | {SHUTDOWN}
        if transition.to_mode not in known:
            problems.append(
                f"{where} moves to '{transition.to_label}', which is not a run "
                f"mode. Modes: {mode_names}, or '{SHUTDOWN_LABEL}' to stop the "
                f"engine."
            )
        if transition.from_mode and transition.from_mode not in known:
            problems.append(
                f"{where} applies in '{transition.from_label}', which is not a "
                f"run mode. Modes: {mode_names}."
            )
        if transition.from_mode and transition.from_mode == transition.to_mode:
            problems.append(f"{where} moves '{transition.from_label}' to itself.")

        if transition.when == WHEN_SENSOR:
            if not transition.variable:
                problems.append(f"{where} watches a sensor but names none.")
            elif transition.variable not in by_sensor:
                problems.append(
                    f"{where} watches '{transition.variable_label}', which is not "
                    f"a configured sensor. Sensors: {sensor_names}."
                )
            if transition.setpoint is None:
                problems.append(f"{where} watches a sensor but has no setpoint.")
        elif transition.when == WHEN_PANEL and transition.panel_flag not in PANEL_FLAG_ATTRS:
            problems.append(
                f"{where} watches panel flag '{transition.panel_flag_label}', "
                f"which this app doesn't read."
            )

    return problems


def advisories(sequencer: ModeSequencer) -> list[str]:
    """Observations worth a log line, but not worth alarming anyone about.

    Kept apart from :func:`validate` on purpose. These describe configurations
    that work exactly as written — a mode you only ever pick by hand is a
    perfectly good mode, and is the whole point of the default Fixed one — so
    putting them on the dashboard alongside a mode that names a sensor which
    doesn't exist would teach operators that the status line cries wolf.
    """
    targeted = {t.to_mode for t in sequencer.transitions}
    reachable_anyway = {sequencer.fallback_mode.sanitised}
    notes: list[str] = []
    # With no Start In Mode set a start resumes whatever was left, so every mode
    # is reachable and there is nothing to observe.
    for mode in (sequencer.modes if sequencer.start_mode_name else []):
        if (
            mode.sanitised in targeted
            or mode.sanitised in reachable_anyway
            or mode.synthesised
        ):
            continue
        notes.append(
            f"Run mode '{mode.label}' is only ever selected by hand — no "
            f"transition moves into it."
        )
    return notes
