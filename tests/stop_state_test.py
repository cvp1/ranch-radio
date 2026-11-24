"""STOP TX survives a restart (--stop-state), against a simulated rig (--sim,
never hardware).

  1  StopState: no file -> not stopped; a saved latch comes back; a corrupt or
     wrong-shaped file starts stopped, saying why; save writes atomically
  2  a server stopped by a page, then killed (-9), starts again stopped:
     rigctl T 1 refused, the page told who stopped it; Resume is saved, so
     the next start keys again
  3  a corrupt state file: the server starts stopped
  4  a state file that can't be written: STOP TX still latches, and the page
     is told a restart would clear it
  5  render gives each radio its own file
  6  stop_saved (what update/restart trust) says whether the FILE holds the
     latch: true after a saved STOP TX and after a start that loaded one;
     false after a failed save and after a shutdown STOP TX (not saved by
     design) on a clear file, so a restart can't clear a latch the page shows
     (2026-11-23, bug bash: it said only "a path is configured")

    .venv/bin/python tests/stop_state_test.py
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import testkit  # noqa: E402
from server import StopState  # noqa: E402
from ranch_radio import render  # noqa: E402

fails = 0


def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66}" + ("" if ok else f" got {got!r} want {want!r}"))


tmp = Path(tempfile.mkdtemp(prefix="stop-state-"))

# ---------------------------------------------------------------- 1 StopState
f = tmp / "unit" / "r.json"
check("1 no file: not stopped", StopState(str(f)).load() is None)
check("1 no path: not stopped, save does nothing", StopState(None).load() is None and StopState(None).save({}) is None)
StopState(str(f)).save({"at": 1.5, "by": "192.168.1.9"})
check("1 a saved latch comes back", StopState(str(f)).load() == {"at": 1.5, "by": "192.168.1.9"})
check("1 no temp file left", sorted(p.name for p in f.parent.iterdir()) == ["r.json"])
StopState(str(f)).save(None)
check("1 a saved Resume comes back as not stopped", StopState(str(f)).load() is None)
for raw, why in (("{not json", "corrupt"), ('{"stopped": "yes"}', "corrupt"), ("{}", "corrupt"),
                 ('{"stopped": {"at": "x", "by": "y"}}', "corrupt")):
    f.write_text(raw)
    got = StopState(str(f)).load()
    check(f"1 {raw[:28]!r}: starts stopped, saying {why}", bool(got) and why in got["by"])


# ---------------------------------------------------------------- the server
class Page:
    def __init__(self, ws):
        self.ws, self.msgs = ws, []
        self.task = asyncio.ensure_future(self._read())

    async def _read(self):
        async for m in self.ws:
            if m.type == aiohttp.WSMsgType.TEXT:
                self.msgs.append(json.loads(m.data))

    def hello(self):
        return next((d for d in self.msgs if d.get("t") == "hello"), None)

    def notices(self, text):
        return [d["msg"] for d in self.msgs if d.get("t") == "notice" and text in d.get("msg", "")]

    async def wait(self, pred, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            await asyncio.sleep(0.05)
        return False


def rig_cmd(port, line):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.sendall((line + "\n").encode())
        return s.makefile("r").readline().strip()


async def run(state, scenario):
    port, web = testkit.free_port(), testkit.free_port()
    env = dict(os.environ)
    env.pop("CC_OBS_TOKENS_FILE", None)
    proc = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", "ic7300", "--no-audio",
                             "--listen", str(web), "--rigctl-port", str(port), "--stop-state", str(state)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                await asyncio.sleep(0.1)
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"http://127.0.0.1:{web}/ws") as ws:
                page = Page(ws)
                page.web = web
                await page.wait(lambda: page.hello() is not None, 5)
                await asyncio.sleep(3.5)              # past the post-connect arming window
                await scenario(page, port)
                page.task.cancel()
    finally:
        proc.send_signal(signal.SIGKILL)              # a crash: nothing gets to clean up
        out = proc.communicate(timeout=10)[0]
    check("  no server traceback", out[-600:] if "Traceback" in out else "", "")
    return out


async def saved(page):
    """stop_saved as a new connection's hello reports it (what the probe reads)."""
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"http://127.0.0.1:{page.web}/ws") as ws:
            async for m in ws:
                if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("t") == "hello":
                    c = json.loads(m.data)["config"]
                    return [bool(c.get("tx_stopped")), c.get("stop_saved")]


def stopped(page):
    return (page.hello() or {}).get("config", {}).get("tx_stopped")


