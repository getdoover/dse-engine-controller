"""App-side auto-start orchestration for the DSE engine controller.

The DSE module owns the *real* engine sequence (crank, warm-up, cool-down,
fault latching). This small state machine sits on top of it and decides
**when** to command a remote start — always running a beacon / horn pre-start
warning first so nobody nearby is caught out by the engine cranking.

Design notes
------------
* Six states, deterministic time-based transitions driven from the polling
  loop. No background timers: ``evaluate()`` is called once per main loop with
  the latest panel snapshot and fires at most one transition.
* The machine has **no hardware access of its own**. It only reports *intent*
  — what the beacon / horn should be doing and how long the warning has left —
  and the application performs the modbus + digital-output writes. That keeps
  the logic pure and unit-testable.
* There is intentionally no way to skip the warning: the only path to a running
  engine is ``idle → prestart_warning → starting → running``.
* ``warning_seconds`` is a *floor* on the warning, not its length. The panel's
  Manual→Auto handshake reboots it for ~30s and takes an unpredictable time, so
  the warning is held open — horn and all — until the panel can actually take a
  start command, and only then does ``starting`` send it. ``start_timeout`` is
  one budget for the whole attempt, measured from the operator's press, so it
  bounds the countdown, the wait and the cranking together. The horn then keeps
  sounding through ``starting`` as well, because that is where START commands
  are written and the engine can begin turning at any moment. The rule is simply
  that the horn sounds for as long as a start is being attempted; anything less
  leaves a silent window in which the engine may crank, which is precisely what
  the warning exists to prevent.

State flow::

    idle ──request_start──▶ prestart_warning ──warning_complete──▶ starting
                                  │                                    │
                            cancel_warning                       engine_running
                                  ▼                                    ▼
                                idle ◀─engine_stopped─ stopping ◀─request_stop─┤
                                                          ▲                    │
                                        fault ◀─fault_detected─[starting/running/stopping]
                                          │
                                        reset ──▶ idle

The panel handshake (reset alarms → Auto) is folded into the *application's*
start procedure while the machine is in ``prestart_warning`` / ``starting``, so
a panel that is briefly unreachable while it is being prepared is expected, not
a fault. Comms loss is only treated as a fault once the engine is actually
``running``.

A start may also *begin* with no comms at all. Everything the warning does is
app-side, and a panel that is switched off — or booting on the app's own Pump
Power output — is exactly the case it exists to cover; the panel only has to be
reachable by the time ``starting`` runs out of ``start_timeout``.
"""

from __future__ import annotations

import logging
import time

from pydoover.state import StateMachine

log = logging.getLogger(__name__)


STATES = ["idle", "prestart_warning", "starting", "running", "stopping", "fault"]

TRANSITIONS = [
    {"trigger": "request_start", "source": "idle", "dest": "prestart_warning"},
    {"trigger": "cancel_warning", "source": "prestart_warning", "dest": "idle"},
    {"trigger": "warning_complete", "source": "prestart_warning", "dest": "starting"},
    {"trigger": "engine_running", "source": "starting", "dest": "running"},
    # Reconciliation: the engine is already running while we're idle (app
    # restart, or someone started it at the panel) — adopt it instead of
    # offering a Start that would re-warn a running engine.
    {"trigger": "adopt_running", "source": "idle", "dest": "running"},
    {"trigger": "request_stop", "source": ["starting", "running"], "dest": "stopping"},
    {"trigger": "engine_stopped", "source": "stopping", "dest": "idle"},
    {
        "trigger": "fault_detected",
        "source": ["prestart_warning", "starting", "running", "stopping"],
        "dest": "fault",
    },
    {"trigger": "reset", "source": "fault", "dest": "idle"},
    # Recovery: the engine is running again and we can see it, so resume control
    # instead of sitting in a fault that no longer describes reality. Only for the
    # faults a running engine disproves — see RECOVERABLE_FAULTS.
    {"trigger": "recover_running", "source": "fault", "dest": "running"},
]

# Fault reasons that a running engine disproves. A failed start or a panel fault is
# a real problem that wants a human, even if the engine later turns up running.
FAULT_ENGINE_STOPPED = "Engine stopped unexpectedly"
FAULT_LOST_COMMS = "Lost comms to panel while running"
# A start attempt that gave up, either because the panel never became ready to
# take one or because the engine never ran. Deliberately says nothing about
# which: an operator reading a dashboard warning wants to know the pump didn't
# start, and "panel not ready" is jargon about the Modbus Auto-mode handshake
# they can't act on anyway. Which of the two it was, and the timeout it hit, go
# to the log — where whoever is debugging it will look.
FAULT_START_FAILED = "Auto Start Failed"
RECOVERABLE_FAULTS = frozenset({FAULT_ENGINE_STOPPED, FAULT_LOST_COMMS})

