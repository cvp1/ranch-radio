"""The Logbook tab's settings (logsync_settings.py, station.py's
"logsync_settings" messages): against fakes only, never a real Wavelog, QRZ
or LoTW.

  view       every field's value; each credential only as set/length/when;
             TQSL's station locations from ~/.tqsl/station_data
  secrets    written atomically, mode 600 (directory 700 when made); refused
             empty, multi-line, with spaces or too long; never in a reply,
             a log line or the station's output
  plan       a change is a host-file diff touching only that member; a value
             render refuses (a bad URL), an unknown field, or no change is
             refused; an apply after the host file changed, or of a spent
             plan, is refused
  apply      the host file and station.json are rewritten (station.json as
             render makes it: drift stays clean) and the sync restarts on the
             new block, the station carrying on
  tests      Wavelog check_auth + station_info (read-only key, missing
             profile), QRZ STATUS (good key, bad key), LoTW (good login, a
             wrong password's web page), TQSL's location; no URL or credential
             in any result

    python3 tests/logsync_settings_test.py
"""
import os as _os
_os.environ["RANCH_RADIO_OFFLINE"] = "1"      # no test reaches the internet (logsync_refdata)

import asyncio
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import testkit  # noqa: E402
from fake_lotw import FakeLotw  # noqa: E402
from fake_qrz import FakeQrz  # noqa: E402
from fake_wavelog import FakeWavelog  # noqa: E402

import logsync  # noqa: E402
import logsync_settings as S  # noqa: E402
from ranch_radio import render  # noqa: E402

fails = 0
WL_KEY, QRZ_KEY, LOTW_PW = "WLKEY-s3cret-7f1e", "QRZKEY-s3cret-22ab", "LOTWPW-s3cret-90cd"


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"), flush=True)


class Bg:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)


BG = Bg()


async def _serve(app):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = testkit.free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


tmp = Path(tempfile.mkdtemp(prefix="logsync-settings-"))
home = tmp / "home"
(home / ".tqsl").mkdir(parents=True)
(home / ".tqsl" / "station_data").write_text('<StationDataFile>\n<StationData name="Home">\n<CALL>N0CALL</CALL>\n'
                                             '</StationData>\n<StationData name="Portable">\n</StationData>\n</StationDataFile>\n')
creds = home / "creds"
creds.mkdir(mode=0o700)
for name, val in (("wavelog-key", WL_KEY), ("qrz-key", QRZ_KEY), ("lotw-password", LOTW_PW)):
    (creds / name).write_text(val + "\n")
    os.chmod(creds / name, 0o600)
tqsl = home / "tqsl"
tqsl.write_text("#!/bin/sh\necho 'TQSL Version 2.8.2 [fake]'\nexit 255\n")
os.chmod(tqsl, 0o755)

wl, qrz = FakeWavelog(WL_KEY), FakeQrz(QRZ_KEY, "N0CALL")
lotw = FakeLotw("N0CALL", LOTW_PW, str(tmp / "lotw.jsonl"))
ports = {}
for name, fake in (("wl", wl), ("qrz", qrz), ("lotw", lotw)):
    _, ports[name] = BG.run(_serve(fake.app()))


def block(**over):
    b = {"call": "N0CALL", "udp": {"bind": "127.0.0.1", "port": testkit.free_port()},
         "ledger": str(tmp / "ledger.db"),
         "timing": {"pull_at_start": False},
         "wavelog": {"url": f"http://127.0.0.1:{ports['wl']}", "station_profile_id": 1, "key_file": str(creds / "wavelog-key")},
         "qrz": {"key_file": str(creds / "qrz-key"), "url": f"http://127.0.0.1:{ports['qrz']}/api"},
         "lotw": {"tqsl": str(tqsl), "location": "Home", "login": "N0CALL", "password_file": str(creds / "lotw-password"),
                  "url": f"http://127.0.0.1:{ports['lotw']}/lotwuser/lotwreport.adi"}}
    b.update(over)
    return b


def no_secret(text, extra=()):
    return not any(s in text for s in (WL_KEY, QRZ_KEY, LOTW_PW, *extra))


# ================================================================ view

