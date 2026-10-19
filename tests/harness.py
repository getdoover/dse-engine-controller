"""A DSE engine controller driven through whole runs, faked only at its edges.

The pure pieces — the auto-start machine, the regulator, the run-mode sequencer,
the analog scaling — are well covered by their own unit tests. What those can't
show is the *composition*: that pressing Start really does sound the horn, prepare
the panel, crank it, hold the configured RPM, move through the run modes and stop
again, in that order, with the right thing on the dashboard at each step.

So this builds a real :class:`DseEngineControllerApplication` — every method is the
production one — and replaces only what touches the outside world:

* the modbus interface (writes are recorded, reads come from a snapshot the test
  sets),
* the platform interface (digital outputs and analog inputs),
* tags, ui_cmds and global tags (dict-backed),
* the speaker RPC and activity-log messages.

Time is faked in both modules that read it, so a test can step a thirty-second
pre-start warning in a millisecond and assert exactly when things happened.

Nothing here mocks a method under test. If a test asserts a START was written,
the real ``_run_autostart`` → ``_maybe_send_start`` → ``_send_command`` →
``_write_register`` path ran to produce it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import dse_engine_controller.application as app_module
import dse_engine_controller.autostart as autostart_module
from dse_engine_controller import gencomm as gc
from dse_engine_controller.app_config import DseEngineControllerConfig
from dse_engine_controller.application import DseEngineControllerApplication
from dse_engine_controller.dse_state import DseModel, DseSnapshot

# The register addresses and command values the tests assert on, named the way
# the Kensho-era tests named them so the scenarios read the same.
REG_CONTROL_KEY = gc.address(gc.PAGE_CONTROL, gc.P16_CONTROL_KEY)
REG_REMOTE_RPM = gc.address(gc.PAGE_SPEED_CONTROL, gc.P137_TARGET_SPEED)
REG_SETPOINT = gc.address(gc.PAGE_SPEED_CONTROL, gc.P137_SET_POINT)
CMD_START = int(gc.ControlKey.TELEMETRY_START)
CMD_STOP = int(gc.ControlKey.CANCEL_TELEMETRY_START)
CMD_STOP_MODE = int(gc.ControlKey.SELECT_STOP)
CMD_RESET = int(gc.ControlKey.RESET_ALL_ALARMS)
CMD_AUTO = int(gc.ControlKey.SELECT_AUTO)


_UNSET = object()


def run(coro):
    """Drive one coroutine to completion. The app is async; these tests are not.

    The loop is closed rather than left for the collector — an unclosed one
    surfaces later as an unraisable-exception warning against whichever test
    happened to trigger the collection, which is a miserable thing to debug.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeClock:
    """One clock standing in for ``time.time`` and ``time.monotonic`` in both
    modules that read them, so a test can step through a run deterministically."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = float(start)
        self._patched: list[tuple] = []

    def install(self):
        stub = SimpleNamespace(
            time=lambda: self.now,
            monotonic=lambda: self.now,
            sleep=lambda _s: None,
        )
        for module in (app_module, autostart_module):
            self._patched.append((module, module.time))
            module.time = stub
        return self

    def restore(self):
        for module, original in self._patched:
            module.time = original
        self._patched.clear()

    def advance(self, seconds: float):
        self.now += float(seconds)


class Tag:
    """A tag that records every value it was given, and whether it was logged."""

    def __init__(self, value=None):
        self._value = value
        self.history = []
        self.logged = []

    def get(self):
        return self._value

    async def set(self, value, log=False):
        self._value = value
        self.history.append(value)
        if log:
            self.logged.append(value)


class Tags:
    """Auto-vivifying tag store.

    The app creates tags per configured sensor and per configured run mode, so
    which ones exist depends on the config a test supplies. Rather than enumerate
    them, any name asked for springs into being — a missing tag would otherwise
    fail as an AttributeError in the middle of a sequence and say nothing useful.
    """

    def __init__(self, **initial):
        object.__setattr__(self, "_tags", {})
        for name, value in initial.items():
            self._tags[name] = Tag(value)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._tags.setdefault(name, Tag())

    def names(self):
        return sorted(self._tags)


class Commands:
    """The ui_cmds aggregate: what the operator's controls hold."""

    def __init__(self, **values):
        self.values = dict(values)
        self.sent = []

    async def set_value(self, key, value, log_update=True):
        self.values[key] = value
        self.sent.append((key, value))

    def get_value(self, key):
        return self.values.get(key)


