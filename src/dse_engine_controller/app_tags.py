import logging

from pydoover.tags import RemoteTag, Tag, Tags


from .dse_state import DseModel
from .outputs import output_specs
from .run_modes import build_modes
from .sensors import HIDDEN_TAG_PREFIXES, cfg_value, sensor_specs

log = logging.getLogger(__name__)


def flex_sensor_letters(config) -> str:
    """The flexible-sensor input letters the configured model has."""
    model = cfg_value(getattr(config, "model", None), DseModel.E400)
    if not isinstance(model, DseModel):
        try:
            model = DseModel(str(model))
        except ValueError:
            model = DseModel.E400
    return "ABCDEFGHIJKL"[: model.flex_sensor_count]


def flex_value_tag(letter: str) -> str:
    return f"flex_{letter.lower()}_value"


def flex_hidden_tag(letter: str) -> str:
    return f"flex_{letter.lower()}_hidden"

# Most tags here are bound to the UI and want real-time updates, so they're
# declared with live=True — the framework republishes live tags as one-shot
# messages each main-loop iteration, keeping the dashboard fresh. Genuinely
# static flags (model-based visibility set once at startup) and internal-only
# orchestration tags are left non-live to avoid pointless republish traffic.


class DseEngineControllerTags(Tags):
    # --- Display / state ---
    status_string = Tag("string", default="DSE (no comms)", live=True)
    state = Tag("string", default="initialising", live=True)
    is_contactable = Tag("boolean", default=False, live=True)

    # --- Panel mode / RPM control availability ---
    # In Manual the panel owns engine speed and ignores the target speed
    # register, so the RPM controls can't do anything and say so. (Greying them
    # out would be better, but pydoover only supports `disabled` on Buttons.)
    manual_mode_hidden = Tag("boolean", default=True, live=True)
    rpm_status = Tag("string", default="", live=True)
    # Hides the RPM mode picker in Manual, alongside the two sub-panel flags
    # below. Default visible: we haven't heard from the panel yet at startup.
    run_mode_hidden = Tag("boolean", default=False, live=True)

    # --- Control mode + engine state (GenComm pages 3 and 5) ---
    control_mode = Tag("string", default="", live=True)  # Stop / Auto / Manual
    engine_state = Tag("string", default="", live=True)  # the module's operating state
    is_auto = Tag("boolean", default=False, live=True)
    is_running = Tag("boolean", default=False, live=True)
    is_pre_start = Tag("boolean", default=False, live=True)
    is_warming_up = Tag("boolean", default=False, live=True)
    is_cooling_down = Tag("boolean", default=False, live=True)
    has_common_alarm = Tag("boolean", default=False, live=True)
    panel_locked = Tag("boolean", default=False, live=True)

    # --- Alarms (GenComm pages 153 / 154) ---
    # A DSE reports a condition for every alarm it knows about rather than one
    # fault code. fault_string names the worst active one; alarm_summary lists
    # them all, worst first.
    fault_string = Tag("string", default="", live=True)
    is_faulted = Tag("boolean", default=False, live=True)
    has_warning = Tag("boolean", default=False, live=True)
    alarm_summary = Tag("string", default="", live=True)
    alarm_count = Tag("integer", default=0, live=True)

    # --- Engine telemetry (pages 4, 5, 7) ---
    engine_rpm = Tag("number", default=None, live=True)
    engine_load_pct = Tag("number", default=None, live=True)
    coolant_temp_c = Tag("number", default=None, live=True)
    oil_temp_c = Tag("number", default=None, live=True)
    oil_pressure_kpa = Tag("number", default=None, live=True)
    coolant_level_pct = Tag("number", default=None, live=True)
    oil_level_pct = Tag("number", default=None, live=True)
    fuel_level_pct = Tag("number", default=None, live=True)
    fuel_rate_lph = Tag("number", default=None, live=True)
    fuel_used_l = Tag("number", default=None, live=True)
    boost_pressure_kpa = Tag("number", default=None, live=True)
    intake_manifold_temp_c = Tag("number", default=None, live=True)
    exhaust_temp_c = Tag("number", default=None, live=True)
    alternator_voltage = Tag("number", default=None, live=True)
    battery_voltage = Tag("number", default=None, live=True)
    total_engine_hours = Tag("number", default=None, live=True)
    number_of_starts = Tag("number", default=None, live=True)
    manual_run_time_remaining_s = Tag("number", default=None, live=True)

    # --- Speed control / Maintain Value (page 137) ---
    # target_speed_rpm is the panel's echo of the target RPM this app (or the
    # fascia) last set. The maintain_* tags describe the module's own PID loop,
    # which holds one flexible-sensor input at a set point.
    target_speed_rpm = Tag("number", default=None, live=True)
    maintain_available = Tag("boolean", default=False, live=True)
    maintain_hidden = Tag("boolean", default=True, live=True)
    maintain_setpoint = Tag("number", default=None, live=True)
    maintain_setpoint_min = Tag("number", default=None, live=True)
    maintain_setpoint_max = Tag("number", default=None, live=True)
    maintain_value = Tag("number", default=None, live=True)
    maintain_units = Tag("string", default="", live=True)
    maintain_source = Tag("string", default="", live=True)
    maintain_status = Tag("string", default="", live=True)
    maintain_p_gain = Tag("number", default=None, live=True)
    maintain_i_gain = Tag("number", default=None, live=True)
    maintain_d_gain = Tag("number", default=None, live=True)

    # --- Flexible sensors (page 178) ---
    # One value + hidden flag per input A..L; the module says which are
    # configured, and the UI hides the rest. Created in setup() per model.

    # --- Identity ---
    manufacturer_code = Tag("integer", default=0, live=True)
    model_number = Tag("integer", default=0, live=True)
    serial_number = Tag("integer", default=0, live=True)
    supported_keys = Tag("string", default="")  # which control keys the module advertises

    # --- Auto-start orchestration ---
    auto_start_state = Tag("string", default="Ready", live=True)
    # Last auto-start fault, and whether to show it. Latched past the fault state
    # so an operator arriving later can see what happened; cleared when the pump is
    # started or running normally. This is the app's fault, not the panel's
    # (fault_code / fault_string above).
    auto_start_fault = Tag("string", default="", live=True)
    auto_start_fault_hidden = Tag("boolean", default=True, live=True)
    # When the latched fault happened, so the indicator can stop shouting about
    # one nobody has dealt with in a day. Not live — nothing renders it.
    auto_start_fault_at = Tag("number", default=None)
    auto_start_armed = Tag("boolean", default=False)
    # Whether automated (cross-app) run requests are allowed to start the
    # engine. Operator Start always works; this only gates the run_request tag.
    # Defaults off (Manual Only) so automation is opt-in.
    automation_enabled = Tag("boolean", default=False)
    prestart_remaining_s = Tag("number", default=None, live=True)
    beacon_active = Tag("boolean", default=False)
    horn_active = Tag("boolean", default=False)
    # Whether the app is holding the panel's power output on. Only meaningful
    # when a Pump Power Output Pin is configured; the panel can also be powered
    # by hand with this false.
    pump_power_on = Tag("boolean", default=False, live=True)
    # Cross-app run request: another app sets `run_request` true to demand a
    # run; `run_request_reason` is shown in the UI so operators know why.
    run_request = Tag("boolean", default=False)
    run_request_reason = Tag("string", default="", live=True)

    # --- Remote sensors ---
    # One RemoteTag per config slot (app_config.sensor_ref_<i>), matched by
    # reference_name. republish_locally mirrors the upstream value into this
    # app's namespace under that name, so the dynamic UI binds to it directly.
    # optional=True → a slot the operator left blank resolves to None.
    remote_sensor_0 = RemoteTag("number", reference_name="remote_sensor_0", optional=True, live=True)
    remote_sensor_1 = RemoteTag("number", reference_name="remote_sensor_1", optional=True, live=True)
    remote_sensor_2 = RemoteTag("number", reference_name="remote_sensor_2", optional=True, live=True)

    # --- Native analog inputs ---
    # Created per configured input by setup(), since the config is a list rather
    # than a fixed set of slots. Names are keyed on the AI pin (analog_value_pin2,
    # analog_raw_pin2, analog_fault_pin2), so reordering the list can't move one
    # sensor's history onto another's tags.
    #
    # Aggregate health, published for other apps and alerts rather than shown on
    # the dashboard: a summary line naming what's wrong, and a single flag.
    analog_fault_string = Tag("string", default="", live=True)
    sensor_fault_active = Tag("boolean", default=False, live=True)

    # --- Run modes ---
    # Which of the configured modes is driving engine speed, and what it is
    # waiting for. The *selection* itself lives in the rpm_mode ui_cmd, written
    # by the operator's picker and by transitions alike; these are the readouts.
    # The label of the mode actually running, for other apps and for alerts. The
    # *selection* is the `run_mode` ui_cmd; this is what the app settled on, the
    # same distinction regulate_setpoint_active draws.
    run_mode_active = Tag("string", default="", live=True)
    run_mode_status = Tag("string", default="", live=True)
    # Hidden when it could only ever say one thing — a single mode with nothing
    # that could move it. A line that never changes is furniture.
    run_mode_status_hidden = Tag("boolean", default=False, live=True)
    # The sensor a Regulate mode is holding, by its sanitised name; blank in a
    # Fixed mode. Which sensor that is lives in config, per mode, so nothing
    # else published says it — and a display driving off these tags has to know
    # in order to put the setpoint against the right reading rather than
    # against engine speed. Sanitised rather than the label for the same reason
    # `run_mode` stores the sanitised name: it is for matching, and
    # `run_mode_active` is the one for reading.
    run_mode_variable = Tag("string", default="", live=True)
    # Wall-clock epoch seconds the current mode was entered. Not live — nothing
    # renders it — but persisted, so an app restart mid-run picks the sequence up
    # where it left off instead of restarting every transition timer.
    run_mode_entered_at = Tag("number", default=None)
    # The regulate plot is history of a loop that only runs in a Regulate mode.
    # Statically declared (unlike the per-mode flags) because there is one plot,
    # so the class-level UI declaration can bind straight to it.
    regulate_plot_hidden = Tag("boolean", default=True, live=True)

    # Per-mode UI visibility flags (only the active mode's controls are shown)
    # are created per configured mode by setup() — see RunMode.element_name.

    # --- RPM control mode (Fixed setpoint vs Regulate a variable) ---
    # The mode itself, the chosen variable, the sensitivity and the setpoints are
    # operator settings and live in the ui_cmds aggregate on the controls that
    # set them — see Application._command_value. Only the loop's own readouts
    # are tags.
    regulate_status = Tag("string", default="", live=True)  # human-readable state

    # These four feed the Regulate Plot. No log_on: the application decides when
    # they are worth a data point and writes all four with log=True together, so
    # the four series share x-points and cost one message between them (see
    # _should_log_regulate).
    #
    # log_on=Delta(percent=...) is what they used to carry, and it is the wrong
    # trigger for any of them. The threshold is a percentage of the last logged
    # value, and regulate_error is a signed error that converges on zero by
    # design — as it settled, 2% of it fell below the sensor's own noise and it
    # logged every single poll. One device went from ~460 messages a day to
    # 30,697.
    regulate_setpoint_active = Tag(  # setpoint the loop is holding (sensor units)
        "number", default=None, live=True
    )
    regulate_output_rpm = Tag(  # RPM being commanded
        "number", default=None, live=True
    )
    regulate_error = Tag(  # setpoint - measured
        "number", default=None, live=True
    )
    regulate_measured = Tag(  # current sensor value
        "number", default=None, live=True
    )

    # Manual Start Now / Stop Now button enablement (greyed when not allowed).
    start_now_disabled = Tag("boolean", default=True, live=True)
    stop_now_disabled = Tag("boolean", default=True, live=True)
    # (The buttons keep fixed faces: sequence progress — the countdown, panel
    # prep, cranking, cooldown — is reported on the command itself via
    # ctx.progress(), which the site shows beside the button's spinner.)

    # --- Auto start/stop control ---
    # How starts/stops are auto-triggered ("manual" / "sensor" / "timer"), the
    # trigger variable and its setpoint are operator settings, so they live in
    # the ui_cmds aggregate on their controls rather than in tags here.
    # Which sub-section of each tab is shown, driven by the control-mode select.
    # Default "none": both Sensor and Timer sections hidden.
    start_sensor_hidden = Tag("boolean", default=True, live=True)
    start_timer_hidden = Tag("boolean", default=True, live=True)
    stop_sensor_hidden = Tag("boolean", default=True, live=True)
    stop_timer_hidden = Tag("boolean", default=True, live=True)

    # Per-sensor visibility flags (Regulate / Start / Stop setpoint sliders) are
    # created per configured sensor by setup() — see HIDDEN_TAG_PREFIXES.

    # --- UI visibility flags ---
    # Auto-start button / field visibility (driven from the state machine, so
    # they change during operation → live).
    # (Start Now / Stop Now stay visible and grey out instead — see
    # start_now_disabled / stop_now_disabled. Only Cancel Start is hidden
    # outright, since it means nothing outside the warning countdown.)
    cancel_btn_hidden = Tag("boolean", default=True, live=True)
    run_reason_hidden = Tag("boolean", default=True, live=True)

    async def setup(self):
        """Create one set of tags per configured sensor, and per configured mode.

        Both are lists, so how many tags this app needs isn't known until config
        is loaded. The framework calls this before resolving remote tags and
        before the UI is built, which is exactly the window for it.
        """
        # One state tag per configured digital output, keyed by pin so
        # re-ordering the list can't move one output's history onto another.
        for output in output_specs(self.config):
            self._add(output.tag_name, Tag("boolean", default=False, live=True))
        for mode in build_modes(self.config):
            self._add(
                mode.element_name("hidden"), Tag("boolean", default=True, live=True)
            )
        for letter in flex_sensor_letters(self.config):
            self._add(flex_value_tag(letter), Tag("number", default=None, live=True))
            self._add(flex_hidden_tag(letter), Tag("boolean", default=True, live=True))
        for spec in sensor_specs(self.config):
            if spec.is_analog:
                # Scaled reading. Logging is decided per-write in the application
                # (it needs the configured span), so there's no log_on here.
                self._add(spec.value_tag_name, Tag("number", default=None, live=True))
                # Raw mA / V for commissioning — live only, never logged.
                self._add(spec.raw_tag_name, Tag("number", default=None, live=True))
                self._add(spec.fault_tag_name, Tag("boolean", default=False, live=True))
            for prefix in HIDDEN_TAG_PREFIXES:
                self._add(
                    spec.hidden_tag_name(prefix),
                    Tag("boolean", default=True, live=True),
                )

    def _add(self, name: str, tag: Tag) -> None:
        """Declare a runtime tag, tolerating one that already exists.

        Sensor and mode identities are both de-duplicated where they are parsed
        (``sensors.sensor_specs`` / ``run_modes.build_modes``), so a collision
        here means a class-declared tag of the same name — worth a log line
        rather than taking the app down at startup.
        """
        try:
            self.add_tag(name, tag)
        except ValueError:
            log.warning("Sensor tag %r already exists — leaving it as declared.", name)
