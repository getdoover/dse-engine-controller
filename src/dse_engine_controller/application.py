import logging
import time

from pydoover.docker import Application
from pydoover import ui

from .app_config import DseEngineControllerConfig
from .app_ui import DseEngineControllerUI
from .app_state import DseEngineControllerState

log = logging.getLogger()

class DseEngineControllerApplication(Application):
    config: DseEngineControllerConfig  # not necessary, but helps your IDE provide autocomplete!

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.started: float = time.time()
        self.ui: DseEngineControllerUI = None
        self.state: DseEngineControllerState = None

    async def setup(self):
        self.ui = DseEngineControllerUI()
        self.state = DseEngineControllerState()
        self.ui_manager.add_children(*self.ui.fetch())

    async def main_loop(self):
        log.info(f"State is: {self.state.state}")

        # a random value we set inside our simulator. Go check it out in simulators/sample!
        random_value = self.get_tag("random_value", self.config.sim_app_key.value)
        log.info("Random value from simulator: %s", random_value)

        self.ui.update(
            True,
            random_value,
            time.time() - self.started,
        )

    @ui.callback("send_alert")
    async def on_send_alert(self, new_value):
        log.info(f"Sending alert: {self.ui.test_output.current_value}")
        await self.publish_to_channel("significantAlerts", self.ui.test_output.current_value)
        self.ui.send_alert.coerce(None)

    @ui.callback("test_message")
    async def on_text_parameter_change(self, new_value):
        log.info(f"New value for test message: {new_value}")
        # Set the value as an output to the corresponding variable is this case
        self.ui.test_output.update(new_value)

    @ui.callback("charge_mode")
    async def on_state_command(self, new_value):
        log.info(f"New value for state command: {new_value}")