class Panel:
    """The DSE's answer to a poll, and a record of what was written to it.

    Defaults to a healthy module sitting stopped in Auto — the state a pump
    waits in between runs — so a test only spells out the part it cares about.
    The snapshot is a real :class:`DseSnapshot` decoded from a register image
    this builds, so what the app sees went through the real decoder.
    """

    def __init__(self, **kwargs):
        self.reachable = True
        self.is_running = False
        self.is_auto = True
        self.is_manual = False  # not Auto and not Stop: the fascia's Manual mode
        self.is_faulted = False
        self.is_warming_up = False
        self.is_cooling_down = False
        self.has_common_alarm = False
        self.fault_string = "Low oil pressure"  # the alarm raised when faulted
        self.panel_locked = False
        self.engine_rpm = None  # None → 1500 when running, 0 when stopped
        self.remote_rpm_setpoint = None  # page 137 target speed echo
        # Maintain Value loop: absent unless a test configures it.
        self.maintain_available = False
        self.maintain_setpoint_raw = 250  # 2.50 bar with multiplier -2
        self.maintain_min_raw = 0
        self.maintain_max_raw = 1000
        self.maintain_value_raw = 150
        self.maintain_multiplier = -2
        self.maintain_precision = 2
        self.writes: list[tuple[int, list[int]]] = []
        for key, value in kwargs.items():
            setattr(self, key, value)

    # --- what the decoder produces from the register image ---

    @property
    def display_state(self) -> str:
        return self.snapshot().display_state

    def image(self) -> dict[int, int]:
        img: dict[int, int] = {}

        def put(page, offset, word):
            img[gc.address(page, offset)] = int(word) & 0xFFFF

        # Page 3: identity, mode, flags.
        for offset, word in ((0, 1), (1, 400), (2, 0), (3, 4242), (5, 0xFFFF), (7, 0)):
            put(gc.PAGE_STATUS, offset, word)
        mode = (
            gc.ControlMode.AUTO
            if self.is_auto
            else gc.ControlMode.MANUAL if self.is_manual else gc.ControlMode.STOP
        )
        put(gc.PAGE_STATUS, gc.P3_CONTROL_MODE, mode)
        flags = 0
        if self.is_faulted:
            flags |= 1 << gc.FLAG_SHUTDOWN
        put(gc.PAGE_STATUS, gc.P3_ALARM_FLAGS, flags)
        put(gc.PAGE_STATUS, gc.P3_PANEL_LOCK, (1 << gc.LOCK_EXTERNAL) if self.panel_locked else 0)

        # Page 4: basic instrumentation.
        rpm = self.engine_rpm
        if rpm is None:
            rpm = 1500 if self.is_running else 0
        basic = (
            (0, 350 if self.is_running else 0),
            (1, 80),
            (2, 0x7FFF),
            (3, 75),
            (4, 141 if self.is_running else 0),
            (5, 128),
        )
        for offset, word in basic:
            put(gc.PAGE_BASIC, offset, word)
        put(gc.PAGE_BASIC, gc.P4_ENGINE_SPEED, int(rpm))

        # Page 5: operating state.
        if self.is_running:
            state = (
                gc.EngineOperatingState.WARMING_UP
                if self.is_warming_up
                else gc.EngineOperatingState.COOLING_DOWN
                if self.is_cooling_down
                else gc.EngineOperatingState.RUNNING
            )
        else:
            state = gc.EngineOperatingState.STOPPED
        put(gc.PAGE_EXTENDED, gc.P5_ENGINE_OPERATING_STATE, state)

        # Page 154: named alarms, all inactive unless faulted / alarmed.
        words = [0x1111] * gc.NAMED_ALARM_REGISTERS
        if self.is_faulted or self.has_common_alarm:
            condition = (
                gc.AlarmCondition.SHUTDOWN if self.is_faulted else gc.AlarmCondition.WARNING
            )
            index = (
                gc.NAMED_ALARMS.index(self.fault_string)
                if self.fault_string in gc.NAMED_ALARMS
                else 1
            )
            reg, nibble = divmod(index, 4)
            shift = 12 - 4 * nibble
            words[reg] = (words[reg] & ~(0xF << shift)) | (int(condition) << shift)
        put(gc.PAGE_NAMED_ALARMS, 0, len(gc.NAMED_ALARMS))
        for i, word in enumerate(words):
            put(gc.PAGE_NAMED_ALARMS, i + 1, word)

        # Page 137: target speed echo and the Maintain Value block.
        p = gc.PAGE_SPEED_CONTROL
        put(
            p,
            gc.P137_TARGET_SPEED,
            0xFFFF if self.remote_rpm_setpoint is None else int(self.remote_rpm_setpoint),
        )
        for offset in (gc.P137_P_GAIN, gc.P137_I_GAIN, gc.P137_D_GAIN):
            put(p, offset, 10)
        if self.maintain_available:
            put(p, gc.P137_SET_POINT, gc.to_s16(self.maintain_setpoint_raw))
            put(p, gc.P137_SET_POINT_MIN, gc.to_s16(self.maintain_min_raw))
            put(p, gc.P137_SET_POINT_MAX, gc.to_s16(self.maintain_max_raw))
            put(p, gc.P137_CONTROL_SOURCE_VALUE, gc.to_s16(self.maintain_value_raw))
            put(p, gc.P137_CONTROL_SOURCE_UNIT_ID, gc.UnitId.PRESSURE)
            put(p, gc.P137_CONTROL_SOURCE_MULTIPLIER, gc.to_s16(self.maintain_multiplier))
            put(p, gc.P137_CONTROL_SOURCE_PRECISION, self.maintain_precision)
            put(p, gc.P137_CONTROL_SOURCE_RANGE_MIN, 0)
            put(p, gc.P137_CONTROL_SOURCE_RANGE_MAX, 1000)
            for i, word in enumerate([ord("b"), ord("a"), ord("r")] + [0x20] * 7):
                put(p, gc.P137_CONTROL_SOURCE_SUFFIX + i, word)
            put(p, gc.P137_DISPLAY_UNITS, 1)
            put(p, gc.P137_SET_POINT_TARGET_SOURCE, 0)
        else:
            for offset in range(gc.P137_SET_POINT, gc.P137_SET_POINT_TARGET_SOURCE + 1):
                unsigned = offset in (
                    gc.P137_CONTROL_SOURCE_UNIT_ID,
                    gc.P137_CONTROL_SOURCE_PRECISION,
                    gc.P137_DISPLAY_UNITS,
                    gc.P137_SET_POINT_TARGET_SOURCE,
                )
                put(p, offset, 0xFFFF if unsigned else 0x7FFF)
        return img

    def snapshot(self):
        if not self.reachable:
            return None
        return DseSnapshot(self.image(), 0.0, DseModel.E400)

    # --- convenience for tests ---

    def written(self, register: int) -> list[int]:
        """Every (first-word) value written to one register, oldest first."""
        return [values[0] for r, values in self.writes if r == register]

    def commanded(self) -> list[int]:
        """The control keys written — START / STOP / RESET / AUTO.

        Excludes the target speed and set point, which are settings rather
        than instructions to do something.
        """
        return [values[0] for r, values in self.writes if r == REG_CONTROL_KEY]


