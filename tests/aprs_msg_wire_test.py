"""APRS messaging at the wire (plan 2026-11-07, sections 3.2-3.6): the real
tnc_hub.py on tests/fake_kiss_radio.py, its HTTP API, an in-process fake
APRS-IS server, a crash at a named point, and the UV-PRO add-on's hold
against the real hub.

  - contract both ways: POST api/aprs/message stores exactly the text sent
    (non-ASCII, 68 characters and "{" refused) and the frame on the radio
    carries exactly that text and msgno; a heard ack completes it
  - the observer: the login line with the filter; an IS packet becomes a
    port 'is' row; with a positive control first (an RF message to us is
    acked in effective full), the same message as an IS line puts nothing
    on the radio, ever; an IS line with a bare CR never becomes two packets;
    a 10 kB line is dropped and counted; our own frame heard back on RF is
    not gated
  - a hub killed after radio.write and before the slot is recorded sent:
    the next process does not send that slot again
  - G1: with the add-on's qsy() returned (the hold set on the hub) and no
    link and no inbound window, a due retry writes nothing

Nothing here opens real hardware, reaches APRS-IS or transmits.

    .venv/bin/python tests/aprs_msg_wire_test.py
"""
import asyncio
import copy
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
os.environ.pop("CC_OBS_TOKENS_FILE", None)

import tnc_hub_test as T  # noqa: E402
from ax25 import Frame  # noqa: E402

check, check_true, wait_for = T.check, T.check_true, T.wait_for
MSG = ("--aprs-messaging", "full", "--aprs-firmware-acks", "false", "--aprs-cooldown", "0")


def hub_at(radio, logdir, extra=(), crash=None):
    if crash:
        os.environ["RANCH_RADIO_TEST_CRASH_AT"] = crash
    try:
        return T.HubProc(radio, tx=True, extra=("--log-dir", logdir, *extra))
    finally:
        os.environ.pop("RANCH_RADIO_TEST_CRASH_AT", None)


def thread(hub, call):
    return T._get(hub, f"/api/aprs/thread?with={call}")["messages"]


def air(radio, since=0):
    return [Frame.decode(r).tnc2() for _, r in radio.transmitted()[since:]]


def scenario_contract():
    logdir = tempfile.mkdtemp(prefix="aprsw-")
    radio = T.FakeKissRadio().start()
    hub = hub_at(radio, logdir, MSG)
    try:
        st = hub.status()["aprs"]["messaging"]
        check("precondition: effective full, nothing pausing", (st["effective"], st["pause"]), ("full", None))
        for name, text in (("non-ASCII", "café"), ("68 characters", "y" * 68), ("{", "a{b")):
            code, body = hub.post("/api/aprs/message", {"to": "W7TST", "text": text})
            check(f"POST api/aprs/message refuses {name}: 400 with the reason",
                  (code, body.get("error", "").startswith("text:")), (400, True))
        text = "Ranch radio test 1, 2, 3 -- reply if you copy" + "." * 22
        check("  (67 characters)", len(text), 67)
        code, body = hub.post("/api/aprs/message", {"to": "w7tst", "text": text})
        check("67 characters: accepted", (code, body.get("state")), (200, "pending"))
        sent = wait_for(lambda: air(radio), 10)
        check("the frame on the radio carries exactly that text and msgno", sent,
              [f"N9ABC>APZRR0,WIDE1-1,WIDE2-1::W7TST    :{text}{{{body['msgno']}}}"])
        row = next(m for m in thread(hub, "W7TST") if m["id"] == body["id"])
        check("  stored exactly as sent", (row["text"], row["dst"], row["dir"], row["tries"]), (text, "W7TST", "out", 1))
        radio.inject(T.aprs("W7TST", f":N9ABC    :ack{body['msgno']}"))
        got = wait_for(lambda: next(m for m in thread(hub, "W7TST") if m["id"] == body["id"])["state"] == "acked", 10)
        check_true("a heard ack completes it (the thread says acked)", bool(got))
        time.sleep(2.5)
        radio.inject(T.aprs("W7TST", ":N9ABC    :and back{X7"))
        acked = wait_for(lambda: [t for t in air(radio) if ":ackX7" in t], 10)
        check("their message is acked", acked, ["N9ABC>APZRR0,WIDE1-1,WIDE2-1::W7TST    :ackX7"])
        th = thread(hub, "W7TST")
        check("the thread has both directions", [(m["dir"], m.get("via")) for m in th], [("out", "rf"), ("in", "rf")])
        code, body = hub.post("/api/aprs/message", {"to": "W7TST", "text": "x"}, origin="http://evil.example")
        check("a foreign Origin: refused (the cross-site guard)", code, 403)
    finally:
        hub.stop(); radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


