"""Config flow for the Light Timer integration.

A single config entry manages every Managed_Light (Req 9.1). The initial
``ConfigFlow`` here only creates that single entry: it carries no managed lights
up front and starts globally enabled. All add/edit/remove of managed lights is
handled by the ``OptionsFlow`` (see design "Config and Options Flow").

The created entry stores its mutable configuration under ``entry.options`` in
the shape documented in the design's "Config Entry Shape":

    {
        "globally_enabled": True,   # master switch state (Req 7.8)
        "lights": [],               # no managed lights required up front
    }
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

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
    SUPPORTED_DOMAINS,
)
from .logic import validate_duration, validate_suspension


class LightTimerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the single-entry config flow for Light Timer."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Create the single integration entry or redirect to add a light.

        Only one entry is supported (Req 9.1). If one already exists the flow
        redirects the user to the options flow so they can add a light via the
        same "Add Integration" button — avoiding the confusing
        ``single_instance_allowed`` abort. Otherwise the entry is created
        immediately with the integration globally enabled and an empty list of
        managed lights, and the user is guided to configure it afterward.
        """
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        return self.async_create_entry(
            title="Light Timer",
            data={},
            options={
                CONF_GLOBALLY_ENABLED: DEFAULT_GLOBALLY_ENABLED,
                CONF_LIGHTS: [],
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> LightTimerOptionsFlow:
        """Return the options flow that manages add/edit/remove of lights."""
        return LightTimerOptionsFlow()


def _extract_domain(entity_id: str) -> str:
    """Extract the domain prefix from an entity ID (substring before first '.').

    Returns the portion before the first '.' character, or an empty string
    if the entity ID contains no '.' character.
    """
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


class LightTimerOptionsFlow(config_entries.OptionsFlow):
    """Options flow for managing the lights in the single config entry.

    All add/edit/remove of managed lights is performed here (Req 9.2-9.9). The
    menu exposes ``add_light`` (Req 9.2-9.4), ``edit_light`` (Req 9.5, 9.9) and
    ``remove_light`` (Req 9.6, 9.7).
    """

    def __init__(self) -> None:
        """Initialise the options flow."""
        # The managed-light entity id selected during a two-step edit/remove
        # flow, carried between the pick step and the form/confirm step.
        self._selected_light_id: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the options menu (add / edit / remove a managed light).

        When there are no managed lights yet the user is taken directly to the
        ``add_light`` form — the menu would be confusing since edit/remove have
        nothing to act on and the user's intent is almost certainly to add their
        first light.
        """
        if not self._current_lights():
            return await self.async_step_add_light()

        return self.async_show_menu(
            step_id="init",
            menu_options=["add_light", "edit_light", "remove_light"],
        )

    def _current_lights(self) -> list[dict[str, Any]]:
        """Return a copy of the currently configured managed lights."""
        return list(self.config_entry.options.get(CONF_LIGHTS, []))

    def _light_entity_ids(self) -> list[str]:
        """Return the entity ids of every currently managed light."""
        return [
            light.get(CONF_LIGHT_ENTITY_ID)
            for light in self._current_lights()
            if light.get(CONF_LIGHT_ENTITY_ID)
        ]

    def _find_light(self, entity_id: str) -> dict[str, Any] | None:
        """Return the managed-light dict for ``entity_id`` or ``None``."""
        for light in self._current_lights():
            if light.get(CONF_LIGHT_ENTITY_ID) == entity_id:
                return light
        return None

    @staticmethod
    def _add_light_schema(user_input: dict[str, Any] | None) -> vol.Schema:
        """Build the add-light form schema, retaining prior values (Req 9.9)."""
        data = user_input or {}
        return vol.Schema(
            {
                vol.Required(
                    CONF_LIGHT_ENTITY_ID,
                    default=data.get(CONF_LIGHT_ENTITY_ID),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=list(SUPPORTED_DOMAINS))
                ),
                vol.Required(
                    CONF_TIMER_DURATION,
                    default=data.get(CONF_TIMER_DURATION, DEFAULT_TIMER_DURATION),
                ): int,
                vol.Required(
                    CONF_DEFAULT_SUSPENSION,
                    default=data.get(CONF_DEFAULT_SUSPENSION, DEFAULT_SUSPENSION),
                ): int,
                vol.Optional(
                    CONF_NOTIFICATION_SERVICE,
                    description={
                        "suggested_value": data.get(CONF_NOTIFICATION_SERVICE)
                    },
                ): str,
                vol.Required(
                    CONF_ENABLED,
                    default=data.get(CONF_ENABLED, DEFAULT_ENABLED),
                ): bool,
            }
        )

    async def async_step_add_light(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Add a managed light with validation (Req 9.2, 9.3, 9.4, 9.8, 9.9).

        Validates that the selected entity is a ``light.*`` that exists in HA
        and is not already managed, and validates ``timer_duration`` and
        ``default_suspension`` via the pure validators. On any error the form is
        redisplayed with a per-field error and the submitted values retained
        (Req 9.9). On success the new light is appended to ``options["lights"]``,
        the entry is updated, and the entry is reloaded so the entity platforms
        reflect the change (Req 10.1). The reload re-arms a timer for the added
        light when it is enabled and currently on (Req 9.8) via the coordinator
        setup re-arm, so persisting the option here is sufficient.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            entity_id = user_input.get(CONF_LIGHT_ENTITY_ID)

            # Validate the entity: domain membership → existence → duplicate.
            if not isinstance(entity_id, str) or _extract_domain(entity_id) not in SUPPORTED_DOMAINS:
                errors[CONF_LIGHT_ENTITY_ID] = "not_supported_domain"
            elif self.hass.states.get(entity_id) is None:
                errors[CONF_LIGHT_ENTITY_ID] = "entity_not_found"
            elif any(
                light.get(CONF_LIGHT_ENTITY_ID) == entity_id
                for light in self._current_lights()
            ):
                errors[CONF_LIGHT_ENTITY_ID] = "already_managed"

            # Validate the durations via the shared pure validators.
            duration_result = validate_duration(
                user_input.get(CONF_TIMER_DURATION), DEFAULT_TIMER_DURATION
            )
            if not duration_result.ok:
                errors[CONF_TIMER_DURATION] = duration_result.error

            suspension_result = validate_suspension(
                user_input.get(CONF_DEFAULT_SUSPENSION), DEFAULT_SUSPENSION
            )
            if not suspension_result.ok:
                errors[CONF_DEFAULT_SUSPENSION] = suspension_result.error

            if not errors:
                notification_service = user_input.get(CONF_NOTIFICATION_SERVICE) or None
                new_light = {
                    CONF_LIGHT_ENTITY_ID: entity_id,
                    CONF_TIMER_DURATION: duration_result.value,
                    CONF_DEFAULT_SUSPENSION: suspension_result.value,
                    CONF_NOTIFICATION_SERVICE: notification_service,
                    CONF_ENABLED: bool(user_input.get(CONF_ENABLED, DEFAULT_ENABLED)),
                }

                new_options = dict(self.config_entry.options)
                new_options[CONF_LIGHTS] = [*self._current_lights(), new_light]

                self.hass.config_entries.async_update_entry(
                    self.config_entry, options=new_options
                )
                # Reload so entity platforms are created and the coordinator
                # re-arms a timer when the added light is enabled and on
                # (Req 9.8, 10.1). Persisting the option above is what the
                # setup re-arm reads.
                await self.hass.config_entries.async_reload(
                    self.config_entry.entry_id
                )

                return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(
            step_id="add_light",
            data_schema=self._add_light_schema(user_input),
            errors=errors,
        )

    # --- edit_light -------------------------------------------------------
    @staticmethod
    def _edit_light_schema(light: dict[str, Any]) -> vol.Schema:
        """Build the edit form schema prefilled from ``light`` (Req 9.5, 9.9).

        The light entity field is intentionally omitted: the managed entity is
        immutable after add (Req 9.5). Durations are prefilled from the current
        values and re-validated on submit (Req 9.9).
        """
        return vol.Schema(
            {
                vol.Required(
                    CONF_TIMER_DURATION,
                    default=light.get(CONF_TIMER_DURATION, DEFAULT_TIMER_DURATION),
                ): int,
                vol.Required(
                    CONF_DEFAULT_SUSPENSION,
                    default=light.get(CONF_DEFAULT_SUSPENSION, DEFAULT_SUSPENSION),
                ): int,
                vol.Optional(
                    CONF_NOTIFICATION_SERVICE,
                    description={
                        "suggested_value": light.get(CONF_NOTIFICATION_SERVICE)
                    },
                ): str,
                vol.Required(
                    CONF_ENABLED,
                    default=light.get(CONF_ENABLED, DEFAULT_ENABLED),
                ): bool,
            }
        )

    async def async_step_edit_light(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick which managed light to edit (Req 9.5).

        Aborts with ``no_lights`` when there are no managed lights to edit.
        Otherwise shows a select of the current managed-light entity ids and,
        on submit, stores the selection and advances to the prefilled edit form.
        """
        light_ids = self._light_entity_ids()
        if not light_ids:
            return self.async_abort(reason="no_lights")

        if user_input is not None:
            self._selected_light_id = user_input[CONF_LIGHT_ENTITY_ID]
            return await self.async_step_edit_light_details()

        return self.async_show_form(
            step_id="edit_light",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LIGHT_ENTITY_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=light_ids,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_edit_light_details(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show/process the prefilled edit form (Req 9.5, 9.9).

        The selected light's entity id is read-only (omitted from the form,
        Req 9.5). Durations are validated via the shared validators with
        per-field errors and the form is redisplayed on error (Req 9.9). On
        success the light's dict is updated in ``options["lights"]``, the entry
        is updated and reloaded so the change takes effect (Req 10.1).
        """
        entity_id = self._selected_light_id
        light = self._find_light(entity_id) if entity_id else None
        if light is None:
            # Selection lost (e.g. entry changed underneath); restart the pick.
            return await self.async_step_edit_light()

        errors: dict[str, str] = {}

        if user_input is not None:
            duration_result = validate_duration(
                user_input.get(CONF_TIMER_DURATION), light.get(CONF_TIMER_DURATION)
            )
            if not duration_result.ok:
                errors[CONF_TIMER_DURATION] = duration_result.error

            suspension_result = validate_suspension(
                user_input.get(CONF_DEFAULT_SUSPENSION),
                light.get(CONF_DEFAULT_SUSPENSION),
            )
            if not suspension_result.ok:
                errors[CONF_DEFAULT_SUSPENSION] = suspension_result.error

            if not errors:
                notification_service = (
                    user_input.get(CONF_NOTIFICATION_SERVICE) or None
                )
                updated_light = {
                    CONF_LIGHT_ENTITY_ID: entity_id,
                    CONF_TIMER_DURATION: duration_result.value,
                    CONF_DEFAULT_SUSPENSION: suspension_result.value,
                    CONF_NOTIFICATION_SERVICE: notification_service,
                    CONF_ENABLED: bool(
                        user_input.get(CONF_ENABLED, DEFAULT_ENABLED)
                    ),
                }

                new_lights = [
                    updated_light
                    if existing.get(CONF_LIGHT_ENTITY_ID) == entity_id
                    else existing
                    for existing in self._current_lights()
                ]
                new_options = dict(self.config_entry.options)
                new_options[CONF_LIGHTS] = new_lights

                self.hass.config_entries.async_update_entry(
                    self.config_entry, options=new_options
                )
                await self.hass.config_entries.async_reload(
                    self.config_entry.entry_id
                )

                self._selected_light_id = None
                return self.async_create_entry(title="", data=new_options)

            # Retain submitted values on error (Req 9.9).
            light = {**light, **user_input}

        return self.async_show_form(
            step_id="edit_light_details",
            data_schema=self._edit_light_schema(light),
            errors=errors,
            description_placeholders={"light_entity_id": entity_id},
        )

    # --- remove_light -----------------------------------------------------
    async def async_step_remove_light(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick which managed light to remove (Req 9.6).

        Aborts with ``no_lights`` when there are no managed lights. Otherwise
        shows a select of managed-light entity ids and, on submit, stores the
        selection and advances to an explicit confirmation step (Req 9.6).
        """
        light_ids = self._light_entity_ids()
        if not light_ids:
            return self.async_abort(reason="no_lights")

        if user_input is not None:
            self._selected_light_id = user_input[CONF_LIGHT_ENTITY_ID]
            return await self.async_step_remove_light_confirm()

        return self.async_show_form(
            step_id="remove_light",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LIGHT_ENTITY_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=light_ids,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_remove_light_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Confirm and perform the removal (Req 9.6, 9.7).

        Requires an explicit confirmation submission (Req 9.6). On confirm the
        selected light is removed from ``options["lights"]``, the entry is
        updated and reloaded; the coordinator rebuild on reload stops the timer,
        removes the light's entities and stops monitoring (Req 9.7).
        """
        entity_id = self._selected_light_id
        light = self._find_light(entity_id) if entity_id else None
        if light is None:
            return await self.async_step_remove_light()

        if user_input is not None:
            new_lights = [
                existing
                for existing in self._current_lights()
                if existing.get(CONF_LIGHT_ENTITY_ID) != entity_id
            ]
            new_options = dict(self.config_entry.options)
            new_options[CONF_LIGHTS] = new_lights

            self.hass.config_entries.async_update_entry(
                self.config_entry, options=new_options
            )
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)

            self._selected_light_id = None
            return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(
            step_id="remove_light_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"light_entity_id": entity_id},
        )
