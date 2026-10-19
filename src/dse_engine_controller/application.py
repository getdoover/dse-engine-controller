"""Doover application for the DSE E-series engine controllers (E400, E800).

Polls the GenComm register pages the app needs (see ``gencomm.READ_PLAN``) in
two tiers — the live ones every loop, the slow-moving ones every
``slow_poll_period`` — decodes them into tags, and surfaces remote control
through the UI. Commands are GenComm system control keys: a key and its
complement written to page 16 in one Modbus write. Engine speed is set either
by writing the module's target speed register directly, or by moving the set
point of the module's own Maintain Value loop; which one depends on the active
run mode.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from typing import NamedTuple

from pydoover import ui
from pydoover.docker import Application
from pydoover.utils.utils import sanitize_display_name as sanitise

from .analog import DEFAULT_HOLD_SECONDS, AnalogInput
from .app_config import DseEngineControllerConfig
from .app_state import DseEngineControllerState
from .app_tags import DseEngineControllerTags
from .app_ui import DseEngineControllerUI
from .autostart import AutoStartController
from .outputs import build_outputs, desired_states
from .regulator import DEFAULT_SENSITIVITY, RpmRegulator
from .run_modes import (
    PANEL_FLAG_ATTRS,
    WHEN_SENSOR,
    advisories as run_mode_advisories,
    build_sequencer,
    validate as validate_run_modes,
)
from .sensors import SensorSlot, build_sensors
from . import gencomm as gc
from .app_tags import flex_hidden_tag, flex_value_tag
from .dse_state import DseModel, DseStateStore

log = logging.getLogger(__name__)


def _round(value, places: int):
    """round(), but an absent reading (None) stays absent.

    Registers the panel doesn't populate decode to None rather than an
    impossible number — see gencomm.u16 and friends.
    """
    return None if value is None else round(value, places)


def _control_mode(value) -> str:
    """Normalise a Start/Stop Control select payload to manual|sensor|timer."""
    mode = str(value or "").lower()
    return mode if mode in ("manual", "sensor", "timer") else "manual"


# Keys this app left behind in its tag_values namespace. Nothing reads or
# writes them any more, but a device that has been through several releases
# still carries them in the aggregate, holding whatever value they had when the
# code that wrote them was removed — which reads as live data on an API dump and
# has already caused confusion (a stale analog_value_0 sitting beside a current
# analog_value_pin2 with a different reading).
#
# Three generations of them:
#   * Sensor tags keyed by list index, before they were keyed by AI pin /
#     sanitised label so reordering the config couldn't move one sensor's
#     history onto another's tags.
#   * Flags for features that no longer exist (fault reset, the sensor-fault
#     banner, button visibility now handled by `disabled`).
#   * The operator settings that moved into the ui_cmds aggregate.
#
# This block can be removed once no installed device predates it.
STALE_TAG_KEYS = (
    # Index-keyed sensor tags → analog_{value,raw,fault}_pin<N>.
    *(f"analog_{part}_{i}" for part in ("value", "raw", "fault") for i in range(6)),
    # Index-keyed setpoint-slider visibility flags → <prefix>_<sanitised label>.
    *(
        f"{prefix}_{i}"
        for prefix in ("regulate_var_hidden", "start_var_hidden", "stop_var_hidden")
        for i in range(12)
    ),
    # Removed features.
    "reset_btn_hidden",
    "sensor_fault_hidden",
    "start_btn_hidden",
    "stop_btn_hidden",
    "target_rpm_hidden",
    "control_hidden",
    # Operator settings, now kept on the controls themselves in ui_cmds.
    "target_rpm_value",
    "rpm_mode",
    # The clutch is a configured Digital Output now, driven by the run modes.
    "clutch_engaged",
    # Superseded by the per-mode panels and the run_mode naming (see run_modes.py).
    "rpm_fixed_hidden",
    "rpm_regulate_hidden",
    "rpm_mode_hidden",
    "target_rpm",
    "run_mode",
    "regulate_variable",
    "regulate_sensitivity",
    "regulate_setpoint",
    "start_control_mode",
    "stop_control_mode",
    "start_variable",
    "stop_variable",
    "start_setpoint",
    "stop_setpoint",
)

# Operator settings left in the ui_cmds aggregate by the generation of controls
# the run modes replaced. Unlike a tag, nothing overwrites these — the app just
# stops reading them, and they sit there holding their last value next to the
# per-mode controls that superseded them. A wall display reading ui_cmds sees
# `rpm_mode: regulate` and a setpoint that looks current and is not.
#
# This block can be removed once no installed device predates it.
STALE_COMMAND_KEYS = frozenset(
    {
        # The Fixed/Regulate pair and its settings → one panel per configured
        # mode, named mode_<mode>_{target_rpm,setpoint,sensitivity}.
        "rpm_mode",
        "target_rpm",
        "regulate_variable",
        "regulate_sensitivity",
    }
)

# The per-sensor Regulate setpoint sliders, in both generations they had —
# keyed by list index and later by sanitised sensor name. The whole family is
# dead (a Regulate mode's sensor is config, so it has one setpoint, on the
# mode), which is what makes a bare prefix safe here.
STALE_COMMAND_PREFIXES = ("regulate_setpoint_",)

# Start / Stop trigger setpoints keyed by list index, from before they were
# keyed by sanitised sensor name. Only the index-keyed ones are stale —
# `start_setpoint_discharge_pressure` is a live control — so these match on a
# numeric suffix rather than the prefix alone.
_INDEXED_COMMAND_RE = re.compile(r"^(?:start|stop)_setpoint_\d+$")


def _is_stale_command(key: str) -> bool:
    """Whether a ui_cmds entry belongs to a generation of controls now gone."""
    return (
        key in STALE_COMMAND_KEYS
        or key.startswith(STALE_COMMAND_PREFIXES)
        or bool(_INDEXED_COMMAND_RE.match(key))
    )


# Min gap between sequential modbus writes. GenComm asks for no more than ten
# writes a second to spare the module's memory; one every 200 ms is well inside
# that and still lets a key pair and a speed write land in the same poll.
WRITE_INTER_GAP_S = 0.2

# Panel prep timings. A DSE clears latched alarms on a reset-alarms key (or on
# entering Stop mode) without rebooting, so the reset can be re-issued every
# few seconds while an alarm persists, and the Auto key retried until the mode
# register echoes it.
PANEL_RESET_INTERVAL_S = 5.0
PANEL_AUTO_RETRY_S = 3.0

# Rough wall time a panel sitting faulted in Stop mode needs from the moment a
# start is requested: an alarm reset, the Auto key, and a few seconds of
# cranking. Measured from the press, not added to the pre-start countdown — the
# handshake runs alongside the countdown — so it is a floor on Start Timeout
# Seconds rather than something to add to it. Used only to flag an unworkable
# config at startup (see _check_start_budget).
PANEL_PREP_BUDGET_S = PANEL_RESET_INTERVAL_S + 12.0

# How long a stop is given to take effect through the module's own stopping
# sequence (cancel the telemetry start, let it run its return delay and cool
# down) before the app escalates to Stop mode. Covers a long cool-down; what it
# catches is an engine kept running by something other than our start request —
# a physical remote-start input, or a start made at the fascia in Manual —
# which withdrawing our request can't touch.
STOP_ESCALATION_S = 180.0

# How long after a failed startup-tier read (supported keys, flexible sensor
# unit info) before trying it again.
STARTUP_READ_RETRY_S = 60.0

# How often the module's supported-key bitmap and sensor unit info are
# re-read once they have been seen. Both are configuration, so rarely.
STARTUP_READ_REFRESH_S = 10 * 60.0

# How far the module's echoed Maintain Value set point may differ from what we
# wrote (in raw register counts) before we re-write it, and how often an
# unchanged set point is re-asserted at most.
SETPOINT_ECHO_TOLERANCE = 1
SETPOINT_REASSERT_INTERVAL_S = 10.0

# Pre-start horn pulse: on for HORN_ON_S, off for HORN_OFF_S, phased on the wall
# clock so every pump on site pulses in unison rather than fighting.
HORN_ON_S = 3.0
HORN_OFF_S = 10.0
HORN_CYCLE_S = HORN_ON_S + HORN_OFF_S


# Templates saved before the warning became open-ended still carry a countdown
# placeholder. There is no honest number to put in it any more — the warning runs
# until the panel can take a start — so the clause is reworded on the way out
# rather than left to speak "in 0 seconds" or a literal brace.
_COUNTDOWN_CLAUSE = re.compile(
    r"\bin\s+\{seconds\}\s*(?:seconds|second|secs|sec|s)\b", re.IGNORECASE
)


def _drop_countdown(template: str) -> str:
    """Reword a legacy ``{seconds}`` template into one without a figure."""
    if "{seconds}" not in template:
        return template
    text = _COUNTDOWN_CLAUSE.sub("shortly", template)
    return text.replace("{seconds}", "shortly")


def _horn_phase(elapsed: float) -> float:
    """Where the horn is in its cycle: seconds since the current blast began.

    Shared by the pulse itself and by the announcement timing that dodges it, so
    the two can't drift apart.
    """
    return int(elapsed) % HORN_CYCLE_S


# Beacon/horn warning outputs are coordinated across pumps via a per-pin global
# "active until" deadline tag. Each warning pump pushes the deadline this far
# ahead every loop; the output stays on until the latest deadline passes, so
# multiple pumps cooperate and a lone pump still works.
WARNING_HOLD_MARGIN_S = 1.5

# How far the panel's echoed target speed may differ from what we commanded
# before we re-write it.
RPM_ECHO_TOLERANCE = 1

# Minimum gap between re-asserting an unchanged target RPM. A panel that ignores
# the setpoint — because it's in Manual, say — would otherwise be written to on
# every poll, for no benefit and a lot of log noise.
RPM_REASSERT_INTERVAL_S = 10.0

# How recently comms must have been lost for a not-running, not-in-Auto panel to
# read as "freshly restarted" rather than "the engine stopped". A module that has
# been power-cycled is unreachable for a few seconds and then boots into Stop
# mode, so the two arrive together; an engine that trips while comms are fine
# does not.
PANEL_RESTART_WINDOW_S = 60.0

# Consecutive polls a new engine running/stopped reading must hold before the
# auto-start machine acts on it. Debounces a single glitchy modbus decode so it
# can't false-fault a healthy engine.
RUNNING_DEBOUNCE = 3

# How long the auto-start warning indicator keeps showing a fault nobody has
# acted on. The fault itself stays latched — the machine deliberately waits for a
# human — but an indicator still shouting about yesterday has stopped being news
# and only trains people to ignore it. A *new* fault shows again immediately.
AUTOSTART_FAULT_LATCH_S = 24 * 60 * 60

# Hard floor on the pre-start warning so the beacon/horn can never be skipped,
# even if an old config was saved with a smaller value.
MIN_WARNING_SECONDS = 5.0

# How long after startup the pump-power output is left exactly as it is when we
# can't see the panel. A digital output can't be read back, so a container
# restart that lands mid-run would otherwise drive the pin to the state the app
# computes from an empty picture — and on a pump that is running under our own
# power output, that means cutting its power. So we don't guess: the hold is
# released the moment a single register read succeeds (which tells us whether
# the engine is running), and on this timeout otherwise, so a pin left on by a
# panel that never comes back isn't held on forever.
POWER_STARTUP_HOLD_S = 180.0

# How long after switching the panel's power on the app will still describe the
# silence as "Starting". A DSE boots in well under this; past it the panel is
# not coming up on its own, and the tile should stop implying a start is under
# way (the Auto Start State field beside it still reads "Ready").
PANEL_POWER_UP_S = 60.0

# How long a spoken announcement stays worth playing once it has been sent. The
# speaker drops anything it couldn't get to in time rather than playing it after
# the moment has passed.
ANNOUNCEMENT_EXPIRY_S = 10.0

# How often the pre-start announcement repeats while the warning runs. The
# warning no longer has a fixed length — it stays open until the panel can take
# a start — so there is no single "n seconds to go" moment to announce. Repeating
# is what a warning of unknown length can honestly do, and it is what someone
# walking up part-way through needs.
PRESTART_REPEAT_S = 20.0

# Clear air an announcement needs before we start speaking it. The horn is far
# louder than any speaker, so a phrase that lands under a blast is simply lost —
# and the two would otherwise collide by construction, since the horn is phased
# to sound the instant the warning begins and that is when the opening
# announcement is due. Both run on the same fixed cycle (HORN_ON_S sounding,
# HORN_OFF_S silent), so rather than race it we wait for the silence and speak
# into that. Sized to fit a spoken phrase inside the HORN_OFF_S gap with room
# either side.
ANNOUNCEMENT_QUIET_S = 5.0


class _Announcement(NamedTuple):
    """One announcement waiting for a gap in the horn.

    ``prestart`` marks the phrases that only make sense while the warning is
    still running, so one still queued when the warning ends is dropped rather
    than spoken into a different situation. ``speak_by`` is the monotonic
    deadline past which it goes out over the horn anyway, set by whoever queued
    it to the point where waiting longer costs more than clashing.
    """

    template: str
    prestart: bool
    speak_by: float

# Both expressed in poll periods, so they scale with the configured poll rate.
#
# Longest timestep the regulate loop will integrate in one go. Steps get skipped
# whenever the loop can't run — a faulted sensor, a comms outage, the panel
# disappearing — and time the loop couldn't observe is not time it should
# integrate: handing the integrator the whole gap dumps it in one step and slams
# the commanded RPM toward a rail. Two periods rather than one so ordinary loop
# jitter (a slow modbus read) still integrates its real elapsed time.
MAX_REGULATE_DT_PERIODS = 2

# A gap of this many periods means we've lost the thread: the integral is
# re-seeded from the engine's current RPM, because the engine may have been moved
# — or the panel taken over in Manual — while we weren't stepping.
REGULATE_RESEED_PERIODS = 3

# A faulted analog input holds its last value for this long before publishing
# None, so a dead loop blanks the gauge instead of showing a plausible number
# that never changes. Floored at two poll periods, so a single missed read can't
# blank the display on a slow-polling install.
ANALOG_HOLD_SECONDS = DEFAULT_HOLD_SECONDS

# How far a native analog reading must move, as a fraction of its configured
# engineering span, before it's logged as a data point. Live republishing keeps
# the dashboard current regardless; this only governs stored history, so it can be
# loose enough to keep an idling sensor quiet.
ANALOG_LOG_DELTA_FRACTION = 0.005

# Log a reading at least this often even when it hasn't moved, so plots have
# anchor points through long steady periods.
ANALOG_LOG_HEARTBEAT_S = 300.0

# Granularity of the pre-start countdown reported back on the in-flight ui_cmd.
# Each progress update is its own channel write, so the countdown steps rather
# than ticking every second. prestart_remaining_s carries the per-second value
# for anything that wants it.
PROGRESS_COUNTDOWN_STEP_S = 5

# How often progress is re-reported on an in-flight command even when the text
# hasn't changed. Progress doubles as the site's proof the device is alive, and
# a phase like "Cranking…" can sit unchanged for minutes, so it heartbeats. Must
# stay comfortably under the buttons' command_pending_timeout (see app_ui).
PROGRESS_HEARTBEAT_S = 30.0


class DseEngineControllerApplication(Application):
    config: DseEngineControllerConfig
    tags: DseEngineControllerTags

    config_cls = DseEngineControllerConfig
    tags_cls = DseEngineControllerTags
    ui_cls = DseEngineControllerUI

    async def setup(self):
        self.model: DseModel = self.config.model.value
        self.store = DseStateStore(model=self.model)
        self.controller_state = DseEngineControllerState()
        self._last_write_time: float = 0.0
        # The sparse register image the snapshots are decoded from. Fast-tier
        # blocks are refreshed every poll; slow and startup tiers keep their
        # last good values between their own reads.
        self._register_image: dict[int, int] = {}
        self._last_slow_poll: float = 0.0
        self._startup_read_at: float | None = None  # when the startup tier last succeeded
        self._startup_read_failed_at: float = 0.0
        # Page 16 offsets 0..7: which control keys the module advertises.
        self._supported_keys: list[int] | None = None
        # Maintain Value set point write tracking (mirrors the target-RPM pair).
        self._last_setpoint_written: int | None = None
        self._last_setpoint_assert: float = 0.0
        # When the current stop began, for escalating from a withdrawn start
        # request to Stop mode (see _maybe_send_stop).
        self._stop_started_at: float | None = None

        # --- Auto-start orchestration ---
        self.autostart: AutoStartController | None = None
        if bool(self.config.enable_remote_control.value):
            self.autostart = AutoStartController(
                warning_seconds=max(
                    MIN_WARNING_SECONDS, float(self.config.prestart_warning_seconds.value)
                ),
                start_timeout=float(self.config.start_timeout_seconds.value),
                stop_timeout=float(self.config.stop_timeout_seconds.value),
            )
        # Operator run latch (set by Start, cleared by Stop / Cancel). Combined
        # with the cross-app `run_request` tag to drive the state machine.
        self._operator_run_latch: bool = False
        # After an operator stop, ignore the run-request tag until it goes low
        # again, so a stuck tag can't immediately re-trigger a start.
        self._suppress_run_tag: bool = False
        # Timer-mode one-shot latches: the scheduled epoch-ms value we last acted
        # on, so a reached time fires exactly once (not every loop after it, and
        # not again next idle/run cycle). Cleared when the operator picks a new
        # time (a different value).
        self._start_timer_fired_at: float | None = None
        self._stop_timer_fired_at: float | None = None
        # Rate-limit timers for the panel command choreography.
        self._last_start_cmd: float = 0.0
        self._last_stop_cmd: float = 0.0
        # Panel prep handshake: alarms are reset while the module reports a
        # lockout, and the Auto key retried until the mode register echoes it.
        self._reset_issued: bool = False
        self._last_reset_cmd: float = 0.0
        self._last_auto_cmd: float = 0.0
        # Last value written to each digital output, to avoid redundant writes.
        self._last_beacon_out: bool | None = None
        self._last_horn_out: bool | None = None
        # Warning-episode start per output, used only when no DO pin is set (the
        # pin case shares an episode start across pumps via a global tag).
        self._warn_local_start: dict[str, float] = {}
        # When the pre-start announcement was last actually spoken, so the
        # repeat is paced off the utterance rather than off the loop.
        self._last_prestart_spoken: float = 0.0
        # Announcement queued and waiting for a gap in the horn (see
        # _speak_pending). At most one: a newer phrase supersedes an older one
        # that never found its gap.
        self._pending_announcement: _Announcement | None = None
        # Pump-power output: what we're holding it at (None until the startup
        # hold lifts), its last-written cache, and the hold itself.
        self._pump_power_on: bool | None = None
        self._last_pump_power_out: bool | None = None
        self._power_startup_hold: bool = True
        self._started_at: float = time.monotonic()
        # When the power output was last switched on, so the tile can tell a
        # panel that is still booting from one that is never coming up.
        self._power_on_since: float | None = None
        # Combined run demand from the last loop, so the power output and the
        # no-comms message can read it without re-running _compute_run_request
        # (which latches _suppress_run_tag and must fire once per loop).
        self._run_request_active: bool = False
        self._last_rpm_written: int | None = None
        self._last_rpm_assert: float = 0.0
        # Whether the engine is running under the panel's own Manual control, where
        # it owns engine speed and the RPM controls are hidden (see
        # _apply_rpm_mode_visibility).
        self._manual_while_running: bool = False
        # When comms were last lost, for spotting a panel that has just restarted
        # (see PANEL_RESTART_WINDOW_S).
        self._last_comms_loss: float | None = None
        # Debounced view of whether the engine is running (see RUNNING_DEBOUNCE).
        self._run_streak: int = 0
        self._stop_streak: int = 0
        self._running_confirmed: bool = False

        # --- Sensors (remote tags + native analog inputs) + regulate loop ---
        self._sensors = build_sensors(self.config, self.tags)
        # sanitised label -> sensor entry, for resolving picker selections.
        self._sensor_by_name = {s.sanitised: s for s in self._sensors}
        # One AnalogInput (scaling + filter + fault detection) per analog input,
        # keyed the same way.
        self._analog_slots = [s for s in self._sensors if s.is_analog]
        hold = max(ANALOG_HOLD_SECONDS, 2 * float(self.config.poll_period.value))
        self._analog = {
            s.sanitised: AnalogInput.from_slot(s, hold_seconds=hold)
            for s in self._analog_slots
        }
        self._last_analog_time: float = 0.0
        # Startup config problems (bad pin / wrong signal type). Kept so the
        # per-loop fault summary doesn't scrub them off the dashboard.
        self._analog_config_problems: list[str] = []
        # Pins that failed to read last poll, so a persistent outage logs once
        # rather than every poll.
        self._analog_failed_pins: set[int] = set()
        # Last logged value + when, per analog sensor (see _should_log_analog).
        self._analog_logged: dict[int, float] = {}
        self._analog_logged_at: dict[int, float] = {}
        # Set once a sensor fault has actually stopped the engine, so the fault
        # line on the dashboard says so; cleared when the fault clears.
        self._sensor_fault_stopped: bool = False
        self.regulator = RpmRegulator(
            rpm_min=int(self.config.target_rpm_min.value),
            rpm_max=int(self.config.target_rpm_max.value),
        )
        self._last_regulate_time: float = 0.0
        # Last logged (measured, setpoint) + when (see _should_log_regulate).
        self._regulate_logged: tuple[float, float] | None = None
        self._regulate_logged_at: float = 0.0
        # (variable, setpoint) last seen — re-seed the loop when either changes.
        self._regulate_key: tuple[str, float | None] | None = None

        await self._seed_command_defaults()

        # The mode in force, kept in step with the `run_mode` element it is
        # stored in — see _active_mode for why both exist.
        self._selected_mode = None

        # --- Digital outputs: whatever this device drives ---
        self._outputs = build_outputs(self.config, self.tags)
        # Last value written to each output pin, so a state that hasn't changed
        # isn't rewritten every poll.
        self._last_output_state: dict[int, bool] = {}
        self._check_output_pins()

        # --- Run modes: which of the configured modes drives engine speed ---
        self.sequencer = build_sequencer(self.config, self._sensors, self._outputs)
        self._run_mode_problems = validate_run_modes(self.sequencer, self._sensors)
        for problem in self._run_mode_problems:
            log.error("Run mode config: %s", problem)
        for note in run_mode_advisories(self.sequencer):
            # Worth knowing, not worth alarming anyone about — so it stays out of
            # _run_mode_problems, which reaches the dashboard.
            log.info("Run mode config: %s", note)
        await self._seed_run_mode()

        self.loop_target_period = float(self.config.poll_period.value)
        await self._apply_rpm_mode_visibility()
        await self._apply_control_mode_visibility()
        # Sync the per-sensor setpoint-slider visibility with any persisted
        # variable selections (Start / Stop).
        for select_name, hidden_prefix, sp_prefix in (
                ("start_variable", "start_var_hidden", "start_setpoint"),
                ("stop_variable", "stop_var_hidden", "stop_setpoint"),
        ):
            await self._select_sensor_variable(
                str(self._command_value(select_name, "")),
                select_name=select_name,
                hidden_prefix=hidden_prefix,
                setpoint_prefix=sp_prefix,
            )
        # A latched fault describes a start attempt this process never made: the
        # auto-start machine comes up in `idle` with no memory of it, so the app
        # can no longer vouch for the warning. The event itself is in the
        # activity log either way.
        await self._clear_autostart_fault()
        self._check_start_budget()
        if "{seconds}" in str(self.config.prestart_announcement.value or ""):
            log.warning(
                "Pre-Start Announcement still uses {seconds}. The warning now "
                "runs until the panel is ready to start, so there is no "
                "countdown to speak — the phrase is being reworded to "
                "'shortly'. Update the config to say what you want."
            )
        await self._init_outputs()
        await self._validate_analog_pins()
        await self._purge_stale_tags()
        await self._purge_stale_commands()

    def _check_output_pins(self) -> None:
        """Warn when a configured output shares a pin with a warning output.

        Both would drive it, on different schedules, and the pump would flicker
        its beacon in time with somebody's clutch.
        """
        reserved = {
            "Beacon Output Pin": self.config.beacon_do_pin.value,
            "Horn Output Pin": self.config.horn_do_pin.value,
            "Pump Power Output Pin": self.config.pump_power_do_pin.value,
        }
        seen: dict[int, str] = {}
        for output in self._outputs:
            for name, pin in reserved.items():
                if pin is not None and int(pin) == output.pin:
                    log.error(
                        "Digital output %r is on pin %s, which is also the %s. "
                        "Both will drive it and neither will win.",
                        output.label, output.pin, name,
                    )
            if output.pin in seen:
                # One pin has one state. The second definition wins outright and
                # the first's default and per-mode settings do nothing at all —
                # silently, since both still publish a tag.
                log.error(
                    "Digital outputs %r and %r are both on pin %s. Only %r will "
                    "have any effect.",
                    seen[output.pin], output.label, output.pin, output.label,
                )
            else:
                seen[output.pin] = output.label

    async def _drive_outputs(self) -> None:
        """Put every configured output where the run in progress wants it.

        Outputs follow the engine, not the sequence. Cooldown counts as running,
        because the engine is: dropping a clutch the instant Stop is pressed
        would dump the load with the engine still at speed, which is the one
        thing the old built-in clutch's disengage delay existed to avoid.
        Everything before the engine turns — the warning, the cranking — and
        everything after it stops sits at the off state, where run modes have no
        say.
        """
        if not self._outputs:
            return
        running = self.autostart is not None and self.autostart.state in (
            "running",
            "stopping",
        )
        mode = self._active_mode() if running else None
        states = desired_states(self._outputs, mode)
        for output in self._outputs:
            wanted = states[output.pin]
            if self._last_output_state.get(output.pin) != wanted:
                log.info("Output %s: %s", output.label, "on" if wanted else "off")
            await self._set_output_state(output, wanted)

    async def _set_output_state(self, output, wanted: bool) -> None:
        """Write one output, skipping a state it is already in.

        The cache is only updated after a successful write, so a failed one is
        retried next loop rather than being silently latched as done — the same
        rule _set_output_pin follows for the warning pins.
        """
        if self._last_output_state.get(output.pin) != wanted:
            try:
                await self.platform_iface.set_do(output.pin, wanted)
            except Exception as e:
                log.warning(
                    "Failed to set output %s (pin %s) = %s: %s",
                    output.label, output.pin, wanted, e,
                )
                return
            self._last_output_state[output.pin] = wanted
        if output.tag is not None:
            await output.tag.set(wanted)

    def _check_start_budget(self) -> None:
        """Warn if the start budget can't fit a start.

        Two independent things have to fit inside Start Timeout Seconds, and they
        run *concurrently* rather than one after the other (see
        :meth:`_drive_panel_commands`): the pre-start countdown, and the panel
        handshake, both measured from the press. So the budget has to clear the
        longer of the two, not their sum — worth getting right here, because the
        alternative is an operator discovering it one failed start at a time.
        """
        if self.autostart is None:
            return
        budget = self.autostart.start_timeout
        warning = self.autostart.warning_seconds
        if budget <= warning:
            log.error(
                "Start Timeout Seconds (%.0f) is not longer than Pre-Start "
                "Warning Seconds (%.0f). The countdown always runs in full, so "
                "every start will fault the moment it ends. Raise the timeout.",
                budget,
                warning,
            )
        elif budget < PANEL_PREP_BUDGET_S:
            log.warning(
                "Start Timeout Seconds (%.0f) is under the ~%.0fs a faulted "
                "panel needs to reset its alarms, reach Auto and crank. That "
                "handshake runs during the countdown rather than after it, so "
                "this is measured from the press — but starts from a latched "
                "alarm will still likely fault.",
                budget,
                PANEL_PREP_BUDGET_S,
            )

    # ------------------------------------------------------------------
    # Command values (modes, setpoints, target RPM)
    #
    # These live in the ui_cmds aggregate and nowhere else. They are the
    # operator's settings, so the control they were entered on is the record of
    # them — no shadow tag to drift out of step with what the dashboard shows.
    # Where the app doesn't act on the raw command (it normalises modes and
    # clamps setpoints into the sensor's or the panel's band) it coerces the
    # element to the value it settled on, and the loop then reads that back.
    # ------------------------------------------------------------------

    def _command_value(self, name: str, default=None):
        """Read a command element's value out of the ui_cmds aggregate.

        Returns *default* when the element has never been set — a fresh install,
        or a per-sensor slider for a sensor nobody has picked yet.
        """
        manager = getattr(self, "ui_manager", None)
        if manager is None:
            return default
        try:
            value = manager.get_value(name)
        except KeyError:
            return default
        return default if value is None else value

    def _selected_setpoint(self, select_name: str, prefix: str):
        """The setpoint of whichever sensor the *select_name* picker has chosen.

        Each sensor's slider carries its own value under ``<prefix>_<sensor>``,
        so the setpoint in force is the one belonging to the current selection.
        """
        sanitised = str(self._command_value(select_name, ""))
        if not sanitised:
            return None
        return self._command_value(f"{prefix}_{sanitised}")

    async def _set_command_value(self, name: str, value) -> None:
        """Coerce a command element to the value the app settled on.

        Called wherever the app doesn't simply keep what it was handed —
        clamping into the configured band, normalising a mode string, seeding a
        value at startup or across the Regulate → Fixed handover. Since the
        element is the store, this both records the value and makes the control
        show what the engine is actually being run at.

        ``log_update=False``: these are echoes of a command already in the
        activity log, or app-side seeding the operator never asked for. Logging
        them again would double up the audit trail.
        """
        if value is None:
            return
        manager = getattr(self, "ui_manager", None)
        if manager is None:
            return  # standalone construction (schema export, unit tests)
        try:
            await manager.set_value(name, value, log_update=False)
        except Exception as e:
            # The tag write already succeeded and drives the control loop; a
            # failed UI echo must not take the command down with it.
            log.warning("Could not mirror %s=%r into ui_cmds: %s", name, value, e)

    # Defaults for the command elements that used to come from a tag default.
    # Without these the selects render blank and the sensitivity slider empty on
    # a fresh install, even though the loop already falls back to these values.
    _COMMAND_DEFAULTS = {
        "start_control_mode": "manual",
        "stop_control_mode": "manual",
    }

    async def _seed_command_defaults(self) -> None:
        """Fill in any command element the operator has never touched."""
        for name, default in self._COMMAND_DEFAULTS.items():
            if self._command_value(name) is None:
                await self._set_command_value(name, default)

    async def _seed_run_mode(self) -> None:
        """Give every run mode's controls a value, and settle which one is active.

        An unset slider renders as an empty control and gives the loop nothing to
        hold, so each mode's controls are seeded from its configured starting
        values — the configured minimum RPM is the conservative fallback, being
        the slowest speed the panel will accept. An operator's existing value is
        always left alone.

        The active mode is whatever survived in ``rpm_mode``, which is how a
        restart picks the sequence up where it left off: the picker and every
        transition write that same element, so it persists without anything here
        having to save it. ``run_mode_entered_at`` carries the clock across with
        it, so a transition part-way through its timer doesn't start over.
        """
        rpm_min = int(self.config.target_rpm_min.value)
        rpm_max = int(self.config.target_rpm_max.value)
        sens_min = float(self.config.regulate_min_sensitivity.value)
        sens_max = float(self.config.regulate_max_sensitivity.value)
        for mode in self.sequencer.modes:
            if mode.is_panel:
                await self._seed_element(
                    mode.element_name("panel_setpoint"), mode.panel_setpoint
                )
            elif mode.is_regulate:
                await self._seed_element(
                    mode.element_name("setpoint"), mode.setpoint
                )
                await self._seed_element(
                    mode.element_name("sensitivity"),
                    max(sens_min, min(sens_max, mode.sensitivity or DEFAULT_SENSITIVITY)),
                )
            else:
                target = rpm_min if mode.target_rpm is None else int(mode.target_rpm)
                clamped = max(rpm_min, min(rpm_max, target))
                if clamped != target:
                    # Worth naming: the mode was configured to run at a speed the
                    # panel would ignore anyway, and silently running it 150 rpm
                    # elsewhere is not something to discover from a flow meter.
                    log.warning(
                        "Run mode %r is configured at %s RPM, outside the "
                        "Target RPM band (%s..%s) — using %s.",
                        mode.label, target, rpm_min, rpm_max, clamped,
                    )
                await self._seed_element(mode.element_name("target_rpm"), clamped)

        for transition in self.sequencer.transitions:
            name = transition.tunable_name
            if name is not None:
                await self._seed_element(name, transition.tunable_default)

        active = self.sequencer.resolve(self._command_value("run_mode", ""))
        self._selected_mode = active
        if self.sequencer.start_mode is None:
            log.info(
                "No Start In Mode configured — starts resume the last mode "
                "used, currently %r.",
                active.label,
            )
        await self._set_command_value("run_mode", active.sanitised)
        if self.tags.run_mode_entered_at.get() is None:
            await self.tags.run_mode_entered_at.set(time.time())
        await self._publish_run_mode()

    async def _seed_element(self, name: str, value) -> None:
        """Fill in one command element the operator has never touched."""
        if value is not None and self._command_value(name) is None:
            await self._set_command_value(name, value)

    async def _purge_stale_tags(self) -> None:
        """Clear tag keys left in the aggregate by earlier versions.

        Only keys actually present are written, so a device that has already
        been cleaned (or was commissioned after the renames) does nothing here.
        Setting a key to ``None`` is how a deletion is communicated upstream.

        ``app_key`` is not optional here, however much the signature suggests
        it. ``tag_values`` is namespaced per app, and pydoover's ``KeyPath``
        reads ``app_key=None`` as "the root of the aggregate" rather than "this
        app" — so without it the lookup misses every time, the list comes back
        empty and this returns having cleared nothing, which is how an earlier
        version behaved.
        """
        stale = [
            k
            for k in STALE_TAG_KEYS
            if self.tag_manager.get_tag(k, app_key=self.app_key) is not None
        ]
        if not stale:
            return
        for key in stale:
            await self.tag_manager.set_tag(key, None, app_key=self.app_key)
        log.info(
            "Cleared %d stale tag(s) left by earlier versions: %s",
            len(stale), ", ".join(stale),
        )

    async def _purge_stale_commands(self) -> None:
        """Clear ui_cmds entries left behind by the run-modes refactor.

        The operator settings moved from a fixed set of controls to one set per
        configured mode, and the old entries are not overwritten by that — they
        simply stop being read, and sit in the aggregate holding whatever they
        last held. Anything reading ui_cmds directly (a wall display, another
        app) sees a plausible-looking `rpm_mode` and setpoint next to the
        `run_mode` that replaced them, with nothing to say which is live.

        Unlike the tag manager above, ``UICommandsManager`` namespaces both
        reads and writes by ``app_key`` itself, so there is nothing to pass.
        ``values`` is read directly rather than through ``get_value``, which
        falls back to a declared element's default and raises for a name no
        element declares — which is every name in this list.
        """
        manager = getattr(self, "ui_manager", None)
        if manager is None:
            return  # standalone construction (schema export, unit tests)
        present = getattr(manager, "values", None) or {}
        stale = sorted(
            key
            for key, value in present.items()
            if value is not None and _is_stale_command(key)
        )
        if not stale:
            return
        for key in stale:
            try:
                await manager.set_value(key, None, log_update=False)
            except Exception as e:
                # Same rule as _set_command_value: tidying the aggregate must
                # never be the thing that stops the app coming up.
                log.warning("Could not clear stale ui_cmd %s: %s", key, e)
        log.info(
            "Cleared %d stale ui_cmd(s) left by earlier versions: %s",
            len(stale), ", ".join(stale),
        )

    async def _validate_analog_pins(self) -> None:
        """Warn (don't fail) when a configured analog pin looks wrong.

        Catches the two mistakes that otherwise show up as a mystery reading: a
        pin the device doesn't have, and a 4-20 mA sensor wired to a voltage
        input (on a doovit, AI 0-1 are 0-10 V and AI 2-5 are current loops).
        Older platform interfaces report no per-channel metadata, so a missing
        kind is not an error.
        """
        if not self._analog_slots:
            return
        try:
            details = await self.platform_iface.fetch_io_details()
        except Exception as e:
            log.warning("Could not read the device IO layout: %s", e)
            return
        if details is None:
            return

        channels = {c.channel: c for c in details.channels("AI")}
        problems: list[str] = []
        for slot in self._analog_slots:
            channel = channels.get(slot.pin)
            if channel is None:
                problems.append(
                    f"{slot.label}: no analog input {slot.pin} on this device"
                )
                continue
            # Compare units, not `kind`. pydoover documents kind as
            # "voltage"/"current", but the doovit reports the signal standard
            # ("4-20mA"), so matching on kind flagged every correctly-wired
            # current loop — a permanent config problem that latched
            # sensor_fault_active true and sat on the dashboard forever. Units
            # ("mA" / "V") are what both sides actually agree on.
            if not channel.units or not slot.signal_units:
                continue  # nothing to compare against
            if channel.units.strip().lower() != slot.signal_units.strip().lower():
                problems.append(
                    f"{slot.label}: AI {slot.pin} reads in {channel.units}, "
                    f"configured as {slot.signal_type} ({slot.signal_units})"
                )
        for problem in problems:
            log.warning("Analog input config: %s", problem)
        self._analog_config_problems = problems
        if problems:
            await self.tags.analog_fault_string.set("; ".join(problems))
            await self.tags.sensor_fault_active.set(True)

    async def main_loop(self):
        # Analog inputs first: the regulate loop and the sensor triggers later in
        # this same iteration then act on this loop's readings, not last loop's.
        #
        # Nothing here is caught on purpose. An unexpected exception means this app
        # can't be trusted to control an engine, and pydoover's own handler logs it,
        # marks the app unhealthy and exits so the container restarts — which is
        # both the recovery we want and not something worth reimplementing here.
        await self._poll_analog_inputs()
        await self._poll_modbus()
        await self._update_state_and_tags()
        if self.autostart is not None:
            await self._evaluate_auto_control()
            await self._run_autostart()
        # Last, so they act on this loop's run demand, panel snapshot and mode.
        # Outside the autostart branch: with remote control off the app still
        # wants the panel powered up to read it, and the outputs still rest
        # somewhere definite.
        await self._drive_pump_power()
        await self._drive_outputs()

    # ------------------------------------------------------------------
    # Native analog inputs
    # ------------------------------------------------------------------

    async def _poll_analog_inputs(self) -> None:
        """Read every configured analog input and publish its scaled value.

        All pins go out in one ``fetch_ai`` transaction, so adding inputs costs
        no extra round trips. Scaling, smoothing and fault detection live in
        :class:`~.analog.AnalogInput`; this only does the IO and the tags.
        """
        if not self._analog_slots:
            return

        now = time.monotonic()
        dt = now - self._last_analog_time if self._last_analog_time else 0.0
        self._last_analog_time = now

        readings = await self._read_analog_pins([s.pin for s in self._analog_slots])

        faults: list[str] = []
        for slot, raw in zip(self._analog_slots, readings):
            reading = self._analog[slot.sanitised]
            value = reading.update(raw, dt, now)
            await self._publish_analog_value(slot, value, now)
            await slot.raw_tag.set(
                None if reading.raw is None else round(float(reading.raw), 3)
            )
            await slot.fault_tag.set(reading.fault)
            if reading.fault:
                faults.append(f"{slot.label}: {reading.fault_reason}")

        if not faults:
            self._sensor_fault_stopped = False
        elif self._sensor_fault_stopped:
            faults.append("engine stopped")
        # Startup config problems (bad pin / wrong signal type) ride along, so a
        # healthy read doesn't scrub them off the dashboard.
        summary = self._analog_config_problems + faults
        await self.tags.analog_fault_string.set("; ".join(summary))
        await self.tags.sensor_fault_active.set(bool(summary))

    async def _read_analog_pins(self, pins: list[int]) -> list[float | None]:
        """Read all configured AI pins, one transaction where possible.

        A pin that can't be read yields ``None``, which its AnalogInput classifies
        as "no reading" — it holds its last value and faults after the grace
        period rather than publishing a zero.
        """
        readings = await self._fetch_ai(pins)
        if readings is None and len(pins) > 1:
            # The platform interface answers a batched read all-or-nothing: it
            # gathers one task per pin and errors the whole response if any of
            # them raises. Re-read the pins individually so one dead channel
            # faults only itself instead of blinding every input.
            readings = []
            for pin in pins:
                single = await self._fetch_ai([pin])
                readings.append(None if single is None else single[0])
        if readings is None:
            readings = [None] * len(pins)
        self._log_read_failures(pins, readings)
        return readings

    async def _publish_analog_value(
        self, slot: SensorSlot, value: float | None, now: float
    ) -> None:
        """Write a reading to its tag, deciding whether it's also worth logging.

        ``None`` means the input has gone stale (see AnalogInput.hold_seconds) —
        logged once on the way out so the plot breaks where the reading stopped
        being trustworthy, rather than flat-lining at the last good value.
        """
        if value is None:
            leaving = self._analog_logged.get(slot.sanitised) is not None
            await slot.tag.set(None, log=leaving)
            if leaving:
                self._analog_logged[slot.sanitised] = None
                self._analog_logged_at[slot.sanitised] = now
            return
        value = round(float(value), 3)
        await slot.tag.set(value, log=self._should_log_analog(slot, value, now))

    def _should_log_analog(self, slot: SensorSlot, value: float, now: float) -> bool:
        """Whether this reading is worth storing as a data point.

        Fires on the first reading, on a move of ANALOG_LOG_DELTA_FRACTION of the
        slot's configured engineering span, and on a heartbeat so a steady sensor
        still anchors its plot.
        """
        last = self._analog_logged.get(slot.sanitised)
        if last is None:
            log_it = True
        else:
            span = abs(float(slot.value_max) - float(slot.value_min))
            moved = abs(value - last) >= span * ANALOG_LOG_DELTA_FRACTION
            due = (
                now - self._analog_logged_at.get(slot.sanitised, 0.0)
                >= ANALOG_LOG_HEARTBEAT_S
            )
            log_it = moved or due
        if log_it:
            self._analog_logged[slot.sanitised] = value
            self._analog_logged_at[slot.sanitised] = now
        return log_it

    def _should_log_regulate(
        self, measured: float, setpoint: float, span: float, now: float
    ) -> bool:
        """Whether this regulate step is worth a data point on the plot.

        The same rule the analog readings use: a move of
        ANALOG_LOG_DELTA_FRACTION of the sensor's engineering span, plus a
        heartbeat so a settled loop still anchors its plot. Measured against the
        span rather than against the value, because the series that matters here
        is the error — it converges on zero by design, and a threshold
        proportional to the value shrinks to nothing exactly when the loop is
        behaving. That is what put one device on 30,697 messages in a day.

        One decision covers all four series, so they share x-points on the plot
        and cost a single message between them.
        """
        if self._regulate_logged is None:
            log_it = True
        else:
            last_measured, last_setpoint = self._regulate_logged
            threshold = abs(span) * ANALOG_LOG_DELTA_FRACTION
            log_it = (
                abs(measured - last_measured) >= threshold
                or abs(setpoint - last_setpoint) >= threshold
                or (now - self._regulate_logged_at) >= ANALOG_LOG_HEARTBEAT_S
            )
        if log_it:
            self._regulate_logged = (measured, setpoint)
            self._regulate_logged_at = now
        return log_it

    async def _fetch_ai(self, pins: list[int]) -> list[float | None] | None:
        """One ``fetch_ai`` transaction. ``None`` when the whole request failed.

        Logs at debug only — this runs every poll, so the operator-facing
        reporting is left to :meth:`_log_read_failures` and the fault tags.
        """
        try:
            result = await self.platform_iface.fetch_ai(*pins)
        except Exception as e:
            log.debug("Failed to read analog inputs %s: %s", pins, e)
            return None
        if result is None:
            return None
        if not isinstance(result, (list, tuple)):
            result = [result]  # a single-pin read returns a scalar
        if len(result) != len(pins):
            log.debug(
                "Analog read returned %d values for %d pins", len(result), len(pins)
            )
            return None
        return list(result)

    def _log_read_failures(self, pins: list[int], readings: list) -> None:
        """Log only when the set of unreadable pins changes.

        Polling is continuous, so an unreachable platform interface would
        otherwise fill the log with the same line every couple of seconds.
        """
        failed = {pin for pin, reading in zip(pins, readings) if reading is None}
        if failed == self._analog_failed_pins:
            return
        if failed:
            log.warning("Analog inputs unreadable: %s", sorted(failed))
        else:
            log.info("Analog inputs readable again")
        self._analog_failed_pins = failed

    # ------------------------------------------------------------------
    # Auto start/stop control
    # ------------------------------------------------------------------

    async def _evaluate_auto_control(self) -> None:
        """Drive the run latch from the Start/Stop Control modes.

        Sensor and Timer modes feed the same operator run latch the manual
        buttons use, so they flow through the existing auto-start state machine
        (beacon/horn warning, panel Auto dance). Stop demands take precedence
        over start demands. Manual mode contributes nothing here — the button
        owns the latch.
        """
        c = self.autostart
        fault_stop = self._sensor_fault_stop_reason()
        if fault_stop is not None:
            if self._operator_run_latch:
                log.warning("Stopping on sensor fault: %s", fault_stop)
                self._sensor_fault_stopped = True
            self._operator_run_latch = False
            self._suppress_run_tag = True
            return
        if self._auto_stop_demanded(c):
            if self._operator_run_latch:
                log.info("Auto-stop (%s)", self._command_value("stop_control_mode"))
            self._operator_run_latch = False
            self._suppress_run_tag = True
        elif self._auto_start_demanded(c):
            if not self._operator_run_latch:
                log.info("Auto-start (%s)", self._command_value("start_control_mode"))
            self._operator_run_latch = True

    def _auto_start_demanded(self, c) -> bool:
        mode = str(self._command_value("start_control_mode", "manual"))
        if mode == "sensor":
            return self._sensor_crossed(
                self._command_value("start_variable"),
                self._selected_setpoint("start_variable", "start_setpoint"),
                str(self.config.start_sensor_direction.value),
            )
        if mode == "timer":
            # Start once the scheduled time is reached and the engine is idle.
            # DatetimeInput value (epoch ms UTC) lives in the ui_cmds aggregate;
            # get_value falls back to the element's default (None) when unset.
            return self._timer_reached(
                self.ui_manager.get_value("start_timer_at"),
                required_state="idle",
                state=c.state,
                fired_attr="_start_timer_fired_at",
            )
        return False

    def _auto_stop_demanded(self, c) -> bool:
        mode = str(self._command_value("stop_control_mode", "manual"))
        if mode == "sensor":
            return self._sensor_crossed(
                self._command_value("stop_variable"),
                self._selected_setpoint("stop_variable", "stop_setpoint"),
                str(self.config.stop_sensor_direction.value),
            )
        if mode == "timer":
            # Stop once the scheduled time is reached and the engine is running.
            return self._timer_reached(
                self.ui_manager.get_value("stop_timer_at"),
                required_state="running",
                state=c.state,
                fired_attr="_stop_timer_fired_at",
            )
        return False

    def _timer_reached(
        self, target_ms, *, required_state: str, state: str, fired_attr: str
    ) -> bool:
        """One-shot check for a scheduled timer time (epoch ms UTC).

        True exactly once when ``state`` is ``required_state`` and wall-clock now
        has reached ``target_ms``. Records the fired value under ``fired_attr`` so
        the same scheduled time can't re-fire on a later idle/run cycle; picking a
        new time (a different value) re-arms it.
        """
        if target_ms is None or state != required_state:
            return False
        if getattr(self, fired_attr) == target_ms:
            return False  # already actioned this scheduled time
        if time.time() * 1000.0 >= float(target_ms):
            setattr(self, fired_attr, target_ms)
            return True
        return False

    def _controlling_sensors(self) -> list[SensorSlot]:
        """The sensors the app is currently *acting on*.

        The Regulate variable (it sets engine speed) and the Stop trigger
        variable (it is what shuts the engine down — a dead level sensor can mean
        pumping a tank dry). The Start trigger is deliberately excluded: a broken
        start sensor can only fail to request a run, which is already safe.
        """
        selected: list[str] = []
        mode = self._active_mode()
        if mode.is_regulate:
            selected.append(mode.variable)
        # A transition that can move us out of this mode is acting on its sensor
        # just as surely: a dead reading that should have ended warm-up leaves
        # the pump at warm-up RPM indefinitely. Same reasoning the Stop trigger
        # is included for, and the Start trigger isn't.
        selected.extend(
            t.variable for t in self.sequencer.outgoing(mode) if t.variable
        )
        if str(self._command_value("stop_control_mode", "manual")) == "sensor":
            selected.append(str(self._command_value("stop_variable", "")))

        sensors: list[SensorSlot] = []
        for name in selected:
            sensor = self._sensor_by_name.get(name)
            if sensor is not None and sensor.is_analog and sensor not in sensors:
                sensors.append(sensor)
        return sensors

    def _sensor_fault_stop_reason(self) -> str | None:
        """Why the engine should stop on a sensor fault, or None.

        Only fires with Sensor Fault Action set to Stop Engine, and only once a
        controlling sensor has been faulted for the whole grace period, so a
        one-poll glitch can't shut a pump down.

        An input that has never read successfully is ignored: at boot every input
        starts as "no reading", so acting on that would shut down a healthy
        engine whenever the app restarts ahead of the platform interface.
        """
        if str(self.config.sensor_fault_action.value or "Hold") != "Stop Engine":
            return None
        grace = float(self.config.sensor_fault_grace_seconds.value)
        now = time.monotonic()
        for sensor in self._controlling_sensors():
            reading = self._analog.get(sensor.sanitised)
            if reading is None or not reading.fault or not reading.has_reading:
                continue
            if reading.faulted_for(now) >= grace:
                return f"{sensor.label} sensor fault ({reading.fault_reason})"
        return None

    def _sensor_crossed(self, sanitised, setpoint, direction: str) -> bool:
        """True when the chosen sensor reads past the setpoint in *direction*."""
        sensor = self._sensor_by_name.get(str(sanitised or ""))
        if sensor is None or setpoint is None:
            return False
        if sensor.faulted:
            return False  # a broken loop never fires a trigger
        measured = sensor.tag.get()
        if measured is None:
            return False
        if direction.lower() == "above":
            return float(measured) >= float(setpoint)
        return float(measured) <= float(setpoint)

    # ------------------------------------------------------------------
    # Modbus polling
    # ------------------------------------------------------------------

    async def _poll_modbus(self) -> None:
        """Read the module in tiers and hand the merged image to the store.

        The fast tier (mode, alarms, engine speed, speed control) is read every
        loop and all of it must succeed for the poll to count — a module that
        answers half its pages is a module with a problem. The slow tier is read
        every ``slow_poll_period`` and the startup tier once, then rarely; both
        keep their last good values between reads and never fail a poll.
        """
        now = time.monotonic()
        if not await self._read_tier(gc.Tier.FAST):
            self.store.update(None)
            return

        if now - self._last_slow_poll >= float(self.config.slow_poll_period.value):
            # Stamped before the read so a failing block doesn't retry every
            # loop and starve the fast tier of bus time.
            self._last_slow_poll = now
            await self._read_tier(gc.Tier.SLOW)

        if self._startup_tier_due(now):
            if await self._read_tier(gc.Tier.STARTUP):
                self._startup_read_at = now
                self._note_supported_keys()
            else:
                self._startup_read_failed_at = now

        self.store.update(self._register_image)

    def _startup_tier_due(self, now: float) -> bool:
        if self._startup_read_at is None:
            return now - self._startup_read_failed_at >= STARTUP_READ_RETRY_S or (
                self._startup_read_failed_at == 0.0
            )
        return now - self._startup_read_at >= STARTUP_READ_REFRESH_S

    async def _read_tier(self, tier: gc.Tier) -> bool:
        """Read every block in a tier into the image. False if any block failed."""
        ok = True
        for block in gc.blocks(tier):
            if block.page == gc.PAGE_UNNAMED_ALARMS:
                # Sized per model: the E800 has nearly twice the E400's table.
                block = gc.Block(
                    block.page, 0, self.model.unnamed_alarm_registers + 1, block.tier, block.label
                )
            elif block.page == gc.PAGE_FLEX_SENSORS and block.start == gc.P178_UNIT_INFO_START:
                block = gc.Block(
                    block.page,
                    block.start,
                    gc.P178_UNIT_INFO_STRIDE * self.model.flex_sensor_count,
                    block.tier,
                    block.label,
                )
            values = await self._read_block(block)
            if values is None:
                ok = False
                if tier is gc.Tier.FAST:
                    break  # one failure fails the poll; don't hammer the bus
                continue
            gc.merge(self._register_image, block.address, values)
        return ok

    async def _read_block(self, block: gc.Block) -> list[int] | None:
        try:
            values = await self.modbus_iface.read_registers(
                bus_id=self.config.modbus_config.name.value,
                modbus_id=int(self.config.modbus_id.value),
                start_address=block.address,
                num_registers=block.count,
                register_type=4,
            )
        except Exception as e:
            log.warning("Failed to read DSE %s (page %d): %s", block.label, block.page, e)
            return None
        if values is None:
            return None
        if isinstance(values, int):
            values = [values]
        # Validated by length rather than type: pydoover 1.9 returns a protobuf
        # repeated-field container for multi-register reads, not a list.
        values = list(values)
        if len(values) < block.count:
            log.warning(
                "Short read from DSE %s (got %d, expected %d)",
                block.label,
                len(values),
                block.count,
            )
            return None
        return values

    def _note_supported_keys(self) -> None:
        """Log which control keys the module advertises, once per read.

        Useful when commissioning: the keys this app relies on (Auto, Stop,
        Telemetry Start / Cancel, Reset Alarms) should all show as supported.
        """
        words = [
            self._register_image.get(gc.address(gc.PAGE_CONTROL, i))
            for i in range(gc.P16_SUPPORTED_COUNT)
        ]
        if any(w is None for w in words):
            return
        self._supported_keys = [int(w) for w in words]
        supported = [
            key.name for key in gc.ControlKey if gc.key_supported(self._supported_keys, key)
        ]
        summary = ", ".join(supported) if supported else "none"
        if self.tags.supported_keys.get() != summary:
            log.info("DSE advertises control keys: %s", summary)
            for needed in (
                gc.ControlKey.SELECT_AUTO,
                gc.ControlKey.TELEMETRY_START,
                gc.ControlKey.CANCEL_TELEMETRY_START,
            ):
                if gc.key_supported(self._supported_keys, needed) is False:
                    log.warning(
                        "DSE does not advertise %s — remote control will not "
                        "work until the module is configured to accept it.",
                        needed.name,
                    )
        # Only the summary string is a tag; the raw words stay in memory.
        self._supported_keys_summary = summary

    def _key_supported(self, key: gc.ControlKey) -> bool:
        """Whether to send a key. Unknown (bitmap unread) counts as yes."""
        return gc.key_supported(self._supported_keys, key) is not False


    # ------------------------------------------------------------------
    # State + tag publishing
    # ------------------------------------------------------------------

    async def _update_state_and_tags(self) -> None:
        prev = self.controller_state.state
        new_state = self.controller_state.update(self.store)
        if new_state != prev:
            log.info("State transition: %s -> %s", prev, new_state)
        await self.tags.state.set(new_state)
        await self.tags.is_contactable.set(self.store.is_contactable)

        snap = self.store.latest
        if snap is None or not self.store.is_contactable:
            await self._publish_no_comms()
            return

        await self._publish_panel_mode(snap)

        # --- Mode / engine state ---
        await self.tags.control_mode.set(snap.control_mode_label)
        await self.tags.engine_state.set(snap.engine_state_label)
        await self.tags.is_auto.set(snap.is_auto)
        await self.tags.is_running.set(snap.is_running)
        await self.tags.is_pre_start.set(snap.is_pre_start)
        await self.tags.is_warming_up.set(snap.is_warming_up)
        await self.tags.is_cooling_down.set(snap.is_cooling_down)
        await self.tags.has_common_alarm.set(snap.has_common_alarm)
        await self.tags.panel_locked.set(snap.panel_locked)

        # --- Alarms ---
        await self._publish_alarms(snap)

        # --- Engine telemetry ---
        await self._publish_engine_telemetry(snap)

        # --- Speed control / Maintain Value ---
        await self._publish_speed_control(snap)

        # --- The module's own analogue inputs ---
        await self._publish_flex_sensors(snap)

        # --- Identity ---
        await self.tags.manufacturer_code.set(int(snap.manufacturer_code or 0))
        await self.tags.model_number.set(int(snap.model_number or 0))
        await self.tags.serial_number.set(int(snap.serial_number or 0))
        summary = getattr(self, "_supported_keys_summary", None)
        if summary is not None and self.tags.supported_keys.get() != summary:
            await self.tags.supported_keys.set(summary)

        # --- Status string for the app display name ---
        await self.tags.status_string.set(
            f"{self.app_display_name}: {self._display_state(snap)}"
        )

    async def _publish_alarms(self, snap) -> None:
        """What the module is complaining about, worst first.

        ``fault_string`` is the one the tile and the auto-start machine act on;
        ``alarm_summary`` is the whole list for the dashboard. Logged on change
        only — a shutdown that has been latched for an hour is one line, not a
        line per poll.
        """
        await self.tags.fault_string.set(snap.fault_string)
        await self.tags.is_faulted.set(snap.is_faulted)
        await self.tags.has_warning.set(snap.has_warning)
        await self.tags.alarm_count.set(int(snap.alarm_count))
        summary = snap.alarm_summary or ("None" if not snap.has_common_alarm else snap.fault_string)
        if self.tags.alarm_summary.get() != summary:
            if snap.has_common_alarm:
                log.warning("DSE alarms: %s", summary)
            else:
                log.info("DSE alarms cleared")
            await self.tags.alarm_summary.set(summary, log=True)
        else:
            await self.tags.alarm_summary.set(summary)

    async def _publish_engine_telemetry(self, snap) -> None:
        """Pages 4, 5 and 7. Anything the module doesn't report stays None."""
        await self.tags.engine_rpm.set(_round(snap.engine_rpm, 0))
        await self.tags.engine_load_pct.set(snap.engine_load_pct)
        await self.tags.coolant_temp_c.set(snap.coolant_temp_c)
        await self.tags.oil_temp_c.set(snap.oil_temp_c)
        await self.tags.oil_pressure_kpa.set(snap.oil_pressure_kpa)
        await self.tags.coolant_level_pct.set(snap.coolant_level_pct)
        await self.tags.oil_level_pct.set(snap.oil_level_pct)
        await self.tags.fuel_level_pct.set(snap.fuel_level_pct)
        await self.tags.fuel_rate_lph.set(_round(snap.fuel_rate_lph, 2))
        await self.tags.fuel_used_l.set(snap.fuel_used_l)
        await self.tags.boost_pressure_kpa.set(snap.boost_pressure_kpa)
        await self.tags.intake_manifold_temp_c.set(snap.intake_manifold_temp_c)
        await self.tags.exhaust_temp_c.set(snap.exhaust_temp_c)
        await self.tags.alternator_voltage.set(_round(snap.alternator_voltage, 1))
        await self.tags.battery_voltage.set(_round(snap.battery_voltage, 1))
        await self.tags.total_engine_hours.set(_round(snap.total_engine_hours, 2))
        await self.tags.number_of_starts.set(snap.number_of_starts)
        await self.tags.manual_run_time_remaining_s.set(
            _round(snap.manual_run_time_remaining_s, 0)
        )

    async def _publish_speed_control(self, snap) -> None:
        """Page 137: the target speed the module holds, and its Maintain Value loop."""
        m = snap.maintain
        await self.tags.target_speed_rpm.set(m.target_speed_rpm)
        await self.tags.maintain_available.set(m.available)
        await self.tags.maintain_hidden.set(not m.available)
        if not m.available:
            await self.tags.maintain_status.set("")
            return
        units = m.units
        await self.tags.maintain_setpoint.set(m.set_point)
        await self.tags.maintain_setpoint_min.set(m.set_point_min)
        await self.tags.maintain_setpoint_max.set(m.set_point_max)
        await self.tags.maintain_value.set(m.control_value)
        await self.tags.maintain_units.set(units)
        await self.tags.maintain_source.set(m.target_source_label)
        await self.tags.maintain_p_gain.set(m.p_gain)
        await self.tags.maintain_i_gain.set(m.i_gain)
        await self.tags.maintain_d_gain.set(m.d_gain)
        suffix = f" {units}" if units else ""
        if m.set_point is not None and m.control_value is not None:
            await self.tags.maintain_status.set(
                f"Holding {m.set_point:g}{suffix} (now {m.control_value:g}{suffix})"
            )
        elif m.set_point is not None:
            await self.tags.maintain_status.set(f"Set point {m.set_point:g}{suffix}")
        else:
            await self.tags.maintain_status.set("")

    async def _publish_flex_sensors(self, snap) -> None:
        """Page 178: the module's flexible analogue inputs, hidden unless configured."""
        for sensor in snap.flex_sensors:
            value_tag = getattr(self.tags, flex_value_tag(sensor.letter), None)
            hidden_tag = getattr(self.tags, flex_hidden_tag(sensor.letter), None)
            if value_tag is None or hidden_tag is None:
                continue
            # Unit info arrives with the startup tier; until then nothing is
            # known to be configured, so nothing is shown.
            await hidden_tag.set(not sensor.configured or sensor.is_digital)
            await value_tag.set(_round(sensor.value, 2) if sensor.configured else None)

    def _display_state(self, snap) -> str:
        """What the app tile says the pump is doing.

        Normally the panel's own state, but not while we're starting it: the panel
        reports stopped for the whole pre-start warning and the cranking that
        follows, so an operator who just pressed Start would watch the tile say
        "Stopped" for a minute or more. Our own sequence state is the honest answer
        there — including through the panel's fault-reset and reboot, which this app
        drives on purpose during a start and which would otherwise flicker a fault
        onto the tile.
        """
        if self.autostart is not None and self.autostart.state in (
            "prestart_warning",
            "starting",
        ):
            # No mode named here: nothing is running yet for a mode to be a
            # mode of, and the sequence hasn't reached the one it will start in
            # until the engine catches.
            return "Starting"
        # Only alongside a plain "Running". When the panel is reporting a phase
        # of its own — warming up, cooling down — it is already saying something
        # specific about the engine, and pairing that with our run mode reads as
        # nonsense: "Warming Up (Warmup)".
        panel_phase = bool(snap.is_warming_up or snap.is_cooling_down)
        if snap.is_running and not panel_phase:
            return self._with_run_mode(snap.display_state)
        return snap.display_state

    def _with_run_mode(self, state: str) -> str:
        """Name the active run mode alongside the state, where there is a choice.

        "Running" says nothing about which of a site's modes is running — at
        900 rpm warming up, or holding a discharge pressure. On an install with a
        single mode there is nothing to distinguish, so the suffix would be noise
        on every tile.

        Only while the engine is actually turning. A stopped pump's mode is a
        plan rather than a state — and the panel's own stopped text already
        carries brackets of its own — while a starting one has no mode to report
        yet: nothing is running for a mode to be a mode of.
        """
        if len(self.sequencer.modes) < 2:
            return state
        return f"{state} ({self._active_mode().label})"

    async def _publish_panel_mode(self, snap) -> None:
        """Flag an engine running in Manual, where remote RPM writes do nothing.

        The panel owns engine speed in Manual and ignores the target speed
        register, so an app holding a setpoint the panel is never going to act on
        needs something on the dashboard to explain the gap. So say it, next to
        the controls that can't act.
        """
        # Only while it's *running*: a stopped panel in Manual is no obstacle,
        # because the start sequence puts it into Auto itself.
        in_manual = (
            bool(self.config.enable_remote_control.value)
            and snap is not None
            and not snap.is_auto
            and bool(snap.is_running)
        )
        self._manual_while_running = in_manual
        await self._apply_rpm_mode_visibility()
        await self.tags.manual_mode_hidden.set(not in_manual)
        await self.tags.rpm_status.set(
            "No RPM control in Manual mode" if in_manual else ""
        )

    async def _publish_no_comms(self) -> None:
        # Don't blank everything every loop — just clear engine values that
        # could otherwise look stale. Last-known values for diagnostics are
        # more useful to the operator than zeros while comms come and go.
        await self.tags.is_running.set(False)
        await self.tags.is_auto.set(False)
        await self.tags.is_pre_start.set(False)
        await self.tags.is_warming_up.set(False)
        await self.tags.is_cooling_down.set(False)
        await self.tags.has_common_alarm.set(False)
        await self.tags.control_mode.set("")
        await self.tags.engine_state.set("")
        await self.tags.engine_rpm.set(None)
        # Without comms we can't tell what mode the panel is in — don't claim it,
        # and leave the controls where they are rather than hiding them on a blip.
        await self.tags.manual_mode_hidden.set(True)
        await self.tags.rpm_status.set("")
        await self.tags.status_string.set(
            f"{self.app_display_name}: {self._no_comms_display()}"
        )

    def _no_comms_display(self) -> str:
        """What the app tile says when the panel can't be reached.

        A silent panel is usually one that is switched off — this app released its
        power output, or nobody turned it on — so by default it says so rather
        than reporting a fault the operator can't act on. It only says otherwise
        when something suggests otherwise:

        * Mid-start it is "Starting". The panel may be unreachable on purpose
          there — booting on our own power output — and that is what the tile
          says either side of it (see _display_state).
        * While an engine may still be turning it is "no comms", because "Power
          Off" beside a running pump would be a lie. That covers a fault out of a
          *run*, but not a start that gave up: nothing ever ran, so a quiet panel
          means then what it usually means.
        * If we are holding the panel's power on ourselves and it still isn't
          answering, it should be awake and isn't — worth naming. The exception is
          the boot itself: a start commanded on a dark panel can't leave ``idle``
          until comms arrive, so that window reads "Starting" too, for as long as
          a boot plausibly takes.
        """
        c = self.autostart
        if c is not None and c.state in ("prestart_warning", "starting"):
            return "Starting"
        if self._engine_may_be_turning():
            return "no comms"
        if self._holding_panel_power():
            if self._run_request_active and self._panel_powering_up():
                return "Starting"
            return "no comms"
        message = str(self.config.power_off_message.value or "").strip()
        if message:
            return message
        # Blanked deliberately — fall back to the app's original wording.
        if self.controller_state.state == "initialising":
            return "connecting…"
        return "no comms"

    def _engine_may_be_turning(self) -> bool:
        """Whether anything might still be running on the panel we can't reach.

        The question the tile hangs on: while the answer is yes it must not call
        the panel switched off, however quiet it has gone.
        """
        c = self.autostart
        if c is None:
            return False
        return c.state in ("running", "stopping") or (
            # A fault out of a run leaves an engine that may still be turning. A
            # start that gave up leaves one that never started.
            c.state == "fault" and not c.start_failed
        )

    def _panel_powering_up(self) -> bool:
        """Whether the panel could still just be booting on our power output."""
        return (
            self._power_on_since is not None
            and time.monotonic() - self._power_on_since < PANEL_POWER_UP_S
        )

    def _holding_panel_power(self) -> bool:
        """Whether the app is currently switching the panel's power on itself."""
        return self.config.pump_power_do_pin.value is not None and bool(
            self._pump_power_on
        )


    # ------------------------------------------------------------------
    # Auto-start orchestration
    # ------------------------------------------------------------------

    async def _init_outputs(self) -> None:
        """Drive the warning pins to a known-off state at startup.

        The configured digital outputs are not here: they are driven to their
        resting state by the first loop anyway, and each has its own configured
        default rather than being assumed off.

        The pump-power output is deliberately not here either. The warning pins
        are safe to switch off on a running engine; the panel's power is not, and
        at startup we can't yet tell a stopped pump from one we're powering
        ourselves. It is left alone until the first loop that knows (see
        POWER_STARTUP_HOLD_S).
        """
        await self._set_output_pin(self.config.beacon_do_pin.value, False, "_last_beacon_out")
        await self._set_output_pin(self.config.horn_do_pin.value, False, "_last_horn_out")

    def _compute_run_request(self) -> bool:
        """Combine the operator latch with the cross-app run-request tag.

        The cross-app tag only counts when automated starts are enabled
        ("Allow Automated Starts"); the operator latch always counts. Once an
        operator stops/cancels, the tag is ignored until it drops low again so a
        latched external request can't immediately re-start.
        """
        tag_req = bool(self.tags.run_request.get())
        if not bool(self.tags.automation_enabled.get()):
            tag_req = False  # Manual Only — ignore automated run requests
        if self._suppress_run_tag:
            if not tag_req:
                self._suppress_run_tag = False
            tag_req = False
        return self._operator_run_latch or tag_req

    def _confirm_running(self, raw_running: bool, lost_comms: bool) -> bool:
        """Debounced view of the engine running state.

        A reading must hold for RUNNING_DEBOUNCE consecutive polls before it
        flips the confirmed value, so a single bad modbus decode can't fault a
        healthy engine. While comms are lost we hold the last confirmed value
        rather than guess from stale data.
        """
        if lost_comms:
            return self._running_confirmed
        if raw_running:
            self._run_streak += 1
            self._stop_streak = 0
            if self._run_streak >= RUNNING_DEBOUNCE:
                self._running_confirmed = True
        else:
            self._stop_streak += 1
            self._run_streak = 0
            if self._stop_streak >= RUNNING_DEBOUNCE:
                self._running_confirmed = False
        return self._running_confirmed

    def _panel_looks_restarted(self, snap, raw_running: bool, now: float) -> bool:
        """Whether a not-running reading is more likely a restarted panel.

        A module that has just been power-cycled is unreachable for a few seconds
        and then boots reporting stopped, out of Auto — so those three arrive
        together. An engine that trips while comms are healthy does not.
        """
        if snap is None or raw_running or snap.is_auto:
            return False
        if self._last_comms_loss is None:
            return False
        return now - self._last_comms_loss <= PANEL_RESTART_WINDOW_S

    async def _run_autostart(self) -> None:
        c = self.autostart
        snap = self.store.latest
        lost_comms = self.store.has_lost_comms()
        raw_running = bool(snap.is_running) if snap is not None else False
        is_faulted = bool(snap.is_faulted) if snap is not None else False
        is_running = self._confirm_running(raw_running, lost_comms)

        now = time.monotonic()
        # Deliberately the *raw* contactability, not `lost_comms`: that flag needs
        # 15s and 3 missed polls (see DseStateStore.has_lost_comms), and a panel
        # restart is quicker than that. Keying off the debounced flag would mean
        # this never fired for the exact case it exists for.
        if not self.store.is_contactable:
            self._last_comms_loss = now
        panel_restarting = self._panel_looks_restarted(snap, raw_running, now)

        # Reconcile reality: if we're idle but the engine is already running
        # (app restart, or someone started it at the panel), adopt it straight
        # into the running state — both so we offer a Stop instead of a Start,
        # and so we never beacon-warn or force-reset a running engine. Use the
        # raw reading here so a restart is picked up on the first live poll, and
        # seed the debounce as confirmed so we don't immediately fault.
        if c.state == "idle" and raw_running and not lost_comms:
            log.info("Auto-start: adopting already-running engine")
            self._operator_run_latch = True
            self._running_confirmed = True
            self._run_streak = RUNNING_DEBOUNCE
            self._stop_streak = 0
            is_running = True
            await c.adopt_running()

        run_request = self._compute_run_request()
        # Read back by _drive_pump_power / _no_comms_display later this loop.
        self._run_request_active = run_request

        prev_state = c.state
        await c.evaluate(
            lost_comms=lost_comms,
            is_running=is_running,
            is_faulted=is_faulted,
            run_request=run_request,
            panel_restarting=panel_restarting,
            panel_ready=self._panel_ready_to_start(snap),
        )
        if c.state != prev_state:
            log.info("Auto-start: %s -> %s", prev_state, c.state)
            await self.create_message(
                "activity_logs",
                {"message": f"{self.app_display_name}: auto-start {c.display}"},
            )
            self._announce_state_change(c)
            if c.state == "prestart_warning" and self.sequencer.start_mode:
                # A configured Start In Mode means every deliberate start begins
                # there. With none configured the pump resumes whatever mode it
                # was left in, which is also what happens when an already-running
                # engine is adopted — that path goes idle → running and never
                # touches this state, and is what makes an app restart mid-run
                # keep the mode (and the clock) it was in.
                await self._select_run_mode(
                    self.sequencer.start_mode, reason="starting"
                )
            elif c.state == "running" and prev_state == "starting":
                # The mode was chosen when the warning began — so the dashboard
                # shows what the pump is about to do — but its clock has to run
                # from when the *engine* did. Otherwise "Warm Up for 30s" is
                # satisfied by 30s of pre-start warning with the engine off, and
                # the pump leaves warm-up the instant it catches. Holds re-arm
                # for the same reason: a condition true during the warning was
                # never observed with the engine turning.
                await self.tags.run_mode_entered_at.set(time.time())
                self.sequencer.mode_changed()

        # The warning has no fixed length, so instead of a single "n seconds to
        # go" phrase it simply repeats for as long as it runs — including
        # through the panel prep, where someone arriving late has nothing else
        # to tell them a start is coming.
        if (
            c.state == "prestart_warning"
            and time.monotonic() - self._last_prestart_spoken >= PRESTART_REPEAT_S
        ):
            self._arm_announcement(
                self.config.prestart_announcement.value,
                prestart=True,
                hold_for=PRESTART_REPEAT_S,
            )

        # --- Run mode: which configured mode is driving engine speed ---
        # Only while running: modes set engine speed, and there is no speed to
        # set until there is an engine turning.
        if c.state == "running":
            await self._evaluate_run_mode()
        else:
            # Transitions only run the engine, but the readouts should track the
            # picker whenever it moves — including with the pump stopped, where
            # nothing else would republish them.
            await self._publish_run_mode()

        # --- Panel command choreography, driven by the current state ---
        await self._drive_panel_commands(c, snap)

        # --- Beacon / horn outputs (pulsed + coordinated across pumps) ---
        await self._drive_warning_outputs(c)
        # After the outputs, so this loop's horn phase is settled before we ask
        # whether there's room to speak into it.
        await self._speak_pending(c)

        await self._publish_autostart_tags(run_request, lost_comms)

    async def _drive_panel_commands(self, c, snap) -> None:
        """Panel-side commands for the current state.

        The reset → Auto handshake runs for the *whole* warning, not after it,
        so the panel is ready the moment the countdown is. On a DSE it takes
        seconds rather than the Kensho's thirty, but the shape is kept: a panel
        that is slow to clear an alarm just holds the warning open longer.

        Only the START itself waits for the countdown — it is the thing the
        warning is warning about.
        """
        if c.state in ("prestart_warning", "starting"):
            await self._prep_panel(snap)
            if c.state == "starting":
                await self._maybe_send_start(snap)
        elif c.state == "running":
            await self._write_running_rpm()
        elif c.state == "stopping":
            await self._maybe_send_stop()
        if c.state != "stopping":
            self._stop_started_at = None

    async def _drive_pump_power(self) -> None:
        """Hold the panel's power output on while the pump is wanted.

        The output is only ever *held on* — never held off. It is expected to be
        wired in parallel with the panel's own power switch, so an operator can
        power the Kensho up and run it by hand whenever they like, with this pin
        low and the app none the wiser. Nothing in the app infers "the panel must
        be off" from the pin: comms are still polled, and an engine found running
        on a manually-powered panel is adopted exactly as before (and then keeps
        the pin on, so releasing the manual switch mid-run doesn't kill it).

        No pin configured → the feature is off and nothing is written.
        """
        pin = self.config.pump_power_do_pin.value
        if pin is None:
            return
        wants = self._wants_pump_power()
        if wants is None:
            return  # startup hold — the pin's real state is unknown, leave it
        if wants != self._pump_power_on:
            log.info("Pump power: %s", "on" if wants else "off")
            if wants:
                self._power_on_since = time.monotonic()
        # The hold exists only because a digital output can't be read back. We've
        # just written it, so we know what it is — without this a run demand
        # during the hold (Start pressed right after a restart) would energise the
        # pin and then be unable to release it until the hold timed out.
        self._power_startup_hold = False
        self._pump_power_on = wants
        await self._set_output_pin(int(pin), wants, "_last_pump_power_out")
        await self.tags.pump_power_on.set(wants)

    def _wants_pump_power(self) -> bool | None:
        """Whether the panel's power output should be on. ``None`` = don't know yet.

        On whenever a run is wanted (so the panel is booting while the pre-start
        warning runs), for every state of the start/run/cooldown sequence, and
        for as long as the panel says the engine is running — whoever started it.

        A start that gave up is the exception. The operator's run demand outlives
        the fault (it is what a later Start press clears), but nothing is running
        and nothing will without another press, so holding the panel's power on
        would just flatten the battery in front of a pump that isn't going
        anywhere. A fault out of a *run* is different and keeps the power: the
        engine may still be turning.
        """
        if self.autostart is None:
            # Monitor-only: the app never runs the pump, but it does need to see
            # the panel, so there's nothing to switch the power off for.
            return True
        snap = self.store.latest
        panel_running = bool(
            snap is not None and self.store.is_contactable and snap.is_running
        )
        if self.autostart.start_failed:
            return panel_running
        if self._run_request_active or self.autostart.is_active or panel_running:
            return True
        if self._power_hold_active():
            return None
        return False

    def _power_hold_active(self) -> bool:
        """Whether we're still in the startup hold (see POWER_STARTUP_HOLD_S)."""
        if not self._power_startup_hold:
            return False
        if self.store.snapshots:
            # The panel has answered at least once, so the picture the decision
            # is made from is a real one.
            self._power_startup_hold = False
            return False
        if time.monotonic() - self._started_at < POWER_STARTUP_HOLD_S:
            return True
        log.warning(
            "Pump power: no reply from the panel %.0fs after startup — releasing "
            "the power output",
            POWER_STARTUP_HOLD_S,
        )
        self._power_startup_hold = False
        return False

    def _horn_pulse_on(self, elapsed: float) -> bool:
        """Horn pulse window: on for the first HORN_ON_S of each cycle, given the
        seconds *elapsed* since the warning episode started — so the very first
        cycle fires immediately rather than waiting for the next wall-clock slot.
        """
        return _horn_phase(elapsed) < HORN_ON_S

    def _horn_quiet_for(self, now: float) -> float:
        """Seconds before the horn next sounds. ``0`` while it is sounding.

        ``inf`` when there is nothing to dodge: no horn pin wired (the
        ``horn_active`` tag still pulses, but nothing makes a noise), or no
        warning episode running on that pin. Read from the same per-pin global
        tags :meth:`_coordinated_output` drives the horn from, so a horn shared
        between pumps is dodged on its real phase rather than on this pump's idea
        of it. ``now`` is wall clock, to match those tags.
        """
        pin = self.config.horn_do_pin.value
        if pin is None:
            return math.inf
        until = float(
            self.get_global_tag(f"kensho_horn_warning_until_p{int(pin)}", 0) or 0
        )
        if now >= until:
            return math.inf
        start = float(
            self.get_global_tag(f"kensho_horn_warning_start_p{int(pin)}", now) or now
        )
        phase = _horn_phase(now - start)
        return 0.0 if phase < HORN_ON_S else HORN_CYCLE_S - phase

    def _announce_state_change(self, c) -> None:
        """Queue whatever this transition should say.

        Queued rather than spoken, because the horn sounds its first blast on the
        same loop the warning starts; :meth:`_speak_pending` waits out the noise.
        """
        if c.state == "prestart_warning":
            self._last_prestart_spoken = time.monotonic()
            self._arm_announcement(
                self.config.prestart_announcement.value,
                prestart=True,
                # Worth holding for a gap in the horn until the next repeat is
                # due — past that, that one carries the message and this one is
                # only in its way.
                hold_for=PRESTART_REPEAT_S,
            )
        elif c.state == "starting":
            # Engine is about to crank. The horn has stopped wanting to sound,
            # but the cross-pump hold margin can leave it finishing a blast, so
            # this waits too — briefly. "Starting now" said after the engine has
            # started is worse than said over the horn.
            self._arm_announcement(
                self.config.start_announcement.value,
                prestart=False,
                hold_for=HORN_ON_S,
            )
        elif c.start_failed:
            # The start gave up. Anyone who heard the warning otherwise just
            # hears the horn stop — which is exactly what a start that *worked*
            # sounds like from a distance. This replaces whatever pre-start
            # phrase was queued, which is no longer true.
            self._arm_announcement(
                self.config.start_failed_announcement.value,
                prestart=False,
                hold_for=HORN_ON_S,
            )
        elif c.state in ("idle", "fault"):
            # Cancelled, or faulted out of something other than a start —
            # anything still queued is now untrue.
            self._pending_announcement = None

    def _arm_announcement(self, template, *, prestart: bool, hold_for: float) -> None:
        """Queue an announcement to be spoken at the next gap in the horn.

        ``hold_for`` is how long it is worth waiting for that gap; past it the
        phrase goes out over the horn rather than not at all.
        """
        if not bool(self.config.enable_announcements.value):
            return
        if not str(template or "").strip():
            return
        self._pending_announcement = _Announcement(
            str(template), prestart, time.monotonic() + max(0.0, float(hold_for))
        )

    async def _speak_pending(self, c) -> None:
        """Speak the queued announcement once the horn leaves room for it.

        The horn drowns the speaker, so a phrase spoken under a blast is lost.
        The blasts are short and the silences long (see HORN_ON_S / HORN_OFF_S),
        so waiting for one costs a few seconds at most — well inside what a
        warning of open-ended length can absorb.
        """
        pending = self._pending_announcement
        if pending is None:
            return
        if pending.prestart and c.state != "prestart_warning":
            # The warning ended while this waited its turn. Whatever it was
            # about to say is about a moment that has passed.
            self._pending_announcement = None
            return
        if self._horn_quiet_for(time.time()) < ANNOUNCEMENT_QUIET_S:
            if time.monotonic() < pending.speak_by:
                return  # hold for the gap
            log.info("No clear gap in the horn — announcing over it")
        self._pending_announcement = None
        if pending.prestart:
            self._last_prestart_spoken = time.monotonic()
        await self._speak_announcement(
            pending.template, expires_ms=int(ANNOUNCEMENT_EXPIRY_S * 1000)
        )

    def _local_episode_start(self, name: str, wants: bool, now: float) -> float:
        """Episode start for the no-pin case: latched while wanted, reset when
        the warning clears."""
        if not wants:
            self._warn_local_start.pop(name, None)
            return now
        return self._warn_local_start.setdefault(name, now)

    async def _speak_announcement(self, template: str | None, *, expires_ms: int) -> None:
        """Voice one announcement on the configured speaker app.

        Fire-and-forget: the speaker's ``speak`` RPC is sent with
        ``wait_for_response=False`` so we never wait on (or fault the start over)
        a reply. A short ``expires_after`` lets the speaker drop an announcement
        it couldn't play in time rather than after the moment has passed.
        ``{name}`` in the template is substituted.
        """
        if not bool(self.config.enable_announcements.value):
            return
        app_key = self.config.announcer_app.value
        if not app_key:
            return
        text = _drop_countdown(str(template or "")).strip()
        if not text:
            return
        text = text.replace("{name}", str(self.app_display_name))
        try:
            await self.rpc.call(
                "speak",
                {"text": text, "priority": "high"},
                app_key=app_key,
                expires_after=max(1000, int(expires_ms)),
                wait_for_response=False,
            )
            log.info("Announcement sent to %s: %s", app_key, text)
        except Exception as e:
            log.warning("Failed to send announcement (non-fatal): %s", e)

    async def _drive_warning_outputs(self, c) -> None:
        """Drive the beacon (steady) and horn (pulsed) warning outputs.

        Both are coordinated across pumps via a per-pin global deadline tag:
        while a pump wants the warning it pushes the deadline ahead each loop,
        and the actual pin state is derived from that deadline (every app
        computes the same value, keyed by the wall clock for the horn pulse), so
        multiple pumps sharing an output cooperate instead of fighting over it.
        """
        now = time.time()
        beacon_on = await self._coordinated_output(
            self.config.beacon_do_pin.value, c.beacon_active, now,
            name="beacon", cache_attr="_last_beacon_out", pulse=False,
        )
        horn_on = await self._coordinated_output(
            self.config.horn_do_pin.value, c.horn_active, now,
            name="horn", cache_attr="_last_horn_out", pulse=True,
        )
        await self.tags.beacon_active.set(beacon_on)
        await self.tags.horn_active.set(horn_on)

    async def _coordinated_output(
            self, pin, wants: bool, now: float, *, name: str, cache_attr: str, pulse: bool
    ) -> bool:
        """Resolve and drive one warning output, coordinated across pumps via a
        per-pin global "active until" deadline (plus a shared episode start so a
        pulsed output fires its first cycle immediately). Returns the on-state.
        """
        if pin is None:
            start = self._local_episode_start(name, wants, now)
            return bool(wants and (self._horn_pulse_on(now - start) if pulse else True))

        # Deliberately the same global-tag keys the Kensho app uses, so pumps on
        # one device coordinate their horns and beacons whatever panel drives them.
        until_key = f"kensho_{name}_warning_until_p{int(pin)}"
        start_key = f"kensho_{name}_warning_start_p{int(pin)}"
        active = now < float(self.get_global_tag(until_key, 0) or 0)
        if wants:
            if pulse and not active:
                # Fresh warning episode → record a shared start so the horn
                # fires its first HORN_ON_S immediately, and every pump on this
                # pin pulses in phase from the same instant.
                await self.set_global_tag(start_key, now)
            await self.set_global_tag(
                until_key, now + self.loop_target_period + WARNING_HOLD_MARGIN_S
            )
            active = True
        if not active:
            on = False
        elif pulse:
            start = float(self.get_global_tag(start_key, now) or now)
            on = self._horn_pulse_on(now - start)
        else:
            on = True
        await self._set_output_pin(pin, on, cache_attr)
        return bool(on)

    async def _prep_panel(self, snap) -> None:
        """Drive the panel into Auto (clearing any lockout first), across loops.

        A DSE holds a shutdown alarm latched until it is reset — by the fascia
        Stop/Reset button, by entering Stop mode, or by the reset-alarms key —
        and only takes a remote start in Auto. So while it reports a lockout the
        reset key is re-issued every PANEL_RESET_INTERVAL_S (the alarm's cause
        may still be present, in which case it simply re-latches and the start
        times out); once it is clear the Auto key is nudged every
        PANEL_AUTO_RETRY_S until the mode register echoes Auto. When the panel is
        already in Auto and unfaulted this is a no-op and we can crank.

        Unlike a Kensho, none of this reboots the module, so comms stay up
        throughout and the whole handshake takes seconds.
        """
        if snap is None:
            return
        if snap.is_auto and not snap.is_faulted:
            self._reset_issued = False  # re-arm for a future fault
            return
        if snap.is_running:
            # Never reset or change mode under a running engine. If it's running
            # in Manual we let `starting` see it running and adopt it.
            return
        if snap.panel_locked:
            # An external Panel Lock input blocks mode changes outright; nothing
            # we send will land, and the start will time out into a visible
            # fault. Say why once per prep episode.
            if not self._reset_issued:
                log.warning(
                    "DSE reports Panel Lock active — cannot change mode or reset "
                    "alarms remotely until it is released."
                )
                self._reset_issued = True
            return

        now = time.monotonic()
        if snap.is_faulted:
            if now - self._last_reset_cmd >= PANEL_RESET_INTERVAL_S:
                self._last_reset_cmd = now
                self._last_auto_cmd = now  # first AUTO nudge waits one retry gap
                self._reset_issued = True
                log.info("Auto-start: resetting panel alarms (%s)", snap.fault_string)
                await self.write_force_reset()
            return

        # Unfaulted but not in Auto — keep nudging AUTO until the mode latches.
        if now - self._last_auto_cmd >= PANEL_AUTO_RETRY_S:
            self._last_auto_cmd = now
            log.info("Auto-start: setting panel to Auto (currently %s)", snap.control_mode_label)
            await self.write_set_auto()

    def _panel_ready_to_start(self, snap) -> bool:
        """Whether a START written now would actually be acted on.

        Mirrors the gate in :meth:`_maybe_send_start`, and is what holds the
        pre-start warning open (see AutoStartController.awaiting_panel_ready) —
        the horn must still be sounding when the engine cranks, and the panel's
        reset → Auto handshake takes a few seconds at least.

        A panel we have *never* heard from is not ready. It was briefly treated
        as ready — nothing to prepare, so let the countdown govern and write the
        START blind — and that was backwards: it ended the warning at its
        countdown and then spent the whole start timeout writing STARTs to a bus
        with nobody on it. Nothing is gained by firing blind either, because a
        panel that turns up is read on the next poll and started properly.

        A *stale* snapshot is used as-is, which is the point — mid-handshake the
        last thing the panel said was "Stop", and that is exactly when the
        warning should keep running.
        """
        return snap is not None and bool(snap.is_auto and not snap.is_faulted)

    async def _maybe_send_start(self, snap) -> None:
        """Issue the remote START once the panel is Auto-ready (rate-limited)."""
        if snap is not None and (not snap.is_auto or snap.is_faulted):
            return  # _prep_panel is still getting the panel ready
        now = time.monotonic()
        if now - self._last_start_cmd < 5:
            return
        self._last_start_cmd = now
        await self.write_start()

    async def _maybe_send_stop(self) -> None:
        """Withdraw the start request, escalating to Stop mode if that isn't enough.

        Cancelling the telemetry start lets the module run its own return delay
        and cool-down, exactly as if a remote-start input had dropped, and leaves
        it in Auto for the next start. It only stops an engine *we* started,
        though: one running on a physical remote-start input, or started at the
        fascia in Manual, ignores it. So if the engine is still turning
        STOP_ESCALATION_S after the stop began — or the panel isn't in Auto at
        all, where the cancel means nothing — the Stop mode key is sent instead,
        which stops the engine from any mode. The next start's prep puts the
        panel back into Auto.
        """
        now = time.monotonic()
        if self._stop_started_at is None:
            self._stop_started_at = now
        if now - self._last_stop_cmd < 5:
            return
        self._last_stop_cmd = now
        snap = self.store.latest
        escalate = (now - self._stop_started_at >= STOP_ESCALATION_S) or (
            snap is not None and not snap.is_auto
        )
        if escalate:
            await self.write_stop_mode()
        else:
            await self.write_stop()

    # ------------------------------------------------------------------
    # Run modes
    #
    # Which configured mode is driving engine speed. The selection lives in the
    # `rpm_mode` ui_cmd and nowhere else — the operator's picker writes it, and
    # so does every transition, because switching by hand and switching
    # automatically are the same operation. That is also what makes a restart
    # invisible: the element persists on its own, and run_mode_entered_at carries
    # the clock with it.
    # ------------------------------------------------------------------

    def _tuned_transitions(self) -> dict:
        """Live values for the rules an operator is allowed to move.

        Anything not adjustable, or never touched, is absent — the sequencer
        then falls back to what config asked for.
        """
        tuned = {}
        for transition in self.sequencer.transitions:
            name = transition.tunable_name
            if name is None:
                continue
            value = self._command_value(name)
            if value is not None:
                tuned[transition.index] = float(value)
        return tuned

    def _active_mode(self):
        """The mode currently driving engine speed.

        Held in memory as well as in ``run_mode``, because a read straight after
        a write returns the previous value — ``UICommandsManager.set_value``
        posts to the aggregate channel and its local copy only refreshes when
        that echoes back. The element is still where the selection *lives*: it is
        what persists across a restart, and what is read to seed this at boot.
        Without the in-memory copy every consumer has to be handed the mode by
        whoever changed it, which held for the two on that path and quietly did
        not for the digital outputs, driven from the main loop.
        """
        if self._selected_mode is not None:
            return self._selected_mode
        return self.sequencer.resolve(self._command_value("run_mode", ""))

    async def _select_run_mode(self, mode, *, reason: str) -> None:
        """Make ``mode`` the active one and restart everything keyed to it.

        Everything downstream is handed ``mode`` rather than re-reading the
        element we have just written. ``UICommandsManager.set_value`` posts the
        update to the aggregate channel and does *not* touch its local copy —
        that only refreshes when the aggregate echoes back — so a read straight
        after a write still returns the previous selection. Re-reading here left
        the dashboard a change behind, and inconsistently so, depending on
        whether the echo happened to land first.
        """
        if mode.sanitised == str(self._command_value("run_mode", "")):
            return
        log.info("Run mode → %s (%s)", mode.label, reason)
        self._selected_mode = mode
        await self._set_command_value("run_mode", mode.sanitised)
        await self.tags.run_mode_entered_at.set(time.time())
        self.sequencer.mode_changed()
        # Re-seed the regulate loop on the way in: a new mode is a new setpoint
        # (or none at all), and carrying the old integrator across would spend
        # the first seconds of the new mode unwinding the last one's commitment.
        self._regulate_key = None
        await self._apply_rpm_mode_visibility(active=mode)
        await self._publish_run_mode(mode)
        await self.create_message(
            "activity_logs",
            {"message": f"{self.app_display_name}: run mode {mode.label} ({reason})"},
        )

    async def _evaluate_run_mode(self) -> None:
        """Let the configured transitions move the pump between modes."""
        mode = self._active_mode()
        entered_at = self.tags.run_mode_entered_at.get()
        fired = self.sequencer.evaluate(
            active=mode,
            entered_at=float(entered_at) if entered_at is not None else time.time(),
            now=time.time(),
            crossed=self._transition_crossed,
            flag=self._panel_flag,
            tuned=self._tuned_transitions(),
        )
        if fired is not None and fired.is_shutdown:
            await self._shutdown_from_transition(fired)
        elif fired is not None:
            await self._select_run_mode(
                self.sequencer.resolve(fired.to_mode), reason=fired.cause
            )
        await self._publish_run_mode()

    async def _shutdown_from_transition(self, transition) -> None:
        """Stop the engine because a transition asked for it.

        Goes through the same run latch the Stop button uses rather than
        commanding the panel directly, so the shutdown gets the ordinary
        cooldown, the ordinary timeout, and the ordinary path back to idle. The
        mode is left alone: the pump is stopping, not changing how it runs.

        The latch is read at the top of the next loop, so the stop begins one
        poll later — the same one-loop lag any automatic stop has.
        """
        if not self._operator_run_latch:
            return  # already stopping; nothing to ask for twice
        log.info("Run mode transition is shutting the engine down (%s)", transition.cause)
        self._operator_run_latch = False
        # A cross-app run request that is still high must not undo this on the
        # very next loop, exactly as when an operator presses Stop.
        self._suppress_run_tag = True
        await self.create_message(
            "activity_logs",
            {
                "message": (
                    f"{self.app_display_name}: shutting down "
                    f"({transition.cause})"
                )
            },
        )

    def _transition_crossed(self, variable, setpoint, direction):
        """Whether a sensor reading is past a setpoint, for the sequencer.

        ``None`` for "can't tell" — no such sensor, no reading, or a faulted
        analog input — which the sequencer treats as a reason to stay put rather
        than as a reason to move.
        """
        sensor = self._sensor_by_name.get(str(variable or ""))
        if sensor is None or setpoint is None:
            return None
        reading = self._analog.get(sensor.sanitised)
        if reading is not None and reading.fault:
            return None
        value = sensor.tag.get() if sensor.tag is not None else None
        if value is None:
            return None
        return (
            float(value) > float(setpoint)
            if str(direction).strip().lower() == "above"
            else float(value) < float(setpoint)
        )

    def _panel_flag(self, name):
        """A panel state by its config name, or None with no panel data."""
        snap = self.store.latest
        attr = PANEL_FLAG_ATTRS.get(str(name or ""))
        if snap is None or not self.store.is_contactable or attr is None:
            return None
        return bool(getattr(snap, attr, False))

    async def _publish_run_mode(self, mode=None) -> None:
        """Say which mode is running and what would move it on.

        Takes the mode where the caller already knows it — see
        :meth:`_select_run_mode` on why re-reading the selection is unsafe.
        """
        mode = mode or self._active_mode()
        await self.tags.run_mode_active.set(mode.label)
        # With one mode and nothing that could move it, this line could only ever
        # say the same thing — so there is nothing for an operator to read it
        # for. Config problems still show, since those are the one case where a
        # single-mode install has something to say.
        await self.tags.run_mode_status_hidden.set(
            not self._run_mode_problems
            and len(self.sequencer.modes) < 2
            and not self.sequencer.transitions
        )
        # Set before the problems bail-out below: a mode that can't run still
        # names the sensor it was pointed at, and a display that hangs the
        # setpoint off this shouldn't be left holding the previous mode's.
        await self.tags.run_mode_variable.set(
            mode.variable if mode.is_regulate else "maintain_value" if mode.is_panel else ""
        )
        if self._run_mode_problems:
            # A misconfigured mode has to say so where the operator is looking,
            # not only in a log line nobody reads until something has gone wrong.
            await self.tags.run_mode_status.set("; ".join(self._run_mode_problems))
            return
        # Only count out a wait while one is actually being watched. Transitions
        # are evaluated for a running engine and nothing else, so a countdown
        # ticking away on a stopped pump would be describing something that is
        # not happening.
        running = self.autostart is not None and self.autostart.state == "running"
        entered_at = self.tags.run_mode_entered_at.get() if running else None
        await self.tags.run_mode_status.set(
            self.sequencer.describe(
                mode,
                now=time.time() if running else None,
                entered_at=None if entered_at is None else float(entered_at),
                tuned=self._tuned_transitions(),
            )
        )

    async def _write_running_rpm(self) -> None:
        """Hold the engine at what the active run mode asks for.

        Three primitives: ``Fixed`` holds that mode's Target RPM slider,
        ``Regulate`` runs this app's PI loop against a Doover sensor and writes
        the resulting target RPM, and ``Maintain Value`` leaves speed to the
        module's own loop and only moves its set point. Everything the
        sequencer adds sits above this.
        """
        mode = self._active_mode()
        if mode.is_panel:
            await self._drive_maintain_setpoint(mode)
            return
        if mode.is_regulate:
            target = await self._regulate_target_rpm(mode)
        else:
            await self.tags.regulate_status.set("")
            target = self._command_value(mode.element_name("target_rpm"))

        if target is None:
            return
        target = int(target)
        if not self._rpm_write_needed(target):
            return
        if await self.write_target_rpm(target):
            self._last_rpm_written = target
            self._last_rpm_assert = time.monotonic()

    async def _drive_maintain_setpoint(self, mode) -> None:
        """One step of a Maintain Value mode: keep the module's set point where
        the operator's slider says, and show what the module's loop is doing.

        The loop itself runs on the DSE. This app writes the set point only when
        it differs from what the module echoes back, and re-asserts an unchanged
        one at most every SETPOINT_REASSERT_INTERVAL_S — the same discipline as
        the target RPM, for the same reason (a module that ignores it, because
        it's in Manual, would otherwise be written to every poll).
        """
        snap = self.store.latest
        m = snap.maintain if snap is not None else None
        wanted = self._command_value(mode.element_name("panel_setpoint"))
        if m is None or not m.available:
            await self.tags.regulate_status.set(
                "Panel reports no Maintain Value loop — enable Maintain Value "
                "Control in DSE Configuration Suite"
            )
            await self._publish_maintain_readouts(None, None, None)
            return

        units = f" {m.units}" if m.units else ""
        measured = m.control_value
        if wanted is None:
            # Nothing dialled in yet: show what the panel is holding and leave it.
            await self.tags.regulate_status.set(
                f"Panel holding {m.set_point:g}{units}" if m.set_point is not None else ""
            )
            await self._publish_maintain_readouts(measured, m.set_point, snap.engine_rpm)
            return

        raw = m.raw_set_point_for(float(wanted))
        lo, hi = m.set_point_min_raw, m.set_point_max_raw
        if lo is not None and hi is not None and lo <= hi:
            clamped = max(lo, min(hi, raw))
            if clamped != raw:
                log.info(
                    "Maintain Value set point %s%s is outside the panel's %s..%s — using %s",
                    wanted, units, m.set_point_min, m.set_point_max, gc.scaled(clamped, m.multiplier, m.precision),
                )
            raw = clamped
        raw = gc.to_s16(raw) if raw >= 0 else gc.to_s16(raw)

        if self._setpoint_write_needed(raw, m.set_point_raw):
            if await self.write_maintain_setpoint(raw):
                self._last_setpoint_written = raw
                self._last_setpoint_assert = time.monotonic()

        await self.tags.regulate_status.set(
            f"Panel holding {gc.scaled(gc.s16(raw), m.multiplier, m.precision):g}{units}"
            + (f" (now {measured:g}{units})" if measured is not None else "")
        )
        await self._publish_maintain_readouts(
            measured, gc.scaled(gc.s16(raw), m.multiplier, m.precision), snap.engine_rpm
        )

    def _setpoint_write_needed(self, raw: int, echo) -> bool:
        if raw != self._last_setpoint_written:
            return True
        if echo is None:
            return False
        echo_raw = gc.to_s16(int(echo)) if -0x8000 <= int(echo) <= 0x7FFF else echo
        if abs(gc.s16(echo_raw) - gc.s16(raw)) <= SETPOINT_ECHO_TOLERANCE:
            return False
        if time.monotonic() - self._last_setpoint_assert < SETPOINT_REASSERT_INTERVAL_S:
            return False
        log.info(
            "Panel reports Maintain Value set point %s but we commanded %s — re-asserting",
            gc.s16(echo_raw),
            gc.s16(raw),
        )
        return True

    async def _publish_maintain_readouts(self, measured, setpoint, rpm) -> None:
        """Feed the regulate plot from the panel's loop, so a Maintain Value mode
        gets the same history a Regulate one does."""
        now = time.monotonic()
        do_log = (
            measured is not None
            and setpoint is not None
            and self._should_log_regulate(float(measured), float(setpoint), 1.0, now)
        )
        await self.tags.regulate_measured.set(
            None if measured is None else round(float(measured), 3), log=do_log
        )
        await self.tags.regulate_setpoint_active.set(
            None if setpoint is None else float(setpoint), log=do_log
        )
        await self.tags.regulate_error.set(
            None if measured is None or setpoint is None else round(float(setpoint) - float(measured), 3),
            log=do_log,
        )
        await self.tags.regulate_output_rpm.set(None if rpm is None else round(rpm), log=do_log)

    async def _regulate_target_rpm(self, mode) -> int | None:
        """Run one step of the RPM regulate loop; return the RPM to command.

        The mode supplies which sensor to hold; the operator supplies the
        setpoint and sensitivity from that mode's own controls. Returns ``None``
        (hold last write) when the loop can't run — no sensor, no setpoint, no
        live reading, or a native sensor whose loop has failed (regulating on a
        dead 4-20 mA reading would be worse than holding).
        """
        sanitised = mode.variable
        setpoint = self._command_value(mode.element_name("setpoint"))
        sensitivity = self._command_value(
            mode.element_name("sensitivity"), DEFAULT_SENSITIVITY
        )
        sensor = self._sensor_by_name.get(sanitised)

        if sensor is None or setpoint is None:
            await self.tags.regulate_status.set(
                f"{mode.label} has no usable variable and setpoint — check config"
            )
            await self.tags.regulate_error.set(None)
            await self.tags.regulate_output_rpm.set(None)
            await self.tags.regulate_setpoint_active.set(None)
            return None

        if not sensor.allow_rpm_control:
            # Reported at startup by run_modes.validate too, but a mode that
            # can't drive anything has to say so where the operator is looking.
            await self.tags.regulate_status.set(
                f"{sensor.label} is not allowed to control RPM — turn on Allow "
                f"RPM Control on that sensor, or point {mode.label} elsewhere"
            )
            await self.tags.regulate_error.set(None)
            await self.tags.regulate_output_rpm.set(None)
            await self.tags.regulate_setpoint_active.set(None)
            return None

        reading = self._analog.get(sensor.sanitised)
        if reading is not None and reading.fault:
            # Hold the last commanded RPM. Whether this escalates to a stop is
            # the Sensor Fault Action's call (see _sensor_fault_stop_reason).
            await self.tags.regulate_status.set(
                f"{sensor.label} sensor fault ({reading.fault_reason}) — holding RPM"
            )
            await self.tags.regulate_error.set(None)
            return None

        measured = sensor.tag.get()
        if measured is None:
            await self.tags.regulate_status.set("Waiting for sensor reading…")
            await self.tags.regulate_error.set(None)
            return None

        now = time.monotonic()
        # Time since the last step that actually ran. The hold paths above return
        # without stamping the clock, so this also measures how long the loop has
        # been unable to act.
        gap = now - self._last_regulate_time if self._last_regulate_time else 0.0

        # Re-seed the loop (to the current engine RPM) when first entering
        # Regulate, whenever the variable / setpoint changes, and after a gap long
        # enough that the engine may have moved meanwhile — so RPM doesn't jump on
        # a setpoint edit or when a sensor comes back from a fault.
        period = float(self.loop_target_period)
        key = (sanitised, float(setpoint))
        if key != self._regulate_key or gap >= REGULATE_RESEED_PERIODS * period:
            self.regulator.reset(self.tags.engine_rpm.get())
            self._regulate_key = key
            gap = 0.0

        # Cap the timestep for shorter gaps: integrating skipped seconds in one
        # step is what jumped the commanded RPM ~260 in two polls after a comms
        # blip on Station 2.
        dt = min(gap, MAX_REGULATE_DT_PERIODS * period)
        self._last_regulate_time = now

        output = self.regulator.update(
            measured=float(measured),
            setpoint=float(setpoint),
            sensitivity=(
                float(sensitivity) if sensitivity is not None else DEFAULT_SENSITIVITY
            ),
            dt=dt,
            invert=sensor.invert,
            # The sensor's configured span floors the error normalisation, so a
            # small setpoint on a wide-range sensor doesn't make the loop wild.
            span=abs(float(sensor.sp_max) - float(sensor.sp_min)),
        )
        units = f" {sensor.units}" if sensor.units else ""
        # One logging decision for the whole set, so the four plot series share
        # x-points and a settled loop stops writing a message every poll.
        do_log = self._should_log_regulate(
            float(measured),
            float(setpoint),
            abs(float(sensor.sp_max) - float(sensor.sp_min)),
            now,
        )
        await self.tags.regulate_measured.set(round(float(measured), 3), log=do_log)
        # The loop publishes the setpoint it is *holding*, on its own tag. Writing
        # back to the slider's command value would overwrite an edit made during
        # this iteration with the value read at the top of it.
        await self.tags.regulate_setpoint_active.set(float(setpoint), log=do_log)
        await self.tags.regulate_error.set(
            round(float(setpoint) - float(measured), 3), log=do_log
        )
        await self.tags.regulate_output_rpm.set(round(output), log=do_log)
        await self.tags.regulate_status.set(
            f"Holding {setpoint:g}{units} (now {float(measured):g}{units})"
        )
        return int(round(output))

    async def _set_output_pin(self, pin, state: bool, cache_attr: str) -> None:
        """Set a digital output, skipping redundant writes. No-op if unconfigured.

        The change-detection cache is only updated **after** a successful write,
        so a failed write (e.g. the one energising the beacon) is retried on the
        next loop rather than being silently latched as done.
        """
        if pin is None:
            return
        state = bool(state)
        if getattr(self, cache_attr) == state:
            return
        try:
            await self.platform_iface.set_do(int(pin), state)
        except Exception as e:
            log.warning("Failed to set DO pin %s = %s: %s", pin, state, e)
            return
        setattr(self, cache_attr, state)

    async def _publish_autostart_tags(self, run_request: bool, lost_comms: bool) -> None:
        c = self.autostart
        state = c.state
        reason = self.tags.run_request_reason.get() or ""

        await self.tags.auto_start_state.set(c.display)
        await self._publish_autostart_fault(state, c.fault_reason)
        await self.tags.auto_start_armed.set(run_request)
        # beacon_active / horn_active are published by _drive_warning_outputs
        # (the actual pulsed/coordinated pin state).
        remaining = c.warning_remaining
        await self.tags.prestart_remaining_s.set(
            round(remaining) if remaining is not None else None
        )

        # Cancel Start only means anything during the warning countdown.
        await self.tags.cancel_btn_hidden.set(state != "prestart_warning")
        await self.tags.run_reason_hidden.set(not (run_request and bool(reason)))

        # Manual Start Now / Stop Now stay visible but grey out based on the
        # engine state: disable Start while the engine is running (or being
        # started / cooling down), disable Stop while it's stopped.
        engine_on = state in ("starting", "running", "stopping")
        await self.tags.start_now_disabled.set(engine_on)
        await self.tags.stop_now_disabled.set(not engine_on)

        # The buttons keep fixed faces. Progress through the sequence — the
        # pre-start countdown, panel prep, cranking, cooldown — is reported on
        # the in-flight command via ctx.progress() and shown by the site beside
        # the button's spinner, so it no longer has to ride on the button text.

        # Re-seed the regulate loop next time we're running, so a stop/restart
        # doesn't resume from a stale integral, and drop the readouts it left
        # behind so they can't be read as current.
        if state != "running":
            self._regulate_key = None
            await self._clear_regulate_readouts(c.display)

    async def _publish_autostart_fault(self, state: str, reason: str) -> None:
        """Latch the last auto-start fault for the dashboard's warning indicator.

        Shown while faulted, and kept afterwards so an operator arriving after the
        event still sees what happened — a fault that vanishes the moment it's
        acknowledged tells nobody anything. Cleared once the pump is started or
        running normally, which is the point at which it stops being news.

        It also stops being news simply by getting old. The machine sits in
        ``fault`` until a human acts, so without an age limit the indicator
        re-asserts itself every loop for as long as nobody does — which is how a
        warning about yesterday is still on the dashboard today, and how people
        learn to ignore warnings. Past AUTOSTART_FAULT_LATCH_S it goes quiet; the
        reason and the machine's own state are untouched, and a *different* fault
        raises it again immediately.
        """
        if state == "fault":
            reason = reason or "Auto-start fault"
            if self.tags.auto_start_fault.get() != reason:
                # A new fault, not the same one being re-reported each loop.
                await self.tags.auto_start_fault.set(reason)
                await self.tags.auto_start_fault_at.set(time.time())
            await self.tags.auto_start_fault_hidden.set(self._fault_gone_stale())
            return
        if state in ("prestart_warning", "starting", "running"):
            await self._clear_autostart_fault()
            return
        # idle / stopping: hold whatever is latched, until it ages out.
        await self.tags.auto_start_fault_hidden.set(
            not bool(self.tags.auto_start_fault.get()) or self._fault_gone_stale()
        )

    def _fault_gone_stale(self) -> bool:
        """Whether the latched fault is too old to be worth an indicator."""
        at = self.tags.auto_start_fault_at.get()
        return (
            at is not None
            and time.time() - float(at) >= AUTOSTART_FAULT_LATCH_S
        )

    async def _clear_autostart_fault(self) -> None:
        """Drop the latched fault entirely — it has stopped being true."""
        await self.tags.auto_start_fault.set("")
        await self.tags.auto_start_fault_at.set(None)
        await self.tags.auto_start_fault_hidden.set(True)

    async def _clear_regulate_readouts(self, state_display: str) -> None:
        """Blank the regulate readouts while the loop isn't running.

        Every one of these is written only from inside a regulate step, and they're
        all live tags — so whatever the last step left behind gets republished
        every poll and reads on the dashboard as a current value. That's how a
        status line from before a fault ("now 2.971 bar") ends up sitting next to a
        gauge that has since moved to 5.73 bar. Saying *why* we're not regulating
        beats both a stale reading and an empty box.
        """
        await self.tags.regulate_status.set(f"Not regulating — {state_display}")
        # Next run starts a fresh trace, so its first step logs unconditionally
        # rather than being measured against a reading from before the gap.
        self._regulate_logged = None
        for tag in (
            self.tags.regulate_error,
            self.tags.regulate_output_rpm,
            self.tags.regulate_measured,
            self.tags.regulate_setpoint_active,
        ):
            await tag.set(None)

    # ------------------------------------------------------------------
    # Modbus writes (GenComm control keys and page 137 speed control)
    # ------------------------------------------------------------------

    async def _wait_inter_write(self) -> None:
        delta = time.monotonic() - self._last_write_time
        if delta < WRITE_INTER_GAP_S:
            await asyncio.sleep(WRITE_INTER_GAP_S - delta)

    async def _write_registers(self, address: int, values: list[int]) -> bool:
        """One Modbus function-16 write, rate-limited. False on failure."""
        await self._wait_inter_write()
        try:
            await self.modbus_iface.write_registers(
                bus_id=self.config.modbus_config.name.value,
                modbus_id=int(self.config.modbus_id.value),
                start_address=int(address),
                values=[int(v) & 0xFFFF for v in values],
                register_type=4,
            )
        except Exception as e:
            log.warning("Failed to write reg %s = %s: %s", address, values, e)
            return False
        self._last_write_time = time.monotonic()
        return True

    async def write_control_key(self, key: gc.ControlKey) -> bool:
        """Send a GenComm system control key: the key and its complement, in
        one write to page 16 offsets 8 and 9.

        A key the module says it doesn't support (page 16 bitmap) is refused
        here rather than sent, since the module would only reject it — and the
        log line names it, so a module configuration gap is easy to spot.
        """
        if not self._key_supported(key):
            log.warning("Not sending %s: the DSE does not advertise support for it", key.name)
            return False
        log.info("Writing DSE control key %s (%d)", key.name, int(key))
        return await self._write_registers(
            gc.address(gc.PAGE_CONTROL, gc.P16_CONTROL_KEY), gc.control_key_pair(key)
        )

    async def write_start(self) -> bool:
        """Request a start the way a remote-start input would: only in Auto."""
        return await self.write_control_key(gc.ControlKey.TELEMETRY_START)

    async def write_stop(self) -> bool:
        """Withdraw the start request; the module cools down and stops itself."""
        return await self.write_control_key(gc.ControlKey.CANCEL_TELEMETRY_START)

    async def write_stop_mode(self) -> bool:
        """Put the module in Stop mode: stops the engine from any mode and
        clears latched alarms whose cause has gone."""
        return await self.write_control_key(gc.ControlKey.SELECT_STOP)

    async def write_force_reset(self) -> bool:
        """Clear latched alarms. The reset-alarms key where the module offers it;
        otherwise Stop mode does the same thing (and the Auto nudge follows)."""
        if self._key_supported(gc.ControlKey.RESET_ALL_ALARMS):
            return await self.write_control_key(gc.ControlKey.RESET_ALL_ALARMS)
        if self._key_supported(gc.ControlKey.RESET_ALARMS):
            return await self.write_control_key(gc.ControlKey.RESET_ALARMS)
        return await self.write_stop_mode()

    async def write_set_auto(self) -> bool:
        return await self.write_control_key(gc.ControlKey.SELECT_AUTO)

    async def write_target_rpm(self, rpm: int | float) -> bool:
        """Set the module's target speed (page 137 offset 147)."""
        rpm_int = int(rpm)
        if rpm_int < 0 or rpm_int > gc.TARGET_SPEED_MAX:
            log.warning("Refusing to write out-of-range target RPM: %s", rpm_int)
            return False
        log.info("Writing DSE target speed = %s rpm", rpm_int)
        return await self._write_registers(
            gc.address(gc.PAGE_SPEED_CONTROL, gc.P137_TARGET_SPEED), [rpm_int]
        )

    async def write_maintain_setpoint(self, raw: int) -> bool:
        """Set the Maintain Value loop's set point (page 137 offset 154), as
        the raw signed word the module's unit info scales it from."""
        log.info("Writing DSE Maintain Value set point (raw) = %s", gc.s16(raw))
        return await self._write_registers(
            gc.address(gc.PAGE_SPEED_CONTROL, gc.P137_SET_POINT), [int(raw) & 0xFFFF]
        )

    # ------------------------------------------------------------------
    # UI handlers
    # ------------------------------------------------------------------

    # --- In-flight command status reporting ---------------------------------

    async def _acknowledge(self, ctx) -> None:
        """Tell the site we've picked the command up, before doing any work.

        Without this the command sits on ``sent`` for the whole sequence — which
        for a start is minutes — and the site's ``command_timeout`` clock treats
        that silence as a dead device. Never fatal: a failed acknowledgement
        must not stop the engine starting.
        """
        try:
            await ctx.acknowledge()
        except Exception as e:
            log.warning("Failed to acknowledge command (non-fatal): %s", e)

    async def _command_progress(self, ctx, text: str) -> None:
        """Report a phase of the sequence on the in-flight command.

        The site shows this beside the control's spinner, and treats it as
        proof the device is alive — so the buttons' ``command_pending_timeout``
        must be longer than the gap between reports. Logged and swallowed on
        failure: losing a progress update must not abort a start.
        """
        try:
            await ctx.progress(text)
        except Exception as e:
            log.warning("Failed to publish command progress %r: %s", text, e)

    def _start_progress_text(self) -> str:
        """Human-readable progress for an in-flight Start Now command."""
        c = self.autostart
        snap = self.store.latest
        if c.state == "prestart_warning":
            remaining = c.warning_remaining
            if remaining is None:
                # Countdown spent; the warning is being held open until the panel
                # can take a start, and how long that takes is the panel's call.
                return "Horn sounding — preparing panel…"
            # Round up to the next step so the countdown reads 30 → 25 → … → 5.
            step = PROGRESS_COUNTDOWN_STEP_S
            secs = ((int(remaining) + step - 1) // step) * step
            return f"Horn sounding — at least {secs}s to crank"
        if c.state == "starting":
            if snap is None:
                return "Waiting for the panel…"
            if snap.panel_locked and (snap.is_faulted or not snap.is_auto):
                return "Panel is locked — release Panel Lock to start"
            if snap.is_faulted:
                return f"Resetting panel alarm ({snap.fault_string})…"
            if not snap.is_auto:
                return "Putting panel in Auto…"
            return "Cranking…"
        if c.state in ("running", "fault"):
            return c.display  # "Running", "Fault: …"
        # Still idle: the handler has only just set the run latch and the state
        # machine advances on the next main loop. (A cancel resolves the wait's
        # predicate instead, so it never lands here.)
        return "Preparing to start…"

    def _stop_progress_text(self) -> str:
        """Human-readable progress for an in-flight Stop Now command."""
        c = self.autostart
        snap = self.store.latest
        if c.state == "idle":
            return "Stopped"
        if c.state == "fault":
            return c.display
        # "stopping", plus the brief window where the machine hasn't left
        # running/starting yet — the operator has asked to stop either way.
        if snap is not None and snap.is_cooling_down:
            return "Cooling down…"
        return "Stopping…"

    async def _await_until(
        self,
        predicate,
        *,
        timeout: float,
        poll: float = 0.25,
        ctx=None,
        progress=None,
        on_cancel=None,
    ) -> bool:
        """Block until ``predicate()`` is true or ``timeout`` elapses.

        UI handlers return only once this resolves, so the site keeps the
        control's spinner up until the command has actually taken effect. Each
        ui_cmd is dispatched as its own task, so blocking here doesn't hold up
        the main loop or other handlers (e.g. Cancel while Start is waiting).

        When ``ctx`` and ``progress`` are given, the command message is updated
        with ``progress()`` each time that text changes — and at least every
        ``PROGRESS_HEARTBEAT_S`` regardless — so the operator watches the
        sequence advance rather than staring at a bare spinner, and the site can
        tell a working device from a dead one.
        """
        deadline = time.monotonic() + timeout
        last_text: str | None = None
        last_sent: float = 0.0
        cancelled = False
        while True:
            # Checked before the predicate so the abort has taken effect by the
            # time we test it, and once only — `on_cancel` decides what
            # cancelling this particular command means, and must not be re-run
            # every poll while the sequence unwinds.
            if not cancelled and ctx is not None and getattr(ctx, "cancelled", False):
                cancelled = True
                if on_cancel is not None:
                    await on_cancel(ctx)
            if predicate():
                return True
            # Read the flag live rather than the loop-local one: it can flip
            # while this iteration is mid-flight, and a `pending` progress
            # landing after the cancellation would put the command back to
            # in-progress in the UI.
            if (
                ctx is not None
                and progress is not None
                and not getattr(ctx, "cancelled", False)
            ):
                now = time.monotonic()
                text = progress()
                if text != last_text or now - last_sent >= PROGRESS_HEARTBEAT_S:
                    last_text, last_sent = text, now
                    await self._command_progress(ctx, text)
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(poll)

    @ui.handler("start_now")
    async def on_start_now(self, ctx, value):
        if self.autostart is None:
            return
        log.info("Operator pressed Start Now — arming auto-start")
        await self._acknowledge(ctx)
        if self.autostart.state == "fault":
            # Pressing Start *is* the acknowledgement, and the only operator path
            # out of a fault — there's no Reset Fault button. Without this the press
            # only sets the run latch and nothing happens, because `fault` has no
            # run-request transition (an operator once pressed Start four times
            # to no effect before this existed).
            log.info(
                "Clearing auto-start fault on Start: %s", self.autostart.fault_reason
            )
            await self.autostart.reset()
        self._operator_run_latch = True
        self._suppress_run_tag = False
        # Spin until the engine is actually running (or the start aborts to a
        # fault, or another action — Cancel/Stop — clears the run latch),
        # reporting each phase of the sequence back on the command. The cap
        # covers the pre-start warning plus the panel's start timeout.
        # One start_timeout budget covers the whole attempt, countdown included
        # (see AutoStartController.attempt_elapsed).
        timeout = self.autostart.start_timeout + 10
        await self._await_until(
            lambda: self.autostart.state in ("running", "fault")
                    or not self._operator_run_latch,
            timeout=timeout,
            ctx=ctx,
            progress=self._start_progress_text,
            on_cancel=self._on_start_command_cancelled,
        )

    @staticmethod
    def _who(ctx) -> str:
        """Name whoever issued or cancelled a command, for the activity log."""
        actor = getattr(ctx, "cancelled_by", None) or getattr(ctx, "actor", None)
        if isinstance(actor, dict):
            for key in ("name", "email", "id"):
                if actor.get(key):
                    return str(actor[key])
        return "an operator"

    async def _abort_start(self, *, reason: str) -> None:
        """Drop the run latch, ending the start wherever it has got to.

        Shared by the Cancel Start button and by an operator cancelling the
        Start Now command itself, so the two can't drift apart in what they do —
        they differ only in *when* they are allowed to do it, which is each
        caller's business (see :meth:`_on_start_command_cancelled`).
        """
        if self.autostart is None:
            return
        log.info("Cancelling the start (%s)", reason)
        self._operator_run_latch = False
        self._suppress_run_tag = True
        await self.create_message(
            "activity_logs",
            {"message": f"{self.app_display_name}: start cancelled ({reason})"},
        )

    async def _on_start_command_cancelled(self, ctx) -> None:
        """The operator cancelled the in-flight Start Now command itself.

        The site's cancel is a patch to the command's own message rather than a
        new command (``handleCancelCommand``), so this arrives through the
        context rather than through a handler of its own.

        Unlike the Cancel Start button, this one only aborts during the pre-start
        warning. The button is *shown* only then, so pressing it is an explicit
        "abort this start" and is honoured whenever it lands — but the cancel on
        a pending command is offered for the whole attempt, including while the
        engine is cranking or running. Dismissing a spinner is not a request to
        shut down a turning engine; Stop Now is, and it asks for confirmation
        first.
        """
        state = self.autostart.state if self.autostart is not None else None
        if state != "prestart_warning":
            log.info(
                "Start Now command cancelled in state %r — too late to abort, "
                "the engine is already committed. Use Stop Now to stop it.",
                state,
            )
            return
        await self._abort_start(reason=f"Start Now cancelled by {self._who(ctx)}")

    @ui.handler("cancel_start")
    async def on_cancel_start(self, ctx, value):
        if self.autostart is None:
            return
        log.info("Operator pressed Cancel Start")
        await self._abort_start(reason=f"Cancel Start pressed by {self._who(ctx)}")

    @ui.handler("stop_now")
    async def on_stop_now(self, ctx, value):
        if self.autostart is None:
            return
        log.info("Operator pressed Stop Now")
        await self._acknowledge(ctx)
        self._operator_run_latch = False
        self._suppress_run_tag = True
        if self.autostart.state == "fault":
            # The machine sends nothing from `fault`, so clearing the latch alone
            # would leave a running engine running. Command the panel directly. The
            # fault stays latched — stopping doesn't explain what went wrong.
            log.info("Stopping the panel directly from a faulted auto-start")
            await self.write_stop()
        # Spin until the engine is actually stopped (back to idle) or the stop
        # aborts to a fault. Capped by the panel's stop timeout plus margin.
        await self._await_until(
            lambda: self.autostart.state in ("idle", "fault"),
            timeout=self.autostart.stop_timeout + 10,
            ctx=ctx,
            progress=self._stop_progress_text,
        )

    @ui.handler(re.compile(r"^mode_.+_target_rpm$"), parser=int, auto_update=False)
    async def on_mode_target_rpm(self, ctx, value: int):
        """A Fixed mode's Target RPM slider. One per mode; each owns its own."""
        if self.autostart is None or value is None:
            return
        element = str(ctx.element.name)
        rpm_min = int(self.config.target_rpm_min.value)
        rpm_max = int(self.config.target_rpm_max.value)
        rpm = max(rpm_min, min(rpm_max, value))
        if rpm != value:
            log.info(
                "Clamped target RPM %s -> %s (config bounds %s..%s)",
                value, rpm, rpm_min, rpm_max,
            )
        await self._set_command_value(element, rpm)
        # Apply immediately only if this is the mode actually running. Every
        # other mode's slider is a setting for later, held until it is entered.
        active = self._active_mode()
        if (
            self.autostart.state == "running"
            and not active.is_regulate
            and element == active.element_name("target_rpm")
        ):
            # Waiting on the panel echo below outruns the command timeout.
            await self._acknowledge(ctx)
            if await self.write_target_rpm(rpm):
                self._last_rpm_written = rpm
                # Spin until the panel echoes the new setpoint back on 40048.
                await self._await_until(
                    lambda: self._rpm_setpoint_confirmed(rpm),
                    timeout=10,
                    ctx=ctx,
                    progress=lambda: f"Holding {rpm} RPM…",
                )

    def _rpm_write_needed(self, target: int) -> bool:
        """Whether the panel needs the target RPM written (or re-written).

        Our own last-written value isn't enough to go on. A panel that restarts
        may come back with a different target speed, and a cache-only check
        means the app believes the setpoint is already there and never asserts
        it again.

        So the panel's echo decides. Re-asserts are rate-limited, since a panel in
        Manual ignores the setpoint and would otherwise be written to every poll.
        """
        if target != self._last_rpm_written:
            return True
        snap = self.store.latest
        echo = snap.remote_rpm_setpoint if snap is not None else None
        if echo is None:
            return False  # panel doesn't report it — trust the cache rather than spam
        if abs(int(echo) - target) <= RPM_ECHO_TOLERANCE:
            return False
        if time.monotonic() - self._last_rpm_assert < RPM_REASSERT_INTERVAL_S:
            return False
        log.info(
            "Panel reports target RPM %s but we commanded %s — re-asserting",
            echo,
            target,
        )
        return True

    def _rpm_setpoint_confirmed(self, rpm: int) -> bool:
        snap = self.store.latest
        return (
                snap is not None
                and snap.remote_rpm_setpoint is not None
                and abs(int(snap.remote_rpm_setpoint) - int(rpm)) <= 1
        )

    @ui.handler("run_mode", auto_update=False)
    async def on_run_mode(self, ctx, value):
        """The operator picking a run mode.

        The same operation a transition performs, so it goes through the same
        path. A rule whose condition is currently true will move the pump straight
        back out again — that is the design, not a bug: a mode meant to be held is
        a mode with no transitions out of it.
        """
        await self._select_run_mode(
            self.sequencer.resolve(sanitise(str(value or ""))), reason="operator"
        )

    @ui.handler(re.compile(r"^mode_.+_setpoint$"), parser=float, auto_update=False)
    async def on_mode_setpoint(self, ctx, value: float):
        """A Regulate mode's setpoint, clamped to its own sensor's bounds."""
        if value is None:
            return
        element = str(ctx.element.name)
        for mode in self.sequencer.modes:
            if mode.element_name("setpoint") != element:
                continue
            sensor = self._sensor_by_name.get(mode.variable)
            if sensor is not None:
                value = max(sensor.sp_min, min(sensor.sp_max, float(value)))
            break
        await self._set_command_value(element, float(value))

    @ui.handler(re.compile(r"^mode_.+_panel_setpoint$"), parser=float, auto_update=False)
    async def on_mode_panel_setpoint(self, ctx, value: float):
        """A Maintain Value mode's set point slider.

        Clamped to the slider's own bounds here; the panel's Set Point Min / Max
        are applied when it is written (see _drive_maintain_setpoint), since only
        the module knows them.
        """
        if value is None:
            return
        element = str(ctx.element.name)
        for mode in self.sequencer.modes:
            if mode.element_name("panel_setpoint") != element:
                continue
            value = max(mode.panel_setpoint_min, min(mode.panel_setpoint_max, float(value)))
            break
        await self._set_command_value(element, float(value))
        # Written on the next loop if this is the running mode; a change to any
        # other mode's slider waits until that mode is entered.
        self._last_setpoint_written = None

    @ui.handler(re.compile(r"^transition_.+_(after|setpoint)$"), parser=float,
                auto_update=False)
    async def on_transition_threshold(self, ctx, value: float):
        """A transition's adjustable number, clamped the way its slider is."""
        if value is None:
            return
        element = str(ctx.element.name)
        for transition in self.sequencer.transitions:
            if transition.tunable_name != element:
                continue
            if transition.when == WHEN_SENSOR:
                sensor = self._sensor_by_name.get(transition.variable)
                if sensor is not None:
                    value = max(sensor.sp_min, min(sensor.sp_max, float(value)))
            else:
                value = max(0.0, float(value))
            break
        await self._set_command_value(element, float(value))

    @ui.handler(re.compile(r"^mode_.+_sensitivity$"), parser=float, auto_update=False)
    async def on_mode_sensitivity(self, ctx, value: float):
        if value is None:
            return
        lo = float(self.config.regulate_min_sensitivity.value)
        hi = float(self.config.regulate_max_sensitivity.value)
        await self._set_command_value(
            str(ctx.element.name), max(lo, min(hi, float(value)))
        )

    async def _apply_rpm_mode_visibility(self, active=None) -> None:
        """Show only the active RPM sub-panel — and neither of them in Manual.

        While the engine runs in Manual the panel owns engine speed and ignores
        the target speed register, so none of these controls can do anything; the RPM Control
        Status line stays behind to say why. Greying them out would be better than hiding them, but pydoover
        only supports ``disabled`` on Buttons today (a change for that is drafted
        upstream).
        """
        in_manual = self._manual_while_running
        active = active or self._active_mode()
        # Hidden in Manual, where the panel owns engine speed and none of these
        # controls can do anything — and on an install with a single mode, where
        # a picker offering one option is a control with nothing to control.
        await self.tags.run_mode_hidden.set(
            in_manual or len(self.sequencer.modes) < 2
        )
        for mode in self.sequencer.modes:
            flag = getattr(self.tags, mode.element_name("hidden"), None)
            if flag is not None:
                await flag.set(in_manual or mode is not active)
        # The plot is the regulate loop's own history, so it has nothing to show
        # from a Fixed mode — or from Manual, where the panel owns engine speed
        # and the loop isn't running at all.
        await self.tags.regulate_plot_hidden.set(
            in_manual or not (active.is_regulate or active.is_panel)
        )

    # --- Sensor variable / setpoint pickers (shared by Regulate, Start, Stop) ---

    async def _select_sensor_variable(
            self, sanitised: str, *, select_name: str, hidden_prefix: str,
            setpoint_prefix: str,
    ) -> None:
        """Set the chosen variable and show only that sensor's setpoint slider.

        A sensor that has no setpoint of its own yet inherits the outgoing one,
        clamped into its bounds — the same hand-over the shared setpoint used to
        give, so switching variable never leaves the trigger or the regulate loop
        without a target. A sensor already carrying a setpoint keeps it.
        """
        previous = str(self._command_value(select_name, ""))
        await self._set_command_value(select_name, sanitised)
        selected = self._sensor_by_name.get(sanitised)
        for sensor in self._sensors:
            flag = sensor.hidden_tags.get(hidden_prefix)
            if flag is not None:
                await flag.set(sensor is not selected)
        if selected is None:
            return
        own = self._command_value(f"{setpoint_prefix}_{sanitised}")
        carried = self._command_value(f"{setpoint_prefix}_{previous}") if previous else None
        setpoint = own if own is not None else carried
        if setpoint is not None:
            await self._set_command_value(
                f"{setpoint_prefix}_{sanitised}",
                max(selected.sp_min, min(selected.sp_max, float(setpoint))),
            )

    async def _set_clamped_setpoint(self, ctx, value, prefix) -> None:
        """Clamp a per-sensor setpoint slider value to that sensor's bounds.

        The element name is ``<prefix>_<sensor>``, where the sensor part is the
        sanitised label the Variable picker also stores.
        """
        if value is None:
            return
        name = str(ctx.element.name).removeprefix(f"{prefix}_")
        sensor = self._sensor_by_name.get(name)
        if sensor is not None:
            value = max(sensor.sp_min, min(sensor.sp_max, float(value)))
        await self._set_command_value(str(ctx.element.name), float(value))

    # ------------------------------------------------------------------
    # Auto start/stop control (Start Control / Stop Control modes)
    # ------------------------------------------------------------------

    @ui.handler("start_control_mode", auto_update=False)
    async def on_start_control_mode(self, ctx, value):
        mode = _control_mode(value)
        await self._set_command_value("start_control_mode", mode)
        await self._apply_control_mode_visibility()
        log.info("Start control mode set to %s", mode)

    @ui.handler("stop_control_mode", auto_update=False)
    async def on_stop_control_mode(self, ctx, value):
        mode = _control_mode(value)
        await self._set_command_value("stop_control_mode", mode)
        await self._apply_control_mode_visibility()
        log.info("Stop control mode set to %s", mode)

    @ui.handler("start_variable", auto_update=False)
    async def on_start_variable(self, ctx, value):
        await self._select_sensor_variable(
            str(value or ""),
            select_name="start_variable",
            hidden_prefix="start_var_hidden",
            setpoint_prefix="start_setpoint",
        )

    @ui.handler("stop_variable", auto_update=False)
    async def on_stop_variable(self, ctx, value):
        await self._select_sensor_variable(
            str(value or ""),
            select_name="stop_variable",
            hidden_prefix="stop_var_hidden",
            setpoint_prefix="stop_setpoint",
        )

    @ui.handler(re.compile(r"^start_setpoint_.+$"), parser=float, auto_update=False)
    async def on_start_setpoint(self, ctx, value: float):
        await self._set_clamped_setpoint(ctx, value, "start_setpoint")

    @ui.handler(re.compile(r"^stop_setpoint_.+$"), parser=float, auto_update=False)
    async def on_stop_setpoint(self, ctx, value: float):
        await self._set_clamped_setpoint(ctx, value, "stop_setpoint")

    async def _apply_control_mode_visibility(self) -> None:
        """Show only the active sub-section (Sensor/Timer) of each tab."""
        start = str(self._command_value("start_control_mode", "manual"))
        await self.tags.start_sensor_hidden.set(start != "sensor")
        await self.tags.start_timer_hidden.set(start != "timer")
        stop = str(self._command_value("stop_control_mode", "manual"))
        await self.tags.stop_sensor_hidden.set(stop != "sensor")
        await self.tags.stop_timer_hidden.set(stop != "timer")
