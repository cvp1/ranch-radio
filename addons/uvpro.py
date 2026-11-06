"""UV-PRO add-on: control and manage a BTECH UV-PRO from the page.

What it does: the radio's live state (power, RX/TX, squelch, RSSI, the A/B
channels, dual watch, scan, GPS lock, battery), its channels as CHIRP
memories (addons/chirp.py: CHIRP's columns, value lists and CSV) with an
editor, CHIRP CSV export and a previewed import, the two VFOs (tuned by
writing their records, 251 / 252), the radio settings that have a clear
meaning, volume, regions (switching reads the new region's channels), the
FM broadcast receiver, the APRS path, power on/off, and the APRS beacon
settings. It reaches the radio through
addons/uvpro_bridge.py, a child under the Python that has `benlink`.

With "control" set (the packet hub's control port), the bridge reaches the
radio through the hub, which carries the radio's command protocol on its own
Bluetooth link next to packet (linux-station, 2026-10-16): the add-on stays
connected and packet stays up. This is the default way.

Without it, the radio and the packet hub take turns. The UV-PRO answers its
separate control channel (RFCOMM 4, or BLE) only while nothing holds its KISS
channel, which the packet hub (tnc_hub.py) normally does. With "hub" set,
the add-on borrows the radio: it asks the hub to lend it (the hub
refuses while a packet link is up), runs the bridge, reads everything, makes
the changes asked for, and gives the radio back once nothing has happened
for IDLE_S. A full read takes a few seconds, so packet is off only that
long. Between borrows the page shows the last read, and editing works on it;
a save borrows the radio, reads it again and checks the change against that
fresh read before writing. The lend is renewed while the add-on works and
runs out by itself on the hub, so a crashed server can't keep packet off.
Without "hub" the bridge holds the radio all the time (BLE, no packet hub).

Config (station.json "addons", or server.py --addons FILE):

    "uvpro": {
      "enabled": true,
      "address": "00:11:22:33:44:55",        # Linux: the radio's address; macOS: its CoreBluetooth UUID
      "python": "~/.local/share/venvs/benlink/bin/python",
      "calls": ["N9ABC"],                    # the only calls the APRS beacon may use
      "hub": "http://127.0.0.1:7310",        # the packet hub (its status: no changes while a link is up)
      "control": "tcp:127.0.0.1:8002",       # the hub's control port: no borrowing (see above)
      "transport": "rfcomm:4",               # without "control": rfcomm:N (default with "hub") or ble
      "packet_status": "http://127.0.0.1:7310/api/status",   # no changes while a link is up (default: hub's)
      "battery_every": 60,
      "idle_s": 20,                          # borrow mode: give the radio back after this long idle
      "qsy_channel": 30,                     # Winlink packet: a SPARE channel (1-based) it may write (below)
      "restore_wait_s": 30                   # Winlink packet: how long to wait to put the radio back
    }

Winlink packet QSY (control-port mode only). A Winlink packet session through
the hub (winlink.TunedLane) asks qsy(hz) to put the radio on the gateway's
frequency before Pat dials, and restore() to put it back afterwards:
  - a channel already on the frequency (FM, simplex, TX on) is selected:
    channel A set to it, dual watch and scan off; nothing is written to a
    memory. The channel A is on now wins, then the lowest number.
  - else VFO A is tuned, on a radio that has a VFO (the UV-50PRO has none:
    its VFO records read 0.000000 MHz)
  - else "qsy_channel", if set, is written with the frequency (named WL QSY,
    no tone, FM) and selected; restore writes its old contents back. Name a
    channel you keep spare for this, never one you use.
  - else the connect is refused, saying why: Winlink never dials on whatever
    channel the radio happens to be on.
Before the first write a recovery record is kept; restore() waits (up to
"restore_wait_s"; 5 s while the server stops) until no packet link is up on
the radio and it isn't transmitting, puts channel A / dual watch / scan back,
then the written memory. A restore that can't be done is retried every 5 s
for 10 minutes, and the next qsy() puts the radio back first. While the
radio is tuned, page edits of channels, settings, region, FM radio and power
are refused, and the add-on can't be switched off. Borrow mode ("hub"
without "control") refuses: borrowing drops the hub's Bluetooth link, after
which the radio's KISS service refuses every open until it is power-cycled.

Safety (the radio is a transmitter under the operator's call):
  - Nothing here keys the radio or sends packet data. The bridge has no such
    command; benlink's send_tnc_data / send_bytes are never called.
  - Frequencies are CHIRP's Frequency / Duplex / Offset and may be changed
    (the operator, 2026-10-16 evening: the earlier "don't change TX" was for that
    session only). The bridge reads every channel write back and reports a
    TX the radio rewrote (it can't transmit there).
  - Changes that alter what goes on the air (frequency, duplex, TX tone,
    power, modulation, talk-around, VOX, auto relay, the APRS path, a
    region switch, automatic beacons, power off) need "confirm": true; the
    page asks first.
  - No change while the radio is transmitting, or while the packet hub has a
    Winlink link up on it (packet_status). Winlink packet QSY (above) keeps
    these checks: it writes nothing while a link is up or the radio
    transmits, and its frequency writes are confirmed by the operator's
    Connect in the Winlink panel.
  - The beacon's callsign must be one of "calls".
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from addons import Addon, chirp

log = logging.getLogger("uvpro")

HERE = Path(__file__).resolve().parent
RESTART_S = (5.0, 15.0, 60.0)
WRITE_TIMEOUT_S = 15.0
# Borrowing the radio from the packet hub ("hub").
LEND_S = 60                   # each lend; the hub takes the radio back by itself after it
LEND_RENEW_S = 20             # asked again this often while the bridge runs
IDLE_S = 20                   # the radio goes back this long after the last read or write
READ_TIMEOUT_S = 270.0        # lend + connect + read everything (the bridge's CONNECT_TIMEOUT_S
                              # covers a 180-channel UV-50PRO; the lend is renewed meanwhile)

# Winlink packet QSY (qsy / restore, below): the radio's channel for a session.
QSY_RESTORE_WAIT_S = 30.0     # after a session: wait this long for the link to drop and the radio to be free
QSY_RESTORE_CLOSING_S = 5.0   # the same wait while the server is stopping
QSY_RETRY_EVERY_S = 5.0       # a restore that failed is tried again this often...
QSY_RETRY_MAX_S = 600.0       # ...for this long, then the page and the log are told
QSY_POLL_S = 1.0              # how often the restore wait looks again
QSY_NAME = "WL QSY"           # the scratch channel's name while a session uses it
TUNED_REFUSAL = ("A Winlink packet session is using the radio (tuned for Winlink); "
                 "try again after it is put back.")
# Page verbs refused while the radio is tuned for Winlink: what would move it
# off the session's frequency, or keep it from being put back.
QSY_HELD_VERBS = ("channel", "settings", "import_apply", "region", "radio_power", "fm")

CALL = re.compile(r"^[A-Z0-9]{3,6}$")

ON_AIR = "Changes what this channel transmits."
# A channel, as a CHIRP memory: CHIRP's column names and value lists. Fields
# marked readonly are shown, never written (Frequency, Duplex and Offset set
# the TX frequency in CHIRP's model; TStep, the radio keeps none).
MEMORY_FIELDS = [
    {"name": "Location", "label": "Location", "type": "str", "readonly": True},
    {"name": "Name", "label": "Name", "type": "str", "max_bytes": 10},
    {"name": "Frequency", "label": "Frequency", "type": "freq", "unit": "MHz", "risky": ON_AIR},
    {"name": "Duplex", "label": "Duplex", "type": "enum", "options": chirp.DUPLEXES, "risky": ON_AIR,
     "hint": "off = RX only"},
    {"name": "Offset", "label": "Offset", "type": "freq", "unit": "MHz", "risky": ON_AIR,
     "hint": "TX frequency when Duplex is split"},
    {"name": "Tone", "label": "Tone Mode", "type": "enum", "options": chirp.TONE_MODES},
    {"name": "rToneFreq", "label": "Tone", "type": "enum", "options": chirp.TONE_STRS, "unit": "Hz", "when": ["Tone", "Cross"]},
    {"name": "cToneFreq", "label": "ToneSql", "type": "enum", "options": chirp.TONE_STRS, "unit": "Hz", "when": ["TSQL", "Cross"]},
    {"name": "DtcsCode", "label": "DTCS Code", "type": "enum", "options": chirp.DTCS_STRS, "when": ["DTCS", "Cross"]},
    {"name": "RxDtcsCode", "label": "DTCS Rx Code", "type": "enum", "options": chirp.DTCS_STRS, "when": ["Cross"]},
    {"name": "DtcsPolarity", "label": "DTCS Pol", "type": "str", "readonly": True},
    {"name": "CrossMode", "label": "Cross Mode", "type": "enum", "options": chirp.CROSS_MODES, "when": ["Cross"]},
    {"name": "Mode", "label": "Mode", "type": "enum", "options": list(chirp.MODES), "risky": ON_AIR},
    {"name": "Power", "label": "Power", "type": "enum", "options": list(chirp.POWERS.values()), "risky": ON_AIR},
    {"name": "Skip", "label": "Skip", "type": "enum", "options": chirp.SKIP_VALUES, "hint": "S = left out of scan"},
    {"name": "TStep", "label": "Tuning Step", "type": "str", "unit": "kHz", "readonly": True},
    {"name": "talk_around", "label": "Talk-around", "type": "bool", "extra": True, "risky": ON_AIR},
    {"name": "mute", "label": "Mute", "type": "bool", "extra": True},
]
SETTINGS_FIELDS = [
    {"name": "channel_a", "label": "Channel A", "type": "channel"},
    {"name": "channel_b", "label": "Channel B", "type": "channel"},
    {"name": "scan", "label": "Scan", "type": "bool"},
    {"name": "squelch_level", "label": "Squelch", "type": "int", "min": 0, "max": 9},
    {"name": "mic_gain", "label": "Mic gain", "type": "int", "min": 0, "max": 7},
    {"name": "bt_mic_gain", "label": "Bluetooth mic gain", "type": "int", "min": 0, "max": 7},
    {"name": "tail_elim", "label": "Squelch tail elimination", "type": "bool"},
    {"name": "power_saving_mode", "label": "Power saving", "type": "bool"},
    {"name": "dis_tone", "label": "Tones off", "type": "bool"},
    {"name": "imperial_unit", "label": "Imperial units", "type": "bool"},
    {"name": "ptt_lock", "label": "PTT lock", "type": "bool"},
    {"name": "ch_data_lock", "label": "Channel edit lock", "type": "bool",
     "risky": "On, the radio refuses channel edits, these included, until it is turned off."},
    {"name": "double_channel", "label": "Dual watch", "type": "enum", "options": [0, 1, 2],
     "labels": ["Off", "A", "B"], "hint": "which side the radio reports active"},
    {"name": "tx_time_limit", "label": "TX time limit", "type": "int", "min": 0, "max": 31,
     "hint": "the radio's menu step; 0 = off"},
    {"name": "auto_power_off", "label": "Auto power off", "type": "int", "min": 0, "max": 7,
     "hint": "the radio's menu step; 0 = off"},
    {"name": "screen_timeout", "label": "Screen timeout", "type": "int", "min": 0, "max": 31,
     "hint": "the radio's menu step"},
    {"name": "ns_en", "label": "Noise reduction", "type": "bool"},
    {"name": "wx_mode", "label": "NOAA weather", "type": "enum", "options": [0, 1, 2],
     "labels": ["Off", "Monitor", "Alert"]},
    {"name": "noaa_ch", "label": "NOAA channel", "type": "int", "min": 0, "max": 15, "hint": "0 = WX1"},
    {"name": "alarm_volume", "label": "Alarm volume", "type": "int", "min": 0, "max": 15},
    {"name": "dis_bt_mic", "label": "Bluetooth mic off", "type": "bool"},
    {"name": "gpwpl_upload_en", "label": "Heard positions as NMEA waypoints", "type": "bool",
     "hint": "$GPWPL out over Bluetooth"},
    {"name": "vox_en", "label": "VOX", "type": "bool", "risky": "VOX keys the transmitter on sound at the mic."},
    {"name": "vox_level", "label": "VOX level", "type": "int", "min": 0, "max": 7},
    {"name": "vox_delay", "label": "VOX delay", "type": "int", "min": 0, "max": 7, "hint": "the radio's menu step"},
    {"name": "auto_relay_en", "label": "Auto relay", "type": "bool",
     "risky": "The radio repeats what it receives: it transmits by itself."},
]
# Settings that make the radio key up on its own, outside the packet hub's
# transmit gate (2026-10-17: Auto relay on digipeated APRS under our call and
# read as "transmitting" for seconds at a time, blocking every change).
SELF_KEYING = (("auto_relay_en", "Auto relay"), ("vox_en", "VOX"))


def self_keys_on(settings):
    """Labels of the self-keying settings that are on, in SELF_KEYING order."""
    s = settings or {}
    return [label for name, label in SELF_KEYING if s.get(name)]


AUTO_TX = ("The radio transmits by itself. BTECH's Winlink guide: a share-location interval can turn "
           "Digital Mode back on, which stops the KISS TNC.")
BEACON_FIELDS = [
    {"name": "aprs_callsign", "label": "Callsign", "type": "str", "max_bytes": 6, "risky": ON_AIR},
    {"name": "aprs_ssid", "label": "SSID", "type": "int", "min": 0, "max": 15},
    {"name": "aprs_symbol", "label": "Symbol", "type": "str", "max_bytes": 2, "hint": "table + code, e.g. /["},
    {"name": "beacon_message", "label": "Comment", "type": "str", "max_bytes": 18},
    {"name": "packet_format", "label": "Format", "type": "enum", "options": ["APRS", "BSS"], "risky": ON_AIR},
    {"name": "should_share_location", "label": "Beacon position", "type": "bool", "risky": AUTO_TX},
    {"name": "location_share_interval", "label": "Every", "type": "int", "unit": "s", "min": 0, "max": 2550,
     "step": 10, "risky": AUTO_TX},
    {"name": "send_pwr_voltage", "label": "Include battery voltage", "type": "bool"},
    {"name": "ptt_release_send_location", "label": "Position after each PTT", "type": "bool", "risky": AUTO_TX},
    {"name": "ptt_release_send_id_info", "label": "ID after each PTT", "type": "bool", "risky": AUTO_TX},
]
# The radio's channel fields that change what it puts on the air.
RISKY_RADIO = {"rx_freq": "Frequency", "tx_freq": "TX frequency", "tx_disable": "RX only",
               "tx_mod": "Modulation", "tx_sub_audio": "TX tone", "power": "Power", "talk_around": "Talk-around"}
SCHEMAS = {"channel": MEMORY_FIELDS, "settings": SETTINGS_FIELDS, "beacon": BEACON_FIELDS}
VFO_IDS = (251, 252)          # the VFO records (uvpro_bridge.py)
VFO_NAMES = {251: "VFO A", 252: "VFO B"}
FM_MHZ = (64.0, 108.0)
DIGI = re.compile(r"^[A-Z0-9]{1,6}(-([0-9]|1[0-5]))?$")
APRS_PATH_MAX = 8             # AX.25 carries at most 8 digipeaters
# ("power" is the add-on's own on/off, addons/__init__.py: the radio's is "radio_power")
RADIO_VERBS = ("volume", "region", "fm", "aprs_path", "radio_power")
CONTROL_WHAT = {"volume": "volume", "region": "region", "fm": "FM radio", "aprs_path": "APRS path",
                "radio_power": "radio power"}
BRIDGE_CMD = {"radio_power": "power"}


@dataclass
class QsyResult:
    """What qsy() did: ok, or why not; how ("channel 25", "channel 26
    written", "VFO A") and what restore() goes back to ("channel 24")."""
    ok: bool
    why: str | None = None
    how: str = ""
    back: str = ""


def _where(cid):
    return VFO_NAMES.get(cid) or f"channel {cid + 1}"


class UvproAddon(Addon):
    name = "uvpro"
    group = "radios"
    label = "UV-PRO"
    title = "BTECH UV-PRO: channels, settings, battery and APRS beacon over Bluetooth"
    css = True

    def __init__(self, host, opts):
        super().__init__(host, opts)
        if not opts.get("address"):
            raise ValueError("uvpro needs \"address\": the radio's Bluetooth address (or macOS UUID)")
        hub = str(opts["hub"]).rstrip("/") if opts.get("hub") else None
        self.control_port = opts.get("control")
        self.hub = None if self.control_port else hub          # set: borrow the radio from the hub
        self.idle_s = float(opts.get("idle_s", IDLE_S))
        transport = self.control_port or opts.get("transport", "rfcomm:4" if self.hub else None)
        self.cmd = [os.path.expanduser(opts.get("python", "~/.local/share/venvs/benlink/bin/python")),
                    os.path.expanduser(opts.get("bridge", str(HERE / "uvpro_bridge.py"))), str(opts["address"]),
                    f"--battery-every={float(opts.get('battery_every', 60))}"]
        if transport:
            self.cmd.append(f"--transport={transport}")
        self.calls = {str(c).upper() for c in opts.get("calls", [])}
        self.packet_status = opts.get("packet_status") or (f"{hub}/api/status" if hub else None)
        self._reset()
        self.read_at = None       # when the radio was last read in full
        self.borrowed = False     # the hub has lent us the radio
        self.proc = None
        self._task = None
        self._first = None        # borrow mode: the read at start
        self._renewer = None
        self._idle = None
        self._session_lock = asyncio.Lock()
        self._read = asyncio.Event()   # this bridge run has read everything
        self._closing = False
        self._ref = 0
        self._writes = {}         # ref -> (ws, what, future, batch)
        self._batches = {}        # batch id -> {"ws", "left", "failed": [what], "n"}
        # Winlink packet QSY. qsy_channel: a spare channel (1-based, as on the
        # radio) written with the gateway's frequency when no channel has it.
        qc = opts.get("qsy_channel")
        if qc is not None and (isinstance(qc, bool) or not isinstance(qc, int) or qc < 1):
            raise ValueError("uvpro \"qsy_channel\" is a channel number (1 = the first channel), or absent")
        self.qsy_channel = qc
        self.restore_wait_s = float(opts.get("restore_wait_s", QSY_RESTORE_WAIT_S))
        self.restore_closing_s = QSY_RESTORE_CLOSING_S
        self._qsy = None          # the recovery record while the radio is off its channel for Winlink
        self._lock = asyncio.Lock()   # one owner of check-and-write: QSY, restore, page writes
        self._restore_task = None     # the deferred retry of a restore that failed
        self._gave_up = False         # that retry ran out: switching off is allowed again (loudly)

    def _reset(self):
        self.connected, self.error = False, None
        self.device, self.channels, self.settings, self.beacon = None, [], None, None
        self.status, self.battery = None, None
        self.vfos, self.extras = [], {}
        self.exact = None         # what the bridge reads back byte for byte (None: not told)
        self._reached = False

    # --- lifecycle

    async def start(self):
        self._closing = False
        if self.hub:
            self._first = asyncio.create_task(self._first_read())
        else:
            self._task = asyncio.create_task(self._supervise())

    async def stop(self):
        self._closing = True
        if self._qsy is not None:
            log.warning("UV-PRO: stopping with the radio still tuned for Winlink (%.4f MHz on %s); it may be "
                        "off its APRS channel: put it back on %s by hand", self._qsy["hz"] / 1e6,
                        _where(self._qsy["target"]), _where(self._qsy["prev"].get("channel_a", 0)))
        if self._restore_task and not self._restore_task.done():
            self._restore_task.cancel()
        self._restore_task = None
        for t in (self._first, self._idle, self._renewer, self._task):
            if t and not t.done():
                t.cancel()
        self._first = self._idle = self._renewer = self._task = None
        await self._close_bridge()
        if self.borrowed:
            await self._give_back()
        self._fail_writes("the UV-PRO add-on was switched off")
        self._reset()
        self.read_at = None

    async def _close_bridge(self):
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()   # the bridge disconnects cleanly on EOF
            try:
                await asyncio.wait_for(self.proc.wait(), 8)
            except TimeoutError:
                self.proc.kill()

    async def _run_bridge(self):
        """One bridge process, start to exit: its exit code, or None if it
        could not be started."""
        self._reached = False
        self._read.clear()
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            log.error("cannot start the UV-PRO bridge (%s): %s", self.cmd[0], e)
            self._status(False, f"bridge not started: {e}")
            return None
        log.info("UV-PRO bridge started (pid %d) for %s", self.proc.pid, self.cmd[2])
        err = asyncio.create_task(self._stderr(self.proc.stderr))
        async for raw in self.proc.stdout:
            try:
                self._on_bridge(json.loads(raw))
            except (ValueError, KeyError, TypeError) as e:
                log.warning("UV-PRO bridge: bad line %r (%s)", raw[:200], e)
        code = await self.proc.wait()
        await err
        return code

    async def _supervise(self):
        """Not borrowing (the hub's control port, or no hub): keep the bridge on the radio all the time."""
        fails = 0
        while not self._closing:
            code = await self._run_bridge()
            if code is None:
                await asyncio.sleep(RESTART_S[-1])
                continue
            if self._closing:
                return
            self._fail_writes("the radio went away before the change was confirmed")
            if self.connected:
                self._status(False, self.error or "UV-PRO bridge stopped")
            fails = 0 if self._reached else fails + 1
            wait = RESTART_S[min(fails, len(RESTART_S) - 1)]
            (log.info if fails > 1 else log.warning)(
                "UV-PRO bridge exited (code %s: %s); restarting in %gs", code, self.error, wait)
            await asyncio.sleep(wait)

    # --- borrow mode: the radio, lent by the packet hub for a read or a change

    def _bridge_up(self):
        return (self.connected and self.proc is not None and self.proc.returncode is None
                and not self.proc.stdin.is_closing())

    async def _first_read(self):
        why = await self._session()
        if why:
            log.warning("UV-PRO: first read not done: %s", why)

    async def _session(self):
        """Have the bridge on the radio, borrowing it from the hub first.
        Returns why not, or None once the radio has been read in full."""
        async with self._session_lock:
            if self._bridge_up() and self._read.is_set():
                self._touch()
                return None
            if self._closing:
                return "the UV-PRO add-on was switched off"
            if self._task and not self._task.done():
                # the last borrow is still ending: let it give the radio back first
                await asyncio.wait({self._task}, timeout=15)
            why = await self._lend()
            if why:
                self._status(False, why)
                return why
            self.borrowed = True
            self._status(False, None)
            self._task = asyncio.create_task(self._borrowed_run())
            self._renewer = asyncio.create_task(self._renew())
            read = asyncio.create_task(self._read.wait())
            done, _ = await asyncio.wait({read, self._task}, timeout=READ_TIMEOUT_S,
                                         return_when=asyncio.FIRST_COMPLETED)
            read.cancel()
            if not self._read.is_set():
                if not self._task.done():
                    await self._close_bridge()
                    await asyncio.wait({self._task}, timeout=10)
                return self.error or "the UV-PRO did not answer"
            self._touch()
            return None

    async def _borrowed_run(self):
        code = await self._run_bridge()
        for t in (self._renewer, self._idle):
            if t and not t.done() and t is not asyncio.current_task():
                t.cancel()
        self._renewer = self._idle = None
        self._fail_writes("the radio went away before the change was confirmed")
        if self.borrowed:
            await self._give_back()
        if self.connected or code not in (0, 1):
            self._status(False, None if code in (0, 1) else (self.error or f"UV-PRO bridge stopped ({code})"))
        else:
            self._emit_status()
        log.info("UV-PRO: bridge done (code %s); the radio is back with packet", code)

    def _touch(self):
        """Something was read or written: give the radio back IDLE_S from now."""
        if self._idle and not self._idle.done():
            self._idle.cancel()
        self._idle = asyncio.create_task(self._idle_out())

    async def _idle_out(self):
        await asyncio.sleep(self.idle_s)
        while self._writes:                        # a write still waiting on the radio
            await asyncio.sleep(1)
        log.info("UV-PRO: idle for %gs; giving the radio back", self.idle_s)
        await self._close_bridge()

    async def _hub_post(self, path, body=None):
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            # The hub refuses POSTs without this header (cross-site guard).
            async with s.post(self.hub + path, json=body or {}, headers={"X-Ranch-Radio": "1"}) as r:
                try:
                    d = await r.json(content_type=None)
                except ValueError:
                    d = {}
                return r.status, d

    async def _lend(self):
        """Ask the hub for the radio: why not, or None."""
        try:
            code, d = await self._hub_post("/api/lend", {"seconds": LEND_S, "to": "uvpro"})
        except Exception as e:    # noqa: BLE001 - no hub running: nothing holds the radio
            log.info("packet hub not reached (%s); using the radio without borrowing", e)
            return None
        if code != 200:
            return f"packet has the radio: {d.get('error') or f'HTTP {code}'}"
        log.info("UV-PRO: the packet hub lent us the radio")
        return None

    async def _renew(self):
        while True:
            await asyncio.sleep(LEND_RENEW_S)
            try:
                code, d = await self._hub_post("/api/lend", {"seconds": LEND_S, "to": "uvpro"})
                if code != 200:
                    log.warning("UV-PRO: the hub did not extend the lend: %s", d.get("error") or code)
            except Exception as e:   # noqa: BLE001
                log.warning("UV-PRO: lend not extended (%s)", e)

    async def _give_back(self):
        self.borrowed = False
        try:
            await self._hub_post("/api/return")
            log.info("UV-PRO: radio given back to the packet hub")
        except Exception as e:       # noqa: BLE001 - the hub takes it back when the lend runs out
            log.info("UV-PRO: give back not reached (%s); the lend runs out by itself", e)

    async def _ready(self, ws):
        """The bridge is on the radio (borrowed first, in borrow mode), or a
        notice says why not."""
        if not self.hub:
            return self._usable(ws)
        why = await self._session()
        if why:
            self.notice(f"UV-PRO: {why}", "warn", ws)
            return False
        return True

    async def _prepare(self, ws, check):
        """CHECK() -> (why, *rest). Refuse early on what is held, then get the
        radio and, in borrow mode, check again on what it just read. The rest,
        or None (a notice said why)."""
        if not self.hub and not self._usable(ws):
            return None
        held = bool(self.channels) and self.settings is not None
        for fresh in ((False, True) if self.hub else (False,)):
            if fresh:
                if not await self._ready(ws):
                    return None
            elif self.hub and not held:
                continue                           # nothing read yet: check after the read
            why, *rest = check()
            if not why:
                why = await self._busy()
            if why:
                self.notice(why, "warn", ws)
                return None
        return rest

    async def _stderr(self, stream):
        async for raw in stream:
            log.info("bridge: %s", raw.decode(errors="replace").rstrip())

    # --- bridge -> pages

    def _on_bridge(self, m):
        ev = m["ev"]
        if ev == "status":
            self._status(m["connected"], m.get("error"))
        elif ev == "device":
            self.device = m["info"]
            self.emit({"device": self.device})
        elif ev == "exact":
            self.exact = {"channels": list(m.get("channels") or []), "settings": bool(m.get("settings")),
                          "beacon": bool(m.get("beacon"))}
            self.emit({"exact": self.exact})
        elif ev == "channels":
            self.channels = m["channels"]
            self.emit({"memories": self.memories()})
        elif ev == "channel":
            c = m["channel"]
            self.channels = [c if x["id"] == c["id"] else x for x in self.channels]
            if not any(x["id"] == c["id"] for x in self.channels):
                self.channels.append(c)
            self.emit({"memory": chirp.to_memory(c)})
        elif ev == "vfo":
            self.vfos = m["channels"]
            self.emit({"vfos": self.vfo_memories()})
        elif ev == "extras":
            self.extras = {**self.extras, **m["values"]}
            self.emit({"extras": self.extras})
        elif ev == "settings":
            was = self_keys_on(self.settings)
            self.settings = m["values"]
            now_on = self_keys_on(self.settings)
            if now_on != was:
                if now_on:
                    log.warning("UV-PRO: %s on: the radio transmits by itself", " and ".join(now_on))
                elif was:
                    log.info("UV-PRO: %s off", " and ".join(was))
            self.emit({"settings": self.settings})
        elif ev == "beacon":
            self.beacon = m["values"]
            self.emit({"beacon": self.beacon})
            if self.connected and not self._read.is_set():     # the last of a full read
                self.read_at = time.time()
                self._read.set()
                self._emit_status()
        elif ev == "state":
            was_tx = bool((self.status or {}).get("is_in_tx"))
            self.status = m["status"]
            if bool(self.status.get("is_in_tx")) != was_tx:
                log.info("UV-PRO %s", "transmitting" if self.status.get("is_in_tx") else "transmit ended")
            self.emit({"state": self.status})
        elif ev == "battery":
            self.battery = {"percent": m.get("percent"), "volts": m.get("volts")}
            self.emit({"battery": self.battery})
        elif ev == "done":
            if self.hub:
                self._touch()
            rec = self._writes.pop(m["ref"], None)
            if rec is None:                   # already given up on (_expire) or failed: told once
                log.info("UV-PRO: a late answer for write %s (ok %s) ignored", m.get("ref"), m.get("ok"))
                return
            ws, what, fut, batch = rec
            if fut and not fut.done():
                fut.set_result(m)
            if batch == "silent":             # qsy / restore: the caller reads the result
                if m["ok"]:
                    log.info("UV-PRO: %s written", what)
                else:
                    log.warning("UV-PRO: %s not written: %s", what, m.get("error"))
            elif batch == "quiet":            # the page shows the new value itself; only a failure is told
                if not m["ok"]:
                    log.warning("UV-PRO: %s not set: %s", what, m.get("error"))
                    self.notice(f"UV-PRO: {what} not set: {m.get('error')}", "warn", ws)
            elif batch is not None:
                self._batch_done(batch, what, m)
            elif m["ok"]:
                log.info("UV-PRO: %s written", what)
                self.notice(f"UV-PRO: {what} saved.", "info", ws)
            else:
                log.warning("UV-PRO: %s not written: %s", what, m.get("error"))
                self.notice(f"UV-PRO: {what} not saved: {m.get('error')}", "warn", ws)

    def _status(self, connected, error):
        self.connected, self.error = bool(connected), error
        self._reached = self._reached or self.connected
        log.info("UV-PRO: %s%s", "connected" if connected else "disconnected", f" ({error})" if error else "")
        self._emit_status()

    def status_out(self):
        return {"connected": self.connected, "error": self.error, "borrow": bool(self.hub),
                "via": "hub" if self.control_port else "borrow" if self.hub else "direct",
                "borrowed": self.borrowed, "read_at": self.read_at}

    def _emit_status(self):
        self.emit({"status": self.status_out()})

    def _fail_writes(self, why):
        for ws, what, fut, batch in self._writes.values():
            if fut and not fut.done():
                fut.set_result({"ok": False, "error": why})
            if batch not in (None, "quiet", "silent"):
                self._batch_done(batch, what, {"ok": False, "error": why})
            elif batch != "silent":
                self.notice(f"UV-PRO: {what} may not be saved: {why}.", "warn", ws)
        self._writes.clear()

    def _batch_done(self, bid, what, m):
        if bid in ("quiet", "silent"):
            return
        b = self._batches.get(bid)
        if b is None:
            return
        b["left"] -= 1
        if not m.get("ok"):
            b["failed"].append(f"{what} ({m.get('error')})")
        if b["left"] <= 0:
            del self._batches[bid]
            ok = b["n"] - len(b["failed"])
            if b["failed"]:
                self.notice(f"UV-PRO import: {ok} of {b['n']} channels saved; not saved: "
                            + "; ".join(b["failed"]), "warn", b["ws"])
            else:
                self.notice(f"UV-PRO import: {ok} channel{'s' if ok != 1 else ''} saved.", "info", b["ws"])
            log.info("UV-PRO import: %d of %d channels written", ok, b["n"])

    def memories(self):
        return [chirp.to_memory(c) for c in sorted(self.channels, key=lambda c: c["id"])]

    def vfo_memories(self):
        return [{**chirp.to_memory(c), "Location": VFO_NAMES.get(c["id"], str(c["id"])), "vfo": True}
                for c in sorted(self.vfos, key=lambda c: c["id"])]

    def snapshot(self):
        return {"status": self.status_out(), "device": self.device, "exact": self.exact,
                "memories": self.memories(), "vfos": self.vfo_memories(), "extras": self.extras,
                "settings": self.settings, "beacon": self.beacon,
                "state": self.status, "battery": self.battery,
                "schema": SCHEMAS, "calls": sorted(self.calls), "qsy": self._qsy_out()}

    # --- pages -> radio

    async def command(self, ws, verb, m):
        if verb == "refresh":
            if self.hub and not self._bridge_up():
                await self._ready(ws)              # borrowing it reads everything
            elif self._usable(ws):
                await self._send_bridge({"cmd": "refresh"})
                if self.hub:
                    self._touch()
        elif verb == "give_back":
            if self.hub and self._bridge_up() and not self._writes:
                log.info("UV-PRO: giving the radio back (asked from the page)")
                await self._close_bridge()
        elif verb in ("channel", "settings", "beacon", "import_apply") or verb in RADIO_VERBS:
            await self._owned_write(ws, verb, m)
        elif verb == "export":
            if not self.channels:
                self.notice("The radio's channels are not read yet.", "warn", ws)
            else:
                self.emit({"export": {"csv": chirp.to_csv(self.memories())}}, ws)
        elif verb == "import_preview":
            self.emit({"import_plan": self.import_plan(m.get("csv"))}, ws)
        else:
            await super().command(ws, verb, m)

    def _held_for_winlink(self, ws, verb):
        if self._qsy is not None and verb in QSY_HELD_VERBS:
            self.notice(TUNED_REFUSAL, "warn", ws)
            return True
        return False

    async def _owned_write(self, ws, verb, m):
        """A page write: refused while the radio is tuned for Winlink, else
        checked and queued under the one lock QSY and restore also take."""
        if self._held_for_winlink(ws, verb):
            return
        async with self._lock:
            if self._held_for_winlink(ws, verb):       # tuned while this one waited
                return
            if verb == "channel":
                await self.write_memory(ws, m)
            elif verb in ("settings", "beacon"):
                await self.write(ws, verb, m)
            elif verb == "import_apply":
                await self.import_apply(ws, m)
            else:
                await self.control(ws, verb, m)

    def _usable(self, ws):
        if not self.connected or self.proc is None or self.proc.returncode is not None:
            self.notice("The UV-PRO is not connected.", "warn", ws)
            return False
        return True

    async def _send_bridge(self, d):
        self.proc.stdin.write((json.dumps(d) + "\n").encode())
        await self.proc.stdin.drain()

    # --- channels, as CHIRP memories

    def check_memory(self, cid, edits, confirm=False):
        """Why the CHIRP memory edits for channel CID can't be written, or None
        and the radio values to send (only what changes) and the on-air
        changes among them."""
        if not isinstance(cid, int) or isinstance(cid, bool):
            return "No channel given.", None, []
        vfo = cid in VFO_IDS
        ch = next((c for c in (self.vfos if vfo else self.channels) if c["id"] == cid), None)
        if ch is None:
            return (f"{VFO_NAMES[cid]} was not read from the radio." if vfo
                    else f"The radio has no channel {cid + 1}."), None, []
        if not isinstance(edits, dict) or not edits:
            return "Nothing to save.", None, []
        what = VFO_NAMES[cid] if vfo else f"Channel {cid + 1}"
        if self.exact is not None and cid not in self.exact["channels"]:
            return (f"{what} is not changed from here: benlink does not read it back byte for byte, "
                    "so a write could change more than asked."), None, []
        if vfo and ("Name" in edits or (ch.get("name") or "").strip()):
            return f"{what}: a VFO has no name (a named record 251/252 is not a VFO; not written).", None, []
        fields = {f["name"]: f for f in MEMORY_FIELDS}
        mem, extra = {}, {}
        for k, v in edits.items():
            f = fields.get(k)
            if f is None:
                return f"No CHIRP field {k!r} here.", None, []
            if f.get("extra"):
                if not isinstance(v, bool):
                    return f"{f['label']}: not on/off.", None, []
                extra[k] = v
            else:
                mem[k] = chirp.normalize(k, v)
        if extra:
            mem["extra"] = extra
        why, values = chirp.from_memory(mem, ch)
        if why:
            return why, None, []
        if not values:
            return "No change.", None, []
        risky = [label for k, label in RISKY_RADIO.items() if k in values]
        if risky and not confirm:
            return f"Needs confirming (changes what goes on the air): {', '.join(risky)}.", None, risky
        return None, values, risky

    async def write_memory(self, ws, m):
        cid = m.get("id")
        got = await self._prepare(ws, lambda: self.check_memory(cid, m.get("values"), bool(m.get("confirm"))))
        if got is not None:
            await self._queue(ws, "channel", got[0], cid, VFO_NAMES.get(cid) or f"channel {cid + 1}")

    def import_plan(self, text):
        """What a CHIRP CSV would change, row by row, without changing anything.
        A row is matched to the channel at its Location; channels the file
        doesn't list are left alone (as CHIRP's import does)."""
        if not isinstance(text, str) or len(text) > 200_000:
            return {"error": "Not a CHIRP CSV file (or larger than 200 kB)."}
        if not self.channels:
            return {"error": "The radio's channels are not read yet."}
        why, rows = chirp.parse_csv(text)
        if why:
            return {"error": why}
        out, n_change, risky_any = [], 0, False
        by_id = {c["id"]: c for c in self.channels}
        for r in rows:
            row = {"loc": r.get("Location"), "name": r.get("Name", "")}
            try:
                cid = int(r.get("Location", "")) - 1
            except ValueError:
                cid = None
            ch = by_id.get(cid) if cid is not None else None
            if ch is None:
                row.update(status="refused", why=f"Location {r.get('Location')!r}: the radio has channels 1 to "
                                                 f"{len(self.channels)}.")
            elif not r.get("Frequency"):
                row.update(status="refused", why="An empty row would clear the channel; channels are never "
                                                 "cleared from here.")
            else:
                mem = {k: r[k] for k in ("Name", "Frequency", "Duplex", "Offset", "Tone", "rToneFreq", "cToneFreq",
                                          "DtcsCode", "DtcsPolarity", "RxDtcsCode", "CrossMode", "Mode", "Skip",
                                          "Power") if k in r}
                why, values = chirp.from_memory(mem, ch)
                if why:
                    row.update(status="refused", why=why)
                elif not values:
                    row.update(status="same")
                else:
                    cur, new = chirp.to_memory(ch), chirp.to_memory({**ch, **values})
                    diff = [[f, cur[f], new[f]] for f in ("Name", "Frequency", "Duplex", "Offset", "Tone", "rToneFreq",
                                                           "cToneFreq", "DtcsCode", "RxDtcsCode", "CrossMode", "Mode",
                                                           "Skip", "Power")
                            if cur[f] != new[f] and (f in ("Name", "Frequency", "Duplex", "Tone", "Mode", "Skip", "Power")
                                                     or (f == "Offset" and new["Duplex"] in ("+", "-", "split"))
                                                     or chirp.tone_summary(cur) != chirp.tone_summary(new))]
                    risky = [label for k, label in RISKY_RADIO.items() if k in values]
                    row.update(status="change", id=cid, values=values, diff=diff, risky=risky,
                               tone=[chirp.tone_summary(cur), chirp.tone_summary(new)])
                    n_change += 1
                    risky_any = risky_any or bool(risky)
            out.append(row)
        return {"rows": out, "changes": n_change, "risky": risky_any,
                "refused": sum(r["status"] == "refused" for r in out)}

    async def import_apply(self, ws, m):
        def check():
            plan = self.import_plan(m.get("csv"))     # worked out again here: the page's copy is not trusted
            if plan.get("error"):
                return f"UV-PRO import: {plan['error']}", None
            if not plan["changes"]:
                return "UV-PRO import: nothing to change.", None
            if plan["risky"] and not m.get("confirm"):
                return "UV-PRO import: needs confirming (changes what goes on the air).", None
            return None, plan
        got = await self._prepare(ws, check)
        if got is None:
            return
        plan = got[0]
        self._ref += 1
        bid = f"import-{self._ref}"
        rows = [r for r in plan["rows"] if r["status"] == "change"]
        self._batches[bid] = {"ws": ws, "left": len(rows), "failed": [], "n": len(rows)}
        log.info("UV-PRO import: writing %d channels (%d rows refused)", len(rows), plan["refused"])
        for r in rows:
            await self._queue(ws, "channel", r["values"], r["id"], f"channel {r['id'] + 1}", batch=bid)

    async def _queue(self, ws, kind, values, cid, what, batch=None, quiet=False, silent=False):
        """Send one write to the bridge; its future resolves with the bridge's
        done (or a failure: no answer, not sent, the bridge gone). quiet: the
        page is told only of a failure; silent: never (the caller reads the
        future: qsy / restore)."""
        self._ref += 1
        ref = self._ref
        fut = asyncio.get_running_loop().create_future()
        mark = batch if batch is not None else ("silent" if silent else "quiet" if quiet else None)
        self._writes[ref] = (ws, what, fut, mark)
        cmd = {"cmd": kind, "ref": ref, "values": values}
        if kind == "channel":
            cmd["id"] = cid
        log.info("UV-PRO: writing %s %s", what, sorted(values))
        try:
            await self._send_bridge(cmd)
        except (OSError, AttributeError, RuntimeError, ValueError) as e:   # the bridge gone (or never there)
            self._writes.pop(ref, None)
            err = f"not sent to the radio: {type(e).__name__}: {e}"
            log.warning("UV-PRO: %s %s", what, err)
            if not fut.done():
                fut.set_result({"ok": False, "error": err})
            if mark not in (None, "quiet", "silent"):
                self._batch_done(mark, what, {"ok": False, "error": err})
            elif mark != "silent":
                self.notice(f"UV-PRO: {what} not saved: {err}", "warn", ws)
            return fut
        asyncio.ensure_future(self._expire(ref, fut))
        return fut

    async def _write_wait(self, kind, values, cid, what):
        """Queue a silent write and wait for its outcome: the bridge's done
        message, or {"ok": False, "error": ...}. _expire resolves it after
        WRITE_TIMEOUT_S, so this never waits longer than that (plus a margin)."""
        fut = await self._queue(None, kind, values, cid, what, silent=True)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), WRITE_TIMEOUT_S + 5)
        except TimeoutError:
            return {"ok": False, "error": "no answer from the radio"}

    # --- settings and beacon

    def check(self, kind, values, cid=None, confirm=False):
        """Why VALUES can't be written to the radio, or None (and the values
        typed for the bridge). Checks the field schema, then the result as a
        whole: band plan, callsign, confirmation of on-air changes."""
        if not isinstance(values, dict) or not values:
            return "Nothing to save.", None
        fields = {f["name"]: f for f in SCHEMAS[kind]}
        current = {}
        if kind == "settings":
            current = self.settings or {}
        else:
            current = self.beacon or {}
        if not current:
            return "The radio's values are not read yet.", None
        if self.exact is not None and not self.exact[kind]:
            return (f"The {'settings' if kind == 'settings' else 'beacon settings'} are not changed from here: "
                    "benlink does not read them back byte for byte."), None
        out = {}
        for name, v in values.items():
            f = fields.get(name)
            if f is None:
                return f"No setting {name!r} here.", None
            t, label = f["type"], f["label"]
            if t == "bool":
                if not isinstance(v, bool):
                    return f"{label}: not on/off.", None
            elif t in ("int", "channel"):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
                    return f"{label}: not a whole number.", None
                v = int(v)
                if t == "channel":
                    if not (0 <= v < len(self.channels) or v in {c["id"] for c in self.vfos}):
                        return f"{label}: the radio has no channel {v + 1}.", None
                elif not f["min"] <= v <= f["max"]:
                    return f"{label}: {v} is outside {f['min']}..{f['max']}.", None
                if f.get("step") and v % f["step"]:
                    return f"{label}: must be a multiple of {f['step']}.", None
            elif t == "enum":
                if v not in f["options"] and v != current.get(name):
                    return f"{label}: {v!r} is not one of the choices.", None
            elif t == "str":
                if not isinstance(v, str):
                    return f"{label}: not text.", None
                if len(v.encode()) > f["max_bytes"]:
                    return f"{label}: longer than {f['max_bytes']} bytes.", None
            if v != current.get(name):
                out[name] = v
        if not out:
            return "No change.", None
        if kind == "beacon" and "aprs_callsign" in out:
            call = out["aprs_callsign"] = out["aprs_callsign"].strip().upper()
            if not self.calls:
                return "No callsigns are configured for the beacon (\"calls\").", None
            if not CALL.match(call) or call not in self.calls:
                return f"Callsign {call!r} is not one of {', '.join(sorted(self.calls))}.", None
        risky = [fields[n]["label"] for n in out if fields[n].get("risky")]
        if risky and not confirm:
            return f"Needs confirming (changes what goes on the air): {', '.join(risky)}.", None
        return None, out

    # --- volume, region, FM radio, APRS path, power

    def check_control(self, verb, m):
        """Why VERB can't be done, or None and the bridge's values."""
        v, ex, dev = m.get("values") or {}, self.extras or {}, self.device or {}
        confirm = bool(m.get("confirm"))
        if not isinstance(v, dict) or not v:
            return "Nothing to do.", None
        if verb == "volume":
            level = v.get("level")
            if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 15:
                return "Volume: a whole number 0 to 15.", None
            return None, {"level": level}
        if verb == "region":
            r, names = v.get("region"), ex.get("regions") or []
            n = dev.get("region_count") or len(names)
            if isinstance(r, bool) or not isinstance(r, int) or not 0 <= r < n:
                return f"Region: the radio has regions 1 to {n}." if n else "The radio's regions are not read yet.", None
            if r == ex.get("region"):
                return "The radio is in that region already.", None
            if not confirm:
                return "Needs confirming (a region switch changes the channels the radio is on).", None
            return None, {"region": r}
        if verb == "fm":
            if not dev.get("supports_radio"):
                return "This radio has no FM broadcast receiver.", None
            if set(v) == {"on"} and isinstance(v["on"], bool):
                return None, {"on": v["on"]}
            if set(v) == {"freq"}:
                f = v["freq"]
                if isinstance(f, bool) or not isinstance(f, (int, float)) or not FM_MHZ[0] <= f <= FM_MHZ[1]:
                    return f"FM: a frequency {FM_MHZ[0]:g} to {FM_MHZ[1]:g} MHz.", None
                return None, {"freq": round(float(f), 2)}
            if set(v) == {"seek"} and v["seek"] in ("up", "down"):
                return None, {"seek": v["seek"]}
            return "FM: on (true/false), freq (MHz) or seek (up/down).", None
        if verb == "aprs_path":
            path = v.get("path")
            if not isinstance(path, str):
                return "APRS path: text, e.g. WIDE1-1,WIDE2-1.", None
            path = path.strip().upper().replace(" ", "")
            hops = [h for h in path.split(",")] if path else []
            if len(hops) > APRS_PATH_MAX or not all(DIGI.match(h) for h in hops):
                return (f"APRS path {path!r}: up to {APRS_PATH_MAX} digipeaters, comma separated, "
                        "each a call or alias with an optional -SSID (WIDE1-1,WIDE2-1)."), None
            if path == (ex.get("aprs_path") or ""):
                return "No change.", None
            if not confirm:
                return "Needs confirming (the APRS path changes how far the radio's packets go).", None
            return None, {"path": path}
        if verb == "radio_power":
            on = v.get("on")
            if not isinstance(on, bool):
                return "Power: on is true or false.", None
            if not dev.get("supports_software_power_control"):
                return "This radio has no power control over Bluetooth.", None
            if not on and not confirm:
                return "Needs confirming (powered off, the radio stops receiving).", None
            return None, {"on": on}
        return f"Unknown control {verb!r}.", None

    async def control(self, ws, verb, m):
        got = await self._prepare(ws, lambda: self.check_control(verb, m))
        if got is not None:
            await self._queue(ws, BRIDGE_CMD.get(verb, verb), got[0], None, CONTROL_WHAT[verb],
                              quiet=verb in ("volume", "fm"))

    async def _busy(self):
        """Why the radio must not be changed now, or None."""
        if (self.status or {}).get("is_in_tx"):
            on = self_keys_on(self.settings)
            if on:
                return (f"The UV-PRO is transmitting: {' and '.join(on)} is on, so it keys by itself. "
                        "Turn it off on the radio or under Settings, then try again.")
            return "The UV-PRO is transmitting; try again when it is done."
        if not self.packet_status:
            return None
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as s:
                async with s.get(self.packet_status) as r:
                    st = await r.json()
        except Exception as e:   # hub not running: no link can be up through it
            log.info("packet hub status not read (%s); no link assumed", e)
            return None
        up = [l for l in st.get("links", []) if l.get("state") not in (None, "disconnected")]
        if up:
            return (f"A packet link is up on the UV-PRO ({up[0].get('remote')}); "
                    "changing the radio now would break it.")
        return None

    async def write(self, ws, kind, m):
        got = await self._prepare(ws, lambda: self.check(kind, m.get("values"), m.get("id"), bool(m.get("confirm"))))
        if got is not None:
            await self._queue(ws, kind, got[0], None, {"settings": "settings", "beacon": "beacon settings"}[kind])

    async def _expire(self, ref, fut):
        """The radio never answered: say so, rather than leave the page waiting.
        The future resolves as failed, so a done arriving later finds no
        record and is ignored."""
        try:
            await asyncio.wait_for(asyncio.shield(fut), WRITE_TIMEOUT_S)
        except TimeoutError:
            rec = self._writes.pop(ref, None)
            if rec is None:
                return
            failed = {"ok": False, "error": "no answer from the radio"}
            if not fut.done():
                fut.set_result(failed)
            if rec[3] == "silent":
                log.warning("UV-PRO: no answer from the radio for %s", rec[1])
            elif rec[3] not in (None, "quiet"):
                self._batch_done(rec[3], rec[1], failed)
            else:
                self.notice(f"UV-PRO: no answer from the radio for {rec[1]}; it may not be saved.", "warn", rec[0])

    def can_stop(self):
        if (self._qsy is not None and not self._gave_up) or (self._restore_task and not self._restore_task.done()):
            return ("The UV-PRO is tuned for a Winlink packet session and not put back on its channel yet; "
                    "switch it off once it is.")
        return None

    # --- Winlink packet QSY: the radio on a gateway's frequency for a session, and back

    def _qsy_out(self):
        q = self._qsy
        if q is None:
            return None
        return {"hz": q["hz"], "target": q["target"], "on": _where(q["target"]), "stage": q["stage"],
                "back": _where(q["prev"].get("channel_a", 0)), "written": bool(q["written"])}

    def _set_stage(self, stage):
        self._qsy["stage"] = stage
        self.emit({"qsy": self._qsy_out()})

    async def qsy(self, hz):
        """Put the radio on HZ for a Winlink packet session (winlink.TunedLane).
        Returns QsyResult; on refusal nothing is left changed (or, if a write
        may have landed, the recovery record is kept and restore() retried)."""
        if self.hub:
            return QsyResult(False, "the add-on borrows the radio from the hub, which drops the packet link; "
                                    "set \"control\" to tune for Winlink")
        async with self._lock:
            try:
                return await self._qsy_locked(int(hz))
            except asyncio.CancelledError:
                if self._qsy is not None:
                    self._schedule_retry()
                raise
            except Exception as e:      # noqa: BLE001 - never leave the connect half-done
                log.exception("UV-PRO: tuning for Winlink")
                if self._qsy is not None:
                    await self._restore_locked()
                return QsyResult(False, f"{type(e).__name__}: {e}")

    def _qsy_pick(self, hz):
        """(why, target, how, written, edits): the channel to select, and the
        memory write that goes first (None), with what it overwrites."""
        s = self.settings
        ch_a = s.get("channel_a")
        # 1. A channel already on the frequency: FM simplex, TX on. The one
        # selected now first (no change at all), then the lowest; one without an
        # RX tone ahead of one with (a tone squelch would mute the gateway).
        found = [c for c in self.channels
                 if abs(c["rx_freq"] * 1e6 - hz) < 1 and not c.get("tx_disable")
                 and abs(c["tx_freq"] - c["rx_freq"]) < 5e-7
                 and c.get("rx_mod") == "FM" and c.get("tx_mod") == "FM"]
        if found:
            c = min(found, key=lambda c: (bool(c.get("rx_sub_audio")), c["id"] != ch_a, c["id"]))
            return None, c["id"], f"channel {c['id'] + 1}", None, None
        fmt = chirp.format_freq(hz)
        exact = (self.exact or {}).get("channels") or []
        # 2. VFO A, on a radio that has one (the UV-50PRO does not: its VFO records read 0).
        vfo = next((v for v in self.vfos if v["id"] == 251), None)
        if ((self.device or {}).get("supports_vfo") and vfo is not None and 251 in exact
                and vfo.get("rx_freq", 0) > 0 and not (vfo.get("name") or "").strip()
                and vfo.get("rx_mod") == "FM" and vfo.get("tx_mod") == "FM"):
            mem = chirp.to_memory(vfo)
            old = {k: mem[k] for k in ("Frequency", "Duplex", "Offset")}
            return None, 251, "VFO A", (251, old), {"Frequency": fmt, "Duplex": "", "Offset": "0.000000"}
        # 3. The scratch channel the host file names.
        mhz = f"{hz / 1e6:.3f}"
        if self.qsy_channel is None:
            return f"no FM simplex channel on {mhz} MHz and no qsy_channel set", None, None, None, None
        cid = self.qsy_channel - 1
        ch = next((c for c in self.channels if c["id"] == cid), None)
        why = (f"the radio has no channel {self.qsy_channel}" if ch is None
               else "it is not read back byte for byte" if self.exact is not None and cid not in exact
               else "the radio's Channel edit lock is on" if s.get("ch_data_lock") else None)
        if why:
            return (f"no FM simplex channel on {mhz} MHz, and qsy_channel {self.qsy_channel} can't be used: {why}",
                    None, None, None, None)
        mem = chirp.to_memory(ch)
        names = {f["name"] for f in MEMORY_FIELDS} - {"Location", "talk_around", "mute"}
        old = {k: mem[k] for k in names if k in mem}
        old.update(mem["extra"])          # talk_around, mute
        edits = {"Frequency": fmt, "Duplex": "", "Offset": "0.000000", "Tone": "", "Mode": "FM", "Name": QSY_NAME}
        return None, cid, f"channel {cid + 1} written", (cid, old), edits

    async def _qsy_locked(self, hz):
        if self._closing or not self._bridge_up():
            return QsyResult(False, "the UV-PRO is not connected")
        if self.settings is None or not self.channels:
            return QsyResult(False, "the radio's channels and settings are not read yet")
        why = await self._busy()
        if why:
            return QsyResult(False, why)
        if self._qsy is not None:
            # The last session's radio was never put back: that first, or nothing.
            ok, why = await self._restore_locked()
            if not ok:
                return QsyResult(False, f"the radio is still tuned from the last session and could not be put "
                                        f"back: {why}")
        why, target, how, written, edits = self._qsy_pick(hz)
        if why:
            return QsyResult(False, why)
        prev = {k: self.settings[k] for k in ("channel_a", "double_channel", "scan") if k in self.settings}
        back = _where(prev.get("channel_a", 0))
        # The record before the first write: whatever happens next, restore() knows the way back.
        self._qsy = {"prev": prev, "written": written, "target": target, "hz": hz, "stage": "picked"}
        self._gave_up = False
        self.emit({"qsy": self._qsy_out()})
        log.info("UV-PRO: tuning for Winlink: %.4f MHz on %s (back to %s after)", hz / 1e6, how, back)
        if written:
            cid = written[0]
            why, values, _ = self.check_memory(cid, edits, confirm=True)
            if why and why != "No change.":
                return await self._qsy_failed(f"{_where(cid)} not written: {why}")
            if values:
                res = await self._write_wait("channel", values, cid, f"{_where(cid)} (for Winlink)")
                if not res.get("ok"):
                    # The bridge writes, then reads back: a failed answer may still have changed it.
                    self._set_stage("channel_uncertain")
                    return await self._qsy_failed(f"{_where(cid)} not written: {res.get('error')}")
        why, values = self.check("settings", {"channel_a": target, "double_channel": 0, "scan": False})
        if why and why != "No change.":
            return await self._qsy_failed(f"{_where(target)} not selected: {why}")
        if values:
            res = await self._write_wait("settings", values, None, f"settings (select {_where(target)} for Winlink)")
            if not res.get("ok"):
                self._set_stage("select_failed")
                return await self._qsy_failed(f"{_where(target)} not selected: {res.get('error')}")
        self._set_stage("tuned")
        return QsyResult(True, None, how, back)

    async def _qsy_failed(self, why):
        """A tune that went wrong part way: put back what may have changed, then refuse."""
        ok, rwhy = await self._restore_locked()
        log.warning("UV-PRO: not tuned for Winlink: %s%s", why, "" if ok else f"; and not put back: {rwhy}")
        return QsyResult(False, why if ok else f"{why}; and not put back: {rwhy}")

    async def restore(self, closing=None):
        """Put the radio back as it was before qsy(): (done, why not)."""
        async with self._lock:
            return await self._restore_locked(closing)

    async def _restore_locked(self, closing=None):
        rec = self._qsy
        if rec is None:
            return True, None
        if self._closing:
            return False, "the UV-PRO add-on is stopping"
        # Not while a packet link is still up on the radio (Pat slow to let go)
        # or it transmits, and not to a bridge that isn't there.
        t0 = time.monotonic()
        while True:
            why = "the UV-PRO is not connected" if not self._bridge_up() else await self._busy()
            if why is None:
                break
            limit = self.restore_closing_s if closing and closing() else self.restore_wait_s
            if time.monotonic() - t0 >= limit or self._closing:
                self._schedule_retry()
                return False, why
            await asyncio.sleep(QSY_POLL_S)
        self._set_stage("restoring")
        why, values = self.check("settings", rec["prev"])
        if why and why != "No change.":
            self._schedule_retry()
            return False, f"settings not put back: {why}"
        if values:
            res = await self._write_wait("settings", values, None, f"settings (back to {_where(rec['prev'].get('channel_a', 0))})")
            if not res.get("ok"):
                self._schedule_retry()
                return False, f"settings not put back: {res.get('error')}"
        if rec["written"]:
            cid, old = rec["written"]
            why, values, _ = self.check_memory(cid, old, confirm=True)
            if why and why != "No change.":
                self._schedule_retry()
                return False, f"{_where(cid)} not written back: {why}"
            if values:
                res = await self._write_wait("channel", values, cid, f"{_where(cid)} (written back)")
                if not res.get("ok"):
                    self._schedule_retry()
                    return False, f"{_where(cid)} not written back: {res.get('error')}"
        self._qsy = None
        self.emit({"qsy": None})
        log.info("UV-PRO: back from Winlink (on %s)", _where(rec["prev"].get("channel_a", 0)))
        t = self._restore_task
        if t and not t.done() and t is not asyncio.current_task():
            t.cancel()
        return True, None

    def _schedule_retry(self):
        if self._closing or (self._restore_task and not self._restore_task.done()):
            return
        self._restore_task = asyncio.create_task(self._retry_restore())

    async def _retry_restore(self):
        """A restore that failed, tried again until it works or QSY_RETRY_MAX_S passes."""
        t0 = time.monotonic()
        back = _where((self._qsy or {}).get("prev", {}).get("channel_a", 0))
        while self._qsy is not None:
            await asyncio.sleep(QSY_RETRY_EVERY_S)
            if self._qsy is None:
                return
            ok, why = await self.restore()
            if ok:
                self.notice(f"UV-PRO: back on {back} after Winlink.", "info", None)
                return
            if time.monotonic() - t0 >= QSY_RETRY_MAX_S:
                self._gave_up = True
                log.warning("UV-PRO: gave up putting the radio back on %s after %gs: %s", back, QSY_RETRY_MAX_S, why)
                self.notice(f"UV-PRO: gave up putting the radio back on {back} after Winlink ({why}). "
                            "Put it back by hand.", "warn", None)
                return


ADDON = UvproAddon