class Store:
    """Stands in for DseStateStore, driven by a Panel."""

    def __init__(self, panel: Panel):
        self.panel = panel
        self.snapshots = []
        self.lost_comms = False

    def poll(self):
        snap = self.panel.snapshot()
        if snap is not None:
            self.snapshots.append(snap)

    @property
    def latest(self):
        return self.snapshots[-1] if self.snapshots else None

    @property
    def is_contactable(self) -> bool:
        return self.panel.reachable

    def has_lost_comms(self, timeout_seconds: float = 15.0, min_iters: int = 3):
        return self.lost_comms or not self.panel.reachable


DEFAULT_CONFIG = {
    "dse_model": "E400",
    "modbus_server_id": 10,
    "poll_period_seconds": 2,
    "slow_poll_period_seconds": 10,
    "enable_remote_control": True,
    "prestart_warning_seconds": 30,
    "start_timeout_seconds": 90,
    "stop_timeout_seconds": 120,
    "target_rpm_minimum": 900,
    "target_rpm_maximum": 2000,
    "beacon_output_pin": 3,
    "horn_output_pin": 2,
    "enable_announcements": False,
    "enable_remote_tags": False,
    "analog_inputs": [],
    "sensor_fault_action": "Hold",
    "sensor_fault_grace_seconds": 30,
    "start_trigger_direction": "Below",
    "stop_trigger_direction": "Above",
    "regulate_sensitivity_minimum": 0.1,
    "regulate_sensitivity_maximum": 10,
    "modbus_config": {
        "name": "default",
        "tcp_uri": "127.0.0.1:5000",
        "bus_type": "serial",
        "serial_baud": 9600,
        "serial_port": "/dev/ttyAMA0",
        "tcp_timeout": 2,
        "serial_method": "rtu",
        "serial_parity": "None",
        "serial_timeout": 0.3,
        "serial_data_bits": 8,
        "serial_stop_bits": 1,
    },
}


