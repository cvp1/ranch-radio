"""Logbook sync: every QSO JTDX logs, to Wavelog, QRZ and LoTW, and the
confirmations back (PLAN v3, staged/logbook-sync). A station module, as
Winlink and the packet hub are: station.py runs it from station.json's
"logsync" block; it is not a switchable add-on, and the Logbook tab
(addons/logbook.py) only shows its status. Switching that tab off never
stops logging.

    JTDX --UDP--> intake (logsync_udp: multicast beside GridTracker, or a
                  unicast relay) --> ledger (logsync_ledger, SQLite, with an
                  append-only ADIF safety copy written first)
           ledger --> one sender per destination, independent:
                  Wavelog  api/qso, one QSO per call
                  QRZ      ACTION=INSERT (never REPLACE)
                  LoTW     tqsl -x -d -a compliant -l LOCATION -u FILE
           pulls  --> LoTW qso_qsl=no (receipts) and yes (confirmations),
                  QRZ FETCH, Wavelog get_contacts_adif (hand-typed QSOs)

Which message logs a QSO (PLAN §4): Logged ADIF (type 12) when it comes;
else QSO Logged (type 5) after pair_wait_s with no 12. The two collapse by
exact identity (logsync_adif.key), never by a time window.

Credentials (PLAN §3, §9): each is a file (mode 600) named in the host file,
read at the moment of use and never kept in the snapshot, the ledger, a log
line, argv or an exception string. Every reason the ledger or the page sees
passes scrub() first; network errors are reported by class only (aiohttp's
messages can carry the URL, and LoTW's URL carries the password). TQSL never
gets -p: the certificate must have no password; if TQSL waits on a dialog
anyway it is killed at tqsl_timeout_s.

Backfill (PLAN §7): on first start the Wavelog log is imported with QRZ and
LoTW held_backfill, which no sender reads; LoTW (qso_qsl=no since 1900) and
QRZ (the whole log) then mark what they already have. Only
`ranch-radio logsync backfill --apply HASH` releases the rest.
"""

import asyncio
import html
import json
import logging
import os
import re
import signal
import tempfile
import time
from urllib.parse import parse_qsl

import logsync_adif as adif
import logsync_udp as udp
from logsync_config import (DEFAULT_LEDGER, DESTS, LOTW_URL, QRZ_URL, TIMING, Config,  # noqa: F401
                             check_block)
from logsync_ledger import Ledger

log = logging.getLogger("logsync")

LOTW_BATCH = 50
LOTW_MISSES = 3
WAVELOG_DUP_WINDOW_S = 15 * 60     # Logbook_model.php import_check: +-15 min, call/band/mode
STDERR_CAP = 16 * 1024
QRZ_PAGE = 250
MAX_PAGES = 400
OK, RETRY, HOLD_ALL = "ok", "retry", "hold_all"


class SecretError(Exception):
    """A credential file is missing, unreadable, too open, or empty. The
    message names the file, never its contents."""


# ---------------------------------------------------------------- credentials

def read_secret(path):
    """A credential from its file, at the moment of use. SecretError names
    the file (never the value) when it can't be used."""
    p = os.path.expanduser(str(path))
    try:
        st = os.stat(p)
    except OSError:
        raise SecretError(f"credential file {path} not found") from None
    if st.st_mode & 0o077:
        raise SecretError(f"credential file {path} is readable by others (chmod 600 it)")
    try:
        with open(p) as f:
            v = f.read().strip()
    except OSError:
        raise SecretError(f"credential file {path} can't be read") from None
    if not v or "\n" in v:
        raise SecretError(f"credential file {path} must hold one line")
    return v


_SCRUB = [re.compile(r"(?i)(password|passwd|key|api_key|apikey)(=|\":\s*\"|:\s*)[^&\s\"']+"),
          re.compile(r"https?://\S+")]


def scrub(text, secrets=(), cap=500):
    """Text fit for the ledger, a log line or the page: no credential, no URL
    (LoTW's carries the password), no control characters, capped."""
    t = str(text or "")
    for s in secrets:
        if s:
            t = t.replace(s, "[REDACTED]")
    t = _SCRUB[0].sub(r"\1\2[REDACTED]", t)
    t = _SCRUB[1].sub("[URL]", t)
    t = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", t)
    home = os.path.expanduser("~")
    if home and home != "/":
        t = t.replace(home, "~")
    return t[:cap]


# ---------------------------------------------------------------- the engine

