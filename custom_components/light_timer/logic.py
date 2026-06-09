"""Pure decision-logic core for the Light Timer integration.

This module is the side-effect-free core of the integration. It MUST NOT import
anything from Home Assistant: every function here is deterministic and depends
only on its arguments, which makes the logic verifiable in isolation with
Hypothesis property-based tests.

The module is built up incrementally across several tasks. This file currently
provides:

* the shared types (:class:`LightTimerState`, :class:`ValidationResult`,
  :class:`TimerEvent`, :class:`DecisionContext`), and
* the start decision (:func:`should_start_timer`).

Subsequent tasks extend this module with light-state sequence evaluation, the
``next_state`` transition function, the duration/suspension validators, the
``format_remaining`` formatter, the pure sensor-derivation helper, and the
multi-controller independence model. Keep additions pure and HA-free.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum

from .const import MAX_DURATION_SECONDS, MIN_DURATION_SECONDS

__all__ = [
    "LightTimerState",
    "ValidationResult",
    "TimerEvent",
    "DecisionContext",
    "should_start_timer",
    "LightObservation",
    "SequenceEvaluation",
    "evaluate_light_sequence",
    "RUNNING_CLASS_STATES",
    "next_state",
    "validate_duration",
    "validate_suspension",
    "format_remaining",
    "ControllerRuntimeState",
    "SensorRepresentation",
    "derive_sensor",
    "ControllerOperation",
    "apply_controller_operation",
]


class LightTimerState(Enum):
    """Lifecycle states of a single managed light's timer.

    The state machine that consumes these values is defined by ``next_state``
    (added in a later task). Per Requirement 1.4, a timer is treated as
    "running" from the moment it reaches zero until the light is confirmed off;
    the :attr:`COMMANDING_OFF` and :attr:`FAILED` states model that window.
    """

    IDLE = "idle"
    RUNNING = "running"
    COMMANDING_OFF = "commanding_off"
    FAILED = "failed"
    SUSPENDED = "suspended"
    DISABLED = "disabled"


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating a duration or suspension input.

    Attributes:
        ok: ``True`` when the submitted value was accepted.
        value: On acceptance, the accepted value; on rejection, the value that
            is retained (typically the previously configured value).
        error: ``None`` on success, otherwise the error classification:
            ``"range"`` for an integer outside ``[1, 86400]`` or ``"integer"``
            for a non-integer input (including whole-valued floats such as
            ``300.0``, other floats, strings, ``None`` and ``bool``).
    """

    ok: bool
    value: int | None
    error: str | None


@dataclass(frozen=True)
class TimerEvent:
    """A discrete event that can drive a timer state transition.

    Attributes:
        kind: The event discriminator. One of ``"on_edge"``, ``"off"``,
            ``"expire"``, ``"retry_fail"``, ``"confirm_off"``, ``"user_cancel"``,
            ``"suspend"``, ``"suspend_end"``, ``"disable"`` or ``"enable"``.
    """

    kind: str


@dataclass(frozen=True)
class DecisionContext:
    """Immutable boolean context for pure transition decisions.

    The fields capture the conditions that govern whether a timer should be
    running for a light. ``next_state`` (added in a later task) reads this
    context when an event's effect depends on the surrounding conditions.

    Attributes:
        light_on: Whether the managed light is currently on.
        timer_running: Whether a timer is already running for the light.
        suspended: Whether a suspension period is currently active.
        disabled: Whether the light is individually disabled.
        globally_enabled: Whether the integration's master switch is enabled.
    """

    light_on: bool = False
    timer_running: bool = False
    suspended: bool = False
    disabled: bool = False
    globally_enabled: bool = True


