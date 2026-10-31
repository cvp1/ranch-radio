"""The packet hub's receive-only iGate (igate.py, "packet": {"igate": ...}).

  - which heard frames are gated and how each line reads (the APRS-IS iGate
    rules: no TCPIP/TCPXX/NOGATE/RFONLY, no queries, third-party unwrapped,
    info cut at CR/LF, ",qAR,<login>" after the path)
  - the passcode file and the server's host:port
  - the host file renders the iGate onto the hub's command line, on macOS and
    Linux, as a path to the passcode file; a block missing its passcode, or
    one next to Direwolf's own iGate, is refused

The hub at the wire, against a fake APRS-IS server, is
tests/tnc_hub_test.py's scenario_igate.

    python3 tests/igate_test.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import igate  # noqa: E402
from ax25 import Address, Frame, U_SABM  # noqa: E402
from ranch_radio import render  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def heard(src, info, dest="APRS", digis=(), used=0):
    """A frame as the radio hands it over: the first `used` digis have repeated it."""
    f = Frame.decode(Frame.ui(src, dest, info, digis=digis).encode())
    for d in f.digis[:used]:
        d.bit7 = True
    return f


def line(f, login="N9ABC-10"):
    out, _ = igate.gate_line(f, login)
    return out.decode("latin-1") if out is not None else None


# ---------------------------------------------------------------- what is gated
check("a digipeated position: path as heard, last digi starred, then qAR and our login",
      line(heard("KF7ABC-9", b"!4049.00N/10510.00W>test", digis=["K7RDG-1", "WIDE2-1"], used=1)),
      "KF7ABC-9>APRS,K7RDG-1*,WIDE2-1,qAR,N9ABC-10:!4049.00N/10510.00W>test")
check("heard direct", line(heard("KF7ABC-9", b">status")), "KF7ABC-9>APRS,qAR,N9ABC-10:>status")
for bad in ("TCPIP", "TCPXX", "NOGATE", "RFONLY"):
    check(f"not gated: {bad} in the path", line(heard("KF7ABC-9", b">x", digis=[bad])), None)
check("not gated: a query", line(heard("KF7ABC-9", b"?APRS?")), None)
check("not gated: empty info", line(heard("KF7ABC-9", b"")), None)
check("the info stops at the first CR or LF",
      line(heard("KF7ABC-9", b">first line\r\nsecond")), "KF7ABC-9>APRS,qAR,N9ABC-10:>first line")
check("third party: the inner packet, without the RF header",
      line(heard("KI6RVO-2", b"}N1KXJ-14>APSTKA,WIDE1:=4201.46N/07103.52Wu", dest="APDW18")),
      "N1KXJ-14>APSTKA,WIDE1,qAR,N9ABC-10:=4201.46N/07103.52Wu")
check("not gated: third party that came from APRS-IS (TCPIP inside)",
      line(heard("KI6RVO-2", b"}N1KXJ-14>APSTKA,TCPIP,KI6RVO-2*:=4201.46N/07103.52Wu", dest="APDW18")), None)
check("not gated: a malformed third-party packet", line(heard("KI6RVO-2", b"}garbage")), None)
sabm = Frame.decode(Frame(Address.from_text("N9ABC-1"), Address.from_text("KF7ABC"), kind="U",
                          utype=U_SABM).encode())
check("not gated: connected-mode frames", line(sabm), None)
nol3 = heard("KF7ABC-9", b">x")
nol3.pid = 0xCF
check("not gated: a UI frame that isn't APRS (PID other than F0)", line(nol3), None)
_, k1 = igate.gate_line(heard("KF7ABC-9", b">x", digis=["K7RDG-1"], used=1), "N9ABC-10")
_, k2 = igate.gate_line(heard("KF7ABC-9", b">x", digis=["HELIO"], used=1), "N9ABC-10")
check("two digipeaters' copies share one duplicate key", k1 == k2, True)

# ---------------------------------------------------------------- passcode, server
tmp = Path(tempfile.mkdtemp(prefix="igate-"))
(tmp / "ok").write_text("12345\n")
(tmp / "bad").write_text("hunter2\n")
check("passcode read from its file", igate.read_passcode(str(tmp / "ok")), "12345")
for name, path in (("not digits", tmp / "bad"), ("missing", tmp / "nope")):
    try:
        igate.read_passcode(str(path))
        check(f"passcode file {name}: refused", False, True)
    except ValueError:
        check(f"passcode file {name}: refused", True, True)
check("server without a port: 14580", igate.split_server("noam.aprs2.net"), ("noam.aprs2.net", 14580))
check("server with a port", igate.split_server("127.0.0.1:2000"), ("127.0.0.1", 2000))

# ---------------------------------------------------------------- the host file
station = json.loads((ROOT / "examples/hosts/linux-station.json").read_text())
station["packet"]["igate"] = {"login": "N9ABC-10", "passcode": "~/.config/ranch-radio/aprs-is-passcode"}
unit = next((a for a in render.artifacts(station) if a.path.endswith("uvpro-hub.service")), None)
exec_line = next((ln for ln in (unit.content if unit else "").splitlines() if ln.startswith("ExecStart=")), "")
check("linux: the UV-PRO hub runs the iGate", "--igate-login N9ABC-10" in exec_line, True)
check("  the passcode is a file path, not the passcode",
      "--igate-passcode-file %h/.config/ranch-radio/aprs-is-passcode" in exec_line, True)
check("  default server unless the block names one", "--igate-server" in exec_line, False)
station["packet"]["igate"]["server"] = "rotate.aprs2.net"
exec_line = next(ln for ln in next(a for a in render.artifacts(station) if a.path.endswith("uvpro-hub.service"))
                 .content.splitlines() if ln.startswith("ExecStart="))
check("  a named server is passed on", "--igate-server rotate.aprs2.net" in exec_line, True)

mac = json.loads((ROOT / "examples/hosts/mac-aioc.json").read_text())
mac["packet"]["igate"] = mac["packet"]["direwolf"].pop("igate")
hub = next(a for a in render.artifacts(mac, home="/H") if a.path.endswith("org.ranch-radio.packet-hub.plist"))
hargs = hub.content["ProgramArguments"]
check("mac: the hub runs the iGate, with Direwolf as its modem",
      (hargs[hargs.index("--igate-login") + 1], hargs[hargs.index("--igate-passcode-file") + 1])
      if "--igate-login" in hargs else None, ("N9ABC-10", "/H/.config/ranch-radio/aprs-is-passcode"))


def refused(h, change):
    h = json.loads(json.dumps(h))
    change(h)
    try:
        render.validate(h)
        return False
    except render.HostError:
        return True


check("refused: an iGate with no passcode file", refused(station, lambda h: h["packet"]["igate"].pop("passcode")), True)
check("refused: an iGate with no login", refused(station, lambda h: h["packet"]["igate"].pop("login")), True)
check("refused: the hub's iGate and Direwolf's at once",
      refused(mac, lambda h: h["packet"]["direwolf"].update(igate=dict(h["packet"]["igate"]))), True)
check("accepted: the hub's iGate on its own", refused(station, lambda h: None), False)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
