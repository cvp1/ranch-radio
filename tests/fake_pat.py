"""A stand-in for `pat http`, enough of Pat v1.0.0's API for winlink_test.py.

    python3 tests/fake_pat.py http --addr 127.0.0.1:PORT

Environment:
  FAKE_PAT_RIGCTL   port of Radio Remote's Winlink rigctl lane; a session keys
                    through it like Pat's hamlib rig does (T 1 ... T 0)
  FAKE_PAT_LOG      file this fake appends one JSON line per request to, so the
                    test can see what Radio Remote sent (compose fields, aborts)
  FAKE_PAT_TX_S     seconds a session stays keyed (default 1)
  FAKE_PAT_BUSY     "1": raise a busy-channel prompt before keying
  FAKE_PAT_QSY      "1": a URL with freq= moves the dial through the lane first
                    ("QSY") and puts it back after the session ("QSX"), then
                    answers /api/connect ~1 s later -- the order pat.log showed
                    live on 2026-10-15 (QSX logged before Radio Remote disarmed)
  FAKE_PAT_ARDOP_LOG  directory: write ardopcf-style debug log lines there
                    (ARDOPDebug8515_<UTC date>.log) for the station ID below
  FAKE_PAT_ID       how ardopcf identifies, keying through the lane like Pat:
                    "end"   ID (IDFrame + CW ID) before /api/connect returns,
                            also after an abort (patched ardopcf)
                    "after_abort"  as "end", but after an abort /api/connect
                            returns at once and the ID follows 1 s later
                    "none"  no ID at the end (stock ardopcf, failed connect)
  FAKE_PAT_LATE_ID_S  seconds after a session returns: ardopcf's "10 minute ID"
  FAKE_PAT_SEND_N   how many outbox messages a good session delivers (moves to
                    sent/, as Pat does when the gateway accepts one); default all
  FAKE_PAT_BROKEN_BOX  a mailbox name whose listing answers 500
  FAKE_PAT_NO_PTT   "1": a session to a non-ARDOP URL (packet, telnet) keys
                    nothing through the lane, as Pat's AX.25 over the hub's
                    AGWPE doesn't; it "succeeds" after FAKE_PAT_TX_S. ARDOP
                    keys through the lane as before
  FAKE_PAT_BROKEN_ATTACH  "1": every attachment GET answers 500
  FAKE_PAT_BROKEN_READ    "1": the mark-read route answers 500
  FAKE_PAT_SLOW_MID  a MID whose message GET answers only after 2 s (a stale
                    read the page must drop)
  (All read once at start: a test that needs one starts its own server.)

Routes (shapes as in Pat v1.0.0 api/api.go, api/mailbox.go):
  GET  /api/status, /api/mailbox/{box}, /api/mailbox/{box}/{mid}, /api/rmslist
  POST /api/mailbox/out (multipart; 400 without date)
  POST /api/mailbox/{box}  move: X-Pat-SourcePath names the message, unescaped
                    and cleaned like Pat does (501 without it, 400 when the
                    source message is absent)
  DELETE /api/mailbox/{box}/{mid}  (404 when absent)
  POST /api/mailbox/{box}/{mid}/read  {"read": bool}; 400 for anything that is
                    not an object with a boolean read/Read (stricter than Pat,
                    which takes {} as false, so a bare `true` never earns a
                    "read" record); 500 when the message is absent
  GET  /api/mailbox/{box}/{mid}/{file}  an attachment, text/plain (404 when absent)
  GET  /api/connect?url=  blocks until the session ends; {"NumReceived": n}
  GET|POST /api/disconnect?dirty=
  GET  /ws  MyCall, Status, LogLine, Prompt; reads prompt_response / Pong

FAKE_PAT_LOG gets a {"route": "req", "method", "path", "headers"} line for
every request at entry, before any validation, so a test can prove that no
request reached Pat; the per-route lines ("compose", "move", "delete",
"read", ...) are the evidence that Pat accepted one.

Mailbox: in/ holds ABC123 (N0DAJ, unread, Cc null, no files), DEF456 (N0DAJ,
read, Cc K7ABC, subject "Re: Already a reply", note.txt attached) and GHI789
(W1XYZ, unread, subject "RE: Upper case"). MyCall is N9ABC.
"""
import asyncio
import hashlib
import itertools
import json
import os
import posixpath
import sys
from urllib.parse import unquote