def should_start_timer(
    light_on: bool,
    timer_running: bool,
    suspended: bool,
    disabled: bool,
    globally_enabled: bool,
) -> bool:
    """Decide whether a countdown should start for a managed light.

    This single decision governs every place a timer can begin: the initial
    off-to-on start, re-arm on restart, suspension-end resume, per-light
    re-enable resume, master-switch resume, and add-completion start.

    Returns ``True`` if and only if the light is on, no timer is already
    running for it, no suspension is active, the light is not disabled, and the
    integration is globally enabled. Every other combination returns ``False``.
    """
    return (
        light_on
        and not timer_running
        and not suspended
        and not disabled
        and globally_enabled
    )


@dataclass(frozen=True)
class LightObservation:
    """A single point-in-time observation of a managed light.

    A sequence of these observations models the history of a light's on/off
    state together with any user cancellations that occurred along the way.

    Attributes:
        light_on: Whether the light is on at this observation.
        cancelled: Whether the user cancelled the running timer at this
            observation. A cancellation stops the current countdown but does
            **not** change the light's on/off state; per Requirement 5.3 it must
            never, on its own, start a new countdown while the light stays on.
    """

    light_on: bool
    cancelled: bool = False


@dataclass(frozen=True)
class SequenceEvaluation:
    """Result of evaluating a sequence of light-state observations.

    Attributes:
        countdown_started: ``True`` if at least one countdown start occurred
            anywhere in the sequence.
        start_count: The number of countdown starts triggered by off-to-on
            transitions across the whole sequence.
        timer_running: Whether a countdown is considered running after the
            final observation.
    """

    countdown_started: bool
    start_count: int
    timer_running: bool


def evaluate_light_sequence(
    observations: Iterable[LightObservation],
) -> SequenceEvaluation:
    """Evaluate a light-state observation sequence for countdown starts.

    A countdown starts for a managed light **only** on an off-to-on transition:
    an observation where the light is on and the immediately preceding
    observation had the light off. Consequently:

    * A sequence that is steady-on or steady-off throughout contains no
      off-to-on edge and therefore starts no countdown (Property 2).
    * A leading on-observation with no preceding off-observation is not an edge
      and does not start a countdown.
    * A user cancellation stops the current countdown but, on its own, never
      starts a new one. After a cancel while the light remains continuously on,
      no countdown starts until the light goes off and back on (Requirement
      5.3).

    Args:
        observations: The ordered sequence of light-state observations.

    Returns:
        A :class:`SequenceEvaluation` summarising whether (and how many times) a
        countdown started and whether a countdown is running at the end.
    """
    start_count = 0
    timer_running = False
    prev_on: bool | None = None

    for obs in observations:
        if obs.light_on:
            # An off-to-on transition is the only trigger for a countdown start.
            if prev_on is False and not timer_running:
                start_count += 1
                timer_running = True
            # A cancellation stops any running timer without starting a new one
            # and without changing the light's (still on) state.
            if obs.cancelled:
                timer_running = False
        else:
            # The light is off: any running countdown stops.
            timer_running = False

        prev_on = obs.light_on

    return SequenceEvaluation(
        countdown_started=start_count > 0,
        start_count=start_count,
        timer_running=timer_running,
    )


# States in which a timer is classified as "running" for the purposes of the
# off-transition rule. Per Requirement 1.4 a timer remains "running" from the
# moment it reaches zero (``COMMANDING_OFF``) and even while a shutoff failure
# is active (``FAILED``) until the light is confirmed off.
RUNNING_CLASS_STATES: frozenset[LightTimerState] = frozenset(
    {
        LightTimerState.RUNNING,
        LightTimerState.COMMANDING_OFF,
        LightTimerState.FAILED,
    }
)


