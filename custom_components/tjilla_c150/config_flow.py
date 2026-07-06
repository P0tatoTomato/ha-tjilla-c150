"""Config flow voor Tjilla C150.


- Kamer-configuratie zit nu OOK in de initiële config flow (niet alleen in de
  options flow achteraf). Na de verbindingstest volgen dezelfde twee
  kamerstappen; de kamerdata wordt als options bij de nieuwe entry opgeslagen.
- Config- en options-flow delen dezelfde schema-helpers (geen duplicatie).
- ID-schuifbalk begint bij 1 (min=1) en de max schaalt mee: aantal kamers + 5.
- Aantal kamers mag 0 zijn (geen kamerselectie); default is 1.

Onbenoemde kamers krijgen automatisch "Ruimte <id>". De configuratie kan later
altijd opnieuw geopend en aangepast worden via Configureren.
"""
from __future__ import annotations

import logging
from typing import Any

import tinytuya
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import callback

from .const import (
    DOMAIN, DEFAULT_NAME, DEFAULT_SCAN_INTERVAL, CONF_ROOM_COUNT,
)

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE_ID     = "device_id"
CONF_LOCAL_KEY     = "local_key"
CONF_SCAN_INTERVAL = "scan_interval"

MAX_ROOMS = 20     # bovengrens voor het aantal in te voeren kamers
ID_MARGIN = 5      # ID-schuifbalk loopt tot (aantal kamers + deze marge)

STEP_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST):      str,
    vol.Required(CONF_DEVICE_ID): str,
    vol.Required(CONF_LOCAL_KEY): str,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
})


def _test_connection(host: str, device_id: str, local_key: str) -> dict:
    device = tinytuya.Device(
        dev_id=device_id,
        address=host,
        local_key=local_key,
        version="3.3",
    )
    device.set_socketTimeout(5)
    status = device.status()
    if "Error" in status or "error" in status:
        raise ConnectionError(
            status.get("Error", status.get("error", "Onbekend"))
        )
    return status


# ── Gedeelde schema-helpers (config- én options-flow) ───────────────────────

def build_count_schema(current_count: int, current_scan: int) -> vol.Schema:
    """Pagina 1: aantal kamers + scan-interval.

    Aantal mag 0 zijn (geen kamerselectie). Default 1.
    """
    return vol.Schema({
        vol.Required(
            CONF_ROOM_COUNT, default=current_count
        ): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_ROOMS)),
        vol.Required(
            CONF_SCAN_INTERVAL, default=current_scan
        ): vol.All(vol.Coerce(int), vol.Range(min=5, max=120)),
    })


def build_rooms_schema(room_count: int, existing: dict | None = None) -> vol.Schema:
    """Pagina 2: per kamer een ID en naam.

    - ID-schuifbalk: min=1, max=room_count+ID_MARGIN (schaalt mee met aantal
      kamers; ruim genoeg voor niet-aaneengesloten ID's, niet absurd breed).
    - Naam-veld optioneel; leeg → later "Ruimte <id>".
    - Voorgevuld met bestaande waarden indien aanwezig.
    """
    existing = existing or {}
    id_max = room_count + ID_MARGIN
    fields: dict = {}
    for i in range(room_count):
        default_id = existing.get(f"room_id_{i}", i + 1)
        # Clamp voorgevulde default binnen de (mogelijk gewijzigde) range
        if default_id < 1:
            default_id = 1
        elif default_id > id_max:
            default_id = id_max
        default_name = existing.get(f"room_name_{i}", "")
        fields[vol.Required(
            f"room_id_{i}", default=default_id
        )] = vol.All(vol.Coerce(int), vol.Range(min=1, max=id_max))
        fields[vol.Optional(
            f"room_name_{i}", default=default_name
        )] = str
    return vol.Schema(fields)


def rooms_data_from_input(
    room_count: int, scan_interval: int, user_input: dict
) -> dict:
    """Bouw de options-data dict uit de kamer-invoer."""
    data = {
        CONF_ROOM_COUNT: room_count,
        CONF_SCAN_INTERVAL: scan_interval,
    }
    for i in range(room_count):
        data[f"room_id_{i}"] = int(user_input[f"room_id_{i}"])
        data[f"room_name_{i}"] = (user_input.get(f"room_name_{i}") or "").strip()
    return data


