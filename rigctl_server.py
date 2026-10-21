"""A rigctld-compatible TCP listener, so JTDX, WSJT-X, JS8Call and fldigi can
share the radio with the browser instead of fighting over its serial port.

Point the app at Hamlib's "NET rigctl" (model 2), host 127.0.0.1:4532.

The wire format follows Hamlib's rigctld in plain (non-extended) mode, as
spoken by its netrigctl client: a get command answers with value lines, a
set command answers "RPRT <code>". The open handshake is \\get_powerstat,
\\chk_vfo, then \\dump_state (copied in shape from rigctld 4.7.2 with the
dummy rig, then trimmed to what this server really does).

\\chk_vfo answers 1 (rigctld --vfo mode): netrigctl then names the VFO on
every command ("F VFOB 14074055"). Without it Hamlib sends a set_freq aimed at
VFO B as a bare "F", which lands on VFO A and loses the split TX frequency
(clients that address VFO B directly). Note: JTDX's "F <f+55>" then "F <f>" at
connect is its frequency-resolution probe, not a split set; and Hamlib sends no
"I" at all when the split TX frequency equals RX, so keying on the RX dial
then is correct.

PTT ("T 1") goes through Station.key, the same path as the browser's PTT, so
--no-tx, single-operator ownership and the --tx-timeout limit all apply. A
rigctl client that holds PTT must keep talking (JTDX/WSJT-X poll every
second or so): silence for RIGCTL_KEEPALIVE_S, or a disconnect, unkeys.
"""

import asyncio
import json
import logging
import os
import shlex
import socket
import time
import uuid

log = logging.getLogger("rigctl")

RIGCTL_KEEPALIVE_S = 5.0
# Pat turns a URL's freq= (kHz) into Hz itself; allow its float rounding.
LANE_QSY_SLOP_HZ = 10
# Pat sends nothing while an ARDOP frame plays, so its lane may stay silent
# longer; --tx-timeout still bounds every transmission.
WINLINK_KEEPALIVE_S = 12.0


class RigctlOwner:
    """Stands in for a browser socket as Station.ptt_owner."""
    closed = False
    keepalive_s = RIGCTL_KEEPALIVE_S

    def __init__(self, peer, lane=None):
        self.peer = peer
        self.lane = lane
        if lane == "winlink":
            self.keepalive_s = WINLINK_KEEPALIVE_S
        self.lost_msg = f"rigctl client {peer} went quiet"

    async def send_str(self, _msg):   # Station.notice() targets the owner
        pass

    async def send_bytes(self, _msg):
        pass

# VFO names netrigctl puts in front of arguments in --vfo mode (rig_strvfo).
VFO_NAMES = {"VFOA", "VFOB", "VFOC", "currVFO", "VFOCURR", "VFO", "MEM", "Main", "Sub",
             "MainA", "MainB", "MainC", "SubA", "SubB", "SubC", "TX", "RX", "None"}
TX_SIDE = {"TX", "Sub", "SubA", "SubB", "MainB"}

# Hamlib error codes (rig.h)
RIG_OK, RIG_EINVAL, RIG_ENIMPL, RIG_ENAVAIL = 0, -1, -4, -11
RIG_OP_TUNE = 1 << 11           # vfo_op_t TUNE: start the antenna tuner

# Hamlib setting_t level bits (rig.h, 4.7) for the levels this server can read.
# netrigctl refuses get_level CLIENT-side ("Feature not available", never sent,
# so never logged here) for any level missing from has_get_level. JTDX/WSJT-X
# with "request power"/"request SNR" poll these while keyed, and the refusal
# breaks the CAT link mid-transmit (2026-10-14, JTDX on mac-shack).
LEVEL_BITS = {"RFPOWER": 1 << 12, "SWR": 1 << 28, "ALC": 1 << 29, "STRENGTH": 1 << 30,
              "RFPOWER_METER": 1 << 32, "RFPOWER_METER_WATTS": 1 << 39}


def _interp(x, pts):
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (max(x, x0) - x0) * (y1 - y0) / (x1 - x0)
    return pts[-1][1]