def next_state(
    current: LightTimerState,
    event: TimerEvent,
    ctx: DecisionContext | None = None,
) -> LightTimerState:
    """Compute the next timer state from the current state and an event.

    This is a pure transition function: the result depends only on ``current``,
    ``event.kind`` and the supplied :class:`DecisionContext`. It encodes the
    per-light state machine from the design document.

    The transitions covered are:

    * ``expire`` from :attr:`~LightTimerState.RUNNING` yields
      :attr:`~LightTimerState.COMMANDING_OFF` -- the timer stays classified as
      running until the light is confirmed off (Req 1.4).
    * ``off`` from any running-class state
      (:attr:`~LightTimerState.RUNNING`, :attr:`~LightTimerState.COMMANDING_OFF`,
      :attr:`~LightTimerState.FAILED`) yields :attr:`~LightTimerState.IDLE`,
      regardless of what caused the off transition (Req 1.9, 2.1).
    * ``off`` from any non-running state leaves the state unchanged (Req 2.3).
    * ``user_cancel`` from :attr:`~LightTimerState.RUNNING` yields
      :attr:`~LightTimerState.IDLE` (Req 5.1).
    * ``disable`` from any state yields :attr:`~LightTimerState.DISABLED`
      (Req 7.1, 7.2).
    * ``enable`` from any state yields :attr:`~LightTimerState.IDLE`, clearing
      any active suspension or disabled status (Req 7.4).

    Other events are handled per the design state diagram: ``confirm_off``
    confirms a shutoff (commanding/failed -> idle), ``retry_fail`` moves
    ``COMMANDING_OFF`` to :attr:`~LightTimerState.FAILED`, ``suspend`` moves a
    non-running, non-disabled light to :attr:`~LightTimerState.SUSPENDED`,
    ``suspend_end`` returns a suspended light to :attr:`~LightTimerState.IDLE`,
    and ``on_edge`` starts a countdown from a non-running state when the context
    permits it. Any event that does not apply to ``current`` leaves the state
    unchanged.

    Args:
        current: The current state of the light's timer.
        event: The event driving the transition.
        ctx: Optional decision context. When omitted a default context is used;
            it is only consulted for the ``on_edge`` start decision.

    Returns:
        The resulting :class:`LightTimerState`.
    """
    if ctx is None:
        ctx = DecisionContext()

    kind = event.kind

    # ``disable`` and ``enable`` are global overrides that apply from any state.
    if kind == "disable":
        return LightTimerState.DISABLED
    if kind == "enable":
        # Re-enabling clears any suspension/disabled status and returns to idle.
        return LightTimerState.IDLE

    # An ``off`` transition clears any running-class timer back to idle and is a
    # no-op for every non-running state.
    if kind == "off":
        if current in RUNNING_CLASS_STATES:
            return LightTimerState.IDLE
        return current

    if kind == "expire":
        if current == LightTimerState.RUNNING:
            return LightTimerState.COMMANDING_OFF
        return current

    if kind == "user_cancel":
        if current == LightTimerState.RUNNING:
            return LightTimerState.IDLE
        return current

    if kind == "confirm_off":
        # The light was confirmed off while commanding/failed: stop the timer.
        if current in (
            LightTimerState.COMMANDING_OFF,
            LightTimerState.FAILED,
        ):
            return LightTimerState.IDLE
        return current

    if kind == "retry_fail":
        # Retries exhausted while commanding off: enter the failure state.
        if current == LightTimerState.COMMANDING_OFF:
            return LightTimerState.FAILED
        return current

    if kind == "suspend":
        # A suspension takes effect from any non-running, non-disabled state.
        if current in (LightTimerState.IDLE, LightTimerState.SUSPENDED):
            return LightTimerState.SUSPENDED
        return current

    if kind == "suspend_end":
        if current == LightTimerState.SUSPENDED:
            return LightTimerState.IDLE
        return current

    if kind == "on_edge":
        # Start a countdown on an off->on edge only when the conditions permit.
        if current not in RUNNING_CLASS_STATES and should_start_timer(
            light_on=ctx.light_on,
            timer_running=current in RUNNING_CLASS_STATES,
            suspended=ctx.suspended or current == LightTimerState.SUSPENDED,
            disabled=ctx.disabled or current == LightTimerState.DISABLED,
            globally_enabled=ctx.globally_enabled,
        ):
            return LightTimerState.RUNNING
        return current

    # Unknown / inapplicable event: leave the state unchanged.
    return current


