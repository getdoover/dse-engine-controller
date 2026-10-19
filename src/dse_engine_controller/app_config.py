from pathlib import Path

from pydoover import config
from pydoover.config import ApplicationPosition, ApplicationDefaultOpen
from pydoover.docker.modbus import ModbusConfig

from .analog import SIGNAL_TYPE_CUSTOM, SIGNAL_TYPES
from .sensor_ranges import RANGE_COLOURS
from .dse_state import DseModel

# Number of remote-sensor slots the operator can wire up. Unlike the analog
# inputs — which are a list, so an install shows exactly what it has — these have
# to be a fixed set of top-level config.TagRefs: pydoover's RemoteTag resolution
# only scans the top level of the schema, so a TagRef inside an Array is never
# bound. Three covers the installs we've seen; each is collapsed by default so
# the unused ones cost a row, not a form.
MAX_REMOTE_SENSORS = 3



# Conditions gating whole blocks of config, so the form only shows fields that
# can actually do something. pydoover conditions test equality against one
# non-conditional sibling field (they can't chain or test "is set"), so each
# block is fronted by an explicit boolean and referenced here by field name.
REMOTE_CONTROL_ON = config.equal("enable_remote_control", True)
ANNOUNCEMENTS_ON = config.equal("enable_announcements", True)
# Gates every sensor field, native and remote alike. It has to be a single
# non-conditional boolean: conditions can't be OR'd together and can't depend on
# another conditional element, so one master switch is the only way to gate
# settings shared by both sensor sources. (Its field name is historical — it
# predates native analog inputs — and is kept for config compatibility.)
# Gates the remote-tag slots. The analog inputs need no gate — they're a list, so
# an install with none shows a single empty row.
REMOTE_TAGS_ON = config.equal("enable_remote_tags", True)


def remote_sensor_reference(index: int) -> str:
    """Stable reference_name linking a config slot to its RemoteTag."""
    return f"remote_sensor_{index}"


class RemoteSensorRef(config.TagRef):
    """A remote sensor slot: another app's tag, plus a friendly label + units.

    Subclasses ``config.TagRef`` so pydoover's ``RemoteTag`` resolution binds it
    automatically and republishes the upstream value locally. ``reference_name``
    is fixed and hidden per slot (operators never touch it); they only pick the
    source app + tag and optionally name it / set units / invert it.
    """

    sensor_label = config.String(
        "Sensor Name",
        description="Friendly label shown in the UI and the RPM Regulate picker.",
        default="",
        name="sensor_label",
    )
    sensor_units = config.String(
        "Units",
        description="Units shown next to the value (e.g. kPa). Optional.",
        default="",
        name="sensor_units",
    )
    invert = config.Boolean(
        "Invert Control",
        description=(
            "Inverse-acting control for RPM Regulate. By default the loop "
            "assumes more RPM raises this reading, so it speeds up when the "
            "reading is below setpoint. Turn this on when more RPM LOWERS the "
            "reading, so it speeds up when the reading is above setpoint."
        ),
        default=False,
        name="invert",
    )
    setpoint_min = config.Number(
        "Setpoint Minimum",
        description="Lower bound for this sensor's setpoint slider (in its units).",
        default=0.0,
        name="setpoint_min",
    )
    setpoint_max = config.Number(
        "Setpoint Maximum",
        description="Upper bound for this sensor's setpoint slider (in its units).",
        default=1000.0,
        name="setpoint_max",
    )
    allow_rpm_control = config.Boolean(
        "Allow RPM Control",
        description=(
            "Let this sensor be picked as the variable RPM Regulate holds. Off by "
            "default: driving engine speed from a reading is a deliberate choice, "
            "and a sensor that's only there to be watched (or to trigger a "
            "start/stop) shouldn't be offered for it."
        ),
        default=False,
        name="allow_rpm_control",
    )



class NullSafeArray(config.Array):
    """A ``config.Array`` that tolerates ``null`` as "no items".

    pydoover's ``Array.load_data`` iterates the value straight away, so a stored
    ``"display_ranges": null`` raises ``TypeError`` while the deployment config is
    being loaded — before ``setup()``, so the app never starts. The site writes
    ``null`` for cleared optional fields (see the blank ``remote_sensor_*`` entries
    in ``simulators/app_config.json``), which makes that a one-click outage.
    """

    def load_data(self, data):
        super().load_data(data or [])


