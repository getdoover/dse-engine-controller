"""Scaling, filtering and fault detection for a native analog input.

One :class:`AnalogInput` per configured ``analog_ref_<i>`` slot. It turns the raw
signal the platform interface reports (mA on a current loop, V on a voltage
input) into an engineering value, smooths it, and decides whether the loop is
healthy.

Design
------
* **No hardware access, no timers** — same rule as :mod:`.regulator`. The
  application does the ``fetch_ai`` and passes the reading plus a timestep into
  :meth:`AnalogInput.update`, which keeps all of this unit-testable.
* **Fault before scale.** A broken loop must never be published as a plausible
  number. Faults are classified from the raw signal first; on a fault the last
  good value is briefly held and the fault is flagged, rather than a scaled
  reading of garbage being written to the tag.
* **Stale readings expire.** Holding the last value forever means a dead loop
  renders as a healthy number sitting still, which is the failure mode an
  operator is most likely to miss. After ``hold_seconds`` without a usable
  reading the value goes ``None``, so the dashboard gauge blanks.
* **Reversed ranges are free.** ``value_min > value_max`` scales inversely, which
  covers depth-vs-ullage style sensors without another config field.
"""

from __future__ import annotations

# Slack below the bottom of the signal range that still counts as "at zero"
# rather than a fault — sensors commonly idle a hair under 4 mA at their zero
# reading, and clamping is friendlier than crying fault. Open-circuit detection
# uses the configured fault threshold (typically 3.5 mA), which sits below this.
UNDER_RANGE_SLACK = 0.5

# How far above the top of the signal range is tolerated before flagging
# over-range. Same rationale as the slack below.
OVER_RANGE_SLACK = 0.5

# Signal-type presets: (signal_min, signal_max, units, open-circuit detectable).
# A 4-20 mA loop reading below its fault threshold is a broken loop; 0 V on a
# voltage input is a legitimate reading, so those can't be checked the same way.
SIGNAL_TYPE_CURRENT = "4-20 mA"
SIGNAL_TYPE_VOLTAGE = "0-10 V"
SIGNAL_TYPE_CUSTOM = "Custom"
SIGNAL_TYPES = [SIGNAL_TYPE_CURRENT, SIGNAL_TYPE_VOLTAGE, SIGNAL_TYPE_CUSTOM]
SIGNAL_PRESETS = {
    SIGNAL_TYPE_CURRENT.lower(): (4.0, 20.0, "mA", True),
    SIGNAL_TYPE_VOLTAGE.lower(): (0.0, 10.0, "V", False),
}

# How long a faulted input keeps publishing its last good value before going
# None. Long enough to ride out a glitch or a single missed poll, short enough
# that nobody trusts a dead gauge.
DEFAULT_HOLD_SECONDS = 15.0

FAULT_NO_READING = "no reading"
FAULT_OPEN_CIRCUIT = "open circuit"
FAULT_OVER_RANGE = "over range"