from aiohttp import web

ADDR = sys.argv[sys.argv.index("--addr") + 1]
HOST, PORT = ADDR.rsplit(":", 1)
RIGCTL = int(os.environ.get("FAKE_PAT_RIGCTL", "0"))
LOG = os.environ.get("FAKE_PAT_LOG")
TX_S = float(os.environ.get("FAKE_PAT_TX_S", "1"))
BUSY = os.environ.get("FAKE_PAT_BUSY") == "1"
QSY = os.environ.get("FAKE_PAT_QSY") == "1"
ARDOP_LOG = os.environ.get("FAKE_PAT_ARDOP_LOG")
ID_MODE = os.environ.get("FAKE_PAT_ID", "")
LATE_ID_S = float(os.environ.get("FAKE_PAT_LATE_ID_S", "0"))
SEND_N = os.environ.get("FAKE_PAT_SEND_N")
BROKEN_BOX = os.environ.get("FAKE_PAT_BROKEN_BOX")
NO_PTT = os.environ.get("FAKE_PAT_NO_PTT") == "1"
BROKEN_ATTACH = os.environ.get("FAKE_PAT_BROKEN_ATTACH") == "1"
BROKEN_READ = os.environ.get("FAKE_PAT_BROKEN_READ") == "1"
SLOW_MID = os.environ.get("FAKE_PAT_SLOW_MID")
MYCALL = "N9ABC"


def _addr(a):
    return {"Proto": "", "Addr": a}


BOXES = {"in": [{"MID": "ABC123", "Date": "2026-10-14T20:50:00Z",
                 "From": _addr("N0DAJ"), "To": [_addr(MYCALL)],
                 "Cc": None, "Subject": "Test message", "Body": "", "BodyHTML": "",
                 "Files": [], "P2POnly": False, "Unread": True},
                {"MID": "DEF456", "Date": "2026-10-13T18:00:00Z",
                 "From": _addr("N0DAJ"), "To": [_addr(MYCALL)], "Cc": [_addr("K7ABC")],
                 "Subject": "Re: Already a reply", "Body": "", "BodyHTML": "",
                 "Files": [{"Name": "note.txt", "Size": 5}], "P2POnly": False, "Unread": False},
                {"MID": "GHI789", "Date": "2026-10-12T15:30:00Z",
                 "From": _addr("W1XYZ"), "To": [_addr(MYCALL)], "Cc": None,
                 "Subject": "RE: Upper case", "Body": "", "BodyHTML": "",
                 "Files": [], "P2POnly": False, "Unread": True}],
         "out": [], "sent": [], "archive": []}
BODIES = {"ABC123": "Hello from the test.\r\n", "DEF456": "Attached.\r\n",
          "GHI789": "Shouting, politely.\r\n"}
ATTACH = {("DEF456", "note.txt"): b"hello"}
_out_ids = itertools.count()

clients = set()
status = {"active_listeners": [], "connected": False, "dialing": False, "remote_addr": "",
          "http_clients": [], "config_hash": "x"}
abort = asyncio.Event()
prompt_answers = {}


def record(**kw):
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps(kw) + "\n")


async def push(obj):
    for ws in list(clients):
        try:
            await ws.send_str(json.dumps(obj))
        except ConnectionError:
            pass


async def logline(s):
    await push({"LogLine": s})


async def mailbox(request):
    box = request.match_info["box"]
    if box not in BOXES:
        return web.Response(status=404, text="404 page not found")
    if box == BROKEN_BOX:
        return web.Response(status=500, text="mailbox unreadable")
    return web.Response(text=json.dumps(BOXES[box]), content_type="text/plain")


def find(box, mid):
    for m in BOXES.get(box, []):
        if m["MID"] == mid:
            return m
    return None


