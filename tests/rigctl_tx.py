"""Transmit-safety test for the rigctl listener, against a simulated rig.

Runs the server with TX allowed (--sim, never real hardware) and checks that
rigctl PTT obeys the same safety as the browser:
  - T 1 keys, T 0 unkeys
  - a client that disconnects while keyed is unkeyed at once
  - a client that goes quiet is unkeyed after the keepalive window
  - --tx-timeout cuts a long transmission even while the client keeps polling
  - a second client can't key while the first holds PTT

    .venv/bin/python tests/rigctl_tx.py [rig]
"""
import os
import random
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RIG = sys.argv[1] if len(sys.argv) > 1 else "ic7300"
PORT = 15000 + random.randrange(1000)
WEB = 18000 + random.randrange(1000)
TX_TIMEOUT = 8


class Client:
    def __init__(self):
        self.s = socket.create_connection(("127.0.0.1", PORT), timeout=3)
        self.f = self.s.makefile("r")

    def cmd(self, line, nlines=1):
        self.s.sendall((line + "\n").encode())
        return [self.f.readline().strip() for _ in range(nlines)]

    def close(self):
        self.f.close()       # makefile() holds the socket open until closed
        self.s.close()


def ptt():
    c = Client()
    v = c.cmd("t")[0]
    c.close()
    return v


fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:46} got {got!r}" + ("" if ok else f" want {want!r}"))


server = subprocess.Popen(
    [sys.executable, "server.py", "--sim", "--rig", RIG, "--no-audio", "--listen", str(WEB),
     "--rigctl-port", str(PORT), "--tx-timeout", str(TX_TIMEOUT)],
    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    a = Client()
    check("T 1 keys", a.cmd("T 1"), ["RPRT 0"])
    check("ptt reads 1", a.cmd("t"), ["1"])
    b = Client()
    check("second client can't key", b.cmd("T 1"), ["RPRT -11"])
    b.close()
    check("T 0 unkeys", a.cmd("T 0"), ["RPRT 0"])
    check("ptt reads 0", a.cmd("t"), ["0"])

    check("re-key", a.cmd("T 1"), ["RPRT 0"])
    a.close()
    time.sleep(0.5)
    check("disconnect while keyed -> unkeyed", ptt(), "0")

    q = Client()
    check("quiet client keys", q.cmd("T 1"), ["RPRT 0"])
    time.sleep(6.5)                       # silent past the 5 s keepalive
    check("quiet client -> unkeyed by keepalive", ptt(), "0")
    q.close()

    p = Client()
    check("polling client keys", p.cmd("T 1"), ["RPRT 0"])
    t0 = time.monotonic()
    states = []
    while time.monotonic() - t0 < TX_TIMEOUT + 2:
        states.append(p.cmd("t")[0])      # polling keeps the keepalive fresh
        time.sleep(1)
    check(f"polling client keyed until --tx-timeout {TX_TIMEOUT}s", (states[1], states[-1]), ("1", "0"))
    p.close()

    # --- split, as JTDX/WSJT-X "Rig" split drives it ---
    import asyncio as _aio, json as _json, aiohttp as _aiohttp

    async def radio_state():
        async with _aiohttp.ClientSession() as sess:
            async with sess.ws_connect(f"http://127.0.0.1:{WEB}/ws") as ws:
                return _json.loads((await ws.receive()).data)["state"]

    def rs():
        return _aio.run(radio_state())

    sp = Client()
    sp.cmd("F 14074000")
    check("S 1 VFOB accepted", sp.cmd("S 1 VFOB"), ["RPRT 0"])
    check("split reads on", sp.cmd("s", 2), ["1", "VFOB"])
    check("I sets the split TX frequency", sp.cmd("I 14075500"), ["RPRT 0"])
    check("i reads it back", sp.cmd("i"), ["14075500"])
    time.sleep(0.3)
    st = rs()
    if RIG == "g90":   # no VFO B over CAT: emulated in the server
        check("G90: radio's own split left OFF", st["split"], False)
        check("G90: receive frequency unchanged before keying", st["freq"], 14074000)
        check("T 1 (split)", sp.cmd("T 1"), ["RPRT 0"])
        time.sleep(0.3)
        st = rs()
        check("G90: keyed on the TX frequency", (st["tx"], st["freq"]), (True, 14075500))
        check("G90: f still reports RX while keyed", sp.cmd("f"), ["14074000"])
        check("T 0", sp.cmd("T 0"), ["RPRT 0"])
        time.sleep(0.3)
        st = rs()
        check("G90: back on RX after unkey", (st["tx"], st["freq"]), (False, 14074000))
        sp.cmd("T 1")
        sp.close()
        time.sleep(0.6)
        st = rs()
        check("G90: disconnect while keyed -> unkeyed + RX", (st["tx"], st["freq"]), (False, 14074000))
        q2 = Client()
        # F9: the last client left, so its emulated split went with it.
        check("split cleared after the last client left", q2.cmd("s", 2), ["0", "VFOA"])
        check("S 1 turns emulated split back on", q2.cmd("S 1 VFOB"), ["RPRT 0"])
        check("S 0 turns emulated split off", q2.cmd("S 0 VFOA"), ["RPRT 0"])
        check("split reads off", q2.cmd("s", 2), ["0", "VFOA"])
        q2.close()
    else:              # real split on VFO B
        check("IC-7300: radio split ON, VFO B holds TX", (st["split"], st["freq_b"]), (True, 14075500))
        check("IC-7300: VFO A unchanged", st["freq"], 14074000)
        check("S 0", sp.cmd("S 0 VFOA"), ["RPRT 0"])
        sp.close()

    # --- split TX frequency set via set_freq(VFO_B) before split is on:
    # TX frequency via set_freq(VFO_B) before split is on, in --vfo mode ---
    jx = Client()
    check("vfo mode: F VFOB (TX, split off)", jx.cmd("F VFOB 14075700"), ["RPRT 0"])
    check("vfo mode: F VFOA (RX)", jx.cmd("F VFOA 14074000"), ["RPRT 0"])
    check("vfo mode: S VFOA 1 VFOB", jx.cmd("S VFOA 1 VFOB"), ["RPRT 0"])
    check("vfo mode: f VFOA is RX", jx.cmd("f VFOA"), ["14074000"])
    check("vfo mode: f VFOB is TX", jx.cmd("f VFOB"), ["14075700"])
    if RIG == "g90":
        check("vfo mode: T VFOA 1", jx.cmd("T VFOA 1"), ["RPRT 0"])
        time.sleep(0.3)
        st = rs()
        check("G90 vfo mode: keyed on the VFO B frequency", (st["tx"], st["freq"]), (True, 14075700))
        check("vfo mode: T VFOA 0", jx.cmd("T VFOA 0"), ["RPRT 0"])
        time.sleep(0.3)
        st = rs()
        check("G90 vfo mode: back on RX", (st["tx"], st["freq"]), (False, 14074000))
    else:
        time.sleep(0.3)
        st = rs()
        check("IC-7300 vfo mode: VFO B holds TX, A holds RX",
              (st["split"], st["freq_b"], st["freq"]), (True, 14075700, 14074000))
    jx.cmd("S VFOA 0 VFOA")
    jx.close()
finally:
    server.terminate()
    out = server.communicate(timeout=5)[0]

for line in out.splitlines():
    if "unkeyed" in line or "PTT ON" in line or "Traceback" in line:
        print("   log:", line.split(" ", 2)[-1])
if "Traceback" in out:
    fails += 1
print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
