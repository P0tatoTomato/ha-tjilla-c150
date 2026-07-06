"""Vacuum entity voor Tjilla C150."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    HA_STATE_MAP,
    SUCTION_LABELS,
    ON_DOCK_STATES,
    STOPPED_STATES,
    ACTIVELY_CLEANING_STATES,
    RETURNING_STATES,
    ERROR_STATES,
    STATUS_PAUSED,
)
from .coordinator import TjillaC150Coordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# "closed" / "Uit" is een geldige fan_speed waarde — niet langer filtered.
# - work_mode=only_mop forceert suction display naar "closed" (coordinator)
# - work_mode=sweep_and_mop laat de user direct "Uit" kiezen via fan_speed
FAN_SPEED_LABELS = dict(SUCTION_LABELS)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TjillaC150Coordinator = entry.runtime_data
    async_add_entities(
        [TjillaC150Vacuum(coordinator, entry)]
    )


class TjillaC150Vacuum(CoordinatorEntity, StateVacuumEntity):
    """Tjilla C150 vacuum entity."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        coordinator: TjillaC150Coordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{coordinator.device_id}_vacuum"
        # fan_speed_list dynamisch via property — afhankelijk van work_mode
        # supported_features is nu ook dynamisch (property hieronder),
        # zodat niet-toepasselijke knoppen per toestand verborgen worden.
        self._label_to_tuya = {v: k for k, v in FAN_SPEED_LABELS.items()}

    @property
    def supported_features(self) -> VacuumEntityFeature:
        """Toon knoppen afhankelijk van de robottoestand (geverifieerd gedrag).

        Vaste features (altijd): status, state, fan speed, locate, map,
        send_command. De besturingsknoppen start/pauze/stop/return worden
        per toestand in- of uitgeschakeld, zodat de UI alleen logische acties
        toont:

          - Gedockt/standby/sleep : START (smart clean). Geen pauze/stop/return.
          - Actief aan het reinigen: PAUZE + STOP + RETURN. Geen start
            (die zou pauzeren — bewust verborgen).
          - Gepauzeerd            : START (=hervat) + STOP + RETURN.
          - Onderweg naar dock    : START + PAUZE + STOP.
          - Fout (error)          : als pauze — START(hervat)/STOP/RETURN.

        Kamerselectie loopt via een aparte knop-entity en staat los van deze
        vacuum-besturingsknoppen.
        """
        feats = (
            VacuumEntityFeature.STATUS
            | VacuumEntityFeature.STATE
            | VacuumEntityFeature.FAN_SPEED
            | VacuumEntityFeature.LOCATE
            | VacuumEntityFeature.SEND_COMMAND
        )

        d = self.coordinator.data or {}
        status = d.get("status", "")

        on_dock = status in ON_DOCK_STATES
        stopped = status in STOPPED_STATES     # idle, niet op dock (bv. na stop)
        cleaning = status in ACTIVELY_CLEANING_STATES
        paused = status == STATUS_PAUSED
        returning = status in RETURNING_STATES
        error = status in ERROR_STATES

        # START: tonen wanneer de robot niet actief reinigt.
        #   op dock          → nieuwe smart clean
        #   gestopt/standby   → nieuwe smart clean
        #   paused            → hervat
        #   returning         → nieuwe reiniging
        #   error             → hervat
        if on_dock or stopped or paused or returning or error:
            feats |= VacuumEntityFeature.START

        # PAUZE: tonen tijdens reinigen en tijdens terugrijden.
        if cleaning or returning:
            feats |= VacuumEntityFeature.PAUSE

        # STOP: tonen wanneer er iets te stoppen valt (reinigen, pauze,
        # terugrijden, fout). Niet op dock, niet in standby (al gestopt).
        if cleaning or paused or returning or error:
            feats |= VacuumEntityFeature.STOP

        # RETURN TO DOCK: tonen wanneer de robot NIET al op de dock staat en
        # niet al onderweg is. Dus ook na een stop (standby, staat in de ruimte).
        if cleaning or paused or error or stopped:
            feats |= VacuumEntityFeature.RETURN_HOME

        return feats

    @property
    def device_info(self) -> dict:
        info = self.coordinator.data.get("device_info", {}) or {}
        di = {
            "identifiers": {(DOMAIN, self.coordinator.device_id)},
            "name":         self._entry.data.get("name", "Tjilla C150"),
            "manufacturer": "Tjilla",
            "model":        "C150",
            "sw_version":   info.get("Firmware_Version", "onbekend"),
        }
        if info.get("Mac"):
            di["connections"] = {("mac", info["Mac"])}
        return di

    @property
    def available(self) -> bool:
        """Entity is alleen beschikbaar als de coordinator successvol heeft gedraaid.

        We linken NIET aan device_mgr.connected omdat die tijdelijk False kan zijn
        tijdens een reconnect — de gebruiker zou anders constante UI flicker zien.
        """
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
        )

    @property
    def fan_speed_list(self) -> list[str]:
        """alleen 'Uit' tonen als work_mode = only_mop, anders alle opties."""
        d = self.coordinator.data or {}
        if d.get("work_mode") == "only_mop":
            return [FAN_SPEED_LABELS["closed"]]
        return list(FAN_SPEED_LABELS.values())

    @property
    def activity(self) -> VacuumActivity | None:
        """Robottoestand als VacuumActivity (de moderne HA vacuum-API).

        Vervangt het verouderde state-string-property. HA_STATE_MAP levert
        strings die 1-op-1 overeenkomen met de enum-waarden.
        """
        d = self.coordinator.data or {}
        tuya_status = d.get("status")
        if not tuya_status:
            return None

        mapped = HA_STATE_MAP.get(tuya_status)
        if mapped is not None:
            return VacuumActivity(mapped)

        # Onbekende DP5-status niet blind als idle tonen: val terug op DP4
        # (mode). Actieve reinigings-mode → CLEANING, zodat de stop-knop
        # blijft werken bij firmware-varianten met onbekende status-strings.
        mode = d.get("mode")
        active_modes = {"smart", "select_room", "zone", "pose", "part",
                        "zone_clean", "part_clean", "goto_pos"}
        if mode in active_modes:
            return VacuumActivity.CLEANING

        return VacuumActivity.IDLE

    # battery_level property verwijderd — gebruik sensor.tjilla_batterij
    # (HA 2026.8 deprecation)

    @property
    def fan_speed(self) -> str | None:
        suction = self.coordinator.data.get("suction_raw")
        return FAN_SPEED_LABELS.get(suction)

    @property
    def error(self) -> str | None:
        faults = self.coordinator.data.get("faults", [])
        return ", ".join(faults) if faults else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data or {}

        # toon de geconfigureerde kamerlijst (options-flow + storage),
        # consistent met sensor.bekende_kamers en de kamerselectie-entity.
        known_rooms_dict = d.get("known_rooms", {}) or {}
        room_labels = sorted(known_rooms_dict.values())

        # alleen statische/zelden-wijzigende context. Volatiele
        # waardes (battery, clean_time, clean_area, status, suction, ...)
        # zijn hier bewust WEG: elke attribuutwijziging is een volledige
        # state-rij in de recorder, en losse sensoren loggen dit alles al.
        # Tijdens reinigen scheelde dit een DB-rij per gereinigde m².
        attrs: dict[str, Any] = {
            "mode":       d.get("mode"),
            "rooms":      room_labels,
            "room_count": len(known_rooms_dict),
            "connection": "online" if self.coordinator.is_device_connected else "offline",
        }
        info = d.get("device_info") or {}
        if info:
            attrs["ip"]   = info.get("IP")
            attrs["mac"]  = info.get("Mac")
            attrs["ssid"] = info.get("WiFi_Name")
            attrs["sn"]   = info.get("Device_SN")
        return attrs

    # ── Acties ──────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        await self.coordinator.async_start()

    async def async_pause(self) -> None:
        await self.coordinator.async_pause()

    async def async_stop(self, **kwargs: Any) -> None:
        """Stop = volledig stoppen, terug naar smart-standby (niet naar dock).

        geverifieerd op hardware — stop stopt de huidige taak, zet de
        robot terug naar de standaardmodus (smart) en laat 'm ter plekke staan.
        Terug naar de dock is een aparte actie (return-to-base).
        """
        await self.coordinator.async_stop()

    async def async_return_to_base(self, **kwargs: Any) -> None:
        await self.coordinator.async_return_to_base()

    async def async_locate(self, **kwargs: Any) -> None:
        await self.coordinator.async_locate()

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        tuya_val = self._label_to_tuya.get(fan_speed)
        if tuya_val:
            await self.coordinator.async_set_suction(tuya_val)

    async def async_send_command(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Aangepaste commando's.

        Ondersteunde commando's:
          - clean_rooms: params={"rooms": [1, 7]} of ["Keuken", "Woonkamer"]
          - cancel_selection
        """
        params = params or {}

        if command == "clean_rooms":
            rooms_raw = params.get("rooms", [])
            room_ids: list[int] = []
            # consistent met de service en de select-entity — gebruik
            # de gemergde kamerlijst i.p.v. alleen firmware-DP15-namen.
            known = self.coordinator.data.get("known_rooms", {}) or {}

            for r in rooms_raw:
                if isinstance(r, int):
                    room_ids.append(r)
                elif isinstance(r, str):
                    r_str = r.strip()
                    name_lower = r_str.lower()
                    found_id = None
                    for rid, name in known.items():
                        if name.lower() == name_lower:
                            found_id = rid
                            break
                    if found_id is not None:
                        room_ids.append(found_id)
                    else:
                        try:
                            room_ids.append(int(r_str))
                        except ValueError:
                            _LOGGER.warning("Unknown room: %s", r)
            if room_ids:
                await self.coordinator.async_clean_rooms(room_ids)

        elif command == "cancel_selection":
            await self.coordinator.async_cancel_room_selection()

        else:
            _LOGGER.warning("Unknown command: %s", command)