@web.middleware
async def log_requests(request, handler):
    # Before any validation: "no request reached Pat" is then provable.
    record(route="req", method=request.method, path=request.raw_path,
           headers={k: request.headers[k] for k in ("X-Pat-SourcePath",) if k in request.headers})
    return await handler(request)


async def message(request):
    box, mid = request.match_info["box"], request.match_info["mid"]
    if mid == SLOW_MID:
        await asyncio.sleep(2.0)
    m = find(box, mid)
    if m is not None:
        return web.Response(text=json.dumps(dict(m, Body=m.get("Body") or BODIES.get(mid, ""))),
                            content_type="text/plain")
    return web.Response(status=500, text="no such message")


async def compose(request):
    form = await request.post()
    fields = {k: (v if isinstance(v, str) else f"<file {v.filename}>") for k, v in form.items()}
    uploads = [v for k, v in form.items() if k == "files" and not isinstance(v, str)]
    blobs = [(v, v.file.read()) for v in uploads]
    files = [{"name": v.filename, "size": len(data), "type": v.content_type,
              "sha256": hashlib.sha256(data).hexdigest()} for v, data in blobs]
    record(route="compose", fields=fields, files=files)
    if not form.get("date"):
        return web.Response(status=400, text="Missing date")
    mid = f"OUT{next(_out_ids)}"
    split = lambda s: [a for a in s.replace(",", " ").replace(";", " ").split() if a]  # noqa: E731
    BOXES["out"].append({"MID": mid, "Date": form.get("date"), "From": _addr(MYCALL),
                         "Subject": form.get("subject", ""),
                         "To": [_addr(a) for a in split(form.get("to", ""))],
                         "Cc": [_addr(a) for a in split(form.get("cc", ""))] or None,
                         "Body": form.get("body", ""), "Unread": False,
                         "Files": [{"Name": v.filename, "Size": len(data)} for v, data in blobs]})
    for v, data in blobs:
        ATTACH[(mid, v.filename)] = data
    return web.Response(status=201, text="Message posted (1 kB)")


async def delete(request):
    box, mid = request.match_info["box"], request.match_info["mid"]
    m = find(box, mid)
    if m is None:
        return web.Response(status=404, text="404 page not found")
    BOXES[box].remove(m)
    record(route="delete", box=box, mid=mid)
    return web.Response(text=json.dumps("OK"), content_type="application/json")


async def move(request):
    """Pat's postMessageHandler for a box other than out: rename the file
    X-Pat-SourcePath names (PathUnescape, then Clean) into this box."""
    dest = request.match_info["box"]
    if dest not in BOXES:
        return web.Response(status=404, text="404 page not found")
    src = request.headers.get("X-Pat-SourcePath")
    if not src:
        return web.Response(status=501, text="Not implemented")
    parts = posixpath.normpath(unquote(src)).strip("/").split("/")
    if len(parts) != 4 or parts[:2] != ["api", "mailbox"] or find(parts[2], parts[3]) is None:
        return web.Response(status=400, text="rename: no such file or directory")
    box, mid = parts[2], parts[3]
    m = find(box, mid)
    BOXES[box].remove(m)
    BOXES[dest].append(m)
    record(route="move", src=box, mid=mid, dest=dest)
    return web.Response(text=json.dumps("OK"), content_type="application/json")


async def mark_read(request):
    box, mid = request.match_info["box"], request.match_info["mid"]
    try:
        body = json.loads(await request.read())
    except ValueError:
        body = None
    val = body.get("read", body.get("Read")) if isinstance(body, dict) else None
    if not isinstance(val, bool):
        return web.Response(status=400, text="json: cannot unmarshal bool into Go value of type struct { Read bool }"
                            if body is True or body is False else "json: bad request")
    if BROKEN_READ:
        return web.Response(status=500, text="mark read failed")
    m = find(box, mid)
    if m is None:
        return web.Response(status=500, text="open: no such file or directory")
    m["Unread"] = not val
    record(route="read", box=box, mid=mid, read=val)
    return web.Response(text=json.dumps("OK"), content_type="application/json")


