"""The UV-PRO add-on on the page (headless Chromium), against tests/fake_uvpro_bridge.py.

Single-radio page (server.py --sim --addons):
  - the add-on gets a tab; its view shows its panel and hides the radio's;
    the radio view hides the add-on
  - the status strip: connected, the A channel, battery
  - the channel table; A/B buttons tune the radio
  - channels in CHIRP's columns, VFO A/B rows below them; the editor edits
    Frequency, Duplex and Offset (asks first), shows the tone fields CHIRP
    uses for the Tone Mode, asks before a tone or power change, and sends
    only what changed
  - CHIRP CSV export; import shows a preview (nothing written; a moved
    frequency is a change) and writes only the changed channels
  - the Radio pane: VFO tune (asks), volume, region switch (asks), FM on,
    APRS path (asks), power off (asks)
  - settings: out of range disables Save and says why; beacon: the callsign
    is a choice of the configured calls
  - Switch off asks, then hides the panel's content and dims the tab;
    Switch on connects again
Station page (station.py with "addons"): a UV-PRO tab and Overview card, and
the panel in its own view.
Borrow mode ("hub"): between borrows the strip says "With packet" and when
it was read, Read radio is offered, the editor works on the last read and a
save reaches the radio; a channel not read back byte for byte is locked.

    CHROME=/path/to/chromium python3 tests/uvpro_page_test.py
"""
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from cdp_browser import Browser  # noqa: E402

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME = (os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
          or (MAC_CHROME if os.path.exists(MAC_CHROME) else None))
WEB = 21000 + random.randrange(1000)
CDP = WEB + 1000
TMP = tempfile.mkdtemp(prefix="uvpropage-")
CMD_LOG = os.path.join(TMP, "commands.jsonl")
STATE = os.path.join(TMP, "addons.json")
fails = 0

ADDONS = {"uvpro": {"enabled": True, "address": "00:11:22:33:44:55", "python": sys.executable,
                    "bridge": os.path.join(ROOT, "tests", "fake_uvpro_bridge.py"), "calls": ["N9ABC", "N0CALL"]}}


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


