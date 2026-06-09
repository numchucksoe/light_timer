"""Coordinator data structures for the Light Timer integration.

This module is the side-effect layer of the integration: it owns the per-light
runtime state and (in later tasks) drives timers, the retry loop, suspension,
and enable/disable handling via the Home Assistant runtime.

This file currently provides only the data structures used by that layer:

* :class:`ManagedLightConfig` -- the immutable-ish configuration of a single
  managed light, mirroring an entry in the config entry's ``lights`` list
  (see the design's "Config Entry Shape" / ``ManagedLightConfig``); and
* :class:`PerLightController` -- the mutable per-light runtime holder that
  carries the live state machine value, the remaining countdown, the active
  suspension period, the shutoff-failure flag, the scheduled-countdown cancel
  callback, and the retry counter.

The full ``LightTimerCoordinator`` class and its methods (state-change handling,
timer start, the commanding-off retry loop, cancel/suspend, enable/disable and
restart re-arm) are added in subsequent tasks (5.2+). Keep this module's runtime
state HA-free where practical: ``_cancel_cb`` is typed only as a callable so the
data structures can be exercised without a Home Assistant runtime.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SERVICE_TURN_OFF, STATE_OFF, STATE_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)

from .const import (
    DEFAULT_ENABLED,
    DEFAULT_GLOBALLY_ENABLED,
    DEFAULT_SUSPENSION,
    DEFAULT_TIMER_DURATION,
    DOMAIN,
)
from .logic import (
    RUNNING_CLASS_STATES,
    LightTimerState,
    TimerEvent,
    next_state,
    should_start_timer,
    validate_suspension,
)

_LOGGER = logging.getLogger(__name__)

# Entity domain of the managed lights and the service used to turn them off.
_LIGHT_DOMAIN = "light"

# Commanding-off retry tuning (Req 1.5). After the initial ``light.turn_off`` a
# confirmation check runs every ``_OFF_RETRY_INTERVAL`` seconds; while the light
# is still on the command is re-issued up to ``_MAX_OFF_RETRIES`` times before
# the controller enters the failure state.
_OFF_RETRY_INTERVAL: float = 5
_MAX_OFF_RETRIES = 3

# Dispatcher signal fired each second while any countdown is active, so
# sensors can push state updates to the UI.
SIGNAL_TIMER_TICK = f"{DOMAIN}_timer_tick"


@dataclass
class ManagedLightConfig:
    """Configuration for a single managed light.

    Mirrors one entry in the config entry's ``options["lights"]`` list (see the
    design's "Config Entry Shape"). The ``light_entity_id`` is the immutable
    identity of the managed light (it cannot be changed after the light is
    added, per Req 9.5); the remaining fields are editable through the options
    flow.

    Attributes:
        light_entity_id: The ``light.*`` entity id this configuration manages.
        timer_duration: The countdown length in seconds (integer in
            ``[1, 86400]``) used when a timer starts for this light (Req 9.2).
        default_suspension: The default suspension period in seconds (integer in
            ``[1, 86400]``) applied when a suspension is triggered without a
            custom duration (Req 6.8, 9.2).
        notification_service: The optional notification service called on a
            shutoff failure, or ``None`` when no service is configured (Req 9.2).
        enabled: Whether timer automation is enabled for this light; defaults to
            enabled (Req 9.2).
    """

    light_entity_id: str
    timer_duration: int = DEFAULT_TIMER_DURATION
    default_suspension: int = DEFAULT_SUSPENSION
    notification_service: str | None = None
    enabled: bool = DEFAULT_ENABLED


@dataclass
class PerLightController:
    """Mutable per-light runtime state holder.

    Each managed light owns one controller; the coordinator (added in later
    tasks) never mutates one controller while handling another's event, which is
    what gives the integration its per-light independence (Req 11). This holder
    carries only runtime state and a reference to the light's configuration; the
    behaviour that mutates it lives on the coordinator.

    Per Requirement 1.4 a timer is treated as "running" from the moment it
    reaches zero until the light is confirmed off, so ``state`` may be in a
    running-class state (``RUNNING``/``COMMANDING_OFF``/``FAILED``) while the
    light is still on.

    Attributes:
        light_entity_id: The ``light.*`` entity id this controller manages.
        config: The :class:`ManagedLightConfig` for this light.
        state: The current :class:`LightTimerState`; starts :attr:`IDLE`.
        remaining_seconds: The live countdown value in seconds; ``0`` when no
            timer is running. Source of the sensor state (Req 10.4, 10.5).
        suspension_remaining: The remaining active suspension period in seconds;
            ``0`` when no suspension is active.
        failure_active: ``True`` while a shutoff failure is being indicated for
            this light (Req 1.7).
        _cancel_cb: The callback returned by ``async_call_later`` that cancels
            the scheduled countdown, or ``None`` when none is scheduled.
        _suspend_cancel_cb: The callback returned by ``async_call_later`` that
            cancels the scheduled suspension-end resume, or ``None`` when no
            suspension is scheduled. Tracked separately from ``_cancel_cb`` so a
            suspension can be replaced or cleared independently of any countdown
            or retry callback (Req 6.2, 6.4).
        _retry_count: The number of off-command retries issued so far for the
            in-progress commanding-off sequence (Req 1.5).
    """

    light_entity_id: str
    config: ManagedLightConfig
    state: LightTimerState = LightTimerState.IDLE
    remaining_seconds: int = 0
    suspension_remaining: int = 0
    failure_active: bool = False
    _cancel_cb: Callable[[], None] | None = field(default=None, repr=False)
    _suspend_cancel_cb: Callable[[], None] | None = field(default=None, repr=False)
    _retry_count: int = 0


class LightTimerCoordinator:
    """Side-effect layer that drives per-light timers from HA state changes.

    The coordinator owns one :class:`PerLightController` per managed light, the
    integration's master ``globally_enabled`` flag, and the subscription to the
    managed lights' ``state_changed`` events. It translates each light's
    off-to-on and on-to-off transitions into timer starts and stops using the
    pure decision core (:func:`should_start_timer`, :func:`next_state`) and
    schedules countdowns via Home Assistant's ``async_call_later``.

    This task (5.2) implements state-change handling and timer start only. The
    commanding-off retry loop (5.4), cancel/suspend (5.6), enable/disable and
    restart re-arm (5.8) are added in later tasks; the structure here keeps the
    countdown-expiry path small and overridable so those tasks can extend it
    without reworking the wiring.

    Per-light independence (Req 11.2, 11.6): every handler operates on exactly
    one controller, looked up by the event's entity id, and never reads or
    mutates another controller's state. Simultaneous expirations are handled
    independently because each controller schedules and owns its own
    ``async_call_later`` callback.

    Attributes:
        hass: The Home Assistant instance.
        entry: The integration's config entry.
        globally_enabled: The master enable flag (Req 7); when ``False`` no new
            timers start.
        controllers: The managed-light controllers keyed by ``light.*`` entity
            id.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        controllers: dict[str, PerLightController] | None = None,
        *,
        globally_enabled: bool = DEFAULT_GLOBALLY_ENABLED,
    ) -> None:
        """Initialise the coordinator.

        Args:
            hass: The Home Assistant instance.
            entry: The integration's config entry.
            controllers: Optional pre-built controllers keyed by entity id. When
                omitted an empty mapping is used (controllers are added by later
                setup code).
            globally_enabled: The initial master enable flag.
        """
        self.hass = hass
        self.entry = entry
        self.globally_enabled = globally_enabled
        self.controllers: dict[str, PerLightController] = controllers or {}
        # Cancel callback for the managed-lights state-change subscription.
        self._unsub_state_change: Callable[[], None] | None = None
        # Cancel callback for the per-second countdown tick.
        self._unsub_tick: Callable[[], None] | None = None

    # -- Subscription lifecycle ------------------------------------------

    @callback
    def async_subscribe(self) -> None:
        """Subscribe to ``state_changed`` events for the managed lights only.

        Uses ``async_track_state_change_event`` so the coordinator is only woken
        for the ``light.*`` entities it manages. Re-subscribing replaces any
        existing subscription so the tracked set always matches
        ``self.controllers``.
        """
        self.async_unsubscribe()
        entity_ids = list(self.controllers)
        if not entity_ids:
            return
        self._unsub_state_change = async_track_state_change_event(
            self.hass, entity_ids, self.async_handle_state_change
        )

    @callback
    def async_unsubscribe(self) -> None:
        """Cancel the managed-lights state-change subscription, if any."""
        if self._unsub_state_change is not None:
            self._unsub_state_change()
            self._unsub_state_change = None
        self._stop_tick()

    # -- Per-second tick -------------------------------------------------

    @callback
    def _start_tick(self) -> None:
        """Start the per-second tick if not already running."""
        if self._unsub_tick is not None:
            return
        self._schedule_next_tick()

    @callback
    def _schedule_next_tick(self) -> None:
        """Schedule the next 1-second tick."""
        self._unsub_tick = async_call_later(
            self.hass, 1, self._async_tick
        )

    @callback
    def _stop_tick(self) -> None:
        """Stop the per-second tick if running."""
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None

    @callback
    def _maybe_stop_tick(self) -> None:
        """Stop the tick if no controllers have active countdowns or suspensions.

        The tick is only needed while there's an active scheduled callback
        driving a countdown or suspension. If all callbacks have been cancelled,
        the tick can safely stop even if remaining values are non-zero (they
        represent stale state that will be cleared on the next state transition).
        """
        for controller in self.controllers.values():
            if controller._cancel_cb is not None:
                return
            if controller._suspend_cancel_cb is not None:
                return
        self._stop_tick()

    @callback
    def _async_tick(self, _now) -> None:
        """Decrement remaining_seconds and suspension_remaining each second."""
        self._unsub_tick = None  # The one-shot has fired; clear the handle.
        active = False
        for controller in self.controllers.values():
            if controller.state in RUNNING_CLASS_STATES and controller.remaining_seconds > 0 and controller._cancel_cb is not None:
                controller.remaining_seconds -= 1
                active = True
            if controller.state == LightTimerState.SUSPENDED and controller.suspension_remaining > 0 and controller._suspend_cancel_cb is not None:
                controller.suspension_remaining -= 1
                active = True
        if active:
            self._schedule_next_tick()
            self._notify_update()
        # If nothing was active, tick stops naturally (no reschedule).

    @callback
    def _notify_update(self) -> None:
        """Push a state update to the timer sensors (live countdown + state)."""
        async_dispatcher_send(self.hass, SIGNAL_TIMER_TICK)

    # -- State-change handling -------------------------------------------

    async def async_handle_state_change(self, event: Event) -> None:
        """Handle a managed light's ``state_changed`` event.

        Resolves the event to a single managed controller and acts on the kind
        of transition (Req 1.1, 1.3, 2.1, 2.3):

        * **off -> on edge**: evaluate :func:`should_start_timer`; when it
          returns ``True`` start the countdown via :meth:`async_start_timer`.
          When a timer is already running the repeated on event is ignored, so
          the existing countdown continues unchanged (Req 1.3).
        * **on -> off transition**: stop a running-class timer -- cancel the
          scheduled callback, set ``remaining_seconds`` to ``0`` and transition
          the state to ``IDLE`` via ``next_state(..., "off")`` (Req 2.1, 2.2).
        * **off transition with no running timer**: a no-op; all timer state is
          left unchanged (Req 2.3).

        Only the controller for ``event.data["entity_id"]`` is touched, which
        preserves per-light independence (Req 11.2).
        """
        entity_id = event.data.get("entity_id")
        controller = self.controllers.get(entity_id) if entity_id else None
        if controller is None:
            # Not a managed light (should not happen given the subscription).
            return

        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        new_on = new_state is not None and new_state.state == STATE_ON
        old_on = old_state is not None and old_state.state == STATE_ON

        if new_on and not old_on:
            # Off->on edge: start a countdown when conditions permit. A repeated
            # on event while a timer runs is filtered out by should_start_timer
            # (timer_running) so the running countdown is left untouched.
            self._handle_on_edge(controller)
        elif old_on and not new_on:
            # On->off transition: stop any running-class timer.
            self._handle_off_transition(controller)
        # Any other transition (e.g. attribute-only change) is ignored.

    @callback
    def _handle_on_edge(self, controller: PerLightController) -> None:
        """Start a countdown for ``controller`` on an off->on edge if allowed."""
        timer_running = controller.state in RUNNING_CLASS_STATES
        if should_start_timer(
            light_on=True,
            timer_running=timer_running,
            suspended=controller.suspension_remaining > 0
            or controller.state == LightTimerState.SUSPENDED,
            disabled=not controller.config.enabled
            or controller.state == LightTimerState.DISABLED,
            globally_enabled=self.globally_enabled,
        ):
            self.async_start_timer(controller.light_entity_id)

    @callback
    def _handle_off_transition(self, controller: PerLightController) -> None:
        """Stop a running-class timer for ``controller`` on an on->off edge.

        Per Req 2.3 an off transition with no running timer is a no-op: the
        ``next_state`` ``off`` transition leaves non-running states unchanged, so
        nothing is reset for an idle/suspended/disabled controller.
        """
        resulting = next_state(controller.state, TimerEvent("off"))
        if (
            controller.state in RUNNING_CLASS_STATES
            and resulting == LightTimerState.IDLE
        ):
            self._cancel_scheduled(controller)
            controller.remaining_seconds = 0
            controller.failure_active = False
            controller._retry_count = 0
            controller.state = resulting
            self._maybe_stop_tick()
            self._notify_update()

    # -- Timer start ------------------------------------------------------

    @callback
    def async_start_timer(self, light_id: str) -> None:
        """Start a countdown for the given managed light.

        Sets the controller to :attr:`~LightTimerState.RUNNING`, resets
        ``remaining_seconds`` to the configured ``timer_duration``, and schedules
        a one-shot ``async_call_later`` callback that fires when the countdown
        elapses (Req 1.1). The cancel handle is stored on the controller so the
        countdown can be stopped on an off transition (Req 2.2).

        Any previously scheduled callback for this controller is cancelled first
        so a start never leaks a timer. Only this controller is mutated, keeping
        lights independent (Req 11.2).
        """
        controller = self.controllers.get(light_id)
        if controller is None:
            return

        # Never leak an existing scheduled callback.
        self._cancel_scheduled(controller)

        duration = controller.config.timer_duration
        controller.state = LightTimerState.RUNNING
        controller.remaining_seconds = duration
        controller.failure_active = False
        controller._retry_count = 0

        controller._cancel_cb = async_call_later(
            self.hass,
            duration,
            self._make_expiry_callback(light_id),
        )
        self._start_tick()
        self._notify_update()

    def _make_expiry_callback(self, light_id: str) -> Callable:
        """Build the ``async_call_later`` callback fired when a timer expires.

        The callback transitions the controller to
        :attr:`~LightTimerState.COMMANDING_OFF` and issues a single
        ``light.turn_off`` command for the managed light (Req 1.2). The full
        confirmation/retry loop and failure handling are added in task 5.4; this
        keeps the expiry path minimal but already routes through
        :meth:`async_commanding_off` so the later task can extend behaviour in
        one place.
        """

        async def _on_expire(_now) -> None:
            controller = self.controllers.get(light_id)
            if controller is None:
                return
            # The scheduled callback has now fired; drop the stale handle.
            controller._cancel_cb = None
            if controller.state != LightTimerState.RUNNING:
                # State changed out from under the timer (e.g. light went off);
                # nothing to command off.
                return
            await self.async_commanding_off(controller)

        return _on_expire

    async def async_commanding_off(self, controller: PerLightController) -> None:
        """Command a managed light off after its countdown elapsed.

        Transitions the controller into :attr:`~LightTimerState.COMMANDING_OFF`,
        clamps ``remaining_seconds`` to ``0``, issues a single ``light.turn_off``
        for the light (Req 1.2), and schedules the first confirmation check 5s
        later. Per Req 1.4 the timer remains classified as running until the
        light is confirmed off, which is why the state moves to
        ``COMMANDING_OFF`` rather than ``IDLE`` here.

        The confirmation/retry loop (Req 1.5), the confirmed-off stop (Req 1.6,
        1.9), and the failure indication plus notification (Req 1.7) are driven
        by the scheduled :meth:`_schedule_off_confirmation` callback.
        """
        controller.state = next_state(controller.state, TimerEvent("expire"))
        controller.remaining_seconds = 0
        controller.failure_active = False
        controller._retry_count = 0
        await self._async_turn_off(controller.light_entity_id)
        self._schedule_off_confirmation(controller)
        self._notify_update()

    @callback
    def _schedule_off_confirmation(self, controller: PerLightController) -> None:
        """Schedule a one-shot off-confirmation check 5s out for ``controller``.

        The cancel handle is stored on the controller's ``_cancel_cb`` so an
        on->off transition (handled in :meth:`_handle_off_transition`) cancels
        any pending retry, keeping the retry loop from outliving the timer
        (Req 1.6, 2.2). Any previously scheduled callback is cleared first so a
        confirmation check never leaks.
        """
        self._cancel_scheduled(controller)
        controller._cancel_cb = async_call_later(
            self.hass,
            _OFF_RETRY_INTERVAL,
            self._make_off_confirmation_callback(controller.light_entity_id),
        )

    def _make_off_confirmation_callback(self, light_id: str) -> Callable:
        """Build the ``async_call_later`` callback that confirms a light is off.

        When it fires the callback resolves the controller and:

        * if the controller is gone or no longer commanding off (e.g. the light
          already went off and the state-change handler stopped the timer), it
          does nothing;
        * if the light is confirmed off, it cancels the pending retry and stops
          the timer -- transition to :attr:`~LightTimerState.IDLE`, remaining
          ``0``, failure cleared (Req 1.6, 1.9);
        * if the light is still on and fewer than ``_MAX_OFF_RETRIES`` retries
          have been issued, it re-issues ``light.turn_off``, increments the retry
          counter, and schedules the next check 5s out (Req 1.5);
        * if the light is still on after ``_MAX_OFF_RETRIES`` retries, it keeps
          the timer running (transition to :attr:`~LightTimerState.FAILED`), sets
          ``failure_active`` and notifies via the configured service (Req 1.7).
        """

        async def _on_confirm(_now) -> None:
            controller = self.controllers.get(light_id)
            if controller is None:
                return
            # The scheduled callback has now fired; drop the stale handle.
            controller._cancel_cb = None
            if controller.state not in (
                LightTimerState.COMMANDING_OFF,
                LightTimerState.FAILED,
            ):
                # The timer was already stopped (light off / cancel / disable).
                return

            if not self._is_light_on(light_id):
                # Confirmed off: stop the timer and clear any failure (Req 1.6,
                # 1.9). next_state maps confirm_off to IDLE from commanding/failed.
                controller.state = next_state(
                    controller.state, TimerEvent("confirm_off")
                )
                controller.remaining_seconds = 0
                controller.failure_active = False
                controller._retry_count = 0
                self._maybe_stop_tick()
                return

            if controller._retry_count < _MAX_OFF_RETRIES:
                # Still on: re-issue the off command and schedule the next check.
                controller._retry_count += 1
                await self._async_turn_off(light_id)
                self._schedule_off_confirmation(controller)
                return

            # Retries exhausted and the light is still on: keep the timer running
            # in the failure state, raise the failure indication, and notify.
            controller.state = next_state(controller.state, TimerEvent("retry_fail"))
            controller.failure_active = True
            await self._async_notify_failure(controller)

        return _on_confirm

    @callback
    def _is_light_on(self, light_id: str) -> bool:
        """Return whether the managed light's current HA state is ``on``."""
        state = self.hass.states.get(light_id)
        return state is not None and state.state == STATE_ON

    async def _async_notify_failure(self, controller: PerLightController) -> None:
        """Call the controller's configured notification service on failure.

        The configured ``notification_service`` is a ``domain.service`` string
        (e.g. ``notify.mobile_app``). When it is missing or not a well-formed
        ``domain.service`` pair a warning is logged and the coordinator does not
        crash; the failure indication remains set regardless (Req 1.7).
        """
        service = controller.config.notification_service
        if not service:
            _LOGGER.warning(
                "Shutoff failed for %s after %d retries; no notification "
                "service configured",
                controller.light_entity_id,
                _MAX_OFF_RETRIES,
            )
            return

        domain, _, service_name = service.partition(".")
        if not domain or not service_name:
            _LOGGER.warning(
                "Shutoff failed for %s; configured notification service %r is "
                "not a valid 'domain.service' value",
                controller.light_entity_id,
                service,
            )
            return

        try:
            await self.hass.services.async_call(
                domain,
                service_name,
                {
                    "message": (
                        f"Light Timer failed to turn off {controller.light_entity_id} "
                        f"after {_MAX_OFF_RETRIES} attempts."
                    )
                },
                blocking=False,
            )
        except Exception:  # noqa: BLE001 - never let a bad service crash the loop
            _LOGGER.warning(
                "Shutoff failed for %s and the notification service %r could "
                "not be called",
                controller.light_entity_id,
                service,
                exc_info=True,
            )

    async def _async_turn_off(self, light_id: str) -> None:
        """Issue a ``light.turn_off`` service call for the managed light."""
        await self.hass.services.async_call(
            _LIGHT_DOMAIN,
            SERVICE_TURN_OFF,
            {"entity_id": light_id},
            blocking=False,
        )

    # -- Cancel -----------------------------------------------------------

    @callback
    def async_cancel(self, light_id: str) -> bool:
        """Cancel a running timer for a managed light, leaving the light on.

        Stops a running-class timer (``RUNNING``/``COMMANDING_OFF``/``FAILED``)
        for ``controller``: it cancels the scheduled countdown/retry callback,
        clears ``remaining_seconds`` to ``0``, clears any failure indication and
        retry counter, and transitions the controller to
        :attr:`~LightTimerState.IDLE`. The managed light is **left in its current
        on state** -- ``async_cancel`` never issues ``light.turn_off`` (Req 5.1,
        5.2). Because the controller is left ``IDLE`` while the light stays on, no
        new countdown is started until the light goes off and back on (Req 5.3,
        enforced by the off->on edge handling).

        When the controller has no active timer (idle, suspended or disabled),
        no state change is made and the method returns ``False`` to indicate that
        there was no active timer to cancel (Req 5.4); callers/services surface
        the "no active timer" indication from this result. When a timer was
        cancelled the method returns ``True``.

        Only this controller is touched, preserving per-light independence
        (Req 11.2).

        Args:
            light_id: The ``light.*`` entity id whose timer should be cancelled.

        Returns:
            ``True`` if a running timer was cancelled; ``False`` if there was no
            active timer to cancel (including an unknown ``light_id``).
        """
        controller = self.controllers.get(light_id)
        if controller is None:
            return False

        if controller.state not in RUNNING_CLASS_STATES:
            # No active timer to cancel (idle/suspended/disabled): make no state
            # change and signal "no active timer" via the False return (Req 5.4).
            return False

        # Stop the running-class timer without touching the light (Req 5.1, 5.2).
        # next_state's user_cancel only maps RUNNING->IDLE, so set IDLE directly
        # to also cover COMMANDING_OFF/FAILED, which are running-class states
        # whose pending retry callback must be cancelled here.
        self._cancel_scheduled(controller)
        controller.remaining_seconds = 0
        controller.failure_active = False
        controller._retry_count = 0
        controller.state = LightTimerState.IDLE
        self._maybe_stop_tick()
        return True

    # -- Suspension -------------------------------------------------------

    def async_suspend(self, light_id: str, seconds: int | None = None) -> bool:
        """Suspend timer automation for a managed light for a period of seconds.

        Activates (or replaces) a suspension period for ``controller``:

        * When ``seconds`` is ``None`` the light's configured
          ``default_suspension`` is used (Req 6.8).
        * The value is validated via :func:`validate_suspension` against the
          currently active suspension period; on rejection the active suspension
          is left unchanged and the method returns ``False`` so callers/services
          can surface the validation error (Req 6.3).
        * On acceptance any running-class timer is stopped (its scheduled
          callback cancelled, ``remaining_seconds`` cleared, failure/retry state
          reset), the controller transitions to
          :attr:`~LightTimerState.SUSPENDED`, ``suspension_remaining`` is set to
          the accepted value, and a one-shot suspension-end resume is scheduled
          ``seconds`` out (Req 6.2, 6.5). Setting a new suspension while one is
          active replaces it: the prior scheduled resume is cancelled first so a
          stale resume never fires (Req 6.4).

        When the suspension elapses the scheduled resume clears the suspension
        (state back to :attr:`~LightTimerState.IDLE`, ``suspension_remaining``
        ``0``) and, if the light is on with no running timer, starts a countdown
        (Req 6.6, 6.7).

        Only this controller is touched, preserving per-light independence
        (Req 11.2).

        Args:
            light_id: The ``light.*`` entity id to suspend.
            seconds: The suspension period in seconds, or ``None`` to use the
                light's configured ``default_suspension``.

        Returns:
            ``True`` if a suspension was activated/replaced; ``False`` if the
            submitted value was rejected (or ``light_id`` is unknown).
        """
        controller = self.controllers.get(light_id)
        if controller is None:
            return False

        if seconds is None:
            seconds = controller.config.default_suspension

        current = controller.suspension_remaining or None
        result = validate_suspension(seconds, current)
        if not result.ok:
            # Invalid value: leave any active suspension unchanged (Req 6.3).
            return False

        accepted = result.value

        # Replace any prior scheduled suspension resume (Req 6.4) and stop any
        # running-class timer (its countdown/retry callback lives on _cancel_cb).
        self._cancel_suspension(controller)
        self._cancel_scheduled(controller)

        controller.state = LightTimerState.SUSPENDED
        controller.remaining_seconds = 0
        controller.suspension_remaining = accepted
        controller.failure_active = False
        controller._retry_count = 0

        controller._suspend_cancel_cb = async_call_later(
            self.hass,
            accepted,
            self._make_suspension_end_callback(light_id),
        )
        self._start_tick()
        return True

    def _make_suspension_end_callback(self, light_id: str) -> Callable:
        """Build the ``async_call_later`` callback fired when a suspension ends.

        When it fires the callback resolves the controller and, if it is still
        suspended, clears the suspension (state -> :attr:`~LightTimerState.IDLE`,
        ``suspension_remaining`` ``0``) (Req 6.6). If the managed light is on with
        no running timer the callback starts a countdown via
        :meth:`async_start_timer`, gated by :func:`should_start_timer` so a
        disabled or globally-disabled light is not re-armed (Req 6.7).
        """

        async def _on_suspension_end(_now) -> None:
            controller = self.controllers.get(light_id)
            if controller is None:
                return
            # The scheduled resume has now fired; drop the stale handle.
            controller._suspend_cancel_cb = None
            if controller.state != LightTimerState.SUSPENDED:
                # Suspension was already cleared (e.g. re-enable / replaced).
                return

            # Clear the suspension back to idle (Req 6.6).
            controller.state = next_state(controller.state, TimerEvent("suspend_end"))
            controller.suspension_remaining = 0

            # If the light is on and untimed, resume by starting a timer (Req 6.7).
            if should_start_timer(
                light_on=self._is_light_on(light_id),
                timer_running=controller.state in RUNNING_CLASS_STATES,
                suspended=controller.suspension_remaining > 0
                or controller.state == LightTimerState.SUSPENDED,
                disabled=not controller.config.enabled
                or controller.state == LightTimerState.DISABLED,
                globally_enabled=self.globally_enabled,
            ):
                self.async_start_timer(light_id)
            else:
                self._maybe_stop_tick()

        return _on_suspension_end

    # -- Enable / disable (per-light and global) --------------------------

    @callback
    def async_set_enabled(self, light_id: str, enabled: bool) -> None:
        """Enable or disable timer automation for a single managed light.

        **Disable** (``enabled=False``): stop any running-class timer without
        commanding the light -- cancel the scheduled countdown/retry callback,
        clear ``remaining_seconds`` to ``0`` and reset any failure/retry state --
        set ``config.enabled`` to ``False`` and transition the controller to
        :attr:`~LightTimerState.DISABLED` via ``next_state(..., "disable")``. The
        managed light is **left in its current state**; ``async_set_enabled``
        never issues ``light.turn_off`` (Req 7.1, 7.2). A disabled light leaves
        timers unstarted on subsequent on transitions (Req 7.3, enforced by
        ``should_start_timer``).

        **Enable** (``enabled=True``): set ``config.enabled`` to ``True``, clear
        any active suspension (cancel the scheduled resume and zero
        ``suspension_remaining``) and transition to :attr:`~LightTimerState.IDLE`
        via ``next_state(..., "enable")`` (Req 7.4). If the light is currently on
        and untimed and the integration is globally enabled, start a countdown
        (Req 7.5); the start is gated by :func:`should_start_timer` so a
        globally-disabled integration does not re-arm.

        Persisting ``config.enabled`` back to ``entry.options`` is the
        responsibility of the options/switch layer; this method only updates the
        in-memory configuration and runtime state.

        Only this controller is touched, preserving per-light independence
        (Req 11.4).

        Args:
            light_id: The ``light.*`` entity id to enable or disable.
            enabled: ``True`` to enable timer automation, ``False`` to disable.
        """
        controller = self.controllers.get(light_id)
        if controller is None:
            return

        if not enabled:
            # Disable: stop any running-class timer and mark disabled without
            # commanding the light (Req 7.1, 7.2). Cancel both the countdown/retry
            # callback and any scheduled suspension resume so nothing fires after
            # the light is disabled.
            self._cancel_scheduled(controller)
            self._cancel_suspension(controller)
            controller.remaining_seconds = 0
            controller.suspension_remaining = 0
            controller.failure_active = False
            controller._retry_count = 0
            controller.config.enabled = False
            controller.state = next_state(controller.state, TimerEvent("disable"))
            self._maybe_stop_tick()
            return

        # Enable: clear disabled/suspended status and re-arm if appropriate.
        self._cancel_suspension(controller)
        controller.config.enabled = True
        controller.suspension_remaining = 0
        controller.state = next_state(controller.state, TimerEvent("enable"))

        # Re-arm only when the light is on and untimed and the integration is
        # globally enabled (Req 7.5); should_start_timer gates every condition.
        if should_start_timer(
            light_on=self._is_light_on(light_id),
            timer_running=controller.state in RUNNING_CLASS_STATES,
            suspended=controller.suspension_remaining > 0
            or controller.state == LightTimerState.SUSPENDED,
            disabled=not controller.config.enabled
            or controller.state == LightTimerState.DISABLED,
            globally_enabled=self.globally_enabled,
        ):
            self.async_start_timer(light_id)

    @callback
    def async_set_global(self, enabled: bool) -> None:
        """Enable or disable the entire integration via the master switch.

        **Master off** (``enabled=False``): set :attr:`globally_enabled` to
        ``False`` and, for **every** controller, stop any running-class timer --
        cancel the scheduled countdown/retry callback, clear
        ``remaining_seconds`` to ``0`` and reset failure/retry state -- leaving
        each previously-running light :attr:`~LightTimerState.IDLE`. The managed
        lights are **left in their current states** (no ``light.turn_off``) and
        each light's per-light ``config.enabled`` is left unchanged (Req 7.9,
        7.10). While globally disabled no new timers start, which is enforced by
        :func:`should_start_timer` reading :attr:`globally_enabled`.

        **Master on** (``enabled=True``): set :attr:`globally_enabled` to
        ``True`` and, for each controller that is individually enabled, on and
        untimed, start a countdown (Req 7.11, 7.12). The per-controller start is
        gated by :func:`should_start_timer` so suspended or individually-disabled
        lights are not re-armed.

        Each controller is handled independently in the iteration, preserving
        per-light independence (Req 11). Persisting :attr:`globally_enabled` back
        to ``entry.options`` is handled by the options/switch layer; this method
        only updates the in-memory flag and runtime state.

        Args:
            enabled: ``True`` to globally enable the integration, ``False`` to
                globally disable it.
        """
        if not enabled:
            # Master off: stop every running-class timer without commanding the
            # lights and without changing per-light config.enabled (Req 7.9, 7.10).
            self.globally_enabled = False
            for controller in self.controllers.values():
                if controller.state in RUNNING_CLASS_STATES:
                    self._cancel_scheduled(controller)
                    controller.remaining_seconds = 0
                    controller.failure_active = False
                    controller._retry_count = 0
                    controller.state = LightTimerState.IDLE
            self._maybe_stop_tick()
            return

        # Master on: resume timers for individually-enabled, on, untimed lights
        # (Req 7.11, 7.12). Each controller is evaluated independently.
        self.globally_enabled = True
        for light_id, controller in self.controllers.items():
            if should_start_timer(
                light_on=self._is_light_on(light_id),
                timer_running=controller.state in RUNNING_CLASS_STATES,
                suspended=controller.suspension_remaining > 0
                or controller.state == LightTimerState.SUSPENDED,
                disabled=not controller.config.enabled
                or controller.state == LightTimerState.DISABLED,
                globally_enabled=self.globally_enabled,
            ):
                self.async_start_timer(light_id)

    # -- Startup / restart recovery ---------------------------------------

    @callback
    def async_rearm_on_setup(self) -> None:
        """Re-arm timers for eligible lights on startup or restart (Req 1.8).

        Iterates every controller and starts a countdown for each light that is
        enabled, on, untimed, not suspended and globally enabled -- exactly the
        condition encoded by :func:`should_start_timer`. This recovers running
        timers after a Home Assistant restart that occurred mid-countdown: a
        light that is still on when setup completes is re-armed with its
        configured ``timer_duration`` (suspension and failure state are transient
        and start cleared on a fresh setup).

        Each controller is evaluated and started independently, preserving
        per-light independence (Req 11). Intended to be called from
        ``async_setup_entry`` once the controllers are built and the master flag
        is restored.
        """
        for light_id, controller in self.controllers.items():
            if should_start_timer(
                light_on=self._is_light_on(light_id),
                timer_running=controller.state in RUNNING_CLASS_STATES,
                suspended=controller.suspension_remaining > 0
                or controller.state == LightTimerState.SUSPENDED,
                disabled=not controller.config.enabled
                or controller.state == LightTimerState.DISABLED,
                globally_enabled=self.globally_enabled,
            ):
                self.async_start_timer(light_id)

    # -- Helpers ----------------------------------------------------------

    @callback
    def _cancel_scheduled(self, controller: PerLightController) -> None:
        """Cancel and clear a controller's scheduled countdown callback."""
        if controller._cancel_cb is not None:
            controller._cancel_cb()
            controller._cancel_cb = None
            self._maybe_stop_tick()

    @callback
    def _cancel_suspension(self, controller: PerLightController) -> None:
        """Cancel and clear a controller's scheduled suspension-end resume."""
        if controller._suspend_cancel_cb is not None:
            controller._suspend_cancel_cb()
            controller._suspend_cancel_cb = None
            self._maybe_stop_tick()