def _remote_sensor_slot(index: int) -> RemoteSensorRef:
    # reference_name is fixed + hidden so RemoteTag resolution can bind it; the
    # operator only wires the source app/tag (+ optional label/units/invert). A
    # blank label falls back to "Remote Sensor N" in the UI.
    return RemoteSensorRef(
        f"Remote Sensor {index + 1}",
        description=(
            "An external sensor read from another app. Leave the application "
            "blank to disable this slot."
        ),
        default=None,  # blank slot → skipped by RemoteTag resolution
        reference_name__default=remote_sensor_reference(index),
        reference_name__hidden=True,
        # Collapsed by default: twelve expanded slot forms is what actually fills
        # the config window, and most installs use one or two.
        default_collapsed=True,
        show_if=REMOTE_TAGS_ON,
    )


class DisplayRangeSlot(config.Object):
    """One coloured band on a sensor's gauge.

    The same shape as the built-in engine/pump gauges' ranges (see app_ui), but
    operator-defined, because only the site knows what "low" means for a
    particular tank or discharge line.
    """

    label = config.String(
        "Label",
        description="Shown on the gauge for this band (e.g. Low, Normal). Optional.",
        default="",
        name="label",
    )
    # No numeric defaults: a row the operator adds and then leaves blank must be
    # skippable. With defaults of 0/100 an unfilled row silently becomes a real
    # 0-100 band, which then caps the gauge (configured bands define its extent).
    min_val = config.Number(
        "From",
        description="Reading this band starts at, in the sensor's units.",
        default=None,
        name="min_val",
    )
    max_val = config.Number(
        "To",
        description="Reading this band ends at, in the sensor's units.",
        default=None,
        name="max_val",
    )
    colour = config.Enum(
        "Colour",
        description="Colour of this band on the gauge.",
        default=RANGE_COLOURS[0],
        choices=list(RANGE_COLOURS),
        name="colour",
    )


class AnalogInputSlot(config.Object):
    """A native analog input: a 4-20 mA (or 0-10 V) loop wired to the device.

    Used as the element of the ``analog_inputs`` list. ``default=None`` on the
    element keeps a freshly-added, not-yet-filled-in row loadable — the site saves
    whatever the form holds, and a row without a pin is skipped at runtime rather
    than taking the app down at config load.

    The device's own analog inputs, read straight off the platform interface —
    no separate 4-20 mA app in the middle. Apart from the pin and the signal
    scaling, the fields mirror :class:`RemoteSensorRef`, because everything
    downstream (gauges, start/stop triggers, RPM Regulate) treats both the same.
    """

    ai_pin = config.Integer(
        "Analog Input Pin",
        description=(
            "Device analog-input pin this sensor is wired to. On a doovit, "
            "AI 0-1 are the 0-10 V inputs and AI 2-5 the 4-20 mA current loops."
        ),
        default=None,
        minimum=0,
        maximum=63,
        name="ai_pin",
    )
    sensor_label = config.String(
        "Sensor Name",
        description="Friendly label shown in the UI and the variable pickers.",
        default="",
        name="sensor_label",
    )
    sensor_units = config.String(
        "Units",
        description="Units shown next to the value (e.g. kPa). Optional.",
        default="",
        name="sensor_units",
    )
    signal_type = config.Enum(
        "Signal Type",
        description=(
            "What the sensor puts out. 4-20 mA also gets open-circuit detection; "
            "0-10 V can't (0 V is a real reading). Custom for anything else."
        ),
        default=SIGNAL_TYPES[0],
        choices=SIGNAL_TYPES,
        name="signal_type",
    )
    signal_min = config.Number(
        "Custom Signal Minimum",
        description="Raw signal at the bottom of the range (e.g. 0 for 0-20 mA).",
        default=4.0,
        show_if=config.equal("signal_type", SIGNAL_TYPE_CUSTOM),
        name="signal_min",
    )
    signal_max = config.Number(
        "Custom Signal Maximum",
        description="Raw signal at the top of the range.",
        default=20.0,
        show_if=config.equal("signal_type", SIGNAL_TYPE_CUSTOM),
        name="signal_max",
    )
    value_min = config.Number(
        "Value At Minimum Signal",
        description=(
            "Reading this sensor represents at the bottom of its signal range "
            "(4 mA / 0 V), in its units."
        ),
        default=0.0,
        name="value_min",
    )
    value_max = config.Number(
        "Value At Maximum Signal",
        description=(
            "Reading at the top of the signal range (20 mA / 10 V). May be lower "
            "than the minimum for an inverse-scaled sensor (e.g. ullage)."
        ),
        default=100.0,
        name="value_max",
    )
    invert = config.Boolean(
        "Invert Control",
        description=(
            "Inverse-acting control for RPM Regulate. By default the loop "
            "assumes more RPM raises this reading, so it speeds up when the "
            "reading is below setpoint. Turn this on when more RPM LOWERS the "
            "reading, so it speeds up when the reading is above setpoint."
        ),
        default=False,
        name="invert",
    )
    setpoint_min = config.Number(
        "Setpoint Minimum",
        description="Lower bound for this sensor's setpoint slider (in its units).",
        default=0.0,
        name="setpoint_min",
    )
    setpoint_max = config.Number(
        "Setpoint Maximum",
        description="Upper bound for this sensor's setpoint slider (in its units).",
        default=100.0,
        name="setpoint_max",
    )
    allow_rpm_control = config.Boolean(
        "Allow RPM Control",
        description=(
            "Let this sensor be picked as the variable RPM Regulate holds. Off by "
            "default: driving engine speed from a reading is a deliberate choice, "
            "and a sensor that's only there to be watched (or to trigger a "
            "start/stop) shouldn't be offered for it."
        ),
        default=False,
        name="allow_rpm_control",
    )

    display_ranges = NullSafeArray(
        "Display Ranges",
        element=DisplayRangeSlot("Range"),
        default=[],  # empty → one plain band spanning the sensor's range
        description=(
            "Coloured bands on this sensor's gauge, e.g. 0-200 red 'Low', "
            "200-1400 green 'Normal', 1400-1600 yellow 'High'. Leave empty for a "
            "single plain band across the whole range."
        ),
        name="display_ranges",
    )
    filter_seconds = config.Number(
        "Smoothing Seconds",
        description=(
            "Time constant of the smoothing filter on this input. Bigger is "
            "steadier but slower to react; 0 disables filtering."
        ),
        default=4.0,
        minimum=0.0,
        maximum=120.0,
        advanced=True,
        name="filter_seconds",
    )
    fault_below = config.Number(
        "Open Circuit Below",
        description=(
            "Current below which the loop is treated as broken (open circuit), "
            "rather than as a low reading. 4-20 mA only."
        ),
        default=3.5,
        minimum=0.0,
        maximum=4.0,
        advanced=True,
        show_if=config.equal("signal_type", SIGNAL_TYPES[0]),
        name="fault_below",
    )


