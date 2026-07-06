"""Button entities voor Tjilla C150."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.helpers.entity import EntityCategory
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    DP_RESET_EDGE, DP_RESET_ROLL, DP_RESET_FILTER, DP_RESET_DUSTER
)
from .coordinator import TjillaC150Coordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


@dataclass
class TjillaButtonDescription(ButtonEntityDescription):
    dp: int = 0
    action: str = "reset"  # "reset" of "start_selected"


BUTTONS: list[TjillaButtonDescription] = [
    TjillaButtonDescription(
        key="reset_edge_brush",
        translation_key="reset_edge_brush",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:restore",
        dp=DP_RESET_EDGE,
        action="reset"
    ),
    TjillaButtonDescription(
        key="reset_roll_brush",
        translation_key="reset_roll_brush",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:restore",
        dp=DP_RESET_ROLL,
        action="reset"
    ),
    TjillaButtonDescription(
        key="reset_filter",
        translation_key="reset_filter",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:restore",
        dp=DP_RESET_FILTER,
        action="reset"
    ),
    TjillaButtonDescription(
        key="reset_duster",
        translation_key="reset_duster",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:restore",
        dp=DP_RESET_DUSTER,
        action="reset"
    ),
    # start-knop voor de multi-room kamerselectie. De select-entity
    # togglet kamers in/uit de selectie; deze knop voert de selectie uit.
    TjillaButtonDescription(
        key="start_selected_rooms",
        translation_key="start_selected_rooms",
        icon="mdi:play-circle-outline",
        action="start_selected"
    ),
    # "Onderbreek reiniging" button verwijderd — riep async_break_clean
    # aan wat DP27 (persistente setting) toggelde in plaats van te pauzeren.
    # Gebruikers die pauze willen: gebruik de pause-knop op de vacuum-entity
    # (die stuurt DP2 = True, de correcte pause-actie).
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TjillaC150Coordinator = entry.runtime_data
    entities: list[ButtonEntity] = [
        TjillaButton(coordinator, entry, desc) for desc in BUTTONS
    ]
    # één directe knop per geconfigureerde kamer —
    # "Reinig keuken" is één tik i.p.v. de select-flow. De multi-select
    # blijft bestaan voor combinaties. Bij wijziging van de kamerconfig
    # (options-flow) herlaadt de entry en worden de knoppen ververst.
    for room_id, room_name in coordinator.get_known_rooms().items():
        entities.append(
            TjillaRoomCleanButton(coordinator, entry, room_id, room_name)
        )
    async_add_entities(entities)


class TjillaButton(CoordinatorEntity, ButtonEntity):
    entity_description: TjillaButtonDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TjillaC150Coordinator,
        entry: ConfigEntry,
        description: TjillaButtonDescription
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

    async def async_press(self) -> None:
        if self.entity_description.action == "reset":
            await self.coordinator.async_reset_consumable(
                self.entity_description.dp
            )
        elif self.entity_description.action == "start_selected":
            # voert de kamerselectie uit. Raises HomeAssistantError
            # (nette UI-melding) als de selectie leeg is.
            await self.coordinator.async_start_selected_rooms()
        # break_clean action-handler verwijderd samen met de button.


class TjillaRoomCleanButton(CoordinatorEntity, ButtonEntity):
    """Directe éen-tik-reiniging van één kamer (0x14-frame)."""

    _attr_has_entity_name = True
    _attr_translation_key = "clean_room"
    _attr_icon = "mdi:broom"

    def __init__(
        self,
        coordinator: TjillaC150Coordinator,
        entry: ConfigEntry,
        room_id: int,
        room_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._room_id = room_id
        self._attr_unique_id = f"{coordinator.device_id}_clean_room_{room_id}"
        self._attr_translation_placeholders = {"room": room_name}

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self.coordinator.device_id)},
            "name":         self._entry.data.get("name", "Tjilla C150"),
            "manufacturer": "Tjilla",
            "model":        "C150",
        }

    async def async_press(self) -> None:
        await self.coordinator.async_clean_rooms([self._room_id])