def wait_port(p):
    for _ in range(600):                  # 60 s ceiling; returns once the port answers
        try:
            socket.create_connection(("127.0.0.1", WEB), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    print(p.stdout.read())
    raise SystemExit("server did not start")


def start_server(addons=ADDONS, **env_extra):
    path = os.path.join(TMP, "addons-cfg.json")
    with open(path, "w") as f:
        json.dump(addons, f)
    env = dict(os.environ, FAKE_UVPRO_LOG=CMD_LOG, **env_extra)
    return wait_port(subprocess.Popen(
        [sys.executable, "server.py", "--sim", "--no-audio", "--listen", str(WEB), "--rigctl-port", "0",
         "--addons", path, "--addons-state", STATE],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))


def start_station():
    path = os.path.join(TMP, "station.json")
    with open(path, "w") as f:
        json.dump({"name": "test", "radios": [], "addons": ADDONS, "addons_state": STATE}, f)
    env = dict(os.environ, FAKE_UVPRO_LOG=CMD_LOG)
    return wait_port(subprocess.Popen(
        [sys.executable, "station.py", "--config", path, "--listen", str(WEB),
         "--rigctl-host", "127.0.0.1", "--rigctl-port", str(WEB + 2000)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))


def stop(p):
    p.terminate()
    try:
        p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


def commands():
    try:
        with open(CMD_LOG) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")

TEXT = "(s) => (document.querySelector(s)?.textContent || '')"
VISIBLE = "(s) => { const e = document.querySelector(s); return !!e && !e.closest('[hidden]') && e.offsetParent !== null; }"
P = ".addon-uvpro"
FIELD = ("(sel, l) => [...document.querySelectorAll(sel + ' label')].find((x) => x.textContent.startsWith(l))")


b = Browser(CHROME, CDP)


def set_field(form, label, value, ev="input"):
    b.js(f"(() => {{ const i = ({FIELD})({json.dumps(form)}, {json.dumps(label)}).querySelector('input,select'); "
         f"if (i.type === 'checkbox') i.checked = {json.dumps(value)}; else i.value = {json.dumps(value)}; "
         f"i.dispatchEvent(new Event({json.dumps(ev)})); }})()")


def wait_cmd(n, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        c = commands()
        if len(c) > n:
            return c[-1]
        time.sleep(0.05)
    return None


try:
    server = start_server()
    try:
        b.goto(f"http://127.0.0.1:{WEB}/#/uvpro")
        b.wait("document.querySelector('#conn b')?.textContent === 'Connected'")
        check("tab bar has UV-PRO", bool(b.wait("[...document.querySelectorAll('#rr-nav .st-tab')]"
                                                ".some((t) => t.textContent.includes('UV-PRO'))")), True)
        check("view: the panel shows", bool(b.wait(f"({VISIBLE})('{P}')")), True)
        check("view: the radio's panels are hidden",
              b.js("[...document.querySelectorAll('main.grid > .panel:not(.addon)')].every((p) => p.offsetParent === null)"), True)
        check("strip: Connected", b.wait(f"({TEXT})('{P} .uv-conn') === 'Connected' && 'Connected'"), "Connected")
        check("strip: A channel", "2M CALL" in b.wait(f"({TEXT})('{P} .uv-items')"), True)
        check("strip: battery", "87%" in b.js(f"({TEXT})('{P} .uv-items')"), True)
        check("strip: no self-keying warning", b.js(f"!!document.querySelector('{P} .uv-selfkey')"), False)
        rows = f"[...document.querySelectorAll('{P} .uv-mem tbody tr[data-id]:not(.uv-vfo)')]"
        cols = f"[...document.querySelectorAll('{P} .uv-mem thead th')].map((t) => t.textContent)"
        check("table: CHIRP's columns", b.wait(cols),
              ["Loc", "Name", "Frequency", "Duplex", "Offset", "Tone Mode", "Tone / DTCS", "Mode", "Power", "Skip", "Tune"])
        check("table: four memories", b.wait(f"{rows}.length === 4 && {rows}.map((r) => r.children[1].firstChild.textContent.trim())"),
              ["2M CALL", "RPT", "APRS", "NOAA"])
        check("table: RPT as CHIRP shows it", b.js(f"[...{rows}[1].children].slice(0, 10).map((c) => c.textContent.trim())"),
              ["2", "RPT", "146.940000", "-", "0.600000", "Tone", "100.0", "FM", "High", ""])
        check("table: NOAA is Duplex off", b.js(f"{rows}[3].children[3].textContent"), "off")

        n = len(commands())
        b.js(f"{rows}[2].querySelector('button[data-ab=channel_a]').click()")
        c = wait_cmd(n)
        check("A button tunes A", c and (c["cmd"], c["values"]), ("settings", {"channel_a": 2}))

        b.js(f"{rows}[1].click()")
        check("editor opens on the row", b.wait(f"({VISIBLE})('{P} .uv-editor') && ({TEXT})('{P} .uv-ed-title')"),
              "Location 2: RPT")
        ED = f"{P} .uv-ed-form"
        LAB = (f"(l) => [...document.querySelectorAll('{ED} label')]"
               ".find((x) => x.firstChild.firstChild.textContent.trim() === l)")
        def ed_set(label, value, ev="change"):
            b.js(f"(() => {{ const i = ({LAB})({json.dumps(label)}).querySelector('input,select'); "
                 f"if (i.type === 'checkbox') i.checked = {json.dumps(value)}; else i.value = {json.dumps(value)}; "
                 f"i.dispatchEvent(new Event({json.dumps(ev)})); }})()")
        check("editor: Frequency, Duplex, Offset editable",
              b.js(f"['Frequency', 'Duplex', 'Offset'].map((l) => ({LAB})(l)?.querySelector('input,select')?.value)"),
              ["146.940000", "-", "0.600000"])
        check("editor: no frequency input anywhere", b.js(f"document.querySelectorAll('{ED} input[type=number]').length"), 0)
        check("editor: Tone Mode Tone shows Tone, not ToneSql/DTCS",
              b.js(f"['Tone', 'ToneSql', 'DTCS Code'].map((l) => !!({LAB})(l))"), [True, False, False])
        check("editor: Save disabled before an edit", b.js(f"document.querySelector('{P} .uv-ed-foot .uv-save').disabled"), True)
        ed_set("Name", "RPT2", "input")
        n = len(commands())
        b.js(f"document.querySelector('{P} .uv-ed-foot .uv-save').click()")
        c = wait_cmd(n)
        check("plain edit: only the name went", c and (c["cmd"], c["id"], c["values"]), ("channel", 1, {"name": "RPT2"}))
        b.wait(f"!({LAB})('Name').classList.contains('dirty')")

        ed_set("Tone Mode", "TSQL")
        check("Tone Mode TSQL: ToneSql shown, Tone hidden", b.js(f"['Tone', 'ToneSql'].map((l) => !!({LAB})(l))"), [False, True])
        ed_set("ToneSql", "100.0")
        b.js("window.confirm = (q) => { window.asked = q; return true; }")
        b.js("window.asked = null")
        n = len(commands())
        b.js(f"document.querySelector('{P} .uv-ed-foot .uv-save').click()")
        c = wait_cmd(n)
        check("Tone 100.0 -> TSQL 100.0 (TX tone unchanged): no question", b.js("window.asked"), None)
        check("  the radio gets the RX tone only", c and c["values"], {"rx_sub_audio": "100.0"})
        b.wait(f"!({LAB})('Tone Mode').classList.contains('dirty')")
        ed_set("ToneSql", "103.5")
        b.js("window.confirm = (q) => { window.asked = q; return false; }")
        n = len(commands())
        b.js(f"document.querySelector('{P} .uv-ed-foot .uv-save').click()")
        time.sleep(0.5)
        check("TSQL 100.0 -> 103.5 (TX tone changes): asks; no: nothing sent", len(commands()), n)
        check("  the question shows the tones", "Tone: TSQL 100.0 → TSQL 103.5" in (b.js("window.asked") or ""), True)
        b.js(f"document.querySelector('{P} .uv-ed-foot .uv-revert').click()")

        b.js("window.confirm = () => false")
        b.wait(f"!({LAB})('Tone Mode').classList.contains('dirty')")
        ed_set("Power", "Low")
        n = len(commands())
        b.js(f"document.querySelector('{P} .uv-ed-foot .uv-save').click()")
        time.sleep(0.5)
        check("power change, answer no: nothing sent", len(commands()), n)
        b.js("window.confirm = (q) => { window.asked = q; return true; }")
        b.js(f"document.querySelector('{P} .uv-ed-foot .uv-save').click()")
        c = wait_cmd(n)
        check("power change, answer yes: sent", c and c["values"], {"power": "low"})
        check("  the question names the change", "Power: High → Low" in (b.js("window.asked") or ""), True)

        ed_set("Frequency", "147.000000", "input")
        b.js("window.confirm = (q) => { window.asked = q; return true; }")
        n = len(commands())
        b.js(f"document.querySelector('{P} .uv-ed-foot .uv-save').click()")
        c = wait_cmd(n)
        check("frequency edit: asks, names the change", "Frequency: 146.940000 → 147.000000" in (b.js("window.asked") or ""), True)
        check("  the radio gets RX and TX (Duplex - kept)",
              c and {k: round(v, 6) for k, v in c["values"].items()}, {"rx_freq": 147.0, "tx_freq": 146.4})
        b.wait(f"!({LAB})('Frequency').classList.contains('dirty')")
        ed_set("Frequency", "146.94", "input")
        n = len(commands())
        b.js(f"document.querySelector('{P} .uv-ed-foot .uv-save').click()")
        wait_cmd(n)
        ed_set("Frequency", "146.9.4", "input")
        check("frequency edit: not a number disables Save",
              b.js(f"document.querySelector('{P} .uv-ed-foot .uv-save').disabled"), True)
        b.js(f"document.querySelector('{P} .uv-ed-foot .uv-revert').click()")

        # CHIRP CSV export and import
        b.js(f"document.querySelector('{P} .uv-export').click()")
        exp = b.wait("window.lastExport")
        check("export: CHIRP CSV header", (exp or "").split("\n")[0][:40], "Location,Name,Frequency,Duplex,Offset,To")
        csv_text = (exp or "").replace("2,RPT2,", "2,RPT3,").replace("1,2M CALL,146.520000", "1,2M CALL,146.550000")
        n = len(commands())
        b.js("(() => { const i = document.querySelector('.addon-uvpro .uv-import-file'); const dt = new DataTransfer(); "
             f"dt.items.add(new File([{json.dumps(csv_text)}], 'radio.csv', {{ type: 'text/csv' }})); i.files = dt.files; "
             "i.dispatchEvent(new Event('change')); })()")
        check("import: preview shown", bool(b.wait(f"({VISIBLE})('{P} .uv-import')")), True)
        check("  summary", b.wait(f"({TEXT})('{P} .uv-import-sum').includes('to change') && ({TEXT})('{P} .uv-import-sum')"),
              "2 to change · 2 the same · 0 refused")
        check("  the moved frequency is an on-air change",
              "Frequency: 146.520000 → 146.550000" in b.js(f"[...document.querySelectorAll('{P} .uv-imp-change td')].map((t) => t.textContent).join(' ')"), True)
        time.sleep(0.3)
        check("  preview wrote nothing", len(commands()), n)
        b.js("window.confirm = (q) => { window.asked = q; return true; }")
        b.js(f"document.querySelector('{P} .uv-import-apply').click()")
        wait_cmd(n)
        time.sleep(0.5)
        check("import apply: asked, the two changes written",
              ("Frequency" in (b.js("window.asked") or ""), sorted((c["id"], sorted(c["values"])) for c in commands()[n:])),
              (True, [(0, ["rx_freq", "tx_freq"]), (1, ["name"])]))
        check("  preview closes", bool(b.wait(f"!({VISIBLE})('{P} .uv-import')")), True)

        b.js(f"document.querySelector('{P} .uv-panes .chip[data-v=settings]').click()")
        FM = f"{P} .uv-form-form"
        check("settings: squelch value", b.wait(f"({FIELD})('{FM}', 'Squelch')?.querySelector('input').value"), "3")
        set_field(FM, "Squelch", "12")
        check("settings: out of range disables Save",
              b.js(f"document.querySelector('{P} .uv-form-foot .uv-save').disabled"), True)
        check("  and says why", "0–9" in b.js(f"({TEXT})('{P} .uv-form-foot .wl-dim')"), True)
        b.js(f"document.querySelector('{P} .uv-form-foot .uv-revert').click()")

        b.js(f"document.querySelector('{P} .uv-panes .chip[data-v=beacon]').click()")
        check("beacon: callsign is a choice of the calls",
              b.wait(f"[...({FIELD})('{FM}', 'Callsign').querySelectorAll('option')].map((o) => o.value)"),
              ["N0CALL", "N9ABC"])

        # the Radio pane
        R = f"{P} .uv-radio"
        b.js(f"document.querySelector('{P} .uv-panes .chip[data-v=radio]').click()")
        check("radio: VFO A and B", b.wait(f"[...document.querySelectorAll('{R} .uv-vfo-row b')].map((x) => x.textContent)"),
              ["VFO A", "VFO B"])
        b.js("window.confirm = (q) => { window.asked = q; return true; }")
        n = len(commands())
        b.js(f"(() => {{ const i = document.querySelector('{R} .uv-vfo-row .uv-vfo-f'); i.value = '147.42'; "
             f"document.querySelector('{R} .uv-vfo-row .uv-vfo-tune').click(); }})()")
        c = wait_cmd(n)
        check("radio: VFO A tune asks, then goes to record 251",
              ("147.42" in (b.js("window.asked") or ""), c and c["id"], c and round(c["values"]["rx_freq"], 6)),
              (True, 251, 147.42))
        check("radio: volume slider at the radio's 8", b.wait(f"document.querySelector('{R} .uv-vol')?.value"), "8")
        n = len(commands())
        b.js(f"(() => {{ const v = document.querySelector('{R} .uv-vol'); v.value = '11'; v.dispatchEvent(new Event('change')); }})()")
        c = wait_cmd(n)
        check("radio: volume 11 sent", c and (c["cmd"], c["values"]), ("volume", {"level": 11}))
        check("radio: strip shows the volume", b.wait(f"({TEXT})('{P} .uv-items').includes('🔊 11') && 'yes'"), "yes")
        n = len(commands())
        b.js(f"(() => {{ const s = document.querySelector('{R} .uv-region'); s.value = '1'; "
             f"document.querySelector('{R} .uv-region-go').click(); }})()")
        c = wait_cmd(n)
        check("radio: region switch asks, then sent", ("TRAVEL" in (b.js("window.asked") or ""), c and c["values"]),
              (True, {"region": 1}))
        n = len(commands())
        b.js(f"document.querySelector('{R} .uv-fm-on').click()")
        c = wait_cmd(n)
        check("radio: FM on", c and (c["cmd"], c["values"]), ("fm", {"on": True}))
        check("radio: strip shows FM", b.wait(f"({TEXT})('{P} .uv-items').includes('FM 91.5') && 'yes'"), "yes")
        n = len(commands())
        b.js(f"(() => {{ document.querySelector('{R} .uv-path').value = 'wide2-1'; document.querySelector('{R} .uv-path-save').click(); }})()")
        c = wait_cmd(n)
        check("radio: APRS path asks, upper-cased", ("WIDE2-1" in (b.js("window.asked") or ""), c and c["values"]),
              (True, {"path": "WIDE2-1"}))
        b.js("window.confirm = () => false")
        n = len(commands())
        b.js(f"document.querySelector('{R} .uv-pwr').click()")
        time.sleep(0.5)
        check("radio: power off, answer no: nothing sent", len(commands()), n)
        b.screenshot(os.path.join(TMP, "radio-pane.png"))
        print("screenshot:", os.path.join(TMP, "radio-pane.png"))

        b.js("window.confirm = () => true")
        b.js(f"document.querySelector('{P} .addon-power').click()")
        check("switched off: message shown", bool(b.wait(f"({VISIBLE})('{P} .addon-msg') && "
                                                         f"({TEXT})('{P} .addon-msg').includes('switched off')")), True)
        check("switched off: content hidden", b.js(f"({VISIBLE})('{P} .addon-body')"), False)
        check("switched off: tab dimmed", b.js("[...document.querySelectorAll('#rr-nav .st-tab')]"
                                                ".find((t) => t.textContent.includes('UV-PRO')).classList.contains('off')"), True)
        b.js(f"document.querySelector('{P} .addon-power').click()")
        check("switched on: connected again",
              b.wait(f"({VISIBLE})('{P} .addon-body') && ({TEXT})('{P} .uv-conn') === 'Connected' && 'yes'"), "yes")

        b.js("location.hash = '#/radio'")
        check("radio view hides the add-on", bool(b.wait(f"!({VISIBLE})('{P}')")), True)
    finally:
        stop(server)

    station = start_station()
    try:
        b.goto(f"http://127.0.0.1:{WEB}/")
        check("station: UV-PRO tab", bool(b.wait("[...document.querySelectorAll('#st-nav .st-tab')]"
                                                 ".some((t) => t.textContent.includes('UV-PRO'))")), True)
        check("station: Overview card", bool(b.wait("[...document.querySelectorAll('#st-cards .st-card')]"
                                                     ".some((c) => c.textContent.includes('UV-PRO') && c.textContent.includes('87%'))")), True)
        b.js("location.hash = '#/uvpro'")
        check("station: the panel in its own view", bool(b.wait(f"({VISIBLE})('#view-addon-uvpro {P} .uv-mem')")), True)
        check("station: the panel spans the view", b.js(
            "(() => { const p = document.querySelector('#view-addon-uvpro .panel.addon'), g = p.parentElement;"
            " return p.getBoundingClientRect().width > 0.9 * (g.clientWidth - 44); })()"), True)
        b.screenshot(os.path.join(TMP, "station-uvpro.png"))
        print("screenshot:", os.path.join(TMP, "station-uvpro.png"))
        check("station: channels listed",
              b.wait(f"document.querySelectorAll('#view-addon-uvpro .uv-mem tbody tr[data-id]:not(.uv-vfo)').length"), 4)
    finally:
        stop(station)

    # Auto relay on (2026-10-17: it digipeated by itself and blocked every change)
    server = start_server(FAKE_UVPRO_SETTINGS='{"auto_relay_en": true}')
    try:
        b.goto(f"http://127.0.0.1:{WEB}/#/uvpro")
        b.wait("document.querySelector('#conn b')?.textContent === 'Connected'")
        check("auto relay on: strip warns",
              b.wait(f"({TEXT})('{P} .uv-selfkey')"), "⚠ Auto relay on: transmits by itself")
    finally:
        stop(server)

    # borrow mode; nothing listens at "hub", so the add-on uses the radio without lending
    borrow = {"uvpro": {**ADDONS["uvpro"], "hub": f"http://127.0.0.1:{WEB + 3000}", "idle_s": 1}}
    server = start_server(borrow, FAKE_UVPRO_INEXACT="2")
    try:
        b.goto(f"http://127.0.0.1:{WEB}/#/uvpro")
        check("borrow: strip says With packet after the read",
              b.wait(f"({TEXT})('{P} .uv-conn') === 'With packet' && 'yes'"), "yes")
        check("borrow: when it was read", "read " in b.js(f"({TEXT})('{P} .uv-items')"), True)
        check("borrow: Read radio offered, Give back hidden",
              b.js(f"[({TEXT})('{P} .uv-refresh'), document.querySelector('{P} .uv-giveback').hidden]"), ["Read radio", True])
        rows = f"[...document.querySelectorAll('{P} .uv-mem tbody tr[data-id]:not(.uv-vfo)')]"
        check("borrow: inexact channel locked", b.js(f"{rows}.map((r) => !!r.querySelector('.uv-lock'))"),
              [False, False, True, False])
        NAME = f"[...document.querySelectorAll('{P} .uv-ed-form label')].find((x) => x.textContent.startsWith('Name')).querySelector('input')"
        b.js(f"{rows}[2].click()")
        check("borrow: locked channel's editor is disabled", b.wait(f"{NAME}.disabled && 'yes'"), "yes")
        b.js(f"{rows}[0].click()")
        check("borrow: editor works on the last read", b.wait(f"!{NAME}.disabled && 'yes'"), "yes")
        n = len(commands())
        b.js(f"(() => {{ const i = {NAME}; i.value = 'LENT'; i.dispatchEvent(new Event('input')); }})()")
        b.js(f"document.querySelector('{P} .uv-ed-foot .uv-save').click()")
        c = wait_cmd(n, timeout=10)
        check("borrow: Save reaches the radio", c and (c["cmd"], c["values"]), ("channel", {"name": "LENT"}))
        check("borrow: back to With packet after", b.wait(f"({TEXT})('{P} .uv-conn') === 'With packet' && "
                                                         f"({TEXT})('{P} .uv-mem').includes('LENT') && 'yes'"), "yes")
    finally:
        stop(server)
finally:
    b.close() if hasattr(b, "close") else None

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