class LogSync:
    def __init__(self, cfg, notify=None, alert=None, clock=time.time):
        self.cfg = cfg if isinstance(cfg, Config) else Config(cfg)
        self.notify_cb, self.alert_cb, self.clock = notify, alert, clock
        self.ledger = None
        self.intake = None
        self.http = None
        self.tasks = []
        self.wake = {}
        self.holds = {}                  # dest -> reason class (the whole destination waits)
        self.hold_mark = {}              # dest -> what clears the hold (a file's mtime)
        self.last_error = {}             # dest -> error class
        self.backoff = {}
        self.held5 = {}                  # key -> (fields, TimerHandle): a type 5 waiting for its 12
        self.imported = asyncio.Event()
        self.tqsl_lock = asyncio.Lock()
        self.tqsl_proc = None
        self._secrets = set()
        self._notify_handle = None
        self.crash_after_send = None     # tests: raise after a destination answered, before the ledger
        self.pulls_done = {}             # pull name -> runs finished (tests wait on it)
        self.started = False

    # -- lifecycle

    async def start(self):
        import aiohttp
        c = self.cfg
        self.ledger = Ledger(c.ledger, c.dests, clock=self.clock)
        n = self.ledger.recover()
        if n:
            log.warning("logsync: %d send(s) were in flight at the last stop; checking them before any retry", n)
        self.safety = os.path.join(os.path.dirname(c.ledger) or ".", "logsync-safety.adi")
        fwd = udp.Forwarder(c.forward)
        if c.shape == "multicast":
            self.intake = udp.MulticastIntake(c.group, c.port, self._message, fwd, iface=c.iface)
        else:
            self.intake = udp.RelayIntake(c.bind, c.port, self._message, fwd, relay=c.relay)
        await self.intake.start()
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=float(c.timing["http_timeout_s"])))
        for d in c.dests:
            self.wake[d] = asyncio.Event()
            self.backoff[d] = c.backoff[0]
        if not c.wavelog or self.ledger.cursor("backfill_wavelog"):
            self.imported.set()
        for d in c.dests:
            self.tasks.append(asyncio.ensure_future(self._sender(d)))
        if c.wavelog:
            self.tasks.append(asyncio.ensure_future(self._every(self.pull_wavelog, "wavelog_every_s")))
        if c.lotw:
            self.tasks.append(asyncio.ensure_future(self._every(self.pull_lotw, "lotw_every_s", after_import=True)))
        if c.qrz:
            self.tasks.append(asyncio.ensure_future(self._every(self.pull_qrz, "qrz_every_s", after_import=True)))
        self.started = True
        log.info("logsync: listening (%s, port %d); destinations: %s", c.shape, c.port,
                 ", ".join(c.dests) or "none")

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        p = self.tqsl_proc
        if p is not None and p.returncode is None:
            _kill_group(p)
            await p.wait()
        for _, h in self.held5.values():
            h.cancel()
        self.held5.clear()
        if self.intake is not None:
            await self.intake.stop()
        if self.http is not None:
            await self.http.close()
        if self._notify_handle is not None:
            self._notify_handle.cancel()
        if self.ledger is not None:
            self.ledger.close()
        self.started = False

    # -- intake

    def _message(self, typ, data, addr):
        if typ == 5:
            f = udp.parse_logged(data)
            k = adif.key(adif.qso(f, self.cfg.call))
            if k in self.held5 or (self.ledger and self.ledger.db.execute(
                    "SELECT 1 FROM qso WHERE key=?", (k,)).fetchone()):
                if k not in self.held5:
                    self.record(f, "jtdx5")    # a re-log: record() decides same or revision
                return
            h = asyncio.get_running_loop().call_later(float(self.cfg.timing["pair_wait_s"]), self._lone5, k)
            self.held5[k] = (f, h)
        elif typ == 12:
            f = udp.parse_adif_message(data)
            k = adif.key(adif.qso(f, self.cfg.call))
            w = self.held5.pop(k, None)
            if w is not None:
                w[1].cancel()
            self.record(f, "jtdx12")

    def _lone5(self, k):
        w = self.held5.pop(k, None)
        if w is not None:
            self.record(w[0], "jtdx5")

    def record(self, fields, source):
        """Write the safety copy, then the ledger; wake the senders."""
        line = adif.record(fields)
        with open(self.safety, "a") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        qid, what = self.ledger.add(fields, source, {d: "pending" for d in self.cfg.dests}, own=self.cfg.call)
        q = self.ledger.qso(qid)
        log.info("logsync: %s %s %s %s: %s (%s)", q["call"], q["band"], q["mode"] + ("/" + q["submode"] if q["submode"] else ""),
                 q["ts"], what, source)
        if what == "new":
            for e in self.wake.values():
                e.set()
        if what != "same":
            self._changed()
        return qid, what

    # -- senders

    async def _sender(self, dest):
        send = {"wavelog": self._send_wavelog, "qrz": self._send_qrz, "lotw": self._send_lotw}[dest]
        while True:
            if dest in self.holds:
                if not self._hold_cleared(dest):
                    self.wake[dest].clear()
                    await self._wait(self.wake[dest], 5.0)
                    continue
                log.info("logsync: %s: the hold (%s) is cleared; sending again", dest, self.holds.pop(dest))
                self._changed()
            try:
                if dest != "lotw" and self.ledger.in_state(dest, "unknown"):
                    r = await self._reconcile(dest)
                else:
                    rows = self._pick(dest)
                    if not rows:
                        self.wake[dest].clear()
                        await self._wait(self.wake[dest], 5.0)
                        continue
                    r = await send(rows)
            except SecretError as e:
                r = self._hold(dest, "credential", scrub(str(e), self._secrets))
            except _Crash:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as e:            # a sender bug must not stop the others
                log.error("logsync: %s sender: %s", dest, type(e).__name__)
                self.last_error[dest] = type(e).__name__
                r = RETRY
            if r == RETRY:
                await asyncio.sleep(self.backoff[dest])
                self.backoff[dest] = min(self.backoff[dest] * 2, self.cfg.backoff[1])
            elif r == OK:
                self.backoff[dest] = self.cfg.backoff[0]
                self.last_error.pop(dest, None)
            self._changed()

    @staticmethod
    async def _wait(ev, timeout):
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except TimeoutError:
            pass

    def _pick(self, dest):
        if dest != "lotw":
            return self.ledger.pending(dest, 1)
        head = self.ledger.pending(dest, 1)
        if head and head[0]["backfill"]:
            return self.ledger.pending(dest, LOTW_BATCH, backfill=True)     # backfill: batches of <= 50
        return head                                                        # live: one QSO per TQSL run

    def _hold(self, dest, cls, detail, mark=None):
        """The whole destination waits (bad key, TQSL hung); alert once."""
        if dest not in self.holds:
            self.holds[dest] = cls
            self.hold_mark[dest] = mark if mark is not None else self._mark(dest)
            self._alert(dest, cls, f"{dest}: {detail}")
        return HOLD_ALL

    def _mark(self, dest):
        f = {"wavelog": "key_file", "qrz": "key_file", "lotw": "password_file"}[dest]
        try:
            return os.stat(os.path.expanduser(getattr(self.cfg, dest)[f])).st_mtime_ns
        except OSError:
            return None

    def _hold_cleared(self, dest):
        """A credential hold clears when its file changes; a TQSL hang only at restart."""
        if self.holds.get(dest) == "tqsl hung":
            return False
        return self._mark(dest) != self.hold_mark.get(dest)

    def _alert(self, dest, cls, msg):
        msg = scrub(msg, self._secrets)
        self.ledger.alert(dest, cls, msg)
        log.warning("logsync: ALERT %s", msg)
        if self.alert_cb:
            try:
                self.alert_cb(msg)
            except Exception as e:
                log.debug("logsync: alert callback: %s", type(e).__name__)

    def _secret(self, path):
        v = read_secret(path)
        self._secrets.add(v)
        return v

    def _net_error(self, dest, e):
        self.last_error[dest] = type(e).__name__
        log.warning("logsync: %s: %s", dest, type(e).__name__)     # the class only: messages can carry the URL

    def _crash(self, dest):
        if self.crash_after_send == dest:
            raise _Crash(dest)

    # Wavelog: application/controllers/Api.php qso(): 201 created, or 400 abort
    # with reasons in "messages" (a duplicate says "Duplicate for <CALL>").
    async def _send_wavelog(self, rows):
        import aiohttp
        q = rows[0]
        w = self.cfg.wavelog
        key = self._secret(w["key_file"])             # before the attempting mark: a missing key holds, never strands
        payload = self.ledger.latest_adif(q["id"])
        self.ledger.attempting([q["id"]], "wavelog", payload)
        body = {"key": key, "station_profile_id": w["station_profile_id"], "type": "adif", "string": payload}
        try:
            async with self.http.post(w["url"].rstrip("/") + "/index.php/api/qso", json=body) as r:
                status = r.status
                text = await r.text()
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            self._net_error("wavelog", e)
            self.ledger.set(q["id"], "wavelog", "unknown", "network", type(e).__name__)
            return RETRY
        self._crash("wavelog")
        try:
            d = json.loads(text)
        except ValueError:
            d = {}
        msgs = " ".join(str(m) for m in (d.get("messages") or []) if m)
        if status == 201 and d.get("status") == "created":
            if int(d.get("adif_count") or 0) < 1:
                self.ledger.set(q["id"], "wavelog", "held", "nothing imported", "Wavelog answered created but imported 0 records")
                self._alert("wavelog", "nothing imported", f"Wavelog imported nothing for {q['call']} {q['ts']}")
                return OK
            self.ledger.set(q["id"], "wavelog", "sent")
            return OK
        if status == 400 and d.get("status") == "abort" and "duplicate" in msgs.lower():
            return self._duplicate("wavelog", q, scrub(msgs, self._secrets))
        if status in (401, 403):
            self.ledger.set(q["id"], "wavelog", "pending")
            return self._hold("wavelog", "auth", f"Wavelog refused the API key (HTTP {status}); "
                                                 "every QSO waits until the key file changes")
        if status == 429 or status >= 500:
            self.last_error["wavelog"] = f"HTTP {status}"
            self.ledger.set(q["id"], "wavelog", "pending")
            return RETRY
        self.ledger.set(q["id"], "wavelog", "held", "rejected", scrub(f"HTTP {status} {msgs}", self._secrets))
        self._alert("wavelog", "rejected", f"Wavelog refused {q['call']} {q['ts']} (HTTP {status})")
        return OK

    def _duplicate(self, dest, q, detail):
        """A destination said "duplicate". It counts as sent (PLAN §6) unless it
        can't be this QSO: a correction (the destination kept the original),
        or, for Wavelog, another QSO of ours inside its 15-minute window."""
        if q["correction"]:
            self.ledger.set(q["id"], dest, "held", "kept original",
                            f"{dest} kept the original record; edit it there ({detail})")
            self._alert(dest, "kept original", f"{dest} refused the correction for {q['call']} {q['ts']} as a duplicate")
            return OK
        if dest == "wavelog" and self.ledger.siblings(q["id"], WAVELOG_DUP_WINDOW_S):
            self.ledger.set(q["id"], dest, "held", "duplicate window",
                            "Wavelog keeps one QSO per call, band and mode within 15 minutes; this one was refused")
            self._alert(dest, "duplicate window", f"Wavelog refused {q['call']} {q['ts']}: another QSO with "
                                                  "them on this band and mode within 15 minutes")
            return OK
        self.ledger.set(q["id"], dest, "sent", None, "already there (duplicate)")
        return OK

    def _qrz_headers(self):
        from ranch_radio import __version__
        return {"User-Agent": f"RanchRadio/{__version__} ({self.cfg.call})"}

    async def _qrz(self, data):
        """POST to the QRZ Logbook API -> (fields, adif text or None)."""
        async with self.http.post(self.cfg.qrz["url"], data=data, headers=self._qrz_headers()) as r:
            status = r.status
            text = await r.text()
        if status != 200:
            raise _Http(status)
        head, sep, adif_text = text.partition("ADIF=")
        fields = {k.upper(): v for k, v in parse_qsl(head.rstrip("&"), keep_blank_values=True)}
        return fields, (html.unescape(adif_text) if sep else None)

    # QRZ Logbook API: RESULT=OK|FAIL|AUTH; a duplicate is FAIL with a free-text REASON.
    async def _send_qrz(self, rows):
        import aiohttp
        q = rows[0]
        key = self._secret(self.cfg.qrz["key_file"])
        payload = self.ledger.latest_adif(q["id"])
        self.ledger.attempting([q["id"]], "qrz", payload)
        try:
            f, _ = await self._qrz({"KEY": key, "ACTION": "INSERT", "ADIF": payload})
        except _Http as e:
            self.last_error["qrz"] = f"HTTP {e.status}"
            self.ledger.set(q["id"], "qrz", "pending")
            return RETRY
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            self._net_error("qrz", e)
            self.ledger.set(q["id"], "qrz", "unknown", "network", type(e).__name__)
            return RETRY
        self._crash("qrz")
        res, reason = f.get("RESULT", "").upper(), scrub(f.get("REASON", ""), self._secrets)
        if res == "OK":
            self.ledger.set(q["id"], "qrz", "sent", None, f"LOGID {f.get('LOGID', '?')}")
            return OK
        if res == "AUTH":
            self.ledger.set(q["id"], "qrz", "pending")
            return self._hold("qrz", "auth", "QRZ refused the API key; every QSO waits until the key file changes")
        if res == "FAIL" and re.search(r"duplicate", reason, re.I):
            return self._duplicate("qrz", q, reason)
        if res == "FAIL":
            self.ledger.set(q["id"], "qrz", "held", "rejected", reason)
            self._alert("qrz", "rejected", f"QRZ refused {q['call']} {q['ts']}")
            return OK
        self.ledger.set(q["id"], "qrz", "unknown", "no result", "QRZ answered without a RESULT")
        return RETRY

    # LoTW: one QSO per TQSL run (backfill: batches of <= 50); TQSL's exit code
    # (lotw-help/cmdline): 0 all uploaded; 8 none, 9 some, processed because
    # of duplicates OR out of date range; 2 rejected; 11 network.
    async def _send_lotw(self, rows):
        lw = self.cfg.lotw
        qids = [r["id"] for r in rows]
        recs = [self.ledger.latest_adif(i) for i in qids]
        doc = adif.document(recs)
        async with self.tqsl_lock:                       # one TQSL at a time
            d = tempfile.mkdtemp(prefix="logsync-tqsl-", dir=os.path.dirname(self.cfg.ledger) or None)
            path = os.path.join(d, "upload.adi")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(doc)
            self.ledger.attempting(qids, "lotw", doc)
            argv = [os.path.expanduser(lw["tqsl"]), "-x", "-d", "-a", "compliant", "-l", lw["location"], "-u", path]
            try:
                rc, err = await self._tqsl(argv)
            finally:
                for p in (path,):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
                try:
                    os.rmdir(d)
                except OSError:
                    pass
        what = f"{len(qids)} QSO(s) from {rows[0]['call']} {rows[0]['ts']}"
        if rc is None:
            self.ledger.set(qids, "lotw", "held", "tqsl hung",
                            f"TQSL did not finish in {self.cfg.timing['tqsl_timeout_s']} s (a dialog?); killed. {err}")
            return self._hold("lotw", "tqsl hung", f"TQSL hung on {what} and was killed; LoTW waits until "
                                                   "the station restarts (check the certificate has no password)")
        self._crash("lotw")
        if rc == 0:
            self.ledger.set(qids, "lotw", "submitted", None, err)
            return OK
        if rc in (8, 9):
            self.ledger.set(qids, "lotw", "unknown", f"tqsl {rc}", err)
            return OK
        if rc == 11:
            self.last_error["lotw"] = "tqsl 11 (network)"
            self.ledger.set(qids, "lotw", "pending")
            return RETRY
        if rc == 2:
            self.ledger.set(qids, "lotw", "held", "rejected", err)
            self._alert("lotw", "rejected", f"LoTW rejected {what}")
            return OK
        if rc == 3:
            self.ledger.set(qids, "lotw", "unknown", "tqsl 3", err)
            return OK
        self.ledger.set(qids, "lotw", "pending")
        return self._hold("lotw", f"tqsl {rc}", f"TQSL failed (exit {rc}) on {what}: {err}; LoTW waits until "
                                                f"the password file changes or the station restarts")

    async def _tqsl(self, argv):
        """(exit code or None if it hung, sanitised stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        self.tqsl_proc = proc
        buf = bytearray()

        async def drain():
            while chunk := await proc.stderr.read(4096):
                if len(buf) < STDERR_CAP:
                    buf.extend(chunk[:STDERR_CAP - len(buf)])
            return await proc.wait()
        try:
            rc = await asyncio.wait_for(drain(), float(self.cfg.timing["tqsl_timeout_s"]))
        except TimeoutError:
            _kill_group(proc)
            await proc.wait()
            rc = None
        finally:
            self.tqsl_proc = None
        lines = [x.strip() for x in buf.decode("utf-8", "replace").splitlines() if x.strip()]
        return rc, scrub(" | ".join(lines[-4:]), self._secrets, cap=600)

    # -- reconcile (PLAN §5): an unknown send is checked before any retry

    async def _reconcile(self, dest):
        import aiohttp
        rows = self.ledger.in_state(dest, "unknown")
        try:
            if dest == "wavelog":
                recs = await self._wavelog_fetch(int(self.ledger.cursor("wavelog_last_id") or 0), advance=False)
                found = self._matches(recs)
                for r in rows:
                    self.ledger.set(r["id"], dest, "sent" if r["id"] in found else "pending",
                                    None, "found on reconcile" if r["id"] in found else None)
            else:
                for r in rows:
                    f, text = await self._qrz({"KEY": self._secret(self.cfg.qrz["key_file"]), "ACTION": "FETCH",
                                               "OPTION": f"CALL:{r['call']},MAX:{QRZ_PAGE}"})
                    if f.get("RESULT", "").upper() == "AUTH":
                        return self._hold("qrz", "auth", "QRZ refused the API key")
                    recs = adif.parse(text, strict=False)[1] if text else []
                    found = self._matches(recs)
                    self.ledger.set(r["id"], dest, "sent" if r["id"] in found else "pending",
                                    None, "found on reconcile" if r["id"] in found else None)
        except (aiohttp.ClientError, TimeoutError, OSError, _Http, adif.AdifError, _PullError) as e:
            self._net_error(dest, e)
            return RETRY
        log.info("logsync: %s: %d unknown send(s) checked", dest, len(rows))
        return OK

    def _matches(self, recs):
        """Ledger ids of the records that match a QSO exactly."""
        out = {}
        for f in recs:
            try:
                row = self.ledger.find(adif.qso(f))
            except adif.AdifError:
                continue
            if row is not None:
                out[row["id"]] = f
        return out

    # -- pulls

    async def _every(self, pull, every, after_import=False):
        if after_import:
            await self.imported.wait()
        if not self.cfg.timing["pull_at_start"]:
            await asyncio.sleep(float(self.cfg.timing[every]))
        while True:
            ok = False
            try:
                ok = await pull()
            except asyncio.CancelledError:
                raise
            except SecretError as e:
                log.warning("logsync: pull: %s", scrub(str(e), self._secrets))
            except Exception as e:
                log.error("logsync: pull %s: %s", getattr(pull, "__name__", "?"), type(e).__name__)
            name = getattr(pull, "__name__", "?")
            self.pulls_done[name] = self.pulls_done.get(name, 0) + 1
            self._changed()
            await asyncio.sleep(float(self.cfg.timing[every]) if ok else max(self.cfg.backoff[0], 1.0))

    async def _wavelog_fetch(self, from_id, advance=True):
        """Every Wavelog record past from_id (paged), as ADIF field dicts."""
        import aiohttp
        w = self.cfg.wavelog
        out, cur = [], from_id
        for _ in range(MAX_PAGES):
            body = {"key": self._secret(w["key_file"]), "station_id": w["station_profile_id"], "fetchfromid": cur}
            try:
                async with self.http.post(w["url"].rstrip("/") + "/index.php/api/get_contacts_adif", json=body) as r:
                    status, text = r.status, await r.text()
            except (aiohttp.ClientError, TimeoutError, OSError) as e:
                raise _PullError(type(e).__name__) from None
            if status in (401, 403):
                raise _PullError(f"HTTP {status}")
            if status != 200:
                raise _PullError(f"HTTP {status}")
            try:
                d = json.loads(text)
            except ValueError:
                raise _PullError("not JSON") from None
            n = int(d.get("exported_qsos") or 0)
            if n == 0 or not d.get("adif"):
                break
            recs = adif.parse(d["adif"], strict=True)[1]
            if len(recs) != n:
                raise _PullError(f"{len(recs)} records for exported_qsos {n} (truncated)")
            out += recs
            last = int(d.get("lastfetchedid") or cur)
            if last <= cur or n < 20000:
                cur = last
                break
            cur = last
        if advance:
            self._wavelog_next = cur
        return out

    async def pull_wavelog(self):
        """Hand-typed QSOs (and, the first time, the whole log as the backfill)."""
        first = not self.ledger.cursor("backfill_wavelog")
        try:
            recs = await self._wavelog_fetch(int(self.ledger.cursor("wavelog_last_id") or 0))
        except (_PullError, adif.AdifError) as e:
            self.last_error["wavelog"] = f"pull: {scrub(str(e), self._secrets, 80)}"
            log.warning("logsync: Wavelog pull failed: %s", self.last_error["wavelog"])
            return False
        found = self._matches(recs)
        added = 0
        for f in recs:
            try:
                q = adif.qso(f, self.cfg.call)
            except adif.AdifError:
                continue
            row = self.ledger.find(q)
            if row is not None:
                self.ledger.set(row["id"], "wavelog", "sent", only_from=("pending", "unknown", "held_backfill"))
                continue
            later = "held_backfill" if first else "pending"
            states = {d: later for d in self.cfg.dests}
            states["wavelog"] = "sent"
            self.ledger.add(f, "backfill" if first else "wavelog", states, own=self.cfg.call)
            added += 1
        self.ledger.set_cursor("wavelog_last_id", self._wavelog_next)
        if first:
            self.ledger.set_cursor("backfill_wavelog", int(self.clock()))
            self.imported.set()
        log.info("logsync: Wavelog pull: %d record(s), %d matched, %d new%s", len(recs), len(found), added,
                 " (backfill: held)" if first else "")
        if added and not first:
            for e in self.wake.values():
                e.set()
        return True

    async def pull_lotw(self):
        """Receipts (qso_qsl=no) and confirmations (qso_qsl=yes), each with its
        own cursor, advanced only after a complete, valid ADIF import."""
        import aiohttp
        lw = self.cfg.lotw
        for qsl, cur_name, since_param, last_field in (
                ("no", "lotw_qsorx", "qso_qsorxsince", "APP_LOTW_LASTQSORX"),
                ("yes", "lotw_qsl", "qso_qslsince", "APP_LOTW_LASTQSL")):
            started = self.clock()
            params = {"login": lw["login"], "password": self._secret(lw["password_file"]), "qso_query": "1",
                      "qso_qsl": qsl, "qso_withown": "yes",
                      since_param: self.ledger.cursor(cur_name) or "1900-01-01"}
            try:
                async with self.http.get(lw["url"], params=params) as r:
                    status, text = r.status, await r.text()
            except (aiohttp.ClientError, TimeoutError, OSError) as e:
                self._net_error("lotw", e)
                return False
            try:
                if status != 200:
                    raise adif.AdifError(f"HTTP {status}")
                head, recs = adif.parse(text, strict=True)
                n = head.get("APP_LOTW_NUMREC")
                if n is not None and n.strip().isdigit() and int(n) != len(recs):
                    raise adif.AdifError(f"{len(recs)} records but APP_LoTW_NUMREC {n} (truncated)")
            except adif.AdifError as e:
                self.last_error["lotw"] = f"pull: {scrub(str(e), self._secrets, 80)}"
                log.warning("logsync: LoTW pull (qso_qsl=%s) failed: %s; cursor unchanged", qsl, self.last_error["lotw"])
                return False
            found = self._matches(recs)
            if qsl == "no":
                for qid in found:
                    self.ledger.set(qid, "lotw", "accepted",
                                    only_from=("submitted", "unknown", "pending", "held_backfill", "held"))
                for r in self.ledger.in_state("lotw", "submitted", "unknown"):
                    if r["id"] in found or r["updated"] > started:
                        continue
                    if self.ledger.miss(r["id"], "lotw") < LOTW_MISSES:
                        continue
                    if r["state"] == "unknown" and r["reason"] == "restart":
                        # A run lost to a restart (TQSL may never have run): PLAN §5,
                        # absent -> pending. A re-send TQSL finds a duplicate is exit 8,
                        # unknown again, and held after three more misses.
                        self.ledger.requeue(r["id"], "lotw")
                        log.info("logsync: LoTW: %s %s (lost at a restart) is not on LoTW; sending it again",
                                 r["call"], r["ts"])
                        if "lotw" in self.wake:
                            self.wake["lotw"].set()
                        continue
                    why = r["detail"] or "TQSL reported no problem"
                    self.ledger.set(r["id"], "lotw", "held", "not on LoTW",
                                    f"not in LoTW after {LOTW_MISSES} pulls: {why}")
                    self._alert("lotw", "not on LoTW", f"{r['call']} {r['ts']} is not on LoTW after "
                                                       f"{LOTW_MISSES} checks")
            else:
                for qid, f in found.items():
                    if (f.get("QSL_RCVD") or "Y").upper() == "Y":
                        self.ledger.set(qid, "lotw", "confirmed",
                                        only_from=("submitted", "unknown", "pending", "held_backfill", "held",
                                                   "accepted"))
            last = head.get(last_field)
            if last:
                self.ledger.set_cursor(cur_name, last.strip())
            log.info("logsync: LoTW pull (qso_qsl=%s): %d record(s), %d matched", qsl, len(recs), len(found))
        return True

    async def pull_qrz(self):
        """The QRZ log, paged by AFTERLOGID (MAX:250): marks what QRZ has and
        what is confirmed there."""
        import aiohttp
        after = int(self.ledger.cursor("qrz_last_logid") or 0)
        total = matched = 0
        for _ in range(MAX_PAGES):
            try:
                f, text = await self._qrz({"KEY": self._secret(self.cfg.qrz["key_file"]), "ACTION": "FETCH",
                                           "OPTION": f"MAX:{QRZ_PAGE},AFTERLOGID:{after}"})
            except (aiohttp.ClientError, TimeoutError, OSError, _Http) as e:
                self._net_error("qrz", e)
                return False
            res = f.get("RESULT", "").upper()
            if res == "AUTH":
                self._hold("qrz", "auth", "QRZ refused the API key")
                return False
            if res == "FAIL" and re.search(r"\bno\b.*(found|entries|records)", f.get("REASON", ""), re.I):
                break
            if res != "OK":
                self.last_error["qrz"] = "pull: " + (res or "no RESULT")
                return False
            try:
                recs = adif.parse(text or "", strict=False)[1]
            except adif.AdifError as e:
                self.last_error["qrz"] = f"pull: {scrub(str(e), self._secrets, 80)}"
                return False
            count = f.get("COUNT", "")
            if count.isdigit() and int(count) != len(recs):
                self.last_error["qrz"] = "pull: truncated"
                return False
            found = self._matches(recs)
            for qid, rec in found.items():
                st = "confirmed" if (rec.get("APP_QRZLOG_STATUS") or "").upper() == "C" else "sent"
                self.ledger.set(qid, "qrz", st, only_from=("pending", "unknown", "held_backfill", "held", "sent"))
            total += len(recs)
            matched += len(found)
            ids = [int(r["APP_QRZLOG_LOGID"]) for r in recs if (r.get("APP_QRZLOG_LOGID") or "").isdigit()]
            if not recs or not ids:
                break
            after = max(ids)
            self.ledger.set_cursor("qrz_last_logid", after)
            if len(recs) < QRZ_PAGE:
                break
        log.info("logsync: QRZ pull: %d record(s), %d matched", total, matched)
        return True

    # -- the operator

    async def correct(self, qid, dest):
        """Send correction (the page): queue the latest revision for dest.
        Returns why not, or None."""
        if dest == "lotw":
            return ("LoTW records carry no report or grid, so there is nothing to correct there "
                    "(a changed call, band, mode or time is a new QSO)")
        if dest not in self.cfg.dests:
            return f"{dest} is not set up here"
        if not self.ledger.correction(int(qid), dest):
            return "nothing to correct for that QSO there"
        self.wake[dest].set()
        self._changed()
        return None

    # -- what the page sees (an allowlist; never a URL, a path or a credential)

    def status(self):
        if self.ledger is None:
            return {"loaded": True, "running": False}
        recent = []
        for q, ds in self.ledger.recent(15):
            recent.append({"id": q["id"], "call": q["call"], "band": q["band"],
                           "mode": q["submode"] or q["mode"], "time": _when(q["ts"]), "near": bool(q["near"]),
                           "d": {d: {"state": r["state"], "why": r["reason"] if r["state"] == "held" else None,
                                     "edited": bool(r["edited"])} for d, r in ds.items()}})
        return {"loaded": True, "running": self.started, "intake": self.cfg.shape, "dests": list(self.cfg.dests),
                "counts": self.ledger.counts(), "holds": dict(self.holds), "errors": dict(self.last_error),
                "recent": recent,
                "alerts": [{"dest": a["dest"], "cls": a["cls"], "at": int(a["created"])} for a in self.ledger.alerts(5)]}

    def _changed(self):
        if self.notify_cb is None:
            return
        if self._notify_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def fire():
            self._notify_handle = None
            try:
                self.notify_cb(self.status())
            except Exception as e:
                log.debug("logsync: notify: %s", type(e).__name__)
        self._notify_handle = loop.call_later(0.3, fire)


class _Crash(Exception):
    pass


class _Http(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class _PullError(Exception):
    pass


def _when(ts):
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}" if ts and len(ts) == 14 else ts


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------- `ranch-radio logsync`

def report(ledger_path):
    """The backfill report (PLAN §7): counts per destination, every QSO still
    held, the near misses, and the hash --apply must be given."""
    led = Ledger(ledger_path, dests=())
    try:
        pairs = led.backfill_set()
        h = Ledger.set_hash(pairs)
        lines = [f"Backfill report, {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}", ""]
        counts = led.counts()
        for d in sorted(counts):
            lines.append(f"  {d:<8} " + ", ".join(f"{s} {n}" for s, n in sorted(counts[d].items())))
        lines += ["", f"Held for backfill ({len(pairs)} QSO-destination pairs):"]
        for qid, d in pairs:
            q = led.qso(qid)
            lines.append(f"  #{qid:<6} {d:<8} {q['call']:<10} {q['band']:<6} "
                         f"{(q['submode'] or q['mode']):<6} {_when(q['ts'])}")
        near = led.db.execute("SELECT * FROM qso WHERE near=1 ORDER BY call, ts").fetchall()
        lines += ["", f"Near misses (same call, band and mode within 2 minutes; judge these) ({len(near)}):"]
        for q in near:
            lines.append(f"  #{q['id']:<6} {q['call']:<10} {q['band']:<6} {(q['submode'] or q['mode']):<6} {_when(q['ts'])}")
        lines += ["", f"Hash: {h}", f"Release exactly this set: ranch-radio logsync backfill --apply {h}"]
        return h, "\n".join(lines) + "\n"
    finally:
        led.close()


def apply(ledger_path, want):
    """Release the reported set; None when the hash is stale."""
    led = Ledger(ledger_path, dests=())
    try:
        return led.release(want)
    finally:
        led.close()


def status_text(ledger_path):
    led = Ledger(ledger_path, dests=())
    try:
        out = []
        for d, c in sorted(led.counts().items()):
            out.append(f"{d:<8} " + ", ".join(f"{s} {n}" for s, n in sorted(c.items())))
        held = led.db.execute("SELECT d.*, q.call, q.ts FROM dest d JOIN qso q ON q.id=d.qso_id "
                              "WHERE d.state IN ('held', 'unknown') ORDER BY q.ts").fetchall()
        if held:
            out.append("")
            for r in held:
                out.append(f"#{r['qso_id']:<6} {r['dest']:<8} {r['call']:<10} {_when(r['ts'])}  {r['state']}"
                           f"({r['reason'] or ''}) {r['detail'] or ''}")
        al = led.alerts(10)
        if al:
            out.append("")
            out += [f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(a['created']))}  {a['msg']}" for a in al]
        return "\n".join(out) + "\n" if out else "The ledger is empty.\n"
    finally:
        led.close()


def main(argv=None):
    """python -m logsync {status,backfill} --ledger PATH [--apply HASH]
    (what `ranch-radio logsync` runs)."""
    import argparse
    import sys
    p = argparse.ArgumentParser(prog="python -m logsync")
    p.add_argument("action", choices=["status", "backfill"])
    p.add_argument("--ledger", required=True)
    p.add_argument("--apply", metavar="HASH")
    a = p.parse_args(argv)
    path = os.path.expanduser(a.ledger)
    if a.action == "status":
        sys.stdout.write(status_text(path))
        return 0
    if a.apply:
        n = apply(path, a.apply)
        if n is None:
            print("ranch-radio: that hash is stale (the held set changed since the report): run "
                  "`ranch-radio logsync backfill` again and review the new report.", file=sys.stderr)
            return 1
        print(f"Released {n} QSO-destination pair(s) to their senders.")
        return 0
    h, text = report(path)
    out = os.path.join(os.path.dirname(path), f"logsync-backfill-{h}.txt")
    with open(out, "w") as f:
        f.write(text)
    sys.stdout.write(text)
    print(f"\n(written to {out})")
    return 0


__all__ = ["Config", "LogSync", "SecretError", "apply", "check_block", "read_secret", "report", "scrub",
           "status_text"]


if __name__ == "__main__":
    raise SystemExit(main())