class FakeAprsIs:
    """An APRS-IS server on loopback: records what the client sends, answers
    the login, then sends the scripted lines (bytes, sent as they are)."""

    def __init__(self, script=()):
        self.script = list(script)
        self.lines = []
        self.conns = []
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c):
        c.sendall(b"# aprsc 2.1.19 fake\r\n")
        try:
            for raw in c.makefile("rb"):
                text = raw.decode("latin-1").rstrip("\r\n")
                self.lines.append(text)
                if text.startswith("user "):
                    c.sendall(f"# logresp {text.split()[1]} verified, server FAKE\r\n".encode())
        except OSError:
            pass

    def send(self, data):
        for c in self.conns:
            c.sendall(data)

    def packets(self):
        return [ln for ln in self.lines if not ln.startswith("user ")]

    def stop(self):
        self.srv.close()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass


def scenario_observer():
    logdir = tempfile.mkdtemp(prefix="aprsw-")
    pfile = os.path.join(logdir, "passcode")
    with open(pfile, "w") as f:
        f.write("12345\n")
    srv = FakeAprsIs()
    radio = T.FakeKissRadio().start()
    hub = hub_at(radio, logdir, (*MSG, "--igate-login", "N9ABC-10", "--igate-passcode-file", pfile,
                                 "--igate-server", f"127.0.0.1:{srv.port}", "--igate-observe"))
    try:
        ob = wait_for(lambda: (hub.status()["igate"].get("observe") or {}).get("verified")
                      and hub.status()["igate"]["observe"], 15)
        check("login line: our filter for our calls and messages to them", srv.lines[:1],
              ["user N9ABC-10 pass 12345 vers RanchRadio 0.1 filter b/N9ABC* g/N9ABC*"])
        check_true("status: the observer connected and verified, with the filter",
                   bool(ob) and ob["filter"] == "b/N9ABC* g/N9ABC*", str(ob))
        # positive control: an RF message with a msgno, to us, in effective full, is acked
        radio.inject(T.aprs("W7TST", ":N9ABC    :over the air{51"))
        check("positive control: an RF message to N9ABC is acked (effective full)",
              wait_for(lambda: air(radio), 10), ["N9ABC>APZRR0,WIDE1-1,WIDE2-1::W7TST    :ack51"])
        n = len(radio.transmitted())
        srv.send(b"K7XYZ>APRS,TCPIP*,qAC,T2TEST::N9ABC    :from the internet{52\r\n")
        srv.send(b"W7TST>APRS,TCPIP*,qAC,T2TEST::N9ABC    :over the air{51\r\n")
        srv.send(b"K7BAD>APRS,TCPIP*,qAC,T2TEST::N9ABC    :one{53\rK7BAD>APRS::N9ABC    :two{54\r\n")
        srv.send(b"K7BIG>APRS,TCPIP*:>" + b"z" * 10000 + b"\r\n")
        rows = wait_for(lambda: [m for m in thread(hub, "K7XYZ") if m["port"] == "is"], 10)
        check("an IS packet becomes a port 'is' row, shown via internet",
              [(m["text"], m["via"]) for m in rows or []], [("from the internet", "internet")])
        ob = wait_for(lambda: (lambda o: o["too_long"] >= 1 and o)(hub.status()["igate"]["observe"]), 10)
        check("a 10 kB line is dropped and counted", ob and ob["too_long"], 1)
        time.sleep(3)                       # a quiet window: anything the IS lines caused would be on air by now
        check("IS copies of messages to us: nothing on the radio, ever", air(radio, n), [])
        bad = [m for m in thread(hub, "K7BAD")]
        check("a line with a bare CR inside never becomes two packets (it is dropped)",
              ([m["text"] for m in bad], hub.counts().get("is_line_break")), ([], 1))
        st = hub.status()["aprs"]["messaging"]
        check("  and the engine sent no ack for any of them", st.get("acks_sent"), 1)
        radio.inject(T.repeated("N9ABC", ":W7TST    :ack51", ["K7RDG-1", "WIDE2-1"]))
        radio.inject(T.aprs("KD7XYZ", ">someone else"))
        got = wait_for(lambda: [p for p in srv.packets() if "KD7XYZ" in p], 10)
        check("our own frame heard back is not gated; another station's is (qAO)",
              [p for p in srv.packets() if p.startswith(("N9ABC", "KD7XYZ"))], ["KD7XYZ>APRS,qAO,N9ABC-10:>someone else"])
        del got
    finally:
        hub.stop(); radio.stop(); srv.stop()
        shutil.rmtree(logdir, ignore_errors=True)


