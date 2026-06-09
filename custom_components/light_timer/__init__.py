"""The Light Timer integration.

Home Assistant HACS custom integration that turns managed lights off after a
configurable countdown. A single config entry manages any number of lights,
each with independent timer duration, default suspension duration, optional
notification service, and enable/disable state.

This module wires the integration lifecycle together (Req 8.1):

* :func:`async_setup_entry` builds the :class:`LightTimerCoordinator` from the
  entry's ``options``, stores it on ``hass.data`` **before** forwarding the
  entity platforms (the platforms read the coordinator back from there),
  subscribes to the managed lights, re-arms timers for lights that are still on
  after a restart (Req 1.8), and registers the integration's services.
* :func:`async_unload_entry` tears the entry down: unsubscribes the coordinator,
  cancels every scheduled callback, unloads the platforms and drops the stored
  coordinator.
* :func:`async_reload_entry` reloads the entry when its options change so the
  coordinator and entities are rebuilt against the new configuration (Req 10.1,
  10.3).

Two services are registered once for the integration domain (Req 5.1, 5.4, 6.8):

* ``light_timer.cancel`` -- cancel a running timer for the targeted managed
  light(s), leaving the light on; and
* ``light_timer.suspend`` -- suspend timer automation for the targeted managed
  light(s) for an optional ``duration`` (validated via
  :func:`logic.validate_suspension`), falling back to each light's configured
  ``default_suspension`` when omitted (Req 6.8).
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry

from .const import (
    CONF_DEFAULT_SUSPENSION,
    CONF_ENABLED,
    CONF_GLOBALLY_ENABLED,
    CONF_LIGHT_ENTITY_ID,
    CONF_LIGHTS,
    CONF_NOTIFICATION_SERVICE,
    CONF_TIMER_DURATION,
    DEFAULT_ENABLED,
    DEFAULT_GLOBALLY_ENABLED,
    DEFAULT_SUSPENSION,
    DEFAULT_TIMER_DURATION,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import (
    LightTimerCoordinator,
    ManagedLightConfig,
    PerLightController,
)
from .logic import validate_suspension

_LOGGER = logging.getLogger(__name__)

# Service names exposed under the integration domain.
SERVICE_CANCEL = "cancel"
SERVICE_SUSPEND = "suspend"

# Service call fields.
ATTR_DURATION = "duration"

# Schema for the optional suspend duration. Validation of the integer range is
# deferred to validate_suspension so the service surfaces the same rules as the
# rest of the integration; here we only accept an optional value.
_CANCEL_SERVICE_SCHEMA = cv.make_entity_service_schema({})
_SUSPEND_SERVICE_SCHEMA = cv.make_entity_service_schema(
    {vol.Optional(ATTR_DURATION): vol.Any(int, str)}
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Light Timer from a config entry.

    Builds the coordinator from ``entry.options`` (one
    :class:`PerLightController` per managed light), stores it on ``hass.data``
    keyed by ``entry.entry_id`` **before** forwarding the entity platforms so the
    platforms can read it back, subscribes to the managed lights' state changes,
    re-arms timers for lights still on after a restart (Req 1.8), and registers
    the integration services.
    """
    options = entry.options or {}

    controllers: dict[str, PerLightController] = {}
    for light in options.get(CONF_LIGHTS, []):
        entity_id = light.get(CONF_LIGHT_ENTITY_ID)
        if not entity_id:
            continue
        config = ManagedLightConfig(
            light_entity_id=entity_id,
            timer_duration=light.get(CONF_TIMER_DURATION, DEFAULT_TIMER_DURATION),
            default_suspension=light.get(CONF_DEFAULT_SUSPENSION, DEFAULT_SUSPENSION),
            notification_service=light.get(CONF_NOTIFICATION_SERVICE) or None,
            enabled=bool(light.get(CONF_ENABLED, DEFAULT_ENABLED)),
        )
        controllers[entity_id] = PerLightController(
            light_entity_id=entity_id, config=config
        )

    globally_enabled = bool(
        options.get(CONF_GLOBALLY_ENABLED, DEFAULT_GLOBALLY_ENABLED)
    )

    coordinator = LightTimerCoordinator(
        hass, entry, controllers, globally_enabled=globally_enabled
    )

    # Store the coordinator BEFORE forwarding platform setups: the sensor,
    # switch and button platforms read hass.data[DOMAIN][entry.entry_id].
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Subscribe to the managed lights and re-arm any timers that should still be
    # running after a restart (Req 1.8). async_rearm_on_setup is a @callback.
    coordinator.async_subscribe()
    coordinator.async_rearm_on_setup()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry whenever its options change (add/edit/remove light, or a
    # persisted enable/disable flag) so the coordinator and entities rebuild.
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # If Home Assistant is still starting, a managed light's own integration
    # (e.g. ESPHome) may not have registered the light entity/device yet, so the
    # timer entities would fall back to a standalone device. Once HA has fully
    # started, reload the entry so the entities re-evaluate their device link
    # and attach to the light's device.
    _async_schedule_device_link_reload(hass, entry, controllers)

    _async_register_services(hass)

    return True


@callback
def _async_schedule_device_link_reload(
    hass: HomeAssistant,
    entry: ConfigEntry,
    controllers: dict[str, PerLightController],
) -> None:
    """Reload the entry after HA starts if any light's device isn't linked yet.

    During startup the managed lights may not yet be registered (their
    integrations can load after this one), so the timer entities cannot attach
    to the light's device. After ``EVENT_HOMEASSISTANT_STARTED`` everything is
    loaded; if any managed light has a device that the timer entities aren't
    linked to yet, reload the entry once so the link is established.
    """
    if hass.state is CoreState.running:
        # Already fully started: device info was resolved correctly at setup.
        return

    @callback
    def _on_started(_event: Event) -> None:
        ent_reg = er.async_get(hass)
        for light_id in controllers:
            light_entry = ent_reg.async_get(light_id)
            if light_entry is not None and light_entry.device_id is not None:
                # At least one managed light now has a device; reload so the
                # timer entities attach to it.
                hass.async_create_task(
                    hass.config_entries.async_reload(entry.entry_id)
                )
                return

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Light Timer config entry.

    Unsubscribes the coordinator, cancels every controller's scheduled
    countdown/retry and suspension callbacks, unloads the entity platforms and
    drops the stored coordinator. Returns the platform unload result.
    """
    coordinator: LightTimerCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is not None:
        coordinator.async_unsubscribe()
        # Cancel any scheduled callbacks so nothing fires after unload.
        for controller in coordinator.controllers.values():
            coordinator._cancel_scheduled(controller)
            coordinator._cancel_suspension(controller)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        domain_data = hass.data.get(DOMAIN, {})
        domain_data.pop(entry.entry_id, None)
        # Drop the integration's services once no entries remain.
        if not domain_data:
            _async_unregister_services(hass)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a Light Timer config entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


# Backwards-compatible alias used by some HA tooling/tests.
async def async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a Light Timer config entry (alias for :func:`async_reload_entry`)."""
    await async_reload_entry(hass, entry)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Allow removal of devices that no longer have managed lights (Req 1.5).

    Returns ``True`` if the device's identifier corresponds to a light that is
    no longer managed by the coordinator (i.e. was removed from the config),
    allowing Home Assistant to clean up the orphaned device.

    Returns ``False`` for the Integration_Device (entry_id identifier) while the
    config entry exists, and ``False`` as a safe default for unknown devices.
    """
    coordinator: LightTimerCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is None:
        return True

    for identifier in device_entry.identifiers:
        if len(identifier) == 2 and identifier[0] == DOMAIN:
            # If it's the integration device, don't remove while entry exists.
            if identifier[1] == entry.entry_id:
                return False
            # If it's a per-light device, allow removal if light is no longer managed.
            if identifier[1] not in coordinator.controllers:
                return True
    return False


# -- Services --------------------------------------------------------------


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the ``cancel`` and ``suspend`` services once for the domain."""
    if hass.services.has_service(DOMAIN, SERVICE_CANCEL) and hass.services.has_service(
        DOMAIN, SERVICE_SUSPEND
    ):
        # Already registered (guard against double registration across entries).
        return

    async def _handle_cancel(call: ServiceCall) -> None:
        """Cancel running timers for every targeted managed light (Req 5.1)."""
        for coordinator, light_id in _resolve_targets(hass, call):
            coordinator.async_cancel(light_id)

    async def _handle_suspend(call: ServiceCall) -> None:
        """Suspend targeted managed lights for an optional duration (Req 6.8)."""
        raw_duration = call.data.get(ATTR_DURATION)
        seconds: int | None = None
        if raw_duration is not None:
            # Validate via the shared validator so the service enforces the same
            # integer-in-range rules as the rest of the integration.
            result = validate_suspension(raw_duration, None)
            if not result.ok:
                raise HomeAssistantError(
                    f"Invalid suspension duration {raw_duration!r}: {result.error}"
                )
            seconds = result.value

        for coordinator, light_id in _resolve_targets(hass, call):
            coordinator.async_suspend(light_id, seconds)

    if not hass.services.has_service(DOMAIN, SERVICE_CANCEL):
        hass.services.async_register(
            DOMAIN, SERVICE_CANCEL, _handle_cancel, schema=_CANCEL_SERVICE_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SUSPEND):
        hass.services.async_register(
            DOMAIN, SERVICE_SUSPEND, _handle_suspend, schema=_SUSPEND_SERVICE_SCHEMA
        )


def _async_unregister_services(hass: HomeAssistant) -> None:
    """Remove the integration's services (called when the last entry unloads)."""
    if hass.services.has_service(DOMAIN, SERVICE_CANCEL):
        hass.services.async_remove(DOMAIN, SERVICE_CANCEL)
    if hass.services.has_service(DOMAIN, SERVICE_SUSPEND):
        hass.services.async_remove(DOMAIN, SERVICE_SUSPEND)


def _resolve_targets(
    hass: HomeAssistant, call: ServiceCall
) -> list[tuple[LightTimerCoordinator, str]]:
    """Resolve a service call's target entities to (coordinator, light_id) pairs.

    A target may be a managed ``light.*`` entity directly, or one of the
    integration's own entities (the remaining-time ``sensor``, the per-light
    enable ``switch``, or the cancel/suspend ``button``). Integration entities
    are mapped back to their managed light via the entity registry ``unique_id``
    (``{entry_id}_{light_entity_id}_<suffix>``). Duplicate light targets are
    de-duplicated so each managed light is acted on at most once per call.
    """
    target_entity_ids = call.data.get("entity_id") or []
    if isinstance(target_entity_ids, str):
        target_entity_ids = [target_entity_ids]

    domain_data: dict[str, LightTimerCoordinator] = hass.data.get(DOMAIN, {})
    registry = er.async_get(hass)

    resolved: list[tuple[LightTimerCoordinator, str]] = []
    seen: set[tuple[str, str]] = set()

    for entity_id in target_entity_ids:
        # Case 1: the target is itself a managed light entity id.
        matched = False
        for coordinator in domain_data.values():
            if entity_id in coordinator.controllers:
                key = (coordinator.entry.entry_id, entity_id)
                if key not in seen:
                    seen.add(key)
                    resolved.append((coordinator, entity_id))
                matched = True
        if matched:
            continue

        # Case 2: the target is one of the integration's own entities; map it
        # back to a managed light via its registry unique_id.
        reg_entry = registry.async_get(entity_id)
        if reg_entry is None or reg_entry.platform != DOMAIN:
            continue
        coordinator = domain_data.get(reg_entry.config_entry_id)
        if coordinator is None:
            continue
        light_id = _light_id_from_unique_id(reg_entry.unique_id, coordinator)
        if light_id is None:
            continue
        key = (coordinator.entry.entry_id, light_id)
        if key not in seen:
            seen.add(key)
            resolved.append((coordinator, light_id))

    return resolved


def _light_id_from_unique_id(
    unique_id: str | None, coordinator: LightTimerCoordinator
) -> str | None:
    """Extract the managed light id embedded in an entity's ``unique_id``.

    Integration entities use ``{entry_id}_{light_entity_id}_<suffix>`` unique
    ids. Rather than parse the suffix, match against the known managed light ids
    so the embedded ``light.*`` id (which itself contains a ``.``) is recovered
    unambiguously.
    """
    if not unique_id:
        return None
    prefix = f"{coordinator.entry.entry_id}_"
    if not unique_id.startswith(prefix):
        return None
    remainder = unique_id[len(prefix) :]
    for light_id in coordinator.controllers:
        if remainder.startswith(f"{light_id}_"):
            return light_id
    return None
