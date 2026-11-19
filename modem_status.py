"""What each HF modem is doing right now, read from its own log, for the
Winlink panel's status strip (2026-11-18: the strip had state and queue but
nothing from the modem; a peer station's XarAC dashboard shows S/N, speed,
BUSY and PTT, and both modems already log all of it).

Read only. Nothing here talks to a modem: Pat owns ardopcf's host port and
xara's, and a second client there would break sessions. The lines come from
the log tails station_id already runs (ArdopLogTail, VaraLogTail), which
call feed_ardop / feed_xara with each line's text after its timestamp.

The station's own call (to tell the partner on a VARA CONNECTED line from
this end) is read from the same logs: xara's "> Mycall(Ok(["N0CALL"]))",
ardopcf's "TO Host MYCALL N0CALL"; without either, the second call.

ardopcf (its --logdir debug log), what it tells Pat, traced:
    Command Trace TO Host NEWSTATE ISS|IRS|IDLE|DISC|...
    Command Trace TO Host CONNECTED <call> <bw>
    Command Trace TO Host BUSY TRUE|FALSE
    Command Trace TO Host BUFFER <bytes>
    [Main.KeyPTT]  PTT-TRUE|PTT-FALSE
    [Frame Type Decode OK  ] Ldr; S:N(3KHz) Early= x dB, Full <snr> dB, ... Type=H51: 4PSK.500.100.O, ...
    Qual = <0-100>
xara (its --log), the VARA host lines it echoes:
    < CONNECTED <from> <to> <bw>      < DISCONNECTED
    < SN <snr>                        < BITRATE (<level>)  <bps> bps TX|RX
    < BUSY ON|OFF                     < PTT ON|OFF            < BUFFER <bytes>

S/N is the other station's signal as this modem decoded it (3 kHz noise
bandwidth for ardopcf; VARA's own scale for xara). Every value carries the
wall time it was seen, so the page can show its age: a number from an hour
ago must not read as the link now.
"""

import re
import time

MODEMS = ("ardop", "varahf")

_A_HOST = re.compile(r"Command Trace TO Host (NEWSTATE|CONNECTED|BUSY|BUFFER|DISCONNECTED|MYCALL)\b ?(.*)$")
_A_PTT = re.compile(r"\[Main\.KeyPTT\]\s+PTT-(TRUE|FALSE)")
_A_DECODE = re.compile(r"\[Frame Type Decode OK\s*\].*?Full\s+(-?\d+(?:\.\d+)?) dB.*?Type=H[0-9A-F]+:\s*([^,]+),")
_A_QUAL = re.compile(r"^Qual = (\d+)")
# A data frame names its modulation: 4PSK.500.100.O (mode.bandwidth.baud.parity).
_A_DATA_FRAME = re.compile(r"^(\d*[A-Z]*(?:PSK|FSK|QAM)\.\d+\.\d+)\.[EO]$")

_X_LINE = re.compile(r"^\s*[\d.]+ < ([A-Z]+)\b ?(.*)$")
_X_MYCALL = re.compile(r'^\s*[\d.]+ > Mycall\(Ok\(\["([A-Z0-9/-]+)"')
_X_RATE = re.compile(r"^\((\d+)\)\s+(\d+) bps (TX|RX)")

UNLINKED = ("DISC", "IDLE_DISC", "OFFLINE")


def _base(call):
    return (call or "").upper().split("-")[0]


