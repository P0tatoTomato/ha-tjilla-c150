"""Data coördinator voor de Tjilla C150."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import timedelta
from typing import Any, Iterable, Optional

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
)

from .const import (
    DOMAIN,
    DEFAULT_SCAN_INTERVAL,
    ACTIVELY_CLEANING_STATES,
    STATUS_SMART,
    STATUS_SELECT_ROOM,
    STATUS_PAUSED,
    STATUS_STANDBY,
    STATUS_GOTO_CHARGE,
    RETURNING_STATES,
    MOP_STATE_NONE, MOP_STATE_INSTALLED,
    LOCATE_AUTO_DISABLE_DELAY,
    # DPs
    DP_POWER_GO, DP_PAUSE, DP_SWITCH_CHARGE,
    DP_STATUS, DP_BATTERY, DP_CLEAN_TIME, DP_CLEAN_AREA,
    DP_SUCTION, DP_CISTERN, DP_WORK_MODE, DP_MODE,
    DP_SEEK, DP_FAULT,
    DP_TOTAL_AREA, DP_TOTAL_COUNT, DP_TOTAL_TIME,
    DP_EDGE_BRUSH, DP_ROLL_BRUSH, DP_FILTER, DP_DUSTER,
    DP_DISTURB, DP_VOLUME, DP_BREAK_CLEAN,
    DP_AUTO_BOOST, DP_CRUISE, DP_Y_MOP, DP_CUSTOMIZE,
    DP_DEVICE_INFO, DP_MOP_STATE,
    DP_UNSEEN_MSG,
    # Aliassen voor inconsistente device-vs-doc enum strings
    WORK_MODE_ALIASES,
    # Protocol
    build_select_rooms, parse_fault_bitmap,
)
from .map_storage import MapStorage
from .local_device import LocalDeviceManager, ConnectionLostError

_LOGGER = logging.getLogger(__name__)


def _dp(dps: dict, dp: int, default: Any = None) -> Any:
    """Haal DP waarde op — handelt integer/string keys en None-waardes correct af."""
    v = dps.get(dp)
    if v is None:
        v = dps.get(str(dp))
    return v if v is not None else default


class TjillaC150Coordinator(DataUpdateCoordinator):
    """Coördineert alle data van de Tjilla C150."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        device_id: str,
        local_key: str,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        self.host       = host
        self.device_id  = device_id
        self.local_key  = local_key

        # Persistent connection manager (vervangt directe tinytuya calls)
        self._device_mgr = LocalDeviceManager(
            host=host,
            device_id=device_id,
            local_key=local_key,
            protocol_version="3.3",
            on_status_update=self._on_dps_pushed,
            on_connection_change=self._on_connection_change,
        )
        self._connection_alive = False
        self._disconnected_since: float | None = None

        # Voor Repairs — wordt ingesteld door __init__ na coordinator creatie
        self.config_entry_id: str = ""
        self.config_entry_name: str = "Tjilla C150"

        # Optimistic state buffer — DPs die we lokaal hebben gezet maar nog
        # niet bevestigd zijn door device.  Wordt opgeschoond bij echte status update.
        self._optimistic_dps: dict[int, Any] = {}
        self._optimistic_clear_at: dict[int, float] = {}

        # Reservoir-aware UX state tracking
        # _prev_* zijn None op fresh init zodat eerste cycle geen auto-default trigger
        self._prev_mop_state: Optional[str] = None
        self._prev_work_mode: Optional[str] = None
        # Rolling laatst-bekende niet-"closed" waarde voor restore na work_mode wissel
        self._last_suction: str = "normal"
        self._last_cistern: str = "low"
        # Internal lock om race conditions in handlers te voorkomen
        self._reservoir_lock = asyncio.Lock()

        # Parsed state
        self._device_info: dict = {}

        # Kamer-selectie voor multi-room reiniging.
        # De select-entity togglet kamers in/uit deze set; de start-button
        # voert de selectie uit. Selectie is bewust NIET persistent over
        # HA-restarts — een halfvergeten selectie van gisteren die onverwacht
        # start is erger dan opnieuw moeten kiezen.
        self._room_selection: set[int] = set()

        # kamers geconfigureerd via de options-flow ({id: naam}).
        # Dit is de enige bron van bekende kamers sinds cloud/firmware-push
        # geen kamerlijst leveren.
        self._configured_rooms: dict[int, str] = {}

        # Persistent storage voor kamerdata (namen, zones, walls)
        self.map_storage: MapStorage = MapStorage(hass, device_id)

        self._poll_count = 0

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

    def set_configured_rooms(self, rooms: dict[int, str]) -> None:
        """Stel de via options-flow geconfigureerde kamers in ({id: naam})."""
        self._configured_rooms = dict(rooms)

    # ── Connectie ──────────────────────────────────────────────────────

    async def async_start_device_manager(self) -> None:
        """Start de persistent connection (1× bij setup)."""
        await self._device_mgr.async_start()

    async def async_stop_device_manager(self) -> None:
        await self._device_mgr.async_stop()

    @property
    def is_device_connected(self) -> bool:
        return self._device_mgr.connected

    def _on_connection_change(self, connected: bool) -> None:
        """Callback van LocalDeviceManager bij connectie status verandering."""
        if self._connection_alive == connected:
            return
        self._connection_alive = connected
        _LOGGER.info("Connection %s", "active" if connected else "lost")

        if connected:
            self._disconnected_since = None
            # Issue oplossen indien aanwezig
            try:
                from .repairs import clear_issue, ISSUE_DEVICE_OFFLINE
                clear_issue(self.hass, self.config_entry_id, ISSUE_DEVICE_OFFLINE)
            except (ImportError, AttributeError):
                # Repairs module mogelijk niet beschikbaar in oudere HA versies
                pass
        else:
            self._disconnected_since = self.hass.loop.time()

        # Trigger HA listener update zodat entities availability tonen.
        # BELANGRIJK: alleen pushen als self.data al gezet is — anders zouden
        # we tijdens eerste connect een leeg dict pushen wat alle entities
        # naar 'unavailable' zou gooien.
        if self.data is not None:
            try:
                self.async_set_updated_data(self.data)
            except RuntimeError:
                # async_set_updated_data raised when called outside event loop
                # (kan voorkomen tijdens shutdown)
                pass

    def _check_long_outage(self) -> None:
        """Maak een Repair issue aan als device langer dan 5 minuten offline is."""
        if self._disconnected_since is None:
            return
        outage = self.hass.loop.time() - self._disconnected_since
        if outage < 300:  # 5 min
            return
        try:
            from .repairs import create_device_offline_issue
            create_device_offline_issue(
                self.hass,
                self.config_entry_id,
                self.config_entry_name,
                self.host,
            )
        except (ImportError, AttributeError):
            pass

    def _on_dps_pushed(self, dps: dict) -> None:
        """Callback bij elke nieuwe DP-update (push of pull).

        Lichte, snelle afhandeling. We ruimen optimistic state op
        en pushen de bijgewerkte data direct naar HA, maar alleen als er
        daadwerkelijk relevante DPs zijn veranderd. De vorige versie
        rebuildde bij ELKE push (ook heartbeats) de volledige data, wat de
        integratie juist trager maakte.
        """
        # Schoon overeenkomstige optimistic state op
        cleared_any = False
        for dp in list(self._optimistic_dps.keys()):
            if dp in dps or str(dp) in dps:
                self._optimistic_dps.pop(dp, None)
                self._optimistic_clear_at.pop(dp, None)
                cleared_any = True

        # Alleen een directe entity-update als deze push status-relevante DPs
        # bevat (status/mode/battery/etc.) of een optimistic override oploste.
        relevant = cleared_any or any(
            str(k) in {"1", "2", "3", "4", "5", "6", "7", "8"} for k in dps
        )
        if not relevant:
            return

        try:
            full_dps = self._device_mgr.last_status
            if not full_dps:
                return
            for dp, val in self._optimistic_dps.items():
                full_dps[dp] = val
            self.async_set_updated_data(self._build_data_from_dps(full_dps))
        except RuntimeError:
            pass
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Push-update kon niet direct worden verwerkt: %s", err)

    async def _async_send_dp(
        self, dp: int, value: Any, *,
        optimistic: bool = False,
        debounce: bool = False,
        timeout: float = 10.0,
    ) -> None:
        """Stuur DP-write via de manager. Bij optimistic=True wordt de waarde
        meteen lokaal als state aangenomen.

        
        - Default timeout 5.0s → 10.0s (Tjilla firmware is structureel traag met
          DP-ACKs over de persistente connectie; fysieke actie gebeurt direct
          maar de bevestiging via TCP komt soms 6-8s later).
        - Bij optimistic writes vangen we ConnectionLostError op en loggen als
          WARNING; we propageren niet als HomeAssistantError. De optimistic
          state staat al in de UI, en de werkelijke waarde komt via push/poll
          binnen enkele seconden. Geen vervelende ERROR popup voor de gebruiker.
        - Niet-optimistic writes (kritisch) blijven wel een
          HomeAssistantError genereren — daar is wachten op confirmation
          functioneel relevant.
        """
        # fail-fast met zichtbare feedback. Is de robot niet
        # verbonden, geef dan direct een nette fout i.p.v. optimistisch
        # "succes" te tonen dat 10s later stil terugvalt. HA toont dit als
        # toast bij de knopdruk — de gebruiker weet meteen waar hij staat.
        if not self._device_mgr.connected:
            raise HomeAssistantError(
                "Robot niet verbonden — controleer of de stofzuiger aan "
                "staat en bereikbaar is op het netwerk"
            )

        if optimistic:
            self._optimistic_dps[dp] = value
            # Auto-clear optimistic na 10s als geen bevestiging volgt
            self._optimistic_clear_at[dp] = self.hass.loop.time() + 10
            # Trigger UI update direct
            if self.data is not None:
                try:
                    cur_dps = dict(self.data.get("dps", {}) or {})
                    cur_dps[dp] = value
                    self.async_set_updated_data(self._build_data_from_dps(cur_dps))
                except RuntimeError:
                    # async_set_updated_data raises if called outside event loop
                    # (zou niet moeten in normale flow)
                    pass

        try:
            await self._device_mgr.async_set_dp(
                dp, value, debounce=debounce, timeout=timeout
            )
        except ConnectionLostError as err:
            # door fail-fast betekent dit een echte disconnect (geen
            # trage ACK meer). Optimistic state terugdraaien zodat de UI niet
            # liegt, en escaleren als nette melding.
            if optimistic:
                self._optimistic_dps.pop(dp, None)
                self._optimistic_clear_at.pop(dp, None)
            raise HomeAssistantError(
                f"Verbinding met de robot verbroken tijdens commando: {err}"
            ) from err


    async def _async_send_command_trans(self, payload: bytes) -> dict:
        """Verstuur DP15 binary payload (fire-and-forget)."""
        return await self._device_mgr.async_send_command_trans(payload)

    # ── Parsers ────────────────────────────────────────────────────────

    def _decode_raw(self, raw_value) -> bytes | None:
        """Decode base64/list/bytes naar bytes.

        Returns None bij decode fouten (gelogd op DEBUG voor diagnostiek).
        """
        if not raw_value:
            return None
        if isinstance(raw_value, str):
            try:
                return base64.b64decode(raw_value)
            except (ValueError, TypeError) as err:
                _LOGGER.debug("Base64 decode failed: %s", err)
                return None
        if isinstance(raw_value, (bytes, bytearray)):
            return bytes(raw_value)
        if isinstance(raw_value, list):
            try:
                return bytes(raw_value)
            except (ValueError, TypeError) as err:
                _LOGGER.debug("List-to-bytes conversion failed: %s", err)
                return None
        return None

    # NOTE: _parse_command_trans is verwijderd — DP15 parsing gebeurt nu
    # via _on_dp15_pushed callback geregistreerd op de LocalDeviceManager.
    # _parse_path_data verwijderd samen met de kaart-functionaliteit.

    def _parse_device_info(self, raw_value) -> None:
        """Parseer DP34 device_info (base64 JSON)."""
        if not raw_value or not isinstance(raw_value, str):
            return
        try:
            decoded = base64.b64decode(raw_value)
            self._device_info = json.loads(decoded)
        except (ValueError, TypeError, json.JSONDecodeError) as err:
            _LOGGER.debug("Device info parse failed: %s", err)

    # ── Main update cycle ──────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        """Wordt periodiek aangeroepen door HA.

        Belangrijk: deze methode mag NIET hangen of crashen tijdens de
        eerste refresh — HA zou dan de hele config entry setup cancelen.
        Bij elke I/O fout valt fallback terug op de gecachte device state.
        """
        try:
            dps_response = await self._device_mgr.async_request_status()
        except (ConnectionLostError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Status fetch failed: %s", err)
            dps_response = {}
        except asyncio.CancelledError:
            # Setup of refresh wordt door HA gecanceld — propageer netjes
            raise
        except Exception as err:  # noqa: BLE001
            # Onverwachte fouten (bv. tinytuya internals) niet laten
            # ontsnappen — dat zou setup blocken
            _LOGGER.debug("Unexpected status fetch error: %s", err)
            dps_response = {}

        # Manager houdt zelf de meest recente DPs bij — gebruik die als bron
        cached = self._device_mgr.last_status
        # Combineer met respons (cached al gepatcht via push callbacks)
        if isinstance(dps_response, dict) and "dps" in dps_response:
            cached.update(dps_response["dps"])

        dps = dict(cached)

        # Long outage detection — leid naar Repair issue indien 5+ min disconnected
        self._check_long_outage()

        # Verwerk device_info (DP34)
        self._parse_device_info(_dp(dps, DP_DEVICE_INFO))

        now = self.hass.loop.time()

        # Cleanup verlopen optimistic DPs
        for dp in list(self._optimistic_clear_at.keys()):
            if now >= self._optimistic_clear_at[dp]:
                self._optimistic_dps.pop(dp, None)
                self._optimistic_clear_at.pop(dp, None)

        # Apply optimistic overrides (UI ziet het sneller)
        for dp, val in self._optimistic_dps.items():
            dps[dp] = val

        # Detecteer mop_state en work_mode wijzigingen voor auto-defaults
        # en last-state restoration. Eerste cycle (None prev) triggert niets.
        data = self._build_data_from_dps(dps)
        self._check_reservoir_and_work_mode_changes(data)

        return data

    def _check_reservoir_and_work_mode_changes(self, data: dict) -> None:
        """Detect mop_state en work_mode changes; trigger handlers.

        Wordt aangeroepen aan het einde van _async_update_data nadat data
        gebouwd is. Handlers draaien als background tasks zodat ze de
        update niet blokkeren.

        Edge cases:
        - Eerste cycle: _prev_* is None → geen handler triggered
        - Geen change: niets gebeurt
        - Tijdens actieve toestanden: handlers checken zelf en skippen waar nodig
        """
        cur_mop_state = data.get("mop_state")
        cur_work_mode = data.get("work_mode")

        if self._prev_mop_state is not None and cur_mop_state != self._prev_mop_state:
            old, new = self._prev_mop_state, cur_mop_state
            _LOGGER.info(
                "Reservoir wissel gedetecteerd: %s → %s", old, new,
            )
            self.hass.async_create_task(
                self._handle_reservoir_change(old, new)
            )

        if self._prev_work_mode is not None and cur_work_mode != self._prev_work_mode:
            old, new = self._prev_work_mode, cur_work_mode
            _LOGGER.debug(
                "Work mode wissel gedetecteerd: %s → %s", old, new,
            )
            self.hass.async_create_task(
                self._handle_work_mode_change(old, new)
            )

        self._prev_mop_state = cur_mop_state
        self._prev_work_mode = cur_work_mode

    async def _handle_reservoir_change(
        self, old_state: str, new_state: str,
    ) -> None:
        """Auto-default work_mode bij fysieke reservoir wissel.

        - none → installed (water geplaatst): set work_mode = both_work
          als huidige modus only_sweep is
        - installed → none (stof geplaatst): set work_mode = only_sweep
          als huidige modus both_work of only_mop is

        Skipt tijdens actieve reiniging of terugrijden — robot moest fysiek
        worden onderbroken om reservoir te wisselen, dus user weet wat hij doet.
        """
        async with self._reservoir_lock:
            status = (self.data or {}).get("status", "")
            if status in ACTIVELY_CLEANING_STATES or status in RETURNING_STATES:
                _LOGGER.info(
                    "Reservoir wissel tijdens %s — geen auto work_mode "
                    "aanpassing (gebruiker rondt huidige reiniging af)",
                    status,
                )
                return

            cur_work_mode = (self.data or {}).get("work_mode")

            if new_state == MOP_STATE_INSTALLED and old_state == MOP_STATE_NONE:
                # Water reservoir geplaatst
                if cur_work_mode == "only_sweep":
                    _LOGGER.info(
                        "Water reservoir geplaatst — auto work_mode → both_work"
                    )
                    await self.async_set_work_mode("both_work")
            elif new_state == MOP_STATE_NONE and old_state == MOP_STATE_INSTALLED:
                # Stof reservoir geplaatst
                if cur_work_mode in ("both_work", "only_mop"):
                    _LOGGER.info(
                        "Stof reservoir geplaatst — auto work_mode → only_sweep"
                    )
                    await self.async_set_work_mode("only_sweep")

    async def _handle_work_mode_change(
        self, old_mode: str, new_mode: str,
    ) -> None:
        """Restore suction/cistern bij relevante work_mode transitie.

        - Bij verlaten van only_sweep (cistern was forced closed): herstel cistern
        - Bij verlaten van only_mop (suction was forced closed): herstel suction

        Stuur restore via _async_send_dp met optimistic=True zodat geen
        ERROR popup bij ACK-delay.
        """
        # Cistern restore: only_sweep → both_work/only_mop
        if old_mode == "only_sweep" and new_mode in ("both_work", "only_mop"):
            _LOGGER.info(
                "Work mode %s → %s: cistern herstellen naar %s",
                old_mode, new_mode, self._last_cistern,
            )
            try:
                await self._async_send_dp(
                    DP_CISTERN, self._last_cistern, optimistic=True,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Cistern restore failed: %s", err)

        # Suction restore: only_mop → both_work/only_sweep
        if old_mode == "only_mop" and new_mode in ("both_work", "only_sweep"):
            _LOGGER.info(
                "Work mode %s → %s: suction herstellen naar %s",
                old_mode, new_mode, self._last_suction,
            )
            try:
                await self._async_send_dp(
                    DP_SUCTION, self._last_suction, optimistic=True,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Suction restore failed: %s", err)

    def _build_data_from_dps(self, dps: dict) -> dict[str, Any]:
        """Bouw de data dict op uit DP state."""
        fault_bitmap = _dp(dps, DP_FAULT, 0) or 0
        faults = parse_fault_bitmap(int(fault_bitmap))

        raw_suction = _dp(dps, DP_SUCTION)
        raw_cistern = _dp(dps, DP_CISTERN)
        raw_work_mode = _dp(dps, DP_WORK_MODE)
        # normaliseer device-aliassen voor work_mode
        # (firmware kan 'sweep_and_mop' rapporteren maar het canonieke is 'both_work')
        work_mode = WORK_MODE_ALIASES.get(raw_work_mode, raw_work_mode)
        raw_mop_state = _dp(dps, DP_MOP_STATE)

        # Track rolling laatst-bekende niet-"closed" waarde.
        # Wordt later gebruikt om suction/cistern te herstellen na work_mode wissel.
        # Optimistische schrijfacties (en user-handmatig "Uit") worden bewust
        # uitgesloten — alleen waardes die het device ACTUEEL terugmeldt en
        # niet-closed zijn.
        if raw_suction and raw_suction != "closed":
            self._last_suction = raw_suction
        if raw_cistern and raw_cistern != "closed":
            self._last_cistern = raw_cistern

        # Display-waardes tonen 'closed' als werkmodus dat impliceert
        suction_display = "closed" if work_mode == "only_mop" else raw_suction
        cistern_display = "closed" if work_mode == "only_sweep" else raw_cistern

        return {
            "status":            _dp(dps, DP_STATUS),
            "battery":           _dp(dps, DP_BATTERY),
            "clean_time":        _dp(dps, DP_CLEAN_TIME),
            "clean_area":        _dp(dps, DP_CLEAN_AREA),
            "mode":              _dp(dps, DP_MODE),
            "suction":           suction_display,
            "suction_raw":       raw_suction,
            "cistern":           cistern_display,
            "cistern_raw":       raw_cistern,
            "work_mode":         work_mode,
            "work_mode_raw":     raw_work_mode,
            "mop_state":         raw_mop_state,
            "total_area":        _dp(dps, DP_TOTAL_AREA),
            "total_count":       _dp(dps, DP_TOTAL_COUNT),
            "total_time":        _dp(dps, DP_TOTAL_TIME),
            "edge_brush":        _dp(dps, DP_EDGE_BRUSH),
            "roll_brush":        _dp(dps, DP_ROLL_BRUSH),
            "filter":            _dp(dps, DP_FILTER),
            "duster":            _dp(dps, DP_DUSTER),
            "do_not_disturb":    _dp(dps, DP_DISTURB, False),
            "break_clean":       _dp(dps, DP_BREAK_CLEAN, False),
            "auto_boost":        _dp(dps, DP_AUTO_BOOST, False),
            "cruise":            _dp(dps, DP_CRUISE, False),
            "y_mop":             _dp(dps, DP_Y_MOP, False),
            "customize_mode":    _dp(dps, DP_CUSTOMIZE, False),
            "volume":            _dp(dps, DP_VOLUME, 50),
            "unseen_messages":   _dp(dps, DP_UNSEEN_MSG, 0),
            "faults":            faults,
            "fault_bitmap":      fault_bitmap,
            # kamerlijst komt uit options-flow config + map_storage.
            "known_rooms":       self.get_known_rooms(),
            "room_selection":    sorted(self._room_selection),
            # Map storage (kamer-namen, no-go zones, virtual walls)
            "custom_rooms":      self.map_storage.get_rooms(),
            # Device info
            "device_info":       self._device_info,
            # Raw
            "dps":               dps,
        }

    # ── Acties ─────────────────────────────────────────────────────────

    def _set_optimistic_status(self, status: str) -> None:
        """Zet DP5 (status) optimistisch, zodat de vacuum-kaart DIRECT de
        verwachte toestand toont in plaats van te wachten op de robotpush.

        dit was het gat in de optimistic updates — commando's zetten
        alleen hun eigen DP (bv. DP1=True), maar de kaart-status komt uit
        DP5, die pas via de push/poll bijtrok. De echte status overschrijft
        deze aanname binnen ~1s (push) of uiterlijk 10s (auto-clear).
        """
        self._optimistic_dps[DP_STATUS] = status
        self._optimistic_clear_at[DP_STATUS] = self.hass.loop.time() + 10
        self._push_data_update()

    async def async_start(self) -> None:
        """Start / hervat — toestand-bewust (geverifieerd op hardware).

        Gedrag per toestand:
          - gedockt/standby/sleep/gestopt → DP1=True start smart clean
          - paused                        → DP1=True hervat huidige taak
          - actief aan het reinigen       → NEGEREN (DP1=True zou pauzeren!)
          - goto_charge (onderweg dock)   → DP1=True start nieuwe reiniging

        Geverifieerd: vanuit paused zet DP1=True de status terug van 'paused'
        naar de actieve modus (hervat). Vanuit een actieve reiniging zou
        dezelfde DP1=True de robot juist pauzeren — daarom negeren we start
        wanneer de robot al reinigt. De UI verbergt de start-knop dan ook
        (zie vacuum.py supported_features).
        """
        status = (self.data or {}).get("status", "")

        if status in ACTIVELY_CLEANING_STATES:
            _LOGGER.debug("Start genegeerd: robot reinigt al (status=%s)", status)
            return

        # Docked, standby, sleep, gestopt, paused of onderweg naar dock:
        # in al deze gevallen start/hervat DP1=True correct. Kaart direct
        # laten meebewegen: hervatten vanuit pauze behoudt de kamer-modus.
        mode = (self.data or {}).get("mode", "")
        expected = (
            STATUS_SELECT_ROOM if mode == "select_room" else STATUS_SMART
        )
        await self._async_send_dp(DP_POWER_GO, True, optimistic=True)
        self._set_optimistic_status(expected)

    async def async_pause(self) -> None:
        """Pauze — zet de robot ter plekke op pauze (DP2=True).

        Werkt vanuit elke actieve toestand (reinigen, select_room, goto_charge).
        Tijdens goto_charge pauzeert dit het terugrijden zonder de modus te
        verlaten (geverifieerd gedrag).
        """
        await self._async_send_dp(DP_PAUSE, True, optimistic=True)
        self._set_optimistic_status(STATUS_PAUSED)

    async def async_stop(self) -> None:
        """Stop — stop ter plekke en val terug naar smart-standby (DP1=False).

        GEVERIFIEERD OP HARDWARE: DP1=False stopt de huidige taak (ook een
        kamerreiniging), zet mode terug naar 'smart' en status naar 'standby',
        en gaat NIET naar de dock. Dit is precies het gewenste stop-gedrag:
        volledig stoppen, terug naar de standaardmodus, blijven staan.

        """
        await self._async_send_dp(DP_POWER_GO, False, optimistic=True)
        self._set_optimistic_status(STATUS_STANDBY)

    async def async_return_to_base(self) -> None:
        # Return-to-base altijd toestaan — moet juist in error werken.
        await self._async_send_dp(DP_SWITCH_CHARGE, True, optimistic=True)
        self._set_optimistic_status(STATUS_GOTO_CHARGE)

    async def async_locate(self) -> None:
        """Piepen — automatisch uitschakelen na 3s.

        DP11 (seek) blijft aan tot we expliciet False sturen, anders blijft
        de stofzuiger doorpiepen.

        gebruik optimistic=False (geen UI state om te tonen) maar
        de generieke timeout-handler in _async_send_dp vangt al de ACK-delay
        op. Robot piept gewoon op fysiek niveau, de TCP ACK is alleen
        vertraagd.

        Eigenlijk willen we voor seek WEL optimistic-style afhandeling
        omdat de actie fire-and-forget is. Truc: gebruik optimistic=True
        (zelfde DP zonder echte state-betekenis).
        """
        await self._async_send_dp(DP_SEEK, True, optimistic=True, timeout=10.0)

        async def _auto_disable() -> None:
            try:
                await asyncio.sleep(LOCATE_AUTO_DISABLE_DELAY)
                await self._async_send_dp(
                    DP_SEEK, False, optimistic=True, timeout=3.0,
                )
            except (ConnectionLostError, asyncio.TimeoutError,
                    asyncio.CancelledError) as err:
                _LOGGER.debug("Auto-disable seek failed: %s", err)

        self.hass.async_create_task(_auto_disable())

    async def async_set_suction(self, suction: str) -> None:
        """Stel zuigkracht in.

        'closed' is nu direct toegestaan — device accepteert DP9='closed'.
        Bij work_mode=only_mop wordt suction door coordinator automatisch als
        'closed' getoond, ongeacht raw DP9. Bij sweep_and_mop kan gebruiker
        rechtstreeks 'Uit' kiezen.
        """
        await self._async_send_dp(DP_SUCTION, suction, optimistic=True)
        await self.async_request_refresh()

    async def async_set_cistern(self, cistern: str) -> None:
        """Stel dweilwater in.

        'closed' is direct toegestaan — device accepteert DP10='closed'.
        Bij work_mode=only_sweep wordt cistern automatisch als 'closed' getoond.
        """
        await self._async_send_dp(DP_CISTERN, cistern, optimistic=True)
        await self.async_request_refresh()

    async def async_set_work_mode(self, work_mode: str) -> None:
        await self._async_send_dp(DP_WORK_MODE, work_mode, optimistic=True)
        await self.async_request_refresh()

    async def async_set_volume(self, volume: int) -> None:
        """Volume met debouncing — gebruiker kan slider bewegen zonder
        elke tussenwaarde naar device te sturen.
        """
        await self._async_send_dp(
            DP_VOLUME, int(volume), optimistic=True, debounce=True,
        )
        await self.async_request_refresh()

    async def async_set_switch(self, dp: int, value: bool) -> None:
        await self._async_send_dp(dp, bool(value), optimistic=True)
        await self.async_request_refresh()

    async def async_reset_consumable(self, dp: int) -> None:
        await self._async_send_dp(dp, True)
        await self.async_request_refresh()

        # Lokale state ook wissen
        await self.async_request_refresh()

    # async_break_clean method verwijderd. Zette DP27=True wat een
    # persistente setting is, niet een momentary onderbreek-actie. Voor pause
    # gebruik async_pause (DP2=True) via de standaard vacuum.pause service.

    async def async_set_direction(self, direction: str) -> None:
        """Handmatig sturen via DP12.

        direction: 'forward', 'backward', 'turn_left', 'turn_right', 'stop'
        """
        valid = {"forward", "backward", "turn_left", "turn_right", "stop"}
        if direction not in valid:
            raise ValueError(f"Ongeldige richting: {direction}")
        await self._async_send_dp(12, direction)

    # ── Geavanceerde commando's ─────────────────────────────────────────

    async def async_clean_rooms(self, room_ids: Iterable[int]) -> None:
        """Start reinigen van geselecteerde kamers.

        Geverifieerd op hardware: het volstaat om ALLEEN het
        0x14-frame (SetRoomClean) op DP15 te sturen. De robot start zelf en
        zet DP4/DP5 zelf naar select_room. Geen DP1, geen DP4-mode-switch,
        geen gecombineerde write.

        Alleen dit ene DP15-frame met cmd 0x14 wordt verstuurd; cmd 0x15
        is de status-reflectie die de robot terugkaatst, niet het commando.
        Na het frame toont de robot mode=select_room, status=select_room en
        rijdt hij naar de juiste kamer.

        Vanuit pauze werkt hetzelfde frame: het herstart de kamerreiniging
        met de nieuwe selectie.
        """
        ids = list(room_ids)
        if not ids:
            _LOGGER.warning("async_clean_rooms: lege kamerlijst genegeerd")
            return

        # Eén DP15-write met het 0x14-startframe. Meer is niet nodig.
        payload = build_select_rooms(ids)
        _LOGGER.debug("Kamerreiniging start: 0x14-frame voor kamers %s", ids)
        try:
            await self._async_send_command_trans(payload)
        except (ConnectionLostError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Kamer-frame (DP15) versturen mislukt: %s", err)
        else:
            self._set_optimistic_status(STATUS_SELECT_ROOM)

    async def async_cancel_room_selection(self) -> None:
        """Annuleer de kamerselectie — puur lokaal.

        Er gaat bewust GEEN frame naar de robot: het eerder gebruikte
        "cancel-frame" was een ongeverifieerde constructie (leeg 0x14) die
        nergens is waargenomen. Een lopende reiniging stop je met de
        stop-knop (DP1=False, geverifieerd); selectie annuleren is alleen
        het wissen van de nog-niet-verstuurde keuze.
        """
        self.clear_room_selection()

    # ── Kamer-detectie en multi-room selectie ───────────────────

    def get_known_rooms(self) -> dict[int, str]:
        """Alle bekende kamers als {room_id: naam}, gesorteerd op ID.

        Bronnen (oplopende prioriteit):
          1. Options-flow config  — de kamers die de gebruiker heeft ingevoerd
             (ID + naam). Dit is de primaire bron.
          2. Map storage          — opgeslagen kamernamen (read-only
             fallback wanneer de options-config een kamer niet benoemt).

        Onbenoemde kamers krijgen "Ruimte <id>". Leeg dict als er niets
        geconfigureerd is.
        """
        rooms: dict[int, str] = {}

        # Bron 1: options-flow configuratie
        for rid, name in self._configured_rooms.items():
            rooms[int(rid)] = name or f"Ruimte {rid}"

        # Bron 2: map storage (historisch opgeslagen namen)
        for rid_key, info in (self.map_storage.get_rooms() or {}).items():
            try:
                rid = int(rid_key)
            except (TypeError, ValueError):
                continue
            name = (info or {}).get("name")
            if name:
                rooms[rid] = name
            elif rid not in rooms:
                rooms[rid] = f"Ruimte {rid}"

        return dict(sorted(rooms.items()))

    @property
    def room_selection(self) -> set[int]:
        """Kopie van de huidige kamer-selectie (IDs)."""
        return set(self._room_selection)

    def toggle_room_selection(self, room_id: int) -> None:
        """Voeg kamer toe aan of verwijder uit de selectie."""
        room_id = int(room_id)
        if room_id in self._room_selection:
            self._room_selection.discard(room_id)
        else:
            self._room_selection.add(room_id)
        self._push_data_update()

    def clear_room_selection(self) -> None:
        """Wis de volledige kamer-selectie (alleen lokaal)."""
        if self._room_selection:
            self._room_selection.clear()
            self._push_data_update()

    def _push_data_update(self) -> None:
        """Re-emit de huidige data zodat entities hun state herberekenen.

        Past de optimistische DP-overlay toe op de rauwe DPs, zodat een
        zojuist optimistisch gezette waarde (zoals status=select_room bij
        kamerreiniging) direct in de UI verschijnt in plaats van pas bij de
        volgende push/poll. Selectie-wijzigingen zitten niet in DPs, dus ook
        die worden via deze re-emit direct zichtbaar.
        """
        if self.data is None:
            return
        try:
            merged = dict(self.data.get("dps", {}) or {})
            for dp, val in self._optimistic_dps.items():
                merged[dp] = val
            self.async_set_updated_data(self._build_data_from_dps(merged))
        except RuntimeError:
            pass

    async def async_start_selected_rooms(self) -> None:
        """Start reiniging van alle geselecteerde kamers, wis daarna de selectie."""
        if not self._room_selection:
            raise HomeAssistantError(
                "Geen kamers geselecteerd — kies eerst één of meer kamers "
                "in 'Kamerselectie'"
            )
        rooms = sorted(self._room_selection)
        _LOGGER.info("Start reiniging van geselecteerde kamers: %s", rooms)
        # Selectie pas wissen ná succesvolle verzending, zodat een fout
        # (bv. robot locked) de keuze van de gebruiker niet weggooit.
        await self.async_clean_rooms(rooms)
        self.clear_room_selection()

