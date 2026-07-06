"""Binary sensor entities for Tjilla C150."""
from __future__ import annotations

import logging

from homeassistant.helpers.entity import EntityCategory
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MOP_STATE_INSTALLED
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
        [
            TjillaConnectionBinarySensor(coordinator, entry),
            TjillaErrorBinarySensor(coordinator, entry),
            TjillaWaterReservoirBinarySensor(coordinator, entry),
        ]
    )


class TjillaC150BinarySensorBase(CoordinatorEntity, BinarySensorEntity):
    """Base class voor binary sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TjillaC150Coordinator,
        entry: ConfigEntry,
        unique_suffix: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{coordinator.device_id}_{unique_suffix}"

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self.coordinator.device_id)},
            "name":         self._entry.data.get("name", "Tjilla C150"),
            "manufacturer": "Tjilla",
            "model":        "C150",
        }


class TjillaConnectionBinarySensor(TjillaC150BinarySensorBase):
    """True wanneer de robot lokaal bereikbaar is."""

    _attr_translation_key = "connection"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:lan-connect"

    def __init__(
        self,
        coordinator: TjillaC150Coordinator,
        entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, "connection")

    @property
    def is_on(self) -> bool:
        return self.coordinator.is_device_connected

    @property
    def available(self) -> bool:
        # Connection sensor moet altijd beschikbaar zijn — anders zou hij
        # 'unavailable' tonen bij verbroken verbinding, wat de hele point
        # van deze sensor tegenwerkt.
        return True


class TjillaErrorBinarySensor(TjillaC150BinarySensorBase):
    """True wanneer de robot een fout heeft."""

    _attr_translation_key = "fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert"

    def __init__(
        self,
        coordinator: TjillaC150Coordinator,
        entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, "error")

    @property
    def is_on(self) -> bool:
        faults = self.coordinator.data.get("faults", []) if self.coordinator.data else []
        return bool(faults)

    @property
    def extra_state_attributes(self) -> dict | None:
        if not self.coordinator.data:
            return None
        return {
            "fault_codes": self.coordinator.data.get("faults", []),
            "fault_bitmap": self.coordinator.data.get("fault_bitmap", 0),
        }


class TjillaWaterReservoirBinarySensor(TjillaC150BinarySensorBase):
    """True wanneer het waterreservoir is geïnstalleerd (DP40 = 'installed').

    Exposes mop_state als gebruiker-vriendelijke binary sensor.
    State `on` = water reservoir aanwezig (dweilen mogelijk)
    State `off` = stof reservoir aanwezig (alleen zuigen)
    """

    _attr_translation_key = "water_reservoir"
    _attr_icon = "mdi:water-pump"

    def __init__(
        self,
        coordinator: TjillaC150Coordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry, "water_reservoir")

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        mop_state = self.coordinator.data.get("mop_state")
        if mop_state is None:
            return None
        return mop_state == MOP_STATE_INSTALLED

    @property
    def extra_state_attributes(self) -> dict | None:
        if not self.coordinator.data:
            return None
        return {
            "mop_state_raw": self.coordinator.data.get("mop_state"),
        }