def validate_duration(submitted: object, current: int | None) -> ValidationResult:
    """Validate a submitted timer-duration value against the shared rules.

    A duration is valid **if and only if** it is a true integer within the
    inclusive range ``[MIN_DURATION_SECONDS, MAX_DURATION_SECONDS]`` (``[1,
    86400]``). The rules resolve the historical int-vs-float ambiguity by
    accepting only genuine integers:

    * An ``int`` in ``[1, 86400]`` is accepted; the result carries the submitted
      value and no error (Req 3.1).
    * An ``int`` outside ``[1, 86400]`` is rejected with error ``"range"``; the
      ``current`` value is retained (Req 3.3).
    * Anything that is not a true integer -- whole-valued floats such as
      ``300.0``, other floats, strings, ``None`` -- is rejected with error
      ``"integer"`` and the ``current`` value is retained (Req 3.4).
    * ``bool`` (``True``/``False``) is explicitly **rejected** as a non-integer
      with error ``"integer"`` even though ``bool`` is a subclass of ``int`` in
      Python, keeping validation strict and consistent (Req 9.9).

    On rejection the previously configured ``current`` value is retained so the
    config/options flow never loses the prior setting.

    Args:
        submitted: The raw value submitted by the user (any type).
        current: The previously configured duration to retain on rejection.

    Returns:
        A :class:`ValidationResult` describing acceptance or the rejection
        classification.
    """
    # ``bool`` is an ``int`` subclass in Python, so it must be excluded before
    # the ``isinstance(..., int)`` check or ``True``/``False`` would slip
    # through as 1/0. Non-int types (floats incl. whole-valued, str, None) are
    # all non-integers.
    if isinstance(submitted, bool) or not isinstance(submitted, int):
        return ValidationResult(ok=False, value=current, error="integer")

    if submitted < MIN_DURATION_SECONDS or submitted > MAX_DURATION_SECONDS:
        return ValidationResult(ok=False, value=current, error="range")

    return ValidationResult(ok=True, value=submitted, error=None)


def validate_suspension(submitted: object, current: int | None) -> ValidationResult:
    """Validate a submitted suspension-period value against the shared rules.

    Suspension validation applies the **identical** rules as
    :func:`validate_duration` (Req 9.9 resolves the historical int-vs-float
    divergence in one place): a value is valid **if and only if** it is a true
    integer within the inclusive range ``[MIN_DURATION_SECONDS,
    MAX_DURATION_SECONDS]`` (``[1, 86400]``).

    * An ``int`` in ``[1, 86400]`` is accepted; the result carries the submitted
      value and no error (Req 6.1).
    * An ``int`` outside ``[1, 86400]`` is rejected with error ``"range"``; the
      active suspension period (``current``) is left unchanged (Req 6.3).
    * Anything that is not a true integer -- whole-valued floats such as
      ``3600.0``, other floats, strings, ``None`` -- is rejected with error
      ``"integer"`` and the active suspension period is left unchanged (Req 6.3).
    * ``bool`` (``True``/``False``) is explicitly **rejected** as a non-integer
      with error ``"integer"`` even though ``bool`` is a subclass of ``int`` in
      Python (Req 9.9).

    On rejection the ``current`` value -- representing any active suspension
    period -- is retained unchanged so an in-flight suspension is never
    disturbed by an invalid submission (Req 6.3).

    Args:
        submitted: The raw value submitted by the user (any type).
        current: The active suspension period to leave unchanged on rejection.

    Returns:
        A :class:`ValidationResult` describing acceptance or the rejection
        classification.
    """
    # The acceptance/rejection rules are identical to duration validation;
    # delegate to keep the single source of truth. On rejection ``validate_duration``
    # returns ``value=current`` (the active suspension period), leaving it
    # unchanged as required by Req 6.3.
    return validate_duration(submitted, current)


