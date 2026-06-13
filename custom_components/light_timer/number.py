"""Number platform for the Light Timer integration.

Exposes one :class:`LightTimerDurationNumber` per managed light, allowing the
user to view and adjust the timer duration directly from the device page without
navigating to the integration options flow.

Changes are applied immediately to the in-memory ``ManagedLightConfig`` and
persisted to ``entry.options`` so the value survives restarts. A config entry
reload is NOT triggered — the in-memory update is sufficient for the coordinator
to use the new duration on the next timer start (Req 5.3, 5.4).
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_LIGHT_ENTITY_ID,
    CONF_LIGHTS,
    CONF_TIMER_DURATION,
    DOMAIN,
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
)
from .coordinator import LightTimerCoordinator, PerLightController

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the timer duration number entities for each managed light.

    Reads the coordinator stored at ``hass.data[DOMAIN][entry.entry_id]`` and
    creates one :class:`LightTimerDurationNumber` per managed controller.
    """
    coordinator: LightTimerCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[NumberEntity] = []
    for controller in coordinator.controllers.values():
        entities.append(LightTimerDurationNumber(coordinator, entry, controller))

    async_add_entities(entities)


def _get_light_friendly_name(hass: HomeAssistant, light_entity_id: str) -> str:
    """Get the friendly name of a light entity, falling back to object_id."""
    state = hass.states.get(light_entity_id)
    if state and state.attributes.get("friendly_name"):
        return state.attributes["friendly_name"]
    return light_entity_id.split(".", 1)[-1].replace("_", " ").title()


def _link_to_light_device(
    hass: HomeAssistant, light_entity_id: str
) -> DeviceInfo | None:
    """Build a "link" DeviceInfo to the managed light's device, if it has one."""
    ent_reg = er.async_get(hass)
    light_entry = ent_reg.async_get(light_entity_id)
    if light_entry is None or light_entry.device_id is None:
        return None
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(light_entry.device_id)
    if device is None:
        return None
    if not device.identifiers and not device.connections:
        return None
    link = DeviceInfo()
    if device.identifiers:
        link["identifiers"] = set(device.identifiers)
    if device.connections:
        link["connections"] = set(device.connections)
    return link


class LightTimerDurationNumber(NumberEntity):
    """Per-light number entity exposing the timer duration for direct editing.

    The entity reads its current value from the live controller config and
    persists changes to ``entry.options`` without triggering a reload. Running
    timers are unaffected; the new duration applies on the next timer start
    (Req 5.4).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_native_min_value = MIN_DURATION_SECONDS
    _attr_native_max_value = MAX_DURATION_SECONDS
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX
    _attr_name = "Timer duration"

    def __init__(
        self,
        coordinator: LightTimerCoordinator,
        entry: ConfigEntry,
        controller: PerLightController,
    ) -> None:
        """Initialise the timer duration number entity.

        Args:
            coordinator: The integration's coordinator (live state source).
            entry: The integration's config entry (for option persistence).
            controller: The managed-light controller this entity maps to.
        """
        self._coordinator = coordinator
        self._entry = entry
        self._light_id = controller.light_entity_id
        self._attr_unique_id = (
            f"{entry.entry_id}_{self._light_id}_timer_duration"
        )

    @property
    def _controller(self) -> PerLightController | None:
        """Return the live controller for this entity, or ``None`` if removed."""
        return self._coordinator.controllers.get(self._light_id)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device info to attach this entity to the light's device."""
        link = _link_to_light_device(self.hass, self._light_id)
        if link is not None:
            return link
        # Fallback: standalone Timer_Device
        friendly_name = _get_light_friendly_name(self.hass, self._light_id)
        return DeviceInfo(
            identifiers={(DOMAIN, self._light_id)},
            name=f"{friendly_name}_timer",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float | None:
        """Return the current timer_duration from the controller's config."""
        controller = self._controller
        if controller is None:
            return None
        return controller.config.timer_duration

    async def async_set_native_value(self, value: float) -> None:
        """Update timer_duration in memory and persist to entry.options.

        Validates the value is within the allowed range [1, 86400]. Updates the
        controller's in-memory config and persists to entry.options so the value
        survives restarts. Does NOT trigger a config entry reload (Req 5.3).
        """
        int_value = int(value)

        # Defense-in-depth range check (HA's NumberEntity base enforces min/max
        # but we validate explicitly as well).
        if int_value < MIN_DURATION_SECONDS or int_value > MAX_DURATION_SECONDS:
            _LOGGER.warning(
                "Rejecting timer_duration %d for %s: outside valid range [%d, %d]",
                int_value,
                self._light_id,
                MIN_DURATION_SECONDS,
                MAX_DURATION_SECONDS,
            )
            return

        controller = self._controller
        if controller is None:
            return

        # Update in-memory config (takes effect on next timer start).
        controller.config.timer_duration = int_value

        # Persist to entry.options so the value survives restarts.
        self._persist_timer_duration(int_value)

        self.async_write_ha_state()

    def _persist_timer_duration(self, duration: int) -> None:
        """Persist the timer_duration back to ``entry.options``.

        Updates the matching light dict in ``options["lights"]`` so the value
        survives a reload/restart. Does NOT trigger a config entry reload.
        """
        options = dict(self._entry.options)
        lights = [dict(light) for light in options.get(CONF_LIGHTS, [])]
        changed = False
        for light in lights:
            if light.get(CONF_LIGHT_ENTITY_ID) == self._light_id:
                if light.get(CONF_TIMER_DURATION) != duration:
                    light[CONF_TIMER_DURATION] = duration
                    changed = True
                break
        if not changed:
            return
        options[CONF_LIGHTS] = lights
        self.hass.config_entries.async_update_entry(self._entry, options=options)
