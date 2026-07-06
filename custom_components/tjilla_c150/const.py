"""Constanten en protocol helpers voor de Tjilla C150."""
from __future__ import annotations

import struct
from typing import Iterable

DOMAIN = "tjilla_c150"
DEFAULT_NAME = "Tjilla C150"
DEFAULT_SCAN_INTERVAL = 15

# Hoe lang een optimistisch gezette status (DP5) blijft staan voordat hij
# automatisch wijkt voor de werkelijke robotwaarde. Kort gehouden zodat de
# kaart zich snel corrigeert wanneer de robot een commando niet uitvoert
# (bv. starten terwijl hij is opgetild): de UI reageert direct, maar toont
# een onjuiste aanname hooguit enkele seconden. Andere optimistische
# DP-writes gebruiken een langer venster omdat die hun eigen bevestigende
# push krijgen; een geweigerde start levert juist géén nieuwe status-push.
OPTIMISTIC_STATUS_CLEAR = 3.0

# options-flow config key voor het aantal geconfigureerde kamers.
# Per kamer worden room_id_<i> en room_name_<i> keys opgeslagen.
CONF_ROOM_COUNT = "room_count"

# DP15 ACK timing
LOCATE_AUTO_DISABLE_DELAY = 3.0   # auto-stop seek na N seconden

# ════════════════════════════════════════════════════════════════════════
# DP codes
# ════════════════════════════════════════════════════════════════════════

# Control DPs (write)
DP_POWER_GO          = 1    # bool: start reinigen vanuit dock
DP_PAUSE             = 2    # bool: pauze aan/uit
DP_SWITCH_CHARGE     = 3    # bool: terug naar dock
DP_MODE              = 4    # enum: smart/zone/pose/part/select_room
DP_STATUS            = 5    # enum: huidige status
DP_CLEAN_TIME        = 6    # int: minuten
DP_CLEAN_AREA        = 7    # int: m²
DP_BATTERY           = 8    # int: %
DP_SUCTION           = 9    # enum: gentle/normal/strong (closed via work_mode)
DP_CISTERN           = 10   # enum: low/middle/high (closed via work_mode)
DP_SEEK              = 11   # bool: piepen
DP_DIRECTION         = 12   # enum: richting
DP_RESET_MAP         = 13   # bool: kaart resetten
DP_PATH_DATA         = 14   # raw: rijroute
DP_COMMAND_TRANS     = 15   # raw: binary protocol kanaal
DP_REQUEST           = 16   # enum: get_map/get_path/get_both
DP_EDGE_BRUSH        = 17   # int: minuten gebruikt
DP_RESET_EDGE        = 18   # bool: reset
DP_ROLL_BRUSH        = 19   # int: minuten gebruikt
DP_RESET_ROLL        = 20   # bool: reset
DP_FILTER            = 21   # int: minuten gebruikt
DP_RESET_FILTER      = 22   # bool: reset
DP_DUSTER            = 23   # int: minuten gebruikt
DP_RESET_DUSTER      = 24   # bool: reset
DP_DISTURB           = 25   # bool: niet storen
DP_VOLUME            = 26   # int: 0-100
DP_BREAK_CLEAN       = 27   # bool: doorgaan na laden
DP_FAULT             = 28   # bitmap: 30 foutcodes
DP_TOTAL_AREA        = 29   # int: totaal m²
DP_TOTAL_COUNT       = 30   # int: aantal beurten
DP_TOTAL_TIME        = 31   # int: totaal minuten
DP_TIMER             = 32   # raw
DP_DISTURB_TIME      = 33   # raw
DP_DEVICE_INFO       = 34   # raw (base64 JSON)
DP_VOICE_DATA        = 35   # raw
DP_LANGUAGE          = 36   # enum
DP_CUSTOMIZE         = 39   # bool
DP_MOP_STATE         = 40   # enum: mop status
DP_WORK_MODE         = 41   # enum: only_sweep/sweep_and_mop/only_mop
DP_AUTO_BOOST        = 45   # bool: auto zuigkracht
DP_CRUISE            = 46   # bool: cruise modus
DP_Y_MOP             = 48   # bool: Y-patroon
DP_UNSEEN_MSG        = 128  # enum: ongelezen meldingen

