from pathlib import Path

from pydoover import config


class DseEngineControllerConfig(config.Schema):
    """Configuration schema for DSE Engine Controller."""

    def __init__(self):
        # Display settings
        self.display_name = config.String(
            "Display Name",
            description="Name shown in the UI",
            default="Engine Controller"
        )

        # Engine control settings
        self.auto_start_enabled = config.Boolean(
            "Auto Start Enabled",
            description="Allow automatic engine starting based on conditions",
            default=False
        )

        self.crank_time_seconds = config.Integer(
            "Crank Time (seconds)",
            description="Maximum time to crank before timeout",
            default=10
        )

        self.crank_rest_seconds = config.Integer(
            "Crank Rest Time (seconds)",
            description="Rest time between crank attempts",
            default=5
        )

        self.max_crank_attempts = config.Integer(
            "Max Crank Attempts",
            description="Maximum number of crank attempts before failure",
            default=3
        )

        self.cooldown_time_seconds = config.Integer(
            "Cooldown Time (seconds)",
            description="Engine cooldown period before shutdown",
            default=60
        )

        # Alarm thresholds
        self.low_oil_pressure_psi = config.Number(
            "Low Oil Pressure (PSI)",
            description="Oil pressure threshold for low warning",
            default=15.0
        )

        self.high_coolant_temp_c = config.Number(
            "High Coolant Temp (C)",
            description="Coolant temperature threshold for high warning",
            default=95.0
        )

        self.low_battery_voltage = config.Number(
            "Low Battery Voltage (V)",
            description="Battery voltage threshold for low warning",
            default=11.5
        )

        self.high_battery_voltage = config.Number(
            "High Battery Voltage (V)",
            description="Battery voltage threshold for high warning",
            default=14.5
        )

        self.overspeed_rpm = config.Integer(
            "Overspeed RPM",
            description="RPM threshold for overspeed shutdown",
            default=2000
        )

        self.underspeed_rpm = config.Integer(
            "Underspeed RPM",
            description="RPM threshold for underspeed warning",
            default=1400
        )

        # Data source
        self.simulator_app_key = config.Application(
            "Simulator App Key",
            description="App key for engine data simulator (for testing)"
        )

    @property
    def crank_time_ms(self) -> int:
        """Crank time in milliseconds."""
        return self.crank_time_seconds.value * 1000

    @property
    def cooldown_time_ms(self) -> int:
        """Cooldown time in milliseconds."""
        return self.cooldown_time_seconds.value * 1000


def export():
    """Export configuration schema to doover_config.json."""
    DseEngineControllerConfig().export(
        Path(__file__).parents[2] / "doover_config.json",
        "dse_engine_controller"
    )


if __name__ == "__main__":
    export()