# --- Run modes and the transitions between them ---------------------------
#
# A run mode is one way of deciding the engine's target RPM while it runs, and
# the two ways are the ones this app already had: hold a Fixed RPM, or Regulate a
# sensor to a setpoint. Naming them and listing them turns "the RPM mode" from a
# hardcoded pair into something a site can shape — Warm Up at 800, Line Fill at
# 1300, Run regulating discharge pressure — without any of that being built in
# here. Transitions are a separate list, so several ways of moving between the
# same two modes can coexist, and an operator picking a mode by hand is the same
# operation a transition performs.
#
# Both tables reference modes and sensors by name, matched on the sanitised form
# so "Line Fill" and "line_fill" are the same thing. pydoover config can't offer
# a dynamic picker (only the UI can), so names are typed and checked at startup —
# see run_modes.validate, which names what it couldn't resolve rather than
# leaving the operator to guess.
RUN_MODE_FIXED = "Fixed"
RUN_MODE_REGULATE = "Regulate"
# The DSE's own loop: the module holds one of its flexible-sensor inputs at a
# set point with its internal PID (Maintain Value Control), and this app only
# moves the set point. The loop keeps running if the Doover link drops.
RUN_MODE_PANEL = "Maintain Value"
RUN_MODE_CONTROLS = [RUN_MODE_FIXED, RUN_MODE_REGULATE, RUN_MODE_PANEL]

TRANSITION_TIMER = "Timer"
TRANSITION_SENSOR = "Sensor"
TRANSITION_PANEL = "Panel Flag"
TRANSITION_WHENS = [TRANSITION_TIMER, TRANSITION_SENSOR, TRANSITION_PANEL]

# Panel states worth transitioning on. The DSE reports its own engine operating
# state (warming up, cooling down) and alarm flags, so "move on when the panel
# says warm-up is done" beats guessing with a timer.
PANEL_FLAGS = ["Warming Up", "Cooling Down", "Common Alarm", "Warning"]
PANEL_FLAG_ON = "On"
PANEL_FLAG_OFF = "Off"
PANEL_FLAG_STATES = [PANEL_FLAG_ON, PANEL_FLAG_OFF]

FIXED_ON = config.equal("control", RUN_MODE_FIXED)
REGULATE_ON = config.equal("control", RUN_MODE_REGULATE)
PANEL_ON = config.equal("control", RUN_MODE_PANEL)
WHEN_TIMER_ON = config.equal("when", TRANSITION_TIMER)
WHEN_SENSOR_ON = config.equal("when", TRANSITION_SENSOR)
WHEN_PANEL_ON = config.equal("when", TRANSITION_PANEL)