def format_remaining(total_seconds: int) -> str:
    """Format a remaining-time duration in seconds as ``"M:SS"``.

    The output is the canonical minutes-and-seconds dashboard form (Req 4.1,
    4.3): the whole number of minutes with no leading zero, a colon, and the
    leftover seconds zero-padded to two digits in the range ``00``-``59``.

    The formatting round-trips exactly: parsing the output back yields the
    original value, i.e. ``M * 60 + SS == total_seconds`` (Property 6). In
    particular ``format_remaining(0) == "0:00"``.

    Args:
        total_seconds: A non-negative whole number of seconds remaining. A timer
            that has reached zero is rendered as ``"0:00"``.

    Returns:
        The remaining time formatted as ``"M:SS"`` with zero-padded seconds.

    Raises:
        TypeError: If ``total_seconds`` is not a true integer (``bool`` is an
            ``int`` subclass and is rejected for consistency with the validators).
        ValueError: If ``total_seconds`` is negative.
    """
    # ``bool`` is an ``int`` subclass; exclude it so ``True``/``False`` never
    # masquerade as 1/0, mirroring the strictness of the duration validators.
    if isinstance(total_seconds, bool) or not isinstance(total_seconds, int):
        raise TypeError("total_seconds must be an int")
    if total_seconds < 0:
        raise ValueError("total_seconds must be non-negative")

    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


@dataclass(frozen=True)
class ControllerRuntimeState:
    """Immutable snapshot of a single per-light controller's runtime state.

    This is the pure-core view of the per-controller runtime values described in
    the design's data model. It carries only what the sensor representation is
    derived from, with no Home Assistant types involved.

    Attributes:
        state: The current :class:`LightTimerState` of the controller.
        remaining_seconds: The countdown value in seconds. Only meaningful while
            the timer is in a running-class state; for non-running states the
            derived sensor reports ``0`` regardless of this field.
        timer_duration: The configured timer duration in seconds for the light.
        suspension_remaining: The remaining suspension time in seconds; ``0``
            exactly when no suspension is active for the light.
    """

    state: LightTimerState
    remaining_seconds: int
    timer_duration: int
    suspension_remaining: int = 0


@dataclass(frozen=True)
class SensorRepresentation:
    """The pure derived representation backing a light's timer sensor entity.

    Attributes:
        state: The numeric sensor state in seconds: the remaining seconds while
            a timer is running, and ``0`` for every non-running state (idle,
            suspended, disabled).
        attributes: The sensor's exposed attributes. Always includes
            ``timer_duration``, ``enabled`` (``False`` exactly when the light is
            disabled), ``suspension_remaining`` (``0`` exactly when no suspension
            is active), ``failure_active`` (``True`` exactly in the ``FAILED``
            state), and ``formatted_remaining`` (the numeric state rendered as
            ``"M:SS"`` via :func:`format_remaining`).
    """

    state: int
    attributes: dict[str, object]


