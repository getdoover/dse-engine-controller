"""Configurable digital outputs, and what the run modes do with them.

A site wires whatever it wires — a clutch, a valve, a light, a second pump's
enable line — so the app doesn't get to decide what any of them mean. Each
configured output is a pin, a name and two resting states: where it sits with the
engine stopped, and where it sits while the engine runs. A run mode then names
only the outputs it wants *differently* from that running state, and anything it
doesn't mention takes the default — so a mode never inherits whatever the last
one happened to leave behind, and never has to restate the obvious either.

That replaces the built-in clutch, which was one hard-coded output with its own
engage and disengage delays. A clutch is now a Digital Output that the modes
happen to switch, and the delays are expressed the way every other timing in this
app is — as a mode with a ``Minimum Seconds`` and a transition out of it.

Identity works the way it does for sensors (see ``sensors.py``): ``tag_key`` names
the tag and is keyed on the pin, so re-ordering the list can't move one output's
history onto another; ``sanitised`` names UI elements and follows the operator's
label, so renaming an output renames its display.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydoover.utils.utils import sanitize_display_name as sanitise

from .sensors import cfg_value

log = logging.getLogger(__name__)


@dataclass
class DigitalOutput:
    """One configured digital output."""

    label: str
    sanitised: str
    tag_key: str
    pin: int
    # Where it sits with the engine stopped, and where it sits while the engine
    # runs unless a mode says otherwise. Two defaults rather than one because
    # "off" and "running but unremarkable" are genuinely different resting
    # places: a clutch is disengaged for the first and engaged for the second,
    # and having only one meant every mode had to repeat itself to say so.
    off_state: bool = False
    running_state: bool = False

    # Bound by build_outputs.
    tag: Any = None

    @property
    def tag_name(self) -> str:
        return f"output_{self.tag_key}"

    @property
    def element_name(self) -> str:
        return f"output_{self.sanitised}"


def _display_label(label: str) -> str:
    """The operator's name for an output, tidied for display.

    Capitalises a word only when it has no capitals of its own, so "clutch"
    becomes "Clutch" and "discharge valve" becomes "Discharge Valve" — while
    "PTO valve" keeps its acronym rather than being flattened to "Pto Valve" by
    a blanket title-case.

    Applied at parse time so the log lines and the config warnings read the same
    as the dashboard does.
    """
    return " ".join(
        word if any(c.isupper() for c in word) else word.capitalize()
        for word in label.split()
    ) or label


def output_specs(config) -> list[DigitalOutput]:
    """Parse every configured output from config alone, without binding tags.

    Used by the tag factory, which has to know what tags to create before they
    exist, and by :func:`build_outputs`.
    """
    specs: list[DigitalOutput] = []
    seen_names: set[str] = set()
    seen_keys: set[str] = set()

    def unique(value: str, taken: set[str], what: str, label: str) -> str:
        name, suffix = value, 2
        while name in taken:
            name = f"{value}_{suffix}"
            suffix += 1
        if name != value:
            log.warning(
                "%s for %r collides with another output; using %r instead. Give "
                "the outputs distinct names.",
                what,
                label,
                name,
            )
        taken.add(name)
        return name

    for index, slot in enumerate(
        cfg_value(getattr(config, "digital_outputs", None), []) or []
    ):
        pin = cfg_value(getattr(slot, "do_pin", None))
        if pin is None:
            log.warning("Ignoring digital output %d: no pin set.", index + 1)
            continue
        pin = int(pin)
        label = _display_label(
            str(cfg_value(getattr(slot, "label", None), "") or f"Output {pin}")
        )
        specs.append(
            DigitalOutput(
                label=label,
                sanitised=unique(
                    sanitise(label) or f"output_{index}",
                    seen_names,
                    "Output name",
                    label,
                ),
                # Keyed by pin rather than list position, so re-ordering or
                # deleting an entry can't move another output's history.
                tag_key=unique(f"pin{pin}", seen_keys, "Output pin", label),
                pin=pin,
                off_state=bool(
                    cfg_value(getattr(slot, "default_off_state", None), False)
                ),
                running_state=bool(
                    cfg_value(getattr(slot, "default_running_state", None), False)
                ),
            )
        )
    return specs


def build_outputs(config, tags) -> list[DigitalOutput]:
    """Parse every configured output and bind the tag it publishes its state on."""
    specs = output_specs(config)
    for spec in specs:
        try:
            spec.tag = getattr(tags, spec.tag_name)
        except AttributeError:
            log.warning(
                "Output tag %r does not exist — skipping this binding.",
                spec.tag_name,
            )
    return specs


def desired_states(outputs, mode) -> dict[int, bool]:
    """What every pin should be, given the run mode in force.

    ``mode`` of ``None`` means the engine is not turning, and every output sits
    at its off state — run modes have no say there, since nothing is running for
    them to be a mode of. Otherwise a mode's own settings win, and anything it
    doesn't mention takes the running default. So a mode names only the outputs
    it wants *differently* from the ordinary running state, rather than having to
    restate all of them.
    """
    if mode is None:
        return {output.pin: output.off_state for output in outputs}
    return {
        output.pin: bool(mode.outputs.get(output.sanitised, output.running_state))
        for output in outputs
    }