class DigitalOutputSlot(config.Object):
    """One digital output this device drives."""

    do_pin = config.Integer(
        "Output Pin",
        description="Device digital-output pin this output is wired to.",
        default=None,
        minimum=0,
        maximum=63,
        name="do_pin",
    )
    label = config.String(
        "Output Name",
        description=(
            "What this output is called on the dashboard, and the name a run "
            "mode refers to it by (e.g. Clutch, Discharge Valve)."
        ),
        default="",
        name="label",
    )
    default_off_state = config.Boolean(
        "Default Off State",
        description=(
            "Where this output sits whenever the engine is not turning — between "
            "runs, and through the pre-start warning and the cranking."
        ),
        default=False,
        name="default_off_state",
    )
    default_running_state = config.Boolean(
        "Default Running State",
        description=(
            "Where this output sits while the engine runs, unless a run mode says otherwise."
        ),
        default=False,
        name="default_running_state",
    )


class ModeOutputSlot(config.Object):
    """One output a run mode drives, and where it drives it."""

    output = config.String(
        "Output Name",
        description="Digital output this mode sets, by name.",
        default="",
        name="output",
    )
    state = config.Boolean(
        "On",
        description="Whether this output is energised while this mode runs.",
        default=False,
        name="state",
    )


class RunModeSlot(config.Object):
    """One named way of driving engine speed while the pump runs."""

    label = config.String(
        "Mode Name",
        description=(
            "What this mode is called on the dashboard, and the name transitions "
            "refer to it by (e.g. Warm Up, Line Fill, Run)."
        ),
        default="",
        name="label",
    )
    control = config.Enum(
        "Control",
        description=(
            "How this mode decides engine speed. Fixed holds a target RPM; "
            "Regulate runs this app's PI loop to hold a Doover sensor at a "
            "setpoint by writing the target RPM; Maintain Value hands the loop "
            "to the DSE itself — it holds the flexible-sensor input it was "
            "configured with (in DSE Configuration Suite) at a set point, and "
            "this app only moves that set point."
        ),
        default=RUN_MODE_FIXED,
        choices=RUN_MODE_CONTROLS,
        name="control",
    )
    target_rpm = config.Integer(
        "Target RPM",
        description=(
            "Speed this mode holds. Required: a Fixed mode with no RPM has "
            "nothing to say, and falling back to some app-chosen number would "
            "run the pump at a speed nobody picked. The operator can still move "
            "it on the dashboard while the pump runs; this is where it starts, "
            "and where it returns to on the next deployment."
        ),
        default=None,
        required=True,
        minimum=0,
        maximum=4000,
        show_if=FIXED_ON,
        name="target_rpm",
    )
    variable = config.String(
        "Regulated Variable",
        description=(
            "Sensor this mode holds at a setpoint, by name (its Sensor Name from "
            "the Analog Inputs or Remote Sensor config). The sensor must have "
            "Allow RPM Control turned on. Fixed per mode rather than picked on "
            "the dashboard: which reading drives engine speed is a commissioning "
            "decision, and fixing it here is what lets each mode have one clean "
            "setpoint control instead of one per sensor."
        ),
        default="",
        show_if=REGULATE_ON,
        name="variable",
    )
    setpoint = config.Number(
        "Setpoint",
        description=(
            "Starting value for this mode's setpoint control, in the sensor's "
            "units. The operator can move it on the dashboard within the bounds "
            "set on the sensor itself."
        ),
        default=None,
        show_if=REGULATE_ON,
        name="setpoint",
    )
    sensitivity = config.Number(
        "Regulate Sensitivity",
        description=(
            "Starting value for this mode's sensitivity control. A mode that "
            "wants a gentler or sharper loop than the rest can begin somewhere "
            "else on the scale."
        ),
        default=None,
        advanced=True,
        show_if=REGULATE_ON,
        name="sensitivity",
    )
    panel_setpoint = config.Number(
        "Maintain Value Setpoint",
        description=(
            "Starting set point for the DSE's Maintain Value loop, in the units "
            "of the flexible sensor it was configured to hold (e.g. 2.5 for "
            "2.5 bar). Leave blank to leave whatever set point the panel "
            "already has. The operator can move it on the dashboard; the panel "
            "clamps it to the Set Point Min / Max in its own configuration."
        ),
        default=None,
        show_if=PANEL_ON,
        name="panel_setpoint",
    )
    panel_setpoint_min = config.Number(
        "Maintain Value Slider Minimum",
        description=(
            "Lower bound of the dashboard set point slider for this mode. The "
            "panel's own Set Point Min still applies on top."
        ),
        default=0.0,
        show_if=PANEL_ON,
        name="panel_setpoint_min",
    )
    panel_setpoint_max = config.Number(
        "Maintain Value Slider Maximum",
        description=(
            "Upper bound of the dashboard set point slider for this mode, in "
            "the flexible sensor's units."
        ),
        default=100.0,
        show_if=PANEL_ON,
        name="panel_setpoint_max",
    )
    outputs = NullSafeArray(
        "Digital Outputs",
        element=ModeOutputSlot("Output", default=None),
        default=[],
        description=(
            "Digital outputs this mode drives while it runs. Anything left out "
            "rests at its own Default State, so a mode says what the outputs are "
            "rather than changing some and inheriting the rest from whichever "
            "mode happened to run before it."
        ),
        name="outputs",
    )
    minimum_seconds = config.Integer(
        "Minimum Seconds",
        description=(
            "Least time this mode runs before any transition may move on from it."
        ),
        default=0,
        minimum=0,
        maximum=86400,
        name="minimum_seconds",
    )


