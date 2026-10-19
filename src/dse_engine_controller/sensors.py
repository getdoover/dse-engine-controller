"""Unified sensor model for the DSE engine controller.

Sensor readings reach this app from two places:

* **Native analog inputs** — this app reads a 4-20 mA / 0-10 V loop straight off
  the device's analog inputs. Configured as a list (``analog_inputs``), so an
  install shows exactly the inputs it has and no empty slots.
* **Remote tags** — another app on the device publishes the value and pydoover's
  ``RemoteTag`` republishes it locally. These stay a small fixed number of slots
  (``sensor_ref_<i>``), behind an ``Enable Remote Tags`` switch: pydoover only
  resolves ``TagRef``s declared at the top level of the schema, so they can't live
  in a list yet, and a slot is a form rather than a list row.

Everything downstream — the dashboard gauges, the start/stop triggers and the RPM
regulate loop — only needs a number, a label and some bounds, so both sources are
flattened into one list of :class:`SensorSlot` here.

Reading the config happens in two passes, because the tags a sensor needs don't
exist until they have been created from that same config:

* :func:`sensor_specs` parses config alone. ``DseEngineControllerTags.setup()`` uses
  it to create one set of tags per configured sensor.
* :func:`build_sensors` does the same parse and attaches those tags. The
  application and the UI use it.

Identity matters in two different ways, so a sensor carries two names:

* ``tag_key`` names its tags and is stable — the AI pin for an analog input, the
  slot number for a remote tag. Reordering the list can't move one sensor's
  logged history onto another's tags.
* ``sanitised`` names its UI elements and is what the Variable pickers store. It
  follows the operator's label, so renaming a sensor renames its elements.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydoover.utils.utils import sanitize_display_name as sanitise

from .analog import SIGNAL_PRESETS, SIGNAL_TYPE_CURRENT
from .app_config import MAX_REMOTE_SENSORS
from .sensor_ranges import DEFAULT_RANGE_COLOUR, SensorRange

log = logging.getLogger(__name__)

SENSOR_KIND_REMOTE = "remote"
SENSOR_KIND_ANALOG = "analog"

# Prefixes for the per-sensor UI visibility flags (only the selected variable's
# setpoint slider is shown). Kept here so the tag factory and the UI agree.
# ("regulate_var_hidden" used to live here. The Regulate variable is now fixed
# per run mode in config rather than picked on the dashboard, so there is no
# per-sensor regulate slider left to show or hide — see run_modes.py.)
HIDDEN_TAG_PREFIXES = (
    "start_var_hidden",
    "stop_var_hidden",
)


def cfg_value(elem, default=None):
    """Read a config element's value, tolerating unset/blank required fields.

    Blank optional ``config.Object``s (e.g. an unwired sensor slot) leave their
    sub-fields unset, and required fields raise rather than return None, so every
    read of a possibly-blank slot goes through here.
    """
    if elem is None:
        return default
    try:
        value = elem.value
    except Exception:
        return default
    return default if value is None else value


@dataclass
class SensorSlot:
    """One configured sensor, whatever its source."""

    kind: str
    label: str
    sanitised: str
    tag_key: str
    units: str
    invert: bool
    sp_min: float
    sp_max: float
    # Whether this sensor may be picked as the RPM Regulate variable. Opt-in: a
    # sensor that's only watched, or only used as a start/stop trigger, has no
    # business driving engine speed.
    allow_rpm_control: bool = False

    # --- analog only ---
    pin: int | None = None
    signal_type: str = SIGNAL_TYPE_CURRENT
    signal_min: float = 4.0
    signal_max: float = 20.0
    signal_units: str = "mA"
    value_min: float = 0.0
    value_max: float = 100.0
    fault_below: float | None = 3.5
    filter_seconds: float = 0.0
    ranges: tuple[SensorRange, ...] = ()

    # --- bound tags, filled in by build_sensors ---
    tag: Any = None
    raw_tag: Any = None
    fault_tag: Any = None
    hidden_tags: dict = field(default_factory=dict)

    @property
    def is_analog(self) -> bool:
        return self.kind == SENSOR_KIND_ANALOG

    @property
    def faulted(self) -> bool:
        """Whether this sensor is currently flagged faulted (analog only)."""
        return bool(self.fault_tag.get()) if self.fault_tag is not None else False

    # --- tag names: stable identity, independent of list position ---

    @property
    def value_tag_name(self) -> str:
        if self.is_analog:
            return f"analog_value_{self.tag_key}"
        return f"remote_sensor_{self.tag_key}"  # a declared RemoteTag

    @property
    def raw_tag_name(self) -> str:
        return f"analog_raw_{self.tag_key}"

    @property
    def fault_tag_name(self) -> str:
        return f"analog_fault_{self.tag_key}"

    def hidden_tag_name(self, prefix: str) -> str:
        """Per-sensor UI visibility flag — keyed by UI identity, not tag identity."""
        return f"{prefix}_{self.sanitised}"


def remote_slot_is_wired(slot) -> bool:
    """A remote slot counts as configured once it has both a source app and tag."""
    return bool(cfg_value(slot.app_name)) and bool(cfg_value(slot.tag_name))


def signal_range(slot) -> tuple[float, float, str, float | None]:
    """Resolve an analog slot's signal range, units and fault threshold.

    Returns ``(signal_min, signal_max, signal_units, fault_below)``. The named
    presets fix the range; ``Custom`` takes it from the slot's own fields and gets
    no open-circuit detection (we can't know what "broken" looks like).
    """
    signal_type = str(cfg_value(getattr(slot, "signal_type", None), SIGNAL_TYPE_CURRENT))
    preset = SIGNAL_PRESETS.get(signal_type.strip().lower())
    if preset is None:  # Custom (or an unknown value saved by an older schema)
        lo = float(cfg_value(getattr(slot, "signal_min", None), 4.0))
        hi = float(cfg_value(getattr(slot, "signal_max", None), 20.0))
        return lo, hi, "", None
    lo, hi, units, detects_open_circuit = preset
    fault_below = None
    if detects_open_circuit:
        fault_below = float(cfg_value(getattr(slot, "fault_below", None), 3.5))
    return lo, hi, units, fault_below


def display_ranges(slot) -> tuple[SensorRange, ...]:
    """Parse an analog slot's configured gauge bands.

    Bands entered back-to-front are normalised, zero-width ones are dropped, and
    the result is ordered by where each band starts so the gauge reads left to
    right however they were entered.
    """
    configured = cfg_value(getattr(slot, "display_ranges", None), []) or []
    ranges: list[SensorRange] = []
    for item in configured:
        lo = cfg_value(getattr(item, "min_val", None))
        hi = cfg_value(getattr(item, "max_val", None))
        if lo is None or hi is None:
            # A row added in the form and left blank. Skipping it beats inventing
            # a band, since bands define the gauge's extent.
            log.warning("Ignoring display range with no From / To value.")
            continue
        lo, hi = float(lo), float(hi)
        lo, hi = min(lo, hi), max(lo, hi)
        if lo == hi:
            log.warning("Ignoring zero-width display range at %s.", lo)
            continue
        ranges.append(
            SensorRange(
                min_val=lo,
                max_val=hi,
                colour=str(
                    cfg_value(getattr(item, "colour", None), DEFAULT_RANGE_COLOUR)
                    or DEFAULT_RANGE_COLOUR
                ),
                label=str(cfg_value(getattr(item, "label", None), "") or ""),
            )
        )
    return tuple(sorted(ranges, key=lambda r: r.min_val))


def sensor_specs(config) -> list[SensorSlot]:
    """Parse every configured sensor from config alone, without binding tags.

    Used by the tag factory (which has to know what tags to create before they
    exist) and by :func:`build_sensors`.
    """
    specs: list[SensorSlot] = []
    seen_sanitised: set[str] = set()
    seen_tag_keys: set[str] = set()

    def unique(value: str, taken: set[str], what: str, label: str) -> str:
        name = value
        suffix = 2
        while name in taken:
            name = f"{value}_{suffix}"
            suffix += 1
        if name != value:
            log.warning(
                "%s for %r collides with another sensor; using %r instead. Give "
                "the sensors distinct names.",
                what,
                label,
                name,
            )
        taken.add(name)
        return name

    # --- native analog inputs: a list, so there are no empty slots to scroll past ---
    for index, slot in enumerate(cfg_value(config.analog_inputs, []) or []):
        pin = cfg_value(getattr(slot, "ai_pin", None))
        if pin is None:
            log.warning("Ignoring analog input %d: no pin set.", index + 1)
            continue
        pin = int(pin)
        label = str(cfg_value(slot.sensor_label) or f"Analog Input {index + 1}")
        signal_min, signal_max, signal_units, fault_below = signal_range(slot)
        specs.append(
            SensorSlot(
                kind=SENSOR_KIND_ANALOG,
                label=label,
                sanitised=unique(
                    sanitise(label) or f"analog_{index}",
                    seen_sanitised,
                    "Sensor name",
                    label,
                ),
                # Keyed by pin rather than list position, so reordering or
                # deleting an entry can't move another sensor's history.
                tag_key=unique(f"pin{pin}", seen_tag_keys, "Analog input pin", label),
                units=str(cfg_value(slot.sensor_units) or ""),
                invert=bool(cfg_value(slot.invert, False)),
                sp_min=float(cfg_value(slot.setpoint_min, 0.0)),
                sp_max=float(cfg_value(slot.setpoint_max, 100.0)),
                allow_rpm_control=bool(cfg_value(slot.allow_rpm_control, False)),
                pin=pin,
                signal_type=str(
                    cfg_value(getattr(slot, "signal_type", None), SIGNAL_TYPE_CURRENT)
                ),
                signal_min=signal_min,
                signal_max=signal_max,
                signal_units=signal_units,
                value_min=float(cfg_value(slot.value_min, 0.0)),
                value_max=float(cfg_value(slot.value_max, 100.0)),
                fault_below=fault_below,
                filter_seconds=float(cfg_value(slot.filter_seconds, 0.0)),
                ranges=display_ranges(slot),
            )
        )

    # --- remote tags: fixed slots behind their own switch (see the docstring) ---
    if not bool(cfg_value(config.enable_remote_tags, False)):
        # Switched off — but say so, rather than leaving an operator (or a config
        # profile) staring at a filled-in slot that does nothing.
        wired = _count_wired_remote_slots(config)
        if wired:
            log.warning(
                "%d remote tag slot(s) are configured but 'Enable Remote Tags' is "
                "off — they will be ignored.",
                wired,
            )
        return specs

    for i in range(MAX_REMOTE_SENSORS):
        slot = getattr(config, f"sensor_ref_{i}", None)
        if slot is None or not remote_slot_is_wired(slot):
            continue
        label = str(cfg_value(slot.sensor_label) or f"Remote Sensor {i + 1}")
        specs.append(
            SensorSlot(
                kind=SENSOR_KIND_REMOTE,
                label=label,
                sanitised=unique(
                    sanitise(label) or f"remote_{i}",
                    seen_sanitised,
                    "Sensor name",
                    label,
                ),
                tag_key=str(i),  # matches the declared remote_sensor_<i> RemoteTag
                units=str(cfg_value(slot.sensor_units) or ""),
                invert=bool(cfg_value(slot.invert, False)),
                sp_min=float(cfg_value(slot.setpoint_min, 0.0)),
                sp_max=float(cfg_value(slot.setpoint_max, 1000.0)),
                allow_rpm_control=bool(cfg_value(slot.allow_rpm_control, False)),
            )
        )

    return specs


def build_sensors(config, tags) -> list[SensorSlot]:
    """Parse every configured sensor and bind the tags it uses.

    ``tags`` is the live ``Tags`` instance: the analog value/raw/fault tags and
    every per-sensor visibility flag are created from this same config by
    ``DseEngineControllerTags.setup()``, which the framework runs first.
    """
    specs = sensor_specs(config)
    for spec in specs:
        spec.tag = _bind(tags, spec.value_tag_name)
        if spec.is_analog:
            spec.raw_tag = _bind(tags, spec.raw_tag_name)
            spec.fault_tag = _bind(tags, spec.fault_tag_name)
        spec.hidden_tags = {
            prefix: _bind(tags, spec.hidden_tag_name(prefix))
            for prefix in HIDDEN_TAG_PREFIXES
        }
    return specs


def _bind(tags, name: str):
    """Fetch a tag by name, tolerating one that was never created."""
    try:
        return getattr(tags, name)
    except AttributeError:
        log.warning("Sensor tag %r does not exist — skipping this binding.", name)
        return None


def _count_wired_remote_slots(config) -> int:
    """How many remote tag slots look configured, ignoring the switch."""
    return sum(
        1
        for i in range(MAX_REMOTE_SENSORS)
        if (slot := getattr(config, f"sensor_ref_{i}", None)) is not None
        and remote_slot_is_wired(slot)
    )
