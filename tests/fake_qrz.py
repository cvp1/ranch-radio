"""A fake QRZ Logbook API for the logbook sync tests, after PLAN v3 §2 (QRZ's
own page may refuse automated fetches; the shape here is the plan's, and the
real one is unverified until the operator's live run):

  POST /api, form-encoded KEY, ACTION, ADIF / OPTION; the answer is
  form-encoded too:
    ACTION=INSERT  RESULT=OK&LOGID=n&COUNT=1
                   RESULT=FAIL&REASON=Unable to add QSO to database: duplicate
                   RESULT=FAIL&REASON=<anything else>   (fail_next)
                   RESULT=AUTH                           (a wrong KEY)
    ACTION=FETCH   OPTION=MAX:n,AFTERLOGID:n | CALL:x
                   RESULT=OK&COUNT=n&ADIF=<html-escaped ADIF with APP_QRZLOG_LOGID>
                   RESULT=FAIL&REASON=No log entries found&COUNT=0

It refuses (and counts) any request whose User-Agent isn't an identifiable
RanchRadio/<version> (<call>): QRZ rate-limits generic agents. Never sends
OPTION=REPLACE semantics: an INSERT that carries it is recorded as a violation.
"""
import html
import re

from aiohttp import web

from fake_wavelog import fields, records


class FakeQrz:
    def __init__(self, key, call="N0CALL"):
        self.key, self.call = key, call
        self.log = []                     # [{"id", "f"}]
        self.next_id = 1000
        self.inserts = []                 # ADIF strings, in arrival order
        self.bad_agents = 0
        self.violations = []
        self.down = False
        self.fail_next = 0

    def add(self, f, status="N"):
        self.next_id += 1
        f = {**f, "APP_QRZLOG_LOGID": str(self.next_id), "APP_QRZLOG_STATUS": status}
        self.log.append({"id": self.next_id, "f": f})
        return self.next_id

    def app(self):
        a = web.Application()
        a.router.add_post("/api", self.api)
        return a

    async def api(self, request):
        if self.down:
            return web.Response(status=503, text="down")
        ua = request.headers.get("User-Agent", "")
        if not re.fullmatch(r"RanchRadio/\S+ \(" + re.escape(self.call) + r"\)", ua):
            self.bad_agents += 1
            return web.Response(text="RESULT=FAIL&REASON=identify your application in the User-Agent")
        d = await request.post()
        if d.get("KEY") != self.key:
            return web.Response(text="RESULT=AUTH&REASON=invalid api key")
        act = d.get("ACTION")
        opt = d.get("OPTION", "")
        if act == "INSERT":
            if "REPLACE" in opt.upper():
                self.violations.append("REPLACE")
            self.inserts.append(d.get("ADIF", ""))
            if self.fail_next:
                self.fail_next -= 1
                return web.Response(text="RESULT=FAIL&REASON=wrong band for that frequency&EXTENDED=")
            f = (records("<EOH>" + d.get("ADIF", "")) or [{}])[0]
            for q in self.log:
                g = q["f"]
                if (g.get("CALL"), g.get("BAND", "").lower(), g.get("MODE"), g.get("QSO_DATE"), g.get("TIME_ON", "")[:4]) == \
                        (f.get("CALL"), f.get("BAND", "").lower(), f.get("MODE"), f.get("QSO_DATE"), f.get("TIME_ON", "")[:4]):
                    return web.Response(text="RESULT=FAIL&REASON=Unable to add QSO to database: duplicate&EXTENDED=")
            n = self.add(f)
            return web.Response(text=f"RESULT=OK&LOGID={n}&COUNT=1")
        if act == "FETCH":
            o = dict(p.split(":", 1) for p in opt.split(",") if ":" in p)
            rows = [q for q in self.log if q["id"] > int(o.get("AFTERLOGID", 0))]
            if "CALL" in o:
                rows = [q for q in rows if q["f"].get("CALL") == o["CALL"].upper()]
            rows = rows[:int(o.get("MAX", 250))]
            if not rows:
                return web.Response(text="RESULT=FAIL&REASON=No log entries found&COUNT=0")
            text = "".join(" ".join(f"<{k}:{len(v)}>{v}" for k, v in q["f"].items()) + " <EOR>\n" for q in rows)
            return web.Response(text=f"RESULT=OK&COUNT={len(rows)}&ADIF={html.escape(text)}")
        return web.Response(text="RESULT=FAIL&REASON=unknown action")


__all__ = ["FakeQrz", "fields"]