# ════════════════════════════════════════════════════════════════════════
# Status waarden (DP5) — exact de officiële spec-enum van model exnzts.
# Bron: Tuya IoT-console, Function Definition. Geen verzonnen waarden:
# alles hieronder kan de robot daadwerkelijk rapporteren.
# ════════════════════════════════════════════════════════════════════════

STATUS_STANDBY       = "standby"
STATUS_SMART         = "smart"            # hoofd-reinigingsstatus
STATUS_ZONE_CLEAN    = "zone_clean"
STATUS_PART_CLEAN    = "part_clean"
STATUS_CLEANING      = "cleaning"
STATUS_PAUSED        = "paused"
STATUS_GOTO_POS      = "goto_pos"
STATUS_POS_ARRIVED   = "pos_arrived"
STATUS_POS_UNARRIVE  = "pos_unarrive"
STATUS_GOTO_CHARGE   = "goto_charge"
STATUS_CHARGING      = "charging"
STATUS_CHARGE_DONE   = "charge_done"
STATUS_SLEEP         = "sleep"
STATUS_SELECT_ROOM   = "select_room"
STATUS_SEEK_DUST     = "seek_dust_bucket" # spec-waarde; C150 heeft geen leegstation
STATUS_COLLECT_DUST  = "collecting_dust"  # idem — gemapt maar verder ongebruikt
STATUS_IN_TROUBLE    = "in_trouble"       # DE foutstatus volgens de spec
STATUS_REMOTE_CTRL   = "remote_ctrl"      # handmatige afstandsbediening actief

# ── Toestandsgroepen voor knoplogica en state-mapping ────────────────────
# Gebaseerd op geverifieerd hardwaregedrag (knoppen-toestandstest) en de
# spec-enum. Eén samenhangende familie zonder overlap.

# Robot is ACTIEF bezig (reinigen of rijden). Start-knop wordt hier
# genegeerd én verborgen: DP1=True zou pauzeren (geverifieerd).
ACTIVELY_CLEANING_STATES = {
    STATUS_SMART,
    STATUS_SELECT_ROOM,
    STATUS_CLEANING,
    STATUS_ZONE_CLEAN,
    STATUS_PART_CLEAN,
    STATUS_GOTO_POS,      # onderweg naar punt: actief, conservatief behandelen
    STATUS_REMOTE_CTRL,   # wordt handmatig bestuurd: actief
}
# Onderweg terug (naar dock of leegstation).
RETURNING_STATES = {
    STATUS_GOTO_CHARGE,
    STATUS_SEEK_DUST,
}
# Fysiek op de dock.
ON_DOCK_STATES = {
    STATUS_CHARGING,
    STATUS_CHARGE_DONE,
    STATUS_COLLECT_DUST,
}
# Aan, stilstaand, NIET op de dock (bv. net gestopt of doel bereikt).
STOPPED_STATES = {
    STATUS_STANDBY,
    STATUS_SLEEP,
    STATUS_POS_ARRIVED,
    STATUS_POS_UNARRIVE,
}
# Foutsituatie. Gedrag per gebruikerswens: als pauze (start/stop/dock tonen).
ERROR_STATES = {
    STATUS_IN_TROUBLE,
}

# HA vacuum activity mapping. Waarden corresponderen 1-op-1 met de
# VacuumActivity-enum ("cleaning"/"docked"/"idle"/"paused"/"returning"/
# "error"); vacuum.py zet ze om naar de enum.
HA_STATE_MAP = {
    STATUS_STANDBY:      "idle",      # aan, niet op dock (bv. net na stop)
    STATUS_SMART:        "cleaning",
    STATUS_ZONE_CLEAN:   "cleaning",
    STATUS_PART_CLEAN:   "cleaning",
    STATUS_CLEANING:     "cleaning",
    STATUS_PAUSED:       "paused",
    STATUS_GOTO_POS:     "cleaning",
    STATUS_POS_ARRIVED:  "idle",
    STATUS_POS_UNARRIVE: "idle",
    STATUS_GOTO_CHARGE:  "returning",
    STATUS_CHARGING:     "docked",
    STATUS_CHARGE_DONE:  "docked",
    STATUS_SLEEP:        "idle",
    STATUS_SELECT_ROOM:  "cleaning",
    STATUS_SEEK_DUST:    "returning",
    STATUS_COLLECT_DUST: "docked",
    STATUS_IN_TROUBLE:   "error",
    STATUS_REMOTE_CTRL:  "cleaning",  # actief bestuurd; beste HA-benadering
}

