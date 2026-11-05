"""The MeshCore tab in a real browser, against tests/fake_meshcore_bridge.py.

  - the tab shows the node, its link and the nodes it heard
  - Settings opens the form with the node's sections; the Wi-Fi password
    field is a password field and starts empty
  - a radio preset fills the form and enables Save; Save writes those
    values to the node (they reach the fake bridge) and the form shows the
    read-back
  - a typed Wi-Fi password reaches the node, and after the save no input
    on the page holds it
  - laid out like the Mesh tab: the map shows this node and the contacts
    that share a location, the node list has the node not yet added
  - a repeater's card: Status fills its numbers, Neighbours draws a link on
    the map, Find path draws the route; a saved login enables the console,
    a command's reply shows in it; a favourite star sticks
  - "+" adds the pending node; Message opens the Direct view with a To bar,
    and a sent message shows delivered; a #channel is joined from the page

    python3 tests/meshcore_page_test.py      (SHOT=/path.png saves a screenshot)
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
from testkit import free_port  # noqa: E402

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME = (os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or (MAC_CHROME if os.path.exists(MAC_CHROME) else None))
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
TMP = tempfile.mkdtemp(prefix="mcpage-")
GAMMA = "d4" * 32
SAVED = os.path.join(TMP, "gamma_pw")
with open(SAVED, "w") as f:
    f.write("Sav3d-Pw\n")
CFG, CMD_LOG = os.path.join(TMP, "addons.json"), os.path.join(TMP, "commands.jsonl")
PWD = "page-pa55word"
fails = 0

with open(CFG, "w") as f:
    json.dump({"meshcore": {"port": os.path.join(TMP, "port"), "python": sys.executable, "log": "",
                            "bridge": os.path.join(ROOT, "tests", "fake_meshcore_bridge.py"),
                            "logins": {GAMMA[:12]: SAVED},
                            "presets": [{"label": "Local", "freq": 908.205, "bw": 62.5, "sf": 9, "cr": 8}]}}, f)


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def sets():
    try:
        with open(CMD_LOG) as f:
            return [c for c in map(json.loads, filter(str.strip, f)) if c.get("cmd") == "set"]
    except OSError:
        return []


def wait_for(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        r = pred()
        if r:
            return r
        time.sleep(0.1)
    return pred()


web = free_port()
server = subprocess.Popen([sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(web),
                           "--rigctl-port", "0", "--addons", CFG, "--addons-state", os.path.join(TMP, "state.json")],
                          cwd=ROOT, env=dict(os.environ, FAKE_MC_LOG=CMD_LOG),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(150):
    try:
        socket.create_connection(("127.0.0.1", web), timeout=0.2).close()
        break
    except OSError:
        time.sleep(0.1)
b = Browser(CHROME, free_port(), size="1300,1000")
P = "document.querySelector('.addon-meshcore')"
try:
    b.goto(f"http://127.0.0.1:{web}/#/meshcore")
    check("the tab shows the node", b.wait(f"{P}?.querySelector('.mc-name')?.textContent === 'TestNode' && 'yes'"), "yes")
    check("  its link", b.wait(f"({P}.querySelector('.mc-sub')?.textContent || '').includes('via Wi-Fi') && 'yes'"), "yes")
    check("  and the nodes it heard (three contacts, one not added)", b.js(f"{P}.querySelectorAll('.mc-node').length"), 4)

    b.js(f"{P}.querySelector('[data-cfg]').click()")
    check("Settings opens with the node's sections",
          b.wait(f"[...{P}.querySelectorAll('[data-sec]')].map(x => x.dataset.sec).join(',') || null"),
          "node,radio,wifi,bluetooth")

    b.js(f"{P}.querySelector('[data-sec=radio]').click()")
    b.wait(f"{P}.querySelector('[data-preset]') && 'yes'")
    check("radio: Save starts disabled", b.js(f"{P}.querySelector('[data-save]').disabled"), True)
    b.js(f"{P}.querySelector('[data-preset=\"0\"]').click()")
    check("  a preset fills the form", b.js(f"{P}.querySelector('[data-f=sf]').value"), "9")
    check("  and enables Save", b.js(f"{P}.querySelector('[data-save]').disabled"), False)
    b.js("window.confirm = () => true")          # the risky-change question
    b.js(f"{P}.querySelector('[data-save]').click()")
    check("  Save writes the preset to the node", (wait_for(sets) or [{}])[-1].get("values"),
          {"freq": 908.205, "sf": 9, "cr": 8})
    check("  the form shows the read-back",
          b.wait(f"{P}.querySelector('[data-f=sf]')?.value === '9' && {P}.querySelector('[data-save]').disabled && 'yes'"),
          "yes")

    b.js(f"{P}.querySelector('[data-sec=wifi]').click()")
    b.wait(f"{P}.querySelector('[data-f=password]') && 'yes'")
    check("Wi-Fi: the password is a password field", b.js(f"{P}.querySelector('[data-f=password]').type"), "password")
    check("  and starts empty", b.js(f"{P}.querySelector('[data-f=password]').value"), "")
    b.js(f"""(() => {{ const i = {P}.querySelector('[data-f=password]'); i.value = {json.dumps(PWD)};
             i.dispatchEvent(new Event('input', {{bubbles: true}})); }})()""")
    b.js(f"{P}.querySelector('[data-save]').click()")
    check("  a typed password reaches the node",
          wait_for(lambda: any((c.get("values") or {}).get("password") == PWD for c in sets())), True)
    b.wait(f"{P}.querySelector('[data-save]')?.disabled && 'yes'")
    check("  and after the save no input holds it",
          b.js(f"[...document.querySelectorAll('input')].some(i => i.value === {json.dumps(PWD)})"), False)

    b.js(f"{P}.querySelector('[data-cfg]').click()")          # back to the main view
    check("laid out like the Mesh tab (its columns)", b.wait(f"!{P}.querySelector('.mc-main').hidden && {P}.querySelector('.mesh-cols') && 'yes'"), "yes")
    check("the map: this node and the two contacts with a location",
          b.wait(f"{P}.querySelectorAll('.mc-map .mm-node').length === 3 && 'yes'"), "yes")
    check("  this node drawn as me", b.js(f"{P}.querySelectorAll('.mc-map .mm-node.me').length"), 1)
    check("  a repeater carries its glyph", b.js(f"[...{P}.querySelectorAll('.mc-map .mm-glyph')].map(x => x.textContent).join()"), "R")
    check("the node list shows the one not added", b.js(f"[...{P}.querySelectorAll('.mc-node')].some(x => x.textContent.includes('not added'))"), True)

    G = f"{P}.querySelector('.mc-node[data-key=\"{GAMMA}\"]')"
    b.js(f"{G}.click()")
    check("a repeater's card opens", b.wait(f"!{P}.querySelector('.mc-card').hidden && {P}.querySelector('.mc-card b')?.textContent"), "Gamma Rpt")
    check("  with Log in (saved) and Status", b.js(f"!!{P}.querySelector('[data-act=login]') && !!{P}.querySelector('[data-act=status]') && !{P}.querySelector('.mc-pwd')"), True)
    b.js(f"{P}.querySelector('[data-act=status]').click()")
    check("Status fills its numbers", b.wait(f"({P}.querySelector('.mc-status')?.textContent || '').includes('-117 dBm') && 'yes'"), "yes")
    time.sleep(2.2)
    b.js(f"{P}.querySelector('[data-act=neighbours]').click()")
    check("Neighbours lists them", b.wait(f"({P}.querySelector('.mc-neighbours')?.textContent || '').includes('Alpha') && 'yes'"), "yes")
    check("  and draws the link on the map", b.wait(f"{P}.querySelectorAll('.mc-map .mm-link').length === 1 && 'yes'"), "yes")
    time.sleep(2.2)
    b.js(f"{P}.querySelector('[data-act=path]').click()")
    check("Find path shows the route", b.wait(f"({P}.querySelector('.mc-path')?.textContent || '').includes('there') && 'yes'"), "yes")
    time.sleep(2.2)
    check("  the console waits for an admin login", b.js(f"{P}.querySelector('[data-act=console]').disabled"), True)
    b.js(f"{P}.querySelector('[data-act=login]').click()")
    check("the saved login logs in as admin", b.wait(f"!{P}.querySelector('[data-act=console]').disabled && 'yes'"), "yes")
    check("  and the list says so", b.wait(f"{G}.textContent.includes('admin') && 'yes'"), "yes")
    b.js(f"{P}.querySelector('[data-act=console]').click()")
    b.wait(f"{P}.querySelector('.mc-cli') && 'yes'")
    time.sleep(2.2)
    b.js(f"""(() => {{ const i = {P}.querySelector('.mc-cli'); i.value = 'ver';
             i.closest('form').requestSubmit(); }})()""")
    check("a command's reply shows in the console", b.wait(f"({P}.querySelector('.mc-console-out')?.textContent || '').includes('echo: ver') && 'yes'"), "yes")
    b.js(f"{G}.querySelector('.mesh-fav').click()")
    check("a favourite star sticks", b.wait(f"{G}?.querySelector('.mesh-fav.on') && 'yes'"), "yes")

    D = f"[...{P}.querySelectorAll('.mc-node')].find(x => x.textContent.includes('Delta'))"
    b.js(f"{D}.querySelector('.mesh-fav').click()")
    check("+ adds the pending node", b.wait(f"{D} && !{D}.textContent.includes('not added') && 'yes'"), "yes")

    A = f"{P}.querySelector('.mc-node[data-key=\"{'a1' * 32}\"]')"
    b.js(f"{A}.click()")
    b.wait(f"{P}.querySelector('[data-act=dm]') && 'yes'")
    b.js(f"{P}.querySelector('[data-act=dm]').click()")
    check("Message opens the Direct view with a To bar", b.wait(f"!{P}.querySelector('.mc-to').hidden && ({P}.querySelector('.mc-to').textContent || '').includes('Alpha') && 'yes'"), "yes")
    b.js(f"""(() => {{ const i = {P}.querySelector('.mc-text'); i.value = 'page dm';
             i.dispatchEvent(new Event('input')); {P}.querySelector('.mc-form').requestSubmit(); }})()""")
    check("  a sent message shows delivered", b.wait(f"[...{P}.querySelectorAll('.mc-msg.mine')].some(x => x.textContent.includes('page dm') && x.querySelector('.mesh-ack.delivered')) && 'yes'"), "yes")

    b.js("window.prompt = () => '#ranchnet'")
    b.js(f"{P}.querySelector('[data-join]').click()")
    check("a #channel is joined from the page", b.wait(f"[...{P}.querySelectorAll('.mc-tabs [data-view]')].some(x => x.textContent.includes('#ranchnet')) && 'yes'"), "yes")
    if os.environ.get("SHOT"):
        b.js(f"{G}.click()")
        time.sleep(1.5)
        b.screenshot(os.environ["SHOT"])
finally:
    b.close()
    server.terminate()
    server.wait(10)

print("PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
