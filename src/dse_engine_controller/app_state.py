import logging
from typing import TYPE_CHECKING

from pydoover.state import StateMachine

if TYPE_CHECKING:
    from .application import DseEngineControllerApplication

log = logging.getLogger(__name__)


class EngineState:
    """
    Engine state machine for controlling engine start/stop sequences.

    States:
        - stopped: Engine is off
        - pre_crank: Preparing to crank (fuel pump priming, etc.)
        - cranking: Engine is cranking
        - crank_rest: Resting between crank attempts
        - running: Engine is running normally
        - cooling_down: Engine is in cooldown period before shutdown
        - fault: Engine is in fault state (requires manual reset)
    """

    state: str

    states = [
        {"name": "stopped"},
        {"name": "pre_crank", "timeout": 3, "on_timeout": "crank"},
        {"name": "cranking", "timeout": 10, "on_timeout": "crank_timeout"},
        {"name": "crank_rest", "timeout": 5, "on_timeout": "retry_crank"},
        {"name": "running"},
        {"name": "cooling_down", "timeout": 60, "on_timeout": "shutdown_complete"},
        {"name": "fault"},
    ]

    transitions = [
        # Start sequence
        {"trigger": "start_request", "source": "stopped", "dest": "pre_crank"},
        {"trigger": "crank", "source": "pre_crank", "dest": "cranking"},
        {"trigger": "engine_started", "source": "cranking", "dest": "running"},
        {"trigger": "crank_timeout", "source": "cranking", "dest": "crank_rest"},
        {"trigger": "retry_crank", "source": "crank_rest", "dest": "cranking"},
        {"trigger": "max_cranks_exceeded", "source": "crank_rest", "dest": "fault"},

        # Stop sequence
        {"trigger": "stop_request", "source": "running", "dest": "cooling_down"},
        {"trigger": "shutdown_complete", "source": "cooling_down", "dest": "stopped"},
        {"trigger": "immediate_stop", "source": ["running", "cooling_down"], "dest": "stopped"},

        # Fault handling
        {"trigger": "fault_detected", "source": "*", "dest": "fault"},
        {"trigger": "reset_fault", "source": "fault", "dest": "stopped"},

        # Emergency stop from any state
        {"trigger": "emergency_stop", "source": "*", "dest": "stopped"},
    ]

    def __init__(self, app: "DseEngineControllerApplication"):
        self.app = app
        self.crank_attempts = 0

        self.state_machine = StateMachine(
            states=self.states,
            transitions=self.transitions,
            model=self,
            initial="stopped",
            queued=True,
        )

    async def on_enter_stopped(self):
        """Called when engine enters stopped state."""
        log.info("Engine stopped")
        self.crank_attempts = 0
        if self.app.ui:
            self.app.ui.engine_status.update("Stopped")

    async def on_enter_pre_crank(self):
        """Called when preparing to crank."""
        log.info("Pre-crank: Priming fuel system...")
        self.crank_attempts = 0
        if self.app.ui:
            self.app.ui.engine_status.update("Pre-crank")

    async def on_enter_cranking(self):
        """Called when cranking begins."""
        self.crank_attempts += 1
        log.info(f"Cranking (attempt {self.crank_attempts})...")
        if self.app.ui:
            self.app.ui.engine_status.update(f"Cranking ({self.crank_attempts})")

    async def on_exit_cranking(self):
        """Check crank attempts when exiting cranking state."""
        pass

    async def on_enter_crank_rest(self):
        """Called when resting between crank attempts."""
        log.info(f"Crank rest (attempt {self.crank_attempts} of {self.app.config.max_crank_attempts.value})")
        if self.app.ui:
            self.app.ui.engine_status.update("Crank Rest")

        # Check if max attempts exceeded
        if self.crank_attempts >= self.app.config.max_crank_attempts.value:
            log.error("Max crank attempts exceeded")
            await self.max_cranks_exceeded()

    async def on_enter_running(self):
        """Called when engine starts running."""
        log.info("Engine running")
        self.crank_attempts = 0
        if self.app.ui:
            self.app.ui.engine_status.update("Running")

    async def on_enter_cooling_down(self):
        """Called when engine enters cooldown."""
        log.info("Engine cooling down...")
        if self.app.ui:
            self.app.ui.engine_status.update("Cooling Down")

    async def on_enter_fault(self):
        """Called when engine enters fault state."""
        log.error("Engine fault detected!")
        if self.app.ui:
            self.app.ui.engine_status.update("FAULT")

    async def evaluate_state(self, engine_running: bool, fault_active: bool):
        """
        Evaluate current conditions and trigger appropriate transitions.

        Args:
            engine_running: Whether the engine is actually running (RPM > threshold)
            fault_active: Whether any fault condition is active
        """
        # Check for faults first
        if fault_active and self.state != "fault":
            await self.fault_detected()
            return

        # Check if engine started during cranking
        if self.state == "cranking" and engine_running:
            await self.engine_started()

    # Type hints for dynamically created trigger methods
    async def start_request(self): ...
    async def crank(self): ...
    async def engine_started(self): ...
    async def crank_timeout(self): ...
    async def retry_crank(self): ...
    async def max_cranks_exceeded(self): ...
    async def stop_request(self): ...
    async def shutdown_complete(self): ...
    async def immediate_stop(self): ...
    async def fault_detected(self): ...
    async def reset_fault(self): ...
    async def emergency_stop(self): ...
