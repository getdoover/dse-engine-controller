"""Pure RPM regulator for the DSE engine controller's "Regulate" RPM mode.

In Regulate mode the operator picks a controlled variable (a remote sensor —
e.g. inlet pressure), a setpoint, and a single "sensitivity" knob. This class
runs a PI loop that nudges the engine's target RPM so the measured variable
holds at the setpoint.

Design
------
* **Unit-agnostic.** The loop works on *relative* error — ``(setpoint -
  measured) / |setpoint|`` — so one "sensitivity" knob behaves the same whether
  the variable is kPa, mm, %, or m³/hr. The operator never has to think in
  RPM-per-kPa.
* **One knob, geometrically mapped.** Sensitivity multiplies both gains by
  ``sensitivity_gain()`` — ten-fold per :data:`SENSITIVITY_DECADE` steps, anchored
  so the top of the slider is the fastest loop these pumps need — so the range
  runs *downwards* into the gentle end, where it was previously missing.
* **Direction-aware.** By default it's direct-acting — more RPM → more of the
  variable (true for pump pressure / flow), so it speeds up when the reading is
  below setpoint. Pass ``invert=True`` for inverse-acting variables (more RPM →
  less of the variable), where it speeds up when the reading is above setpoint.
* **Output-clamped with anti-windup.** The commanded RPM is clamped to the
  configured min/max, and the integral is held to what those rails can express, so
  it can't wind up a commitment the engine will have to unwind later.
* **No hardware access, no timers.** ``update()`` is called once per main loop
  with the latest reading; the application performs the modbus RPM write. That
  keeps the logic pure and unit-testable.
"""

from __future__ import annotations

import logging
import math

log = logging.getLogger(__name__)


# The operator gets one "sensitivity" knob, and it maps onto the loop's gains
# *geometrically*, anchored at the top: REFERENCE_SENSITIVITY (the top of the
# default slider) means the base gains, and every SENSITIVITY_DECADE steps below it
# divides them by ten.
#
# Anchored at the top because that end is the one we can put a number on: the
# fastest loop these pumps have ever needed is what the old linear scale called
# "6", so the base gains below are exactly that, and knob 10 reproduces it. The
# rest of the slider then buys range where it was missing — the old scale bottomed
# out at "1", which still hunted on one site (a sustained ±40 RPM limit cycle at
# the configured minimum) — that setting now sits around knob 5.3, with
# five decades-worth of slider below it.
REFERENCE_SENSITIVITY = 10.0
# Six steps per decade. Two fixed points pin the scale: knob 10 is the fastest loop
# these pumps need (the old linear scale's "6"), and the bottom of the slider — 0.1
# — is about a 45th of that, which is the gentlest setting anyone has actually
# wanted. Three steps per decade put the floor another 20x below that, which turned
# out to be so slow as to be useless.
SENSITIVITY_DECADE = 6.0

# Where a fresh install starts: gentle enough not to hunt on the pumps we've seen —
# that site's limit cycle was at the equivalent of about knob 5.3 — leaving the
# operator to turn it up until the loop tracks as briskly as they want.
DEFAULT_SENSITIVITY = 2.0


def sensitivity_gain(sensitivity: float) -> float:
    """Multiplier the sensitivity knob applies to both gains."""
    return 10.0 ** ((float(sensitivity) - REFERENCE_SENSITIVITY) / SENSITIVITY_DECADE)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _usable(value) -> bool:
    """Whether a reading can be arithmetic'd on.

    A sensor or an upstream app can publish NaN or inf — a divide in someone
    else's scaling, a JSON "Infinity". Left unchecked those poison the integral
    and then raise ValueError in ``int(round(...))`` when the RPM is written,
    which takes the whole main loop down with a running engine.
    """
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