# Radio raw meter (Icom 0-255 scales, as the browser reads them) -> Hamlib units.
# Inverses of hamlib_rig.py's strength_raw / po_raw / swr_raw.
def strength_db(raw):
    return round(raw / 120 * 54 - 54) if raw <= 120 else round((raw - 120) / 121 * 60)


def po_frac(raw):
    return _interp(raw, [(0, 0), (143, 0.5), (213, 1.0)])


def swr_ratio(raw):
    return _interp(raw, [(0, 1.0), (48, 1.5), (80, 2.0), (120, 3.0), (255, 9.9)])


def readable_levels(caps):
    """Hamlib level names this rig's state can answer, from what it measures."""
    m, names = caps.get("meters", []), []
    if "smeter" in m:
        names.append("STRENGTH")
    if "po" in m:
        names += ["RFPOWER_METER", "RFPOWER_METER_WATTS"]
    if "swr" in m:
        names.append("SWR")
    if "alc" in m:
        names.append("ALC")
    if "power" in caps.get("levels", []):
        names.append("RFPOWER")
    return names

# Hamlib rmode_t bits, and our mode names <-> Hamlib's
HL_BITS = {"AM": 0x1, "CW": 0x2, "USB": 0x4, "LSB": 0x8, "RTTY": 0x10, "FM": 0x20,
           "CWR": 0x80, "RTTYR": 0x100, "PKTLSB": 0x400, "PKTUSB": 0x800, "PKTFM": 0x1000}
TO_HL = {"CW-R": "CWR", "RTTY-R": "RTTYR"}
FROM_HL = {v: k for k, v in TO_HL.items()}
PKT = {"PKTUSB": "USB", "PKTLSB": "LSB", "PKTFM": "FM"}

# Passband per filter slot (FIL1..3), matching the UI's FILTER_BW table.
FILTER_BW = {"SSB": [3000, 2400, 1800], "CW": [1200, 500, 250], "RTTY": [2400, 500, 250],
             "AM": [9000, 6000, 3000], "FM": [15000, 10000, 7000]}


def _family(mode):
    if mode in ("USB", "LSB"):
        return "SSB"
    for f in ("CW", "RTTY"):
        if mode.startswith(f):
            return f
    return mode


def hamlib_modes(caps):
    """Hamlib mode names this rig can be asked for."""
    names = [TO_HL.get(m, m) for m in caps["modes"]]
    names += ["PKT" + m for m in caps.get("data_modes", []) if "PKT" + m in HL_BITS]
    return names


