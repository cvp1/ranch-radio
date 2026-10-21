#!/usr/bin/env python3
"""Radio Remote — serve a browser control panel for a transceiver on the LAN.

    python3 server.py --sim                            # try it without a radio
    python3 server.py --port /dev/ttyUSB0              # IC-7300 over USB (default rig)
    python3 server.py --rig g90 --port /dev/ttyUSB0    # Xiegu G90
    python3 server.py --rig qmx --port /dev/ttyACM0     # QRP Labs QMX / QMX+
    python3 server.py --rig hamlib --hamlib-model 1035 --port /dev/ttyUSB0   # any Hamlib rig
    python3 server.py --list-rigs
    python3 server.py ... --mesh-port /dev/meshtastic  # add a Meshtastic node's messages
    python3 server.py --https                          # needed for the browser mic off-localhost

Transmit safety: the transmitter is only keyed while the client holding PTT
keeps sending keep-alives. It unkeys if that client disconnects, goes quiet
for 2 s, or the TX time limit is reached.
"""

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import pathlib
import secrets
import socket
import ssl
import struct
import subprocess
import time

from aiohttp import WSCloseCode, WSMsgType, web

import codec_level
import webguard
from tx_audio import TxAudio
import rigctl_server
import rigs
import station_id
from audio import DEFAULT_DEVICE, AudioBridge

HERE = pathlib.Path(__file__).resolve().parent
log = logging.getLogger("server")

LEVEL_KEYS = {"af", "rf", "sql", "power"}   # every CAT level any rig profile can list

PTT_KEEPALIVE_S = 2.0

# Amateur bands (IARU Region 2 / US) for auto-tune. Off-band = never tune.
HAM_BANDS = [("160 m", 1.8e6, 2.0e6), ("80 m", 3.5e6, 4.0e6), ("60 m", 5.25e6, 5.45e6),
             ("40 m", 7.0e6, 7.3e6), ("30 m", 10.1e6, 10.15e6), ("20 m", 14.0e6, 14.35e6),
             ("17 m", 18.068e6, 18.168e6), ("15 m", 21.0e6, 21.45e6), ("12 m", 24.89e6, 24.99e6),
             ("10 m", 28.0e6, 29.7e6), ("6 m", 50.0e6, 54.0e6)]
AUTO_TUNE_SETTLE_S = 2.0     # wait for the dial to stop before tuning
AUTO_TUNE_RETRY_S = 2.0      # someone is transmitting: try again shortly
AUTO_TUNE_GIVE_UP_S = 60.0
AUTO_TUNE_ARM_S = 3.0        # after connecting, readings only set the band
HOLD_SETTLE_S = 1.5          # let a front-panel change finish before undoing it
HOLD_MAX_RESTORES = 3        # per HOLD_WINDOW_S; then stop fighting and warn
HOLD_WINDOW_S = 30.0
TUNE_START_GRACE_S = 5.0     # a tune we asked for must show up (tuner=2) by then
TX_OFF_CONFIRM_S = 1.0       # "not transmitting" must hold this long to mean an unkey at the radio
# Settings that retune or re-route the transmitter: refused while on the air.
PENDING_FREQ_MAX_S = 30.0    # a queued change waits this long for a tune to end
RETUNE_KEYS = {"freq", "freq_b", "mode", "data", "filter", "vfo", "split", "tuner"}


def band_of(hz):
    return next((name for name, lo, hi in HAM_BANDS if lo <= hz <= hi), None)


def codec_levels(text):
    """argparse type for --winlink-codec-db: a number or a per-band table."""
    try:
        return codec_level.parse_levels(text, [name for name, _, _ in HAM_BANDS])
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def winlink_mode(text):
    """argparse type for --winlink-mode: USB, USB+DATA, LSB ..."""
    mode, _, data = text.upper().partition("+")
    if not mode or data not in ("", "DATA"):
        raise argparse.ArgumentTypeError("MODE or MODE+DATA, e.g. USB")
    return mode, data == "DATA"


SCOPE_FRAME = 0x01