async def main():
    state = tmp / "stop-tx" / "ic7300.json"

    async def first(page, port):
        check("2 first start: not stopped, rigctl T 1 keys", (stopped(page), rig_cmd(port, "T 1")) == (None, "RPRT 0"))
        rig_cmd(port, "T 0")
        await page.ws.send_str(json.dumps({"t": "tx_stop"}))
        ok = await page.wait(lambda: any(d.get("t") == "tx_stop" and d.get("stopped") for d in page.msgs), 3)
        check("2 STOP TX pressed", ok)
        d = json.loads(state.read_text()) if state.exists() else {}
        check("2 it is on disk before the crash", bool(d.get("stopped")))
        check("6 a saved STOP TX: stop_saved", await saved(page), [True, True])
    await run(state, first)

    async def second(page, port):
        st = stopped(page)
        check("2 after kill -9 and start: still stopped, by whom", (st or {}).get("by"), "127.0.0.1")
        check("2   rigctl T 1 refused", rig_cmd(port, "T 1"), "RPRT -11")
        check("6 started stopped from the file: stop_saved", await saved(page), [True, True])
        await page.ws.send_str(json.dumps({"t": "tx_resume"}))
        ok = await page.wait(lambda: any(d.get("t") == "tx_stop" and d.get("stopped") is None for d in page.msgs), 3)
        check("2 Resume", ok)
        check("2   saved as not stopped", json.loads(state.read_text()), {"stopped": None})
    out = await run(state, second)
    check("2 the log says STOP TX is still on from before", "still on from before this start" in out)

    async def third(page, port):
        check("2 after Resume and start: not stopped, T 1 keys", (stopped(page), rig_cmd(port, "T 1")) == (None, "RPRT 0"))
        rig_cmd(port, "T 0")
    await run(state, third)

    bad = tmp / "bad.json"
    bad.write_text("{half a file")

    async def corrupt(page, port):
        st = stopped(page)
        check("3 corrupt file: starts stopped, saying so", "corrupt" in (st or {}).get("by", ""))
        check("3   rigctl T 1 refused", rig_cmd(port, "T 1"), "RPRT -11")
    await run(bad, corrupt)

    blocker = tmp / "a-file"
    blocker.write_text("")

    async def unwritable(page, port):
        await page.ws.send_str(json.dumps({"t": "tx_stop"}))
        ok = await page.wait(lambda: bool(page.notices("could not be saved")), 3)
        check("4 can't save: the page is told a restart would clear it", ok)
        check("4   STOP TX still holds: rigctl T 1 refused", rig_cmd(port, "T 1"), "RPRT -11")
    await run(blocker / "r.json", unwritable)

    # A path that loads clean (no file) but can't be written: the latch is
    # in memory only, and stop_saved must say so. (Scenario 4's path is under
    # a file, so it loads as unreadable and STARTS stopped: a restart would
    # come back stopped, and stop_saved is rightly true there.)
    ro = tmp / "read-only"
    ro.mkdir()
    ro.chmod(0o555)

    async def failed_save(page, port):
        check("6 a clean start: not stopped", stopped(page), None)
        await page.ws.send_str(json.dumps({"t": "tx_stop"}))
        ok = await page.wait(lambda: bool(page.notices("could not be saved")), 3)
        check("6 a failed save: the page is told", ok)
        check("6   stop_saved false", await saved(page), [True, False])
    if os.geteuid() == 0:
        print("  (root ignores the read-only folder: failed-save case not checked)")
    else:
        await run(ro / "r.json", failed_save)
    ro.chmod(0o755)

    clear = tmp / "stop-tx" / "clear.json"

    async def shutdown_stop(page, port):
        await page.ws.send_str(json.dumps({"t": "tx_stop", "shutdown": True}))
        ok = await page.wait(lambda: any(d.get("t") == "tx_stop" and d.get("stopped") for d in page.msgs), 3)
        check("6 a shutdown STOP TX latches", ok)
        check("6   not on disk, and stop_saved says so", [clear.exists() and bool(json.loads(clear.read_text()).get("stopped")),
                                                         await saved(page)], [False, [True, False]])
        await page.ws.send_str(json.dumps({"t": "tx_stop"}))
        await page.wait(lambda: clear.exists(), 3)
        check("6 then a STOP TX that saves: stop_saved", await saved(page), [True, True])
    await run(clear, shutdown_stop)


asyncio.run(main())

# ---------------------------------------------------------------- 5 render
home = "/home/you"
pth = lambda p: p.replace("~", home, 1)  # noqa: E731
files = []
for rid in ("ic7300", "g90"):
    a = render.server_args({"os": "linux"}, {"id": rid, "device": "/dev/x", "web": 7301}, pth)
    files.append(a[a.index("--stop-state") + 1] if "--stop-state" in a else None)
check("5 render: each radio its own STOP TX file", files,
      [f"{home}/.local/state/radio-remote/stop-tx/ic7300.json", f"{home}/.local/state/radio-remote/stop-tx/g90.json"])

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