def dump_state(caps, tx_allowed=False):
    """The \\dump_state block netrigctl parses at open (protocol version 1)."""
    mask = 0
    for name in hamlib_modes(caps):
        mask |= HL_BITS.get(name, 0)
    lo, hi = caps["freq"]
    vfos = 0x3   # VFOA | VFOB
    lines = [
        "1",            # protocol version
        "1",            # rig model (generic; we are not any one Hamlib backend)
        "0",            # ITU region
        f"{lo:.6f} {hi:.6f} 0x{mask:x} -1 -1 0x{vfos:x} 0x1",   # RX range
        "0 0 0 0 0 0 0",
    ]
    if tx_allowed:   # TX range: low/high power in mW
        lines.append(f"{lo:.6f} {hi:.6f} 0x{mask:x} 1000 {caps['max_watts'] * 1000} 0x{vfos:x} 0x1")
    lines += [
        "0 0 0 0 0 0 0",
        f"0x{mask:x} 1", "0 0",                                 # tuning steps
    ]
    for fam, widths in FILTER_BW.items():
        bits = {"SSB": HL_BITS["USB"] | HL_BITS["LSB"] | HL_BITS["PKTUSB"] | HL_BITS["PKTLSB"],
                "CW": HL_BITS["CW"] | HL_BITS["CWR"], "RTTY": HL_BITS["RTTY"] | HL_BITS["RTTYR"],
                "AM": HL_BITS["AM"], "FM": HL_BITS["FM"] | HL_BITS["PKTFM"]}[fam] & mask
        if bits:
            lines += [f"0x{bits:x} {w}" for w in widths]
    lines += [
        "0 0",
        "0", "0", "0",  # max RIT, XIT, IF shift
        "0",            # announces
        "",             # preamp list
        "",             # attenuator list
        "0x0", "0x0",   # has_get_func, has_set_func
        f"0x{sum(LEVEL_BITS[n] for n in readable_levels(caps)):x}", "0x0",   # has_get_level, has_set_level
        "0x0", "0x0",   # has_get_parm, has_set_parm
        f"vfo_ops=0x{RIG_OP_TUNE if tx_allowed and 'tuner' in caps['features'] else 0:x}",
        "ptt_type=0x1",             # RIG_PTT_RIG: PTT is a CAT command (refused for now)
        # RIG_TARGETABLE_FREQ|MODE. Without these, libhamlib's rig_set_split_freq
        # (what JTDX/WSJT-X call) refuses client-side with "Feature not
        # available" and never sends I; the rigctl CLI takes another path and
        # hid it. With them it sends I / X, which the server handles per rig.
        # Plus RIG_TARGETABLE_LEVEL|PTT (0x20|0x80): with split on, a client
        # keying or metering the TX VFO (set_ptt(VFO_TX)) was refused client-
        # side as "Feature not available" (has_set_vfo=0 leaves libhamlib no
        # fallback). The server keys / meters the one radio whatever VFO is named.
        "targetable_vfo=0xa3",
        "has_set_vfo=1", "has_get_vfo=1",   # set_vfo is per-client (sel_vfo)
        "has_set_freq=1", "has_get_freq=1",
        "has_set_conf=0", "has_get_conf=0",
        "has_power2mW=0", "has_mW2power=0",
        "has_get_ant=0", "has_set_ant=0",
        "timeout=0",
        "rig_model=1",
        f"rigctld_version=Radio Remote ({caps['name']})",
        "done",
    ]
    return "\n".join(lines) + "\n"