# ── Config flow (initiële setup) ────────────────────────────────────────────

class TjillaC150ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2
    MINOR_VERSION = 4

    def __init__(self) -> None:
        self._base_data: dict[str, Any] = {}
        self._room_count: int = 0
        self._scan_interval: int = DEFAULT_SCAN_INTERVAL

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host      = user_input[CONF_HOST].strip()
            device_id = user_input[CONF_DEVICE_ID].strip()
            local_key = user_input[CONF_LOCAL_KEY].strip()

            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()

            try:
                await self.hass.async_add_executor_job(
                    _test_connection, host, device_id, local_key
                )
            except ConnectionError as err:
                _LOGGER.error("Connection error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during connection test")
                errors["base"] = "unknown"
            else:
                self._base_data = {
                    CONF_HOST:      host,
                    CONF_DEVICE_ID: device_id,
                    CONF_LOCAL_KEY: local_key,
                    CONF_NAME:      user_input.get(CONF_NAME, DEFAULT_NAME),
                }
                # Door naar de kamer-configuratie (aantal → ID's+namen)
                return await self.async_step_room_count()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_room_count(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Initiële setup pagina 2: aantal kamers + scan-interval."""
        if user_input is not None:
            self._room_count = int(user_input[CONF_ROOM_COUNT])
            self._scan_interval = int(user_input[CONF_SCAN_INTERVAL])
            if self._room_count <= 0:
                return self.async_create_entry(
                    title=self._base_data[CONF_NAME],
                    data=self._base_data,
                    options={
                        CONF_ROOM_COUNT: 0,
                        CONF_SCAN_INTERVAL: self._scan_interval,
                    },
                )
            return await self.async_step_room_details()

        return self.async_show_form(
            step_id="room_count",
            data_schema=build_count_schema(1, DEFAULT_SCAN_INTERVAL),
        )

    async def async_step_room_details(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Initiële setup pagina 3: per kamer ID + naam."""
        if user_input is not None:
            options = rooms_data_from_input(
                self._room_count, self._scan_interval, user_input
            )
            return self.async_create_entry(
                title=self._base_data[CONF_NAME],
                data=self._base_data,
                options=options,
            )

        return self.async_show_form(
            step_id="room_details",
            data_schema=build_rooms_schema(self._room_count),
            description_placeholders={"count": str(self._room_count)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "TjillaC150OptionsFlow":
        return TjillaC150OptionsFlow(config_entry)


# ── Options flow (achteraf aanpassen) ───────────────────────────────────────

class TjillaC150OptionsFlow(config_entries.OptionsFlow):
    """Tweetraps opties: pagina 1 aantal kamers, pagina 2 ID + naam per kamer."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._room_count: int = 0
        self._scan_interval: int = DEFAULT_SCAN_INTERVAL

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Pagina 1: aantal kamers + scan-interval."""
        opts = self._config_entry.options

        if user_input is not None:
            self._room_count = int(user_input[CONF_ROOM_COUNT])
            self._scan_interval = int(user_input[CONF_SCAN_INTERVAL])
            if self._room_count <= 0:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_ROOM_COUNT: 0,
                        CONF_SCAN_INTERVAL: self._scan_interval,
                    },
                )
            return await self.async_step_rooms()

        current_count = opts.get(CONF_ROOM_COUNT, 1)
        current_scan = opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=build_count_schema(current_count, current_scan),
        )

    async def async_step_rooms(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Pagina 2: per kamer een ID en naam."""
        if user_input is not None:
            data = rooms_data_from_input(
                self._room_count, self._scan_interval, user_input
            )
            return self.async_create_entry(title="", data=data)

        return self.async_show_form(
            step_id="rooms",
            data_schema=build_rooms_schema(
                self._room_count, dict(self._config_entry.options)
            ),
            description_placeholders={"count": str(self._room_count)},
        )
