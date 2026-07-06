"""Sensor entities voor Tjilla C150."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import EntityCategory
from homeassistant.const import PERCENTAGE, UnitOfArea, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, HA_STATE_MAP
from .coordinator import TjillaC150Coordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


def _pct_remaining(used_min: int | None, max_min: int) -> int | None:
    """Bereken resterend percentage voor verbruiksitem."""
    if used_min is None:
        return None
    try:
        pct = 100 - (int(used_min) / max_min * 100)
        return max(0, min(100, round(pct)))
    except (ValueError, TypeError, ZeroDivisionError):
        return None


@dataclass
class TjillaSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict], Any] = lambda d: None
    extra_attrs_fn: Callable[[dict], dict] | None = None


SENSORS: list[TjillaSensorDescription] = [
    TjillaSensorDescription(
        key="battery",
        translation_key="battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("battery")
    ),
    TjillaSensorDescription(
        key="clean_time",
        translation_key="clean_time",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-outline",
        value_fn=lambda d: d.get("clean_time")
    ),
    TjillaSensorDescription(
        key="clean_area",
        translation_key="clean_area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-outline",
        value_fn=lambda d: d.get("clean_area")
    ),
    TjillaSensorDescription(
        key="status",
        translation_key="tuya_status",
        device_class=SensorDeviceClass.ENUM,
        options=sorted(HA_STATE_MAP),
        icon="mdi:robot-vacuum",
        value_fn=lambda d: d.get("status")
    ),
    TjillaSensorDescription(
        key="total_area",
        translation_key="total_area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:map-check-outline",
        value_fn=lambda d: d.get("total_area")
    ),
    TjillaSensorDescription(
        key="total_count",
        translation_key="total_count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        value_fn=lambda d: d.get("total_count")
    ),
    TjillaSensorDescription(
        key="total_time",
        translation_key="total_time",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.get("total_time")
    ),
    TjillaSensorDescription(
        key="edge_brush",
        translation_key="edge_brush",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:brush",
        value_fn=lambda d: _pct_remaining(d.get("edge_brush"), 9000),
        extra_attrs_fn=lambda d: {
            "minuten_gebruikt": d.get("edge_brush"),
            "max_minuten": 9000,
        }
    ),
    TjillaSensorDescription(
        key="roll_brush",
        translation_key="roll_brush",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:vacuum",
        value_fn=lambda d: _pct_remaining(d.get("roll_brush"), 18000),
        extra_attrs_fn=lambda d: {
            "minuten_gebruikt": d.get("roll_brush"),
            "max_minuten": 18000,
        }
    ),
    TjillaSensorDescription(
        key="filter",
        translation_key="filter",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:air-filter",
        value_fn=lambda d: _pct_remaining(d.get("filter"), 9000),
        extra_attrs_fn=lambda d: {
            "minuten_gebruikt": d.get("filter"),
            "max_minuten": 9000,
        }
    ),
    TjillaSensorDescription(
        key="duster",
        translation_key="duster",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water",
        value_fn=lambda d: _pct_remaining(d.get("duster"), 9000),
        extra_attrs_fn=lambda d: {
            "minuten_gebruikt": d.get("duster"),
            "max_minuten": 9000,
        }
    ),
    TjillaSensorDescription(
        key="rooms",
        translation_key="rooms",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:floor-plan",
        # telt de geconfigureerde kamerlijst (options-flow + storage).
        value_fn=lambda d: len(d.get("known_rooms", {}) or {}),
        extra_attrs_fn=lambda d: d.get("known_rooms", {}) or {}
    ),
    # cloud-sensoren verwijderd.
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TjillaC150Coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        TjillaC150Sensor(coordinator, entry, desc) for desc in SENSORS
    ]
    # TjillaMapDataSensor verwijderd (kaart-functie weg).
    async_add_entities(entities)


class TjillaC150Sensor(CoordinatorEntity, SensorEntity):
    entity_description: TjillaSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TjillaC150Coordinator,
        entry: ConfigEntry,
        description: TjillaSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{coordinator.device_id}_{description.key}"

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self.coordinator.device_id)},
            "name":         self._entry.data.get("name", "Tjilla C150"),
            "manufacturer": "Tjilla",
            "model":        "C150",
        }

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.extra_attrs_fn:
            return self.entity_description.extra_attrs_fn(self.coordinator.data)
        return None