# ════════════════════════════════════════════════════════════════════════
# Enum waarden
# ════════════════════════════════════════════════════════════════════════

SUCTION_OPTIONS    = ["closed", "gentle", "normal", "strong", "max"]
CISTERN_OPTIONS    = ["closed", "low", "middle", "high"]
# 'both_work' is de werkelijke device-canonieke waarde voor wat
# gebruikers/Tuya app/docs "sweep_and_mop" noemen. Tjilla firmware accepteert
# `sweep_and_mop` als input maar rapporteert altijd `both_work` als output —
# zie WORK_MODE_ALIASES voor de mapping bij inkomende waarden.
WORK_MODE_OPTIONS  = ["only_sweep", "both_work", "only_mop"]
MODE_OPTIONS       = ["smart", "zone", "pose", "part"]
# DP4-modewaarde voor kamerreiniging. Volgens de Tuya-interactie-
# logica moet vóór het room-clean-frame de modus expliciet op select_room
# worden gezet, anders draait de robot in smart-modus (volledige reiniging).
MODE_SELECT_ROOM   = "select_room"

# mop_state (DP40) — fysieke detectie welk reservoir is geïnstalleerd.
# Geverifieerde enum-waarden:
#   "none"       — stofreservoir (groot, voor enkel zuigen)
#   "installed"  — waterreservoir (kleiner, voor dweilen)
MOP_STATE_NONE      = "none"
MOP_STATE_INSTALLED = "installed"

# Aliassen voor inkomende DP-waarden — als device een alternatieve
# string stuurt, wordt die genormaliseerd naar onze canonieke set.
WORK_MODE_ALIASES = {
    "sweep_and_mop": "both_work",
}

# Nederlandse labels
# labels aangepast zodat ze matchen met wat de Tuya app toont —
# geeft gebruikers een consistent mentaal model tussen HA en app.
# "Uit" bij suction is een fysieke motor-uitstand.
# "Gesloten" bij cistern is een gesloten waterklep (fysieke metafoor).
SUCTION_LABELS = {
    "closed": "Uit",
    "gentle": "Eco",
    "normal": "Standaard",
    "strong": "Sterk",
    "max":    "Max",
}
CISTERN_LABELS = {
    "closed": "Gesloten",
    "low":    "Laag",
    "middle": "Midden",
    "high":   "Hoog",
}
WORK_MODE_LABELS = {
    "only_sweep": "Alleen stofzuigen",
    "both_work":  "Stofzuigen + dweilen",
    "only_mop":   "Alleen dweilen",
}

# ════════════════════════════════════════════════════════════════════════
# Foutcodes (DP28 bitmap, 30 bits)
# ════════════════════════════════════════════════════════════════════════

FAULT_CODES = {
    0:  "Zijborstel storing",
    1:  "Hoofdborstel storing",
    2:  "Linker wiel storing",
    3:  "Rechter wiel storing",
    4:  "Stofbak probleem",
    5:  "Landingssensor storing",
    6:  "Botsingssensor storing",
    7:  "Wiel vastgelopen",
    8:  "Batterij laag",
    9:  "Apparaat uit",
    10: "Upgrade niet mogelijk",
    11: "Rand vastgelopen",
    12: "Kan doel niet bereiken",
    13: "TD sensor fout",
    14: "Lidar afgedekt",
    15: "Lidar snelheid fout",
    16: "Lidar punt fout",
    17: "PSD vuil",
    18: "Zijborstel probleem",
    19: "Ventilator snelheidsfout",
    20: "Stofbak niet geplaatst",
    21: "Stofbak vol",
    22: "Stofbak vol (er uit)",
    23: "Stofzuiger vast",
    24: "Stofzuiger opgetild",
    25: "Geen waterbak",
    26: "Waterbak leeg",
    27: "Verboden zone",
    28: "Laadstation niet gevonden",
    29: "Batterij fout",
}

# ════════════════════════════════════════════════════════════════════════
# Protocol constanten
# ════════════════════════════════════════════════════════════════════════

# Command bytes in DP15 payload
# DP15 commando-bytes. Tuya-conventie (geverifieerd op vier paren via
# bennesp/robottino-rs + eigen hardware): SET = even byte X, de robot
# rapporteert status/reflectie op X+1. Zie DP15_PROTOCOL_VERIFIED.md.
CMD_ROOM_SELECT = 0x14   # SetRoomClean — kamerreiniging starten (geverifieerd)
CMD_ROOM_STATUS = 0x15   # status-reflectie van 0x14 (robot → ons)
CMD_ROOM_NAMES  = 0x25   # kamernaam-STATUS (reflectie; set zou 0x24 zijn,
                         # ongeverifieerd — rename is daarom niet geïmplementeerd)
