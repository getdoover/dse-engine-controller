import random
import time

from pydoover.docker import Application, run_app
from pydoover.config import Schema


class EngineSimulator(Application):
    """
    Simulates engine sensor data for testing the DSE Engine Controller.

    Provides realistic engine parameters that vary based on simulated engine state.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.engine_running = False
        self.engine_hours = 1234.5
        self.start_time = time.time()

    async def setup(self):
        pass

    async def main_loop(self):
        # Simulate engine state based on external control
        # In a real scenario, this would respond to start/stop commands
        run_command = self.get_tag("run_command")
        if run_command == "start":
            self.engine_running = True
        elif run_command == "stop":
            self.engine_running = False

        # Generate simulated engine data
        if self.engine_running:
            # Running engine parameters
            rpm = 1500 + random.uniform(-50, 50)
            oil_pressure = 40 + random.uniform(-5, 5)
            coolant_temp = 85 + random.uniform(-3, 3)
            battery_voltage = 14.2 + random.uniform(-0.2, 0.2)
            fuel_level = max(0, 75 - (time.time() - self.start_time) / 3600 * 5)  # Slow consumption
            self.engine_hours += 1 / 3600  # Add 1 second of runtime
        else:
            # Stopped engine parameters
            rpm = 0
            oil_pressure = 0
            coolant_temp = 25 + random.uniform(-2, 2)
            battery_voltage = 12.6 + random.uniform(-0.1, 0.1)
            fuel_level = 75

        # Publish simulated sensor values as tags
        await self.set_tag("rpm", round(rpm, 1))
        await self.set_tag("oil_pressure", round(oil_pressure, 1))
        await self.set_tag("coolant_temp", round(coolant_temp, 1))
        await self.set_tag("battery_voltage", round(battery_voltage, 2))
        await self.set_tag("fuel_level", round(fuel_level, 0))
        await self.set_tag("engine_hours", round(self.engine_hours, 1))
        await self.set_tag("engine_running", self.engine_running)


def main():
    """Run the engine simulator application."""
    run_app(EngineSimulator(config=Schema()))


if __name__ == "__main__":
    main()