def build_config(**overrides) -> DseEngineControllerConfig:
    """A real config schema loaded the way the app loads one at boot."""
    data = dict(DEFAULT_CONFIG)
    data.update(overrides)
    config = DseEngineControllerConfig()
    config._inject_deployment_config(data)
    return config


class Rig:
    """A real application wired to fakes, plus the levers a test needs."""

    def __init__(self, config=None, panel=None, clock=None, stored=None,
                 **config_overrides):
        """``stored`` is what a previous process left in ui_cmds — the operator's
        settings and the mode it was last in. Applied before anything is seeded,
        which is when a real app reads them."""
        from dse_engine_controller.run_modes import validate

        self.clock = (clock or FakeClock()).install()
        self.config = config or build_config(**config_overrides)
        self.panel = panel or Panel()
        self.store = Store(self.panel)
        self.tags = Tags()
        self.cmds = Commands(**(stored or {}))
        self.messages: list[str] = []
        self.spoken: list[dict] = []
        self.outputs: list[tuple[int, bool]] = []
        self.globals: dict = {}
        self.analog_readings: dict[int, float | None] = {}
        self.io_details = _UNSET  # a normal doovit unless a test says otherwise

        app = DseEngineControllerApplication.__new__(DseEngineControllerApplication)
        self.app = app
        app.config = self.config
        app.tags = self.tags
        app.store = self.store
        app.ui_manager = self.cmds
        app.app_display_name = "Pump 1"
        app.loop_target_period = float(self.config.poll_period.value)

        app.app_key = APP_KEY
        app.platform_iface = SimpleNamespace(
            set_do=self._set_do,
            fetch_ai=self._fetch_ai,
            fetch_io_details=self._fetch_io_details,
        )
        app.tag_manager = _TagManager()
        app.modbus_iface = SimpleNamespace(write_registers=self._write_registers)
        app.rpc = SimpleNamespace(call=self._rpc_call)
        app.create_message = self._create_message
        app.get_global_tag = self.globals.get
        app.set_global_tag = self._set_global_tag
        app._wait_inter_write = self._noop  # the real one sleeps 600ms per write
        # The button handlers spin here reporting progress until the engine is
        # running or the attempt aborts. That spin is the *reporting*, not the
        # sequence — the latch is set before it, and the loop is what advances
        # things. Waiting in it would deadlock a harness that ticks by hand.
        app._await_until = self._await_until
        self.awaited: list[float] = []
        # Same messages as `self.messages`, but keeping the channel they went to.
        self.channel_messages: list[tuple[str, str]] = []

        self.sequencer = None  # built in _init_state, once the sensors exist
        self._validate = validate

        self._init_state()

    # --- IO fakes -----------------------------------------------------

    async def _noop(self, *a, **kw):
        return None

    async def _await_until(
        self, predicate, *, timeout, ctx=None, progress=None, on_cancel=None
    ):
        self.awaited.append(timeout)
        if progress is not None:
            progress()  # exercise the progress text at least once
        # The real one polls; here the command is either already cancelled when
        # the handler runs or it isn't, which is enough to prove the wiring —
        # cancelling *mid-wait* is covered directly against the real
        # _await_until in test_pump_control.
        if ctx is not None and getattr(ctx, "cancelled", False) and on_cancel:
            await on_cancel(ctx)
        return bool(predicate())

    async def _set_do(self, pin, state):
        self.outputs.append((int(pin), bool(state)))

    async def _fetch_ai(self, *pins):
        values = [self.analog_readings.get(p) for p in pins]
        return values[0] if len(pins) == 1 else values

    async def _write_registers(self, *, start_address, values, **kwargs):
        self.panel.writes.append((int(start_address), [int(v) for v in values]))

    async def _rpc_call(self, method, payload, **kwargs):
        self.spoken.append({"method": method, **payload, **kwargs})

    async def _create_message(self, channel, payload):
        self.messages.append(payload.get("message", ""))
        self.channel_messages.append((channel, payload.get("message", "")))

    async def _set_global_tag(self, key, value):
        self.globals[key] = value

    async def _fetch_io_details(self):
        """A doovit's IO layout: AI 0-1 are 0-10 V, AI 2-5 are 4-20 mA."""
        if self.io_details is _UNSET:
            return SimpleNamespace(
                channels=lambda kind: [
                    SimpleNamespace(
                        channel=n,
                        kind="voltage" if n < 2 else "current",
                        units="V" if n < 2 else "mA",
                    )
                    for n in range(6)
                ]
            )
        return self.io_details

    # --- state the real setup() would have established ----------------

    def _init_state(self):
        from dse_engine_controller.autostart import AutoStartController
        from dse_engine_controller.regulator import RpmRegulator

        app = self.app
        app.controller_state = app_module.DseEngineControllerState()
        app.autostart = AutoStartController(
            warning_seconds=max(
                app_module.MIN_WARNING_SECONDS,
                float(self.config.prestart_warning_seconds.value),
            ),
            start_timeout=float(self.config.start_timeout_seconds.value),
            stop_timeout=float(self.config.stop_timeout_seconds.value),
        )
        app._operator_run_latch = False
        app._suppress_run_tag = False
        app._start_timer_fired_at = None
        app._stop_timer_fired_at = None
        app._last_start_cmd = 0.0
        app._last_stop_cmd = 0.0
        app._reset_issued = False
        app._last_reset_cmd = 0.0
        app._last_auto_cmd = 0.0
        app._last_beacon_out = None
        app._last_horn_out = None
        app._warn_local_start = {}
        app._pending_announcement = None
        app._last_prestart_spoken = 0.0
        app._pump_power_on = None
        app._last_pump_power_out = None
        app._power_startup_hold = True
        app._started_at = self.clock.now
        app._power_on_since = None
        app._run_request_active = False
        app._last_rpm_written = None
        app._last_rpm_assert = 0.0
        app._manual_while_running = False
        app._last_comms_loss = None
        app._run_streak = 0
        app._stop_streak = 0
        app._running_confirmed = False
        app._last_write_time = 0.0
        app.model = self.config.model.value
        app._register_image = {}
        app._last_slow_poll = 0.0
        app._startup_read_at = None
        app._startup_read_failed_at = 0.0
        app._supported_keys = None
        app._last_setpoint_written = None
        app._last_setpoint_assert = 0.0
        app._stop_started_at = None
        # Sensors, built from config the way setup() does, so a rig configured
        # with analog inputs can run a Regulate mode for real.
        from dse_engine_controller.analog import AnalogInput
        from dse_engine_controller.sensors import build_sensors

        app._sensors = build_sensors(self.config, self.tags)
        app._sensor_by_name = {s.sanitised: s for s in app._sensors}
        app._analog_slots = [s for s in app._sensors if s.is_analog]
        hold = max(
            app_module.ANALOG_HOLD_SECONDS,
            2 * float(self.config.poll_period.value),
        )
        app._analog = {
            s.sanitised: AnalogInput.from_slot(s, hold_seconds=hold)
            for s in app._analog_slots
        }
        app._last_analog_time = 0.0

        # Digital outputs, then the sequencer — the modes reference both, so
        # both have to exist first. Same order as the real setup().
        from dse_engine_controller.outputs import build_outputs
        from dse_engine_controller.run_modes import build_sequencer

        app._selected_mode = None
        app._outputs = build_outputs(self.config, self.tags)
        app._last_output_state = {}
        app._check_output_pins()
        self.sequencer = build_sequencer(self.config, app._sensors, app._outputs)
        app.sequencer = self.sequencer
        app._analog_config_problems = []
        app._analog_failed_pins = set()
        app._analog_logged = {}
        app._analog_logged_at = {}
        app._sensor_fault_stopped = False
        app.regulator = RpmRegulator(
            rpm_min=int(self.config.target_rpm_min.value),
            rpm_max=int(self.config.target_rpm_max.value),
        )
        app._last_regulate_time = 0.0
        app._regulate_logged = None
        app._regulate_logged_at = 0.0
        app._regulate_key = None

        # Config problems are found before anything is seeded, because seeding
        # publishes the mode status and that reports them. Same order as setup().
        app._run_mode_problems = self._validate(self.sequencer, app._sensors)

        # What setup() seeds: each mode's controls, and the active selection.
        run(app._seed_command_defaults())
        run(app._seed_run_mode())

    # --- driving ------------------------------------------------------

    def real_setup(self):
        """Run the production ``setup()``, then put the fake store back.

        The reconstruction in ``_init_state`` is what most tests want — it is
        quick and lets them start from any state. This is for the tests that are
        *about* setup: that it wires things in an order that works, seeds every
        control, and survives the configs a site might actually save.
        """
        run(self.app.setup())
        self.app.store = self.store
        self.sequencer = self.app.sequencer
        return self

    def tick(self, count: int = 1, seconds: float | None = None):
        """Run the main loop, minus the modbus read (the Panel is the panel)."""
        step = self.config.poll_period.value if seconds is None else seconds
        for _ in range(count):
            run(self.app._poll_analog_inputs())
            self.store.poll()
            run(self.app._update_state_and_tags())
            if self.app.autostart is not None:
                run(self.app._evaluate_auto_control())
                run(self.app._run_autostart())
            run(self.app._drive_pump_power())
            run(self.app._drive_outputs())
            self.clock.advance(step)

    def run_for(self, seconds: float):
        """Tick until *seconds* of pump time have passed."""
        self.tick(count=max(1, int(seconds / self.config.poll_period.value)))

    def set_analog(self, pin: int, milliamps: float | None):
        """What a 4-20 mA loop is reading, in mA at the input."""
        self.analog_readings[pin] = milliamps

    def press_start(self):
        run(self.app.on_start_now(_Ctx(), None))

    def press_stop(self):
        run(self.app.on_stop_now(_Ctx(), None))

    def press_cancel(self):
        run(self.app.on_cancel_start(_Ctx(), None))

    def cancel_start_command(self, by=None):
        """The operator cancels the in-flight Start Now command itself.

        The site's other cancel affordance: a patch to the command's own
        message rather than a new command, which reaches the handler through
        its context.
        """
        run(self.app._on_start_command_cancelled(_Ctx(cancelled=True, cancelled_by=by)))

    def messages_on(self, channel: str) -> list[str]:
        """Every message published to *channel* so far."""
        return [msg for name, msg in self.channel_messages if name == channel]

    def pick_mode(self, label: str):
        run(self.app.on_run_mode(_Ctx(), label))

    # --- reading the dashboard ---------------------------------------

    @property
    def state(self) -> str:
        return self.app.autostart.state

    @property
    def mode(self) -> str:
        return self.tags.run_mode_active.get()

    @property
    def status(self) -> str:
        return self.tags.status_string.get()

    def horn_on(self) -> bool:
        return bool(self.tags.horn_active.get())

    def beacon_on(self) -> bool:
        return bool(self.tags.beacon_active.get())

    def output(self, label: str) -> bool | None:
        """The published state of a configured digital output, by name."""
        from pydoover.utils.utils import sanitize_display_name as sanitise

        wanted = sanitise(label)
        for out in self.app._outputs:
            if out.sanitised == wanted:
                return out.tag.get()
        raise KeyError(label)

    def pin_history(self, pin: int) -> list[bool]:
        return [state for p, state in self.outputs if p == pin]

    def close(self):
        self.clock.restore()