async def attachment(request):
    mi = request.match_info
    if BROKEN_ATTACH:
        return web.Response(status=500, text="attachment unreadable")
    data = ATTACH.get((mi["mid"], mi["file"])) if find(mi["box"], mi["mid"]) else None
    if data is None:
        return web.Response(status=404, text="404 page not found")
    return web.Response(body=data, headers={"Content-Type": "text/plain"})


async def rmslist(request):
    record(route="rmslist", query=dict(request.query))
    return web.Response(text=json.dumps([
        {"callsign": "N0DAJ", "gridsquare": "DM34OA", "distance": 338, "azimuth": 315,
         "modes": "ARDOP 2000", "freq": {"hz": 3590000, "khz": 3590.0, "desc": "3.590.00 MHz"},
         "dial": {"hz": 3588500, "khz": 3588.5, "desc": "3.588.50 MHz"},
         "url": "ardop:///N0DAJ?bw=2000MAX&freq=3588.5", "prediction": None}]),
        content_type="text/plain")


async def get_status(request):
    return web.Response(text=json.dumps(status), content_type="text/plain")


_rig = None


async def rig(cmd):
    """One rigctl connection held open, as Pat's hamlib rig keeps it (a client
    that disconnects while keyed is unkeyed by Radio Remote)."""
    global _rig
    if _rig is None:
        _rig = await asyncio.open_connection("127.0.0.1", RIGCTL)
    r, w = _rig
    w.write((cmd + "\n").encode())
    await w.drain()
    return (await r.readline()).decode().strip()


def ardop_log(msg, level="D"):
    if not ARDOP_LOG:
        return
    import time
    now = time.time()
    day = time.strftime("%Y%m%d", time.gmtime(now))
    stamp = time.strftime("%H:%M:%S", time.gmtime(now)) + f".{int(now * 1000) % 1000:03d}"
    with open(os.path.join(ARDOP_LOG, f"ARDOPDebug8515_{day}.log"), "a") as f:
        f.write(f"{stamp} {level} {msg}\n")


async def send_id(reason):
    """ardopcf's SendID(): IDFrame then CW ID, each keyed through the lane."""
    ardop_log(f"SendID  N0CALL:[FN31]  {reason}")
    ardop_log("Sending Frame Type IDFrame", "I")
    replies = [await rig("T 1")]
    ardop_log("[Main.KeyPTT]  PTT-TRUE")
    await asyncio.sleep(0.3)
    await rig("T 0")
    ardop_log("[Main.KeyPTT]  PTT-FALSE")
    ardop_log("Sending CW ID of N0CALL")
    replies.append(await rig("T 1"))
    ardop_log("[Main.KeyPTT]  PTT-TRUE")
    await asyncio.sleep(0.6)
    await rig("T 0")
    ardop_log("[Main.KeyPTT]  PTT-FALSE")
    record(route="id", reason=reason, replies=replies)


async def later(delay, coro):
    await asyncio.sleep(delay)
    await coro


