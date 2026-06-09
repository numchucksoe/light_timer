"""Switch platform for the Light Timer integration.

Exposes two kinds of switch entity that adapt the coordinator's enable/disable
behaviour to the Home Assistant UI (see the design's "Entity Platforms" table):

* :class:`LightTimerEnableSwitch` -- one per managed light. Its on/off state
  mirrors the light's per-light ``config.enabled`` flag and toggling it maps to
  :meth:`LightTimerCoordinator.async_set_enabled`. Turning it off disables timer
  automation for that light (the disabled indication, Req 7.6); turning it back
  on re-enables it and clears the indication (Req 7.7). The managed light's
  entity id is surfaced via the ``managed_light`` state attribute.
* :class:`LightTimerMasterSwitch` -- a single global master switch (Req 7.8).
  Its on/off state mirrors :attr:`LightTimerCoordinator.globally_enabled` and
  toggling it maps to :meth:`LightTimerCoordinator.async_set_global`, reflecting
  the global-disabled indication (Req 7.13) and clearing it on re-enable
  (Req 7.14).

Both entities read their state live from the coordinator and persist the
resulting flag back to ``entry.options`` (via ``async_update_entry``) so the
enable/disable choice survives a reload. The ``hass.data[DOMAIN][entry.entry_id]``
lookup convention is established by ``__init__.py`` (task 8.4).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ENABLED,
    CONF_GLOBALLY_ENABLED,
    CONF_LIGHT_ENTITY_ID,
    CONF_LIGHTS,
    DOMAIN,
)
from .coordinator import LightTimerCoordinator, PerLightController


def _get_light_friendly_name(hass: HomeAssistant, light_entity_id: str) -> str:
    """Get the friendly name of a light entity, falling back to object_id."""
    state = hass.states.get(light_entity_id)
    if state and state.attributes.get("friendly_name"):
        return state.attributes["friendly_name"]
    # Fallback: strip domain and title-case the object_id
    return light_entity_id.split(".", 1)[-1].replace("_", " ").title()


def _get_light_device_info(hass: HomeAssistant, light_entity_id: str) -> DeviceInfo | None:
    """Look up the device that owns the light entity, if any."""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(light_entity_id)
    if entry is None or entry.device_id is None:
        return None
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(entry.device_id)
    if device is None:
        return None
    return DeviceInfo(identifiers=device.identifiers)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switch entities for a Light Timer config entry.

    Reads the coordinator from ``hass.data[DOMAIN][entry.entry_id]`` (convention
    established by ``__init__.py``) and creates one
    :class:`LightTimerEnableSwitch` per managed controller plus a single
    :class:`LightTimerMasterSwitch` (Req 7.8).
    """
    coordinator: LightTimerCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SwitchEntity] = [
        LightTimerEnableSwitch(coordinator, entry, controller)
        for controller in coordinator.controllers.values()
    ]
    entities.append(LightTimerMasterSwitch(coordinator, entry))

    async_add_entities(entities)


class LightTimerEnableSwitch(SwitchEntity):
    """Per-light enable switch mapping to ``async_set_enabled`` (Req 7.6, 7.7)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: LightTimerCoordinator,
        entry: ConfigEntry,
        controller: PerLightController,
    ) -> None:
        """Initialise the per-light enable switch.

        Args:
            coordinator: The integration's coordinator (live state source).
            entry: The integration's config entry (for option persistence).
            controller: The managed-light controller this switch maps to.
        """
        self._coordinator = coordinator
        self._entry = entry
        self._light_id = controller.light_entity_id
        self._attr_unique_id = f"{entry.entry_id}_{self._light_id}_enable"
        self._attr_name = "Enabled"

    @property
    def _controller(self) -> PerLightController | None:
        """Return the live controller for this switch, or ``None`` if removed."""
        return self._coordinator.controllers.get(self._light_id)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device info to attach this entity to the light's device."""
        info = _get_light_device_info(self.hass, self._light_id)
        if info is not None:
            return info
        # Fallback: standalone Timer_Device
        friendly_name = _get_light_friendly_name(self.hass, self._light_id)
        return DeviceInfo(
            identifiers={(DOMAIN, self._light_id)},
            name=f"{friendly_name}_timer",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self) -> bool:
        """Return whether timer automation is enabled for this light.

        Reads live from the controller's ``config.enabled``; a disabled light
        reports ``False`` so the dashboard reflects the disabled indication
        (Req 7.6, 7.7).
        """
        controller = self._controller
        return bool(controller and controller.config.enabled)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the managed light's entity id as ``managed_light``."""
        return {"managed_light": self._light_id}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable timer automation for this light (Req 7.7)."""
        self._coordinator.async_set_enabled(self._light_id, True)
        self._persist_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable timer automation for this light (Req 7.6)."""
        self._coordinator.async_set_enabled(self._light_id, False)
        self._persist_enabled(False)
        self.async_write_ha_state()

    @callback
    def _persist_enabled(self, enabled: bool) -> None:
        """Persist this light's ``enabled`` flag back to ``entry.options``.

        Updates the matching light dict in ``options["lights"]`` so the choice
        survives a reload. Leaves the options untouched if the light is not found
        in the stored configuration.
        """
        options = dict(self._entry.options)
        lights = [dict(light) for light in options.get(CONF_LIGHTS, [])]
        changed = False
        for light in lights:
            if light.get(CONF_LIGHT_ENTITY_ID) == self._light_id:
                if light.get(CONF_ENABLED) != enabled:
                    light[CONF_ENABLED] = enabled
                    changed = True
                break
        if not changed:
            return
        options[CONF_LIGHTS] = lights
        self.hass.config_entries.async_update_entry(self._entry, options=options)


class LightTimerMasterSwitch(SwitchEntity):
    """Global master switch mapping to ``async_set_global`` (Req 7.8, 7.13, 7.14)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: LightTimerCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the global master switch.

        Args:
            coordinator: The integration's coordinator (live state source).
            entry: The integration's config entry (for option persistence).
        """
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_master"
        self._attr_name = "Master"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to attach this entity to the Integration_Device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Light Timer",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self) -> bool:
        """Return whether the integration is globally enabled (Req 7.13, 7.14)."""
        return bool(self._coordinator.globally_enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Globally enable the integration (Req 7.14)."""
        self._coordinator.async_set_global(True)
        self._persist_global(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Globally disable the integration (Req 7.13)."""
        self._coordinator.async_set_global(False)
        self._persist_global(False)
        self.async_write_ha_state()

    @callback
    def _persist_global(self, enabled: bool) -> None:
        """Persist ``globally_enabled`` back to ``entry.options``."""
        if self._entry.options.get(CONF_GLOBALLY_ENABLED) == enabled:
            return
        options = dict(self._entry.options)
        options[CONF_GLOBALLY_ENABLED] = enabled
        self.hass.config_entries.async_update_entry(self._entry, options=options)
