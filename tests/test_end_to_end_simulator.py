"""The real application against the real simulator model, end to end.

Everything in between is production code: the app's tiered GenComm polling,
the decoder, the auto-start machine, the run modes and the command layer on
one side; the simulator's register image, control-key handling, engine
lifecycle and Maintain Value loop on the other. Only the Modbus transport is
replaced — reads and writes go straight to the model instead of over TCP —
and time is faked so a whole run takes milliseconds.

This is the structural check the unit tests can't give: that what the app
reads is what the simulator serves at those addresses, that the keys the app
writes are the keys the simulator (and, per GenComm, the module) acts on, and
that a start really does end with an engine turning at the speed we asked
for.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest

from dse_engine_controller import gencomm as gc
from dse_engine_controller.dse_state import DseModel, DseStateStore

from .harness import (
    CMD_AUTO,
    CMD_RESET,
    CMD_START,
    CMD_STOP,
    REG_CONTROL_KEY,
    REG_REMOTE_RPM,
    REG_SETPOINT,
    Rig,
    run,
)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "simulators" / "dse"))
from model import E400Model, Key, Mode, Phase, Timings  # noqa: E402

# Short lifecycle so a whole run fits in a few dozen polls.
TIMINGS = Timings(start_delay=2.0, crank=3.0, warmup=6.0, return_delay=2.0, cooldown=6.0)


class LiveRig:
    """A :class:`Rig` whose panel is an :class:`E400Model` instead of a stub."""

    def __init__(self, *, maintain: bool = False, **config_overrides):
        self.rig = Rig(**config_overrides)
        clock = self.rig.clock
        self.model = E400Model(
            clock=lambda: clock.now,
            wall_clock=lambda: clock.now,
            timings=TIMINGS,
        )
        self.model.maintain_enabled = maintain
        app = self.rig.app
        app.store = DseStateStore(model=DseModel.E400)
        self.writes: list[tuple[int, list[int]]] = []
        self.rejected: list[tuple[int, list[int]]] = []
        app.modbus_iface = SimpleNamespace(
            read_registers=self._read, write_registers=self._write
        )

    async def _read(self, *, start_address, num_registers, **kwargs):
        return self.model.read(int(start_address), int(num_registers))

    async def _write(self, *, start_address, values, **kwargs):
        values = [int(v) for v in values]
        self.writes.append((int(start_address), values))
        if not self.model.write(int(start_address), values):
            self.rejected.append((int(start_address), values))
            raise RuntimeError("Modbus exception 3 (illegal data value)")

    @property
    def app(self):
        return self.rig.app

    @property
    def step(self) -> float:
        return float(self.rig.config.poll_period.value)

    def tick(self, count: int = 1) -> None:
        """One main loop, the real one: poll the model, act, then let time pass."""
        app = self.app
        for _ in range(count):
            run(app._poll_analog_inputs())
            run(app._poll_modbus())
            run(app._update_state_and_tags())
            if app.autostart is not None:
                run(app._evaluate_auto_control())
                run(app._run_autostart())
            run(app._drive_pump_power())
            run(app._drive_outputs())
            self.rig.clock.advance(self.step)
            self.model.tick()

    def run_for(self, seconds: float) -> None:
        self.tick(max(1, int(seconds / self.step)))

    def commanded(self) -> list[int]:
        return [values[0] for address, values in self.writes if address == REG_CONTROL_KEY]

    def written(self, register: int) -> list[int]:
        return [values[0] for address, values in self.writes if address == register]

    def close(self):
        self.rig.close()


@pytest.fixture
def live():
    rigs = []

    def make(**kwargs):
        rig = LiveRig(**kwargs)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.close()


def test_the_app_reads_the_simulator_as_a_healthy_stopped_panel(live):
    lr = live()
    lr.tick(2)
    tags = lr.rig.tags
    assert tags.is_contactable.get() is True
    assert tags.control_mode.get() == "Stop"
    assert tags.is_running.get() is False
    assert tags.engine_rpm.get() == 0
    assert tags.battery_voltage.get() == pytest.approx(12.8, abs=0.3)
    assert tags.alarm_summary.get() == "None"
    assert lr.rig.state == "idle"
    assert lr.rig.status == "Pump 1: Stopped"
    # The slow tier ran on the first pass and the startup tier too.
    assert tags.total_engine_hours.get() == pytest.approx(125.0, abs=0.1)
    assert tags.number_of_starts.get() == 42
    assert "SELECT_AUTO" in tags.supported_keys.get()
    assert "TELEMETRY_START" in tags.supported_keys.get()


def test_a_full_start_run_stop_cycle_in_a_fixed_rpm_mode(live):
    lr = live(maintain=False)
    lr.tick(2)

    # Start: the warning runs, and the panel is put into Auto underneath it.
    lr.rig.press_start()
    lr.run_for(10)
    assert lr.rig.state == "prestart_warning"
    assert CMD_AUTO in lr.commanded()
    assert lr.model.mode is Mode.AUTO
    assert CMD_START not in lr.commanded()

    # Countdown over: the telemetry start goes, the sim cranks and catches.
    lr.run_for(30)
    assert CMD_START in lr.commanded()
    lr.run_for(20)
    assert lr.model.phase is Phase.RUNNING
    assert lr.rig.state == "running"
    assert lr.rig.tags.is_running.get() is True

    # The default Fixed RPM mode holds the configured minimum (900) — written
    # to page 137 offset 147, accepted by the sim, and the engine settles there.
    lr.run_for(20)
    assert lr.written(REG_REMOTE_RPM)[-1] == 900
    assert lr.model.target_speed == 900
    assert lr.model.rpm == pytest.approx(900, abs=5)
    assert lr.rig.tags.engine_rpm.get() == pytest.approx(900, abs=5)
    assert lr.rig.tags.target_speed_rpm.get() == 900
    assert lr.rejected == []

    # Stop: the start request is withdrawn and the sim winds itself down.
    lr.rig.press_stop()
    lr.run_for(4)
    assert CMD_STOP in lr.commanded()
    assert lr.model.phase in (Phase.RETURN_DELAY, Phase.COOLING_DOWN)
    assert lr.rig.state == "stopping"
    lr.run_for(20)
    assert lr.model.phase is Phase.STOPPED
    assert lr.rig.state == "idle"
    assert lr.model.mode is Mode.AUTO, "left in Auto for the next start"
    assert lr.rejected == []


def test_a_maintain_value_mode_moves_the_panels_set_point_not_its_speed(live):
    lr = live(
        maintain=True,
        run_modes=[
            {
                "label": "Panel Pressure",
                "control": "Maintain Value",
                "panel_setpoint": 3.0,
                "panel_setpoint_min": 0,
                "panel_setpoint_max": 8,
            }
        ],
        include_default_fixed_run_mode=False,
        start_in_mode="Panel Pressure",
    )
    lr.tick(2)
    assert lr.rig.tags.maintain_available.get() is True
    assert lr.rig.tags.maintain_setpoint.get() == pytest.approx(2.5)  # the sim's default

    lr.rig.press_start()
    lr.run_for(70)
    assert lr.rig.state == "running"
    assert lr.rig.mode == "Panel Pressure"

    lr.run_for(10)
    # The set point went to the panel as the raw word its unit info scales
    # (3.00 bar, multiplier -2 → 300); the target speed was left alone.
    assert lr.written(REG_SETPOINT) == [300]
    assert lr.model.set_point == 300
    assert lr.written(REG_REMOTE_RPM) == []
    assert lr.rig.tags.maintain_setpoint.get() == pytest.approx(3.0)
    assert lr.rig.tags.regulate_setpoint_active.get() == pytest.approx(3.0)

    # The panel's own loop takes it there; the app just watches.
    lr.run_for(60)
    assert lr.model.control_source == pytest.approx(300, rel=0.15)
    assert lr.rig.tags.maintain_value.get() == pytest.approx(3.0, abs=0.5)
    assert lr.rig.tags.regulate_status.get().startswith("Panel holding 3 bar")
    assert lr.written(REG_SETPOINT) == [300], "written once, not every poll"
    assert lr.rejected == []


def test_a_set_point_outside_the_panels_limits_is_clamped_to_them(live):
    lr = live(
        maintain=True,
        run_modes=[
            {
                "label": "Panel Pressure",
                "control": "Maintain Value",
                "panel_setpoint": 9.5,  # above the sim's 8.00 bar maximum
                "panel_setpoint_min": 0,
                "panel_setpoint_max": 10,
            }
        ],
        include_default_fixed_run_mode=False,
        start_in_mode="Panel Pressure",
    )
    lr.rig.press_start()
    lr.run_for(80)
    assert lr.rig.state == "running"
    assert lr.written(REG_SETPOINT) == [800]
    assert lr.rejected == []


def test_a_shutdown_alarm_faults_the_run_and_the_next_start_resets_it(live):
    lr = live(maintain=False)
    lr.rig.press_start()
    lr.run_for(70)
    assert lr.rig.state == "running"

    lr.model.inject_alarm("Low oil pressure", "shutdown")
    lr.run_for(6)
    assert lr.rig.tags.is_faulted.get() is True
    assert lr.rig.tags.fault_string.get() == "Low oil pressure"
    assert "Low oil pressure (Shutdown)" in lr.rig.tags.alarm_summary.get()
    assert lr.rig.state == "fault"
    assert lr.model.phase is Phase.STOPPED
    assert lr.rig.status == "Pump 1: Fault: Low oil pressure"

    # The cause goes away but the alarm stays latched — until the next start's
    # prep resets it, after which the start proceeds as normal.
    lr.model.clear_alarm("Low oil pressure")
    lr.tick(1)
    assert lr.rig.tags.is_faulted.get() is True
    lr.rig.press_start()
    lr.run_for(10)
    assert CMD_RESET in lr.commanded()
    assert lr.model.active_alarms() == {}
    assert lr.rig.tags.is_faulted.get() is False
    lr.run_for(60)
    assert lr.rig.state == "running"
    assert lr.rejected == []


def test_an_engine_started_at_the_fascia_is_adopted_and_stopped_via_stop_mode(live):
    """Cancelling a telemetry start can't stop an engine the panel started on
    its own in Manual, so the stop escalates to Stop mode."""
    from dse_engine_controller import application as app_module

    lr = live(maintain=False)
    lr.model.set_mode(Mode.MANUAL)
    lr.model.press_key(Key.START_ENGINE)  # the fascia Start button
    lr.run_for(20)
    assert lr.model.phase is Phase.RUNNING
    assert lr.rig.state == "running", "adopted, not re-warned"
    assert lr.rig.tags.manual_mode_hidden.get() is False
    assert CMD_AUTO not in lr.commanded(), "never changes mode under a running engine"

    lr.rig.press_stop()
    lr.run_for(6)
    assert lr.model.mode is Mode.STOP
    lr.run_for(10)
    assert lr.model.phase is Phase.STOPPED
    assert lr.rig.state == "idle"
    assert app_module.STOP_ESCALATION_S > 0  # the escalation also fires on time, not only on mode


def test_every_read_plan_block_is_served_without_a_short_read(live):
    """The read plan and the simulator agree on every address, so nothing the
    app asks for comes back short or raises."""
    lr = live()
    for block in gc.READ_PLAN:
        values = lr.model.read(block.address, block.count)
        assert len(values) == block.count, block
    lr.tick(1)
    assert lr.app.store.is_contactable
    assert lr.app._startup_read_at is not None
