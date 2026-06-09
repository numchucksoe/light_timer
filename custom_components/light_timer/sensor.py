"""Remaining-time sensor platform for the Light Timer integration.

This platform exposes one sensor entity per managed light reporting the timer's
remaining time in seconds as the Home Assistant sensor state (Req 10.1, 10.5).
The numeric state and the entity's attributes are derived from the pure
:func:`logic.derive_sensor` helper applied to a snapshot of the light's live
:class:`~.coordinator.PerLightController` runtime state, so the entity stays a
thin adapter over the coordinator and the derivation logic remains
property-tested in isolation (Property 7).

Each sensor exposes the attributes required by Req 10.4 plus the human-readable
``formatted_remaining`` M:SS view (Req 4.1, 4.3):

* ``timer_duration`` -- the configured countdown length in seconds;
* ``enabled`` -- ``False`` exactly when the light is disabled (Req 10.6);
* ``suspension_remaining`` -- remaining suspension seconds, ``0`` when inactive;
* ``failure_active`` -- ``True`` exactly while a shutoff failure is indicated;
* ``formatted_remaining`` -- the numeric state rendered as ``"M:SS"`` via
  :func:`logic.format_remaining`.

The sensors are registered as default/visible entities (``entity_registry_
enabled_default`` defaults to ``True`` and ``entity_registry_visible_default`` is
``True``) so they appear on the Home Assistant Overview automatically (Req 10.2).
Sensors are created when their managed light is added (one entity per controller
at setup) and removed when the light is removed, because the platform is set up
from the per-entry coordinator's controllers and the config-entry reload on
add/remove re-runs ``async_setup_entry`` against the new controller set
(Req 10.1, 10.3).

Coordinator access assumption: ``async_setup_entry`` reads the integration's
:class:`~.coordinator.LightTimerCoordinator` from
``hass.data[DOMAIN][entry.entry_id]``. This is the convention established by the
integration's ``async_setup_entry`` in ``__init__.py`` (task 8.4); when that
data is not yet present (e.g. the platform is set up before the coordinator is
stored) no entities are added.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SIGNAL_TIMER_TICK, LightTimerCoordinator, PerLightController
from .logic import ControllerRuntimeState, SensorRepresentation, derive_sensor


def _get_light_friendly_name(hass: HomeAssistant, light_entity_id: str) -> str:
    """Get the friendly name of a light entity, falling back to object_id.

    Looks up the current state of the light entity in the HA state machine. If
    it has a ``friendly_name`` attribute, that is returned. Otherwise, the
    object_id portion of the entity ID is converted to a human-readable form
    (underscores replaced with spaces, title-cased).
    """
    state = hass.states.get(light_entity_id)
    if state and state.attributes.get("friendly_name"):
        return state.attributes["friendly_name"]
    # Fallback: strip domain and title-case the object_id
    return light_entity_id.split(".", 1)[-1].replace("_", " ").title()


def _get_light_device_info(hass: HomeAssistant, light_entity_id: str) -> DeviceInfo | None:
    """Look up the device that owns the light entity, if any.

    Returns a DeviceInfo with the light's device identifiers so that timer
    entities attach to the same device as the light. Returns None if the light
    has no device (e.g. template lights), in which case callers fall back to a
    standalone Timer_Device.
    """
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
    """Set up the remaining-time sensors for the config entry's managed lights.

    Reads the integration coordinator from ``hass.data[DOMAIN][entry.entry_id]``
    (task 8.4 convention) and creates one :class:`LightTimerRemainingSensor` per
    managed controller. Because the config entry is reloaded on add/remove of a
    managed light, this runs again with the updated controller set, which is what
    creates a sensor on add and removes it on removal (Req 10.1, 10.3).
    """
    coordinator: LightTimerCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is None:
        # The coordinator has not been stored yet; nothing to expose.
        return

    entities = [
        LightTimerRemainingSensor(coordinator, controller)
        for controller in coordinator.controllers.values()
    ]
    if entities:
        async_add_entities(entities)


class LightTimerRemainingSensor(SensorEntity):
    """Per-light sensor reporting the timer's remaining time in seconds.

    The entity is a thin adapter: every read of :attr:`native_value` and
    :attr:`extra_state_attributes` projects the light's **live** controller
    runtime state through the pure :func:`logic.derive_sensor` helper, so the
    sensor always reflects the coordinator's current state without holding its
    own copy (Req 10.4, 10.5, 10.6). The state is the remaining seconds while a
    timer runs and ``0`` for every non-running state (idle, suspended, disabled).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    # Register as a default/visible entity so it appears on the Overview
    # automatically (Req 10.2).
    _attr_entity_registry_enabled_default = True
    _attr_entity_registry_visible_default = True

    def __init__(
        self,
        coordinator: LightTimerCoordinator,
        controller: PerLightController,
    ) -> None:
        """Initialise the sensor for a single managed light.

        Args:
            coordinator: The integration coordinator owning the controllers.
            controller: The per-light controller this sensor reflects.
        """
        self._coordinator = coordinator
        self._light_entity_id = controller.light_entity_id
        # A stable, per-light unique id so the entity persists across reloads
        # and is removed only when its managed light is removed (Req 10.3).
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{self._light_entity_id}_remaining"
        self._attr_name = "Timer remaining"

    @property
    def _controller(self) -> PerLightController | None:
        """Return the live controller for this light, or ``None`` if removed."""
        return self._coordinator.controllers.get(self._light_entity_id)

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator's per-second tick signal."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_TIMER_TICK, self._handle_tick
            )
        )

    @callback
    def _handle_tick(self) -> None:
        """Push a state update to HA on each coordinator tick."""
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device info to attach this entity to the light's device.

        If the managed light belongs to a device, the timer entity attaches to
        that same device so they appear together in the UI. If the light has no
        device (e.g. template lights), falls back to a standalone Timer_Device.
        """
        info = _get_light_device_info(self.hass, self._light_entity_id)
        if info is not None:
            return info
        # Fallback: standalone Timer_Device
        friendly_name = _get_light_friendly_name(
            self.hass, self._light_entity_id
        )
        return DeviceInfo(
            identifiers={(DOMAIN, self._light_entity_id)},
            name=f"{friendly_name}_timer",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Whether the sensor's managed light is still managed."""
        return self._controller is not None

    def _derive(self) -> SensorRepresentation | None:
        """Project the live controller runtime state via :func:`derive_sensor`."""
        controller = self._controller
        if controller is None:
            return None
        runtime = ControllerRuntimeState(
            state=controller.state,
            remaining_seconds=controller.remaining_seconds,
            timer_duration=controller.config.timer_duration,
            suspension_remaining=controller.suspension_remaining,
        )
        return derive_sensor(runtime)

    @property
    def native_value(self) -> int:
        """The remaining seconds; ``0`` when no timer is running/disabled."""
        representation = self._derive()
        if representation is None:
            return 0
        return representation.state

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """The derived sensor attributes (Req 10.4).

        Always includes ``timer_duration``, ``enabled``, ``suspension_remaining``,
        ``failure_active`` and ``formatted_remaining``.
        """
        representation = self._derive()
        if representation is None:
            return {}
        return dict(representation.attributes)