def derive_sensor(runtime: ControllerRuntimeState) -> SensorRepresentation:
    """Derive the pure sensor representation from a controller runtime state.

    This is a side-effect-free projection of a :class:`ControllerRuntimeState`
    onto the values an HA sensor entity exposes, so the derivation can be
    property-tested in isolation (Property 7).

    The numeric state is the remaining seconds **only** while the timer is in a
    running-class state (:attr:`~LightTimerState.RUNNING`,
    :attr:`~LightTimerState.COMMANDING_OFF`, :attr:`~LightTimerState.FAILED`);
    for every non-running state (idle, suspended, disabled) the state is ``0``
    (Req 10.5, 10.6). A negative ``remaining_seconds`` is clamped to ``0`` so the
    sensor never reports a nonsensical countdown.

    The attributes always include (Req 10.4):

    * ``timer_duration`` -- the configured duration in seconds;
    * ``enabled`` -- ``False`` exactly when the state is
      :attr:`~LightTimerState.DISABLED`, otherwise ``True``;
    * ``suspension_remaining`` -- the remaining suspension seconds, ``0`` exactly
      when no suspension is active (clamped to ``0`` if negative);
    * ``failure_active`` -- ``True`` exactly in the
      :attr:`~LightTimerState.FAILED` state;
    * ``formatted_remaining`` -- the numeric state rendered as ``"M:SS"`` via
      :func:`format_remaining`.

    Args:
        runtime: The controller runtime snapshot to project.

    Returns:
        The derived :class:`SensorRepresentation`.
    """
    if runtime.state in RUNNING_CLASS_STATES:
        # While running, the state is the remaining countdown (never negative).
        numeric_state = max(0, runtime.remaining_seconds)
    else:
        # Idle, suspended, disabled: no timer is running, so the state is 0.
        numeric_state = 0

    suspension_remaining = max(0, runtime.suspension_remaining)

    attributes: dict[str, object] = {
        "timer_duration": runtime.timer_duration,
        "enabled": runtime.state != LightTimerState.DISABLED,
        "suspension_remaining": suspension_remaining,
        "failure_active": runtime.state == LightTimerState.FAILED,
        "formatted_remaining": format_remaining(numeric_state),
    }

    return SensorRepresentation(state=numeric_state, attributes=attributes)


@dataclass(frozen=True)
class ControllerOperation:
    """A single targeted operation applied to one controller in a collection.

    The operation identifies its target by light id and carries the operation
    discriminator plus an optional payload. It is the pure-core representation of
    the per-light events and configuration changes enumerated by Requirement 11
    (Property 8).

    Attributes:
        light_id: The id of the controller the operation targets. An id that is
            not present in the collection is a no-op: the collection is returned
            unchanged.
        kind: The operation discriminator. One of:

            * ``"start"`` -- begin a countdown: enter
              :attr:`~LightTimerState.RUNNING` with ``remaining_seconds`` reset to
              the controller's ``timer_duration``;
            * ``"stop"`` -- the light went off: clear a running-class timer back
              to :attr:`~LightTimerState.IDLE` with ``remaining_seconds`` ``0``;
            * ``"expire"`` -- the countdown reached zero: move
              :attr:`~LightTimerState.RUNNING` to
              :attr:`~LightTimerState.COMMANDING_OFF` and clamp
              ``remaining_seconds`` to ``0``;
            * ``"cancel"`` -- user cancellation: a running timer returns to
              :attr:`~LightTimerState.IDLE` with ``remaining_seconds`` ``0``;
            * ``"suspend"`` -- begin/replace a suspension: enter
              :attr:`~LightTimerState.SUSPENDED` and set
              ``suspension_remaining`` from ``payload`` (defaulting to the current
              value when no payload is supplied);
            * ``"set_duration"`` -- change the configured ``timer_duration`` to
              ``payload`` without otherwise altering state;
            * ``"set_suspension"`` -- set the active ``suspension_remaining`` to
              ``payload`` without changing state;
            * ``"enable"`` -- (re)enable: return to
              :attr:`~LightTimerState.IDLE`, clearing any disabled/suspended
              status and ``suspension_remaining``;
            * ``"disable"`` -- disable: enter :attr:`~LightTimerState.DISABLED`
              with ``remaining_seconds`` ``0``.
        payload: An optional integer payload. Used as the new duration for
            ``"set_duration"`` and as the suspension seconds for ``"suspend"`` and
            ``"set_suspension"``. Ignored by every other operation kind.
    """

    light_id: str
    kind: str
    payload: int | None = None


