"""meshcore_bridge.Bridge against a scripted node (no radio, no meshcore package).

  - a contact's route: direct, flood, and two hops split by key hash; the
    favourite flag
  - a direct message with no ACK is tried MAX_TRIES times, the route is
    forgotten before the FLOOD_AFTER-th try, and it ends "no ACK after 3
    tries"; an ACK on the second try settles it delivered with its tries
    and round trip
  - a trace runs out along the route and back the same way (through a
    repeater target and back), refuses a companion in direct range and a
    node with no route
  - a repeater's reply to a remote command becomes a cli_reply, not a
    direct message

    python3 tests/meshcore_bridge_logic_test.py
"""
import asyncio
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD = r'''
import asyncio, json, sys, types
sys.path.insert(0, sys.argv[1])
import meshcore_bridge as mb

out = []
mb.emit = lambda **d: out.append(d)
mb.ACK_WAIT_MIN_S = 0.05

E = types.SimpleNamespace(ERROR="error", MSG_SENT="msg_sent", TRACE_DATA="trace", OK="ok")

class Ev:
    def __init__(self, type, payload=None):
        self.type, self.payload = type, payload or {}

class Dispatcher:
    def __init__(self):
        self.next = {}
    async def wait_for_event(self, etype, attribute_filters=None, timeout=None):
        return self.next.pop(etype, None)

class Commands:
    def __init__(self):
        self.calls, self.n, self.ack_on = [], 0, None
        self.bridge = None
    async def send_msg(self, c, text, ts, attempt=0):
        self.calls.append(("send_msg", attempt))
        self.n += 1
        code = bytes([0, 0, 0, self.n])
        if self.ack_on == attempt:
            async def later():
                await asyncio.sleep(0.01)
                await self.bridge.on_ack(Ev("ack", {"code": code.hex(), "trip_time": 1234}))
            asyncio.ensure_future(later())
        return Ev(E.MSG_SENT, {"type": 0, "expected_ack": code, "suggested_timeout": 0})
    async def reset_path(self, c):
        self.calls.append(("reset_path",))
        return Ev(E.OK)
    async def send_trace(self, tag=None, flags=None, path=None):
        self.calls.append(("send_trace", flags, path))
        return Ev(E.MSG_SENT, {"suggested_timeout": 0})

def mk(cmds):
    mc = types.SimpleNamespace(commands=cmds, dispatcher=Dispatcher(), self_info={})
    b = mb.Bridge(mc, E)
    cmds.bridge = b
    return b

def contact(key, type_, plen, path="", mode=0, flags=0):
    return {"public_key": key, "type": type_, "out_path_len": plen, "out_path": path, "out_path_hash_mode": mode,
            "flags": flags, "adv_name": key[:4], "last_advert": 0, "adv_lat": 0, "adv_lon": 0}

R = {}
R["direct"] = mb.contact(contact("aa" * 32, 1, 0))
R["flood"] = mb.contact(contact("aa" * 32, 1, -1))
R["two"] = mb.contact(contact("aa" * 32, 2, 2, "d4b2", flags=1))
R["two2"] = mb.contact(contact("aa" * 32, 2, 2, "d4d4b2b2", mode=1))

async def main():
    c = Commands(); b = mk(c); out.clear()
    await b.send_dm(1, contact("aa" * 32, 1, 1, "d4"), "hi")
    R["noack_calls"] = c.calls
    R["noack_out"] = [d["ev"] + ":" + str(d.get("error") or "") for d in out]

    c = Commands(); c.ack_on = 1; b = mk(c); out.clear()
    await b.send_dm(2, contact("aa" * 32, 1, 1, "d4"), "hi")
    R["ack_calls"] = c.calls
    R["ack_out"] = [{k: d.get(k) for k in ("ev", "attempts", "trip_ms")} for d in out]

    for name, ct in [("rpt", contact("75" * 32, 2, 1, "d4")), ("chat", contact("aa" * 32, 1, 2, "d4b2")),
                     ("rpt_direct", contact("75" * 32, 2, 0)), ("chat_direct", contact("aa" * 32, 1, 0)),
                     ("none", contact("aa" * 32, 1, -1)), ("two_byte", contact("75" * 32, 2, 1, "d4d4", mode=1))]:
        c = Commands(); b = mk(c)
        b.mc.dispatcher.next[E.TRACE_DATA] = Ev(E.TRACE_DATA, {"path": [{"hash": "d4", "snr": 8.0}, {"snr": 7.5}]})
        try:
            r = await b.req_trace(ct, {})
            R["trace_" + name] = [c.calls[-1][1], c.calls[-1][2], r["hops"][0]["snr"]]
        except ValueError as e:
            R["trace_" + name] = "ValueError: " + str(e)

    b = mk(Commands()); out.clear()
    await b.on_msg(Ev("contact_message", {"type": "PRIV", "txt_type": 1, "pubkey_prefix": "75" * 6, "text": "v1.18.0"}))
    await b.on_msg(Ev("contact_message", {"type": "PRIV", "txt_type": 0, "pubkey_prefix": "75" * 6, "text": "hello"}))
    R["cli"] = [d["ev"] for d in out]

asyncio.run(main())
mb._out.write(json.dumps(R) + "\n")
'''

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


p = subprocess.run([sys.executable, "-c", CHILD, ROOT], capture_output=True, text=True, timeout=60)
try:
    R = json.loads(p.stdout.strip().splitlines()[-1])
except (ValueError, IndexError):
    print(p.stdout, p.stderr)
    raise SystemExit("the child did not report")

check("a direct contact: 0 hops, no path", (R["direct"]["hops"], R["direct"]["path"]), (0, []))
check("no route known: hops None (flood)", (R["flood"]["hops"], R["flood"]["path"]), (None, []))
check("two hops, 1-byte hashes", R["two"]["path"], ["d4", "b2"])
check("  the favourite flag", R["two"]["fav"], True)
check("two hops, 2-byte hashes", R["two2"]["path"], ["d4d4", "b2b2"])

check("no ACK: three tries, the route forgotten before the third", R["noack_calls"],
      [["send_msg", 0], ["send_msg", 1], ["reset_path"], ["send_msg", 2]])
check("  sent once, then not delivered", R["noack_out"], ["sent:", "send_failed:no ACK after 3 tries"])
check("an ACK on the second try: delivered, no third", R["ack_calls"], [["send_msg", 0], ["send_msg", 1]])
check("  with its tries and round trip", R["ack_out"],
      [{"ev": "sent", "attempts": None, "trip_ms": None}, {"ev": "delivered", "attempts": 2, "trip_ms": 1234}])

check("trace to a repeater: out, through it, and back", R["trace_rpt"], [0, "d4,75,d4", 8.0])
check("trace to a companion: to its last repeater and back", R["trace_chat"], [0, "d4,b2,d4", 8.0])
check("trace to a repeater in direct range: just it", R["trace_rpt_direct"], [0, "75", 8.0])
check("trace to a companion in direct range is refused", R["trace_chat_direct"].startswith("ValueError: a companion"),
      True)
check("trace with no route is refused", R["trace_none"].startswith("ValueError: no route"), True)
check("trace with 2-byte hashes", R["trace_two_byte"], [1, "d4d4,7575,d4d4", 8.0])

check("a command reply is a cli_reply; a text is a message", R["cli"], ["cli_reply", "msg"])

print("PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