class RigctlSession:
    """One client connection. Pure command -> reply logic; I/O lives below."""

    def __init__(self, station, peer="?", lane=None):
        self.station = station
        self.radio, self.caps = station.radio, station.radio.caps
        self.lane = lane         # "winlink": Pat's own listener, keys only while armed
        self.owner = RigctlOwner(peer, lane)
        self.vfo = None          # VFO named on the command being handled
        # VFO this client selected with "V" (None = the radio's own). Virtual:
        # the radio is never switched. Hamlib before 4.6 (JTDX bundles 4.5)
        # ignores targetable_vfo in dump_state, so for a split TX frequency it
        # does "V VFOB", "F <tx>", "V VFOA"; selecting the other VFO here points
        # unnamed freq/mode commands at the split TX side until it switches back.
        self.sel_vfo = None

    def targets_tx(self):
        """True if the current command names the split TX side (the other VFO)."""
        v = self.vfo
        if v in (None, "currVFO", "VFOCURR", "VFO") and self.sel_vfo:
            v = self.sel_vfo
        if v in TX_SIDE:
            return True
        if v in ("VFOA", "VFOB"):
            return v != f"VFO{self.s.get('vfo', 'A')}"
        return False

    def tx_freq(self):
        if self.real_split:
            return self.s.get("freq_b", self.s["freq"])
        tx = self.station.emu_split_tx
        return tx if tx is not None else self.s["freq"]

    async def set_tx_freq(self, hz):
        lo, hi = self.caps["freq"]
        if not lo <= hz <= hi:
            return RIG_EINVAL
        if self.real_split:
            if self.station.retune_blocked() and hz != self.s.get("freq_b"):
                return RIG_ENAVAIL           # VFO B is the live TX frequency now
            await self.radio.set("freq_b", hz)
        else:
            self.station.emu_split_tx = hz
        return RIG_OK

    @property
    def tx_allowed(self):
        return not self.station.args.no_tx

    @property
    def real_split(self):
        """True if the radio can hold a TX frequency itself (VFO B over CAT)."""
        return "vfo_b" in self.caps["features"] and "split" in self.caps["features"]

    def holds_ptt(self):
        return self.station.ptt_owner is self.owner

    async def release(self):
        """Client gone: never leave the transmitter keyed behind it."""
        if self.holds_ptt():
            await self.station.unkey(f"rigctl client {self.owner.peer} disconnected")

    @property
    def s(self):
        return self.radio.state

    def mode_reply(self):
        m = self.s["mode"]
        name = TO_HL.get(m, m)
        if self.s.get("data") and m in self.caps.get("data_modes", []):
            name = "PKT" + m
        widths = FILTER_BW.get(_family(m), [0, 0, 0])
        fil = self.s.get("filter") or 1
        return f"{name}\n{widths[min(fil, len(widths)) - 1]}\n"

    async def set_mode(self, name):
        base = PKT.get(name, FROM_HL.get(name, name))
        if base not in self.caps["modes"]:
            return RIG_EINVAL
        want_data = name in PKT
        data_ok = base in self.caps.get("data_modes", [])
        if want_data and not data_ok:
            return RIG_EINVAL
        old_mode, old_data = self.s["mode"], bool(self.s.get("data"))
        self.station.app_set("mode", base, hold=self.lane is None)
        if base != old_mode:
            await self.radio.set("mode", base)
        if data_ok:
            # A rig whose line audio needs DATA (G90: data_sticky) keeps it on
            # for a plain USB/LSB request, as the browser does. Otherwise follow
            # the request. Re-assert after a mode change: Icom-style 06 mode
            # sets drop data mode.
            sticky = bool(self.caps.get("data_sticky"))
            target = want_data or (sticky and old_data)
            self.station.app_set("data", target, hold=self.lane is None)
            # On a sticky rig, send DATA on every time it's wanted: our read
            # of it can be wrong (2026-10-13 the G90 sat in plain USB, its
            # audio dead, while the server showed data on, so nothing was sent).
            if target != old_data or (target and (base != old_mode or sticky)):
                await self.radio.set("data", target)
        return RIG_OK

    async def handle(self, line):
        """Return (reply text, close?)."""
        try:
            parts = shlex.split(line)
        except ValueError:
            return f"RPRT {RIG_EINVAL}\n", False
        if not parts:
            return "", False
        cmd, args = parts[0], parts[1:]
        if cmd.startswith("\\"):
            cmd = cmd[1:]
        vfo = None
        if cmd not in ("V", "set_vfo") and args and args[0] in VFO_NAMES:
            vfo, args = args[0], args[1:]
        self.vfo = vfo
        if self.holds_ptt():
            # Any traffic from the keying client is its keepalive.
            self.station.ptt_seen = time.monotonic()
        try:
            reply, close = await self._dispatch(cmd, args)
            if getattr(self.station.args, "rigctl_trace", False):
                log.info("rigctl %s: %r -> %r", self.owner.peer, line, reply)
            elif reply.startswith("RPRT -"):
                # Every refusal is visible: an app's "Hamlib error" maps to a line here.
                log.info("rigctl %s: %r -> %s", self.owner.peer, line, reply.strip())
            return reply, close
        except ValueError as e:
            log.info("rigctl: rejected %r: %s", line, e)
            return f"RPRT {RIG_EINVAL}\n", False
        except Exception:
            log.exception("rigctl: %r failed", line)
            return f"RPRT {RIG_EINVAL}\n", False

    async def _dispatch(self, cmd, args):
        s, caps = self.s, self.caps
        ok = f"RPRT {RIG_OK}\n"
        if cmd in ("q", "Q", "quit", "exit"):
            return ok, True              # rigctld 4.7 answers q with RPRT 0
        # Replies below copied from rigctld 4.7.2: Hamlib's rig_set_mode asks
        # for the lock mode first and stalls ~20 s (then skips the set) if the
        # answer is "not implemented".
        if cmd == "get_lock_mode":
            return "0\n" + ok, False
        if cmd == "set_lock_mode":
            return ok, False
        if cmd == "get_vfo_info":
            m, width = self.mode_reply().split()
            hz = self.tx_freq() if self.targets_tx() else s["freq"]
            on = s.get("split") if self.real_split else self.station.emu_split
            return f"{int(hz)}\n{m}\n{width}\n{1 if on else 0}\n0\n", False
        if cmd == "chk_vfo":
            return "1\n", False
        if cmd == "dump_state":
            return dump_state(caps, self.tx_allowed), False
        if cmd == "get_powerstat":
            return ("1\n" if s.get("connected") else "0\n"), False
        if cmd in ("F", "set_freq") and self.lane == "winlink" and args:
            # Pat moves the dial only for a session; after one it reverts to
            # its pre-session frequency, which (a band change) fired an ATU
            # tune carrier (2026-10-15). Unarmed: answer ok so Pat doesn't
            # log an error, but leave the radio alone. Every lane F is logged.
            # Pat's revert (QSX) comes ~1 s BEFORE its /api/connect returns,
            # i.e. still armed: while a session named a frequency, that is the
            # only one the lane may set.
            hz = int(round(float(args[-1])))
            armed = self.station.winlink_armed
            want = self.station.winlink_qsy_hz
            if not armed:
                why = "ignored (no session armed)"
            elif want is not None and abs(hz - want) > LANE_QSY_SLOP_HZ:
                why = f"ignored (armed; not the session's {want} Hz)"
            else:
                why = "applied (armed)"
            log.info("rigctl winlink lane %s: set_freq %s%d Hz %s", self.owner.peer,
                     f"{self.vfo} " if self.vfo else "", hz, why)
            if why.startswith("ignored"):
                return ok, False
        if cmd in ("f", "get_freq") and self.targets_tx():
            return f"{int(self.tx_freq())}\n", False
        if cmd in ("F", "set_freq") and self.targets_tx():
            return f"RPRT {await self.set_tx_freq(int(round(float(args[-1]))))}\n", False
        if cmd in ("m", "get_mode", "M", "set_mode") and self.targets_tx():
            # Split TX uses the RX mode here (as X / set_split_mode does).
            return (self.mode_reply() if cmd in ("m", "get_mode") else ok), False
        if cmd in ("f", "get_freq"):
            rx = self.station.split_restore      # emulated split, keyed: VFO A is "RX"
            if rx is None:
                rx = self.station.pending_freq   # change waiting for a tune to end
            return f"{int(rx if rx is not None else s['freq'])}\n", False
        if cmd in ("F", "set_freq"):
            hz = int(round(float(args[-1])))
            if self.station.retune_blocked():
                rx = self.station.split_restore
                if hz == (rx if rx is not None else s["freq"]):
                    return ok, False         # already there: harmless re-send
                if self.station.tune_only_blocked():
                    self.station.freq_after_tune(hz)   # applied when the tune ends
                    return ok, False
                log.warning("rigctl %s: F %d refused: transmitting", self.owner.peer, hz)
                return f"RPRT {RIG_ENAVAIL}\n", False
            self.station.pending_freq = None         # superseded
            self.station.app_set("freq", hz, hold=self.lane is None)
            await self.radio.set("freq", hz)
            return ok, False
        if cmd in ("m", "get_mode"):
            return self.mode_reply(), False
        if cmd in ("M", "set_mode"):
            if self.station.retune_blocked():
                if self.mode_reply().split()[0] == args[0]:
                    return ok, False         # already in that mode
                log.warning("rigctl %s: M %s refused: transmitting", self.owner.peer, args[0])
                return f"RPRT {RIG_ENAVAIL}\n", False
            return f"RPRT {await self.set_mode(args[0])}\n", False
        if cmd in ("v", "get_vfo"):
            return f"{self.sel_vfo or 'VFO' + s.get('vfo', 'A')}\n", False
        if cmd in ("V", "set_vfo"):
            want = args[0] if args else ""
            radio_vfo = f"VFO{s.get('vfo', 'A')}"
            if want in (radio_vfo, "currVFO", "VFOCURR", "RX", "Main", "MainA"):
                self.sel_vfo = None
                return ok, False
            if want in ("VFOA", "VFOB", "TX", "Sub", "SubA", "MainB"):
                self.sel_vfo = "VFOB" if radio_vfo == "VFOA" else "VFOA"
                return ok, False
            return f"RPRT {RIG_ENAVAIL}\n", False
        if cmd in ("t", "get_ptt"):
            return ("1\n" if s.get("tx") else "0\n"), False
        if cmd in ("T", "set_ptt"):
            on = bool(args) and args[-1] != "0"
            if not on:
                if self.holds_ptt() or s.get("tx"):
                    await self.station.unkey()
                return ok, False
            st = self.station
            if self.lane == "winlink" and not st.winlink_armed:
                st.winlink_lane_refused(self.owner.peer)
                return f"RPRT {RIG_ENAVAIL}\n", False
            why = await st.can_key(self.owner)
            if why:
                # Refused before the split hop: the dial never moves.
                log.warning("rigctl %s: PTT refused: %s", self.owner.peer, why)
                return f"RPRT {RIG_ENAVAIL}\n", False
            tx = st.emu_split_tx if (st.emu_split and not self.real_split) else None
            if (tx is not None and tx != s["freq"] and st.split_restore is None
                    and not st.split_hop_ok(s["freq"], tx)):
                log.warning("rigctl %s: PTT refused: split TX %d is not on RX %d's band",
                            self.owner.peer, tx, s["freq"])
                return f"RPRT {RIG_ENAVAIL}\n", False
            if tx is not None and tx != s["freq"] and st.split_restore is None:
                # Dial the TX frequency in first; the key command queues after it.
                st.split_restore = s["freq"]
                log.info("split: TX on %d (RX %d)", tx, s["freq"])
                await self.radio.set("freq", tx)
            await st.key(self.owner, True)
            if not self.holds_ptt():
                # Refused after all (a race): undo the hop, never leave
                # split_restore set (it would hold the hold busy forever).
                rx, st.split_restore = st.split_restore, None
                if rx is not None:
                    await self.radio.set("freq", rx)
                return f"RPRT {RIG_ENAVAIL}\n", False
            log.info("rigctl: PTT ON from %s", self.owner.peer)
            if self.lane == "winlink":
                st.id_ledger.ptt(True)
            return ok, False
        if cmd in ("s", "get_split_vfo"):
            on = s.get("split") if self.real_split else self.station.emu_split
            return ("1\nVFOB\n" if on else "0\nVFOA\n"), False
        if cmd in ("S", "set_split_vfo"):
            on = bool(int(args[0])) if args else False
            if not self.real_split:
                # Never touch the radio's own split here: with no CAT access to
                # VFO B it would transmit on whatever B happens to hold.
                self.station.emu_split = on
                if not on:
                    self.station.emu_split_tx = None
                return ok, False
            if on != bool(s.get("split")):
                if self.station.retune_blocked():
                    # F18: the radio's own split picks the transmit VFO; turning
                    # it on or off while keyed moves the carrier to the other one.
                    log.warning("rigctl %s: S %d refused: transmitting", self.owner.peer, on)
                    return f"RPRT {RIG_ENAVAIL}\n", False
                await self.radio.set("split", on)
            return ok, False
        if cmd in ("i", "get_split_freq"):
            return f"{int(self.tx_freq())}\n", False
        if cmd in ("I", "set_split_freq"):
            return f"RPRT {await self.set_tx_freq(int(round(float(args[-1]))))}\n", False
        if cmd in ("x", "get_split_mode"):
            return self.mode_reply(), False
        if cmd in ("X", "set_split_mode"):
            return ok, False   # split TX uses the RX mode here
        if cmd in ("l", "get_level"):
            name = args[0] if args else ""
            if name not in readable_levels(caps):
                return f"RPRT {RIG_ENAVAIL}\n", False
            if name == "STRENGTH":
                val = str(strength_db(s.get("smeter") or 0))
            elif name == "RFPOWER_METER":
                val = f"{po_frac(s.get('po') or 0):.6f}"
            elif name == "RFPOWER_METER_WATTS":
                val = f"{po_frac(s.get('po') or 0) * caps['max_watts']:.6f}"
            elif name == "SWR":
                val = f"{swr_ratio(s.get('swr') or 0):.6f}"
            elif name == "ALC":
                val = f"{min((s.get('alc') or 0) / 120, 1.0):.6f}"
            else:   # RFPOWER: the power setting, 0..1
                val = f"{(s.get('power') or 0) / 255:.6f}"
            return f"{val}\n", False
        if cmd in ("G", "vfo_op"):
            if args and args[0] == "TUNE":
                if not self.tx_allowed or "tuner" not in caps["features"]:
                    return f"RPRT {RIG_ENAVAIL}\n", False
                if self.station.ptt_owner is not None or s.get("tx"):
                    return f"RPRT {RIG_ENAVAIL}\n", False
                log.info("rigctl: ATU tune requested by %s", self.owner.peer)
                await self.station.start_tune()
                return ok, False
            return f"RPRT {RIG_ENIMPL}\n", False
        return f"RPRT {RIG_ENIMPL}\n", False


