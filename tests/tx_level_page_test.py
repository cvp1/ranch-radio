"""The radio page's TX level row, in a real browser (headless Chromium).

Checks the row appears only when the server has a codec level, shows the
band and level for the chosen use, steps the level with the buttons (the
lock respected), marks an unsaved level, and hides Save without --codec-save.
Server-side behaviour is tests/tx_level_test.py; this is the page.

    python3 tests/tx_level_page_test.py          # SHOTS=dir to keep screenshots
"""
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

CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")
SHOTS = os.environ.get("SHOTS")
TMP = tempfile.mkdtemp(prefix="tx-level-page-")
with open(os.path.join(TMP, "amixer"), "w") as f:
    f.write(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT}/tests/fake_amixer.py" "$@"\n')
os.chmod(os.path.join(TMP, "amixer"), 0o755)
ENV = dict(os.environ, PATH=TMP + os.pathsep + os.environ["PATH"],
           FAKE_AMIXER_STATE=os.path.join(TMP, "level"), CODEC_LEVEL_BACKEND="alsa")
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


RIGCTL = [0]


def serve(*extra, audio=False, log=None):
    web = 18000 + random.randrange(1500)
    RIGCTL[0] = 15000 + random.randrange(1500)
    out = open(log, "w") if log else subprocess.DEVNULL
    p = subprocess.Popen([sys.executable, "server.py", "--sim", "--rig", "g90",
                          *(() if audio else ("--no-audio",)),
                          "--listen", str(web), "--host", "127.0.0.1",
                          "--rigctl-port", str(RIGCTL[0]), "--rigctl-host", "127.0.0.1",
                          *extra], cwd=ROOT, env=ENV, stdout=out, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", web), timeout=0.2).close()
            return p, web
        except OSError:
            time.sleep(0.1)
    raise SystemExit("server did not start")


def stop(p):
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


VISIBLE = "(s) => { const e = document.querySelector(s); return !!e && !e.closest('[hidden]') && e.offsetParent !== null; }"
TEXT = "(s) => (document.querySelector(s)?.textContent || '')"
LEVELS = ("--winlink-codec-db", "default=-32,20m=-33", "--winlink-codec-mixer", "CODEC:PCM",
          "--voice-codec-db", "default=-30", "--codec-min-db", "-36")

b = Browser(CHROME, 19000 + random.randrange(500), size="1500,1000",
            extra=("--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"))
try:
    print("# no codec level: no row")
    p, web = serve()
    try:
        b.goto(f"http://127.0.0.1:{web}/")
        b.wait(f"({TEXT})('#conn b') === 'Connected'")
        time.sleep(0.5)
        check("row hidden", b.js(f"({VISIBLE})('#txl')"), False)
    finally:
        stop(p)

    print("\n# levels set: the row, its uses and its buttons")
    p, web = serve(*LEVELS)
    try:
        b.goto(f"http://127.0.0.1:{web}/")
        b.wait(f"({TEXT})('#conn b') === 'Connected'")
        b.js("localStorage.removeItem('txlUse')")
        b.goto(f"http://127.0.0.1:{web}/")
        b.wait(f"({TEXT})('#conn b') === 'Connected'")
        b.wait(f"({TEXT})('#txl-val').includes('dB')")
        check("row shown", b.js(f"({VISIBLE})('#txl')"), True)
        check("uses: Winlink, Voice", b.js("[...document.querySelectorAll('#txl-use .chip')].map((c) => c.textContent)"),
              ["Winlink", "Voice"])
        check("Winlink first, the 20 m level", b.js(f"({TEXT})('#txl-val')"), "20 m -33 dB")
        check("Save hidden without --codec-save", b.js(f"({VISIBLE})('#txl-save')"), False)
        b.js("document.querySelector('#txl-use .chip[data-use=voice]').click()")
        b.wait(f"({TEXT})('#txl-val') === '20 m -30 dB'")
        check("Voice: the default level on 20 m", b.js(f"({TEXT})('#txl-val')"), "20 m -30 dB")
        if SHOTS:
            b.screenshot(os.path.join(SHOTS, "txl-voice.png"))

        locked = b.js("document.querySelector('#btn-lock').getAttribute('aria-checked') === 'true'")
        if locked:
            b.js("document.querySelector('#txl-down').click()")
            time.sleep(0.5)
            check("locked: a step does nothing", b.js(f"({TEXT})('#txl-val')"), "20 m -30 dB")
            b.js("document.querySelector('#btn-lock').click()")
        b.js("document.querySelector('#txl-down').click()")
        b.wait(f"({TEXT})('#txl-val') === '20 m -31 dB'")
        check("− steps 1 dB down", b.js(f"({TEXT})('#txl-val')"), "20 m -31 dB")
        check("marked unsaved", b.js("document.querySelector('#txl-val').classList.contains('unsaved')"), True)
        for _ in range(6):
            b.js("document.querySelector('#txl-down').click()")
            time.sleep(0.15)
        b.wait(f"({TEXT})('#txl-val') === '20 m -36 dB'")
        check("stops at the floor", b.js(f"({TEXT})('#txl-val')"), "20 m -36 dB")
        check("− disabled at the floor", b.js("document.querySelector('#txl-down').disabled"), True)
        b.js("document.querySelector('#txl-up').click()")
        b.wait(f"({TEXT})('#txl-val') === '20 m -35 dB'")
        check("+ steps back up", b.js(f"({TEXT})('#txl-val')"), "20 m -35 dB")
        b.js("document.querySelector('#txl-use .chip[data-use=winlink]').click()")
        check("Winlink untouched by voice steps", b.js(f"({TEXT})('#txl-val')"), "20 m -33 dB")
        b.js("document.querySelector('#txl-use .chip[data-use=voice]').click()")
        b.wait(f"({TEXT})('#txl-note').includes('not saved')")
        check("the note says it in words", b.js(f"({TEXT})('#txl-note')"),
              "lost on restart (saving is off)")
        for _ in range(7):
            b.js("document.querySelector('#txl-up').click()")
            time.sleep(0.15)
        b.wait(f"({TEXT})('#txl-val') === '20 m -28 dB'")
        check("above the saved level: a warning in words",
              ("above the saved -30 dB" in b.js(f"({TEXT})('#txl-note')"),
               b.js("document.querySelector('#txl-note').classList.contains('warn')")), (True, True))

        print("\n# a refused change: the page shows the radio's real value")
        import socket as _s
        b.js("setRadio('freq', 14230000)")      # not keyed: accepted
        b.wait("S.freq === 14230000")
        with _s.create_connection(("127.0.0.1", RIGCTL[0]), timeout=3) as c:
            c.sendall(b"T 1\n"); c.recv(32)
            time.sleep(0.5)
            b.js("setRadio('freq', 7074000)")   # keyed: refused
            time.sleep(1.2)
            check("refused retune: page shows the radio's frequency",
                  (b.js("S.freq"), b.js(f"({TEXT})('#freq').replace(/\\s/g, '')").startswith("14.230")),
                  (14230000, True))
            c.sendall(b"T 0\n"); c.recv(32)
        if SHOTS:
            b.screenshot(os.path.join(SHOTS, "txl-notes.png"))
        if SHOTS:
            b.screenshot(os.path.join(SHOTS, "txl-winlink.png"))
    finally:
        stop(p)

    print("\n# G90 with the page's Mic on: PTT waits for DATA, and says why")
    SLOG = os.path.join(TMP, "server.log")
    p, web = serve(*LEVELS, audio=True, log=SLOG)
    try:
        b.goto(f"http://127.0.0.1:{web}/")
        b.wait(f"({TEXT})('#conn b') === 'Connected'")
        b.js("setRadio('mode', 'USB'); setRadio('data', false)")
        b.js("document.querySelector('#btn-mic').click()")
        b.wait("!!audio.mic")
        b.js("if (!armed) setArmed(true)")
        b.wait(f"({TEXT})('#ptt-sub').includes('DATA')")
        check("plain USB, Mic on: PTT disabled with the reason",
              (b.js("document.querySelector('#ptt').disabled"), b.js(f"({TEXT})('#ptt-sub')")),
              (True, "turn DATA on · plain USB sends no audio from here"))
        b.js("setRadio('data', true)")
        b.wait("!document.querySelector('#ptt').disabled")
        check("DATA on: PTT back", b.js("document.querySelector('#ptt').disabled"), False)
        b.js("setRadio('freq', 14369000)")
        b.wait(f"({TEXT})('#ptt-sub').includes('outside')")
        check("off-band (14.369 MHz): PTT disabled with the reason",
              (b.js("document.querySelector('#ptt').disabled"), b.js(f"({TEXT})('#ptt-sub')")),
              (True, "outside the amateur bands · tune into a band"))
        b.js("setRadio('freq', 14230000)")
        b.wait("!document.querySelector('#ptt').disabled")

        print("\n# the page's mic, reported in the server log (diagnosis)")
        # mac-shack (2026-10-18): the page's audio engine was suspended, so the mic
        # produced 0 frames and PTT went out silent. Suspend it as a browser may
        # (a device or focus change), then press PTT for real (a CDP mouse press,
        # a user gesture): the press must resume it and the mic must reach the radio.
        b.js("audio.ctx.suspend()")
        b.wait("audio.ctx.state === 'suspended'")
        check("before the press: the audio engine is suspended", b.js("audio.ctx.state"), "suspended")
        r = b.js("(() => { document.querySelector('#ptt').scrollIntoView({block: 'center'}); "
                 "const q = document.querySelector('#ptt').getBoundingClientRect(); "
                 "return [q.x + q.width / 2, q.y + q.height / 2]; })()")
        b.call("Input.dispatchMouseEvent", type="mousePressed", x=r[0], y=r[1], button="left", clickCount=1)
        time.sleep(1.5)
        b.call("Input.dispatchMouseEvent", type="mouseReleased", x=r[0], y=r[1], button="left", clickCount=1)
        time.sleep(1.2)
        check("the press resumed it", b.js("audio.ctx.state"), "running")
        text = open(SLOG).read()
        import re
        m = re.search(r"page \S+: voice: mic frames (\d+), sent (\d+)", text)
        check("the page logs its mic frames, and sent some", bool(m and int(m.group(2)) > 0), True)
        m2 = re.search(r"(\d+) kB of microphone audio sent \((\d+) kB arrived\)", text)
        check("the server logs what arrived and what it forwarded", bool(m2 and int(m2.group(1)) > 0), True)
        check("mic on is logged with the device", "page " in text and ": mic on: " in text, True)
        check("the browser's own auto gain is off", "AGC false" in text, True)
        lines = [l for l in text.splitlines() if "voice: page PTT released" in l]
        m = re.search(r"audio (-?\d+) dBFS RMS \(peak (-?\d+|-inf)\)", lines[-1] if lines else "")
        check("the mic's peak is held at or under the tone's -12 dBFS (the limiter)",
              bool(m) and (m.group(2) == "-inf" or int(m.group(2)) <= -12), True)
        check("the page ends the transmission with a level verdict", "voice level \"" in text, True)

        print("\n# mic level: the row, the meter, the gain per device")
        check("the mic row is shown with the mic on", b.js(f"({VISIBLE})('#micl')"), True)
        check("the device list names the mic in use",
              b.js("[...document.querySelectorAll('#mic-dev option')].some((o) => o.value === micLevel.label)"), True)
        b.wait("!!micLevel.lvl")
        check("the meter gets levels", b.js("typeof micLevel.lvl.rms"), "number")
        if SHOTS:
            b.js("document.querySelector('#micl').scrollIntoView({block: 'center'})")
            b.screenshot(os.path.join(SHOTS, "mic-row.png"))
        g0 = b.js("micLevel.gainDb")
        b.js("document.querySelector('#micg-up').click(); document.querySelector('#micg-up').click()")
        check("+ raises the gain 1 dB a press", b.js("micLevel.gainDb"), g0 + 2)
        check("kept for this mic", b.js("micLevel.profile(micLevel.label).gainDb"), g0 + 2)
        check("another mic keeps its own (none yet)", b.js("micLevel.profile('Apple EarPods')"), None)
        b.js("document.querySelector('#micg-down').click(); document.querySelector('#micg-down').click()")
        # The analysis, on known levels: speech is the power mean above the gate.
        a = b.js("micLevel.analyse([...Array(20).fill({rms: -30, peak: -14, gr: 0}), "
                 "...Array(10).fill({rms: -70, peak: -60, gr: 0})])")
        check("analyse: 2 s of speech at -30 dBFS, floor -70",
              (a["speechS"], round(a["rms"]), a["floor"], a["limiting"]), (2, -30, -70, 0))
        b.js("micLevel.tx = [...Array(30).fill({rms: -40, peak: -30, gr: 0})]; micLevel.txDone()")
        check("verdict: quiet", "quiet" in b.js(f"({TEXT})('#mic-note')"), True)
        b.js("micLevel.tx = [...Array(30).fill({rms: -16, peak: -12, gr: 9})]; micLevel.txDone()")
        check("verdict: hot", "hot" in b.js(f"({TEXT})('#mic-note')"), True)
        b.js("micLevel.tx = [...Array(30).fill({rms: -24, peak: -12, gr: 1})]; micLevel.txDone()")
        check("verdict: good", b.js(f"({TEXT})('#mic-note')"), "Last transmission: good level (-24 dBFS).")
        print("\n# Mic check: the gain that puts this speech at -24 dBFS")
        b.js("micLevel.CHECK_S = 1; micLevel.check(); "
             "setTimeout(() => { micLevel.checking = Array(10).fill({rms: -34, peak: -20, gr: 0}); }, 300)")
        b.wait("!document.querySelector('#mic-check').disabled && micLevel.checking === null", timeout=5)
        check("speech at -34 with gain 0: gain +10 dB", b.js("micLevel.gainDb"), g0 + 10)
        check("saved as checked", bool(b.js("micLevel.profile(micLevel.label).checked")), True)
        b.js("micLevel.step(-10)")

        print("\n# test tone: a steady 1 kHz instead of the mic, measured in the log")
        b.js("document.querySelector('#txl-tone').click()")
        b.wait("!!audio.tone")
        r = b.js("(() => { document.querySelector('#ptt').scrollIntoView({block: 'center'}); "
                 "const q = document.querySelector('#ptt').getBoundingClientRect(); "
                 "return [q.x + q.width / 2, q.y + q.height / 2]; })()")
        b.call("Input.dispatchMouseEvent", type="mousePressed", x=r[0], y=r[1], button="left", clickCount=1)
        time.sleep(1.5)
        b.call("Input.dispatchMouseEvent", type="mouseReleased", x=r[0], y=r[1], button="left", clickCount=1)
        time.sleep(1.2)
        text = open(SLOG).read()
        lines = [l for l in text.splitlines() if "voice: page PTT released" in l]
        m = re.search(r"audio (-?\d+) dBFS RMS \(peak (-?\d+)\)", lines[-1] if lines else "")
        check("the tone arrives at -15 dBFS RMS, -12 peak", (int(m.group(1)), int(m.group(2))) if m else None, (-15, -12))
        check("the page says it was the test tone", "voice (test tone): mic frames" in text, True)
        b.js("document.querySelector('#txl-tone').click()")
        b.wait("!audio.tone")
        b.js("setRadio('data', false)")
        b.js("document.querySelector('#btn-mic').click()")     # Mic off: the radio's own mic
        b.wait("!audio.mic")
        check("plain USB, Mic off: PTT allowed (the radio's own mic)",
              b.js("document.querySelector('#ptt').disabled"), False)
    finally:
        stop(p)

    print("\n# --codec-save: Save shown, enabled only when unsaved")
    p, web = serve(*LEVELS, "--codec-save", "nohost:g90",
                   "--winlink-rigctl-port", str(16000 + random.randrange(1500)))
    try:
        b.goto(f"http://127.0.0.1:{web}/")
        b.wait(f"({TEXT})('#conn b') === 'Connected'")
        b.wait(f"({TEXT})('#txl-val').includes('dB')")
        check("Save shown", b.js(f"({VISIBLE})('#txl-save')"), True)
        check("Save disabled while saved", b.js("document.querySelector('#txl-save').disabled"), True)
        if b.js("document.querySelector('#btn-lock').getAttribute('aria-checked') === 'true'"):
            b.js("document.querySelector('#btn-lock').click()")
        b.js("document.querySelector('#txl-down').click()")
        b.wait("!document.querySelector('#txl-save').disabled")
        check("Save enabled once unsaved", b.js("document.querySelector('#txl-save').disabled"), False)

        print("\n# a Winlink session armed: the page's PTT disabled, and says why")
        import asyncio, json, threading, aiohttp
        release = threading.Event()

        def hold_armed():                  # a second client arms, holds, then closes (disarms)
            async def go():
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(f"http://127.0.0.1:{web}/ws") as ws:
                        await ws.receive()
                        await ws.send_str(json.dumps({"t": "winlink_arm", "v": True}))
                        while not release.is_set():
                            await asyncio.sleep(0.1)
            asyncio.run(go())
        b.js("document.querySelector('#btn-arm').click()")       # TX ARMED on the page
        t = threading.Thread(target=hold_armed, daemon=True)
        t.start()
        b.wait(f"({TEXT})('#ptt-sub').includes('Winlink')")
        check("PTT disabled during the session, with the reason",
              (b.js("document.querySelector('#ptt').disabled"), b.js(f"({TEXT})('#ptt-sub')")),
              (True, "Winlink session armed · waits until it ends"))
        release.set()
        t.join(5)
        b.wait("!document.querySelector('#ptt').disabled")
        check("PTT back once the session ends", b.js("document.querySelector('#ptt').disabled"), False)
    finally:
        stop(p)
finally:
    b.close()

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
