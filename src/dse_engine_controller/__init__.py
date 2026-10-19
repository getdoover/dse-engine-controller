from pydoover.docker import run_app

from .application import DseEngineControllerApplication


def main():
    """Run the application."""
    run_app(DseEngineControllerApplication())