#: The app namespace every aggregate in these tests is keyed under.
APP_KEY = "dse_engine_controller_1"


class _TagManager:
    """The tag aggregate, for the stale-key purge setup() runs.

    Namespaced by ``app_key``, because the real one is: ``tag_values`` holds
    ``{app_key: {tag: value}}`` and pydoover's ``KeyPath`` reads
    ``app_key=None`` as *the root of the aggregate*, not *this app*. A fake
    that ignored the argument let a purge which never passed it go on passing
    its test while clearing nothing on a real device — for weeks. Writes that
    name no app land in ``root`` where nothing will ever read them, which is
    exactly what they do in the field.
    """

    def __init__(self, app_key=APP_KEY, **initial):
        self.app_key = app_key
        self.tags = dict(initial)
        self.root = {}
        self.cleared = []

    def _namespace(self, app_key):
        return self.tags if app_key == self.app_key else self.root

    def get_tag(self, key, default=None, app_key=None, raise_key_error=False):
        return self._namespace(app_key).get(key, default)

    async def set_tag(self, key, value, app_key=None, **kwargs):
        self._namespace(app_key)[key] = value
        if value is None and app_key == self.app_key:
            self.cleared.append(key)


class _Ctx:
    """A ui_cmd context.

    Mirrors the parts of ``pydoover.rpc.RPCContext`` the handlers touch: they
    acknowledge, report progress, read ``.element``, and — since the site lets
    an operator cancel a command that is still in flight — ask whether the
    command they are serving has been withdrawn.
    """

    def __init__(self, name: str = "", cancelled: bool = False, cancelled_by=None):
        self.element = SimpleNamespace(name=name)
        self.cancelled = cancelled
        self.cancelled_by = cancelled_by
        self.actor = None
        self.progress_texts: list[str] = []

    async def acknowledge(self, *a, **kw):
        return None

    async def progress(self, text=None, **kw):
        self.progress_texts.append(text)
        return None
