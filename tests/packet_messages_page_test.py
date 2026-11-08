"""APRS messaging on the packet page (plan 2026-11-07, 3.5), in headless
Chromium, against the real hub (messaging full, no cooldown) on
tests/fake_kiss_radio.py.

  - compose is disabled until TX ARMED in this browser, and while a hold is
    on or messaging is paused (a KISS client); the page names the reason
  - a hold shows its holder and "back" channel; Release (confirmed) clears it
  - the 67-character counter, and a "{" refuses the send button
  - a message sent from the page reaches the radio with exactly its text;
    the thread shows pending, then acked once a fake ack is heard
  - an inbound message makes an unread badge

A foreign Origin through the station is tests/station_test.py's.

    CHROME=... python3 tests/packet_messages_page_test.py [screenshot.png]
"""
import os
import random
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
os.environ.pop("CC_OBS_TOKENS_FILE", None)
from cdp_browser import Browser  # noqa: E402
from ax25 import Frame  # noqa: E402
import tnc_hub_test as T  # noqa: E402

CDP = 19000 + random.randrange(1000)
CHROME = os.environ.get("CHROME") or shutil.which("chromium") or shutil.which("google-chrome")
SHOT = sys.argv[1] if len(sys.argv) > 1 else None
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")

logdir = tempfile.mkdtemp(prefix="msgpage-")
radio = T.FakeKissRadio().start()
hub = T.HubProc(radio, tx=True, extra=("--log-dir", logdir, "--aprs-messaging", "full", "--aprs-firmware-acks", "false",
                                       "--aprs-cooldown", "0"))
b = Browser(CHROME, CDP)
DISABLED = "document.querySelector('#msg-text').disabled"
try:
    b.goto(f"http://127.0.0.1:{hub.http}/#messages")
    b.wait("document.querySelector('#msg-mode span').textContent.startsWith('messaging full')")
    check("effective mode shown", b.js("document.querySelector('#msg-mode span').textContent"), "messaging full")
    check("compose disabled until TX ARMED", b.js(DISABLED), True)
    check("  the page says why", b.js("document.querySelector('#msg-pause').textContent"), "arm TX in this browser to send")
    b.js("window.confirm = () => true")
    b.js("document.querySelector('#msg-arm').click()")
    check("armed: compose enabled", b.wait(f"!{DISABLED} && 'on'"), "on")

    hub.post("/api/hold", {"by": "winlink-qsy", "why": "Winlink QSY to 145.0900 MHz", "back": "channel 3"})
    check("a hold: compose disabled again", b.wait(f"{DISABLED} && 'off'", 10), "off")
    check("  the banner names the holder and the way back",
          b.wait("document.querySelector('.msg-hold') && document.querySelector('.msg-hold').textContent.includes('back to channel 3')"),
          True)
    b.js("window.confirmed = []; window.confirm = (m) => { window.confirmed.push(m); return true; }")
    b.js("document.querySelector('.msg-release').click()")
    check("Release asks first, naming the holder", b.wait("window.confirmed.length && window.confirmed[0].includes('winlink-qsy')"), True)
    check("  and clears the hold on the hub", T.wait_for(lambda: hub.status()["tx"]["holds"] == [], 10), True)
    check("  compose comes back", b.wait(f"!{DISABLED} && 'on'", 10), "on")

    k = T.Kiss(hub.kiss)
    T.clients_ready(hub, kiss=1)
    check("a KISS client: compose disabled (paused)", b.wait(f"{DISABLED} && 'off'", 10), "off")
    check("  the reason names it", b.js("document.querySelector('#msg-pause').textContent.includes('KISS client')"), True)
    k.close()
    check("  gone: enabled", b.wait(f"!{DISABLED} && 'on'", 15), "on")

    b.js("(() => { const t = document.querySelector('#msg-text'); t.value = 'x'.repeat(67); t.dispatchEvent(new Event('input')); })()")
    check("the counter", b.js("document.querySelector('#msg-count').textContent"), "67/67")
    b.js("(() => { const t = document.querySelector('#msg-text'); t.value = 'no {braces}'; t.dispatchEvent(new Event('input')); })()")
    b.js("(() => { const t = document.querySelector('#msg-to'); t.value = 'W7TST'; t.dispatchEvent(new Event('input')); })()")
    check("a '{': Send disabled, saying why", (b.js("document.querySelector('#msg-send').disabled"),
          b.js("document.querySelector('#msg-note').textContent").startswith("not allowed")), (True, True))
    b.js("(() => { const t = document.querySelector('#msg-text'); t.value = 'hello from the page'; t.dispatchEvent(new Event('input')); })()")
    b.js("document.querySelector('#msg-send').click()")
    sent = T.wait_for(lambda: [Frame.decode(r).tnc2() for _, r in radio.transmitted()], 10)
    msgno = (sent or [""])[0].rsplit("{", 1)[-1].rstrip("}")
    check("the message reaches the radio with exactly its text", sent,
          [f"N9ABC>APZRR0,WIDE1-1,WIDE2-1::W7TST    :hello from the page{{{msgno}}}"])
    check("  the thread shows it pending, slot 1/6",
          b.wait("(t => t.includes('slot') && t)([...document.querySelectorAll('#msg-thread tr.msg-row')]"
                 ".map((r) => r.children[3].textContent).join('|'))", 10),
          "pending · slot 1/6")
    radio.inject(T.aprs("W7TST", f":N9ABC    :ack{msgno}"))
    check("  a heard ack: the thread shows acked",
          b.wait("document.querySelector('#msg-thread tr.msg-row')?.dataset.state === 'acked' && 'acked'", 10), "acked")
    radio.inject(T.aprs("K7XYZ", ":N9ABC    :are you there{55"))
    b.js("aprsPage.showTab('stations')")
    check("an inbound message: an unread badge on the Messages tab", b.wait("document.querySelector('#msg-unread')?.textContent", 20), "1")
    if SHOT:
        b.js("aprsPage.showTab('messages')")
        b.screenshot(SHOT)
finally:
    b.close()
    hub.stop()
    radio.stop()
    shutil.rmtree(logdir, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
