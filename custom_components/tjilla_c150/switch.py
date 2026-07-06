"""Switch entities voor Tjilla C150."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.helpers.entity import EntityCategory
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    DP_DISTURB, DP_BREAK_CLEAN,
    DP_AUTO_BOOST, DP_CRUISE, DP_Y_MOP,
)
from .coordinator import TjillaC150Coordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


@dataclass
class TjillaSwitchDescription(SwitchEntityDescription):
    data_key: str = ""
    dp: int = 0


SWITCHES: list[TjillaSwitchDescription] = [
    TjillaSwitchDescription(
        key="do_not_disturb",
        translation_key="do_not_disturb",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:do-not-disturb",
        data_key="do_not_disturb",
        dp=DP_DISTURB
    ),
    # DP27 correct label + waarschuwing.
    # Deze DP was eerder verwarrend "Doorgaan na laden" genoemd, terwijl hij
    # in werkelijkheid "Continue after breaking point" heet in de Tuya app —
    # d.w.z. na een onderbreking (batterij leeg → opladen, dustbin vol, etc.)
    # gaat de robot verder waar hij gebleven was i.p.v. een nieuwe sessie te
    # starten. De handleiding raadt af om dit aan te zetten omdat resume-
    # coördinaten soms onbetrouwbaar zijn en de kaart-state corrupt kan raken.
    TjillaSwitchDescription(
        key="break_clean",
        translation_key="break_clean",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:play-pause",
        data_key="break_clean",
        dp=DP_BREAK_CLEAN
    ),
    TjillaSwitchDescription(
        key="auto_boost",
        translation_key="auto_boost",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:fan-plus",
        data_key="auto_boost",
        dp=DP_AUTO_BOOST
    ),
    TjillaSwitchDescription(
        key="cruise",
        translation_key="cruise",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:compass",
        data_key="cruise",
        dp=DP_CRUISE
    ),
    TjillaSwitchDescription(
        key="y_mop",
        translation_key="y_mop",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:vector-curve",
        data_key="y_mop",
        dp=DP_Y_MOP
    ),
    # master-switch voor Customize Mode (DP39).
    # Wanneer aan, gebruikt robot per-kamer preferences. Setten van die
    # per-kamer preferences zelf (zuigkracht, dweilintensiteit, work_mode,
    # sweep count) zit in DP15 binary protocol-frames die we nog niet
    # hebben gedecodeerd.
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TjillaC150Coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [
        TjillaSwitch(coordinator, entry, desc) for desc in SWITCHES
    ]
    # selectie-switch per kamer voor dashboard-gebruik — aan/uit =
    # in/uit de kamerselectie. Combineer met de knop "Start kamerreiniging"
    # om de geselecteerde kamers als één 0x14-commando te versturen. De
    # aan/uit-staat maakt state-gekleurde dashboard-tiles mogelijk.
    for room_id, room_name in coordinator.get_known_rooms().items():
        entities.append(
            TjillaRoomSelectionSwitch(coordinator, entry, room_id, room_name)
        )
    async_add_entities(entities)


class TjillaSwitch(CoordinatorEntity, SwitchEntity):
    entity_description: TjillaSwitchDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TjillaC150Coordinator,
        entry: ConfigEntry,
        description: TjillaSwitchDescription
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
    def is_on(self) -> bool | None:
        return bool(self.coordinator.data.get(self.entity_description.data_key))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(
            self.entity_description.dp, True
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(
            self.entity_description.dp, False
        )


class TjillaRoomSelectionSwitch(CoordinatorEntity, SwitchEntity):
    """Kamer in/uit de reinigingsselectie (voor dashboard-tiles)."""

    _attr_has_entity_name = True
    _attr_translation_key = "room_selected"
    _attr_icon = "mdi:checkbox-marked-circle-outline"

    def __init__(self, coordinator, entry, room_id: int, room_name: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._room_id = int(room_id)
        self._attr_unique_id = (
            f"{coordinator.device_id}_room_selected_{room_id}"
        )
        self._attr_translation_placeholders = {"room": room_name}

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self.coordinator.device_id)},
            "name":         self._entry.data.get("name", "Tjilla C150"),
            "manufacturer": "Tjilla",
            "model":        "C150",
        }

    @property
    def is_on(self) -> bool:
        return self._room_id in self.coordinator.room_selection

    async def async_turn_on(self, **kwargs) -> None:
        if not self.is_on:
            self.coordinator.toggle_room_selection(self._room_id)

    async def async_turn_off(self, **kwargs) -> None:
        if self.is_on:
            self.coordinator.toggle_room_selection(self._room_id)