# How long a not-running reading is tolerated, without faulting, while the panel
# looks freshly restarted (back from a comms outage and no longer in Auto). A
# power-cycled DSE boots reporting stopped out of Auto, so faulting on that reading
# declares a healthy pump dead — the gap before the panel reported running again
# was ~85s on both stations.
PANEL_RESTART_GRACE_S = 150.0

DISPLAY = {
    "idle": "Ready",
    "prestart_warning": "Pre-start warning",
    "starting": "Starting",
    "running": "Running",
    "stopping": "Stopping",
    "fault": "Fault",
}


class AutoStartController:
    """Holds the auto-start :class:`StateMachine` and the pure decision logic.

    The machine is wired with ``model=self`` so the generated trigger methods
    (``request_start`` …) and ``on_enter_<state>`` callbacks live on this
    instance, alongside the helper properties below.
    """

    # Attributes provided by the state machine at runtime.
    state: str

    def __init__(
        self,
        *,
        warning_seconds: float = 30.0,
        start_timeout: float = 180.0,
        stop_timeout: float = 120.0,
        panel_restart_grace: float = PANEL_RESTART_GRACE_S,
    ):
        self.warning_seconds = float(warning_seconds)
        self.start_timeout = float(start_timeout)
        self.stop_timeout = float(stop_timeout)
        self.panel_restart_grace = float(panel_restart_grace)
        self.fault_reason: str = ""
        self._entered_at: float = time.time()
        # Set while a not-running reading is being tolerated because the panel
        # looks freshly restarted. Surfaced in `display`, so "Running" can't
        # quietly mean "we think so but the panel disagrees".
        self.awaiting_panel: bool = False
        self._awaiting_since: float | None = None
        # Whether the current fault came from a start attempt (see _fault).
        self._start_failed: bool = False
        # When the start attempt's timeout budget began — see attempt_elapsed.
        self._attempt_started_at: float | None = None
        # Whether the panel has reported the engine running at any point since
        # the machine last sat idle. Read by `stopping` (see evaluate).
        self._seen_running: bool = False

        self.state_machine = StateMachine(
            states=STATES,
            transitions=TRANSITIONS,
            model=self,
            initial="idle",
            queued=True,
            auto_transitions=False,
            after_state_change="_stamp_enter",
        )

    # ------------------------------------------------------------------
    # State machine callbacks (auto-wired by name)
    # ------------------------------------------------------------------

    async def _stamp_enter(self) -> None:
        """Record when the current state was entered (drives all timers)."""
        self._entered_at = time.time()

    async def on_enter_idle(self) -> None:
        self.fault_reason = ""
        self._clear_awaiting()
        self._seen_running = False  # a fresh run episode starts from here
        self._attempt_started_at = None

    async def on_enter_prestart_warning(self) -> None:
        # The attempt's timeout budget runs from here — the operator's press —
        # so it covers the countdown as well as everything after it.
        self._attempt_started_at = time.time()

    async def on_enter_running(self) -> None:
        self.fault_reason = ""
        self._clear_awaiting()
        self._attempt_started_at = None  # the attempt succeeded

    def _clear_awaiting(self) -> None:
        self.awaiting_panel = False
        self._awaiting_since = None

    async def _fault(self, reason: str, *, start_failed: bool = False) -> None:
        """Fault with a reason, recording whether it was a start that gave up.

        The distinction matters outside this class: a start that gave up leaves an
        engine that definitely never ran, while a fault out of a run leaves one
        that may well still be turning. What the app dares say about the pump —
        and whether it keeps the panel powered — hangs on which it was.
        """
        self.fault_reason = reason
        self._start_failed = start_failed
        await self.fault_detected()

    # ------------------------------------------------------------------
    # Derived state used by the application each loop
    # ------------------------------------------------------------------

    @property
    def time_in_state(self) -> float:
        return time.time() - self._entered_at

    @property
    def warning_remaining(self) -> float | None:
        """Seconds left on the pre-start countdown.

        ``None`` when there is no number to give: not warning at all, or warning
        on past the countdown while the panel is got ready (see
        :attr:`awaiting_panel_ready`), where how long is left is genuinely
        unknown — it depends on how the panel's reset/reboot/Auto handshake goes.
        """
        if self.state != "prestart_warning":
            return None
        remaining = self.warning_seconds - self.time_in_state
        return remaining if remaining > 0 else None

    @property
    def attempt_elapsed(self) -> float:
        """Seconds spent on this start attempt, counting from the countdown's end.

        One budget covers the whole attempt — the countdown, getting the panel
        ready, and cranking — so it has to survive the ``prestart_warning →
        starting`` transition. ``time_in_state`` resets there, which would
        quietly hand each phase its own full ``start_timeout`` and multiply the
        worst case, and with it how long the horn sounds, since it sounds for
        all of it.

        Falls back to ``time_in_state`` if the attempt's start was never stamped,
        so the cap can never go missing altogether.
        """
        if self._attempt_started_at is None:
            return self.time_in_state
        return time.time() - self._attempt_started_at

    @property
    def awaiting_panel_ready(self) -> bool:
        """Warning held open past its countdown until the panel can take a start.

        The countdown is a floor on the warning, not a promise about when the
        engine cranks: the panel's Manual→Auto handshake reboots it for ~30s, and
        an engine that cranks after the horn has stopped is the exact thing the
        warning exists to prevent.
        """
        return (
            self.state == "prestart_warning"
            and self.time_in_state >= self.warning_seconds
        )

    @property
    def beacon_active(self) -> bool:
        """Beacon is steady-on for the whole warning and through cranking."""
        return self.state in ("prestart_warning", "starting")

    @property
    def horn_active(self) -> bool:
        """Whether the horn should sound — for as long as a start is being tried.

        Both warning states, not just the countdown. ``starting`` is where START
        commands are actually being written, so it is the window in which the
        engine can begin turning at any moment with no further notice — and it
        can last the whole of ``start_timeout`` while an engine fails to catch or
        a panel fails to answer. A horn that stops before that window closes
        leaves exactly the silent, engine-may-crank gap the warning exists to
        prevent, and it is the longest such gap in the whole sequence.

        This is the *intent*; the application turns it into the actual pulsed,
        cross-pump-coordinated horn output (see ``_drive_warning_outputs``).
        """
        return self.state in ("prestart_warning", "starting")

    @property
    def start_failed(self) -> bool:
        """Faulted because a start gave up, so nothing is running or about to.

        False in every other state, including a fault out of a run — there the
        engine may still be turning and the app must not say otherwise.
        """
        return self.state == "fault" and self._start_failed

    @property
    def is_active(self) -> bool:
        """True whenever the engine is being commanded (not idle / fault)."""
        return self.state in ("prestart_warning", "starting", "running", "stopping")

    @property
    def display(self) -> str:
        if self.state == "fault" and self.fault_reason:
            return f"Fault: {self.fault_reason}"
        if self.state == "running" and self.awaiting_panel:
            return "Running (waiting for the panel)"
        if self.awaiting_panel_ready:
            return "Pre-start warning (preparing panel)"
        return DISPLAY.get(self.state, self.state)

    # ------------------------------------------------------------------
    # The brain: fire at most one transition for the current inputs
    # ------------------------------------------------------------------

    def _tolerate_panel_restart(self, panel_restarting: bool) -> bool:
        """Whether to wait out a not-running reading instead of faulting.

        A DSE that has just been power-cycled boots reporting stopped out of Auto, so
        the first readings after a comms outage look identical to an engine that
        dropped out — and faulting on them takes a healthy, running pump out of
        remote control until somebody presses Start again. Waiting costs little: if
        the engine really has stopped, the grace period expires and the fault lands
        anyway.
        """
        if not panel_restarting:
            self._clear_awaiting()
            return False
        now = time.time()
        if self._awaiting_since is None:
            self._awaiting_since = now
            self.awaiting_panel = True
            log.warning(
                "Panel reports stopped in Manual just after a comms outage — "
                "waiting up to %.0fs for it to come back before faulting",
                self.panel_restart_grace,
            )
        if now - self._awaiting_since < self.panel_restart_grace:
            return True
        log.warning(
            "Panel did not come back within %.0fs — treating the engine as stopped",
            self.panel_restart_grace,
        )
        self._clear_awaiting()
        return False

    async def evaluate(
        self,
        *,
        lost_comms: bool,
        is_running: bool,
        is_faulted: bool,
        run_request: bool,
        panel_restarting: bool = False,
        panel_ready: bool = True,
    ) -> None:
        """Advance the machine one step from the latest panel snapshot.

        Parameters
        ----------
        lost_comms:
            Debounced comms-loss flag (a single dropped poll should be False).
        is_running / is_faulted:
            Panel-reported engine state from the status word + fault code.
        run_request:
            Combined run demand (operator latch OR cross-app request tag).
        panel_restarting:
            Whether the panel looks freshly restarted rather than genuinely
            stopped — back from a comms outage and no longer in Auto. Defers the
            not-running fault for ``panel_restart_grace`` rather than declaring a
            running pump dead.
        panel_ready:
            Whether the panel could take a start command right now (in Auto,
            unfaulted). The pre-start warning is held open past its countdown
            until this is true, so the horn is still sounding when the engine
            actually cranks; ``start_timeout`` caps the wait. Defaults True for
            callers that don't model the panel handshake.
        """
        s = self.state
        if is_running:
            self._seen_running = True

        if s == "idle":
            # Deliberately not gated on comms. The pre-start warning is entirely
            # app-side — beacon, horn, announcement, countdown — so it needs no
            # panel, and a panel switched off (or on its way up on the app's own
            # power output) is exactly when a start has to be able to begin. The
            # safety cap is `starting`'s start_timeout: if the panel never comes
            # up, the attempt aborts to a visible fault instead of the operator
            # pressing Start and watching nothing happen at all.
            if run_request:
                await self.request_start()

        elif s == "prestart_warning":
            # Comms / panel faults here are expected — the app is resetting the
            # panel into Auto. Only the operator (or a cleared request) aborts.
            if not run_request:
                await self.cancel_warning()
            elif self.time_in_state < self.warning_seconds:
                pass  # the countdown is a floor — it always runs in full
            elif panel_ready or is_running:
                # Ready to crank — or already running, because someone started it
                # at the panel while we warned. Either way there is nothing left
                # to hold the warning open for.
                await self.warning_complete()
            elif self.attempt_elapsed >= self.start_timeout:
                # Checked only past the countdown, so the countdown stays a floor
                # that always runs in full. A budget shorter than the countdown
                # therefore faults the instant it ends — which is a config that
                # can never start an engine, and is flagged at startup.
                log.warning(
                    "Panel never became ready to take a start within %.0fs "
                    "(reset → reboot → Auto never completed) — giving up.",
                    self.start_timeout,
                )
                await self._fault(FAULT_START_FAILED, start_failed=True)

        elif s == "starting":
            if not run_request:
                await self.request_stop()
            elif is_running:
                await self.engine_running()
            elif self.attempt_elapsed >= self.start_timeout:
                # The same budget the held-open warning was drawing on: one for
                # the whole attempt, not one per phase.
                log.warning(
                    "Engine never reported running within %.0fs of the start "
                    "attempt beginning — giving up. The panel was reachable and "
                    "in Auto, so look at the engine: fuel, battery, starter.",
                    self.start_timeout,
                )
                await self._fault(FAULT_START_FAILED, start_failed=True)

        elif s == "running":
            if not run_request:
                self._clear_awaiting()
                await self.request_stop()
            elif is_faulted:
                await self._fault("Panel fault while running")
            elif not is_running:
                if self._tolerate_panel_restart(panel_restarting):
                    return  # hold `running` and wait for the panel to come back
                await self._fault(FAULT_ENGINE_STOPPED)
            elif lost_comms:
                await self._fault(FAULT_LOST_COMMS)
            else:
                self._clear_awaiting()

        elif s == "fault":
            # The engine is running, we can see it, and it is still wanted — so
            # whatever we concluded earlier is no longer true. Resume control
            # rather than leave a running pump unregulated pending a button press.
            if (
                run_request
                and is_running
                and not lost_comms
                and self.fault_reason in RECOVERABLE_FAULTS
            ):
                log.warning(
                    "Auto-start recovering from fault (%s): the engine is running "
                    "again and comms are healthy",
                    self.fault_reason,
                )
                await self.recover_running()

        elif s == "stopping":
            # Without comms we can't watch the engine wind down — unless we never
            # saw it running in the first place, in which case there is nothing to
            # wait for. That is the ordinary shape of a start aborted on a panel
            # that never answered, and timing it out into "Engine did not stop"
            # would fault a pump that never turned over.
            if not is_running and (not lost_comms or not self._seen_running):
                await self.engine_stopped()
            elif self.time_in_state >= self.stop_timeout:
                await self._fault(
                    f"Engine did not stop within {int(self.stop_timeout)}s"
                )

        # state == "fault": waits for an explicit reset() from the UI handler.
