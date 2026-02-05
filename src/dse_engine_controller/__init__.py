from pydoover.docker import run_app

from .application import DseEngineControllerApplication
from .app_config import DseEngineControllerConfig

def main():
    """
    Run the application.
    """
    run_app(DseEngineControllerApplication(config=DseEngineControllerConfig()))
