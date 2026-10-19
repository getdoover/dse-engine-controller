"""Derived display state for the DSE engine controller app.

The DSE module itself owns the real state machine — we just summarise the
control mode, engine operating state and alarms from the latest read into a
short string for the UI status display and the ``state`` tag.
"""

from __future__ import annotations

from .dse_state import DseStateStore


KNOWN_STATES = (
    "initialising",
    "no_comms",
    "stopped_manual",
    "stopped_auto",
    "pre_start",
    "warming_up",
    "running",
    "cooling_down",
    "fault",
)


class DseEngineControllerState:
    """Holds the most recently derived state name."""

    def __init__(self):
        self.state: str = "initialising"

    def update(self, store: DseStateStore) -> str:
        self.state = self._derive(store)
        return self.state

    @staticmethod
    def _derive(store: DseStateStore) -> str:
        if not store.snapshots:
            return "initialising"
        if not store.is_contactable:
            return "no_comms"
        snap = store.latest
        if snap is None:
            return "no_comms"
        if snap.is_faulted:
            return "fault"
        if snap.is_running:
            if snap.is_warming_up:
                return "warming_up"
            if snap.is_cooling_down:
                return "cooling_down"
            return "running"
        if snap.is_pre_start:
            return "pre_start"
        return "stopped_auto" if snap.is_auto else "stopped_manual"
