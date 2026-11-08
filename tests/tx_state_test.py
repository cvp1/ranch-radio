"""The packet hub's transmit state at the wire: the operator's stop survives a
restart, the off-channel holds, and the gate's classes and reserves.

The real tnc_hub.py on tests/fake_kiss_radio.py, driven over KISS, AGWPE and
its web API (tests/tnc_hub_test.py's harness). Crashes are the hub's own
os._exit at a named point (RANCH_RADIO_TEST_CRASH_AT). Never real hardware.

    .venv/bin/python tests/tx_state_test.py          (ONLY=<name part> to pick one)
"""
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
os.environ.pop("CC_OBS_TOKENS_FILE", None)

import tnc_hub_test as T  # noqa: E402
from ax25 import Frame  # noqa: E402
from tnc_hub import ui_airtime_s  # noqa: E402

check, check_true, wait_for = T.check, T.check_true, T.wait_for


def state_file(logdir):
    try:
        with open(os.path.join(logdir, "tx-state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def hub_at(radio, logdir, tx=True, crash=None, extra=()):
    old = os.environ.get("RANCH_RADIO_TEST_CRASH_AT")
    if crash:
        os.environ["RANCH_RADIO_TEST_CRASH_AT"] = crash
    else:
        os.environ.pop("RANCH_RADIO_TEST_CRASH_AT", None)
    try:
        return T.HubProc(radio, tx=tx, extra=("--log-dir", logdir, "--no-aprs", *extra))
    finally:
        if old is None:
            os.environ.pop("RANCH_RADIO_TEST_CRASH_AT", None)
        else:
            os.environ["RANCH_RADIO_TEST_CRASH_AT"] = old


def post_or_none(hub, path, body):
    """(status, body), or None when the hub never answered (it crashed)."""
    try:
        return hub.post(path, body)
    except (urllib.error.URLError, ConnectionError, OSError):
        return None


def kiss_frame(hub, radio, text):
    """Send one app UI frame over KISS; True if it reached the radio."""
    k = T.Kiss(hub.kiss)
    try:
        T.clients_ready(hub, kiss=1)
        n = len(radio.transmitted())
        before = hub.counts().get("tx_frames", 0) + hub.counts().get("tx_refused", 0)
        k.send(T.aprs("N9ABC-7", text))
        T.processed(hub, before + 1)
        T.on_air(radio, hub)
        return len(radio.transmitted()) > n
    finally:
        k.close()


# ---------------------------------------------------------------- the stop

def scenario_stop_survives_restart():
    logdir = tempfile.mkdtemp(prefix="txstate-")
    radio = T.FakeKissRadio().start()
    try:
        hub = hub_at(radio, logdir)
        try:
            st = hub.status()["tx"]
            check("no state file: transmit follows --tx (on)", (st["enabled"], st["inhibit"]), (True, False))
            check_true("  positive control: an app frame goes out", kiss_frame(hub, radio, ">before off"))
            check("Off: 200", hub.set_tx(False), (200, {"enabled": False}))
            check("  the inhibit is on disk", (state_file(logdir) or {}).get("tx_inhibit"), True)
        finally:
            hub.stop()
        hub = hub_at(radio, logdir)
        try:
            st = hub.status()["tx"]
            check("restart with --tx and the inhibit set: off", (st["enabled"], st["inhibit"]), (False, True))
            check_true("  the page is told why", "stopped by the operator" in (st["notice"] or ""), str(st["notice"]))
            check("  an app frame is refused", kiss_frame(hub, radio, ">while inhibited"), False)
            check("On clears it", hub.set_tx(True), (200, {"enabled": True}))
            check("  on disk too", (state_file(logdir) or {}).get("tx_inhibit"), False)
        finally:
            hub.stop()
        hub = hub_at(radio, logdir)
        try:
            check("restart after On: transmitting", hub.status()["tx"]["enabled"], True)
        finally:
            hub.stop()
    finally:
        radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


def scenario_stop_write_fails():
    logdir = tempfile.mkdtemp(prefix="txstate-")
    radio = T.FakeKissRadio().start()
    hub = hub_at(radio, logdir)
    try:
        check_true("positive control: transmit on, an app frame goes out", kiss_frame(hub, radio, ">ok"))
        os.mkdir(os.path.join(logdir, "tx-state.json"))     # the rename onto it fails
        code, body = hub.set_tx(False)
        check("Off with the write failing: 500, transmit off", (code, body.get("enabled")), (500, False))
        check_true("  the answer says it isn't persisted", "not persisted" in body.get("error", ""), str(body))
        check("  the gate is off: an app frame is refused", kiss_frame(hub, radio, ">after a failed write"), False)
        check_true("  status shows the warning", "not persisted" in (hub.status()["tx"]["notice"] or ""))
        code, body = hub.set_tx(True)
        check("On with the write failing: refused, stays off", (code, body.get("enabled")), (500, False))
    finally:
        hub.stop(); radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


def scenario_stop_crash_points():
    for point, want_inhibit in (("tx-off-after-fsync", True), ("tx-off-before-fsync", False)):
        logdir = tempfile.mkdtemp(prefix="txstate-")
        radio = T.FakeKissRadio().start()
        try:
            hub = hub_at(radio, logdir, crash=point)
            try:
                check(f"{point}: positive control, transmitting before the Off", hub.status()["tx"]["enabled"], True)
                got = post_or_none(hub, "/api/tx", {"enabled": False})
                check(f"{point}: the client never saw a 200", got is not None and got[0] == 200, False)
                check_true(f"{point}:   the hub died at the point", bool(wait_for(lambda: hub.p.poll() is not None, 10)))
            finally:
                hub.stop()
            hub = hub_at(radio, logdir)
            try:
                st = hub.status()["tx"]
                check(f"{point}: after restart, inhibited = {want_inhibit}", (st["inhibit"], st["enabled"]),
                      (want_inhibit, not want_inhibit))
            finally:
                hub.stop()
        finally:
            radio.stop()
            shutil.rmtree(logdir, ignore_errors=True)


def scenario_stop_corrupt_file():
    logdir = tempfile.mkdtemp(prefix="txstate-")
    with open(os.path.join(logdir, "tx-state.json"), "w") as f:
        f.write("{not json")
    radio = T.FakeKissRadio().start()
    hub = hub_at(radio, logdir)
    try:
        st = hub.status()["tx"]
        check("a corrupt state file: transmit starts off", st["enabled"], False)
        check_true("  with a notice", "corrupt" in (st["notice"] or ""), str(st["notice"]))
        check("  an app frame is refused", kiss_frame(hub, radio, ">corrupt"), False)
        check("  On works (started with --tx)", hub.set_tx(True), (200, {"enabled": True}))
        check_true("  and then a frame goes", kiss_frame(hub, radio, ">after on"))
    finally:
        hub.stop(); radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


def connect_frames(hub, radio, sim_call="W7TST-10"):
    """AGWPE connect to a deaf station; the frames that went on air for it."""
    a = T.Agw(hub.agw)
    try:
        a.send("X", "N9ABC-10")
        a.wait("X")
        n = len(radio.transmitted())
        a.send("C", "N9ABC-10", sim_call)
        time.sleep(1.0)
        T.on_air(radio, hub)
        return [Frame.decode(r).name() for _, r in radio.transmitted()[n:]], a.got("d")
    finally:
        a.close()


def scenario_inhibit_blocks_winlink():
    logdir = tempfile.mkdtemp(prefix="txstate-")
    with open(os.path.join(logdir, "tx-state.json"), "w") as f:
        json.dump({"tx_inhibit": True, "t": 0, "by": "test", "holds": []}, f)
    radio = T.FakeKissRadio().start()
    hub = hub_at(radio, logdir)
    try:
        names, d = connect_frames(hub, radio)
        check("inhibited: a Winlink connect puts nothing on air", names, [])
        check_true("  the AGWPE client is told transmit is off", bool(d) and b"transmit is off" in d[0][3], repr(d))
        hub.set_tx(True)
        names, _ = connect_frames(hub, radio)
        check_true("positive control after On: the connect sends SABM", "SABM" in names, str(names))
    finally:
        hub.stop(); radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


# ---------------------------------------------------------------- holds

def scenario_holds():
    logdir = tempfile.mkdtemp(prefix="txstate-")
    radio = T.FakeKissRadio().start()
    try:
        hub = hub_at(radio, logdir)
        try:
            code, body = hub.post("/api/hold", {"by": "winlink-qsy", "why": "Winlink on 145.090", "back": "channel 3"})
            check("hold set from this machine: 200", (code, [h["by"] for h in body.get("holds", [])]),
                  (200, ["winlink-qsy"]))
            check("  on disk", [h["by"] for h in (state_file(logdir) or {}).get("holds", [])], ["winlink-qsy"])
            check("  shown in status", [h["back"] for h in hub.status()["tx"]["holds"]], ["channel 3"])
            check("a bad holder name: 400", hub.post("/api/hold", {"by": "Bad Name"})[0], 400)
            check_true("a hold never blocks an app frame", kiss_frame(hub, radio, ">during a hold"))
        finally:
            hub.stop()
        hub = hub_at(radio, logdir)
        try:
            check("hub restart keeps the hold", [h["by"] for h in hub.status()["tx"]["holds"]], ["winlink-qsy"])
            code, body = hub.post("/api/hold/release", {"by": "winlink-qsy", "operator": True})
            check("release (the operator, from the page): 200, gone", (code, body.get("holds")), (200, []))
            with open(os.path.join(logdir, "tx.jsonl")) as f:
                rel = [json.loads(ln) for ln in f if "hold-released" in ln]
            check("  logged", [(r["by"], r["who"].split()[0]) for r in rel], [("winlink-qsy", "operator")])
            check("  releasing it again: 404", hub.post("/api/hold/release", {"by": "winlink-qsy"})[0], 404)
            check("  off disk", (state_file(logdir) or {}).get("holds"), [])
        finally:
            hub.stop()
    finally:
        radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


def scenario_hold_from_lan_refused():
    lan = T.lan_address()
    if not lan:
        print("skip  hold from another machine: no network address here")
        return
    logdir = tempfile.mkdtemp(prefix="txstate-")
    radio = T.FakeKissRadio().start()
    hub = hub_at(radio, logdir, extra=("--http-bind", "0.0.0.0"))
    try:
        try:
            import urllib.request
            urllib.request.urlopen(f"http://{lan}:{hub.http}/api/status", timeout=3).close()
        except OSError as e:
            print(f"skip  hold from another machine: this machine's own LAN address doesn't answer ({e})")
            return
        check("positive control: a hold from loopback is set", hub.post("/api/hold", {"by": "a"})[0], 200)
        check("a hold from another machine: 403", hub.post("/api/hold", {"by": "b"}, host=lan)[0], 403)
        check("  and none was added", [h["by"] for h in hub.status()["tx"]["holds"]], ["a"])
    finally:
        hub.stop(); radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


def scenario_hold_never_blocks_winlink():
    logdir = tempfile.mkdtemp(prefix="txstate-")
    radio = T.FakeKissRadio().start()
    sim = T.SimStation(radio, mode="echo")
    hub = hub_at(radio, logdir)
    try:
        hub.post("/api/hold", {"by": "winlink-qsy", "why": "test"})
        a = T.Agw(hub.agw)
        a.send("X", "N9ABC-10")
        a.wait("X")
        a.send("C", "N9ABC-10", "W7TST-10")
        c = a.wait("C", 20)
        check_true("held: a Winlink link still connects", bool(c))
        a.send("D", "N9ABC-10", "W7TST-10", b"hello through a hold", pid=0xF0)
        got = wait_for(lambda: b"".join(d[3] for d in a.got("D")) == b"hello through a hold", 20)
        check_true("  I-frames go out and the echo comes back", bool(got))
        T.on_air(radio, hub)
        names = [Frame.decode(r).name() for _, r in radio.transmitted()]
        check_true("  the I-frame was on air", any(n.startswith("I") for n in names), str(names))
        a.close()
    finally:
        hub.stop(); sim.stop(); radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


# ---------------------------------------------------------------- the gate

def scenario_gate_overall_reserve():
    """The overall cap keeps its last frames for acks: app frames stop at
    total - reserve. (Acks are the engine's; tests/aprs_msg_test.py sends them
    past a saturated gate.)"""
    logdir = tempfile.mkdtemp(prefix="txstate-")
    radio = T.FakeKissRadio().start()
    hub = hub_at(radio, logdir, extra=("--max-frames-per-min", "9", "--max-ui-per-min", "100",
                                       "--max-ui-total-per-min", "100", "--max-airtime-s-per-min", "100"))
    try:
        k = T.Kiss(hub.kiss)
        T.clients_ready(hub, kiss=1)
        for i in range(9):
            k.send(T.aprs("N9ABC-7", f">burst {i}"))
        T.processed(hub, 9)
        T.on_air(radio, hub)
        check("overall 9/min with 4 kept for acks: 5 app frames go", len(radio.transmitted()), 5)
        with open(os.path.join(logdir, "tx.jsonl")) as f:
            why = [json.loads(ln)["why"] for ln in f if '"refused"' in ln]
        check_true("  the refusal names the cap and the reserve",
                   bool(why) and "9 frames/min" in why[0] and "kept for acks" in why[0], str(why[:1]))
        k.close()
    finally:
        hub.stop(); radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


def scenario_gate_airtime():
    logdir = tempfile.mkdtemp(prefix="txstate-")
    radio = T.FakeKissRadio().start()
    hub = hub_at(radio, logdir, extra=("--max-ui-per-min", "6"))
    try:
        k = T.Kiss(hub.kiss)
        T.clients_ready(hub, kiss=1)
        big = ">" + "x" * 370
        raw = T.aprs("N9ABC-7", big)
        per = ui_airtime_s(len(raw))
        for i in range(5):
            k.send(T.aprs("N9ABC-7", big[:-1] + str(i)))
        T.processed(hub, 5)
        T.on_air(radio, hub)
        n = len(radio.transmitted())
        check(f"five {len(raw)}-byte app frames ({per:.2f} s each): airtime stops them at 12 s", n, int(12 // per))
        with open(os.path.join(logdir, "tx.jsonl")) as f:
            why = [json.loads(ln)["why"] for ln in f if '"refused"' in ln]
        check_true("  refused by the airtime cap, which says so", bool(why) and why[0].startswith("UI airtime limit"),
                   str(why[:1]))
        u = hub.status()["tx"]["used"]
        check("  status shows the app frames and their airtime", (u["app"], round(u["ui_airtime_s"], 1)),
              (n, round(n * per, 1)))
        k.close()
    finally:
        hub.stop(); radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


def scenario_gate_u_frames_are_app():
    """A U frame that isn't a link's (here a U_TEST) counts as app UI."""
    from ax25 import Address, U_TEST
    logdir = tempfile.mkdtemp(prefix="txstate-")
    radio = T.FakeKissRadio().start()
    hub = hub_at(radio, logdir, extra=("--max-ui-per-min", "2"))
    try:
        k = T.Kiss(hub.kiss)
        T.clients_ready(hub, kiss=1)
        test = Frame(Address.from_text("W7TST"), Address.from_text("N9ABC-7"), kind="U", utype=U_TEST, info=b"t")
        for _ in range(3):
            k.send(test.encode())
        T.processed(hub, 3)
        T.on_air(radio, hub)
        check("three U_TEST frames with the app cap at 2: 2 go", len(radio.transmitted()), 2)
        check("  counted as app", hub.status()["tx"]["used"]["app"], 2)
        k.close()
    finally:
        hub.stop(); radio.stop()
        shutil.rmtree(logdir, ignore_errors=True)


def unit_airtime():
    check("airtime formula: 100 bytes, 300 ms TX delay", round(ui_airtime_s(100, 300), 4),
          round((16 + 102 * 8 * 1.2) / 1200 + 0.3, 4))
    check("  stuffing counted at the worst rate (1.2)", round(ui_airtime_s(100, 0) * 1200, 1), round(16 + 102 * 9.6, 1))
    check("  TX delay added once per frame", round(ui_airtime_s(10, 500) - ui_airtime_s(10, 0), 3), 0.5)


SCENARIOS = [unit_airtime, scenario_stop_survives_restart, scenario_stop_write_fails, scenario_stop_crash_points,
             scenario_stop_corrupt_file, scenario_inhibit_blocks_winlink, scenario_holds,
             scenario_hold_from_lan_refused, scenario_hold_never_blocks_winlink, scenario_gate_overall_reserve,
             scenario_gate_airtime, scenario_gate_u_frames_are_app]

if __name__ == "__main__":
    only = os.environ.get("ONLY", "")
    if os.path.splitext(os.path.basename(__file__))[0] in only.split():
        only = ""
    picked = [sc for sc in SCENARIOS if not only or only in sc.__name__]
    if not picked:
        T.fails += 1
        print(f"FAIL  ONLY={only!r} matches no scenario")
    for sc in picked:
        print(f"--- {sc.__name__}")
        try:
            sc()
        except Exception as e:     # noqa: BLE001
            T.fails += 1
            print(f"FAIL  {sc.__name__} raised {type(e).__name__}: {e}")
        T.stop_leftovers()
    print(f"\n{'ALL PASS' if not T.fails else f'{T.fails} FAILED'}")
    sys.exit(1 if T.fails else 0)
