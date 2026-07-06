"""Diagnostics voor de Tjilla C150-integratie.

Levert een downloadbare dump voor debugging en support: config (met
GEREDACTE local_key), de actuele coordinator-data, het rauwe DP-snapshot
en de verbindingsstatus. Bevat bewust géén geheimen.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

TO_REDACT = {"local_key", "device_id", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = dict(coordinator.data or {})
    # device_info bevat SN/MAC/SSID — redacten voor publiek delen
    data.pop("device_info", None)

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "connection": {
            "connected": coordinator.is_device_connected,
        },
        "coordinator_data": async_redact_data(data, TO_REDACT),
        "raw_dps": {
            k: v for k, v in (data.get("dps") or {}).items()
            if str(k) not in ("14", "15", "34")  # path-blobs + device_info
        },
    }
