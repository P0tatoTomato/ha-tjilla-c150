"""Number entities voor Tjilla C150."""
from __future__ import annotations

import logging

from homeassistant.helpers.entity import EntityCategory
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TjillaC150Coordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TjillaC150Coordinator = entry.runtime_data
    async_add_entities(
        [TjillaVolumeNumber(coordinator, entry)]
    )


class TjillaVolumeNumber(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "volume"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:volume-high"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: TjillaC150Coordinator,
        entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{coordinator.device_id}_volume"

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self.coordinator.device_id)},
            "name":         self._entry.data.get("name", "Tjilla C150"),
            "manufacturer": "Tjilla",
            "model":        "C150",
        }

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.get("volume")

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_volume(int(value))