def scenario_restart_mid_retry():
    logdir = tempfile.mkdtemp(prefix="aprsw-")
    radio = T.FakeKissRadio().start()
    try:
        hub = hub_at(radio, logdir, MSG, crash="aprs-after-write")
        try:
            got = hub.post("/api/aprs/message", {"to": "W7TST", "text": "crash after the write"})
            check("accepted", got[0] if got else None, 200)
            check_true("the hub dies right after handing slot 1 to the radio", bool(wait_for(lambda: hub.p.poll() is not None, 10)))
            check("  the frame reached the radio", len(wait_for(lambda: radio.transmitted(), 5) or []), 1)
        finally:
            hub.stop()
        import sqlite3
        db = sqlite3.connect(f"file:{os.path.join(logdir, 'aprs.db')}?mode=ro", uri=True)
        check("  killed before the slot was recorded sent: the row still says sending",
              db.execute("SELECT state, tries FROM messages WHERE dir='out'").fetchall(), [("sending", 0)])
        db.close()
        hub = hub_at(radio, logdir, MSG)
        try:
            row = wait_for(lambda: next(iter(thread(hub, "W7TST")), None), 10)
            check("the next process counts that slot as tried", (row["state"], row["tries"]), ("pending", 1))
            time.sleep(4)
            check("  and does not send it again", len(radio.transmitted()), 1)
            code, body = hub.post("/api/aprs/message", {"to": "K7XYZ", "text": "positive control"})
            check("positive control: a new message from this process goes out at once",
                  (code, len(wait_for(lambda: len(radio.transmitted()) >= 2 and radio.transmitted(), 10) or [])), (200, 2))
        finally:
            hub.stop()
    finally:
        radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


# ---------------------------------------------------------------- G1: the add-on's hold, the real hub

