"""A stand-in for `pat http`: enough of Pat v1.0.0's API for the Winlink tests.

    python3 tests/fake_pat.py [global flags] http --addr 127.0.0.1:PORT

Environment (read once at start: a test that needs one starts its own server):
  FAKE_PAT_RIGCTL   port of Radio Remote's Winlink rigctl lane; a session keys
                    through it like Pat's hamlib rig does (T 1 ... T 0)
  FAKE_PAT_LOG      file: one JSON line per request/event, each with "t"
  FAKE_PAT_TX_S     seconds a session stays keyed (default 1)
  FAKE_PAT_BUSY     "1": raise a busy-channel prompt before keying
  FAKE_PAT_QSY      "1": a URL with freq= QSYs the dial through the lane first
                    and QSXes it back after the session, then answers
                    /api/connect ~1 s later (the order pat.log shows live)
  FAKE_PAT_ARDOP_LOG  directory: write ardopcf-style debug log lines there
  FAKE_PAT_ID       how ardopcf identifies, keying through the lane:
                    "end"          IDFrame + CW ID before /api/connect returns,
                                   also after an abort (patched ardopcf)
                    "after_abort"  as "end", but after an abort the ID
                                   follows 1 s after /api/connect returns
                    "none"         no ID at the end (stock ardopcf)
  FAKE_PAT_LATE_ID_S  seconds after a session returns: ardopcf's "10 minute ID"
  FAKE_PAT_SEND_N   how many outbox messages a good session delivers (default all)
  FAKE_PAT_BROKEN_BOX  a mailbox name whose listing answers 500
  FAKE_PAT_NO_PTT   "1": a non-ARDOP session (packet, telnet) keys nothing, as
                    Pat's AX.25 over AGWPE doesn't; ARDOP still keys
  FAKE_PAT_BROKEN_ATTACH  "1": every attachment GET answers 500
  FAKE_PAT_BROKEN_READ    "1": the mark-read route answers 500
  FAKE_PAT_SLOW_MID  a MID whose message GET answers only after 2 s
  FAKE_PAT_LISTEN_FAIL  "1": requested listeners never start
  FAKE_PAT_P2P      comma list of P2P peer calls, each optionally CALL=FW1+FW2
                    (its advertised forwarders; default CALL). A session to
                    one sends only messages whose ONLY receiver is one of them
                    (wl2k-go GetOutbound); any other target is a CMS gateway:
                    everything except P2POnly

VARA HF (varahf:// URLs): as Pat with varahf.ptt_ctrl false, this fake never
keys the lane; the modem (tests/fake_xara.py at varahf.addr) does. Like
Pat-Vara it opens command and data ports on first use, sends the init lines,
then CONNECT; /api/connect returns at DISCONNECTED. A dead modem connection is
reopened on the next session. /api/disconnect sends DISCONNECT or (dirty)
ABORT. freq= is QSY'd through the rig varahf.rig names.

Global flags, as Pat's (before `http`):
  --config FILE     its "listen" applies when --listen is absent (Pat's fallback)
  --listen/-l M,..  listed in Status active_listeners; ax25+agwpe also
                    registers MyCall with the config's agwpe.addr, held to exit

Inbound sessions, announced as Pat does: "Got connect (<method>:<call>)", then
a Status push with connected=true and remote_addr ("AX.25:N0XYZ", "tcp:...").
  POST /fake/inbound {"remote", "method", "receive": n, "mode", "call"}
       "cooperative": ends on a graceful /api/disconnect (dirty answers 400:
                      Pat's dirty has no AX.25 branch)
       "stubborn":    ignores both and every SIGTERM; only SIGKILL ends it
  POST /fake/inbound/end   the remote hangs up (n messages into in/)
  POST /fake/ws/drop {"refuse_s": s}  close every WebSocket, refuse new ones for s

Routes (shapes as in Pat v1.0.0 api/api.go, api/mailbox.go):
  GET  /api/status, /api/mailbox/{box}, /api/mailbox/{box}/{mid}, /api/rmslist
  POST /api/mailbox/out (multipart; 400 without date)
  POST /api/mailbox/{box}  move the message X-Pat-SourcePath names (501 without
                    it, 400 when absent)
  DELETE /api/mailbox/{box}/{mid}  (404 when absent)
  POST /api/mailbox/{box}/{mid}/read  {"read": bool}; 400 for anything else
                    (stricter than Pat, which takes {} as false); 500 when absent
  GET  /api/mailbox/{box}/{mid}/{file}  an attachment (404 when absent)
  GET  /api/connect?url=  blocks until the session ends; {"NumReceived": n}
  GET|POST /api/disconnect?dirty=
  GET  /ws  MyCall, Status, LogLine, Prompt; reads prompt_response

FAKE_PAT_LOG gets a {"route": "req"} line for every request before any
validation, so a test can prove no request reached Pat; per-route lines
("compose", "move", ...) prove Pat accepted one.

Mailbox: in/ holds ABC123 (N0DAJ, unread), DEF456 (N0DAJ, read, Cc K7ABC,
note.txt attached) and GHI789 (W1XYZ, unread). MyCall is N9ABC.
"""
import asyncio
import hashlib
import itertools
import json
import os
import posixpath
import signal
import struct
import sys
import time
from urllib.parse import parse_qs, unquote, urlsplit