def _apply_operation_to_controller(
    runtime: ControllerRuntimeState,
    operation: ControllerOperation,
) -> ControllerRuntimeState:
    """Apply a single operation to one controller, returning a new snapshot.

    This is the per-controller core of :func:`apply_controller_operation`. It
    never mutates ``runtime`` -- :class:`ControllerRuntimeState` is a frozen
    dataclass, so every result is produced via :func:`dataclasses.replace`.
    """
    kind = operation.kind

    if kind == "start":
        # Begin a countdown: reset remaining to the configured duration.
        return replace(
            runtime,
            state=LightTimerState.RUNNING,
            remaining_seconds=runtime.timer_duration,
        )

    if kind == "stop":
        # The light went off: clear a running-class timer back to idle.
        new_state = next_state(runtime.state, TimerEvent("off"))
        if new_state == LightTimerState.IDLE and runtime.state in RUNNING_CLASS_STATES:
            return replace(runtime, state=new_state, remaining_seconds=0)
        return replace(runtime, state=new_state)

    if kind == "expire":
        # Countdown reached zero: command off, clamp remaining to 0.
        new_state = next_state(runtime.state, TimerEvent("expire"))
        if new_state != runtime.state:
            return replace(runtime, state=new_state, remaining_seconds=0)
        return runtime

    if kind == "cancel":
        # User cancellation: a running timer returns to idle, remaining 0.
        new_state = next_state(runtime.state, TimerEvent("user_cancel"))
        if new_state != runtime.state:
            return replace(runtime, state=new_state, remaining_seconds=0)
        return runtime

    if kind == "suspend":
        new_state = next_state(runtime.state, TimerEvent("suspend"))
        if new_state == LightTimerState.SUSPENDED:
            seconds = (
                operation.payload
                if operation.payload is not None
                else runtime.suspension_remaining
            )
            return replace(
                runtime,
                state=new_state,
                remaining_seconds=0,
                suspension_remaining=seconds,
            )
        return runtime

    if kind == "set_duration":
        # Reconfigure the timer duration only; leave runtime state untouched.
        if operation.payload is None:
            return runtime
        return replace(runtime, timer_duration=operation.payload)

    if kind == "set_suspension":
        # Set the active suspension period only; leave runtime state untouched.
        if operation.payload is None:
            return runtime
        return replace(runtime, suspension_remaining=operation.payload)

    if kind == "enable":
        # (Re)enable: clear disabled/suspended status and any suspension time.
        return replace(
            runtime,
            state=next_state(runtime.state, TimerEvent("enable")),
            suspension_remaining=0,
        )

    if kind == "disable":
        # Disable: stop any running timer and mark disabled.
        return replace(
            runtime,
            state=next_state(runtime.state, TimerEvent("disable")),
            remaining_seconds=0,
        )

    # Unknown operation kind: leave the controller unchanged.
    return runtime


def apply_controller_operation(
    controllers: Mapping[str, ControllerRuntimeState],
    operation: ControllerOperation,
) -> dict[str, ControllerRuntimeState]:
    """Apply a single targeted operation to one controller in a collection.

    This is the pure multi-controller independence model behind Property 8. It
    takes a collection of per-light controller states keyed by light id and a
    single :class:`ControllerOperation` targeting exactly one light, and returns
    a **new** collection in which only the targeted controller may differ.

    The function never mutates its inputs: the returned mapping is a fresh
    ``dict`` and every :class:`ControllerRuntimeState` is a frozen dataclass, so
    each non-targeted controller is carried over by identity and is therefore
    structurally identical before and after the operation (Req 11.2, 11.3, 11.4,
    11.5). If ``operation.light_id`` is not present in ``controllers``, the
    collection is returned unchanged (copied).

    Args:
        controllers: The current collection of controller states keyed by light
            id.
        operation: The single targeted operation to apply.

    Returns:
        A new ``dict`` mapping light id to controller state, identical to
        ``controllers`` except for the targeted controller.
    """
    # Build a fresh mapping. Non-targeted controllers are carried over by
    # reference; since ControllerRuntimeState is frozen, this guarantees they are
    # structurally unchanged without copying.
    updated: dict[str, ControllerRuntimeState] = dict(controllers)

    target = updated.get(operation.light_id)
    if target is None:
        # No such controller: nothing to change.
        return updated

    updated[operation.light_id] = _apply_operation_to_controller(target, operation)
    return updated
