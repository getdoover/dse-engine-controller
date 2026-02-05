"""
Basic tests for an application.

This ensures all modules are importable and that the config is valid.
"""

def test_import_app():
    from dse_engine_controller.application import DseEngineControllerApplication
    assert DseEngineControllerApplication

def test_config():
    from dse_engine_controller.app_config import DseEngineControllerConfig

    config = DseEngineControllerConfig()
    assert isinstance(config.to_dict(), dict)

def test_ui():
    from dse_engine_controller.app_ui import DseEngineControllerUI
    assert DseEngineControllerUI

def test_state():
    from dse_engine_controller.app_state import DseEngineControllerState
    assert DseEngineControllerState