class Station(TxAudio):
    """Glue between one radio and any number of browser clients."""

    def __init__(self, radio, audio, args):
        self.radio, self.audio, self.args = radio, audio, args
        self.clients = set()
        self.ptt_owner = None
        self._winlink_arming = None  # the page arming a session (mode, codec) right now
        self.ptt_started = 0.0
        self.ptt_seen = 0.0
        self._unkey_check = None     # task confirming an unkey made at the radio (F8)
        # A tune this server started (rigctl G TUNE, browser, auto-tune): the
        # only tune the watchdog may cut (F7; the operator 2026-10-13: never manual).
        self._tune_ours_at = None
        self._tune_seen = False
        self._last_scope = 0.0
        # Auto-tune: tune the ATU on every band change, and the first time an
        # app uses the band the radio was already on.
        self.auto_tune = bool(getattr(args, "auto_tune", False))
        if self.auto_tune and (args.no_tx or "tuner" not in radio.caps["features"]):
            log.warning("--auto-tune ignored: needs transmit allowed and an ATU")
            self.auto_tune = False
        self._band = None            # band of the last frequency seen
        self._tune_armed_at = None   # no auto-tune until the radio has reported in
        self._tuned_bands = set()
        self._tune_task = None
        self._tune_why = "band change"
        self._first_use_wait = None  # app freq seen before arming: tune once armed (F11)
        # Split emulated for rigctl apps on rigs that can't set VFO B over CAT
        # (G90): the TX frequency is held here and dialled in only while keyed.
        self.emu_split = False
        self.emu_split_tx = None     # Hz
        self.split_restore = None    # RX frequency to return to after unkey
        # An app's frequency change that arrived during an ATU tune: applied
        # when the tune ends (2026-10-14), reported by "f" meanwhile.
        self.pending_freq = None
        self._pending_task = None
        # Hold: while a rigctl app (JTDX) is connected, the radio stays on the
        # frequency/mode/data the apps last set. A change made at the radio
        # (band button, front-panel tune, dial) is undone once it is receiving.
        # The G90's band button recalls its band memory in plain USB, which
        # kills line audio and gives 0 W (linux-station, 2026-10-13).
        self.rigctl_clients = 0
        self.hold = {}
        self._hold_task = None
        self._hold_restores = []     # monotonic times of recent restores
        self._hold_gave_up = 0.0     # monotonic time we stopped fighting
        # Winlink: Pat keys through its own rigctl lane only while a session
        # is armed from a page; the page that armed it must stay open.
        self.winlink_owner = None
        # The dial frequency the armed session's URL asked for (None: no freq=
        # or armed without a session): the only one Pat may set on the lane.
        self.winlink_qsy_hz = None
        self.codec = None                # codec_level.CodecProfile when --winlink-codec-db is set
        self.addons = None               # addons.Addons: --addons and/or --mesh-port (UV-PRO, Mesh, ...)
        self.modules = None              # ranch_radio.pageapi.ModulesService with --host-file
        # Station ID (47 CFR 97.119): which of ardopcf's IDs went on air, and
        # whether one is owed. Tracked only with --winlink-ardop-log.
        self.id_ledger = station_id.IdLedger(bool(getattr(args, "winlink_ardop_log", None)),
                                             on_change=self._id_changed)
        self._lane_refused_warned = 0.0  # monotonic time of the last refusal WARNING
        radio.on_state(self._on_state)
        radio.on_scope(self._on_scope)
        if hasattr(radio, "on_caps"):   # Hamlib learns capabilities after connecting
            radio.on_caps(lambda caps: self._broadcast(json.dumps({"t": "config", "config": self.config()})))

    @property
    def winlink(self):
        """winlink.PatEngine while the Winlink add-on (addons/winlink.py) is
        switched on, else None: off reads exactly like "no Pat here", so every
        lane and safety path below is unchanged."""
        return self.addons.engine("winlink") if self.addons else None

    def config(self):
        return {"name": self.radio.name, "tx_allowed": not self.args.no_tx,
                "auto_tune": self.auto_tune,
                # The Winlink panel: only where this server runs Pat. A lane
                # without Pat here is driven by the station page's Pat.
                "winlink": bool(self.addons and self.addons.get("winlink")),
                "mesh": bool(self.addons and self.addons.get("mesh")),
                "addons": self.addons.describe() if self.addons else [],
                "modules": self.modules is not None,
                "tx_timeout": self.args.tx_timeout, "sim": self.args.sim,
                "audio": self.audio is not None, "caps": rigs.caps_for_client(self.radio.caps),
                "codec": ({"uses": [u for u in codec_level.USES if self.codec.has(u)],
                           "min_db": self.codec.min_db, "max_db": self.codec.max_db,
                           "save": self.codec.saver is not None}
                          if self.codec is not None else None)}

    # radio -> clients

    def _broadcast(self, msg):
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.ensure_future(self._send(ws, msg))

    async def _send(self, ws, msg):
        try:
            if isinstance(msg, bytes):
                await ws.send_bytes(msg)
            else:
                await ws.send_str(msg)
        except (ConnectionResetError, RuntimeError):
            pass

    def _on_state(self, diff):
        if ("tx" in diff and not diff["tx"] and self.ptt_owner is not None
                and time.monotonic() - self.ptt_started > 1.0
                and not (self._unkey_check and not self._unkey_check.done())):
            # Maybe unkeyed at the radio (e.g. front panel). One "not
            # transmitting" reply is not proof: a stale poll reply looks the
            # same (F8). Confirm it holds before letting go of the key.
            self._unkey_check = asyncio.ensure_future(self._confirm_unkeyed(self.ptt_owner))
        if diff.get("connected"):
            self._tune_armed_at = time.monotonic() + AUTO_TUNE_ARM_S
            self._band = band_of(self.radio.state["freq"])
        elif diff.get("connected") is False:
            self._tune_armed_at = None
        if "freq" in diff:
            self._band_watch(diff["freq"])
        if diff.keys() & {"freq", "mode", "data", "split", "tx", "tuner"}:
            self._check_hold()
        self._broadcast(json.dumps({"t": "state", "d": diff}))

    async def _confirm_unkeyed(self, owner):
        """Release `owner`'s key only if the radio keeps reporting 'not
        transmitting' for TX_OFF_CONFIRM_S. Nothing is written to the radio:
        it already stopped, and an OFF now could cut a new manual TX."""
        try:
            end = time.monotonic() + TX_OFF_CONFIRM_S
            while time.monotonic() < end:
                await asyncio.sleep(0.05)
                if self.radio.state.get("tx") or self.ptt_owner is not owner:
                    return            # stale reply, or released some other way
            log.info("unkeyed at the radio; releasing PTT")
            await self.unkey(write=False)
        except asyncio.CancelledError:
            pass

    # hold: keep the radio where the connected apps put it

    def app_set(self, key, value, hold=True):
        """Record a frequency/mode/data change made by an app (rigctl or browser).
        hold=False (Pat's Winlink lane): never moves or seeds the hold."""
        if key not in ("freq", "mode", "data"):
            return
        if not hold:
            if key == "freq":
                self._tune_on_app_freq(value)
            return
        if self.rigctl_clients and not self.hold:
            # First app setting: seed the rest from the radio. A data-sticky
            # rig (G90) only has line audio in DATA, so an app needs it on.
            s, caps = self.radio.state, self.radio.caps
            self.hold = {"freq": s["freq"], "mode": s["mode"], "data": bool(s.get("data"))}
            if caps.get("data_sticky") and s["mode"] in caps.get("data_modes", []):
                self.hold["data"] = True
            if "vfo_b" not in caps["features"] and "split" in caps["features"]:
                # G90: the radio's own split is never wanted (no CAT to VFO B).
                self.hold["split"] = False
        if self.rigctl_clients:
            self.hold[key] = value
        if key == "freq":
            self._tune_on_app_freq(value)

    def rigctl_connected(self, on):
        self.rigctl_clients += 1 if on else -1
        if self.rigctl_clients <= 0:
            self.rigctl_clients = 0
            self.hold = {}
            # F9: an emulated split belongs to the apps that set it. A later
            # client that keys without sending I must not hop to a stale
            # TX frequency (JTDX re-sends S 1 and I when it reconnects).
            if self.emu_split or self.emu_split_tx is not None:
                log.info("split: last rigctl client left; emulated split cleared")
            self.emu_split, self.emu_split_tx = False, None
            self._hold_restores, self._hold_gave_up = [], 0.0
            if self._hold_task and not self._hold_task.done():
                self._hold_task.cancel()

    def _hold_busy(self):
        s = self.radio.state
        # A Winlink session owns the radio while armed: no app's hold may
        # move it between ARDOP frames.
        return (s.get("tx") or self.ptt_owner is not None or self.split_restore is not None
                or s.get("tuner") == 2 or not s.get("connected") or self.winlink_armed)

    def _hold_drift(self):
        s = self.radio.state
        return {k: v for k, v in self.hold.items() if s.get(k) != v}

    def _check_hold(self):
        if not self.rigctl_clients or not self.hold or self._hold_busy():
            return
        if time.monotonic() - self._hold_gave_up < HOLD_WINDOW_S:
            return
        if self._hold_drift() and not (self._hold_task and not self._hold_task.done()):
            self._hold_task = asyncio.ensure_future(self._restore_hold())

    async def _restore_hold(self):
        try:
            await asyncio.sleep(HOLD_SETTLE_S)
            while self._hold_busy():                  # e.g. front-panel tune still running
                await asyncio.sleep(0.5)
            drift = self._hold_drift()
            if not drift or not self.rigctl_clients:
                return
            now = time.monotonic()
            if now - self._hold_gave_up < HOLD_WINDOW_S:
                return
            self._hold_restores = [t for t in self._hold_restores if now - t < HOLD_WINDOW_S]
            if len(self._hold_restores) >= HOLD_MAX_RESTORES:
                self._hold_gave_up = now
                log.warning("hold: radio keeps changing (%s); not restoring again for now", drift)
                self.notice("The radio keeps moving away from the app's settings; "
                            "leaving it alone. Check the radio.", "warn")
                return
            self._hold_restores.append(now)
            s = self.radio.state
            was = {k: s.get(k) for k in drift}
            log.info("hold: radio changed at the radio %s; restoring %s", was, drift)
            self.notice("Radio changed at the front panel; restored "
                        + ", ".join(f"{k} {v}" for k, v in drift.items()), "warn")
            # Split off first. Mode before DATA: on the G90 a mode set drops
            # DATA. Each set re-checks that nothing keyed in the meantime.
            steps = []
            if "split" in drift:
                steps.append(("split", False))
            if "mode" in drift:
                steps.append(("mode", drift["mode"]))
            if "data" in drift or ("mode" in drift and self.hold.get("data")):
                steps.append(("data", self.hold["data"]))
            if "freq" in drift:
                steps.append(("freq", drift["freq"]))
            for k, v in steps:
                if self._hold_busy():
                    log.info("hold: transmitter busy; restore of %s deferred", k)
                    return
                await self.radio.set(k, v)
        except asyncio.CancelledError:
            pass

    def _tune_on_app_freq(self, hz):
        """An app put the radio on a band not tuned yet this run: tune it.
        Covers a band reached while the server was off (no change to watch)."""
        band = band_of(hz)
        armed = self._tune_armed_at is not None and time.monotonic() >= self._tune_armed_at
        if not armed and self.auto_tune and band is not None:
            # F11: JTDX reconnects ~0.4 s after a restart, while the radio is
            # still reporting in. Look again once arming is over.
            if not (self._first_use_wait and not self._first_use_wait.done()):
                self._first_use_wait = asyncio.ensure_future(self._first_use_after_arming())
            return
        if (not self.auto_tune or not armed or band is None or band in self._tuned_bands
                or (self._tune_task and not self._tune_task.done())):
            return
        self._schedule_tune(band, "first use this run")

    async def _first_use_after_arming(self):
        """Wait out the arming window, then treat the radio's frequency (where
        the app put it) as the app's first use of that band."""
        try:
            end = time.monotonic() + AUTO_TUNE_ARM_S + 10
            while time.monotonic() < end:
                await asyncio.sleep(0.1)
                if self._tune_armed_at is not None and time.monotonic() >= self._tune_armed_at:
                    if self.rigctl_clients or self.hold:
                        self._tune_on_app_freq(self.radio.state["freq"])
                    return
        except asyncio.CancelledError:
            pass

    # auto-tune on band change

    def _band_watch(self, hz):
        band = band_of(hz)
        if band == self._band:
            if self._tune_task and not self._tune_task.done():
                self._tune_task.cancel()          # still moving: restart the settle
                self._schedule_tune(band, self._tune_why)
            return
        settling = self._tune_armed_at is None or time.monotonic() < self._tune_armed_at
        self._band = band
        if self._tune_task and not self._tune_task.done():
            self._tune_task.cancel()
            self._tune_task = None
        # Tune on every band change (2026-10-13: going back to a band
        # must retune too). Never while the radio is just reporting in, or
        # off-band.
        if not self.auto_tune or settling or band is None:
            return
        self._schedule_tune(band, "band change")

    def _schedule_tune(self, band, why):
        self._tune_why = why
        self._tune_task = asyncio.ensure_future(self._auto_tune(band, why))

    async def _auto_tune(self, band, why="band change"):
        try:
            await asyncio.sleep(AUTO_TUNE_SETTLE_S)
            deadline = time.monotonic() + AUTO_TUNE_GIVE_UP_S
            while self.ptt_owner is not None or self.radio.state.get("tx"):
                if time.monotonic() > deadline:
                    log.warning("auto-tune for %s skipped: transmitter busy", band)
                    return
                await asyncio.sleep(AUTO_TUNE_RETRY_S)
            if band_of(self.radio.state["freq"]) != band:
                return
            if why == "first use this run" and band in self._tuned_bands:
                return
            if self.retune_blocked() or not self.radio.state.get("connected"):
                return
            self._tuned_bands.add(band)
            log.info("auto-tune: ATU tuning for %s at %.3f MHz", band, self.radio.state["freq"] / 1e6)
            self.notice(f"ATU tuning for {band} ({why})", "info")
            await self.start_tune()
        except asyncio.CancelledError:
            pass

    def _on_scope(self, start, end, data):
        now = time.monotonic()
        if now - self._last_scope < 1 / 30:   # cap at 30 fps
            return
        self._last_scope = now
        self._broadcast(struct.pack("<Bdd", SCOPE_FRAME, start, end) + data)

    def notice(self, msg, level="info", ws=None):
        payload = json.dumps({"t": "notice", "level": level, "msg": msg})
        if ws is not None:
            asyncio.ensure_future(self._send(ws, payload))
        else:
            self._broadcast(payload)

    # Winlink session arming

    band_of = staticmethod(band_of)

    @property
    def winlink_armed(self):
        return self.winlink_owner is not None

    def _winlink_keying(self):
        return getattr(self.ptt_owner, "lane", None) == "winlink"

    def _id_changed(self, snap):
        self._broadcast(json.dumps({"t": "winlink", "id": snap}))

    LANE_WARN_EVERY_S = 10.0             # Hamlib retries a refused PTT; one WARNING per burst

    def winlink_lane_refused(self, peer):
        """Pat asked to key with no session armed. Between sessions ardopcf
        keys only to send its station ID (its end-of-session or 10-minute
        ID), so this is an ID that does not go on air."""
        self.id_ledger.ptt(False)
        now = time.monotonic()
        if now - self._lane_refused_warned < self.LANE_WARN_EVERY_S:
            log.info("rigctl %s: PTT refused: no Winlink session is armed", peer)
            return
        self._lane_refused_warned = now
        owed = self.id_ledger.owed()
        log.warning("rigctl %s: PTT refused: no Winlink session is armed. Between sessions "
                    "ardopcf keys only to send its station ID (end-of-session or 10-minute "
                    "ID): this station ID is NOT going on air%s", peer,
                    "; an ID is owed for the last session" if owed
                    else "" if owed is False else " (ID not tracked: no --winlink-ardop-log)")

    async def winlink_arm(self, ws, on, freq_hz=None):
        """freq_hz: the session's target frequency (connect URL), which sets
        the codec's band; without one, the radio's current frequency."""
        if on:
            if self.args.no_tx:
                self.notice("Transmit is disabled on this server (--no-tx); Winlink can't connect.",
                            "warn", ws)
                return
            if not getattr(self.args, "winlink_rigctl_port", 0):
                self.notice("Winlink is not enabled on this server.", "warn", ws)
                return
            # Arming sets the mode and the codec's drive: never under someone
            # else's transmission (bug bash 2026-10-17, Sol #5). can_key
            # refuses new keys until arming is done.
            why = self._winlink_arm_blocked()
            if why:
                self.notice(f"Winlink not armed: {why}", "warn", ws)
                return
            self._winlink_arming = ws
            try:
                await self._winlink_arm_prepare(ws, freq_hz)
            finally:
                self._winlink_arming = None
        else:
            await self.winlink_disarm()

    def _winlink_arm_blocked(self):
        s = self.radio.state
        if self.ptt_owner is not None and not self._winlink_keying():
            return "another operator is transmitting."
        if s.get("tx") and not self._winlink_keying():
            return "the radio is transmitting (keyed at the radio)."
        if s.get("tuner") == 2:
            return "the ATU is tuning; try again in a moment."
        if self._winlink_arming is not None:
            return "a session is already being armed."
        return None

    async def _winlink_arm_prepare(self, ws, freq_hz):
        if self.codec is not None:
            hz = freq_hz if freq_hz is not None else self.radio.state.get("freq")
            try:
                await self.codec.enter(band_of(hz) if hz else None)
            except codec_level.CodecError as e:
                log.warning("winlink: codec level not set: %s", e)
                self.notice(f"Could not set the codec's audio level for Winlink ({e}); "
                            "not connecting.", "warn", ws)
                return
        if not await self._winlink_mode_enter(ws):
            if self.codec is not None:
                await self.codec.leave()
            return
        self.winlink_owner = ws
        log.info("winlink: session armed")
        self._codec_broadcast("winlink")
        self._broadcast(json.dumps({"t": "winlink", "armed": True,
                                    "codec_db": self.codec and self.codec.current}))

    async def _winlink_end(self, ws, started, ok, reason):
        import winlink
        await winlink.id_tail(self, started, lambda: self.winlink_owner is not ws,
                              lambda d: self._broadcast(json.dumps({"t": "winlink", **d})))
        if self.winlink_owner is ws:
            await self.winlink_disarm(None if ok else (str(reason) if reason else "session failed"))

    # The mode a Winlink session needs (--winlink-mode): on the QMX, ARDOP
    # must go out in USB (SSB); its DiGi mode (USB + DATA, for FT8) sends one
    # carrier at the measured audio tone and gave 0 W for ARDOP (linux-station,
    # 2026-10-16). Set while arming, before anything can key; the previous
    # mode is put back after the session is unkeyed.

    async def _winlink_mode_enter(self, ws):
        """False if the radio could not be made ready (the page is told why)."""
        self._winlink_mode_was = None
        self._winlink_ssb = False
        want = getattr(self.args, "winlink_mode", None)
        if not want:
            return True
        s = self.radio.state
        mode, data = want
        if (s.get("mode"), bool(s.get("data"))) != (mode, data):
            self._winlink_mode_was = (s.get("mode"), bool(s.get("data")))
            log.info("winlink: %s%s for the session (was %s%s)", mode, " DATA" if data else "",
                     s.get("mode"), " DATA" if s.get("data") else "")
            await self.radio.set("mode", mode)
            if "data" in self.radio.caps.get("features", []) or self.radio.caps.get("data_modes"):
                await self.radio.set("data", data)
            self.notice(f"Winlink: radio set to {mode}{' DATA' if data else ''} for the session; "
                        "it goes back afterwards.", "info", ws)
        if not data and hasattr(self.radio, "ssb_from_usb"):
            # QMX: plain USB is its SSB mode, whose audio comes from the mic
            # jack unless set otherwise (0 W from the PC, 2026-10-16).
            try:
                await self.radio.ssb_from_usb(True)
                self._winlink_ssb = True
            except ValueError as e:
                await self._winlink_mode_leave()
                self.notice(f"Winlink not armed: {e}.", "warn", ws)
                return False
        return True

    async def _winlink_mode_leave(self):
        was, self._winlink_mode_was = getattr(self, "_winlink_mode_was", None), None
        ssb, self._winlink_ssb = getattr(self, "_winlink_ssb", False), False
        if self.radio.state.get("tx"):
            return
        if ssb:
            await self.radio.ssb_from_usb(False)
        if not was:
            return
        mode, data = was
        log.info("winlink: back to %s%s", mode, " DATA" if data else "")
        await self.radio.set("mode", mode)
        if "data" in self.radio.caps.get("features", []) or self.radio.caps.get("data_modes"):
            await self.radio.set("data", data)

    async def winlink_disarm(self, reason=None):
        if self.winlink_owner is None:
            return
        self.winlink_owner = None
        self.winlink_qsy_hz = None
        log.info("winlink: disarmed%s", f" ({reason})" if reason else "")
        if self.winlink is not None and reason:
            await self.winlink.abort(reason)   # no-op unless a session is still running
        if self._winlink_keying():
            await self.unkey(f"Winlink disarmed{f' ({reason})' if reason else ''}")
        await self._winlink_mode_leave()         # after unkeying, as the codec level
        if self.codec is not None:               # after unkeying: never change level mid-frame
            err = await self.codec.leave()
            if err:
                self.notice(f"Could not put the codec's audio level back ({err}); "
                            "check it before voice transmit.", "warn")
        self._broadcast(json.dumps({"t": "winlink", "armed": False,
                                    "codec_db": self.codec and self.codec.current,
                                    "id": self.id_ledger.snapshot()}))
        self._codec_broadcast("winlink")
        # The radio stays where the session left it (the operator 2026-10-15): pulling
        # it back to an app's band meant a band change and an ATU tune carrier
        # after every session. The app's next setting seeds the hold again.
        if self.hold:
            log.info("hold: cleared after the Winlink session (was %s)", self.hold)
            self.hold = {}
            if self._hold_task and not self._hold_task.done():
                self._hold_task.cancel()

    # transmit control

    def retune_blocked(self):
        """True while the radio is (or is about to be) on the air: no
        frequency/mode/band change may reach it then."""
        s = self.radio.state
        return bool(s.get("tx") or self.ptt_owner is not None or s.get("tuner") == 2)

    @staticmethod
    def split_hop_ok(rx, tx):
        """F9: an emulated split may only hop within the RX frequency's ham band."""
        band = band_of(tx)
        return band is not None and band == band_of(rx)

    def _radio_split_unsafe(self):
        """The radio's own split is on, on a rig whose VFO B we can't set (G90):
        keying would transmit on whatever stale frequency B holds."""
        caps = self.radio.caps
        return ("vfo_b" not in caps["features"] and "split" in caps["features"]
                and bool(self.radio.state.get("split")))

    async def can_key(self, owner):
        """Why `owner` may not key now (None = it may). Checked BEFORE anything
        moves the dial for a split transmission."""
        s = self.radio.state
        if self.args.no_tx:
            return "Transmit is disabled on this server (--no-tx)."
        if self.ptt_owner not in (None, owner):
            return "Another operator is transmitting."
        if self._winlink_arming is not None:
            return "A Winlink session is being armed (setting mode and level); try again in a moment."
        if s.get("tuner") == 2:
            return "The ATU is tuning; try again in a moment."
        if self.pending_freq is not None:
            return "A frequency change is waiting for the ATU tune to finish."
        if self.ptt_owner is None and s.get("tx"):
            return "The radio is already transmitting (keyed at the radio)."
        if self._radio_split_unsafe():
            log.warning("PTT refused: the radio's own split is on; turning it off")
            await self.radio.set("split", False)
            return "The radio's split was on (it would transmit on VFO B); turned it off. Key again."
        return None

    def tune_only_blocked(self):
        """Blocked only because an ATU tune is running (nobody holds PTT)."""
        return self.radio.state.get("tuner") == 2 and self.ptt_owner is None

    def freq_after_tune(self, hz):
        """Queue an app's frequency change until the running tune ends."""
        self.pending_freq = hz
        log.info("freq %d requested during an ATU tune; applying when it ends", hz)
        if not (self._pending_task and not self._pending_task.done()):
            self._pending_task = asyncio.ensure_future(self._apply_after_tune())

    async def _apply_after_tune(self):
        deadline = time.monotonic() + PENDING_FREQ_MAX_S
        try:
            while self.retune_blocked():
                if time.monotonic() > deadline or not self.radio.state.get("connected"):
                    log.warning("dropped pending freq %s: the tune never finished", self.pending_freq)
                    self.notice("The ATU tune didn't finish; the frequency change was dropped.", "warn")
                    self.pending_freq = None
                    return
                await asyncio.sleep(0.1)
            hz, self.pending_freq = self.pending_freq, None
            if hz is not None:
                log.info("tune finished; moving to %d", hz)
                self.app_set("freq", hz)
                await self.radio.set("freq", hz)
        except asyncio.CancelledError:
            self.pending_freq = None

    async def key(self, ws, on):
        if on:
            if getattr(ws, "ptt_latched", False):
                # F17: this page's PTT was released by the server (time limit,
                # another operator, the radio) while its button was still
                # held. Its keep-alives are the same message as a key-down,
                # so they must not key again: a key-up comes first.
                return
            why = await self.can_key(ws)
            if why:
                self.notice(why, "warn", ws)
                return
            self.ptt_seen = time.monotonic()
            if self.ptt_owner is None:
                self.ptt_owner, self.ptt_started = ws, self.ptt_seen
                await self.radio.set("tx", True)
        else:
            # Anyone may unkey.
            if getattr(ws, "ptt_latched", False):
                ws.ptt_latched = False      # the button was let go: it may key again
            await self.unkey(by=ws)

    async def _confirm(self, ws, *keys):
        """After a refused change: that page gets the radio's real value, which
        it takes even inside its local-edit window. Before, the page kept
        showing the refused value (review 2026-10-18, Sol, Chromium)."""
        d = {k: self.radio.state.get(k) for k in keys}
        await self._send(ws, json.dumps({"t": "state", "d": d, "confirmed": True}))

    async def start_tune(self):
        """Start an ATU tune on behalf of an app. The watchdog cuts it after
        --tune-timeout; a tune started at the radio is never cut."""
        self._tune_ours_at, self._tune_seen = time.monotonic(), False
        await self.radio.set("tuner", 2)

    async def unkey(self, reason=None, write=True, force=False, by=None):
        """Release the key. write=False: the radio already stopped (unkeyed
        at the radio); only release ownership and restore RX. force: write
        OFF even if nobody holds PTT (a tune we started, F7). by: the client
        whose own key-up this is; any other release of a browser's PTT
        latches that browser off until it lets go (F17)."""
        owner = self.ptt_owner
        was = force or owner is not None or self.radio.state["tx"]
        self.ptt_owner = None
        if (owner is not None and owner is not by
                and isinstance(owner, web.WebSocketResponse) and not owner.closed):
            # Only browsers: they repeat the key-down twice a second while the
            # button is held. rigctl apps send one T 1 per transmission.
            owner.ptt_latched = True
            asyncio.ensure_future(self._send(owner, json.dumps({"t": "ptt_released"})))
        if was and write:
            await self.radio.set("tx", False)
            if reason:
                log.warning("unkeyed: %s", reason)
                self.notice(f"Transmitter released: {reason}", "warn")
        if getattr(self, "_voice_since", None) is not None:
            held, self._voice_since = time.monotonic() - self._voice_since, None
            # Diagnosis (mac-shack, 2026-10-18): taps and silent mics both read
            # as "near 0 W"; this says which.
            log.info("voice: page PTT released after %.1f s, %d kB of microphone audio sent "
                     "(%d kB arrived)%s", held, getattr(self, "voice_bytes", 0) // 1024,
                     getattr(self, "voice_rx_bytes", 0) // 1024, self.voice_summary())
        # After the unkey is queued: nothing changes mid-transmission.
        await self._voice_restore()
        if self.split_restore is not None:
            # Emulated split: back to the RX frequency, queued after the unkey
            # so the radio never retunes while transmitting.
            rx, self.split_restore = self.split_restore, None
            log.info("split: back to RX %d", rx)
            await self.radio.set("freq", rx)

    async def _tune_watch(self):
        """F7: a tune we started that runs past --tune-timeout is cut."""
        if self._tune_ours_at is None:
            return
        now, tuning = time.monotonic(), self.radio.state.get("tuner") == 2
        if tuning:
            self._tune_seen = True
            limit = self.args.tune_timeout
            if now - self._tune_ours_at > limit:
                self._tune_ours_at = None
                await self.unkey(f"ATU tune still running after {limit:g}s", force=True)
        elif self._tune_seen or now - self._tune_ours_at > TUNE_START_GRACE_S:
            # Ours ended (or never started): a later tune at the radio isn't ours.
            self._tune_ours_at = None

    async def safety_loop(self):
        while True:
            await asyncio.sleep(0.25)
            await self._tune_watch()
            if self.ptt_owner is None or self.ptt_owner is getattr(self, "_voice_preparing", None):
                continue                      # preparing: not keyed; bounded by VOICE_PREP_S
            now = time.monotonic()
            keepalive = getattr(self.ptt_owner, "keepalive_s", PTT_KEEPALIVE_S)
            if now - self.ptt_seen > keepalive:
                await self.unkey(getattr(self.ptt_owner, "lost_msg",
                                         "lost contact with the operator's browser"))
            elif now - self.ptt_started > self.args.tx_timeout:
                await self.unkey(f"{self.args.tx_timeout}s transmit time limit")

    # client handling

    async def handle(self, ws):
        self.clients.add(ws)
        await ws.send_str(json.dumps({"t": "hello", "state": self.radio.state,
                                      "config": self.config(),
                                      "winlink": (self.addons.own_snapshot("winlink")
                                                  if self.addons and self.addons.get("winlink")
                                                  else {"armed": self.winlink_armed,
                                                        "codec_db": self.codec and self.codec.current,
                                                        "id": self.id_ledger.snapshot()}),
                                      "mesh": self.addons.own_snapshot("mesh") if self.addons else None,
                                      "addons": self.addons.snapshot() if self.addons else {}}))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    m = json.loads(msg.data)
                    if self._addon_message(m):
                        # A long add-on job (an OpenGD77 codeplug read/write) must
                        # not stop this loop: aiohttp reads the heartbeat's pong
                        # here, and closed the socket after ~7.5 s (review
                        # 2026-10-18, Sol). One chain per page keeps their order.
                        self._chain(ws, m)
                    else:
                        await self._command(ws, m)
                except (ValueError, KeyError, TypeError) as e:
                    self.notice(f"Rejected: {e}", "warn", ws)
        finally:
            self.clients.discard(ws)
            if self.ptt_owner is ws:
                await self.unkey("operator disconnected")
            if self.winlink_owner is ws:
                await self.winlink_disarm("operator's page closed")

    def _addon_message(self, m):
        t = m.get("t") if isinstance(m, dict) else None
        return bool(self.addons) and (t == "addon" or bool(t and self.addons.route(t)))

    def _chain(self, ws, m):
        """Run an add-on command after this page's previous one, off the receive loop."""
        prev = getattr(ws, "addon_chain", None)

        async def run():
            if prev is not None:
                try:
                    await prev
                except Exception:              # noqa: BLE001 (logged where it happened)
                    pass
            try:
                await self._command(ws, m)
            except (ValueError, KeyError, TypeError) as e:
                self.notice(f"Rejected: {e}", "warn", ws)
            except Exception:                  # noqa: BLE001
                log.exception("add-on command %s failed", m.get("t"))
                self.notice("The add-on command failed; see the server log.", "warn", ws)
        ws.addon_chain = asyncio.ensure_future(run())

    async def _command(self, ws, m):
        t = m["t"]
        if t == "set":
            k = m["k"]
            v = self.radio.validate(k, m["v"])     # every check below sees the real value
            if k == "tx":
                # mic: the page sends audio (absent from older pages: assume so)
                await self.voice_key(ws, v, mic=m.get("mic", True) is not False)
            elif k in RETUNE_KEYS and self.retune_blocked():
                log.warning("refused browser %s=%r: transmitting", k, v)
                self.notice(f"Not while transmitting: {k} unchanged.", "warn", ws)
                await self._confirm(ws, k)
            elif k == "tuner" and v == 2 and self.args.no_tx:
                self.notice("Tuning transmits; disabled by --no-tx.", "warn", ws)
                await self._confirm(ws, k)
            elif k == "split" and v and "vfo_b" not in self.radio.caps["features"]:
                # No CAT access to VFO B (G90): the radio's own split would
                # transmit on whatever stale frequency B holds. JTDX/WSJT-X
                # split is emulated in the rigctl listener instead.
                log.warning("refused split on: this rig's VFO B is not reachable over CAT")
                self.notice("Split here would transmit on an unknown VFO B. "
                            "Use the app's Split=Rig; it is emulated safely.", "warn", ws)
                await self._confirm(ws, k)
            elif k in LEVEL_KEYS and k not in self.radio.caps["levels"]:
                log.warning("refused %s set: not a CAT level on this rig", k)
                self.notice(f"{k} is set on the radio itself, not here.", "warn", ws)
                await self._confirm(ws, k)
            elif k == "tuner" and v == 2:
                await self.start_tune()
            else:
                self.app_set(k, v)
                await self.radio.set(k, v)
        elif t == "sim_panel" and self.args.sim:
            # Test hook (simulator only): a change made at the radio's front
            # panel, bypassing every app path.
            self.radio._update(**m["d"])
        elif t == "winlink_arm":
            # From a page (manual arm), or from the station page's Pat
            # (station.py) for a session: freq sets the codec's band, qsy is
            # the one frequency Pat may set on the lane while armed.
            freq, qsy = m.get("freq"), m.get("qsy")
            freq = int(freq) if freq is not None else None
            await self.winlink_arm(ws, bool(m["v"]), freq)
            if m["v"] and self.winlink_owner is ws:
                self.winlink_qsy_hz = int(qsy) if qsy is not None else None
        elif t == "winlink_end":
            # A session run by another process's Pat ended: hold the lane for
            # ardopcf's end-of-session ID here, where PTT and the ledger are,
            # then disarm. Only the session's owner may end it.
            if self.winlink_owner is ws:
                asyncio.ensure_future(self._winlink_end(ws, float(m["started"]), bool(m.get("ok")),
                                                        m.get("reason")))
        elif t in ("winlink_connect", "winlink_stop", "winlink_prompt"):
            if self.addons and self.addons.route(t):
                await self.addons.command(ws, m)          # the Winlink add-on (on, or says it's off)
            elif t == "winlink_connect":
                self.notice("Winlink is not enabled on this server.", "warn", ws)
        elif t.startswith("mesh_") and not (self.addons and self.addons.route(t)):
            self.notice("Mesh is not enabled on this server.", "warn", ws)
        elif self.addons and self.addons.route(t):     # an add-on's own page (Mesh)
            await self.addons.command(ws, m)
        elif t == "addon":
            if self.addons is None:
                self.notice("No add-ons on this server.", "warn", ws)
            else:
                await self.addons.command(ws, m)
        elif t in ("codec_get", "codec_step", "codec_save"):
            await self.codec_command(ws, m)
        elif t in ("modules", "modules_check", "module_plan", "module_apply"):
            if self.modules is None:
                self.notice("Modules: this server doesn't know its host file (--host-file). On a "
                            "station host they are on the station page.", "warn", ws)
            else:
                self.modules.handle(ws, m)
        elif t == "page_log":
            # Diagnosis from a page (the LAN is trusted; capped, one line).
            peer = getattr(getattr(ws, "_req", None), "remote", None) or "page"
            log.info("page %s: %s", peer, str(m.get("msg", ""))[:300].replace("\n", " "))
        elif t == "swap":
            if self.retune_blocked():
                # F18: exchanging A and B moves the transmitter to B's frequency.
                log.warning("refused browser VFO swap: transmitting")
                self.notice("Not while transmitting: VFOs not swapped.", "warn", ws)
                await self._confirm(ws, "freq", "freq_b")
            else:
                await self.radio.swap_vfo()
        elif t == "power":
            await self.radio.power(bool(m["v"]))
        elif t == "ping":
            await ws.send_str(json.dumps({"t": "pong", "ts": m.get("ts")}))


# --- HTTP ------------------------------------------------------------------

def make_app(station, args, rigctl_sock=None, rigctl_adopted=()):
    # A page from elsewhere, open in a browser on the LAN, must not reach the
    # radio (PTT, codec Save, add-ons): unknown Host names and cross-site
    # Origins are refused, as on the station (modules plan M0, 2026-10-21).
    app = web.Application(middlewares=[webguard.cross_site_guard(webguard.allowed_names(args), log)])

    def authorised(request):
        return not args.token or secrets.compare_digest(
            request.query.get("token", ""), args.token)

    async def index(request):
        return web.FileResponse(HERE / "static" / "index.html",
                                headers={"Cache-Control": "no-cache"})

    sockets = set()   # open browser WebSockets, closed first at shutdown (F10)

    async def ws_control(request):
        ws = web.WebSocketResponse(heartbeat=5, max_msg_size=1 << 16)
        await ws.prepare(request)
        if not authorised(request):
            await ws.close(code=4401, message=b"token required")
            return ws
        sockets.add(ws)
        try:
            await station.handle(ws)
        finally:
            sockets.discard(ws)
        return ws

    async def ws_audio(request):
        ws = web.WebSocketResponse(heartbeat=5, max_msg_size=1 << 16)
        await ws.prepare(request)
        if not authorised(request):
            await ws.close(code=4401, message=b"token required")
            return ws
        q = station.audio.subscribe()
        sockets.add(ws)

        async def pump_out():
            while True:
                await ws.send_bytes(await q.get())

        sender = asyncio.create_task(pump_out())
        try:
            async for msg in ws:
                # Microphone audio: only forwarded while this client holds PTT.
                if msg.type == WSMsgType.BINARY:
                    station.voice_rx_bytes = getattr(station, "voice_rx_bytes", 0) + len(msg.data)
                    station.voice_audio(msg.data)
                if (msg.type == WSMsgType.BINARY and station.ptt_owner is not None
                        and station.radio.state["tx"]):
                    station.voice_bytes = getattr(station, "voice_bytes", 0) + len(msg.data)
                    await station.audio.play(msg.data)
        finally:
            sockets.discard(ws)
            sender.cancel()
            station.audio.unsubscribe(q)
        return ws

    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_control)
    if station.addons is not None:
        station.addons.add_routes(app, authorised)      # Winlink's mailbox under /winlink/api/
    if station.audio is not None:
        app.router.add_get("/audio", ws_audio)
    app.router.add_static("/static", HERE / "static")

    @web.middleware
    async def no_cache(request, handler):
        resp = await handler(request)
        if request.path.startswith("/static"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp
    app.middlewares.append(no_cache)

    async def on_startup(app):
        await station.radio.start()
        app["safety"] = asyncio.create_task(station.safety_loop())
        app["rigctl"] = None
        if args.rigctl_port:
            try:
                app["rigctl"] = await rigctl_server.serve(
                    station, args.rigctl_host, args.rigctl_port, sock=rigctl_sock,
                    adopted=rigctl_adopted)
            except OSError as e:   # e.g. a real rigctld already owns the port
                log.warning("rigctl listener not started on %s:%d: %s",
                            args.rigctl_host, args.rigctl_port, e)
        if station.codec is not None:
            await station.codec.refresh()
        if station.addons is not None:
            await station.addons.start()
        app["ardop_log"] = None
        if getattr(args, "winlink_ardop_log", None):
            app["ardop_log"] = station_id.ArdopLogTail(args.winlink_ardop_log, station.id_ledger)
            app["ardop_log"].start()
            log.info("station ID: following ardopcf's log in %s", args.winlink_ardop_log)
        app["winlink_rigctl"] = None
        if args.winlink_rigctl_port:
            # Pat's private lane: this machine only, whatever --rigctl-host says.
            app["winlink_rigctl"] = await rigctl_server.serve(
                station, "127.0.0.1", args.winlink_rigctl_port, lane="winlink")

    async def on_shutdown(app):
        # F10: close every client before the web server waits on them (an open
        # browser WebSocket held a restart ~7 s). Each close releases that
        # client's PTT through Station.unkey; apps see an orderly EOF/close.
        if station.addons is not None:
            await station.addons.stop()       # Winlink: ends a session first, then Pat
        if app.get("rigctl"):
            await rigctl_server.shutdown(app["rigctl"])
        if app.get("winlink_rigctl"):
            await rigctl_server.shutdown(app["winlink_rigctl"])
        closes = [ws.close(code=WSCloseCode.GOING_AWAY, message=b"server restarting")
                  for ws in list(sockets) if not ws.closed]
        if closes:
            await asyncio.wait([asyncio.ensure_future(c) for c in closes], timeout=1.0)

    async def on_cleanup(app):
        if app.get("ardop_log"):
            app["ardop_log"].stop()
        if app.get("rigctl"):
            app["rigctl"].close()
        if app.get("winlink_rigctl"):
            app["winlink_rigctl"].close()
        app["safety"].cancel()
        # Release only a transmission an app or browser started here; the
        # radio driver's stop() also unkeys anything it keyed. A transmission
        # made at the radio (front panel, mic) is never cut (2026-10-13).
        if station.ptt_owner is not None or station.split_restore is not None:
            await station.unkey()
        if station.codec is not None:
            await station.codec.leave()       # after unkeying; no-op unless a use still holds it
        await station.radio.stop()
        if station.audio:
            await station.audio.stop()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.on_cleanup.append(on_cleanup)
    return app


def systemd_rigctl_socket():
    """(listening socket, [(name, connection)]) handed to us by systemd.

    The listening socket is the one g90-remote.socket holds across restarts,
    so an app reconnecting mid-restart waits in the backlog (F10b). The
    connections are rigctl sessions a previous run put in the fd store
    (FDNAME rigctl-*), adopted so the app never sees a disconnect (F10c).
    Returns (None, []) when not started by systemd."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return None, []
    try:
        n = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError:
        return None, []
    names = os.environ.get("LISTEN_FDNAMES", "").split(":")
    for var in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(var, None)          # not for our children
    listen, adopted = None, []
    for i in range(n):
        fd = 3 + i                         # SD_LISTEN_FDS_START
        name = names[i] if i < len(names) else ""
        try:
            sock = socket.socket(fileno=fd)
        except OSError:
            continue
        if sock.type != socket.SOCK_STREAM:
            log.warning("inherited fd %d (%s) is not a stream socket; ignoring it", fd, name)
            sock.detach()
            continue
        sock.setblocking(False)
        try:
            sock.getpeername()             # a connection has a peer; a listener doesn't
            listening = False
        except OSError:
            listening = True
        if listening:
            if listen is None:
                listen = sock
            else:
                sock.close()
        elif name.startswith("rigctl-"):
            adopted.append((name, sock))
        else:
            log.warning("inherited fd %d (%s) not recognised; closing it", fd, name)
            sock.close()
    return listen, adopted


def _host_names():
    names = {"localhost"}
    for n in (socket.gethostname(), socket.getfqdn()):
        base = n.split(".")[0]
        names |= {n, base, f"{base}.local"}
    return sorted(names)


def _is_ip(s):
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _host_ips():
    ips = {"127.0.0.1"}
    try:  # the LAN address: no packet is sent for a UDP connect
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.0.2.1", 9))
            ips.add(sk.getsockname()[0])
    except OSError:
        pass
    return sorted(ips)


def tls_context(certdir, extra_names=()):
    certdir.mkdir(exist_ok=True)
    cert, key = certdir / "cert.pem", certdir / "key.pem"
    if not cert.exists():
        log.info("generating a self-signed certificate in %s", certdir)
        # Browsers ignore the CN; the names clients use must be in subjectAltName.
        # --tls-name takes names or IP addresses; auto-detection finds only the
        # default-route address, which is a VPN's when one is up.
        extra_ips = {n for n in extra_names if _is_ip(n)}
        names = sorted({*_host_names(), *(set(extra_names) - extra_ips)})
        san = ",".join([*(f"DNS:{n}" for n in names),
                        *(f"IP:{i}" for i in sorted({*_host_ips(), *extra_ips}))])
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-days", "3650",
                        "-subj", f"/CN=Radio Remote {socket.gethostname().split('.')[0]}",
                        "-addext", f"subjectAltName={san}",
                        "-addext", "extendedKeyUsage=serverAuth",
                        # Not a CA: trusting it on a client (Brave install,
                        # macos/install-webapp.sh) can then only vouch for
                        # these names, never mint certificates for other sites.
                        "-addext", "basicConstraints=critical,CA:FALSE",
                        "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
                        "-keyout", str(key), "-out", str(cert)],
                       check=True, capture_output=True)
        os.chmod(key, 0o600)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    return ctx


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--rig", default="ic7300", help="radio profile (see --list-rigs)")
    p.add_argument("--list-rigs", action="store_true", help="show supported radios and exit")
    p.add_argument("--sim", action="store_true", help="simulate the chosen rig instead of opening it")
    p.add_argument("--port", default="/dev/ttyUSB0", help="serial port of the radio")
    p.add_argument("--baud", type=int, help="serial speed (default: the rig profile's)")
    p.add_argument("--civ-addr", type=lambda s: int(s, 16),
                   help="CI-V address in hex (default: the rig profile's)")
    p.add_argument("--max-watts", type=int, help="override the rig's maximum power for the UI")
    p.add_argument("--hamlib-model", type=int, help="Hamlib model number (rigctl -l) for --rig hamlib")
    p.add_argument("--rigctld", metavar="HOST:PORT", help="use an already-running rigctld")
    p.add_argument("--audio-device", default=DEFAULT_DEVICE,
                   help="the radio's USB audio codec (ALSA device on Linux, device name on macOS)")
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--listen", type=int, default=7300, help="HTTP port (default 7300)")
    p.add_argument("--https", action="store_true",
                   help="serve over TLS with a self-signed cert (browsers only allow the mic on https)")
    p.add_argument("--tls-name", action="append", default=[],
                   help="extra hostname for the --https certificate (repeatable)")
    p.add_argument("--host-file", metavar="HOST",
                   help="this host's hosts/HOST.json: turns on the page's Modules tab "
                        "(rendered by ranch-radio; never inferred from the machine's name)")
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="another host name this page may be opened by (IPs, single-label and "
                        ".local-style names are always allowed)")
    p.add_argument("--token", default=os.environ.get("IC7300_TOKEN"),
                   help="require ?token=... to connect (or set IC7300_TOKEN)")
    p.add_argument("--no-tx", action="store_true", help="receive-only: refuse to key the radio")
    p.add_argument("--auto-tune", action="store_true",
                   help="tune the ATU once per band after a band change (needs transmit allowed; it transmits)")
    p.add_argument("--rigctl-port", type=int, default=4532,
                   help="rigctld-compatible port for JTDX/WSJT-X/fldigi (Hamlib NET rigctl); 0 = off")
    p.add_argument("--rigctl-trace", action="store_true",
                   help="log every rigctl command and reply (diagnosing an app's Hamlib errors)")
    p.add_argument("--rigctl-host", default="127.0.0.1",
                   help="address for the rigctl listener (default: this machine only)")
    p.add_argument("--winlink-rigctl-port", type=int, default=0,
                   help="rigctl port for Pat (Winlink) on 127.0.0.1; it may key only while a "
                        "Winlink session is armed from the page. 0 = off")
    p.add_argument("--winlink-pat", metavar="CMD",
                   help="run Pat (Winlink engine) with this command, e.g. 'pat'; needs "
                        "--winlink-rigctl-port, and Pat's hamlib rig pointed at it")
    p.add_argument("--winlink-pat-port", type=int, default=8095,
                   help="port for Pat's API on 127.0.0.1 (never exposed; default 8095)")
    p.add_argument("--winlink-session-max", type=float, default=900,
                   help="seconds a Winlink session may run before it is cut (default 900)")
    p.add_argument("--winlink-ardop-log", metavar="DIR",
                   help="ardopcf's log directory (its --logdir), read only: tells which station "
                        "IDs went on air through the lane and whether one is owed; a session "
                        "that transmitted stays armed (bounded) until ardopcf's ID is out")
    p.add_argument("--winlink-mode", type=winlink_mode, metavar="MODE[+DATA]",
                   help="mode for a Winlink session, set on arming and put back on disarm "
                        "(QMX: USB; its DiGi mode, USB+DATA, can't send ARDOP)")
    p.add_argument("--winlink-codec-db", type=codec_levels, metavar="DB|TABLE",
                   help="codec output level (dB) while a Winlink session is armed: one number "
                        "(-38) or a per-band table for the session's band, "
                        "'default=-38,80m=-38,40m=-44,20m=-50' (unlisted bands: default). The previous "
                        "level is restored on disarm. Unset = codec left alone")
    p.add_argument("--winlink-codec-mixer", metavar="NAME",
                   help="mixer for --winlink-codec-db: ALSA CARD:CONTROL on Linux (default: the "
                        "--audio-device card, control PCM), CoreAudio device name on macOS "
                        "(default --audio-device); with --sim and none, a simulated level")
    p.add_argument("--voice-codec-db", type=codec_levels, metavar="DB|TABLE",
                   help="codec output level (dB) while this page's own PTT transmits voice, same "
                        "form as --winlink-codec-db (same mixer); restored on unkey. Apps keying "
                        "over rigctl never move it. Unset = voice leaves the codec alone")
    p.add_argument("--codec-max-db", type=float, metavar="DB",
                   help="highest level the page may step the codec to (default 0 dB, the codec's "
                        "top): a radio that overdrives below 0 dB gets a ceiling")
    p.add_argument("--codec-min-db", type=float, metavar="DB",
                   help="lowest level the page may step the codec to (e.g. -36 when the mixer's "
                        "next step down is mute)")
    p.add_argument("--codec-save", metavar="HOST:RADIO",
                   help="let the page save its levels into hosts/HOST.json for RADIO (and install "
                        "that radio's unit); unset = levels can be stepped but not saved")
    p.add_argument("--mesh-port", metavar="DEV",
                   help="serial device of a Meshtastic node (e.g. /dev/meshtastic), or ble:NAME for "
                        "Bluetooth (e.g. ble:NODEM_7720): adds the Mesh "
                        "panel (messages, channels, node list). Unset = no mesh")
    p.add_argument("--mesh-python", default="~/.local/share/venvs/meshtastic/bin/python",
                   help="Python with the meshtastic package, which runs mesh_bridge.py "
                        "(default: %(default)s)")
    p.add_argument("--mesh-bridge", default=str(HERE / "mesh_bridge.py"), help=argparse.SUPPRESS)
    p.add_argument("--mesh-key", action="append", default=[], metavar="CHANNEL=FILE",
                   help="check that the node's CHANNEL uses the base64 key in FILE; the page "
                        "warns if not (repeatable). The key never leaves the bridge")
    p.add_argument("--mesh-log", default="~/.local/state/radio-remote/mesh.jsonl",
                   help="message history kept across restarts (mode 600; '' = memory only)")
    p.add_argument("--addons", metavar="FILE",
                   help="JSON file of add-ons to run, {\"uvpro\": {\"enabled\": true, ...}} (see addons/). "
                        "Unset = none")
    p.add_argument("--addons-state", default="~/.local/state/radio-remote/addons.json",
                   help="where the page's add-on on/off choices are kept (default: %(default)s)")
    p.add_argument("--tune-timeout", type=float, default=20,
                   help="max seconds for an ATU tune this server started (front-panel tunes are never cut)")
    p.add_argument("--tx-timeout", type=int, default=180, help="max seconds per transmission")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    rigctl_sock, rigctl_adopted = systemd_rigctl_socket()
    if args.list_rigs:
        print(rigs.describe())
        return

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    caps = rigs.get(args.rig)
    if args.max_watts:
        caps["max_watts"] = args.max_watts
    if args.sim:
        from sim import SimRadio
        radio = SimRadio(caps)
    elif caps["backend"] == "qmx":
        from qmx import QmxRadio
        radio = QmxRadio(caps, args.port, args.baud)
    elif caps["backend"] == "hamlib":
        from hamlib_rig import HamlibRadio
        radio = HamlibRadio(caps, args.hamlib_model, args.port, args.baud, args.rigctld)
    else:
        from civ import CivRadio
        radio = CivRadio(caps, args.port, args.baud, args.civ_addr)

    audio = None if args.no_audio else AudioBridge(args.audio_device, sim=radio if args.sim else None)
    station = Station(radio, audio, args)
    if args.winlink_pat:
        if not args.winlink_rigctl_port:
            p.error("--winlink-pat needs --winlink-rigctl-port (Pat keys only through that lane)")
    saver = None
    if args.codec_save:
        host, _, rid = args.codec_save.partition(":")
        if not (host and rid):
            p.error("--codec-save is HOST:RADIO, e.g. linux-station:g90")

        def saver(use, table, host=host, rid=rid):
            from ranch_radio import levels
            # RANCH_RADIO_REPO: tests point the save at a scratch copy of the repo.
            repo = os.environ.get("RANCH_RADIO_REPO") or os.path.dirname(os.path.abspath(__file__))
            written = levels.save(repo, host, rid, use, table)
            log.info("codec: wrote %s", ", ".join(written))
    station.codec = codec_level.make(args, saver)
    addon_cfg = {}
    if args.addons:
        with open(os.path.expanduser(args.addons)) as f:
            addon_cfg = json.load(f)
    if args.winlink_pat:          # Winlink (Pat) is an add-on (addons/winlink.py); the lane is not
        addon_cfg.setdefault("winlink", {"engine_args": args})
    if args.mesh_port:            # Mesh is an add-on (addons/mesh.py); --mesh-* configure it
        addon_cfg.setdefault("mesh", {"port": args.mesh_port, "python": args.mesh_python,
                                      "bridge": args.mesh_bridge, "keys": list(args.mesh_key),
                                      "log": args.mesh_log})
    if addon_cfg:
        from addons import Addons
        station.addons = Addons(station, addon_cfg, args.addons_state or None)
    if args.host_file:
        from ranch_radio import pageapi
        # RANCH_RADIO_REPO / RANCH_RADIO_HOME: tests point these at scratch copies.
        repo = os.environ.get("RANCH_RADIO_REPO") or os.path.dirname(os.path.abspath(__file__))
        station.modules = pageapi.ModulesService(args.host_file, repo, lambda: station.addons,
                                                 station._send, station._broadcast,
                                                 home=os.environ.get("RANCH_RADIO_HOME"),
                                                 state_path=args.addons_state or None)
    ctx = tls_context(HERE / "certs", args.tls_name) if args.https else None
    scheme = "https" if ctx else "http"
    print(f"\n  {caps['name']} Remote on {scheme}://<this-machine>:{args.listen}/"
          + (f"?token={args.token}" if args.token else "") + "\n")
    web.run_app(make_app(station, args, rigctl_sock, rigctl_adopted), host=args.host, port=args.listen,
                ssl_context=ctx, print=None)


if __name__ == "__main__":
    main()