v = S.view(block(), tqsl_home=str(home))
vt = json.dumps(v)
check("view: every field, with its value", {f["key"]: f["value"] for f in v["fields"]}["wavelog.station_profile_id"], 1)
check("  each credential as set, length and when; mode 600 is ok",
      {d: (s["set"], s["chars"], s["mode_ok"], "updated" in s) for d, s in v["secrets"].items()},
      {"wavelog": (True, len(WL_KEY), True, True), "qrz": (True, len(QRZ_KEY), True, True), "lotw": (True, len(LOTW_PW), True, True)})
check("  never a credential's value", no_secret(vt), True)
check("  TQSL's station locations", v["locations"], ["Home", "Portable"])
os.chmod(creds / "qrz-key", 0o644)
check("  a credential readable by others is flagged", S.view(block(), str(home))["secrets"]["qrz"]["mode_ok"], False)
os.chmod(creds / "qrz-key", 0o600)
check("  a missing file is not set", S.secret_state(creds / "nope"), {"set": False, "file": str(creds / "nope")})

# ================================================================ secrets

nb = block(qrz={"key_file": str(home / "fresh" / "qrz-key")})
st = S.write_secret(nb, "qrz", "  NEWKEY-abc123\n")
p = home / "fresh" / "qrz-key"
check("write_secret: the value, trimmed, in the file the block names", p.read_text(), "NEWKEY-abc123\n")
check("  mode 600, and the directory it made 700", (stat.S_IMODE(p.stat().st_mode), stat.S_IMODE(p.parent.stat().st_mode)),
      (0o600, 0o700))
check("  no temp file left beside it", sorted(x.name for x in p.parent.iterdir()), ["qrz-key"])
check("  the reply is the state, not the value", ("NEWKEY" in json.dumps(st), st["chars"]), (False, 13))
for bad, why in (("", "empty"), ("two words", "spaces"), ("a\nb", "two lines"), ("x" * 300, "too long")):
    try:
        S.write_secret(nb, "qrz", bad)
        check(f"  refused: {why}", "written", "refused")
    except S.SettingsError as e:
        check(f"  refused: {why}", (p.read_text(), bad[:5] in str(e) if bad.strip() else False), ("NEWKEY-abc123\n", False))
try:
    S.write_secret(nb, "telegram", "x")
    check("  an unknown credential is refused", "written", "refused")
except S.SettingsError:
    check("  an unknown credential is refused", True, True)

# ================================================================ live tests


def run_test(dest, b):
    async def go():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
            return await S.test(dest, b, http, logsync.scrub, tqsl_home=str(home))
    return BG.run(go())


r = run_test("wavelog", block())
check("test Wavelog: key valid, profile named", (r["ok"], "Home (N0CALL)" in r["msg"]), (True, True))
wl.rights = "r"
check("  a read-only key fails, saying so", (lambda r: (r["ok"], "read-only" in r["msg"]))(run_test("wavelog", block())), (False, True))
wl.rights = "rw"
r = run_test("wavelog", block(wavelog={**block()["wavelog"], "station_profile_id": 7}))
check("  a profile Wavelog doesn't have fails, listing what it has", (r["ok"], "no station profile 7" in r["msg"], "1 Home" in r["msg"]),
      (False, True, True))
(creds / "wavelog-key").write_text("WRONG-key-1\n")
r = run_test("wavelog", block())
check("  a wrong key fails", (r["ok"], r["msg"]), (False, "Wavelog refused the API key"))
(creds / "wavelog-key").write_text(WL_KEY + "\n")
r = run_test("qrz", block())
check("test QRZ: key valid", (r["ok"], r["msg"].startswith("key valid")), (True, True))
(creds / "qrz-key").write_text("WRONG-qrz-2\n")
r = run_test("qrz", block())
check("  a wrong key fails", (r["ok"], r["msg"]), (False, "QRZ refused the API key"))
(creds / "qrz-key").write_text(QRZ_KEY + "\n")
r = run_test("lotw", block())
check("test LoTW: login accepted, location found", (r["ok"], "accepted" in r["msg"], "found in TQSL" in r["msg"]), (True, True, True))
r = run_test("lotw", block(lotw={**block()["lotw"], "location": "Mars"}))
check("  a location TQSL doesn't have fails", (r["ok"], "NOT in TQSL" in r["msg"]), (False, True))
(creds / "lotw-password").write_text("WRONG-pw-3\n")
r = run_test("lotw", block())
check("  a wrong password fails", (r["ok"], "didn't accept" in r["msg"]), (False, True))
check("  no result names a URL or a credential",
      [x for x in (run_test(d, block()) for d in ("wavelog", "qrz", "lotw"))
       if not no_secret(x["msg"], ("WRONG-pw-3", "http://"))], [])
