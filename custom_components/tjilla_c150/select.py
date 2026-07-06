"""Select entities voor Tjilla C150."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Coroutine

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    SUCTION_LABELS,
    CISTERN_LABELS,
    WORK_MODE_LABELS,
    MOP_STATE_NONE,
)
from .coordinator import TjillaC150Coordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# hardcoded MAX_ROOMS = 7 verwijderd. De kamerlijst komt nu volledig
# uit coordinator.get_known_rooms() — dus alleen daadwerkelijk gedetecteerde
# kamers (firmware / map storage / cloud), geen fantoomkamers meer.


@dataclass
class TjillaSelectDescription(SelectEntityDescription):
    option_labels: dict[str, str] = None
    value_fn: Callable[[dict], str | None] = lambda d: None
    set_fn: Callable[[TjillaC150Coordinator, str], Coroutine] = None


# "closed" / "Uit" is nu een normale optie in beide selects.
# - Bij work_mode=only_mop wordt suction door coordinator naar "closed" forced (display)
# - Bij work_mode=only_sweep wordt cistern naar "closed" forced
# - Bij sweep_and_mop kan de gebruiker direct "Uit" kiezen
# Filtering die er hiervoor zat (k != "closed") is verwijderd zodat de display
# match heeft met een geldige optie en niet als 'unknown' verschijnt.


SELECTS: list[TjillaSelectDescription] = [
    TjillaSelectDescription(
        key="suction",
        translation_key="suction",
        icon="mdi:fan",
        option_labels=SUCTION_LABELS,
        value_fn=lambda d: d.get("suction"),
        set_fn=lambda c, v: c.async_set_suction(v),
    ),
    TjillaSelectDescription(
        key="cistern",
        translation_key="cistern",
        icon="mdi:water",
        option_labels=CISTERN_LABELS,
        value_fn=lambda d: d.get("cistern"),
        set_fn=lambda c, v: c.async_set_cistern(v),
    ),
    TjillaSelectDescription(
        key="work_mode",
        translation_key="work_mode",
        icon="mdi:broom",
        option_labels=WORK_MODE_LABELS,
        value_fn=lambda d: d.get("work_mode"),
        set_fn=lambda c, v: c.async_set_work_mode(v),
    ),
]


class TjillaRoomSelect(CoordinatorEntity, SelectEntity):
    """Kamerselectie voor multi-room reiniging.

    Gedrag:
    - Elke bekende kamer is een optie; aanklikken TOGGELT hem in/uit de
      selectie (feedback via de samenvattings-state en de selectie-
      switches). Er start dus niets direct —
      dat voorkomt per-ongeluk-starts en maakt multi-room mogelijk.
    - De reiniging start via de aparte button "Start kamerreiniging".
    - "Wis selectie" (alleen zichtbaar bij actieve selectie) maakt de
      selectie lokaal leeg.
    - De kamerlijst is dynamisch: alleen daadwerkelijk gedetecteerde kamers
      (firmware DP15 / map storage / cloud). Geen kamers bekend → alleen
      een hint-placeholder.

    De state (current_option) is altijd de samenvattings-placeholder, die
    meetelt hoeveel kamers geselecteerd zijn.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "room_selection"
    _attr_icon = "mdi:door"

    CLEAR_LABEL = "✗ Wis selectie"
    EMPTY_LABEL = "— Geen kamers bekend —"

    def __init__(
        self,
        coordinator: TjillaC150Coordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        # Stabiele unique_id zodat entity-id en dashboards/automations
        # behouden blijven.
        self._attr_unique_id = f"{coordinator.device_id}_room_select"
        # label → room_id mapping, opgebouwd in options
        self._label_to_id: dict[str, int] = {}

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self.coordinator.device_id)},
            "name":         self._entry.data.get("name", "Tjilla C150"),
            "manufacturer": "Tjilla",
            "model":        "C150",
        }

    def _placeholder(self) -> str:
        """Dynamische samenvattings-optie die als state fungeert."""
        selection = self.coordinator.room_selection
        if not selection:
            return "— Kies kamers —"
        n = len(selection)
        return f"— {n} kamer{'s' if n != 1 else ''} geselecteerd —"

    @property
    def options(self) -> list[str]:
        rooms: dict[int, str] = (
            self.coordinator.data.get("known_rooms", {}) or {}
        ) if self.coordinator.data else {}
        selection = self.coordinator.room_selection

        self._label_to_id = {}

        if not rooms:
            return [self.EMPTY_LABEL]

        opts = [self._placeholder()]
        for rid, name in rooms.items():
            # GEEN ✓-prefix meer. HA-core valideert select_option
            # tegen de actuele optielijst; een label dat verandert bij
            # selectie ("✓ Keuken...") maakt dezelfde knop-tap ongeldig na
            # de eerste keer — selecteren lukte, deselecteren niet. Stabiele
            # labels maken togglen via dashboardknoppen/GUI betrouwbaar.
            # Selectie-feedback: de samenvattings-state van deze entity,
            # de selected_rooms-attributen, en de selectie-switches.
            label = f"{name} (ID {rid})"
            opts.append(label)
            self._label_to_id[label] = rid
        if selection:
            opts.append(self.CLEAR_LABEL)
        return opts

    @property
    def current_option(self) -> str | None:
        rooms = (
            self.coordinator.data.get("known_rooms", {}) or {}
        ) if self.coordinator.data else {}
        if not rooms:
            return self.EMPTY_LABEL
        return self._placeholder()

    @property
    def extra_state_attributes(self) -> dict:
        d = self.coordinator.data or {}
        rooms: dict[int, str] = d.get("known_rooms", {}) or {}
        selection: list[int] = d.get("room_selection", []) or []
        return {
            "selected_room_ids": selection,
            "selected_rooms": [rooms.get(rid, f"Kamer {rid}") for rid in selection],
            "available_rooms": rooms,
        }

    async def async_select_option(self, option: str) -> None:
        if option in (self.EMPTY_LABEL,) or option == self._placeholder():
            return

        if option == self.CLEAR_LABEL:
            self.coordinator.clear_room_selection()
            return

        room_id = self._label_to_id.get(option)
        if room_id is None:
            _LOGGER.warning(
                "Kamerselectie: onbekende optie '%s' — lijst mogelijk "
                "verouderd, probeer opnieuw", option,
            )
            return

        # Toggle — de coordinator pusht zelf een data-update waardoor de
        # options/attributes van deze entity direct herberekend worden.
        self.coordinator.toggle_room_selection(room_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TjillaC150Coordinator = entry.runtime_data

    entities: list[SelectEntity] = [
        TjillaC150Select(coordinator, entry, desc) for desc in SELECTS
    ]
    entities.append(TjillaRoomSelect(coordinator, entry))

    async_add_entities(entities)


class TjillaC150Select(CoordinatorEntity, SelectEntity):
    entity_description: TjillaSelectDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TjillaC150Coordinator,
        entry: ConfigEntry,
        description: TjillaSelectDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{coordinator.device_id}_{description.key}"
        # _attr_options niet meer hardcoded — wordt dynamisch berekend
        # in de options property zodat reservoir/work_mode-context meetelt.
        self._label_to_tuya = {v: k for k, v in description.option_labels.items()}

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self.coordinator.device_id)},
            "name":         self._entry.data.get("name", "Tjilla C150"),
            "manufacturer": "Tjilla",
            "model":        "C150",
        }

    def _filtered_raw_options(self) -> list[str]:
        """Return de toegestane raw DP-waardes voor deze select gegeven
        de huidige reservoir- en work_mode-staat.

        
        - work_mode: bij stof reservoir (none) alleen 'only_sweep' tonen
        - cistern:   bij stof reservoir of work_mode=only_sweep alleen 'closed'
        - suction:   bij work_mode=only_mop alleen 'closed'
        """
        d = self.coordinator.data or {}
        mop_state = d.get("mop_state")
        work_mode = d.get("work_mode")
        all_raw = list(self.entity_description.option_labels.keys())
        key = self.entity_description.key

        if key == "work_mode":
            if mop_state == MOP_STATE_NONE:
                return ["only_sweep"]
            return all_raw

        if key == "cistern":
            if mop_state == MOP_STATE_NONE or work_mode == "only_sweep":
                return ["closed"]
            return all_raw

        if key == "suction":
            if work_mode == "only_mop":
                return ["closed"]
            return all_raw

        return all_raw

    @property
    def options(self) -> list[str]:
        labels = self.entity_description.option_labels
        return [labels[raw] for raw in self._filtered_raw_options() if raw in labels]

    @property
    def current_option(self) -> str | None:
        tuya_val = self.entity_description.value_fn(self.coordinator.data)
        if tuya_val is None:
            return None
        # Als de huidige waarde niet meer in de gefilterde opties past
        # (transient state na work_mode wissel), val terug op label van
        # raw waarde zonder een "unknown" te tonen.
        label = self.entity_description.option_labels.get(tuya_val)
        return label

    async def async_select_option(self, option: str) -> None:
        tuya_val = self._label_to_tuya.get(option)
        if tuya_val is None:
            _LOGGER.error("Unknown option: %s", option)
            return
        # Sanity check: gebruiker zou alleen toegestane raw-waarde moeten
        # kunnen picken via dropdown (options filtert), maar service-calls
        # kunnen alles forceren. Log een warning bij mismatch maar laat door.
        if tuya_val not in self._filtered_raw_options():
            _LOGGER.warning(
                "Option '%s' (raw=%s) niet beschikbaar in huidige modus voor "
                "%s — doorstuur maar device kan weigeren",
                option, tuya_val, self.entity_description.key,
            )
        await self.entity_description.set_fn(self.coordinator, tuya_val)
