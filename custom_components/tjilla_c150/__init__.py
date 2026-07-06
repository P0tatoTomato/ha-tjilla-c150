"""Tjilla C150 Robotstofzuiger integratie."""
from __future__ import annotations

import logging
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import (
    DOMAIN,
    DEFAULT_SCAN_INTERVAL,
    CONF_ROOM_COUNT,
)
from .config_flow import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_SCAN_INTERVAL,
)
from .coordinator import TjillaC150Coordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BUTTON,
]


def _rooms_from_options(options: dict) -> dict[int, str]:
    """Bouw {id: naam} uit de options-flow data.

    kamers worden in de options-flow ingevoerd als een aantal (N)
    plus per kamer een ID en naam. We slaan ze op als losse option-keys
    room_id_0..room_id_{N-1} en room_name_0..room_name_{N-1}. Onbenoemde
    kamers krijgen "Ruimte <id>".
    """
    result: dict[int, str] = {}
    count = options.get(CONF_ROOM_COUNT, 0) or 0
    for i in range(int(count)):
        rid = options.get(f"room_id_{i}")
        if rid is None:
            continue
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        name = (options.get(f"room_name_{i}") or "").strip()
        result[rid] = name or f"Ruimte {rid}"
    return result


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migreer een config-entry naar het huidige schema.

    De integratie werkt volledig lokaal. Oudere config-entries kunnen nog
    cloud-velden bevatten uit een eerder schema; die worden hier gestript.
    De version/minor_version-nummers verwijzen naar het config-entry-schema
    van Home Assistant, niet naar de integratieversie.
    """
    current_version = entry.version
    current_minor = getattr(entry, "minor_version", 0)
    _LOGGER.debug(
        "Migrating config entry %s from v%s.%s",
        entry.entry_id, current_version, current_minor,
    )

    data = dict(entry.data)
    new_version = current_version
    new_minor = current_minor

    # Step 1: v1 → v2
    if current_version < 2:
        new_version = 2
        new_minor = 0
        _LOGGER.info("Migrating config entry to v2")

    # Strip cloud-velden: de integratie is volledig lokaal, maar een
    # entry uit een ouder schema kan deze nog bevatten.
    if new_version == 2 and new_minor < 4:
        for k in ("cloud_origin", "cloud_client_id", "cloud_client_secret"):
            data.pop(k, None)
        new_minor = 4
        _LOGGER.info("Config-entry migreren: cloud-velden verwijderen")

    # Apply migration if anything changed
    if new_version != current_version or new_minor != current_minor:
        hass.config_entries.async_update_entry(
            entry, data=data, version=new_version, minor_version=new_minor,
        )
        _LOGGER.info(
            "Config entry migrated to v%d.%d", new_version, new_minor,
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host       = entry.data[CONF_HOST]
    device_id  = entry.data[CONF_DEVICE_ID]
    local_key  = entry.data[CONF_LOCAL_KEY]

    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    coordinator = TjillaC150Coordinator(
        hass=hass,
        host=host,
        device_id=device_id,
        local_key=local_key,
        scan_interval=scan_interval,
    )
    coordinator.config_entry_id = entry.entry_id
    coordinator.config_entry_name = entry.data.get("name", "Tjilla C150")

    # Persistent storage laden (kamers, zones, walls)
    await coordinator.map_storage.async_load()

    # kamers komen uit de options-flow (ID + naam per kamer).
    # Deze vormen de bekende kamerlijst; onbenoemde krijgen "Ruimte <id>".
    rooms = _rooms_from_options(entry.options)
    if rooms:
        coordinator.set_configured_rooms(rooms)

    # Start de persistent connection — vóór de eerste refresh zodat data al binnen is
    await coordinator.async_start_device_manager()

    await coordinator.async_config_entry_first_refresh()

    # modern runtime_data-patroon i.p.v. hass.data.
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await _async_register_services(hass)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: TjillaC150Coordinator | None = (
        getattr(entry, "runtime_data", None)
    )

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        if coordinator is not None:
            try:
                await coordinator.async_stop_device_manager()
            except Exception:  # noqa: BLE001
                # Stop should be idempotent and never fail loudly during unload
                _LOGGER.exception("Failed to stop device manager during unload")

    # Verwijder services als dit de laatste entry was
    loaded = [
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
        and e.state is ConfigEntryState.LOADED
    ]
    if not loaded:
        for svc in ("clean_rooms", "cancel_selection", "set_direction"):
            if hass.services.has_service(DOMAIN, svc):
                hass.services.async_remove(DOMAIN, svc)

    return unload_ok


# ── Services ──────────────────────────────────────────────────────────────

# string EERST, zodat "Keuken" niet faalt op int coercion
SERVICE_CLEAN_ROOMS_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_ids,
    vol.Required("rooms"): vol.All(
        cv.ensure_list,
        [vol.Any(str, vol.Coerce(int))],
    ),
})

SERVICE_CANCEL_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_ids,
})

# SERVICE_REFRESH_CLOUD_MAP_SCHEMA verwijderd.

# SERVICE_BREAK_CLEAN_SCHEMA verwijderd.

SERVICE_SET_DIRECTION_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_ids,
    vol.Required("direction"): vol.In(
        ["forward", "backward", "turn_left", "turn_right", "stop"]
    ),
})


def _find_coordinator_by_entity(
    hass: HomeAssistant, entity_id: str
) -> TjillaC150Coordinator | None:
    """Zoek juiste coordinator op via entity registry — werkt bij meerdere apparaten.

    echte lookup via entity_id → config_entry_id → coordinator.
    """
    registry = er.async_get(hass)
    entry_entry = registry.async_get(entity_id)
    if entry_entry is None:
        # Mogelijk niet in registry (handmatig gemaakt); val terug op device_id match
        _LOGGER.debug("Entity %s niet in registry", entity_id)
        return _find_coordinator_fallback(hass, entity_id)

    config_entry_id = entry_entry.config_entry_id
    if not config_entry_id:
        return _find_coordinator_fallback(hass, entity_id)

    ce = hass.config_entries.async_get_entry(config_entry_id)
    return getattr(ce, "runtime_data", None) if ce else None


def _find_coordinator_fallback(
    hass: HomeAssistant, entity_id: str
) -> TjillaC150Coordinator | None:
    """Fallback wanneer entity registry geen config_entry_id heeft.

    Bij één geconfigureerd apparaat returnen we dat; bij meerdere kunnen
    we zonder registry-info niet betrouwbaar matchen en loggen een
    waarschuwing met de eerste (best-effort gedrag, in praktijk komt
    deze fallback bijna nooit aan bod).
    """
    coords_list = [
        e.runtime_data for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]

    if not coords_list:
        return None
    if len(coords_list) == 1:
        return coords_list[0]

    _LOGGER.warning(
        "Multiple Tjilla devices configured but entity %s not found in "
        "registry — falling back to first device. Pass the correct entity_id.",
        entity_id,
    )
    return coords_list[0]


async def _async_register_services(hass: HomeAssistant) -> None:
    """Registreer de services eenmalig."""

    if hass.services.has_service(DOMAIN, "clean_rooms"):
        return

    async def async_clean_rooms(call: ServiceCall) -> None:
        entity_ids = call.data["entity_id"]
        rooms_raw = call.data["rooms"]

        for eid in entity_ids:
            coord = _find_coordinator_by_entity(hass, eid)
            if not coord:
                raise HomeAssistantError(
                    f"No Tjilla C150 coordinator found for {eid}"
                )

            # Converteer namen → IDs
            room_ids: list[int] = []
            # gebruik de gemergde kamerlijst (firmware + storage +
            # cloud) i.p.v. alleen firmware-DP15-namen. Zo lost de service
            # namen consistent op met wat de gebruiker in de UI ziet.
            known = coord.get_known_rooms() or {}

            for r in rooms_raw:
                if isinstance(r, int):
                    room_ids.append(r)
                    continue

                # String: eerst als naam (case-insensitive), dan als int
                r_str = str(r).strip()
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
                        _LOGGER.warning(
                            "Unknown room '%s' — available: %s",
                            r_str,
                            list(known.values()) + list(known.keys()),
                        )

            if room_ids:
                _LOGGER.info("clean_rooms: %s → IDs %s", rooms_raw, room_ids)
                await coord.async_clean_rooms(room_ids)
            else:
                _LOGGER.warning("No valid rooms in rooms=%s", rooms_raw)


    async def async_cancel_selection(call: ServiceCall) -> None:
        for eid in call.data["entity_id"]:
            coord = _find_coordinator_by_entity(hass, eid)
            if coord:
                await coord.async_cancel_room_selection()


    # `async_refresh_cloud_map` verwijderd (cloud volledig weg).

    # `async_break_clean` handler verwijderd. Zie services.yaml
    # voor toelichting. Voor onderbreken zonder dock: gebruik vacuum.pause.

    async def async_set_direction(call: ServiceCall) -> None:
        direction = call.data["direction"]
        for eid in call.data["entity_id"]:
            coord = _find_coordinator_by_entity(hass, eid)
            if not coord:
                continue
            await coord.async_set_direction(direction)

    hass.services.async_register(
        DOMAIN, "clean_rooms", async_clean_rooms,
        schema=SERVICE_CLEAN_ROOMS_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "cancel_selection", async_cancel_selection,
        schema=SERVICE_CANCEL_SCHEMA,
    )
    # refresh_cloud_map registratie verwijderd (cloud weg).
    # `break_clean` service registratie verwijderd.
    hass.services.async_register(
        DOMAIN, "set_direction", async_set_direction,
        schema=SERVICE_SET_DIRECTION_SCHEMA,
    )
