from datetime import datetime

from pydoover import ui


class DseEngineControllerUI:
    """
    UI components for DSE Engine Controller.

    Displays engine parameters, status, and control actions.
    """

    def __init__(self):
        # Engine Status Section
        self.engine_status = ui.TextVariable(
            "engine_status",
            "Engine Status",
        )

        # Engine Parameters - RPM
        self.engine_rpm = ui.NumericVariable(
            "engine_rpm",
            "Engine RPM",
            precision=0,
            unit="RPM",
            ranges=[
                ui.Range("Stopped", 0, 100, ui.Colour.grey),
                ui.Range("Cranking", 100, 500, ui.Colour.yellow),
                ui.Range("Underspeed", 500, 1400, ui.Colour.orange),
                ui.Range("Normal", 1400, 1600, ui.Colour.green),
                ui.Range("High", 1600, 2000, ui.Colour.yellow),
                ui.Range("Overspeed", 2000, 3000, ui.Colour.red),
            ]
        )

        # Engine Hours
        self.engine_hours = ui.NumericVariable(
            "engine_hours",
            "Engine Hours",
            precision=1,
            unit="hrs",
        )

        # Oil Pressure
        self.oil_pressure = ui.NumericVariable(
            "oil_pressure",
            "Oil Pressure",
            precision=1,
            unit="PSI",
            ranges=[
                ui.Range("Low", 0, 15, ui.Colour.red),
                ui.Range("Normal", 15, 60, ui.Colour.green),
                ui.Range("High", 60, 100, ui.Colour.yellow),
            ]
        )

        # Coolant Temperature
        self.coolant_temp = ui.NumericVariable(
            "coolant_temp",
            "Coolant Temperature",
            precision=1,
            unit="C",
            ranges=[
                ui.Range("Cold", 0, 40, ui.Colour.blue),
                ui.Range("Warming", 40, 70, ui.Colour.yellow),
                ui.Range("Normal", 70, 95, ui.Colour.green),
                ui.Range("High", 95, 110, ui.Colour.red),
            ]
        )

        # Battery Voltage
        self.battery_voltage = ui.NumericVariable(
            "battery_voltage",
            "Battery Voltage",
            precision=2,
            unit="V",
            ranges=[
                ui.Range("Low", 0, 11.5, ui.Colour.red),
                ui.Range("Normal", 11.5, 14.5, ui.Colour.green),
                ui.Range("High", 14.5, 16, ui.Colour.yellow),
            ]
        )

        # Fuel Level
        self.fuel_level = ui.NumericVariable(
            "fuel_level",
            "Fuel Level",
            precision=0,
            unit="%",
            ranges=[
                ui.Range("Critical", 0, 10, ui.Colour.red),
                ui.Range("Low", 10, 25, ui.Colour.orange),
                ui.Range("Normal", 25, 100, ui.Colour.green),
            ]
        )

        # Last Update Time
        self.last_update = ui.DateTimeVariable(
            "last_update",
            "Last Update",
        )

        # Warning Indicators
        self.low_oil_warning = ui.WarningIndicator(
            "low_oil_warning",
            "Low Oil Pressure",
            hidden=True,
        )

        self.high_temp_warning = ui.WarningIndicator(
            "high_temp_warning",
            "High Coolant Temperature",
            hidden=True,
        )

        self.low_battery_warning = ui.WarningIndicator(
            "low_battery_warning",
            "Low Battery Voltage",
            hidden=True,
        )

        self.overspeed_warning = ui.WarningIndicator(
            "overspeed_warning",
            "Engine Overspeed",
            hidden=True,
        )

        # Control Actions
        self.start_engine = ui.Action(
            "start_engine",
            "Start Engine",
            colour=ui.Colour.green,
            position=1,
        )

        self.stop_engine = ui.Action(
            "stop_engine",
            "Stop Engine",
            colour=ui.Colour.yellow,
            position=2,
        )

        self.emergency_stop = ui.Action(
            "emergency_stop",
            "Emergency Stop",
            colour=ui.Colour.red,
            requires_confirm=True,
            position=3,
        )

        self.reset_fault = ui.Action(
            "reset_fault",
            "Reset Fault",
            colour=ui.Colour.blue,
            position=4,
            hidden=True,
        )

        # Engine Mode Command
        self.engine_mode = ui.StateCommand(
            "engine_mode",
            "Engine Mode",
            user_options=[
                ui.Option("manual", "Manual"),
                ui.Option("auto", "Auto"),
                ui.Option("off", "Off"),
            ]
        )

        # Alert stream for notifications
        self.alerts = ui.AlertStream()

    def fetch(self):
        """Return all UI components to be registered."""
        return (
            # Status
            self.engine_status,
            # Parameters
            self.engine_rpm,
            self.engine_hours,
            self.oil_pressure,
            self.coolant_temp,
            self.battery_voltage,
            self.fuel_level,
            self.last_update,
            # Warnings
            self.low_oil_warning,
            self.high_temp_warning,
            self.low_battery_warning,
            self.overspeed_warning,
            # Controls
            self.start_engine,
            self.stop_engine,
            self.emergency_stop,
            self.reset_fault,
            self.engine_mode,
            # Alerts
            self.alerts,
        )

    def update_parameters(
        self,
        rpm: float = 0,
        oil_pressure: float = 0,
        coolant_temp: float = 0,
        battery_voltage: float = 0,
        fuel_level: float = 0,
        engine_hours: float = 0,
    ):
        """Update all engine parameters."""
        self.engine_rpm.update(rpm)
        self.oil_pressure.update(oil_pressure)
        self.coolant_temp.update(coolant_temp)
        self.battery_voltage.update(battery_voltage)
        self.fuel_level.update(fuel_level)
        self.engine_hours.update(engine_hours)
        self.last_update.update(datetime.now())

    def update_warnings(
        self,
        low_oil: bool = False,
        high_temp: bool = False,
        low_battery: bool = False,
        overspeed: bool = False,
    ):
        """Update warning indicator visibility."""
        self.low_oil_warning.set_hidden(not low_oil)
        self.high_temp_warning.set_hidden(not high_temp)
        self.low_battery_warning.set_hidden(not low_battery)
        self.overspeed_warning.set_hidden(not overspeed)

    def show_fault_reset(self, show: bool = True):
        """Show or hide the fault reset button."""
        self.reset_fault.set_hidden(not show)
