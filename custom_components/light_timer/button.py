"""Button platform for the Light Timer integration.

Exposes two per-managed-light action buttons that adapt presses onto the
coordinator's pure control methods:

* :class:`LightTimerCancelButton` -> :meth:`LightTimerCoordinator.async_cancel`
  -- cancel a running timer, leaving the light on (Req 5.1); and
* :class:`LightTimerSuspendButton` -> :meth:`LightTimerCoordinator.async_suspend`
  with no explicit duration, so the light's configured ``default_suspension`` is
  used (Req 6.8).

The platform is a thin adapter: it reads the already-built coordinator from
``hass.data[DOMAIN][entry.entry_id]`` (the convention established by the
integration setup in ``__init__.py``) and creates one cancel button and one
suspend button per managed controller. All decision and side-effect behaviour
lives on the coordinator.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import LightTimerCoordinator, PerLightController


def _friendly_light_name(light_entity_id: str) -> str:
    """Return a human-friendly name fragment for a managed light entity id.

    Strips the ``light.`` domain prefix and turns the object id into a
    title-cased, space-separated label (e.g. ``light.living_room`` ->
    ``Living Room``) for use in the button entity names.
    """
    object_id = light_entity_id.split(".", 1)[-1]
    return object_id.replace("_", " ").title()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the cancel/suspend buttons for each managed light.

    Reads the coordinator stored at ``hass.data[DOMAIN][entry.entry_id]`` and
    creates, per managed controller, one :class:`LightTimerCancelButton` and one
    :class:`LightTimerSuspendButton`.
    """
    coordinator: LightTimerCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[ButtonEntity] = []
    for controller in coordinator.controllers.values():
        entities.append(LightTimerCancelButton(coordinator, entry, controller))
        entities.append(LightTimerSuspendButton(coordinator, entry, controller))

    async_add_entities(entities)


class _LightTimerButtonBase(ButtonEntity):
    """Common wiring for the per-light Light Timer action buttons.

    Holds a reference to the coordinator and the managed light's entity id, and
    declares the button as an integration-controlled (non-pollable) entity.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: LightTimerCoordinator,
        entry: ConfigEntry,
        controller: PerLightController,
    ) -> None:
        """Store the coordinator and the managed light identity."""
        self._coordinator = coordinator
        self._entry = entry
        self._light_id = controller.light_entity_id


class LightTimerCancelButton(_LightTimerButtonBase):
    """Per-light button that cancels a running timer (Req 5.1).

    Pressing the button calls :meth:`LightTimerCoordinator.async_cancel`, which
    stops a running-class timer without turning the light off. When there is no
    active timer the coordinator makes no state change.
    """

    def __init__(
        self,
        coordinator: LightTimerCoordinator,
        entry: ConfigEntry,
        controller: PerLightController,
    ) -> None:
        """Initialise the cancel button with a per-light unique id and name."""
        super().__init__(coordinator, entry, controller)
        self._attr_unique_id = f"{entry.entry_id}_{self._light_id}_cancel"
        self._attr_name = f"Cancel Timer {_friendly_light_name(self._light_id)}"

    async def async_press(self) -> None:
        """Cancel the managed light's running timer (Req 5.1)."""
        self._coordinator.async_cancel(self._light_id)


class LightTimerSuspendButton(_LightTimerButtonBase):
    """Per-light button that suspends timer automation (Req 6.8).

    Pressing the button calls :meth:`LightTimerCoordinator.async_suspend` with no
    explicit duration, so the coordinator uses the light's configured
    ``default_suspension`` value.
    """

    def __init__(
        self,
        coordinator: LightTimerCoordinator,
        entry: ConfigEntry,
        controller: PerLightController,
    ) -> None:
        """Initialise the suspend button with a per-light unique id and name."""
        super().__init__(coordinator, entry, controller)
        self._attr_unique_id = f"{entry.entry_id}_{self._light_id}_suspend"
        self._attr_name = f"Suspend Timer {_friendly_light_name(self._light_id)}"

    async def async_press(self) -> None:
        """Suspend the managed light using the configured default duration (Req 6.8)."""
        self._coordinator.async_suspend(self._light_id)
