import logging
import math
from datetime import timedelta
from pathlib import Path

from pydoover import ui

from .app_tags import (
    DseEngineControllerTags,
    flex_hidden_tag,
    flex_sensor_letters,
    flex_value_tag,
)
from .outputs import build_outputs
from .run_modes import WHEN_SENSOR, WHEN_TIMER, build_modes, build_transitions
from .sensors import build_sensors, cfg_value

log = logging.getLogger(__name__)

CMD_KW = {
    # A healthy device answers within seconds however long the work then takes,
    # so this stays short. Handlers that then keep working acknowledge straight
    # away and report progress as they go (see _await_until); the pending
    # timeout is the budget for that work, and must exceed the app's progress
    # heartbeat (PROGRESS_HEARTBEAT_S) by a comfortable margin.
    "command_timeout": timedelta(seconds=5),
    "command_pending_timeout": timedelta(minutes=2),
    "command_retry_timeout": timedelta(minutes=5),
}

# Start / Stop AUTO-control mode. "Manual" = no automatic trigger (the always-on
# Start Now / Stop Now buttons still work); "Sensor" = trigger when a remote
# sensor crosses a setpoint; "Timer" = trigger at a scheduled date/time. Option
# names sanitise to "manual" / "sensor" / "timer".
CONTROL_OPTIONS = [
    ui.Option("Manual"),
    ui.Option("Sensor"),
    ui.Option("Timer"),
]


# Engine RPM gauge. Full scale is the configured Target RPM Maximum plus a small
# margin, so the dial covers the speeds this install actually uses instead of
# wasting half its sweep on RPM the engine can't reach. The margin is the yellow
# band: anything in it is above the fastest speed the app will ever command. The
# Idle/Run boundary is the engine's physical idle rather than a control setting, so
# it stays put unless a very low maximum forces it down.
IDLE_RPM_TO = 800
HIGH_RPM_MARGIN = 100


def _gauge_ranges(sensor) -> list["ui.Range"]:
    """Coloured bands for a sensor's gauge.

    Operator-configured bands win. With none configured, fall back to a single
    plain band spanning the sensor (see :func:`_gauge_range`), which is what every
    sensor looked like before ranges were configurable.
    """
    if sensor.ranges:
        return [
            ui.Range(
                label=band.label or None,
                min_val=band.min_val,
                max_val=band.max_val,
                colour=band.colour,
            )
            for band in sensor.ranges
        ]
    gauge_min, gauge_max = _gauge_range(sensor)
    return [ui.Range(min_val=gauge_min, max_val=gauge_max, colour=ui.Colour.green)]


def _gauge_range(sensor) -> tuple[float, float]:
    """Span for a sensor's gauge.

    A native analog input knows what its sensor actually reads, so the gauge spans
    that (an operator who scales 0-1600 kPa and leaves the setpoint bounds alone
    would otherwise see a gauge pegged at 100). A remote tag only has its setpoint
    bounds to go on — and changing that would move every existing install's gauge.
    Ordered low-to-high so an inverse-scaled sensor still renders.
    """
    if sensor.is_analog:
        lo, hi = float(sensor.value_min), float(sensor.value_max)
    else:
        lo, hi = float(sensor.sp_min), float(sensor.sp_max)
    return min(lo, hi), max(lo, hi)


def _rpm_slider_label(mode) -> str:
    """What a Fixed mode's speed control is called.

    Named for its mode, so the operator can see which one they are editing — but
    without stuttering when the mode is already named for its RPM, as the
    always-available "Fixed RPM" one is.
    """
    if mode.label.strip().lower().endswith("rpm"):
        return mode.label
    return f"{mode.label} RPM"


def _slider_step(lo: float, hi: float) -> float:
    """Pick a tidy slider step (~100 increments, snapped to 1/2/5 ×10ⁿ)."""
    span = abs(float(hi) - float(lo))
    if span <= 0:
        return 1.0
    raw = span / 100.0
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if m * mag >= raw:
            return float(m * mag)
    return float(mag)


