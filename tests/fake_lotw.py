"""A fake LoTW report service for the logbook sync tests, after
lotw-help/developer-query-qsos-qsls (read 2026-11-13):

  GET /lotwuser/lotwreport.adi?login=&password=&qso_query=1&qso_qsl=no|yes
      &qso_qsorxsince= | &qso_qslsince= &qso_withown=yes
  qso_qsl=no   every QSO LoTW has accepted, received on or after qso_qsorxsince;
               header APP_LoTW_LASTQSORX (the newest receipt in this file)
  qso_qsl=yes  the confirmed ones only (QSL_RCVD=Y), since qso_qslsince;
               header APP_LoTW_LASTQSL
  a failed query is an HTML page with no <eoh>

The two sets differ: a QSO is accepted when it lands in the store, and
confirmed only when a test confirms it. The store is a JSON-lines file that
tests/fake_tqsl.py appends to when it "uploads". MODE comes back as LoTW
names it (FT4, not MFSK/FT4), so matching has to canonicalise.

Switches: html (the login page, whatever is asked), truncated (the ADIF cut
off mid-record), and every query string is kept (to prove the cursor).
"""
import json
import os
import time

from aiohttp import web

LOGIN_PAGE = ("<html><head><title>Logbook of the World</title></head><body><form method=post>"
              "Username or password incorrect<input name=login><input type=password name=password>"
              "</form></body></html>")


class FakeLotw:
    def __init__(self, login, password, store):
        self.login, self.password, self.store = login, password, store
        self.html = False
        self.truncated = False
        self.queries = []
        self.confirmed = set()            # (CALL, QSO_DATE, TIME_ON)

    def rows(self):
        if not os.path.exists(self.store):
            return []
        with open(self.store) as f:
            return [json.loads(x) for x in f if x.strip()]

    def accept(self, f, rx=None):
        """As if TQSL had uploaded f and LoTW processed it."""
        with open(self.store, "a") as out:
            out.write(json.dumps({**f, "APP_LOTW_RXQSO": rx or time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}) + "\n")

    def confirm(self, call, date, t):
        self.confirmed.add((call, date, t))

    def app(self):
        a = web.Application()
        a.router.add_get("/lotwuser/lotwreport.adi", self.report)
        return a

    async def report(self, request):
        q = request.query
        self.queries.append({k: v for k, v in q.items() if k != "password"})
        if self.html or q.get("login") != self.login or q.get("password") != self.password:
            return web.Response(text=LOGIN_PAGE, content_type="text/html")
        qsl = q.get("qso_qsl", "yes")
        rows = []
        for r in self.rows():
            k = (r.get("CALL"), r.get("QSO_DATE"), r.get("TIME_ON"))
            if qsl == "yes":
                if k not in self.confirmed:
                    continue
                since = q.get("qso_qslsince") or "1900-01-01"
                r = {**r, "QSL_RCVD": "Y", "APP_LOTW_RXQSL": r["APP_LOTW_RXQSO"]}
                if r["APP_LOTW_RXQSL"] < since:
                    continue
            else:
                since = q.get("qso_qsorxsince") or "1900-01-01"
                if r["APP_LOTW_RXQSO"] < since:
                    continue
                r = {**r, "QSL_RCVD": "N"}
            rows.append(r)
        rows.sort(key=lambda r: r["APP_LOTW_RXQSO"], reverse=True)
        head = "ARRL Logbook of the World Status Report\n<PROGRAMID:4>LoTW\n"
        if rows:
            last = rows[0]["APP_LOTW_RXQSO"]
            name = "APP_LoTW_LASTQSL" if qsl == "yes" else "APP_LoTW_LASTQSORX"
            head += f"<{name}:{len(last)}>{last}\n"
        head += f"<APP_LoTW_NUMREC:{len(str(len(rows)))}>{len(rows)}\n<eoh>\n"
        body = ""
        for r in rows:
            out = {"APP_LoTW_OWNCALL": r.get("STATION_CALLSIGN", ""), "STATION_CALLSIGN": r.get("STATION_CALLSIGN", ""),
                   "CALL": r["CALL"], "BAND": r.get("BAND", "").upper(),
                   "MODE": r.get("SUBMODE") or r.get("MODE", ""), "APP_LoTW_MODEGROUP": "DATA",
                   "QSO_DATE": r["QSO_DATE"], "TIME_ON": r["TIME_ON"], "APP_LoTW_RXQSO": r["APP_LOTW_RXQSO"],
                   "QSL_RCVD": r["QSL_RCVD"]}
            body += "\n".join(f"<{k}:{len(v)}>{v}" for k, v in out.items() if v) + "\n<eor>\n\n"
        text = head + body
        if self.truncated and body:
            text = text[:len(head) + len(body) // 2]
        return web.Response(text=text, content_type="application/x-arrl-adif")
