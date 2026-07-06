"""Persistente opslag voor kamerconfiguratie (ID → naam/kleur).

Data staat in HA's storage directory (.storage/tjilla_c150_map.<device_id>).
Historisch bevatte dit bestand ook no-go zones en virtuele muren; die
functies zijn verwijderd (ze schreven alleen lokaal, de robot deed er niets
mee). Bestaande opslag met die sleutels laadt gewoon; alleen 'rooms' wordt
nog gebruikt.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


class MapStorage:
    """Wrapper rond HA Store voor kamerconfiguratie per device."""

    def __init__(self, hass: HomeAssistant, device_id: str) -> None:
        self._hass = hass
        self._device_id = device_id
        self._store: Store = Store(
            hass,
            STORAGE_VERSION,
            f"tjilla_c150_map.{device_id}",
        )
        self._data: dict = {
            "rooms": {},   # room_id (str) → {name, color, ...}
        }
        self._loaded = False

    async def async_load(self) -> None:
        """Laad data vanaf disk (alleen 'rooms' wordt nog gebruikt)."""
        data = await self._store.async_load()
        if data is not None and "rooms" in data:
            self._data["rooms"] = data["rooms"]
        self._loaded = True
        _LOGGER.debug(
            "Map storage geladen: %d kamers", len(self._data["rooms"])
        )

    async def async_save(self) -> None:
        """Schrijf data naar disk."""
        await self._store.async_save(self._data)

    # ── Rooms ──────────────────────────────────────────────────────────

    def get_rooms(self) -> dict[int, dict]:
        """Return alle kamer definities met int-keys (voor HA templates).

        Templates kunnen nu `{{ rooms[5] }}` gebruiken in plaats van `{{ rooms['5'] }}`.
        Storage zelf gebruikt strings (JSON-compat), maar de externe view is int.
        """
        result: dict[int, dict] = {}
        for k, v in self._data["rooms"].items():
            try:
                result[int(k)] = v
            except (ValueError, TypeError):
                # Sla niet-numerieke keys over (zou niet mogen voorkomen)
                _LOGGER.debug("Niet-numerieke room key overgeslagen: %s", k)
        return result

    def get_rooms_raw(self) -> dict[str, dict]:
        """Return alle kamer definities met string-keys (raw storage view)."""
        return dict(self._data["rooms"])

    def get_room(self, room_id: int) -> dict | None:
        return self._data["rooms"].get(str(room_id))

    async def async_save_room(
        self,
        room_id: int,
        name: str | None = None,
        polygon: list[list[float]] | None = None,
        color: str | None = None,
    ) -> None:
        """Maak of update een kamer."""
        rid = str(room_id)
        existing = self._data["rooms"].get(rid, {})

        if name is not None:
            existing["name"] = name
        if polygon is not None:
            existing["polygon"] = [list(p) for p in polygon]
        if color is not None:
            existing["color"] = color

        existing.setdefault("name", f"Kamer {room_id}")
        existing.setdefault("polygon", [])
        existing.setdefault("color", _default_color(room_id))

        self._data["rooms"][rid] = existing
        await self.async_save()
        _LOGGER.info("Kamer %s opgeslagen: %s", rid, existing.get("name"))

    async def async_delete_room(self, room_id: int) -> None:
        rid = str(room_id)
        if rid in self._data["rooms"]:
            del self._data["rooms"][rid]
            await self.async_save()

    # ── No-go zones ────────────────────────────────────────────────────


_COLORS = [
    "#4FC3F7", "#81C784", "#FFB74D", "#BA68C8",
    "#E57373", "#4DB6AC", "#F06292", "#A1887F",
]


def _default_color(room_id: int) -> str:
    return _COLORS[(room_id - 1) % len(_COLORS)]
