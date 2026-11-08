"""APRS message management on the packet page (2026-11-08), in headless
Chromium, against the real hub (messaging full, no cooldown) on
tests/fake_kiss_radio.py.

  - the Messages badge counts unread messages (not threads), from the hub,
    so a second browser agrees; opening a thread marks it read on the hub
  - Mark unread brings the badge back; Mark all read clears it
  - Cancel (confirmed) stops one of ours being sent: cancelled, never sent
    again; Resend puts it back in compose; a delete while sending is refused
  - Delete one message (confirmed); Archive hides a thread, Archived (n)
    shows it, a new message brings it back, Unarchive restores it
  - Delete the whole thread (confirmed, naming the count)
  - Export downloads the thread as text; the heard list searches by text
  - every management POST is behind the cross-site guard

    CHROME=... python3 tests/packet_messages_manage_page_test.py [screenshot.png]
"""
import json
import os
import random
import shutil
import sys
import tempfile
import time
import urllib.request

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


def get(hub, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{hub.http}{path}", timeout=5) as r:
        return json.load(r)


def threads(hub, archived=False):
    d = get(hub, "/api/aprs/thread" + ("?archived=1" if archived else ""))
    return {t["call"]: t["unread"] for t in d["threads"]}


def sent(radio, text):
    """Frames on the radio carrying TEXT (full mode also sends acks)."""
    return sum(f":{text}{{" in Frame.decode(r).tnc2() for _, r in radio.transmitted())


def badge(b):
    return b.js("(document.querySelector('#msg-unread') || {}).textContent || ''")


def thread_states(b):
    return b.js("[...document.querySelectorAll('#msg-thread tr.msg-row')].map((r) => r.dataset.state).join(',')")


def click_in_row(b, text, cls):
    """Click the CLS button in the open thread's row whose message is TEXT."""
    return b.js(f"(() => {{ const r = [...document.querySelectorAll('#msg-thread tr.msg-row')]"
                f".find((r) => r.children[2].textContent === {json.dumps(text)});"
                f" const x = r && r.querySelector('button.{cls}'); if (x) x.click(); return !!x; }})()")


if not CHROME:
    raise SystemExit("no chromium found (set CHROME=)")

logdir = tempfile.mkdtemp(prefix="msgmanage-")
radio = T.FakeKissRadio().start()
hub = T.HubProc(radio, tx=True, extra=("--log-dir", logdir, "--aprs-messaging", "full", "--aprs-firmware-acks", "false",
                                       "--aprs-cooldown", "0"))
b = Browser(CHROME, CDP)
try:
    radio.inject(T.aprs("K7XYZ", ":N9ABC    :hello{11"))
    radio.inject(T.aprs("K7XYZ", ":N9ABC    :you there{12"))
    radio.inject(T.aprs("W7TST", ":N9ABC    :cq{21"))
    radio.inject(T.aprs("N0CALL", ":N0CALL-5 :not for us{31"))
    check("the hub: three unread, in two threads", T.wait_for(lambda: threads(hub) == {"K7XYZ": 2, "W7TST": 1} and threads(hub), 10),
          {"K7XYZ": 2, "W7TST": 1})
    b.goto(f"http://127.0.0.1:{hub.http}/#stations")
    check("the badge counts messages, not threads", b.wait("(document.querySelector('#msg-unread') || {}).textContent", 15), "3")
    check("  a thread row shows its count and last message",
          b.js("(r => r && [r.querySelector('.badge').textContent, r.querySelector('.thr-prev').textContent])"
               "(document.querySelector('#msg-threads tr[data-call=\"K7XYZ\"]'))"), ["2", "you there"])

    b.js("aprsPage.showTab('messages')")
    b.js("document.querySelector('#msg-threads tr[data-call=\"K7XYZ\"]').click()")
    check("open a thread: read on the hub (a second browser agrees)", T.wait_for(lambda: threads(hub).get("K7XYZ") == 0, 10), True)
    check("  the badge drops to the other thread's 1", b.wait("(document.querySelector('#msg-unread') || {}).textContent === '1' && '1'", 10), "1")
    b.js("document.querySelector('#thr-unread').click()")
    check("Mark unread: the newest received one is unread again", T.wait_for(lambda: threads(hub).get("K7XYZ") == 1, 10), True)
    check("  the thread closes (so it isn't read again at once)", b.wait("document.querySelector('#thr-tools').hidden && 'closed'", 10), "closed")
    check("  the badge says 2", b.wait("(document.querySelector('#msg-unread') || {}).textContent === '2' && '2'", 10), "2")
    b.js("document.querySelector('#thr-readall').click()")
    check("Mark all read: nothing unread on the hub", T.wait_for(lambda: threads(hub) == {"K7XYZ": 0, "W7TST": 0}, 10), True)
    check("  no badge", b.wait("!document.querySelector('#msg-unread') && 'none'", 10), "none")

    # Cancel and Resend.
    b.js("window.confirmed = []; window.confirm = (m) => { window.confirmed.push(m); return true; }")
    b.js("document.querySelector('#msg-arm').click()")
    b.wait("!document.querySelector('#msg-text').disabled && 'on'", 10)
    b.js("aprsPage.openThread('W7TST', true)")
    b.wait("document.querySelector('#msg-to').value === 'W7TST' && 'ok'", 10)
    b.js("(() => { const t = document.querySelector('#msg-text'); t.value = 'are you on'; t.dispatchEvent(new Event('input')); })()")
    b.js("document.querySelector('#msg-send').click()")
    check("a message sent: on the radio once", T.wait_for(lambda: sent(radio, "are you on") == 1, 10), True)
    check("  a Cancel button on it, no delete", b.wait(
        "(r => r && r.dataset.state === 'pending' && [!!r.querySelector('.m-cancel'), !!r.querySelector('.m-del')])"
        "([...document.querySelectorAll('#msg-thread tr.msg-row')].find((r) => r.children[2].textContent === 'are you on'))", 10),
        [True, False])
    mid = [m for m in get(hub, "/api/aprs/thread?with=W7TST")["messages"] if m["text"] == "are you on"][0]["id"]
    check("  the hub refuses deleting it while it is being sent",
          hub.post("/api/aprs/message/delete", {"id": mid}), (409, {"error": "it is still being sent: cancel it first"}))
    check("  and a thread delete too", hub.post("/api/aprs/thread/delete", {"with": "W7TST"})[0], 409)
    click_in_row(b, "are you on", "m-cancel")
    check("Cancel asks first, naming the station", b.wait("window.confirmed.length && window.confirmed.at(-1).includes('W7TST')"), True)
    check("  the thread shows it cancelled", b.wait(
        "(r => r && r.dataset.state === 'cancelled' && r.lastElementChild.previousElementSibling.textContent)"
        "([...document.querySelectorAll('#msg-thread tr.msg-row')].find((r) => r.children[2].textContent === 'are you on'))", 10),
        "cancelled")
    time.sleep(32)                       # past the second retry slot (t0 + 30 s)
    check("  never sent again (one frame, past slot 2)", sent(radio, "are you on"), 1)
    check("  cancelling again is refused by the hub", hub.post("/api/aprs/message/cancel", {"id": mid})[0], 409)
    click_in_row(b, "are you on", "m-resend")
    check("Resend: back in compose, not sent", (b.js("document.querySelector('#msg-to').value"),
          b.js("document.querySelector('#msg-text').value"), sent(radio, "are you on")), ("W7TST", "are you on", 1))

    # Delete one message, then archive.
    b.js("(() => { const t = document.querySelector('#msg-text'); t.value = ''; t.dispatchEvent(new Event('input')); })()")
    click_in_row(b, "cq", "m-del")
    check("Delete one: asks first, quoting it", b.wait("window.confirmed.at(-1).includes('\"cq\"')"), True)
    check("  gone on the hub; the rest stay", T.wait_for(lambda: [m["text"] for m in get(hub, "/api/aprs/thread?with=W7TST")["messages"]] == ["are you on"], 10), True)
    b.js("document.querySelector('#thr-archive').click()")
    check("Archive: out of the list on the hub", T.wait_for(lambda: "W7TST" not in threads(hub) and "W7TST" in threads(hub, True), 10), True)
    check("  the page offers Archived (1)", b.wait("(x => !x.hidden && x.textContent)(document.querySelector('#thr-archived'))", 10), "Archived (1)")
    b.js("document.querySelector('#thr-archived').click()")
    check("  which lists it", b.wait("!!document.querySelector('#msg-threads tr[data-call=\"W7TST\"]') && 'listed'", 10), "listed")
    b.js("document.querySelector('#msg-threads tr[data-call=\"W7TST\"]').click()")
    check("  open: the button reads Unarchive", b.wait("document.querySelector('#thr-archive').textContent === 'Unarchive' && 'ok'", 10), "ok")
    b.js("document.querySelector('#thr-archive').click()")
    check("Unarchive: back in the list", T.wait_for(lambda: "W7TST" in threads(hub), 10), True)
    b.js("document.querySelector('#thr-archived').click()")       # back to conversations
    b.wait("document.querySelector('#thr-archived').hidden && 'ok'", 10)
    hub.post("/api/aprs/thread/archive", {"with": "W7TST", "archived": True})
    radio.inject(T.aprs("W7TST", ":N9ABC    :back again{22"))
    check("a new message brings an archived thread back, unread", T.wait_for(lambda: threads(hub).get("W7TST") == 1, 10), True)

    # Export and delete the thread.
    b.js("aprsPage.openThread('K7XYZ', true)")
    b.wait("document.querySelectorAll('#msg-thread tr.msg-row').length === 2 && 'ok'", 10)
    b.js("window.dl = null; HTMLAnchorElement.prototype.click = function () {"
         " const name = this.download; fetch(this.href).then((r) => r.text()).then((t) => { window.dl = [name, t]; }); }")
    b.js("document.querySelector('#thr-export').click()")
    dl = b.wait("window.dl", 10) or ["", ""]
    check("Export: a dated text file named for the station", dl[0].startswith("aprs-k7xyz-") and dl[0].endswith(".txt"), True)
    check("  holding both messages, oldest first", [ln.split("\t")[2] for ln in dl[1].splitlines()[2:]], ["hello", "you there"])
    b.js("document.querySelector('#thr-delete').click()")
    check("Delete the thread: asks first, naming the count", b.wait("window.confirmed.at(-1).includes('K7XYZ (2 messages)')"), True)
    check("  gone on the hub", T.wait_for(lambda: "K7XYZ" not in threads(hub), 10), True)
    check("  the page is back to a new message", b.wait("document.querySelector('#msg-with-h').textContent", 10), "New message")

    # Search, and the guard.
    b.js("(() => { const q = document.querySelector('#msg-q'); q.value = 'NOT FOR'; q.dispatchEvent(new Event('change')); })()")
    check("search the heard list by text, any case",
          b.wait("(t => t.length === 1 && t[0])([...document.querySelectorAll('#msg-table tr')].filter((r) => r.children.length > 3)"
                 ".slice(1).map((r) => r.children[3].textContent))", 10), "not for us")
    for path, body in (("/api/aprs/message/cancel", {"id": mid}), ("/api/aprs/message/delete", {"id": mid}),
                       ("/api/aprs/thread/delete", {"with": "W7TST"}), ("/api/aprs/thread/read", {"with": "W7TST"}),
                       ("/api/aprs/thread/read-all", {}), ("/api/aprs/thread/archive", {"with": "W7TST"})):
        check(f"guarded: {path} without the header", hub.post(path, body, csrf=False)[0], 403)
    check("  and from a foreign Origin", hub.post("/api/aprs/thread/delete", {"with": "W7TST"}, origin="http://evil.example")[0], 403)
    check("  W7TST survived all of that", "W7TST" in threads(hub), True)
    if SHOT:
        b.js("aprsPage.openThread('W7TST', true)")
        time.sleep(1)
        b.screenshot(SHOT)
finally:
    b.close()
    hub.stop()
    radio.stop()
    shutil.rmtree(logdir, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