from aiohttp import web

ADDR = sys.argv[sys.argv.index("--addr") + 1]
GLOBAL = sys.argv[1:sys.argv.index("http")] if "http" in sys.argv else []


def _flag(names):
    for i, a in enumerate(GLOBAL):
        for n in names:
            if a == n and i + 1 < len(GLOBAL):
                return GLOBAL[i + 1]
            if a.startswith(n + "="):
                return a[len(n) + 1:]
    return None


CONFIG = {}
if _flag(["--config"]):
    try:
        with open(_flag(["--config"])) as _f:
            CONFIG = json.load(_f)
    except (OSError, ValueError):
        CONFIG = {}
_listen = _flag(["--listen", "-l"])
LISTEN = [m for m in (_listen.split(",") if _listen else (CONFIG.get("listen") or [])) if m]
if os.environ.get("FAKE_PAT_LISTEN_FAIL") == "1":
    LISTEN = []
P2P = {}
for _e in filter(None, os.environ.get("FAKE_PAT_P2P", "").split(",")):
    _c, _, _fw = _e.partition("=")
    P2P[_c.upper()] = [x.upper() for x in (_fw.split("+") if _fw else [_c])]
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
VARA = CONFIG.get("varahf") or {}


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
status = {"active_listeners": list(LISTEN), "connected": False, "dialing": False, "remote_addr": "",
          "http_clients": [], "config_hash": "x"}
abort = asyncio.Event()
prompt_answers = {}


def record(**kw):
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps({**kw, "t": time.time()}) + "\n")


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
                         "P2POnly": bool(form.get("p2ponly")),
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
    if request.query.get("mode") == "varahf":
        return web.Response(text=json.dumps([
            {"callsign": "N0CALL", "gridsquare": "DM42AA", "distance": 210, "azimuth": 20,
             "modes": "VARA 500", "freq": {"hz": 3590000, "khz": 3590.0, "desc": "3.590.00 MHz"},
             "dial": {"hz": 3588500, "khz": 3588.5, "desc": "3.588.50 MHz"},
             "url": "varahf:///N0CALL?bw=500&freq=3588.5", "prediction": None}]),
            content_type="text/plain")
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


_vara = {"conn": None, "data": None}
abort_dirty = [False]


async def vara_line(timeout=None):
    """One reply line from the modem's command port (CR-ended), or None at EOF."""
    r = _vara["conn"][0]
    try:
        raw = await asyncio.wait_for(r.readuntil(b"\r"), timeout)
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        _vara["conn"] = None
        return None
    return raw.decode().strip()


async def vara_send(line):
    w = _vara["conn"][1]
    w.write((line + "\r").encode())
    await w.drain()


async def vara_open():
    """Pat-Vara's first use: dial command and data ports, then the init lines."""
    host, port = (VARA.get("addr") or "localhost:8300").rsplit(":", 1)
    try:
        _vara["conn"] = await asyncio.open_connection(host, int(port))
        _vara["data"] = await asyncio.open_connection(host, int(port) + 1)
    except OSError as e:
        _vara["conn"] = None
        record(route="vara_open", ok=False, error=str(e))
        return False
    await vara_send("VERSION")
    v = await vara_line(5)
    for cmd in ("PUBLIC ON", "CWID ON", "COMPRESSION TEXT", f"MYCALL {MYCALL}", "LISTEN OFF"):
        await vara_send(cmd)
        await vara_line(5)
    record(route="vara_open", ok=v is not None and v.startswith("VERSION VARA"), version=v)
    return v is not None