class RunTransitionSlot(config.Object):
    """One rule for moving from one run mode to another."""

    from_mode = config.String(
        "From Mode",
        description=(
            "Mode this rule applies in, by name. Leave blank to apply in every "
            "mode."
        ),
        default="",
        name="from_mode",
    )
    to_mode = config.String(
        "To Mode",
        description=(
            "Mode to move into when this rule fires, by name. The one reserved "
            "name is 'Shutdown', which stops the engine"
        ),
        default="",
        name="to_mode",
    )
    when = config.Enum(
        "When",
        description=(
            "What fires this transition. Timer: time spent in the From Mode. "
            "Sensor: a reading crossing a setpoint. Panel Flag: a status bit the "
            "module itself reports."
        ),
        default=TRANSITION_SENSOR,
        choices=TRANSITION_WHENS,
        name="when",
    )
    after_seconds = config.Integer(
        "After Seconds",
        description="How long to stay in the From Mode before moving on.",
        default=60,
        minimum=1,
        maximum=86400,
        show_if=WHEN_TIMER_ON,
        name="after_seconds",
    )
    variable = config.String(
        "Trigger Variable",
        description="Sensor this rule watches, by name.",
        default="",
        show_if=WHEN_SENSOR_ON,
        name="variable",
    )
    direction = config.Enum(
        "Trigger Direction",
        description=(
            "Fire when the reading is Above or Below the setpoint below. Same "
            "vocabulary as the Start and Stop sensor triggers."
        ),
        default="Above",
        choices=["Above", "Below"],
        show_if=WHEN_SENSOR_ON,
        name="direction",
    )
    setpoint = config.Number(
        "Trigger Setpoint",
        description="Value the reading is compared against, in the sensor's units.",
        default=None,
        show_if=WHEN_SENSOR_ON,
        name="setpoint",
    )
    panel_flag = config.Enum(
        "Panel Flag",
        description="State reported by the DSE module itself.",
        default=PANEL_FLAGS[0],
        choices=PANEL_FLAGS,
        show_if=WHEN_PANEL_ON,
        name="panel_flag",
    )
    panel_state = config.Enum(
        "Flag State",
        description=(
            "Fire while the flag is On or Off. 'Warming Up / Off' is how you say "
            "'once the panel has finished warming up' — pair it with a Hold "
            "For Seconds so it can't fire before warm-up has even begun."
        ),
        default=PANEL_FLAG_OFF,
        choices=PANEL_FLAG_STATES,
        show_if=WHEN_PANEL_ON,
        name="panel_state",
    )
    adjustable = config.Boolean(
        "Adjustable On Dashboard",
        description=(
            "Put this rule's number on the dashboard so an operator can move it "
            "while the pump runs — how long a Timer waits, or the level a Sensor "
            "fires at. It appears with the controls of the mode the rule applies "
            "in, since that is when it matters, and starts at the value set "
            "here. What the rule *is* stays in config; only its threshold moves. "
            "A Panel Flag rule has no number to offer, so this does nothing for "
            "one."
        ),
        default=False,
        advanced=True,
        name="adjustable",
    )
    # A hold applies to Sensor and Panel Flag but is meaningless for Timer, which
    # is already a duration. pydoover conditions only test equality against one
    # sibling, so there is no way to say "show unless Timer" or "show for either
    # of these two" — hence one field per condition kind rather than a single one
    # that appears where it does nothing. Both feed the same runtime hold.
    sensor_hold_seconds = config.Integer(
        "Hold Condition For Seconds",
        description=(
            "How long the reading must stay past the setpoint before this fires "
            "— 'pressure above 200 kPa for 10 seconds'. 0 fires the moment it "
            "crosses. The hold re-arms whenever the mode changes, and a reading "
            "that drops out mid-hold starts it over."
        ),
        default=0,
        minimum=0,
        maximum=86400,
        show_if=WHEN_SENSOR_ON,
        name="sensor_hold_seconds",
    )
    panel_hold_seconds = config.Integer(
        "Hold Condition For Seconds",
        description=(
            "How long the flag must stay in that state before this fires. 0 "
            "fires as soon as it is. Worth pairing with 'Warming Up / Off', "
            "which reads true before the panel has raised its warm-up bit at "
            "all — though a Minimum Seconds on the mode is the sturdier guard."
        ),
        default=0,
        minimum=0,
        maximum=86400,
        show_if=WHEN_PANEL_ON,
        name="panel_hold_seconds",
    )


