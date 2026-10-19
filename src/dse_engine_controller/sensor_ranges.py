"""Coloured display bands for a sensor's gauge.

Kept in its own module so :mod:`.app_config` (which needs the colour choices) and
:mod:`.sensors` (which parses the configured bands) don't have to import each
other. The values are the colour names :class:`pydoover.ui.Colour` accepts.
"""

from __future__ import annotations

from dataclasses import dataclass

# Offered in the config form, ordered so the common traffic-light choices come
# first. The site accepts any HTML colour name, but a fixed list keeps the form a
# picker rather than a free-text field.
RANGE_COLOURS = (
    "green",
    "yellow",
    "red",
    "blue",
    "orange",
    "grey",
    "limegreen",
    "tomato",
    "purple",
    "magenta",
)

DEFAULT_RANGE_COLOUR = RANGE_COLOURS[0]


@dataclass(frozen=True)
class SensorRange:
    """One band on a gauge: ``min_val`` → ``max_val``, coloured, optionally labelled."""

    min_val: float
    max_val: float
    colour: str = DEFAULT_RANGE_COLOUR
    label: str = ""