async def vara_session(url):
    """(ok, received) for a varahf:// session: the modem keys, never us."""
    u = urlsplit(url)
    q = parse_qs(u.query)
    target = u.path.strip("/").upper()
    if _vara["conn"] is not None and _vara["conn"][0].at_eof():
        _vara["conn"] = None             # the modem went away: Pat's Ping fails, the handle re-inits
        record(route="vara_reinit")
    if _vara["conn"] is None and not await vara_open():
        await logline("Unable to establish connection to remote: VARA modem not answering")
        return False, 0
    if q.get("freq") and VARA.get("rig"):
        rig_addr = (CONFIG.get("hamlib_rigs") or {}).get(VARA["rig"], {}).get("address")
        if rig_addr:
            hz = int(float(q["freq"][0]) * 1000)
            got = await rig(f"F {hz}")
            record(route="qsy", hz=hz, reply=got, modem="varahf")
            await logline(f"QSY varahf: {q['freq'][0]}")
    await vara_send(f"BW{(q.get('bw') or ['2300'])[0]}")
    if await vara_line(5) is None:
        await logline("Unable to establish connection to remote: VARA modem closed")
        return False, 0
    await vara_send(f"CONNECT {MYCALL} {target}")
    connected, sent_stop = False, False
    while True:
        get = asyncio.ensure_future(vara_line())
        stop = asyncio.ensure_future(abort.wait())
        done, _ = await asyncio.wait({get, stop}, return_when=asyncio.FIRST_COMPLETED)
        if stop in done and not sent_stop and _vara["conn"] is not None:
            sent_stop = True
            await vara_send("ABORT" if abort_dirty[0] else "DISCONNECT")
            record(route="vara_stop", dirty=abort_dirty[0])
        if get not in done:
            line = await get
        else:
            stop.cancel()
            line = get.result()
        if line is None:
            await logline("Unable to establish connection to remote: VARA modem closed the connection")
            return False, 0
        record(route="vara_reply", line=line)
        if line.startswith("CONNECTED"):
            connected = True
            status.update(dialing=False, connected=True)
            await push({"Status": status})
            await logline(f"Connected to {target} (VARA HF)")
        elif line.startswith("DISCONNECTED"):
            break
    ok = connected and not sent_stop
    received = 0
    if ok:
        received = 1
        for m in outbound(url):
            BOXES["out"].remove(m)
            BOXES["sent"].append(m)
            await logline(f">FC EM {m['MID']} accepted")
    elif sent_stop:
        await logline("Connect cancelled")
    await logline("Disconnected.")
    return ok, received


async def connect(request):
    url = request.query.get("url", "")
    record(route="connect", url=url)
    if url.startswith("varahf:"):
        abort.clear()
        abort_dirty[0] = False
        status.update(dialing=True)
        await push({"Status": status})
        await logline(f"Connecting to {url}...")
        try:
            ok, received = await vara_session(url)
        finally:
            status.update(dialing=False, connected=False)
            await push({"Status": status})
        body = json.dumps({"NumReceived": received})
        if not ok:
            return web.Response(status=500, text="Session failure\n" + body)
        return web.Response(text=body, content_type="text/plain")
    abort.clear()
    status.update(dialing=True)
    await push({"Status": status})
    await logline(f"Connecting to {url}...")
    received = 0
    ok = True
    revert = None
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
                    for m in outbound(url):
                        BOXES["out"].remove(m)
                        BOXES["sent"].append(m)
                        await logline(f">FC EM {m['MID']} accepted")
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


def receivers(m):
    return [a["Addr"].upper() for a in (m.get("To") or []) + (m.get("Cc") or [])]


def outbound(url):
    """What Pat sends this session (wl2k-go GetOutbound): to a P2P peer,
    messages whose only receiver is one of its forwarders; to a CMS,
    everything but P2POnly. FAKE_PAT_SEND_N caps it (a gateway that took
    only some)."""
    target = urlsplit(url).path.strip("/").split("/")[-1].upper()
    fws = P2P.get(target)
    if fws is not None:
        out = [m for m in BOXES["out"] if len(receivers(m)) == 1 and receivers(m)[0] in fws]
    else:
        out = [m for m in BOXES["out"] if not m.get("P2POnly")]
    return out if SEND_N is None else out[:int(SEND_N)]


inbound = {"task": None, "mode": None, "end": None}