class RpmRegulator:
    """PI controller mapping a sensor error onto a target RPM.

    Parameters
    ----------
    rpm_min, rpm_max:
        Hard bounds on the commanded RPM (the configured Target RPM Min / Max).
    base_kp:
        Proportional gain, as a fraction of the RPM span applied per unit of
        *relative* error, at ``sensitivity == REFERENCE_SENSITIVITY``. The
        effective gain is ``base_kp * span * sensitivity_gain(sensitivity)``.
    base_ki:
        Integral gain, as a fraction of the RPM span per unit relative error per
        second, at ``sensitivity == REFERENCE_SENSITIVITY``.
    min_scale:
        Absolute floor on the normalising scale, so a setpoint of 0 can't divide
        by zero.
    scale_floor_fraction:
        Floor on the normalising scale as a fraction of the controlled variable's
        span (passed to :meth:`update`). Relative error divides by the setpoint's
        magnitude, so a small setpoint on a wide-range sensor — 5 kPa on a
        0-1600 kPa loop — would otherwise make one unit of error look like a
        300% deviation and slam the engine to a rail. Flooring the divisor at a
        fraction of the span keeps the loop's aggressiveness roughly constant
        wherever the setpoint sits.
    """

    def __init__(
        self,
        rpm_min: float,
        rpm_max: float,
        *,
        base_kp: float = 0.30,
        base_ki: float = 0.06,
        min_scale: float = 1e-6,
        scale_floor_fraction: float = 0.1,
    ):
        if rpm_max < rpm_min:
            rpm_min, rpm_max = rpm_max, rpm_min
        self.rpm_min = float(rpm_min)
        self.rpm_max = float(rpm_max)
        self.span = max(0.0, self.rpm_max - self.rpm_min)
        self.base_kp = float(base_kp)
        self.base_ki = float(base_ki)
        self.min_scale = float(min_scale)
        self.scale_floor_fraction = float(scale_floor_fraction)

        # Output = rpm_min + integral + P. The integral carries the steady-state
        # bias; P provides the immediate response. Seeded by reset().
        self._integral: float = self.span / 2.0
        self._output: float = self.rpm_min + self._integral

    @property
    def output(self) -> float:
        return self._output

    def reset(self, current_rpm: float | None = None) -> None:
        """Seed the loop so its first output matches where the engine is now.

        Call this when entering Regulate mode (or switching variable/setpoint)
        so the engine doesn't jump — the integral is back-calculated from the
        current RPM with zero proportional contribution.
        """
        start = (
            _clamp(float(current_rpm), self.rpm_min, self.rpm_max)
            if current_rpm is not None
            else self.rpm_min + self.span / 2.0
        )
        self._integral = start - self.rpm_min
        self._output = start

    def update(
        self,
        measured: float | None,
        setpoint: float | None,
        sensitivity: float,
        dt: float,
        *,
        invert: bool = False,
        span: float | None = None,
    ) -> float:
        """Advance the loop one step and return the new target RPM.

        Holds the last output unchanged when there's nothing to act on: a missing,
        NaN or infinite reading or setpoint, or a non-positive timestep. ``invert``
        flips the control direction for inverse-acting variables (more RPM → less
        of the variable). ``span`` is the controlled variable's configured range,
        used to floor the error normalisation (see ``scale_floor_fraction``).
        """
        if dt <= 0:
            return self._output
        if not (_usable(measured) and _usable(setpoint)):
            # Hold, and say why once — a NaN reaching the RPM write would raise
            # and take down a loop that is controlling a running engine.
            log.warning(
                "Regulate step skipped: unusable measured=%r setpoint=%r",
                measured,
                setpoint,
            )
            return self._output

        sensitivity = max(0.0, float(sensitivity))
        if sensitivity <= 0:
            return self._output  # zero still means "hold, don't control"
        gain = sensitivity_gain(sensitivity)
        scale_floor = self.min_scale
        if span is not None and _usable(span):
            scale_floor = max(scale_floor, abs(float(span)) * self.scale_floor_fraction)
        scale = max(abs(float(setpoint)), scale_floor)
        rel_error = (float(setpoint) - float(measured)) / scale
        if invert:
            rel_error = -rel_error

        kp = self.base_kp * self.span * gain
        ki = self.base_ki * self.span * gain

        p_term = kp * rel_error
        integral_candidate = self._integral + ki * rel_error * dt

        # Anti-windup by back-calculation: let the integral move, but only as far
        # as the rails allow once P is added, so it can never carry a commitment
        # the output can't express.
        #
        # The obvious alternative — discard the whole step when it would saturate —
        # leaves the loop stalled short of the rail whenever a single step would
        # overshoot: the rejected candidate means the output is recomputed from the
        # *old* integral. Hot gains hid that (P alone reached the rail); gentle ones
        # don't.
        headroom = self.span - p_term
        floor = -p_term
        self._integral = _clamp(
            integral_candidate, min(floor, headroom), max(floor, headroom)
        )

        self._output = _clamp(
            self.rpm_min + self._integral + p_term, self.rpm_min, self.rpm_max
        )
        return self._output