def sd_notify(msg, fds=()):
    """Send one message (and fds) to systemd's NOTIFY_SOCKET. False if there is none."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    if path.startswith("@"):
        path = "\0" + path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(path)
            socket.send_fds(s, [msg.encode()], list(fds)) if fds else s.send(msg.encode())
        return True
    except OSError as e:
        log.warning("sd_notify %r failed: %s", msg.split("\n", 1)[0], e)
        return False


class Handover:
    """Keep rigctl sessions open across a restart or crash (F10c).

    Each accepted connection is put in systemd's fd store (FDSTORE=1) at once,
    so it outlives this process; the next process adopts it from LISTEN_FDS
    and the app never sees a disconnect. JTDX showed "IO error while getting
    correct VFO" on every restart that broke its session (linux-station 2026-10-14).
    The session state an app set and won't send again (emulated split, hold,
    selected VFO) is written to $RUNTIME_DIRECTORY after every command, so a
    carried-over JTDX still keys on its split TX frequency."""

    FILE = "rigctl-handover.json"

    def __init__(self, station, enabled=True):
        self.station = station
        self.enabled = enabled and bool(os.environ.get("NOTIFY_SOCKET"))
        d = os.environ.get("RUNTIME_DIRECTORY")
        self.path = os.path.join(d.split(":")[0], self.FILE) if (d and self.enabled) else None
        self.frozen = False      # shutting down: the saved state is final
        self._last = None

    def store(self, name, sock):
        if self.enabled and sock is not None:
            sd_notify(f"FDSTORE=1\nFDNAME={name}", [sock.fileno()])

    def forget(self, name):
        if self.enabled and not self.frozen:
            sd_notify(f"FDSTOREREMOVE=1\nFDNAME={name}")

    def save(self, conns):
        if not self.path or self.frozen:
            return
        st = self.station
        snap = {"emu_split": st.emu_split, "emu_split_tx": st.emu_split_tx, "hold": st.hold,
                "sel_vfo": {c.name: c.sess.sel_vfo for c in conns if c.sess}}
        if snap == self._last:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(snap, f)
            os.replace(tmp, self.path)
            self._last = snap
        except OSError as e:
            log.warning("rigctl handover state not saved: %s", e)

    def load(self):
        if not self.path:
            return None
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def discard(self):
        if self.path:
            try:
                os.remove(self.path)
            except OSError:
                pass


class _Conn:
    """One open rigctl connection, as tracked for shutdown."""

    def __init__(self, writer, name):
        self.writer = writer
        self.name = name
        self.sess = None
        self.task = asyncio.current_task()
        self.busy = False        # a command is being answered
        self.closing = False
        self.handed_over = False  # left open in systemd's fd store for the next process


async def serve(station, host, port, sock=None, adopted=(), lane=None):
    """lane="winlink": Pat's private listener (no systemd handover; PTT only
    while a Winlink session is armed, longer keepalive)."""
    conns = set()
    handover = Handover(station, enabled=lane is None)
    saved = handover.load() if adopted else None
    if adopted and saved:
        # Carried-over sessions: restore what their app set and won't resend.
        station.emu_split = bool(saved.get("emu_split"))
        station.emu_split_tx = saved.get("emu_split_tx")
        station.hold = dict(saved.get("hold") or {})
        log.info("rigctl: restored split=%s tx=%s hold=%s for %d carried-over session(s)",
                 station.emu_split, station.emu_split_tx, station.hold, len(adopted))
    elif not adopted:
        handover.discard()       # nothing carried over: never resurrect old state

    async def client(reader, writer, name=None):
        peer = writer.get_extra_info("peername")
        carried = name is not None
        if not carried:
            name = f"rigctl-{uuid.uuid4().hex[:12]}"
            handover.store(name, writer.get_extra_info("socket"))
        log.info("rigctl %sclient %s %s", f"{lane} " if lane else "", peer,
                 "carried over from the last run" if carried else "connected")
        if lane is None:
            # Pat stays connected all the time: it must not hold the radio
            # where it last put it (that is for apps like JTDX).
            station.rigctl_connected(True)
        sess = RigctlSession(station, f"{peer[0]}:{peer[1]}" if peer else "?", lane)
        conn = _Conn(writer, name)
        conn.sess = sess
        if carried and saved:
            sess.sel_vfo = (saved.get("sel_vfo") or {}).get(name)
        conns.add(conn)
        try:
            while not conn.closing:
                raw = await reader.readline()
                if not raw:
                    break
                conn.busy = True
                reply, close = await sess.handle(raw.decode("utf-8", "replace").strip())
                if reply:
                    writer.write(reply.encode())
                    await writer.drain()
                conn.busy = False
                handover.save(conns)
                if close:
                    break
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            conns.discard(conn)
            await sess.release()
            if lane is None:
                station.rigctl_connected(False)
            if conn.handed_over:
                log.info("rigctl client %s handed over (stays connected)", peer)
            else:
                handover.forget(name)
                log.info("rigctl client %s disconnected", peer)
            writer.close()   # our fd only: systemd's stored copy keeps a handed-over one open

    if sock is not None:
        # Held by systemd (g90-remote.socket): closing our copy at shutdown
        # leaves it listening, so clients queue across a restart (F10b).
        server = await asyncio.start_server(client, sock=sock)
        h, p = sock.getsockname()[:2]
        log.info("rigctl listener on %s:%d inherited from systemd (Hamlib NET rigctl, model 2)", h, p)
    else:
        server = await asyncio.start_server(client, host, port)
        log.info("rigctl %slistener on %s:%d (Hamlib NET rigctl, model 2)",
                 f"{lane} " if lane else "", host, port)
    server.rigctl_conns = conns
    server.rigctl_handover = handover
    for name, s in adopted:
        try:
            r, w = await asyncio.open_connection(sock=s)
        except OSError as e:
            log.warning("rigctl: could not adopt %s: %s", name, e)
            continue
        asyncio.ensure_future(client(r, w, name=name))
    return server


async def shutdown(server, timeout=1.0):
    """Stop listening, then end every session in order (F10): a command being
    answered gets its reply first; each session releases its PTT and then
    closes with a clean EOF instead of dying with the process. Under systemd
    with an fd store (F10c) the session is handed over instead: no EOF."""
    server.close()
    conns = list(getattr(server, "rigctl_conns", ()))
    handover = getattr(server, "rigctl_handover", None)
    if handover and handover.enabled:
        # F10c: sessions stay open in systemd's fd store for the next process.
        handover.save(conns)
        handover.frozen = True
        for c in conns:
            c.handed_over = True
    for c in conns:
        c.closing = True
        if not c.busy:
            c.writer.close()     # the pending readline sees EOF
    tasks = [c.task for c in conns if c.task is not None]
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:            # stuck on a write to a dead peer: give up on it
        t.cancel()
    if pending:
        await asyncio.wait(pending, timeout=0.5)
        log.warning("rigctl: %d session(s) did not close in %.1fs", len(pending), timeout)