(creds / "lotw-password").write_text(LOTW_PW + "\n")
r = run_test("wavelog", block(wavelog={**block()["wavelog"], "url": f"http://127.0.0.1:{testkit.free_port()}"}))
check("  an unreachable Wavelog fails as unreachable", (r["ok"], r["msg"].startswith("couldn't reach it")), (False, True))

# ================================================================ plan / apply

repo = tmp / "repo"
(repo / "hosts").mkdir(parents=True)
h = json.loads((ROOT / "examples" / "hosts" / "mac-shack.json").read_text())
h["host"] = "test"
h["logsync"] = block(ledger=str(tmp / "station-ledger.db"))
host_path = repo / "hosts" / "test.json"
host_path.write_text(json.dumps(h, indent=1) + "\n")
before = host_path.read_text()
plans = S.Plans()
pl = plans.make(str(host_path), {"wavelog.station_profile_id": "2"}, render.validate)
check("plan: a diff of the one member", ([ln for ln in pl["diff"].splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---")],
                                          pl["changes"][0]["to"]),
      (['-   "station_profile_id": 1,', '+   "station_profile_id": 2,'], 2))
check("  the host file isn't touched by a plan", host_path.read_text(), before)
for changes, why in (({"wavelog.url": "ftp://x"}, "a URL render refuses"), ({"udp.port": 1}, "a field that isn't a setting"),
                     ({"wavelog.station_profile_id": "1"}, "no change"), ({"wavelog.station_profile_id": "two"}, "not a number")):
    try:
        plans.make(str(host_path), changes, render.validate)
        check(f"  refused: {why}", "planned", "refused")
    except S.SettingsError:
        check(f"  refused: {why}", True, True)
new_h = plans.apply(pl["id"])
check("apply: the host file has the change and nothing else",
      (new_h["logsync"]["wavelog"]["station_profile_id"], {k: v for k, v in new_h.items() if k != "logsync"} == {k: v for k, v in h.items() if k != "logsync"}),
      (2, True))
try:
    plans.apply(pl["id"])
    check("  a spent plan is refused", "applied", "refused")
except S.SettingsError:
    check("  a spent plan is refused", True, True)
pl2 = plans.make(str(host_path), {"lotw.location": "Portable"}, render.validate)
host_path.write_text(host_path.read_text().replace('"login": "N0CALL"', '"login": "N0CALL" '))
try:
    plans.apply(pl2["id"])
    check("  a plan made before someone else edited the host file is refused", "applied", "refused")
except S.SettingsError as e:
    check("  a plan made before someone else edited the host file is refused", "changed since" in str(e), True)
(home / ".config" / "radio-remote").mkdir(parents=True, exist_ok=True)
nb = S.write_station_json(json.loads(host_path.read_text()), home=str(home))
art = [a for a in render.artifacts(json.loads(host_path.read_text()), str(home)) if a.path.endswith("station.json")][0]
check("write_station_json: what render makes (drift stays clean)",
      render.same(art, (home / ".config" / "radio-remote" / "station.json").read_bytes()), True)
check("  and returns the new logsync block", nb["wavelog"]["station_profile_id"], 2)

# ================================================================ the station, end to end

shome = tmp / "shome"
(shome / ".tqsl").mkdir(parents=True)
shutil.copy(home / ".tqsl" / "station_data", shome / ".tqsl" / "station_data")
(shome / ".config" / "radio-remote").mkdir(parents=True)
h2 = dict(h, logsync=block(ledger=str(tmp / "e2e-ledger.db")))
(repo / "hosts" / "test.json").write_text(json.dumps(h2, indent=1) + "\n")
web_p, radio_web, rig = testkit.free_port(), testkit.free_port(), testkit.free_port()
radio = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio", "--host", "127.0.0.1",
                          "--listen", str(radio_web), "--rigctl-host", "127.0.0.1", "--rigctl-port", str(rig)],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
children = [radio]


def _reap():                                  # any exit but the clean os._exit: no child outlives the test
    for p in children:
        if p.poll() is None:
            p.kill()


