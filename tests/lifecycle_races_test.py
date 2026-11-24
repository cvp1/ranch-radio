"""Two lifecycle races from the 2026-11-23 bug bash, in process (no servers,
no ports, no hardware):

  power    an add-on switched off by one page while its stop() still awaits
           its bridge's exit, and on by another page meanwhile: the registry
           runs one transition at a time, so the add-on ends On WITH an engine
           (addons/mesh.py's stop() cleared the engine reference after its
           await, which dropped the engine the overlapping start had made)
  journal  an update journals a file's backup, then dies before moving the
           file there; the retry moves it to its own backup; a rollback puts
           the ORIGINAL back (it skipped the never-written first backup and
           left the new file)

    python3 tests/lifecycle_races_test.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("CC_OBS_TOKENS_FILE", None)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import addons  # noqa: E402
from ranch_radio import updater  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"), flush=True)


# ---------------------------------------------------------------- power
class Host:
    def notice(self, *a, **k):
        pass

    def _broadcast(self, *a, **k):
        pass


class SlowStop:
    """Shaped like addons/mesh.py: start() makes a fresh engine; stop() awaits
    the engine's exit (held on `gate`), then clears the reference."""
    label, page = "Slow", False

    def __init__(self):
        self.engine, self.made, self.gate = None, 0, asyncio.Event()

    def can_stop(self):
        return None

    async def start(self):
        self.made += 1
        self.engine = f"engine {self.made}"

    async def stop(self):
        await self.gate.wait()                  # the bridge's exit
        self.engine = None

    def snapshot(self):
        return {}


async def power_race():
    reg = addons.Addons(Host(), {}, state_path=None)
    a = SlowStop()
    reg.items["slow"] = {"addon": a, "on": True, "error": None, "running": True}
    await a.start()
    off = asyncio.ensure_future(reg.power(None, "slow", False))
    await asyncio.sleep(0.01)                   # the off is awaiting the exit
    on = asyncio.ensure_future(reg.power(None, "slow", True))
    await asyncio.sleep(0.01)
    a.gate.set()                                # the bridge exits
    await asyncio.wait_for(asyncio.gather(off, on), 5)
    rec = reg.items["slow"]
    return rec["on"], rec["running"], a.engine is not None, a.made


on, running, engine, made = asyncio.run(power_race())
check("power: off then on, overlapping: ends On", on, True)
check("  running", running, True)
check("  with an engine (the overlapping stop did not drop it)", engine, True)
check("  a fresh engine was made for the on", made, 2)


# ---------------------------------------------------------------- journal
with tempfile.TemporaryDirectory() as t:
    home = Path(t)
    rel = "Library/LaunchAgents/x.plist"
    p = home / rel
    p.parent.mkdir(parents=True)
    p.write_text("ORIGINAL")
    b1, b2 = home / "bak-1" / rel, home / "bak-2" / rel
    # attempt 1: journals b1, dies before moving the file there
    updater.journal_files(home, "rev", [(rel, str(b1))])
    # attempt 2 (same rev): journals b2, moves the original there, installs the new file
    updater.journal_files(home, "rev", [(rel, str(b2))])
    b2.parent.mkdir(parents=True)
    p.rename(b2)
    p.write_text("NEW")
    j = updater.load_journal(home, "rev", log=lambda *_: None)
    check("journal: the retry's backup is the one recorded", [f["backup"] for f in j["files"]], [str(b2)])
    restored, _ = updater.restore_files(home, j, home / "aside")
    check("  rollback puts the original back", p.read_text(), "ORIGINAL")

    # A first backup that WAS written is the original: a retry never replaces it.
    q = home / "Library/LaunchAgents/y.plist"
    q.write_text("ORIGINAL-Y")
    rel_q = "Library/LaunchAgents/y.plist"
    c1, c2 = home / "c-1" / rel_q, home / "c-2" / rel_q
    updater.journal_files(home, "rev2", [(rel_q, str(c1))])
    c1.parent.mkdir(parents=True)
    q.rename(c1)
    q.write_text("NEW-1")
    updater.journal_files(home, "rev2", [(rel_q, str(c2))])
    j2 = updater.load_journal(home, "rev2", log=lambda *_: None)
    check("journal: a written first backup is kept", [f["backup"] for f in j2["files"]], [str(c1)])

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
