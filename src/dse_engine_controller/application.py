import logging
import json
from datetime import datetime

from pydoover.docker import Application
from pydoover import ui

from .app_config import DseEngineControllerConfig
from .app_ui import DseEngineControllerUI
from .app_state import EngineState

log = logging.getLogger(__name__)


class DseEngineControllerApplication(Application):
    """
    DSE Engine Controller Application.

    Monitors and controls engine parameters including:
    - Engine RPM
    - Oil pressure
    - Coolant temperature
    - Battery voltage
    - Fuel level
    - Engine hours

    Provides start/stop control with proper sequencing and fault handling.
    """

    config: DseEngineControllerConfig
    loop_target_period = 1  # 1 second loop for responsive engine control

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.ui: DseEngineControllerUI = None
        self.state: EngineState = None
        self.engine_mode: str = "manual"

        # Engine parameters (read from simulator or real hardware)
        self.rpm: float = 0
        self.oil_pressure: float = 0
        self.coolant_temp: float = 0
        self.battery_voltage: float = 12.6
        self.fuel_level: float = 100
        self.engine_hours: float = 0

        # Fault tracking
        self.active_faults: list[str] = []

    async def setup(self):
        """Initialize UI, state machine, and resources."""
        self.ui = DseEngineControllerUI()
        self.state = EngineState(self)
        self.ui_manager.add_children(*self.ui.fetch())

        # Set display name from config
        display_name = self.config.display_name.value or "Engine Controller"
        self.ui_manager.set_display_name(display_name)

        # Initialize UI state
        self.ui.engine_status.update("Stopped")

        log.info(f"DSE Engine Controller initialized: {display_name}")

    async def main_loop(self):
        """Main application loop - read sensors, evaluate state, update UI."""
        # Read engine parameters from simulator or hardware
        await self._read_engine_parameters()

        # Check alarm conditions
        self._evaluate_alarms()

        # Evaluate state machine
        engine_running = self.rpm > 100
        fault_active = len(self.active_faults) > 0
        await self.state.evaluate_state(engine_running, fault_active)

        # Update UI
        self.ui.update_parameters(
            rpm=self.rpm,
            oil_pressure=self.oil_pressure,
            coolant_temp=self.coolant_temp,
            battery_voltage=self.battery_voltage,
            fuel_level=self.fuel_level,
            engine_hours=self.engine_hours,
        )

        # Update warning indicators
        self.ui.update_warnings(
            low_oil="low_oil_pressure" in self.active_faults,
            high_temp="high_coolant_temp" in self.active_faults,
            low_battery="low_battery_voltage" in self.active_faults,
            overspeed="overspeed" in self.active_faults,
        )

        # Show/hide fault reset button based on state
        self.ui.show_fault_reset(self.state.state == "fault")

        # Persist state to tags
        await self.set_tag("engine_state", self.state.state)
        await self.set_tag("engine_rpm", self.rpm)
        await self.set_tag("oil_pressure", self.oil_pressure)
        await self.set_tag("coolant_temp", self.coolant_temp)
        await self.set_tag("battery_voltage", self.battery_voltage)
        await self.set_tag("fuel_level", self.fuel_level)
        await self.set_tag("active_faults", self.active_faults)

        # Publish to data channel
        await self._publish_engine_data()

        log.debug(
            f"State: {self.state.state}, RPM: {self.rpm}, "
            f"Oil: {self.oil_pressure} PSI, Temp: {self.coolant_temp} C"
        )

    async def _read_engine_parameters(self):
        """Read engine parameters from simulator or hardware."""
        # Try to read from simulator app if configured
        sim_key = self.config.simulator_app_key.value
        if sim_key:
            self.rpm = self.get_tag("rpm", sim_key) or 0
            self.oil_pressure = self.get_tag("oil_pressure", sim_key) or 0
            self.coolant_temp = self.get_tag("coolant_temp", sim_key) or 0
            self.battery_voltage = self.get_tag("battery_voltage", sim_key) or 12.6
            self.fuel_level = self.get_tag("fuel_level", sim_key) or 100
            self.engine_hours = self.get_tag("engine_hours", sim_key) or 0
        else:
            # In real implementation, read from hardware/Modbus/CAN
            # For now, use stored values (modified by state machine simulation)
            pass

    def _evaluate_alarms(self):
        """Check engine parameters against alarm thresholds."""
        new_faults = []

        # Only check these alarms when engine is running
        if self.rpm > 100:
            # Low oil pressure (critical when running)
            if self.oil_pressure < self.config.low_oil_pressure_psi.value:
                new_faults.append("low_oil_pressure")

            # High coolant temperature
            if self.coolant_temp > self.config.high_coolant_temp_c.value:
                new_faults.append("high_coolant_temp")

            # Overspeed
            if self.rpm > self.config.overspeed_rpm.value:
                new_faults.append("overspeed")

        # Battery voltage - always check
        if self.battery_voltage < self.config.low_battery_voltage.value:
            new_faults.append("low_battery_voltage")
        elif self.battery_voltage > self.config.high_battery_voltage.value:
            new_faults.append("high_battery_voltage")

        # Log new faults
        for fault in new_faults:
            if fault not in self.active_faults:
                log.warning(f"Fault detected: {fault}")

        # Log cleared faults
        for fault in self.active_faults:
            if fault not in new_faults:
                log.info(f"Fault cleared: {fault}")

        self.active_faults = new_faults

    async def _publish_engine_data(self):
        """Publish engine data to channel for logging."""
        data = {
            "timestamp": datetime.now().isoformat(),
            "state": self.state.state,
            "rpm": self.rpm,
            "oil_pressure": self.oil_pressure,
            "coolant_temp": self.coolant_temp,
            "battery_voltage": self.battery_voltage,
            "fuel_level": self.fuel_level,
            "engine_hours": self.engine_hours,
            "faults": self.active_faults,
        }
        await self.publish_to_channel("engine_data", json.dumps(data))

    # UI Callbacks

    @ui.callback("start_engine")
    async def on_start_engine(self, new_value):
        """Handle start engine button press."""
        log.info("Start engine requested")

        if self.state.state == "stopped":
            await self.state.start_request()
            await self.ui.alerts.send_alert("Engine start sequence initiated")
        else:
            log.warning(f"Cannot start engine from state: {self.state.state}")

        self.ui.start_engine.coerce(None)

    @ui.callback("stop_engine")
    async def on_stop_engine(self, new_value):
        """Handle stop engine button press."""
        log.info("Stop engine requested")

        if self.state.state == "running":
            await self.state.stop_request()
            await self.ui.alerts.send_alert("Engine stop sequence initiated")
        else:
            log.warning(f"Cannot stop engine from state: {self.state.state}")

        self.ui.stop_engine.coerce(None)

    @ui.callback("emergency_stop")
    async def on_emergency_stop(self, new_value):
        """Handle emergency stop button press."""
        log.warning("EMERGENCY STOP activated!")

        await self.state.emergency_stop()
        await self.ui.alerts.send_alert("EMERGENCY STOP ACTIVATED!")

        self.ui.emergency_stop.coerce(None)

    @ui.callback("reset_fault")
    async def on_reset_fault(self, new_value):
        """Handle fault reset button press."""
        log.info("Fault reset requested")

        if self.state.state == "fault":
            self.active_faults = []
            await self.state.reset_fault()
            await self.ui.alerts.send_alert("Fault reset - engine ready")
        else:
            log.warning("No fault to reset")

        self.ui.reset_fault.coerce(None)

    @ui.callback("engine_mode")
    async def on_engine_mode_change(self, new_value):
        """Handle engine mode change."""
        log.info(f"Engine mode changed to: {new_value}")
        self.engine_mode = new_value

        if new_value == "off" and self.state.state == "running":
            await self.state.stop_request()
        elif new_value == "auto":
            # Auto mode would start based on external conditions
            # (e.g., load demand, schedule, etc.)
            pass
