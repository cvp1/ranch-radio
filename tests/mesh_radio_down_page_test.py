"""One Meshtastic node of two not connected, on the station page (station.html)
in headless Chromium: station.py with two tests/fake_mesh_bridge.py nodes,
one of which finds no node, as a real bridge does for a cable that is out.

Seen live 2026-11-22: a node's USB cable was out overnight; its bridge said
"not connected" each minute it retried, and the bar and the Shack showed
all of Mesh down while the other node carried on.

  - both nodes up: the Mesh tab's dot is green, the card says nothing is down
  - LF's node goes away: Mesh stays up (the tab is not greyed), its dot and
    the card turn amber and name LF, "Offline" lists "Mesh · LF" and not Mesh
  - it stays so across LF's bridge retrying (each retry says "not connected")
    and across a reload
  - with LF picked on the Mesh tab (its view says Not connected), the same
  - LF back: green again, nothing listed
  - both down: Mesh is off (its card; an off tab leaves the bar), "Offline"
    lists Mesh once
  - switched off: off, and on again comes back

    python3 tests/mesh_radio_down_page_test.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402
import testkit  # noqa: E402

WEB = testkit.free_port()
CDP = testkit.free_port()
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
TMP = tempfile.mkdtemp(prefix="meshradiodown-")
ABSENT = os.path.join(TMP, "absent")           # the ports whose node "is not there" (fake_mesh_bridge.py)
MARK_A = os.path.join(TMP, "port-mf18")
MARK_B = os.path.join(TMP, "port-lf")
STARTS = os.path.join(TMP, "starts")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:66} got {got!r}" + ("" if ok else f" want {want!r}"))


def absent(*ports):
    with open(ABSENT, "w") as f:
        f.write(" ".join(os.path.basename(p) for p in ports))


def starts():
    try:
        return len(open(STARTS).read().split())
    except OSError:
        return 0


cfg = {"name": "Test Station", "radios": [],
       "mesh": {"port": MARK_A, "label": "MF18", "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py",
                "log": "", "radios": [{"label": "LF", "port": MARK_B, "log": ""}]},
       "addons_state": os.path.join(TMP, "addons.json")}   # never the real ~/.local/state one
cfg_path = os.path.join(TMP, "station.json")
with open(cfg_path, "w") as f:
    json.dump(cfg, f)
absent()

TEXT = "(s) => (document.querySelector(s)?.textContent || '')"
TAB = "[...document.querySelectorAll('#st-nav .st-tab')].find((t) => t.textContent.startsWith('Mesh'))"
CARD = "[...document.querySelectorAll('.st-card')].find((c) => c.querySelector('b')?.textContent === 'Mesh')"
DOT = f"[...{TAB}.querySelector('.st-dot').classList].filter((c) => c !== 'st-dot').join(' ')"
CARD_DOT = f"[...{CARD}.querySelector('.st-dot').classList].filter((c) => c !== 'st-dot').join(' ')"
# The card's tag, but for "N new": the fakes send a message at each start, so unread counts come and go.
TAG = f"({CARD}.querySelector('.mc-tag')?.textContent || '').replace(/^\\d+ new$/, '')"
NEEDS = "([...document.querySelectorAll('#mc-needs .mc-need')].map((e) => e.textContent).find((t) => t.startsWith('Offline')) || '')"


# The fake's node goes away when sent "die" (its bridge exits, as on a pulled cable).
GONE_MF18 = 'send({ t: "mesh_send", text: "die", ch: 0 })'
GONE_LF = 'send({ t: "mesh_send", text: "die", ch: 0, node: "lf" })'


def state():
    return b.js(f"[{DOT}, {TAB}.classList.contains('off'), {TAB}.title, {CARD_DOT}, {TAG}]")


# The bridge's restart waits, shortened: a retry every half second, as the
# real one's every minute.
env = dict(os.environ, FAKE_MESH_ABSENT=ABSENT, FAKE_MESH_PORT_NAMES="1", FAKE_MESH_STARTS=STARTS,
           MESH_RESTART_S="0.3,0.5,0.5")
front = subprocess.Popen([sys.executable, "station.py", "--config", cfg_path, "--listen", str(WEB), "--rigctl-port", "0"],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
b = Browser(CHROME, CDP, size="1500,1000")
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    b.goto(f"http://127.0.0.1:{WEB}/")
    b.wait(f"({TEXT})('#conn b') === 'Connected'")
    b.wait(f"{TAB} && {CARD} && {DOT} === 'ok'")
    check("both nodes up: green dot, tab on, nothing named", state(), ["ok", False, "Meshtastic node", "ok", ""])
    check("  nothing offline", b.js(NEEDS), "")

    absent(MARK_B)
    b.js(GONE_LF)                                         # LF's node goes away; its bridge then finds none
    b.wait(f"{DOT} === 'warn'")
    check("LF's node gone: Mesh stays up, amber, and names LF", state(),
          ["warn", False, "Meshtastic: LF not connected", "warn", "LF not connected"])
    check("  Offline lists LF's node, not Mesh", b.wait(f"{NEEDS}"), "Offline: Mesh · LF")
    n = starts()
    testkit.wait_for(lambda: starts() >= n + 3, 15)       # LF's bridge retried: each try says "not connected"
    check("  it stays so while LF's bridge retries", state(),
          ["warn", False, "Meshtastic: LF not connected", "warn", "LF not connected"])
    # The old page satisfies this wait until it unloads: a mark only it carries (2026-11-24).
    b.js("window.__beforeReload = true; location.reload()")
    b.wait(f"!window.__beforeReload && document.readyState === 'complete' && "
           f"({TEXT})('#conn b') === 'Connected' && {TAB} && {CARD} && {DOT} === 'warn'")
    check("  and across a reload", state(), ["warn", False, "Meshtastic: LF not connected", "warn", "LF not connected"])

    b.js("location.hash = '#/mesh'")
    b.wait("document.querySelectorAll('#mesh-radios .mesh-radio').length === 2")
    b.js("[...document.querySelectorAll('#mesh-radios .mesh-radio')].find((x) => x.textContent.trim().startsWith('LF')).click()")
    check("LF picked on the Mesh tab: its view says Not connected", b.wait(f"({TEXT})('#mesh-state') === 'Not connected' && 'no'"), "no")
    n = starts()
    testkit.wait_for(lambda: starts() >= n + 2, 15)
    b.js("location.hash = '#/home'")
    b.wait(f"{CARD}")
    check("  Mesh is still up, amber", state(), ["warn", False, "Meshtastic: LF not connected", "warn", "LF not connected"])

    absent()
    b.wait(f"{DOT} === 'ok'")
    check("LF back: green, nothing named", state(), ["ok", False, "Meshtastic node", "ok", ""])
    check("  nothing offline", b.js(NEEDS), "")

    absent(MARK_A, MARK_B)
    b.js(GONE_LF)
    b.js(GONE_MF18)
    b.wait(f"!{CARD}")
    # An off tab leaves the bar unless it is the one shown (nav.js tucks it);
    # its card leaves the Shack too (2026-11-23): the Offline chip names it.
    check("both down: Mesh is off (its tab and its card leave, the Offline chip names it)", b.js(f"[!{TAB}, !{CARD}]"),
          [True, True])
    check("  Offline lists Mesh once", b.js(NEEDS), "Offline: Mesh")

    absent()
    b.wait(f"{TAB} && {DOT} === 'ok'")
    b.js('send({ t: "addon", addon: "mesh", do: "power", on: false })')
    check("switched off: off", b.wait(f"!{TAB} && !{CARD} && 'off'"), "off")
    b.js('send({ t: "addon", addon: "mesh", do: "power", on: true })')
    check("  on again: green", b.wait(f"{TAB} && {DOT} === 'ok' && {CARD_DOT} === 'ok' && 'ok'"), "ok")
finally:
    b.close()
    front.terminate()
    try:
        front.wait(5)
    except subprocess.TimeoutExpired:
        front.kill()
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