async def connect(request):
    url = request.query.get("url", "")
    record(route="connect", url=url)
    abort.clear()
    status.update(dialing=True)
    await push({"Status": status})
    await logline(f"Connecting to {url}...")
    received = 0
    ok = True
    revert = None
    from urllib.parse import parse_qs, urlsplit
    freq = parse_qs(urlsplit(url).query).get("freq")
    if QSY and freq:
        old = await rig("f")
        got = await rig(f"F {int(float(freq[0]) * 1000)}")
        record(route="qsy", hz=int(float(freq[0]) * 1000), reply=got)
        await logline(f"QSY ardop: {freq[0]}")
        revert = old
        await asyncio.sleep(6.0)       # ARDOP setup; the ATU tune (2 s settle + 2.5 s sim) ends first
    try:
        if BUSY:
            prompt_answers.clear()
            await push({"Prompt": {"id": "busy1", "kind": "busy-channel",
                                   "message": "Waiting for clear channel...", "options": []}})
            for _ in range(100):
                if "busy1" in prompt_answers or abort.is_set():
                    break
                await asyncio.sleep(0.1)
            record(route="prompt_answer", value=prompt_answers.get("busy1"))
            if prompt_answers.get("busy1") == "abort":
                await logline("Unable to establish connection to remote: Connect cancelled")
                ok = False
        if ok:
            # Keyed through Radio Remote's lane, as Pat's hamlib PTT would be.
            keys = not (NO_PTT and not url.startswith("ardop"))
            got = await rig("T 1") if keys else "RPRT 0"
            if keys:
                record(route="ptt_on", reply=got)
            if got != "RPRT 0":
                await logline("Unable to establish connection to remote: PTT refused")
                ok = False
            else:
                status.update(dialing=False, connected=True)
                await push({"Status": status})
                ardop_log("[Main.KeyPTT]  PTT-TRUE")
                try:
                    await asyncio.wait_for(abort.wait(), TX_S)
                    await logline("Connect cancelled")
                    ok = False
                except asyncio.TimeoutError:
                    received = 1
                    # The gateway accepted the outbox: Pat moves each to sent/.
                    n = len(BOXES["out"]) if SEND_N is None else int(SEND_N)
                    for m in BOXES["out"][:n]:
                        BOXES["sent"].append(m)
                        await logline(f">FC EM {m['MID']} accepted")
                    del BOXES["out"][:n]
                    await push({"Notification": {"title": "New message from N0DAJ",
                                                 "body": "Test"}})
                if keys:
                    await rig("T 0")
                    ardop_log("[Main.KeyPTT]  PTT-FALSE")
                if ID_MODE == "end" or (ID_MODE == "after_abort" and ok):
                    await send_id("Rcvd END in IDLE ProtocolState" if ok else "ABORT after transmitting")
                elif ID_MODE == "after_abort":
                    asyncio.ensure_future(later(1.0, send_id("ABORT after transmitting")))
                if LATE_ID_S:
                    asyncio.ensure_future(later(LATE_ID_S, send_id("10 minute ID")))
                await logline("Disconnected.")
    finally:
        status.update(dialing=False, connected=False)
        await push({"Status": status})
    if revert is not None:
        got = await rig(f"F {revert}")
        record(route="qsx", hz=int(revert), reply=got)
        await logline(f"QSX ardop: {int(revert) / 1000:.3f}")
        await asyncio.sleep(1.0)
    body = json.dumps({"NumReceived": received})
    if not ok:
        return web.Response(status=500, text="Session failure\n" + body)
    return web.Response(text=body, content_type="text/plain")


async def disconnect(request):
    record(route="disconnect", dirty=request.query.get("dirty", "false"))
    if not (status["connected"] or status["dialing"]):
        return web.Response(status=400, text="Nothing to abort")
    abort.set()
    return web.Response(text="{}", content_type="text/plain")


async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    clients.add(ws)
    await ws.send_str(json.dumps({"MyCall": "N9ABC"}))
    await ws.send_str(json.dumps({"Status": status}))
    try:
        async for msg in ws:
            m = json.loads(msg.data)
            if "prompt_response" in m:
                pr = m["prompt_response"]
                prompt_answers[pr["id"]] = pr["value"]
    finally:
        clients.discard(ws)
    return ws


app = web.Application(middlewares=[log_requests])
app.router.add_get("/api/status", get_status)
app.router.add_get("/api/mailbox/{box}", mailbox)
app.router.add_get("/api/mailbox/{box}/{mid}", message)
app.router.add_post("/api/mailbox/out", compose)
app.router.add_post("/api/mailbox/{box}", move)
app.router.add_delete("/api/mailbox/{box}/{mid}", delete)
app.router.add_post("/api/mailbox/{box}/{mid}/read", mark_read)
app.router.add_get("/api/mailbox/{box}/{mid}/{file}", attachment)
app.router.add_get("/api/rmslist", rmslist)
app.router.add_route("*", "/api/connect", connect)
app.router.add_route("*", "/api/disconnect", disconnect)
app.router.add_get("/ws", ws_handler)
record(route="start", pid=os.getpid())
web.run_app(app, host=HOST, port=int(PORT), print=None)