class AnalogInput:
    """Scales, filters and fault-checks one analog input channel.

    Parameters
    ----------
    signal_min, signal_max:
        Raw signal at the bottom / top of the range (e.g. 4 and 20 for mA).
    value_min, value_max:
        Engineering value at ``signal_min`` / ``signal_max``.
    fault_below:
        Raw reading below which the loop is considered broken (open circuit).
        ``None`` disables open-circuit detection — correct for voltage inputs,
        where 0 V is a legitimate reading.
    filter_seconds:
        Time constant of the exponential moving average. ``0`` passes readings
        through unfiltered.
    hold_seconds:
        How long a faulted input keeps reporting its last good value before
        :attr:`value` goes ``None``. Duration is only measured when ``update`` is
        given a ``now``; without one the value is held indefinitely.
    clamp:
        Whether to clamp the scaled value into the ``value_min``/``value_max``
        band. On by default, so a sensor sitting slightly out of range reads as
        the end of scale rather than an impossible number.
    """

    def __init__(
        self,
        *,
        signal_min: float = 4.0,
        signal_max: float = 20.0,
        value_min: float = 0.0,
        value_max: float = 100.0,
        fault_below: float | None = 3.5,
        filter_seconds: float = 0.0,
        hold_seconds: float = DEFAULT_HOLD_SECONDS,
        clamp: bool = True,
    ):
        self.signal_min = float(signal_min)
        self.signal_max = float(signal_max)
        self.value_min = float(value_min)
        self.value_max = float(value_max)
        self.fault_below = None if fault_below is None else float(fault_below)
        self.filter_seconds = max(0.0, float(filter_seconds))
        self.hold_seconds = max(0.0, float(hold_seconds))
        self.clamp = bool(clamp)

        # _value is the last good reading — kept even once it's too stale to
        # publish, so the filter stays continuous and `has_reading` still knows
        # this input has worked at some point. _published is what the app writes
        # to the tag, and goes None once a fault outlasts hold_seconds.
        self._value: float | None = None
        self._published: float | None = None
        self._ever_read: bool = False
        self._raw: float | None = None
        self._fault_reason: str = FAULT_NO_READING
        self._fault_since: float | None = None

    @classmethod
    def from_slot(
        cls, slot, *, hold_seconds: float = DEFAULT_HOLD_SECONDS
    ) -> "AnalogInput":
        """Build from a :class:`~.sensors.SensorSlot` describing an analog slot."""
        return cls(
            signal_min=slot.signal_min,
            signal_max=slot.signal_max,
            value_min=slot.value_min,
            value_max=slot.value_max,
            fault_below=slot.fault_below,
            filter_seconds=slot.filter_seconds,
            hold_seconds=hold_seconds,
        )

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def value(self) -> float | None:
        """The value to publish.

        The scaled + filtered reading, held through a brief fault, then ``None``
        once the fault outlasts :attr:`hold_seconds`.
        """
        return self._published

    @property
    def last_good(self) -> float | None:
        """Last usable reading, kept even once it's too stale to publish."""
        return self._value

    @property
    def raw(self) -> float | None:
        """Last raw signal reading (mA / V), including faulted ones."""
        return self._raw

    @property
    def fault(self) -> bool:
        return bool(self._fault_reason)

    @property
    def stale(self) -> bool:
        """Whether the fault has outlasted the hold window (nothing to publish)."""
        return bool(self._fault_reason) and self._published is None and self._ever_read

    @property
    def has_reading(self) -> bool:
        """Whether this input has ever produced a usable reading.

        An input starts life faulted with :data:`FAULT_NO_READING`, which is
        indistinguishable from a loop that has just broken. Anything that *acts*
        on a fault (rather than just reporting it) has to wait for this, or a
        slow-starting platform interface looks like a dead sensor at boot.
        """
        return self._ever_read

    @property
    def fault_reason(self) -> str:
        """One of the ``FAULT_*`` strings, or ``""`` when healthy."""
        return self._fault_reason

    def faulted_for(self, now: float) -> float:
        """Seconds this input has been continuously faulted (0 when healthy)."""
        if not self._fault_reason or self._fault_since is None:
            return 0.0
        return max(0.0, float(now) - self._fault_since)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update(self, raw: float | None, dt: float, now: float | None = None) -> float | None:
        """Take one reading; return the current engineering value.

        ``dt`` is the seconds since the previous update (drives the filter) and
        ``now`` a monotonic timestamp used only for fault duration. A faulted
        reading holds the previous value rather than publishing nonsense.
        """
        self._raw = None if raw is None else float(raw)
        reason = self._classify(self._raw)
        self._note_fault(reason, now)
        if reason:
            if now is not None and self.faulted_for(now) > self.hold_seconds:
                # Too stale to stand behind — publish nothing rather than a
                # number that looks alive.
                self._published = None
            return self._published

        scaled = self._scale(float(raw))
        self._value = self._filter(scaled, dt)
        self._published = self._value
        self._ever_read = True
        return self._published

    def _classify(self, raw: float | None) -> str:
        """Fault reason for a raw reading, or ``""`` when it's usable."""
        if raw is None:
            return FAULT_NO_READING
        if self.fault_below is not None and raw < self.fault_below:
            return FAULT_OPEN_CIRCUIT
        lo = min(self.signal_min, self.signal_max)
        hi = max(self.signal_min, self.signal_max)
        if raw < lo - UNDER_RANGE_SLACK:
            # Below range but above the open-circuit threshold: with detection
            # disabled (voltage inputs) this is the only signal that something is
            # wrong, so treat it the same way.
            return FAULT_OPEN_CIRCUIT
        if raw > hi + OVER_RANGE_SLACK:
            return FAULT_OVER_RANGE
        return ""

    def _note_fault(self, reason: str, now: float | None) -> None:
        if reason and not self._fault_reason:
            self._fault_since = now  # fault has just begun
        elif not reason:
            self._fault_since = None
        elif self._fault_since is None:
            self._fault_since = now
        self._fault_reason = reason

    def _scale(self, raw: float) -> float:
        span = self.signal_max - self.signal_min
        if span == 0:
            return self.value_min
        fraction = (raw - self.signal_min) / span
        value = self.value_min + fraction * (self.value_max - self.value_min)
        if self.clamp:
            lo = min(self.value_min, self.value_max)
            hi = max(self.value_min, self.value_max)
            value = max(lo, min(hi, value))
        return value

    def _filter(self, value: float, dt: float) -> float:
        """Exponential moving average with a time constant in seconds.

        First reading seeds the filter, so startup doesn't ramp from zero.
        """
        if self.filter_seconds <= 0 or self._value is None:
            return value
        dt = max(0.0, float(dt))
        if dt <= 0:
            return self._value
        alpha = dt / (self.filter_seconds + dt)
        return self._value + alpha * (value - self._value)
