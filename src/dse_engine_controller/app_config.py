from pathlib import Path

from pydoover import config


class DseEngineControllerConfig(config.Schema):
    def __init__(self):
        self.outputs_enabled = config.Boolean("Digital Outputs Enabled", default=True)
        self.funny_message = config.String("A Funny Message")  # this will be required as no default given.

        self.sim_app_key = config.Application("Simulator App Key", description="The app key for the simulator")


def export():
    DseEngineControllerConfig().export(Path(__file__).parents[2] / "doover_config.json", "dse_engine_controller")

if __name__ == "__main__":
    export()