class DseEngineControllerConfig(config.Schema):
    model = config.Enum(
        "DSE Model",
        description=(
            "Which DSE E-series controller this app is talking to. Decides how "
            "many flexible sensor inputs and alarm registers are read."
        ),
        default=DseModel.E400,
        choices=DseModel,
    )
    modbus_id = config.Integer(
        "Modbus Server ID",
        description=(
            "Modbus address of the DSE module (factory default 10). The "
            "module's RS485 port defaults to 19200 baud, 8 data bits, no "
            "parity, 1 stop bit — set the Modbus bus to match."
        ),
        default=10,
        minimum=1,
        maximum=247,
    )
    poll_period = config.Number(
        "Poll Period Seconds",
        description=(
            "How often to read the module's live registers (mode, alarms, "
            "engine speed, speed control). The module's own 'client inactivity "
            "timeout' must be set to at least twice this, or it will treat the "
            "link as lost between polls."
        ),
        default=2.0,
        minimum=0.5,
        maximum=60,
        advanced=True,
    )
    slow_poll_period = config.Number(
        "Slow Poll Period Seconds",
        description=(
            "How often to read the registers that change slowly or not at all: "
            "run hours, fuel, the full input-alarm table, flexible sensor "
            "readings. Keeps the per-poll bus load down at 19200 baud."
        ),
        default=10.0,
        minimum=2,
        maximum=600,
        advanced=True,
    )
    power_off_message = config.String(
        "Power Off Message",
        description=(
            "What the app tile says when the panel can't be reached. A module "
            "that is switched off looks exactly like one that is unreachable, "
        ),
        default="Power Off",
        advanced=True,
    )
    enable_remote_control = config.Boolean(
        "Enable Remote Control",
        description=(
            "Show the auto-start controls (Start / Stop / target RPM). Every "
            "start runs a beacon/horn pre-start warning and handles the panel "
            "alarm-reset + Auto-mode sequence automatically. Disable to run as "
            "monitor-only."
        ),
        default=True,
    )
    prestart_warning_seconds = config.Integer(
        "Pre-Start Warning Seconds",
        description=(
            "How long minimum warning period is before the engine "
            "may actually turn over start."
        ),
        default=30,
        minimum=5,
        maximum=300,
        show_if=REMOTE_CONTROL_ON,
        advanced=True,
    )
    start_timeout_seconds = config.Integer(
        "Start Timeout Seconds",
        description=(
            "Safety cap on the whole start attempt: Starts counting from the "
            "moment a start is requested, covering the pre-start warning, "
            "getting the panel ready (reset alarms → Auto) and the engine "
            "actually reporting running."
        ),
        default=90,
        minimum=30,
        maximum=600,
        show_if=REMOTE_CONTROL_ON,
        advanced=True,
    )
    stop_timeout_seconds = config.Integer(
        "Stop Timeout Seconds",
        description=(
            "Safety cap: if the engine hasn't reported stopped this many "
            "seconds after a stop command, the auto-start flags a fault."
        ),
        default=120,
        minimum=10,
        maximum=600,
        show_if=REMOTE_CONTROL_ON,
        advanced=True,
    )
    beacon_do_pin = config.Integer(
        "Beacon Output Pin",
        description=(
            "Device digital-output pin that drives the warning beacon. Energised "
            "for the whole pre-start warning and while cranking. Leave empty to "
            "disable beacon output."
        ),
        default=None,
        show_if=REMOTE_CONTROL_ON,
    )
    horn_do_pin = config.Integer(
        "Horn Output Pin",
        description=(
            "Optional second digital-output pin for a warning horn. Pulses "
            "during the pre-start warning countdown. Leave empty to disable."
        ),
        default=None,
        show_if=REMOTE_CONTROL_ON,
    )
    pump_power_do_pin = config.Integer(
        "Panel Power Output Pin",
        description=(
            "Optional digital-output pin that switches power to the DSE "
            "module, so it can be left unpowered between runs. Held on while a "
            "run is wanted, for the whole start sequence and run, and through "
            "cooldown; released once the pump is idle again. It is never held "
            "off: wire it in parallel with the panel's own power switch and the "
            "panel can still be powered up and run by hand at any time. With "
            "Enable Remote Control off the app only watches the panel, so the "
            "pin is simply held on. Leave empty if the panel is always powered."
        ),
        default=None,
    )
    target_rpm_min = config.Integer(
        "Target RPM Minimum",
        description=(
            "Lower bound for the Target RPM input. Keep it above the engine's "
            "configured minimum speed on the DSE."
        ),
        default=900,
        minimum=0,
        maximum=4000,
        show_if=REMOTE_CONTROL_ON,
        advanced=True,
    )
    target_rpm_max = config.Integer(
        "Target RPM Maximum",
        description=(
            "Upper bound for the Target RPM input. Keep it below the DSE's "
            "overspeed and maximum speed settings."
        ),
        default=2000,
        minimum=0,
        maximum=4000,
        show_if=REMOTE_CONTROL_ON,
        advanced=True,
    )
    # --- Run modes -------------------------------------------------------
    # Lists rather than fixed fields, so a site shows exactly the modes it runs.
    # Empty is the ordinary case and means "one Fixed mode" — the app synthesises
    # it (see run_modes.build_run_modes), so there is always something to run and
    # deleting every row can't leave the pump with no way to set a speed.
    run_modes = NullSafeArray(
        "Run Modes",
        element=RunModeSlot("Run Mode", default=None),
        default=[],
        description=(
            "Ways this pump can be run. Leave empty for a single Fixed RPM mode, which is "
            "how the app behaves with nothing configured."
        ),
        name="run_modes",
    )
    include_default_fixed_run_mode = config.Boolean(
        "Include Default Fixed Run Mode",
        description=(
            "Offer a plain Fixed RPM mode alongside whatever is configured "
            "above, so an operator always has somewhere to take manual control "
            "of engine speed regardless of the programme. Nothing transitions "
            "into or out of it — it is reached by hand and stays until it is "
            "left by hand. Turn off to restrict the picker to the configured "
            "modes only. With no modes configured at all it is the only mode, "
            "on or off."
        ),
        default=True,
        advanced=True,
        name="include_default_fixed_run_mode",
    )
    start_in_mode = config.String(
        "Start In Mode",
        description=(
            "Mode every start begins in, by name. Leave blank to carry on in "
            "whatever mode the pump was last left in — including one an operator "
            "picked before shutting it down. Deliberately not tied to the order "
            "of the list above: that order is how the picker reads, and having it "
            "decide what every start does would turn re-ordering into a "
            "behaviour change."
        ),
        default="",
        name="start_in_mode",
    )
    run_mode_transitions = NullSafeArray(
        "Run Mode Transitions",
        element=RunTransitionSlot("Transition", default=None),
        default=[],
        description=(
            "Rules for moving between run modes automatically, checked in order "
            "— the first one whose condition is met wins. Leave empty to change "
            "modes only by hand."
        ),
        name="run_mode_transitions",
    )

    # --- Spoken pre-start announcement ---
    enable_announcements = config.Boolean(
        "Enable Spoken Announcements",
        description=(
            "Speak the pre-start warning and the start over a speaker app. Only "
            "applies when Enable Remote Control is on."
        ),
        default=False,
        advanced=True,
        name="enable_announcements",
    )
    announcer_app = config.Application(
        "Announcement Speaker",
        description="The speaker app that voices the announcements.",
        default=None,
        advanced=True,
        show_if=ANNOUNCEMENTS_ON,
    )
    prestart_announcement = config.String(
        "Pre-Start Announcement",
        description=(
            "What the speaker says while the pre-start warning runs, repeated "
            "every 20 seconds until the engine is commanded to start. {name} is "
            "replaced with this app's display name. Deliberately no countdown: "
            "the warning stays up until the panel is ready to take a start, and "
            "the panel's reset/reboot into Auto takes an unpredictable time, so "
            "any figure would be a guess. An old {seconds} placeholder is "
            "reworded to 'shortly' automatically."
        ),
        default="{name} starting.",
        advanced=True,
        show_if=ANNOUNCEMENTS_ON,
    )
    start_announcement = config.String(
        "Start Announcement",
        description=(
            "What the speaker says the moment the engine begins to crank, after the pre-start warning."
        ),
        default="{name} starting now.",
        advanced=True,
        show_if=ANNOUNCEMENTS_ON,
    )
    start_failed_announcement = config.String(
        "Start Failed Announcement",
        description=(
            "What the speaker says when an auto-start gives up — the panel never "
            "became ready, or the engine never ran. Anyone who heard the warning "
            "otherwise just hears the horn stop, which is what a successful start "
            "sounds like too. {name} is replaced with this app's display name; "
            "leave blank for no announcement."
        ),
        default="{name} startup failed.",
        advanced=True,
        show_if=ANNOUNCEMENTS_ON,
    )

    # --- Sensors ---
    # Native analog inputs — the same sensor contract as a remote tag, read
    # straight off the device's own 4-20 mA / 0-10 V inputs. A list rather than
    # fixed slots: an install shows exactly the inputs it has, and adding a
    # fourth doesn't need a new app release.
    analog_inputs = NullSafeArray(
        "Analog Inputs",
        element=AnalogInputSlot("Analog Input", default=None),
        default=[],
        description=(
            "Sensors wired directly to this device's analog inputs. Add one entry "
            "per sensor. On a doovit, AI 0-1 are the 0-10 V inputs and AI 2-5 the "
            "4-20 mA current loops."
        ),
        name="analog_inputs",
    )

    # Whatever this device drives: a clutch, a valve, a light. The app has no
    # opinion on what any of them mean — the run modes say which are on, and
    # everything else rests at its configured default.
    digital_outputs = NullSafeArray(
        "Digital Outputs",
        element=DigitalOutputSlot("Digital Output", default=None),
        default=[],
        description=(
            "Digital outputs wired to this device, one entry per output. Their "
            "state is shown on the dashboard, and each run mode says which of "
            "them it wants on."
        ),
        name="digital_outputs",
    )

    sensor_fault_action = config.Enum(
        "Sensor Fault Action",
        description=(
            "What to do when a native analog sensor the app is acting on fails "
            "(open circuit / over range) for longer than the grace period below. "
            "Hold keeps the last commanded RPM and stops sensor triggers firing; "
            "Stop Engine shuts the engine down through the normal cooldown."
        ),
        default="Hold",
        choices=["Hold", "Stop Engine"],
        advanced=True,
    )
    sensor_fault_grace_seconds = config.Integer(
        "Sensor Fault Grace Seconds",
        description=(
            "How long a sensor must stay faulted before the Sensor Fault Action "
            "is taken. Rides out brief glitches without a nuisance shutdown."
        ),
        default=30,
        minimum=5,
        maximum=600,
        advanced=True,
    )

    start_sensor_direction = config.Enum(
        "Start Trigger Direction",
        description=(
            "For Sensor start control: fire the start when the chosen sensor "
            "reads Above or Below its start setpoint."
        ),
        default="Below",
        choices=["Above", "Below"],
        advanced=True,
    )
    stop_sensor_direction = config.Enum(
        "Stop Trigger Direction",
        description=(
            "For Sensor stop control: fire the stop when the chosen sensor reads "
            "Above or Below its stop setpoint."
        ),
        default="Above",
        choices=["Above", "Below"],
        advanced=True,
    )
    regulate_min_sensitivity = config.Number(
        "Regulate Sensitivity Minimum",
        description=(
            "Lower bound (gentlest) for the RPM Regulate sensitivity slider. The "
            "knob is geometric — every 6 steps divides the loop's gains by ten — so "
            "the default 0.1 is about a 45th of the top of the scale. Raise it if "
            "you never want the loop that slow."
        ),
        default=0.1,
        minimum=0.1,
        maximum=100,
        advanced=True,
    )
    regulate_max_sensitivity = config.Number(
        "Regulate Sensitivity Maximum",
        description="Upper bound (most aggressive) for the RPM Regulate sensitivity input.",
        default=10.0,
        minimum=0.1,
        maximum=100,
        advanced=True,
    )

    # --- Remote tags ---
    # Reading another app's tag is the exception rather than the rule, and each
    # slot is a form rather than a list row, so the slots sit behind a switch.
    enable_remote_tags = config.Boolean(
        "Enable Remote Tags",
        description=(
            "Read sensors published by other apps on this device, in addition to "
            "the analog inputs above. Shows the remote tag slots below."
        ),
        default=False,
        name="enable_remote_tags",
    )
    # Each slot is read from another app and shown in the UI, and can be picked as
    # a start/stop trigger or as the RPM Regulate variable — exactly like an
    # analog input. Wired to the matching RemoteTag declarations in app_tags.py by
    # reference_name.
    sensor_ref_0 = _remote_sensor_slot(0)
    sensor_ref_1 = _remote_sensor_slot(1)
    sensor_ref_2 = _remote_sensor_slot(2)

    modbus_config = ModbusConfig()
    position = ApplicationPosition()
    default_open = ApplicationDefaultOpen()


def export():
    DseEngineControllerConfig.export(
        Path(__file__).parents[2] / "doover_config.json", "dse_engine_controller"
    )