import atexit
atexit.register(_reap)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))   # a runner's SIGTERM unwinds through atexit
cfg = {"name": "test", "radios": [{"id": "ic7300", "name": "IC-7300", "web": radio_web, "rigctl": rig}],
       "addons": {"logbook": {"enabled": True, "url": "https://192.168.1.20:8087/"}},
       "addons_state": str(tmp / "addons.json"), "logsync": h2["logsync"], "host_file": "test"}
(shome / ".config" / "radio-remote" / "station.json").write_text(json.dumps(cfg))
env = {**os.environ, "HOME": str(shome), "RANCH_RADIO_REPO": str(repo)}
env.pop("CC_OBS_TOKENS_FILE", None)
st = subprocess.Popen([sys.executable, "station.py", "--config", str(shome / ".config" / "radio-remote" / "station.json"),
                       "--host", "127.0.0.1", "--listen", str(web_p), "--rigctl-port", "0"],
                      cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
children.append(st)


def _open(p):
    import socket
    try:
        socket.create_connection(("127.0.0.1", p), 0.2).close()
        return True
    except OSError:
        return False


testkit.wait_for(lambda: _open(web_p), 20)
NEW_KEY = "WLKEY-rotated-9a9a"


async def session():
    got = []
    async with aiohttp.ClientSession() as s, s.ws_connect(f"http://127.0.0.1:{web_p}/ws") as ws:
        await ws.receive(timeout=10)                              # hello

        async def ask(m, want_do, timeout=15):
            await ws.send_str(json.dumps(m))
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                msg = await ws.receive(timeout=timeout)
                got.append(msg.data)
                d = json.loads(msg.data)
                if d.get("t") == "logsync_settings" and d.get("do") == want_do:
                    return d
            return None
        out = {"get": await ask({"t": "logsync_settings", "do": "get"}, "get")}
        out["secret"] = await ask({"t": "logsync_settings", "do": "secret", "dest": "wavelog", "value": NEW_KEY}, "secret")
        wl.key = NEW_KEY
        out["test"] = await ask({"t": "logsync_settings", "do": "test", "dest": "wavelog"}, "test")
        out["plan"] = await ask({"t": "logsync_settings", "do": "plan", "changes": {"lotw.location": "Portable"}}, "plan")
        pid = (out["plan"] or {}).get("plan", {}).get("id", "")
        out["apply"] = await ask({"t": "logsync_settings", "do": "apply", "id": pid}, "apply", timeout=30)
        await asyncio.sleep(1.0)
        out["bad"] = await ask({"t": "logsync_settings", "do": "plan", "changes": {"wavelog.url": "ftp://x"}}, "plan")
    out["all"] = got
    return out


o = BG.run(session(), timeout=90)
check("station: get answers with the settings", [f["key"] for f in o["get"]["view"]["fields"]][:2], ["wavelog.url", "wavelog.station_profile_id"])
check("  secret: written, the reply only its state",
      ((creds / "wavelog-key").read_text().strip(), o["secret"]["state"]["chars"], "error" in o["secret"]), (NEW_KEY, len(NEW_KEY), False))
check("  test after the change: the new key is used", o["test"]["result"]["ok"], True)
check("  plan: the diff comes to the page", '"location": "Portable"' in o["plan"]["plan"]["diff"], True)
check("  apply: the host file and station.json changed, the sync restarted",
      (o["apply"].get("applied"), json.loads((repo / "hosts" / "test.json").read_text())["logsync"]["lotw"]["location"],
       json.loads((shome / ".config" / "radio-remote" / "station.json").read_text())["logsync"]["lotw"]["location"]),
      (True, "Portable", "Portable"))
check("  a bad value: refused, with the reason", ("error" in o["bad"], "plan" in o["bad"]), (True, False))
check("  no reply to any page carried a credential", no_secret("\n".join(o["all"]), (NEW_KEY,)), True)
st.send_signal(signal.SIGTERM)
try:
    st.wait(15)
except subprocess.TimeoutExpired:
    st.kill()
out = st.stdout.read()
check("  the station's output has no credential and no traceback", (no_secret(out, (NEW_KEY,)), "Traceback" in out), (True, False))
check("  and says the sync restarted on the new settings", "restarting the sync" in out, True)
radio.send_signal(signal.SIGTERM)
try:
    radio.wait(10)
except subprocess.TimeoutExpired:
    radio.kill()

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'all ok' if not fails else f'{fails} FAILED'}", flush=True)
os._exit(1 if fails else 0)
