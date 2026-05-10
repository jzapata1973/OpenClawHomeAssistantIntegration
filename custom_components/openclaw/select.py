"""Select entity for the OpenClaw integration.

Provides a Select entity that exposes the list of available models
from the gateway's /v1/models endpoint, allowing the user to switch
the active model from the HA dashboard.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_MODEL, DOMAIN
from .coordinator import OpenClawCoordinator

_LOGGER = logging.getLogger(__name__)

SELECT_DESCRIPTIONS: tuple[SelectEntityDescription, ...] = (
    SelectEntityDescription(
        key="active_model",
        translation_key="active_model",
        name="OpenClaw Active Model",
        icon="mdi:brain",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenClaw select entities from a config entry."""
    entry_data: dict = hass.data[DOMAIN][entry.entry_id]
    coordinator: OpenClawCoordinator = entry_data["coordinator"]

    entities = [
        OpenClawModelSelect(coordinator, description, entry)
        for description in SELECT_DESCRIPTIONS
    ]
    async_add_entities(entities)


class OpenClawModelSelect(CoordinatorEntity[OpenClawCoordinator], SelectEntity):
    """Select entity for switching the active OpenClaw model."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenClawCoordinator,
        description: SelectEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "OpenClaw Assistant",
            "manufacturer": "OpenClaw",
            "model": "OpenClaw Gateway",
        }
        # Initialise from coordinator cache, preferring user's persisted choice.
        # The user's selection in entry.options is the source of truth and must
        # not be overridden by the gateway-reported default model.
        # See upstream issues #8, #24, #28.
        models = coordinator.available_models
        self._attr_options = models if models else ["unknown"]
        self._attr_current_option = self._resolve_current_option(
            entry.options.get("active_model"),
            (coordinator.data or {}).get(DATA_MODEL),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update options when coordinator refreshes; preserve user's choice."""
        models = self.coordinator.available_models
        if models:
            self._attr_options = models
        self._attr_current_option = self._resolve_current_option(
            self._entry.options.get("active_model"),
            (self.coordinator.data or {}).get(DATA_MODEL),
        )
        self.async_write_ha_state()

    def _resolve_current_option(
        self,
        user_selection: str | None,
        gateway_default: str | None,
    ) -> str | None:
        """Pick the option to display: user choice wins over gateway default."""
        if user_selection and user_selection in self._attr_options:
            return user_selection
        if gateway_default and gateway_default in self._attr_options:
            return gateway_default
        return self._attr_options[0] if self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        """Handle the user selecting a new model.

        Persists the selection in the config entry's options so that the
        conversation agent and card can read it via ``get_settings``.
        Then triggers a coordinator refresh.
        """
        _LOGGER.info("OpenClaw active model changed to: %s", option)
        self._attr_current_option = option

        # Store in config entry options so other components can read it
        new_options = dict(self._entry.options)
        new_options["active_model"] = option
        self.hass.config_entries.async_update_entry(
            self._entry, options=new_options
        )

        self.async_write_ha_state()
