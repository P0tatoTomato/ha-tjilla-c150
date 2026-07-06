"""LocalDeviceManager — persistent connection naar Tjilla C150.

Vervangt directe tinytuya.Device aanroepen met:
- Persistent TCP connection (proto 3.3) met heartbeat
- Async write-lock (geen interleaved frames)
- Auto-reconnect met exponential backoff
- Command queue met optionele debouncing
- ACK-correlation voor DP15 commando's
- Status streaming via push notifications

De manager draait in een eigen background task en overleeft connection drops
zonder dat de HA entity unavailable wordt.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import tinytuya

_LOGGER = logging.getLogger(__name__)

# Heartbeat interval — Tuya 3.3 devices droppen idle TCP na ~30s
HEARTBEAT_INTERVAL = 20.0
# Normale socket-timeout voor reguliere commando's en status-requests
SOCKET_TIMEOUT = 8.0
# Korte timeout voor de idle push-listener (non-blocking oppikken van pushes).
# Kort genoeg om de executor-thread niet vast te houden, lang genoeg om een
# spontane push die net binnenkomt te vangen.
PUSH_LISTEN_TIMEOUT = 0.4
# Max tijd zonder enige response voordat we de connection als dood beschouwen
DEAD_CONNECTION_TIMEOUT = 45.0
# Reconnect backoff
RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0
RECONNECT_BACKOFF = 2.0
# Per-DP debounce (slider-achtige DPs)
DEBOUNCE_DPS = {26}  # DP26 = volume
DEBOUNCE_DELAY = 0.25
# Default ACK wait voor DP15 commando's
# Command transport DP
DP_COMMAND_TRANS = 15
# DP-cmd byte voor "path ACK"


@dataclass
class _PendingCommand:
    """Een commando in de queue."""
    dp: int
    value: Any
    future: asyncio.Future
    debounce_until: float = 0.0
    expects_ack: bool = False
    ack_cmd_byte: Optional[int] = None
    sent_at: float = 0.0


class ConnectionLostError(Exception):
    """Connection naar device is verloren en kon niet hersteld worden."""


class LocalDeviceManager:
    """Beheert de TCP connection en commando-flow naar de stofzuiger.

    Threading model:
        - Eén background task (`_run_loop`) doet ALLE I/O via tinytuya
        - HA event-loop tasks plaatsen commands op de queue en wachten op future
        - Status updates worden via callback gepusht naar de coordinator
    """

    def __init__(
        self,
        host: str,
        device_id: str,
        local_key: str,
        protocol_version: str = "3.3",
        on_status_update: Optional[Callable[[dict], None]] = None,
        on_dp15_packet: Optional[Callable[[bytes], None]] = None,
        on_connection_change: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.host = host
        self.device_id = device_id
        self.local_key = local_key
        self.protocol_version = protocol_version

        self._on_status_update = on_status_update
        self._on_dp15_packet = on_dp15_packet
        self._on_connection_change = on_connection_change

        self._device: Optional[tinytuya.Device] = None
        self._connected = False
        # eigen éénpersoons-executor voor ALLE device-I/O.
        # De push-listener leest semi-continu de socket (~60% duty); dat
        # hoort niet in HA's gedeelde default-pool waar het andere
        # integraties kan vertragen. Eén dedicated thread volstaat omdat
        # alle I/O toch sequentieel door de verbindingslus loopt.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"tjilla-{device_id[-6:]}"
        )
        self._reconnect_delay = RECONNECT_INITIAL_DELAY
        self._last_rx_time = 0.0
        # _last_status_dps: alleen muteren vanuit event loop (in
        # _handle_status_response). Lezen via last_status property maakt copy
        # zodat externe code geen race ziet. CPython GIL beschermt dict.update
        # tegen corruptie, maar in no-GIL builds (3.13+) zou een lock nodig zijn.
        self._last_status_dps: dict = {}

        # Command queue: list van pending commands
        self._cmd_queue: asyncio.Queue[_PendingCommand] = asyncio.Queue()
        # Per-DP latest pending command (voor debouncing)
        self._pending_by_dp: dict[int, _PendingCommand] = {}
        # ACK waiters: cmd_byte → list van futures

        self._loop_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._executor_lock = asyncio.Lock()  # voor non-queued reads

    # ─────────────────────── Lifecycle ─────────────────────────

    async def async_start(self) -> None:
        """Start de background loop. Idempotent."""
        if self._loop_task and not self._loop_task.done():
            return
        self._stop_event.clear()
        loop = asyncio.get_running_loop()
        self._loop_task = loop.create_task(
            self._run_loop(), name=f"tjilla_device_{self.device_id[:8]}"
        )
        _LOGGER.debug("LocalDeviceManager started for %s", self.device_id[:8])

    async def async_stop(self) -> None:
        """Stop de background loop, sluit verbinding, cancel pending work."""
        self._stop_event.set()

        # Cancel alle pending command futures
        cancelled = 0
        while not self._cmd_queue.empty():
            try:
                cmd = self._cmd_queue.get_nowait()
                if not cmd.future.done():
                    cmd.future.cancel()
                    cancelled += 1
            except asyncio.QueueEmpty:
                break

        # Cancel pending debounce slots
        for cmd in self._pending_by_dp.values():
            if not cmd.future.done():
                cmd.future.cancel()
                cancelled += 1
        self._pending_by_dp.clear()

        if cancelled:
            _LOGGER.debug("Cancelled %d pending operations on stop", cancelled)

        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._async_close_device()
        # Eigen executor netjes afsluiten (wait=False: een eventueel nog
        # lopende socket-read van ≤0,4s mag op de achtergrond aflopen).
        self._executor.shutdown(wait=False)
        _LOGGER.debug("LocalDeviceManager stopped for %s", self.device_id[:8])

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_status(self) -> dict:
        return dict(self._last_status_dps)

    # ─────────────────────── Public API ────────────────────────

    async def async_set_dp(
        self,
        dp: int,
        value: Any,
        *,
        timeout: float = 10.0,
        debounce: bool = False,
    ) -> Any:
        """Stuur een DP-write. Wacht op completion of raise op timeout.

        Args:
            dp: DataPoint nummer
            value: nieuwe waarde
            timeout: max wachttijd voor verzenden+confirmation
            debounce: bij True (auto voor DEBOUNCE_DPS) wordt een vorige
                pending write op dezelfde DP geannuleerd

        Returns:
            De device response dict (of empty dict bij debounce-replacement)
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        cmd = _PendingCommand(dp=dp, value=value, future=fut)

        if debounce or dp in DEBOUNCE_DPS:
            # Annuleer eerdere pending write voor deze DP
            old = self._pending_by_dp.get(dp)
            if old and not old.future.done():
                old.future.set_result({"debounced": True})
                _LOGGER.debug("DP%d debounced (replaced)", dp)
            cmd.debounce_until = time.monotonic() + DEBOUNCE_DELAY
            self._pending_by_dp[dp] = cmd

        await self._cmd_queue.put(cmd)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            # cancel the future so _run_loop skips this command when
            # it eventually picks it up from the queue. Without this, a
            # reconnect-after-timeout would still execute the stale write,
            # causing late side-effects (e.g. resuming a cleaning the user
            # already stopped).
            if not fut.done():
                fut.cancel()
            raise ConnectionLostError(f"DP{dp} write timed out after {timeout}s")


    async def async_send_command_trans(self, payload: bytes) -> Any:
        """Verstuur binary payload via DP15 (fire-and-forget).

        Geen ACK-wachttijd: het geverifieerde 0x14-commando werkt zonder, en
        de robot bevestigt via zijn eigen statuspush (DP4/DP5 → select_room)
        die de push-listener vrijwel direct oppikt.
        """
        b64 = base64.b64encode(payload).decode("ascii")
        return await self.async_set_dp(DP_COMMAND_TRANS, b64)

    async def async_request_status(self) -> dict:
        """Forceer een status-fetch (gebruikt voor periodic poll).

        Returns empty dict bij timeout of disconnected state, zodat
        de caller geen exception hoeft te handelen tijdens normale
        startup-race (HA cancelt setup als deze blijft hangen).
        """
        # Korte fast-path: als manager nog niet is verbonden, niet wachten
        # tot timeout — gewoon het cached snapshot teruggeven (push-data
        # van eerdere sessie of leeg).
        if not self._connected:
            return {}

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        cmd = _PendingCommand(dp=-1, value=None, future=fut)  # dp=-1 = status
        await self._cmd_queue.put(cmd)
        try:
            return await asyncio.wait_for(fut, timeout=8.0)
        except asyncio.TimeoutError:
            return {}
        except asyncio.CancelledError:
            # HA cancelt onze setup of refresh — propageer niet door,
            # maar zorg wel dat de pending future opgeruimd wordt
            if not fut.done():
                fut.cancel()
            raise

    # ─────────────────────── Background loop ──────────────────────

    async def _run_loop(self) -> None:
        """Hoofdloop: connect, process commands, heartbeat, reconnect."""
        loop = asyncio.get_running_loop()

        while not self._stop_event.is_set():
            # 1. Ensure connection
            if not self._connected:
                try:
                    await self._async_connect()
                except Exception as err:  # noqa: BLE001
                    # tinytuya raises a variety of exceptions from its
                    # C-style protocol code; we treat all as transient.
                    _LOGGER.warning(
                        "Connection to %s failed: %s. Retry in %.1fs",
                        self.host, err, self._reconnect_delay,
                    )
                    await self._mark_disconnected()
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=self._reconnect_delay,
                        )
                        return  # stop_event was set
                    except asyncio.TimeoutError:
                        pass
                    self._reconnect_delay = min(
                        self._reconnect_delay * RECONNECT_BACKOFF,
                        RECONNECT_MAX_DELAY,
                    )
                    continue

            # 2. Heartbeat — alleen bij RX-stilte.
            # Elke geslaagde poll/push werkt _last_rx_time bij; een aparte
            # heartbeat is dan overbodig. Pas als er HEARTBEAT_INTERVAL lang
            # níets is ontvangen, sturen we er een om de Tuya-TCP-verbinding
            # levend te houden. Scheelt structureel verkeer richting de
            # zwakke wifi-SoC van de robot.
            now = loop.time()
            if now - self._last_rx_time >= HEARTBEAT_INTERVAL:
                try:
                    await self._async_heartbeat()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Heartbeat failed: %s", err)
                    await self._mark_disconnected()
                    continue

            # 3. Dead-connection detect
            if (now - self._last_rx_time) > DEAD_CONNECTION_TIMEOUT:
                _LOGGER.info("Connection timeout — no response in %.0fs",
                             DEAD_CONNECTION_TIMEOUT)
                await self._mark_disconnected()
                continue

            # 4. Process queued commands (with debounce delay)
            # tijdens idle-tijd (geen commando in de wachtrij) lezen we
            # ACTIEF de socket uit voor spontane pushes van de robot. De Tjilla
            # pusht statuswijzigingen (start, pauze, select_room, dock, enz.)
            # uit zichzelf op de persistente verbinding. Door tijdens
            # idle-tijd actief de socket te lezen pikken we die statuspushes
            # vrijwel direct op, zodat de UI snel meebeweegt.
            try:
                cmd = await asyncio.wait_for(self._cmd_queue.get(), timeout=0.3)
            except asyncio.TimeoutError:
                # Geen commando — luister kort naar spontane pushes.
                # Trade-off (bewust): een knopdruk die binnenkomt terwijl
                # deze read loopt wacht max PUSH_LISTEN_TIMEOUT (~0,2s
                # mediaan) extra. Emergent maar correct: pusht de robot
                # nét vóór een commando, dan kan tinytuya die push als
                # "respons" teruggeven — elke respons met dps wordt hier
                # generiek verwerkt, dus dat is onschadelijk; de echte
                # respons wordt door de volgende poll gelezen.
                await self._async_poll_pushes()
                continue

            # Debounce: als deze command een replacement heeft gehad, skip
            if cmd.future.done():
                continue

            # Wait remaining debounce time
            if cmd.debounce_until > 0:
                wait = cmd.debounce_until - time.monotonic()
                if wait > 0:
                    try:
                        await asyncio.sleep(wait)
                    except asyncio.CancelledError:
                        return
                # Check nog een keer of een nieuwere command is gekomen
                if cmd.future.done():
                    continue
                # Pop from pending
                if self._pending_by_dp.get(cmd.dp) is cmd:
                    self._pending_by_dp.pop(cmd.dp, None)

            # 5. Execute
            try:
                result = await self._async_execute_cmd(cmd)
                if not cmd.future.done():
                    cmd.future.set_result(result)
            except (ConnectionLostError, OSError, asyncio.TimeoutError) as e:
                # Netwerkfouten zijn normaal bij intermittent verbindingen.
                # Reconnect-loop pakt dit op, geen actie van gebruiker nodig.
                _LOGGER.debug(
                    "Command %d=%s failed (network): %s — will reconnect",
                    cmd.dp, cmd.value, e,
                )
                if not cmd.future.done():
                    cmd.future.set_exception(e)
                await self._mark_disconnected()
            except Exception as err:  # noqa: BLE001
                # Unexpected exception — log as warning so users notice.
                # tinytuya can raise diverse exceptions from binary protocol.
                _LOGGER.warning(
                    "Command %d=%s failed (unexpected): %s",
                    cmd.dp, cmd.value, err,
                )
                if not cmd.future.done():
                    cmd.future.set_exception(err)
                await self._mark_disconnected()

    async def _async_execute_cmd(self, cmd: _PendingCommand) -> dict:
        """Voer een commando uit via executor (tinytuya is sync)."""
        loop = asyncio.get_running_loop()

        if cmd.dp == -1:
            # Status fetch
            result = await loop.run_in_executor(self._executor, self._sync_status)
        else:
            result = await loop.run_in_executor(
                self._executor, self._sync_set_value, cmd.dp, cmd.value
            )

        # tinytuya geeft soms een payload mee — process voor DP-updates
        if isinstance(result, dict) and "dps" in result:
            self._handle_status_response(result["dps"])

        self._last_rx_time = loop.time()
        return result

    async def _async_poll_pushes(self) -> None:
        """Lees spontane DP-pushes van de robot (non-blocking).

        de Tjilla stuurt op de persistente verbinding uit zichzelf
        statusupdates zodra er iets verandert (start/pauze/select_room/dock/
        voortgang). Deze methode leest die met een korte socket-timeout uit,
        zodat de HA-UI vrijwel direct meebeweegt i.p.v. te wachten op de
        volgende poll (scan_interval) of heartbeat.

        Robuust: netwerk-/timeout-fouten zijn hier normaal (er is vaak niets
        te lezen) en worden stil genegeerd. Alleen een verbroken verbinding
        markeren we, zodat de reconnect-logica het overneemt.
        """
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(self._executor, self._sync_receive)
        except (ConnectionLostError, OSError) as err:
            _LOGGER.debug("Push-listener: verbinding verbroken: %s", err)
            await self._mark_disconnected()
            return
        except Exception:  # noqa: BLE001
            # tinytuya kan diverse excepties gooien als er niets te lezen is.
            return
        if isinstance(result, dict) and "dps" in result:
            self._handle_status_response(result["dps"])

    def _sync_receive(self) -> dict:
        """Sync non-blocking receive van een spontane push.

        Gebruikt tinytuya's receive() met een KORTE socket-timeout, zodat we
        niet tot de volle socket-timeout (8s) in de executor-thread blijven
        hangen als er niets te lezen is. receive() geeft None bij timeout.
        De normale timeout wordt daarna hersteld voor gewone commando's.
        """
        if not self._device:
            return {}
        try:
            # Korte timeout puur voor deze non-blocking poll.
            try:
                self._device.set_socketTimeout(PUSH_LISTEN_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass
            data = self._device.receive()
            if isinstance(data, dict):
                return data
            return {}
        except Exception:  # noqa: BLE001
            # Timeout of geen data — volkomen normaal tijdens idle.
            return {}
        finally:
            # Herstel de normale socket-timeout voor reguliere commando's.
            try:
                self._device.set_socketTimeout(SOCKET_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass

    async def _async_heartbeat(self) -> None:
        """Stuur heartbeat — tinytuya heeft geen expliciete heartbeat methode,
        we gebruiken een lichtgewicht status() request.
        """
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self._executor, self._sync_heartbeat)
        if result and "dps" in result:
            self._handle_status_response(result["dps"])
        self._last_rx_time = loop.time()

    def _sync_heartbeat(self) -> dict:
        """Sync heartbeat — Tuya 3.3 heeft een NULL command type 9.

        tinytuya exposes deze als heartbeat() vanaf 1.13.x.
        """
        if not self._device:
            return {}
        try:
            # tinytuya heartbeat (Tuya CONTROL_NEW = command 9)
            payload = self._device.heartbeat(nowait=False)
            if isinstance(payload, dict):
                return payload
            return {}
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Heartbeat raw error: %s", err)
            # Fallback: status() also does keepalive work
            try:
                return self._device.status() or {}
            except Exception:  # noqa: BLE001
                raise

    async def _async_connect(self) -> None:
        """Maak een nieuwe verbinding."""
        loop = asyncio.get_running_loop()
        was_first_connect = self._last_rx_time == 0.0
        await loop.run_in_executor(self._executor, self._sync_connect)
        self._connected = True
        self._reconnect_delay = RECONNECT_INITIAL_DELAY
        self._last_rx_time = loop.time()
        if self._on_connection_change:
            try:
                self._on_connection_change(True)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Connection-up callback failed")
        # Initial status fetch — failures are non-fatal, will retry next cycle
        try:
            await self._async_heartbeat()
        except Exception:  # noqa: BLE001
            pass
        # Eerste connect = INFO, reconnects = DEBUG (om log spam te voorkomen)
        if was_first_connect:
            _LOGGER.info("Connected to %s", self.host)
        else:
            _LOGGER.debug("Reconnected to %s", self.host)

    def _sync_connect(self) -> None:
        """Maak tinytuya Device en zet sockopts.

        Returns geen status data — die wordt door _async_heartbeat na connect
        opgepakt vanuit de event loop. Belangrijk: deze functie draait in een
        executor thread, dus mag GEEN callbacks naar de coordinator triggeren
        (die updates state die in event loop wordt gelezen).
        """
        if self._device is not None:
            try:
                self._device.close()
            except Exception:  # noqa: BLE001
                pass
        d = tinytuya.Device(
            dev_id=self.device_id,
            address=self.host,
            local_key=self.local_key,
            version=self.protocol_version,
        )
        d.set_socketTimeout(SOCKET_TIMEOUT)
        try:
            d.set_socketRetryLimit(3)
        except AttributeError:
            pass
        # Force een initial status request om te verifiëren dat de socket werkt.
        # We gooien de DPs WEG — de async_heartbeat na _sync_connect haalt ze
        # opnieuw op vanuit event-loop context.
        result = d.status()
        if isinstance(result, dict) and ("Error" in result or "error" in result):
            raise ConnectionLostError(
                f"{result.get('Error') or result.get('error')}"
            )
        self._device = d

    def _sync_status(self) -> dict:
        if not self._device:
            return {}
        result = self._device.status()
        if isinstance(result, dict) and ("Error" in result or "error" in result):
            raise ConnectionLostError(
                f"{result.get('Error') or result.get('error')}"
            )
        return result or {}

    def _sync_set_value(self, dp: int, value: Any) -> dict:
        if not self._device:
            raise ConnectionLostError("Device not connected")
        result = self._device.set_value(dp, value)
        if isinstance(result, dict) and ("Error" in result or "error" in result):
            raise ConnectionLostError(
                f"{result.get('Error') or result.get('error')}"
            )
        return result or {}


    async def _async_close_device(self) -> None:
        if self._device is not None:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._executor, self._device.close)
            except Exception:  # noqa: BLE001
                # Close errors are non-actionable; tinytuya may raise
                # OSError, AttributeError, RuntimeError, etc. on a stale socket
                pass
            self._device = None

    async def _mark_disconnected(self) -> None:
        if self._connected:
            self._connected = False
            if self._on_connection_change:
                try:
                    self._on_connection_change(False)
                except Exception:  # noqa: BLE001
                    # Callback errors should never propagate up the IO loop
                    _LOGGER.exception("Connection change callback failed")
        # fail-fast — laat wachtend werk direct falen i.p.v.
        # callers hun volle timeout te laten uitzitten. De coordinator
        # vertaalt ConnectionLostError naar een nette HA-foutmelding.
        drained = 0
        while True:
            try:
                cmd = self._cmd_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if cmd.future and not cmd.future.done():
                cmd.future.set_exception(
                    ConnectionLostError("Robot niet verbonden")
                )
                drained += 1
        self._pending_by_dp.clear()
        if drained:
            _LOGGER.debug("Disconnect: %d wachtende commando's gefaald", drained)
        await self._async_close_device()

    # ─────────────────── Status processing ──────────────────────

    def _handle_status_response(self, dps: dict) -> None:
        """Verwerkt een binnenkomende DP update."""
        if not dps:
            return
        # Update last RX timestamp — voorkomt dat dead-connection-detect
        # triggert bij actief verkeer (bv. continue DP15 path pushes tijdens
        # een cleaning sessie).
        try:
            loop = asyncio.get_running_loop()
            self._last_rx_time = loop.time()
        except RuntimeError:
            # Geen running loop (zou niet moeten voorkomen in normale flow)
            pass

        # Update cached state. DP14/15 (path-frames, base64-blobs die de
        # robot tijdens reinigen continu pusht) bewust NIET cachen: groot,
        # vluchtig en door niets gebruikt sinds de kaartfunctie weg is.
        self._last_status_dps.update(
            {k: v for k, v in dps.items() if str(k) not in ("14", "15")}
        )
        # Notify coordinator
        if self._on_status_update:
            try:
                self._on_status_update(dict(dps))
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Status update callback failed")