# Ter referentie, geverifieerd maar hier niet gebruikt:
#   0x12/0x13 virtuele muur (set/status)
#   0x16      goto-punt      0x17 RequestAreaClean (robot vraagt gebiedsdata)
#   0x1A/0x1B verboden zone  0x28/0x29 zone-reiniging

# ════════════════════════════════════════════════════════════════════════
# Protocol builders — maak binary DP15 pakketten
# ════════════════════════════════════════════════════════════════════════

def _build_packet(payload: bytes) -> bytes:
    """Wrap payload in 0xAA header met length en checksum.

    Frame: 0xAA | len(2b BE) | payload | checksum_1b
    checksum = sum(payload) mod 256
    """
    checksum = sum(payload) & 0xFF
    return b"\xaa" + struct.pack(">H", len(payload)) + payload + bytes([checksum])


def build_select_rooms(room_ids: Iterable[int], clean_times: int = 1) -> bytes:
    """Bouw het kamerreiniging-STARTcommando (cmd 0x14, SetRoomClean).

    Formaat: aa <len_2BE> 14 <clean_times> <num_rooms> <room_ids...> <checksum>
      - clean_times = aantal reinigingsbeurten per kamer (1 = één keer)
      - checksum = som(cmd + databytes) & 0xFF

    Geverifieerd op hardware: het versturen van ALLEEN dit frame op DP15
    laat de robot naar de geselecteerde kamer(s) gaan; DP4/DP5 springen zelf
    naar select_room. Geen DP1, geen DP4-mode-switch nodig.

    Belangrijk: cmd 0x14 is het START-commando. De robot stuurt op cmd 0x15
    een reflectie/statusrespons terug (Tuya-conventie: set = X, status = X+1);
    dat is NIET het commando. Voorbeeld-frames (clean_times=1):
        1 kamer, id 1:  aa 00 04 14 01 01 01 17
        1 kamer, id 3:  aa 00 04 14 01 01 03 19

    Room-ID-indexering: de robot rapporteert 1-based ID's. Mocht een apparaat
    0-based blijken, dan is dat een aandachtspunt bij het mappen van ID's.
    """
    ids = list(room_ids)
    if not ids:
        # Reset/annuleer-signaal: geen kamers.
        payload = bytes([CMD_ROOM_SELECT, clean_times & 0xFF, 0x00])
    else:
        payload = (
            bytes([CMD_ROOM_SELECT, clean_times & 0xFF, len(ids) & 0xFF])
            + bytes(i & 0xFF for i in ids)
        )
    return _build_packet(payload)


def distance_to_line_segment(
    px: float, py: float,
    x1: float, y1: float,
    x2: float, y2: float,
) -> float:
    """Kortste afstand van punt naar lijnsegment."""
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5

    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    nx = x1 + t * dx
    ny = y1 + t * dy
    return ((px - nx) ** 2 + (py - ny) ** 2) ** 0.5


# ════════════════════════════════════════════════════════════════════════
# Protocol parsers
# ════════════════════════════════════════════════════════════════════════

def parse_packets(raw: bytes) -> list[dict]:
    """Parseer een DP15 byte stream in losse pakketten."""
    results = []
    pos = 0
    while pos < len(raw) - 3:
        if raw[pos] != 0xAA:
            pos += 1
            continue
        try:
            plen = struct.unpack(">H", raw[pos+1:pos+3])[0]
            if pos + 3 + plen > len(raw):
                break

            payload = raw[pos+3:pos+3+plen]
            if not payload:
                pos += 3 + plen + 1
                continue

            cmd = payload[0]
            results.append({
                "cmd":     cmd,
                "payload": payload,
                "raw":     raw[pos:pos + 3 + plen + 1],
            })
            pos += 3 + plen + 1
        except (struct.error, IndexError):
            pos += 1
    return results


def parse_fault_bitmap(bitmap: int) -> list[str]:
    """Converteer DP28 bitmap naar foutmeldingen."""
    faults = []
    for bit, name in FAULT_CODES.items():
        if bitmap & (1 << bit):
            faults.append(name)
    return faults