def stub_addon(hub_url):
    """The UV-PRO add-on in control mode, its bridge stubbed (answers like the
    bridge), its hold going to the real hub over HTTP."""
    from addons import uvpro

    class Host:
        def __init__(self):
            self.notices = []

        def notice(self, msg, level="info", ws=None):
            self.notices.append((level, msg))

        def _broadcast(self, payload):
            pass

        async def _send(self, ws, payload):
            pass

    a = uvpro.UvproAddon(Host(), {"address": "00:11:22:33:44:55", "control": "tcp:127.0.0.1:1", "hub": hub_url,
                                  "packet_status": f"{hub_url}/api/status"})
    a.connected = True
    a.proc = SimpleNamespace(returncode=None, stdin=SimpleNamespace(is_closing=lambda: False))
    ch = lambda i, rx: {"id": i, "name": f"C{i}", "rx_freq": rx, "tx_freq": rx, "rx_mod": "FM", "tx_mod": "FM",  # noqa: E731
                        "rx_sub_audio": "", "tx_sub_audio": "", "power": "high", "bandwidth": "WIDE", "scan": True,
                        "tx_disable": False, "talk_around": False, "mute": False,
                        "fixed": {"freq": False, "bandwidth": False, "power": False}}
    a.channels = [ch(0, 144.39), ch(1, 145.09)]
    a.settings = {"channel_a": 0, "channel_b": 1, "scan": False, "double_channel": 1, "ch_data_lock": False,
                  "auto_relay_en": False, "vox_en": False}
    a.vfos, a.device, a.status = [], {"supports_vfo": False}, {"is_in_tx": False}
    a.exact = {"channels": [0, 1], "settings": True, "beacon": True}

    async def bridge(d):
        def answer():
            if d["cmd"] == "settings":
                a._on_bridge({"ev": "settings", "values": {**a.settings, **(d.get("values") or {})}})
            a._on_bridge({"ev": "done", "ref": d["ref"], "ok": True, "error": None})
        asyncio.get_running_loop().call_soon(answer)
    a._send_bridge = bridge
    return a


def scenario_g1_hold_stops_retry():
    logdir = tempfile.mkdtemp(prefix="aprsw-")
    radio = T.FakeKissRadio().start()
    hub = hub_at(radio, logdir, MSG)
    url = f"http://127.0.0.1:{hub.http}"
    try:
        code, body = hub.post("/api/aprs/message", {"to": "W7TST", "text": "slot 1 must not go"})
        first = wait_for(lambda: radio.transmitted(), 10)
        check("precondition: the first transmission went", (code, len(first or [])), (200, 1))
        t0 = time.time()
        a = stub_addon(url)

        async def tune():
            return await a.qsy(145_090_000)
        r = asyncio.run(tune())
        st = hub.status()
        check("qsy() returned tuned, and the hub holds APRS", (r.ok, [h["by"] for h in st["tx"]["holds"]]),
              (True, ["winlink-qsy"]))
        check("  no link and no inbound window (the case links/windows can't see)", (st["links"], st["inbound"]), ([], None))
        check_true("  the engine says why it pauses", "off-channel hold by winlink-qsy" in (st["aprs"]["messaging"]["pause"] or ""),
                   str(st["aprs"]["messaging"]["pause"]))
        time.sleep(max(0.0, t0 + 33 - time.time()))
        check("slot 1 (t0+30) came due while held: nothing written", len(radio.transmitted()), 1)
        row = next(m for m in thread(hub, "W7TST") if m["id"] == body["id"])
        check("  still one try", (row["state"], row["tries"]), ("pending", 1))

        async def back():
            return await a.restore()
        check("restore() puts the radio back", asyncio.run(back()), (True, None))
        check("  and releases the hold", hub.status()["tx"]["holds"], [])
        code, body = hub.post("/api/aprs/message", {"to": "K7XYZ", "text": "after the restore"})
        check("positive control: after the release a message goes",
              (code, len(wait_for(lambda: len(radio.transmitted()) >= 2 and radio.transmitted(), 10) or [])), (200, 2))
    finally:
        hub.stop(); radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


SCENARIOS = [scenario_contract, scenario_observer, scenario_restart_mid_retry, scenario_g1_hold_stops_retry]

if __name__ == "__main__":
    only = os.environ.get("ONLY", "")
    if os.path.splitext(os.path.basename(__file__))[0] in only.split():
        only = ""
    picked = [sc for sc in SCENARIOS if not only or only in sc.__name__]
    for sc in picked:
        print(f"--- {sc.__name__}")
        try:
            sc()
        except Exception as e:     # noqa: BLE001
            T.fails += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL  {sc.__name__} raised {type(e).__name__}: {e}")
        T.stop_leftovers()
    print(f"\n{'ALL PASS' if not T.fails else f'{T.fails} FAILED'}")
    sys.exit(1 if T.fails else 0)