async def disconnect(request):
    dirty = request.query.get("dirty", "false")
    record(route="disconnect", dirty=dirty)
    if inbound["task"] and not inbound["task"].done():
        # Pat: graceful closes the exchange conn (true); dirty has no AX.25
        # or telnet branch (false, 400). Stubborn: the close never completes.
        if dirty == "true":
            return web.Response(status=400, text="{}")
        if inbound["mode"] == "cooperative":
            inbound["end"].set()
        return web.Response(text="{}", content_type="text/plain")
    if not (status["connected"] or status["dialing"]):
        return web.Response(status=400, text="Nothing to abort")
    abort_dirty[0] = dirty == "true"
    abort.set()
    return web.Response(text="{}", content_type="text/plain")


async def run_inbound(remote, method, receive):
    call = remote.split(":", 1)[-1].split(" via ")[0]
    if remote.startswith("tcp:"):
        call = inbound.get("call") or "N0XYZ"
    await logline(f"Got connect ({method}:{call})")
    status.update(connected=True, remote_addr=remote)
    await push({"Status": status})
    record(route="inbound", remote=remote, mode=inbound["mode"])
    try:
        await inbound["end"].wait()
        for i in range(receive):
            mid = f"P2P{i}{os.getpid() % 1000}"
            BOXES["in"].append({"MID": mid, "Date": "2026-11-05T20:00:00Z", "From": _addr(call),
                                "To": [_addr(MYCALL)], "Cc": None, "Subject": f"p2p {i}", "Body": "",
                                "BodyHTML": "", "Files": [], "P2POnly": False, "Unread": True})
        await logline("Disconnected.")
    finally:
        status.update(connected=False, remote_addr="")
        await push({"Status": status})
        record(route="inbound_end", remote=remote)


async def fake_inbound(request):
    b = await request.json()
    inbound.update(mode=b.get("mode", "cooperative"), end=asyncio.Event(), call=b.get("call"))
    inbound["task"] = asyncio.ensure_future(run_inbound(b.get("remote", "AX.25:N0XYZ"),
                                                        b.get("method", "ax25+agwpe"), int(b.get("receive", 0))))
    return web.json_response({"ok": True})


async def fake_inbound_end(request):
    if inbound["end"]:
        inbound["end"].set()
    return web.json_response({"ok": True})


ws_refuse_until = [0.0]


async def fake_ws_drop(request):
    b = await request.json()
    ws_refuse_until[0] = time.monotonic() + float(b.get("refuse_s", 0))
    for ws in list(clients):
        await ws.close()
    return web.json_response({"ok": True})


def on_sigterm():
    if inbound["task"] and not inbound["task"].done() and inbound["mode"] == "stubborn":
        record(route="sigterm", kept_running=True)
        asyncio.ensure_future(logline("Got abort signal, disconnecting..."))
        return
    record(route="sigterm", kept_running=False)
    os._exit(0)                        # as quick as Pat with nothing to abort


async def ws_handler(request):
    if time.monotonic() < ws_refuse_until[0]:
        return web.Response(status=503, text="not now")
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
app.router.add_post("/fake/inbound", fake_inbound)
app.router.add_post("/fake/inbound/end", fake_inbound_end)
app.router.add_post("/fake/ws/drop", fake_ws_drop)


async def _signals(app_):
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, on_sigterm)
app.on_startup.append(_signals)

_agw = {}


async def _agw_register(app_):
    """Listening on ax25+agwpe: register MyCall with the AGWPE TNC (the hub)."""
    addr = (CONFIG.get("agwpe") or {}).get("addr")
    if "ax25+agwpe" not in LISTEN or not addr:
        return
    host, port = addr.rsplit(":", 1)
    hdr = struct.Struct("<B3xcxBx10s10sI4x")
    try:
        r, w = await asyncio.open_connection(host, int(port))
        w.write(hdr.pack(0, b"X", 0, MYCALL.encode(), b"", 0))
        await w.drain()
        h = hdr.unpack(await asyncio.wait_for(r.readexactly(hdr.size), 5))
        data = await r.readexactly(h[5]) if h[5] else b""
        _agw["conn"] = (r, w)
        record(route="agw_register", call=MYCALL, ok=data == b"\x01")
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
        record(route="agw_register", call=MYCALL, ok=False, error=str(e))
app.on_startup.append(_agw_register)
record(route="start", pid=os.getpid(), argv=sys.argv[1:], listen=list(LISTEN))
web.run_app(app, host=HOST, port=int(PORT), print=None, handle_signals=False)