class DseEngineControllerUI(ui.UI, display_name=DseEngineControllerTags.status_string):
    # --- Top-level summary ---
    # Bands are rebuilt from config in setup() — the green/yellow boundary follows
    # the configured Target RPM Maximum. Declared here so the element exists (and
    # keeps its position) if setup() can't read config for any reason.
    rpm_gauge = ui.NumericVariable(
        "Engine RPM",
        value=DseEngineControllerTags.engine_rpm,
        name="rpm",
        units="rpm",
        precision=0,
        form=ui.Widget.radial,
        ranges=[
            ui.Range(label="Idle", min_val=0, max_val=IDLE_RPM_TO, colour=ui.Colour.blue),
            ui.Range(label="Run", min_val=IDLE_RPM_TO, max_val=2000, colour=ui.Colour.green),
            ui.Range(label="High", min_val=2000, max_val=2100, colour=ui.Colour.yellow),
        ],
    )
    engine_load = ui.NumericVariable(
        "Engine Load",
        units="%",
        value=DseEngineControllerTags.engine_load_pct,
        name="engine_load",
        precision=0,
        ranges=[
            ui.Range(min_val=0, max_val=15, colour=ui.Colour.blue),
            ui.Range(min_val=15, max_val=100, colour=ui.Colour.green),
        ],
    )
    coolant_temp = ui.NumericVariable(
        "Coolant Temp",
        units="°C",
        value=DseEngineControllerTags.coolant_temp_c,
        name="coolant_temp",
        precision=0,
        ranges=[
            ui.Range(min_val=0, max_val=50, colour=ui.Colour.blue),
            ui.Range(min_val=50, max_val=90, colour=ui.Colour.green),
            ui.Range(min_val=90, max_val=120, colour=ui.Colour.yellow),
        ],
    )
    fuel_rate = ui.NumericVariable(
        "Fuel Rate",
        units="L/hr",
        value=DseEngineControllerTags.fuel_rate_lph,
        name="fuel_rate",
        precision=1,
    )
    sys_volts = ui.NumericVariable(
        "System Voltage",
        units="V",
        value=DseEngineControllerTags.battery_voltage,
        name="sys_volts",
        precision=1,
    )
    engine_hours = ui.NumericVariable(
        "Engine Hours",
        value=DseEngineControllerTags.total_engine_hours,
        name="engine_hours",
        precision=1,
        units="hrs"
    )
    # --- Pump Control (tabbed; hidden when remote control disabled) ---
    # Three tabs: Start, RPM, Stop. Manual Start Now / Stop Now grey out unless
    # the action is available. Every start still runs the beacon/horn pre-start
    # warning and the panel Auto-mode + reset dance — there's no way to skip it.
    # The RPM Control picker, the per-mode sub-panels and the remote-sensor
    # displays are all built dynamically from config in setup() (see below).
    control = ui.TabContainer(
        "Pump Control",
        name="control",
        position=95,
        children=[
            # ----- Start tab -----
            ui.Container(
                "Start",
                name="start_tab",
                children=[
                    # Manual Start Now — always at the top, available in every
                    # control mode (greyed only when the engine isn't stopped).
                    ui.Button(
                        "Start Now",
                        name="start_now",
                        disabled=DseEngineControllerTags.start_now_disabled,
                        colour=ui.Colour.green,
                        position=1,
                        **CMD_KW,
                        requires_confirm=ui.ConfirmDialog(
                            title="Start the engine?",
                            warning_reason=(
                                "The beacon and horn will sound a pre-start "
                                "warning, then the engine will crank "
                                "automatically. Make sure everyone is clear of "
                                "the engine and pump."
                            ),
                            help_text=(
                                "Begins the auto-start sequence: pre-start "
                                "warning (beacon + horn) → the app resets any "
                                "latched alarm and puts the panel into Auto → "
                                "remote START. There is no way to start without "
                                "the warning."
                            ),
                        ),
                    ),
                    ui.Select(
                        "Start Control",
                        name="start_control_mode",
                        options=CONTROL_OPTIONS,
                        position=2,
                        **CMD_KW,
                    ),
                    # Sensor: start when a remote sensor crosses the setpoint.
                    # The Variable picker + per-sensor setpoint sliders are
                    # injected in setup() (same shape as RPM Regulate).
                    ui.Container(
                        "Start On Sensor",
                        name="start_sensor",
                        hidden=DseEngineControllerTags.start_sensor_hidden,
                        position=3,
                        children=[],
                    ),
                    # Timer: start at a scheduled date/time.
                    ui.DatetimeInput(
                        "Start At",
                        name="start_timer_at",
                        default=None,
                        pickers=["date", "time"],
                        direction="future",
                        hidden=DseEngineControllerTags.start_timer_hidden,
                        position=4,
                        **CMD_KW,
                    ),
                    ui.Button(
                        "Cancel Start",
                        name="cancel_start",
                        hidden=DseEngineControllerTags.cancel_btn_hidden,
                        position=6,
                        **CMD_KW,
                        requires_confirm=ui.ConfirmDialog(
                            title="Cancel the pre-start warning?",
                            help_text=(
                                "Aborts the warning before the engine is "
                                "commanded to start. The beacon and horn switch "
                                "off and nothing is sent to the panel."
                            ),
                        ),
                    ),
                ],
            ),
            # ----- RPM tab (one sub-panel per configured run mode) -----
            ui.Container(
                "RPM",
                name="rpm_tab",
                children=[
                    # The RPM Control picker and one container per configured run
                    # mode are injected in setup(), since both come from config.
                    # Only the active mode's container is shown.
                    #
                    # Why the RPM controls won't do anything, when they won't.
                    # (pydoover only supports `disabled` on Buttons, so the
                    # controls stay live and this explains itself instead.)
                    ui.TextVariable(
                        "RPM Control Status",
                        value=DseEngineControllerTags.rpm_status,
                        name="rpm_status",
                        hidden=DseEngineControllerTags.manual_mode_hidden,
                        position=2,
                    ),
                    # What the active mode is doing and what would move it on.
                    ui.TextVariable(
                        "Mode Status",
                        value=DseEngineControllerTags.run_mode_status,
                        name="run_mode_status_text",
                        hidden=DseEngineControllerTags.run_mode_status_hidden,
                        position=3,
                    ),
                    # The plot is history rather than a control, and it's the
                    # tallest thing in the tab — tuck it into a collapsed
                    # submodule at the bottom so the controls stay in view.
                    ui.Submodule(
                        "Regulate Plot",
                        name="regulate_plot_group",
                        is_collapsed=True,
                        hidden=DseEngineControllerTags.regulate_plot_hidden,
                        position=90,
                        children=[
                                    ui.Multiplot(
                                        "Regulate Plot",
                                        name="regulate_plot",
                                        series=[
                                            # Setpoint + Sensor Value share an axis and
                                            # are shown by default, so you can watch the
                                            # PID drive the reading up to the setpoint.
                                            ui.Series(
                                                "Sensor Value",
                                                value=DseEngineControllerTags.regulate_measured,
                                                data_type="number",
                                                active=True,
                                                shared_axis="sensor",
                                            ),
                                            ui.Series(
                                                "Setpoint",
                                                value=DseEngineControllerTags.regulate_setpoint_active,
                                                data_type="number",
                                                active=True,
                                                shared_axis="sensor",
                                            ),
                                            ui.Series(
                                                "Error",
                                                value=DseEngineControllerTags.regulate_error,
                                                data_type="number",
                                                active=False,
                                                shared_axis="sensor",
                                            ),
                                            ui.Series(
                                                "Commanded RPM",
                                                value=DseEngineControllerTags.regulate_output_rpm,
                                                data_type="number",
                                                units="RPM",
                                                active=False,
                                            ),
                                        ],
                                    ),
                        ],
                    ),
                ],
            ),
            # ----- Stop tab -----
            ui.Container(
                "Stop",
                name="stop_tab",
                children=[
                    # Manual Stop Now — always at the top, available in every
                    # control mode (greyed only when the engine isn't running).
                    ui.Button(
                        "Stop Now",
                        name="stop_now",
                        disabled=DseEngineControllerTags.stop_now_disabled,
                        colour=ui.Colour.red,
                        position=1,
                        **CMD_KW,
                        requires_confirm=ui.ConfirmDialog(
                            title="Stop the engine?",
                            warning_reason=(
                                "This will shut the engine down. Anyone relying "
                                "on flow downstream will lose it."
                            ),
                            help_text=(
                                "Withdraws the remote start request. The panel "
                                "runs its own return delay and cooling-down "
                                "period, then stops, and stays in Auto so it can "
                                "be restarted from here."
                            ),
                        ),
                    ),
                    ui.Select(
                        "Stop Control",
                        name="stop_control_mode",
                        options=CONTROL_OPTIONS,
                        position=2,
                        **CMD_KW,
                    ),
                    # Sensor: stop when a remote sensor crosses the setpoint.
                    # The Variable picker + per-sensor setpoint sliders are
                    # injected in setup() (same shape as RPM Regulate).
                    ui.Container(
                        "Stop On Sensor",
                        name="stop_sensor",
                        hidden=DseEngineControllerTags.stop_sensor_hidden,
                        position=3,
                        children=[],
                    ),
                    # Timer: stop at a scheduled date/time.
                    ui.DatetimeInput(
                        "Stop At",
                        name="stop_timer_at",
                        default=None,
                        pickers=["date", "time"],
                        direction="future",
                        hidden=DseEngineControllerTags.stop_timer_hidden,
                        position=4,
                        **CMD_KW,
                    ),
                ],
            ),
        ],
    )

    # --- Panel in Manual ---
    # The panel owns engine speed in Manual and ignores the remote setpoint, so
    # nothing this app writes will land until it's switched to Auto.
    manual_mode_warning = ui.WarningIndicator(
        "Panel is in Manual — no remote engine control",
        name="manual_mode_warning",
        hidden=DseEngineControllerTags.manual_mode_hidden,
        can_cancel=False,
        position=90,
    )

    # --- Auto-start fault ---
    # Only shown when there's something to say, and it says what: this is the app's
    # own fault, not the panel's fault bit — "Engine stopped unexpectedly" here
    # means this app stopped regulating, while the panel may be perfectly happy
    # running in Manual. Latched until the pump is started or running normally, so
    # an operator arriving after the event can still see what happened.
    # can_cancel=False: it clears itself from the run state, and a dismiss button
    # would send a command nothing here handles.
    auto_start_fault = ui.WarningIndicator(
        display_name=DseEngineControllerTags.auto_start_fault,
        name="auto_start_fault",
        hidden=DseEngineControllerTags.auto_start_fault_hidden,
        can_cancel=False,
        position=91,
    )
    # Why an automated run was requested (set by whichever app asked for it).
    run_reason = ui.TextVariable(
        "Run Reason",
        value=DseEngineControllerTags.run_request_reason,
        name="run_reason",
        hidden=DseEngineControllerTags.run_reason_hidden,
        position=92,
    )

    # Position just after the Engine Hours top-level variable (56), before the
    # control tabs (76). Sensor displays are inserted here in setup().
    # Sensor gauges, then digital output states, in one block above the Pump
    # Control tabs. The outputs are positioned from the sensor count rather than
    # a fixed offset, so the block grows with the config instead of the two
    # running into each other on a site with a lot of sensors.
    _SENSOR_BASE_POSITION = 57

    async def setup(self):
        """Build the config-driven UI: sensor displays + variable pickers.

        Runs after config is loaded (the framework calls this before generating
        the runtime schema), so we can read the wired sensor slots — remote tags
        and native analog inputs alike — and render one top-level display per
        configured sensor, plus an Option per sensor in each variable picker.
        """
        configured = build_sensors(self.config, self.tags)

        # Sensor displays live at the top level, just below Engine Hours,
        # rendered as a linear gauge.
        for offset, sensor in enumerate(configured):
            self.add_element(
                ui.NumericVariable(
                    sensor.label,
                    value=sensor.tag,
                    name=f"sensor_{sensor.sanitised}",
                    units=sensor.units,
                    precision=2,
                    form=ui.Widget.linear,
                    ranges=_gauge_ranges(sensor),
                    position=self._SENSOR_BASE_POSITION + offset,
                )
            )

        # Digital output states, immediately below the sensor gauges.
        outputs = build_outputs(self.config, self.tags)
        output_base = self._SENSOR_BASE_POSITION + len(configured)
        for offset, output in enumerate(outputs):
            self.add_element(
                ui.BooleanVariable(
                    output.label,
                    value=output.tag,
                    name=output.element_name,
                    position=output_base + offset,
                )
            )

        self._build_rpm_gauge_bands()
        self._build_run_mode_controls(configured, outputs)
        self._build_analog_diagnostics(configured)
        self._build_flex_sensors()

        # Start / Stop "Sensor" trigger: variable picker + per-sensor setpoint
        # sliders (same shape as RPM Regulate — bounds/units per selected
        # sensor, only the chosen sensor's slider shown).
        for tab_name, select_name, sp_prefix, hidden_prefix in (
            ("start_sensor", "start_variable", "start_setpoint", "start_var_hidden"),
            ("stop_sensor", "stop_variable", "stop_setpoint", "stop_var_hidden"),
        ):
            container = self._find_element(tab_name)
            container.add_children(
                ui.Select(
                    "Variable",
                    name=select_name,
                    options=[ui.Option(s.label) for s in configured],
                    position=1,
                    **CMD_KW,
                )
            )
            self._add_setpoint_sliders(
                container,
                configured,
                name_prefix=sp_prefix,
                hidden_prefix=hidden_prefix,
                position=2,
                confirm_help="Value the trigger compares the sensor reading against.",
            )

    def _add_setpoint_sliders(
        self,
        container,
        configured: list,
        *,
        name_prefix: str,
        hidden_prefix: str,
        position: int,
        confirm_help: str,
    ) -> None:
        """Add one setpoint slider per configured sensor to *container*.

        Each uses that sensor's configured min/max + units, and is hidden unless
        it is the selected variable (via its ``{hidden_prefix}_<sensor>`` flag),
        so only one is ever visible.

        The sliders share a single setpoint tag underneath, but each carries its
        own command value keyed by element name. The app keeps them in step: the
        handler mirrors the clamped value back to the slider it came from, and
        selecting a new variable seeds that sensor's slider from the shared tag
        (see ``_select_sensor_variable``).
        """
        for sensor in configured:
            hidden = sensor.hidden_tags.get(hidden_prefix)
            container.add_children(
                ui.Slider(
                    "Setpoint",
                    name=f"{name_prefix}_{sensor.sanitised}",
                    min_val=sensor.sp_min,
                    max_val=sensor.sp_max,
                    step_size=_slider_step(sensor.sp_min, sensor.sp_max),
                    dual_slider=False,
                    inverted=False,
                    units=sensor.units,
                    hidden=hidden,
                    position=position,
                    **CMD_KW,
                    requires_confirm=ui.ConfirmDialog(
                        title="Set the setpoint?",
                        help_text=confirm_help,
                    ),
                )
            )

    # Run mode sub-panels start here; one per configured mode, in config order.
    _MODE_BASE_POSITION = 10

    def _build_run_mode_controls(self, configured: list, outputs: list) -> None:
        """Build the RPM Control picker and one sub-panel per configured run mode.

        The picker offers the modes this install actually has, and a mode's
        sub-panel holds the numbers an operator may want to move while the pump
        is running — the target RPM of a Fixed mode, the setpoint and sensitivity
        of a Regulate one. Everything structural about a mode stays in config:
        which kind it is, and for Regulate, which sensor it holds.

        Only the active mode's panel is shown, driven by the per-mode hidden flags
        (see ``_apply_rpm_mode_visibility``) — the same shape the per-sensor
        setpoint sliders already use for the Start / Stop triggers.
        """
        by_sensor = {s.sanitised: s for s in configured}
        # Built with the sensors, so a Regulate mode naming "discharge pressure"
        # finds the "Discharge Pressure" input and its panel gets that sensor's
        # bounds and units rather than falling back to a bare 0-100.
        modes = build_modes(self.config, configured, outputs)
        transitions = build_transitions(self.config, modes, configured)
        rpm_tab = self._find_element("rpm_tab")
        rpm_tab.add_children(
            ui.Select(
                "Run Mode",
                name="run_mode",
                options=[ui.Option(mode.label) for mode in modes],
                hidden=DseEngineControllerTags.run_mode_hidden,
                position=1,
                **CMD_KW,
            )
        )
        for offset, mode in enumerate(modes):
            panel = ui.Container(
                mode.label,
                name=mode.element_name("panel"),
                position=self._MODE_BASE_POSITION + offset,
                hidden=self._mode_hidden_tag(mode),
                children=[],
            )
            rpm_tab.add_children(panel)
            if mode.is_regulate:
                self._build_regulate_panel(panel, mode, by_sensor.get(mode.variable))
            elif mode.is_panel:
                self._build_panel_mode_panel(panel, mode)
            else:
                self._build_fixed_panel(panel, mode)
            # A rule's adjustable number belongs with the mode it applies in —
            # that is when moving it means anything.
            for transition in transitions:
                if transition.from_mode == mode.sanitised:
                    self._build_transition_control(panel, transition, by_sensor)

        # A rule with no From Mode applies everywhere, so its control can't live
        # in any one mode's panel; it sits alongside the picker instead.
        for transition in transitions:
            if not transition.from_mode:
                self._build_transition_control(rpm_tab, transition, by_sensor)

    def _build_transition_control(self, parent, transition, by_sensor) -> None:
        """The one number a transition may let an operator move.

        Timer rules get how long to wait, Sensor rules the level they fire at.
        Bounds come from the same place the rule's meaning does: a sensor's own
        setpoint range, or a band around the configured period — a slider running
        the config field's full 1s..24h would be unusable.
        """
        name = transition.tunable_name
        if name is None:
            return
        if transition.when == WHEN_SENSOR:
            sensor = by_sensor.get(transition.variable)
            lo = float(sensor.sp_min) if sensor is not None else 0.0
            hi = float(sensor.sp_max) if sensor is not None else 100.0
            units = sensor.units if sensor is not None else ""
            label = (
                f"{transition.to_label} when "
                f"{transition.variable_label} {transition.direction.lower()}"
            )
        elif transition.when == WHEN_TIMER:
            lo = 0.0
            hi = float(max(2 * transition.after_seconds, 120))
            units = "s"
            label = f"{transition.to_label} after"
        else:
            return
        parent.add_children(
            ui.Slider(
                label,
                name=name,
                min_val=lo,
                max_val=hi,
                step_size=_slider_step(lo, hi),
                dual_slider=False,
                inverted=False,
                units=units,
                position=50 + transition.index,
                **CMD_KW,
                requires_confirm=ui.ConfirmDialog(
                    title=f"Change when the pump moves to {transition.to_label}?",
                    help_text=(
                        "Moves the threshold this rule fires at. What the rule "
                        "watches for is set in config; this is the level."
                    ),
                ),
            )
        )

    def _mode_hidden_tag(self, mode):
        """The per-mode visibility flag created by ``DseEngineControllerTags.setup``.

        Read off the live tags instance, not the class. These are created per
        configured mode at runtime, so they only ever exist on the instance —
        looking them up on the class silently yields ``None``, which binds the
        panel to no flag at all and leaves every mode's controls on screen at
        once. Same reason the per-sensor flags go through ``sensors._bind``.
        """
        name = mode.element_name("hidden")
        tag = getattr(self.tags, name, None)
        if tag is None:
            log.warning(
                "Run mode visibility tag %r does not exist — %r's controls will "
                "always be shown.",
                name,
                mode.label,
            )
        return tag

    def _build_fixed_panel(self, panel, mode) -> None:
        """A Fixed mode's one control: the RPM it holds.

        Bounds come from config because the handler clamps to Target RPM Min /
        Max, and a slider that offers values outside them just shows a number the
        engine will never run at.
        """
        rpm_min = int(cfg_value(self.config.target_rpm_min, 900))
        rpm_max = int(cfg_value(self.config.target_rpm_max, 2000))
        panel.add_children(
            ui.Slider(
                _rpm_slider_label(mode),
                name=mode.element_name("target_rpm"),
                min_val=rpm_min,
                max_val=rpm_max,
                step_size=10,
                dual_slider=False,
                inverted=False,
                units="RPM",
                position=1,
                **CMD_KW,
                requires_confirm=ui.ConfirmDialog(
                    title=f"Change the {mode.label} target RPM?",
                    help_text=(
                        "Sets the engine speed this mode holds. Applied while "
                        "running in this mode; held and asserted on the next "
                        "start otherwise. The panel ignores values outside its "
                        "idle / overspeed band."
                    ),
                ),
            )
        )

    def _build_regulate_panel(self, panel, mode, sensor) -> None:
        """A Regulate mode's controls: the setpoint it holds, and how hard it tries.

        The *variable* isn't here — it is fixed per mode in config, which is what
        lets this be one setpoint slider rather than one per sensor. Its bounds
        and units come from that sensor, so the slider reads in the operator's
        engineering units and can't be dragged somewhere the sensor can't go.
        """
        sp_min = float(sensor.sp_min) if sensor is not None else 0.0
        sp_max = float(sensor.sp_max) if sensor is not None else 100.0
        units = sensor.units if sensor is not None else ""
        label = sensor.label if sensor is not None else (mode.variable_label or "?")
        panel.add_children(
            ui.Slider(
                f"{label} Setpoint",
                name=mode.element_name("setpoint"),
                min_val=sp_min,
                max_val=sp_max,
                step_size=_slider_step(sp_min, sp_max),
                dual_slider=False,
                inverted=False,
                units=units,
                position=1,
                **CMD_KW,
                requires_confirm=ui.ConfirmDialog(
                    title=f"Change the {mode.label} setpoint?",
                    help_text="Target value the loop holds by trimming engine RPM.",
                ),
            ),
            ui.TextVariable(
                "Regulate Status",
                value=DseEngineControllerTags.regulate_status,
                name=mode.element_name("regulate_status"),
                position=2,
            ),
        )
        sens_min = float(self.config.regulate_min_sensitivity.value)
        sens_max = float(self.config.regulate_max_sensitivity.value)
        panel.add_children(
            ui.Slider(
                "Sensitivity",
                name=mode.element_name("sensitivity"),
                min_val=sens_min,
                max_val=sens_max,
                step_size=_slider_step(sens_min, sens_max),
                dual_slider=False,
                inverted=False,
                position=3,
                **CMD_KW,
            )
        )

    def _build_panel_mode_panel(self, panel, mode) -> None:
        """A Maintain Value mode's controls: the set point the DSE's loop holds.

        The variable is whatever flexible sensor the DSE was configured to hold
        (in DSE Configuration Suite), so there is nothing to pick here. Slider
        bounds come from the mode's config; the module clamps to its own Set
        Point Min / Max on top, and the status line says what it settled on.
        """
        lo = float(mode.panel_setpoint_min)
        hi = float(mode.panel_setpoint_max)
        if hi <= lo:
            hi = lo + 100.0
        panel.add_children(
            ui.Slider(
                "Maintain Value Setpoint",
                name=mode.element_name("panel_setpoint"),
                min_val=lo,
                max_val=hi,
                step_size=_slider_step(lo, hi),
                dual_slider=False,
                inverted=False,
                position=1,
                **CMD_KW,
                requires_confirm=ui.ConfirmDialog(
                    title=f"Change the {mode.label} set point?",
                    help_text=(
                        "Target the DSE's own Maintain Value loop holds its "
                        "configured sensor at, by trimming engine speed itself. "
                        "The module clamps it to the limits in its configuration."
                    ),
                ),
            ),
            ui.TextVariable(
                "Maintain Value Status",
                value=DseEngineControllerTags.maintain_status,
                name=mode.element_name("maintain_status"),
                position=2,
            ),
        )

    def _build_flex_sensors(self) -> None:
        """One readout per flexible analogue input the module has.

        Which inputs carry a sensor is the module's configuration, not ours, so
        each is hidden until the module says it is in use (see
        ``_publish_flex_sensors``). Units and ranges come from the module too, but
        a gauge can't change its scale at runtime, so these are plain numbers.
        """
        letters = flex_sensor_letters(self.config)
        container = self._find_element("flex_sensors")
        for offset, letter in enumerate(letters):
            container.add_children(
                ui.NumericVariable(
                    f"Flexible Sensor {letter}",
                    value=getattr(self.tags, flex_value_tag(letter), None),
                    name=f"flex_sensor_{letter.lower()}",
                    hidden=getattr(self.tags, flex_hidden_tag(letter), None),
                    precision=2,
                    position=offset,
                )
            )

    def _build_rpm_gauge_bands(self) -> None:
        """Scale and colour the Engine RPM gauge around the configured maximum.

        The dial ends at Target RPM Maximum + a margin, with that margin as the
        yellow band — so full scale matches what the engine actually does, and
        yellow means "faster than this install will ever command". A fixed
        4000-RPM sweep left the needle idling around half scale on every engine.
        """
        rpm_max = float(cfg_value(self.config.target_rpm_max, 2000))
        gauge_to = rpm_max + HIGH_RPM_MARGIN
        # Keep the bands ordered even if the configured maximum is very low.
        idle_to = min(float(IDLE_RPM_TO), max(rpm_max - HIGH_RPM_MARGIN, 0.0))
        self._find_element("rpm").ranges = [
            ui.Range(label="Idle", min_val=0, max_val=idle_to, colour=ui.Colour.blue),
            ui.Range(
                label="Run", min_val=idle_to, max_val=rpm_max, colour=ui.Colour.green
            ),
            ui.Range(
                label="High", min_val=rpm_max, max_val=gauge_to, colour=ui.Colour.yellow
            ),
        ]

    def _build_analog_diagnostics(self, configured: list) -> None:
        """Add a raw mA / V readout per native analog input to Diagnostics.

        The scaled value is what operators watch; the raw signal is what you need
        to commission or fault-find a loop without shelling into the device.
        """
        analog = [s for s in configured if s.is_analog]
        if not analog:
            return
        diagnostics = self._find_element("diagnostics")
        for offset, sensor in enumerate(analog):
            diagnostics.add_children(
                ui.NumericVariable(
                    f"{sensor.label} (AI {sensor.pin})",
                    value=sensor.raw_tag,
                    name=f"analog_raw_{sensor.sanitised}",
                    units=sensor.signal_units,
                    precision=2,
                    position=200 + offset,
                ),
                ui.BooleanVariable(
                    f"{sensor.label} Fault",
                    value=sensor.fault_tag,
                    name=f"analog_fault_{sensor.sanitised}",
                    position=210 + offset,
                ),
            )

    def _find_element(self, name: str):
        """Depth-first search for a (possibly nested) element by name."""
        stack = list(self._elements.values())
        while stack:
            elem = stack.pop()
            if getattr(elem, "name", None) == name:
                return elem
            children = getattr(elem, "_children", None)
            if children:
                stack.extend(children.values())
        raise KeyError(name)

    # --- Alarms, at the top level so an operator sees them without digging ---
    active_alarms = ui.TextVariable(
        "Active Alarms",
        value=DseEngineControllerTags.alarm_summary,
        name="active_alarms",
        position=56,
    )

    # --- Engine submodule ---
    engine = ui.Submodule(
        "Engine",
        name="engine",
        children=[
            ui.NumericVariable(
                "Oil Pressure",
                value=DseEngineControllerTags.oil_pressure_kpa,
                name="oil_pressure",
                units="kPa",
                precision=0,
                form=ui.Widget.linear,
                ranges=[
                    ui.Range(label="Low", min_val=0, max_val=150, colour=ui.Colour.red),
                    ui.Range(label="Normal", min_val=150, max_val=600, colour=ui.Colour.green),
                    ui.Range(label="High", min_val=600, max_val=1000, colour=ui.Colour.yellow),
                ],
            ),
            ui.NumericVariable(
                "Oil Temp",
                value=DseEngineControllerTags.oil_temp_c,
                name="oil_temp",
                units="°C",
                precision=0,
                form=ui.Widget.linear,
                ranges=[
                    ui.Range(label="Cold", min_val=-40, max_val=60, colour=ui.Colour.blue),
                    ui.Range(label="Normal", min_val=60, max_val=110, colour=ui.Colour.green),
                    ui.Range(label="Hot", min_val=110, max_val=135, colour=ui.Colour.yellow),
                    ui.Range(label="Critical", min_val=135, max_val=200, colour=ui.Colour.red),
                ],
            ),
            ui.NumericVariable(
                "Coolant Level",
                value=DseEngineControllerTags.coolant_level_pct,
                name="coolant_level",
                units="%",
                precision=0,
                form=ui.Widget.linear,
                ranges=[
                    ui.Range(label="Low", min_val=0, max_val=20, colour=ui.Colour.red),
                    ui.Range(label="OK", min_val=20, max_val=100, colour=ui.Colour.green),
                ],
            ),
            ui.NumericVariable(
                "Oil Level",
                value=DseEngineControllerTags.oil_level_pct,
                name="oil_level",
                units="%",
                precision=0,
            ),
            ui.NumericVariable(
                "Fuel Level",
                value=DseEngineControllerTags.fuel_level_pct,
                name="fuel_level",
                units="%",
                precision=0,
                form=ui.Widget.linear,
                ranges=[
                    ui.Range(label="Low", min_val=0, max_val=15, colour=ui.Colour.red),
                    ui.Range(label="OK", min_val=15, max_val=100, colour=ui.Colour.green),
                ],
            ),
            ui.NumericVariable(
                "Fuel Used",
                value=DseEngineControllerTags.fuel_used_l,
                name="fuel_used",
                units="L",
                precision=0,
            ),
            ui.NumericVariable(
                "Intake Manifold Temp",
                value=DseEngineControllerTags.intake_manifold_temp_c,
                name="intake_temp",
                units="°C",
                precision=0,
            ),
            ui.NumericVariable(
                "Exhaust Temp",
                value=DseEngineControllerTags.exhaust_temp_c,
                name="exhaust_temp",
                units="°C",
                precision=0,
            ),
            ui.NumericVariable(
                "Boost Pressure",
                value=DseEngineControllerTags.boost_pressure_kpa,
                name="boost_pressure",
                units="kPa",
                precision=0,
            ),
            ui.NumericVariable(
                "Alternator Voltage",
                value=DseEngineControllerTags.alternator_voltage,
                name="alternator_voltage",
                units="V",
                precision=1,
                form=ui.Widget.linear,
                ranges=[
                    ui.Range(label="Off", min_val=0, max_val=12.5, colour=ui.Colour.blue),
                    ui.Range(label="Charging", min_val=12.5, max_val=15, colour=ui.Colour.green),
                    ui.Range(label="High", min_val=15, max_val=32, colour=ui.Colour.yellow),
                ],
            ),
            ui.NumericVariable(
                "Number of Starts",
                value=DseEngineControllerTags.number_of_starts,
                name="number_of_starts",
                precision=0,
            ),
        ],
    )

    # --- Flexible sensors: the module's own analogue inputs ---
    flex_sensors = ui.Submodule(
        "Panel Sensors",
        name="flex_sensors",
        children=[],  # filled per model in setup() — see _build_flex_sensors
    )

    # --- Speed control: what the module is holding ---
    speed_control = ui.Submodule(
        "Speed Control",
        name="speed_control",
        children=[
            ui.NumericVariable(
                "Panel Target Speed",
                value=DseEngineControllerTags.target_speed_rpm,
                name="target_speed",
                units="rpm",
                precision=0,
            ),
            ui.TextVariable(
                "Maintain Value",
                value=DseEngineControllerTags.maintain_status,
                name="maintain_value_text",
                hidden=DseEngineControllerTags.maintain_hidden,
            ),
            ui.NumericVariable(
                "Maintain Value Reading",
                value=DseEngineControllerTags.maintain_value,
                name="maintain_value",
                precision=2,
                hidden=DseEngineControllerTags.maintain_hidden,
            ),
            ui.NumericVariable(
                "Maintain Value Set Point",
                value=DseEngineControllerTags.maintain_setpoint,
                name="maintain_setpoint",
                precision=2,
                hidden=DseEngineControllerTags.maintain_hidden,
            ),
            ui.TextVariable(
                "Set Point Source",
                value=DseEngineControllerTags.maintain_source,
                name="maintain_source",
                hidden=DseEngineControllerTags.maintain_hidden,
            ),
            ui.NumericVariable(
                "P Gain",
                value=DseEngineControllerTags.maintain_p_gain,
                name="maintain_p_gain",
                precision=1,
                hidden=DseEngineControllerTags.maintain_hidden,
            ),
            ui.NumericVariable(
                "I Gain",
                value=DseEngineControllerTags.maintain_i_gain,
                name="maintain_i_gain",
                precision=1,
                hidden=DseEngineControllerTags.maintain_hidden,
            ),
            ui.NumericVariable(
                "D Gain",
                value=DseEngineControllerTags.maintain_d_gain,
                name="maintain_d_gain",
                precision=1,
                hidden=DseEngineControllerTags.maintain_hidden,
            ),
        ],
    )

    # --- Diagnostics (mode, state flags, identity) ---
    diagnostics = ui.Submodule(
        "Diagnostics",
        name="diagnostics",
        children=[
            ui.TextVariable(
                "Panel Mode",
                value=DseEngineControllerTags.control_mode,
                name="control_mode",
            ),
            ui.TextVariable(
                "Engine State",
                value=DseEngineControllerTags.engine_state,
                name="engine_state",
            ),
            ui.BooleanVariable(
                "Warming Up",
                value=DseEngineControllerTags.is_warming_up,
                name="warming_up",
            ),
            ui.BooleanVariable(
                "Cooling Down",
                value=DseEngineControllerTags.is_cooling_down,
                name="cooling_down",
            ),
            ui.BooleanVariable(
                "Panel Power Held On",
                value=DseEngineControllerTags.pump_power_on,
                name="pump_power_on",
            ),
            ui.BooleanVariable(
                "Common Alarm",
                value=DseEngineControllerTags.has_common_alarm,
                name="common_alarm",
            ),
            ui.BooleanVariable(
                "Warning",
                value=DseEngineControllerTags.has_warning,
                name="has_warning",
            ),
            ui.BooleanVariable(
                "Shutdown / Locked Out",
                value=DseEngineControllerTags.is_faulted,
                name="is_faulted",
            ),
            ui.BooleanVariable(
                "Panel Locked",
                value=DseEngineControllerTags.panel_locked,
                name="panel_locked",
            ),
            ui.NumericVariable(
                "Alarm Count",
                value=DseEngineControllerTags.alarm_count,
                name="alarm_count",
                precision=0,
            ),
            ui.NumericVariable(
                "Manual Run Time Remaining",
                value=DseEngineControllerTags.manual_run_time_remaining_s,
                name="manual_run_time_remaining",
                units="s",
                precision=0,
            ),
            ui.NumericVariable(
                "Serial Number",
                value=DseEngineControllerTags.serial_number,
                name="serial_number",
                precision=0,
            ),
            ui.NumericVariable(
                "Model Number",
                value=DseEngineControllerTags.model_number,
                name="model_number",
                precision=0,
            ),
        ],
    )

    # --- Details: groups Engine, Pump & Sensors and Diagnostics ---
    details = ui.Submodule(
        "Details",
        name="details",
        position=150,
        hidden=True,
        children=[engine, flex_sensors, speed_control, diagnostics],
    )

    # The submodules above live only inside `details`; drop them from the
    # class namespace so the declarative metaclass doesn't also register them as
    # top-level elements (which would render them twice).
    del engine, flex_sensors, speed_control, diagnostics


def export():
    DseEngineControllerUI(None, None, None).export(
        Path(__file__).parents[2] / "doover_config.json",
        "dse_engine_controller",
    )
