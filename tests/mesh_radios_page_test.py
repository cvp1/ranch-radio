"""The Mesh tab with two Meshtastic nodes (mesh-radios, 2026-11-21), driven
in headless Chromium against server.py --sim and two
tests/fake_mesh_bridge.py processes (named after their ports).

  - a switcher beside "Mesh": one button per node, the first picked
  - picking LF shows LF's node and sends LF's commands (the bridge's log)
  - a message from the other node only counts on its button; showing it
    clears the count
  - the pick survives a reload
  - one node only: no switcher

    python3 tests/mesh_radios_page_test.py
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
TMP = tempfile.mkdtemp(prefix="meshradiospage-")
CMD_LOG = os.path.join(TMP, "commands.jsonl")
MARK_A = os.path.join(TMP, "port-mf18")
MARK_B = os.path.join(TMP, "port-lf")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def start(two=True):
    mesh = {"port": MARK_A, "label": "MF18", "python": sys.executable, "bridge": "tests/fake_mesh_bridge.py", "log": ""}
    if two:
        mesh["radios"] = [{"label": "LF", "port": MARK_B, "log": ""}]
    adds = os.path.join(TMP, "addons-cfg.json")
    with open(adds, "w") as f:
        json.dump({"mesh": mesh}, f)
    env = dict(os.environ, FAKE_MESH_LOG=CMD_LOG, FAKE_MESH_PORT_NAMES="1")
    p = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB), "--rigctl-port", "0",
                          "--addons-state", os.path.join(TMP, "state.json"), "--addons", adds],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    raise SystemExit("server did not start")


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


def got(text):
    try:
        with open(CMD_LOG) as f:
            return [c.get("_port") for c in map(json.loads, filter(str.strip, f)) if c.get("text") == text]
    except OSError:
        return []


if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")

TEXT = "(s) => (document.querySelector(s)?.textContent || '')"
BTNS = "[...document.querySelectorAll('#mesh-radios .mesh-radio')]"
ME = f"({TEXT})('#mesh-me')"


def btn(label):
    return f"{BTNS}.find((b) => b.textContent.trim().startsWith({json.dumps(label)}))"


def say(text, ch=0):
    """Send from the compose box, as the operator does."""
    b.js(f"(() => {{ const i = document.querySelector('#mesh-text'); i.value = {json.dumps(text)}; "
         "i.dispatchEvent(new Event('input')); document.querySelector('#mesh-form').requestSubmit(); })()")


b = Browser(CHROME, CDP)
try:
    server = start()
    try:
        time.sleep(1)
        b.goto(f"http://127.0.0.1:{WEB}/?v=mesh#/mesh")
        b.wait(f"{BTNS}.length === 2")
        check("a switcher beside Mesh: one button per node", b.js(f"{BTNS}.map((x) => x.textContent.trim())"), ["MF18", "LF"])
        check("  the first node is picked and shown", (b.js(f"{btn('MF18')}.getAttribute('aria-selected')"),
                                                      "port-mf18" in (b.wait(f"{ME}.includes('port-mf18') && {ME}") or "")),
              ("true", True))
        b.wait(f"{btn('LF')}.querySelector('.mesh-radio-dot.ok')")
        check("  each button says if its node is connected", b.js(f"{BTNS}.every((x) => !!x.querySelector('.mesh-radio-dot.ok'))"), True)

        b.js(f"{btn('LF')}.click()")
        check("picking LF shows LF's node", "port-lf" in (b.wait(f"{ME}.includes('port-lf') && {ME}") or ""), True)
        check("  LF's button is the picked one", b.js(f"{btn('LF')}.getAttribute('aria-selected')"), "true")
        b.wait("!document.querySelector('#mesh-text').disabled")
        say("hello lf")
        time.sleep(1.0)
        check("  a message sent there goes out on LF only", [os.path.basename(x) for x in got("hello lf")], ["port-lf"])

        # Switch Mesh off and on while LF is shown: both nodes start again and
        # each says its start-up message; MF18's counts on MF18's button only.
        b.js('send({ t: "addon", addon: "mesh", do: "power", on: false })')
        time.sleep(1.0)
        b.js('send({ t: "addon", addon: "mesh", do: "power", on: true })')
        check("after Switch on, the view stays on LF", "port-lf" in (b.wait(f"{ME}.includes('port-lf') && {ME}") or ""), True)
        # The fake says two at start: !0000beef's message and !0000cafe's reply to it.
        check("messages on the other node count on its button",
              b.wait(f"(({btn('MF18')}.querySelector('.mesh-radio-n')?.textContent || '') === '2') && '2'"), "2")
        check("  and does not land in LF's view", "port-lf" in b.js(ME), True)

        # The old page satisfies the wait until it unloads: a mark only it carries
        # (2026-11-24; a half-second sleep stood in for this, too short under load).
        b.js("window.__beforeReload = true; location.reload()")
        check("the pick survives a reload", "port-lf" in (b.wait(
            f"!window.__beforeReload && document.readyState === 'complete' && {ME}.includes('port-lf') && {ME}") or ""), True)
        b.js(f"{btn('MF18')}.click()")
        check("back to MF18: its node again", "port-mf18" in (b.wait(f"{ME}.includes('port-mf18') && {ME}") or ""), True)
        check("  its count cleared", b.js(f"!!{btn('MF18')}.querySelector('.mesh-radio-n')"), False)
    finally:
        stop(server)

    server = start(two=False)
    try:
        b.js("window.__beforeReload = true; location.reload()")
        b.wait(f"!window.__beforeReload && document.readyState === 'complete' && {ME}.includes('port-mf18')")
        check("one node only: no switcher", b.js("document.querySelector('#mesh-radios').hidden"), True)
    finally:
        stop(server)
finally:
    b.close()
    shutil.rmtree(TMP, ignore_errors=True)

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