class ModemStatus:
    """One record per modem. on_change(snapshot) is called after a feed
    that changed something; the server coalesces the calls."""

    def __init__(self, mycall=None, on_change=None, clock=time.time):
        self.mycall = mycall
        self.on_change = on_change
        self.clock = clock
        self.state = {m: self._blank(m) for m in MODEMS}

    @staticmethod
    def _blank(modem):
        return {"modem": modem, "seen": None, "state": None, "linked": False, "partner": None,
                "bw_hz": None, "busy": None, "ptt": None, "buffer": None,
                "snr_db": None, "snr_at": None, "quality": None, "frame": None,
                "level": None, "bps": None, "rate_dir": None, "rate_at": None}

    def snapshot(self):
        """Only modems that logged something: an untracked modem is absent, not blank."""
        return {m: dict(s) for m, s in self.state.items() if s["seen"] is not None}

    def _set(self, modem, **kw):
        s = self.state[modem]
        changed = any(s.get(k) != v for k, v in kw.items())
        s.update(kw)
        first = s["seen"] is None
        s["seen"] = self.clock()
        if (changed or first) and self.on_change:
            self.on_change(self.snapshot())

    # --- ardopcf

    def feed_ardop(self, text):
        now = self.clock()
        m = _A_HOST.search(text)
        if m:
            what, arg = m.group(1), m.group(2).strip()
            if what == "NEWSTATE":
                linked = self.state["ardop"]["linked"] and arg not in UNLINKED
                self._set("ardop", state=arg, linked=linked)
            elif what == "CONNECTED":
                parts = arg.split()
                bw = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                self._set("ardop", linked=True, partner=parts[0] if parts else None, bw_hz=bw)
            elif what == "DISCONNECTED":
                self._set("ardop", linked=False)
            elif what == "BUSY":
                self._set("ardop", busy=arg.upper().startswith("TRUE"))
            elif what == "BUFFER" and arg.isdigit():
                self._set("ardop", buffer=int(arg))
            elif what == "MYCALL" and arg:
                self.mycall = arg.split()[0]          # the partner is the other call on a VARA CONNECTED
                self._set("ardop")                    # it is logging: tracked from here
            return
        m = _A_PTT.search(text)
        if m:
            self._set("ardop", ptt=m.group(1) == "TRUE")
            return
        m = _A_DECODE.search(text)
        if m:
            kw = {"snr_db": round(float(m.group(1)), 1), "snr_at": now}
            f = _A_DATA_FRAME.match(m.group(2).strip())
            if f:
                kw.update(frame=f.group(1), rate_at=now)
            self._set("ardop", **kw)
            return
        m = _A_QUAL.match(text.strip())
        if m:
            self._set("ardop", quality=int(m.group(1)))

    # --- xara (VARA HF)

    def feed_xara(self, text):
        c = _X_MYCALL.match(text)
        if c:
            self.mycall = c.group(1)
            self._set("varahf")
            return
        m = _X_LINE.match(text)
        if not m:
            return
        what, arg = m.group(1), m.group(2).strip()
        now = self.clock()
        if what == "CONNECTED":
            parts = arg.split()
            calls = [p for p in parts if not p.isdigit()]
            bw = next((int(p) for p in parts if p.isdigit()), None)
            mine = _base(self.mycall)
            others = [c for c in calls if _base(c) != mine] if mine else calls[1:]
            self._set("varahf", linked=True, state="CONNECTED", bw_hz=bw,
                      partner=(others or calls[-1:] or [None])[0])
        elif what == "DISCONNECTED":
            self._set("varahf", linked=False, state="DISCONNECTED")
        elif what == "SN":
            try:
                self._set("varahf", snr_db=round(float(arg.split()[0]), 1), snr_at=now)
            except (ValueError, IndexError):
                pass
        elif what == "BITRATE":
            r = _X_RATE.match(arg)
            if r:
                self._set("varahf", level=int(r.group(1)), bps=int(r.group(2)), rate_dir=r.group(3), rate_at=now)
        elif what == "BUSY":
            self._set("varahf", busy=arg.upper().startswith("ON"))
        elif what == "PTT":
            self._set("varahf", ptt=arg.upper().startswith("ON"))
        elif what == "BUFFER" and arg.split()[:1] and arg.split()[0].isdigit():
            self._set("varahf", buffer=int(arg.split()[0]))
