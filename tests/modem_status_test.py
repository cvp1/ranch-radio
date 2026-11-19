"""modem_status: what each HF modem is doing, read from its own log lines
(the text after the timestamp, as station_id's log tails pass it).

Covers: a modem that logged nothing is absent from the snapshot (not blank);
ardopcf: CONNECTED (partner, bandwidth), NEWSTATE (linked until DISC), BUSY,
PTT, BUFFER, a decoded frame's S/N (with when) and a data frame's modulation,
a control frame (DataACK) updating S/N but not the modulation, a failed
decode changing nothing, Qual, its MYCALL; xara: Mycall, CONNECTED from
either end (the partner is the other call), SN, BITRATE, BUSY, PTT,
DISCONNECTED, a malformed SN ignored; on_change only when something changed.
Lines are the formats seen in ardopcf 1.0.4 and xara 0.1.3 logs (2026-11-18).

    python3 tests/modem_status_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import modem_status  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


now = [1000.0]
changes = []
m = modem_status.ModemStatus(on_change=changes.append, clock=lambda: now[0])

check("nothing logged: empty snapshot", m.snapshot(), {})

# --- ardopcf
m.feed_ardop(" Command Trace TO Host MYCALL N9ABC")
check("ardop MYCALL learnt", m.mycall, "N9ABC")
check("a modem that logged is in the snapshot, the other is not", sorted(m.snapshot()), ["ardop"])
m.feed_ardop(" Command Trace TO Host NEWSTATE ISS ")
check("NEWSTATE before a link: not linked", (m.state["ardop"]["state"], m.state["ardop"]["linked"]), ("ISS", False))
m.feed_ardop(" Command Trace TO Host CONNECTED N0DAJ 2000")
a = m.state["ardop"]
check("CONNECTED: partner, bandwidth, linked", (a["partner"], a["bw_hz"], a["linked"]), ("N0DAJ", 2000, True))
m.feed_ardop(" Command Trace TO Host NEWSTATE IRS ")
check("NEWSTATE while linked stays linked", (a["state"], a["linked"]), ("IRS", True))
now[0] = 1005.0
m.feed_ardop("[Frame Type Decode OK  ] Ldr; S:N(3KHz) Early= 1.830933 dB, Full -1.329048 dB, Offset= -34.372814 Hz:  "
             "MD Decode;9 ID=HFC, Type=H51: 4PSK.500.100.O, D1= 0.00, D2= 0.01")
check("decoded data frame: S/N (Full), rounded", a["snr_db"], -1.3)
check("decoded data frame: when", a["snr_at"], 1005.0)
check("decoded data frame: modulation without its parity", a["frame"], "4PSK.500.100")
m.feed_ardop("[Frame Type Decode OK  ] Ldr; S:N(3KHz) Early= 1.866058 dB, Full 3.5 dB, Offset= -34.447227 Hz:  "
             "MD Decode;6 ID=HFC, Type=HFD:DataACK, D1= 0.01, D2= 0.01")
check("control frame: S/N moves, modulation kept", (a["snr_db"], a["frame"]), (3.5, "4PSK.500.100"))
m.feed_ardop("[Frame Type Decode OK  ] Ldr; S:N(3KHz) Early= 2 dB, Full 6.25 dB, Offset= 1 Hz:  "
             "MD Decode;9 ID=HFC, Type=H6A: 16QAM.1000.100.E, D1= 0.00, D2= 0.01")
check("16QAM data frame", a["frame"], "16QAM.1000.100")
m.feed_ardop("[Frame Type Decode Fail] Ldr; S:N(3KHz) Early= -15.15 dB, Full -13.25 dB, Offset= 11.4 Hz:  "
             "MD Decode;5 Type1=H29, Type2=HF6, D1= 0.56, D2= 0.54")
check("a failed decode changes nothing", a["snr_db"], 6.2)
m.feed_ardop("Qual = 89")
m.feed_ardop(" Command Trace TO Host BUSY TRUE")
m.feed_ardop("[Main.KeyPTT]  PTT-TRUE")
m.feed_ardop(" Command Trace TO Host BUFFER 74")
check("Qual, BUSY, PTT, BUFFER", (a["quality"], a["busy"], a["ptt"], a["buffer"]), (89, True, True, 74))
m.feed_ardop("[Main.KeyPTT]  PTT-FALSE")
m.feed_ardop(" Command Trace TO Host NEWSTATE DISC ")
check("DISC: not linked, last partner kept", (a["linked"], a["partner"], a["ptt"]), (False, "N0DAJ", False))

n = len(changes)
m.feed_ardop(" Command Trace TO Host NEWSTATE DISC ")
m.feed_ardop("Some unrelated ardopcf line")
check("a repeat or an unrelated line: no change event", len(changes), n)
check("each change event carries the snapshot", changes[-1]["ardop"]["state"], "DISC")

# --- xara (VARA HF): the text after the timestamp, its seconds first
m2 = modem_status.ModemStatus(clock=lambda: now[0])
m2.feed_xara("     12.010 > Mycall(Ok([\"N9ABC\"]))")
check("xara Mycall learnt", m2.mycall, "N9ABC")
m2.feed_xara("   2466.250 < CONNECTED N9ABC N0DAJ 500")
v = m2.state["varahf"]
check("CONNECTED we called: partner is the other call", (v["partner"], v["bw_hz"], v["linked"]), ("N0DAJ", 500, True))
m2.feed_xara("   2470.000 < CONNECTED N0CALL N9ABC 2300")
check("CONNECTED they called: partner is the caller", (v["partner"], v["bw_hz"]), ("N0CALL", 2300))
m2.feed_xara("  91626.920 < SN 13.1")
m2.feed_xara("  91626.920 < BITRATE (4)  88 bps RX")
check("SN and BITRATE", (v["snr_db"], v["level"], v["bps"], v["rate_dir"]), (13.1, 4, 88, "RX"))
m2.feed_xara("  91627.000 < SN junk")
check("a malformed SN is ignored", v["snr_db"], 13.1)
m2.feed_xara("  50651.660 < BUSY ON")
m2.feed_xara("    225.690 < PTT ON")
m2.feed_xara("    225.700 < BUFFER 12")
check("BUSY, PTT, BUFFER", (v["busy"], v["ptt"], v["buffer"]), (True, True, 12))
m2.feed_xara("  91652.670 < DISCONNECTED")
check("DISCONNECTED: not linked", (v["linked"], v["state"]), (False, "DISCONNECTED"))
m2.feed_xara("      0.060 < IAMALIVE")
check("IAMALIVE changes nothing", v["state"], "DISCONNECTED")

m3 = modem_status.ModemStatus()
m3.feed_xara("   1.0 < CONNECTED N9ABC N0DAJ 500")
check("no call known: the second call", m3.state["varahf"]["partner"], "N0DAJ")

print("all passed" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
