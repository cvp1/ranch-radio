"""A fake Wavelog API for the logbook sync tests, after Wavelog's
application/controllers/Api.php and models/Logbook_model.php (read
2026-11-13, github.com/wavelog/wavelog):

  POST /index.php/api/qso  {key, station_profile_id, type: "adif", string}
      201 {"status": "created", "adif_count": 1, "adif_errors": 0, "messages": [""]}
      400 {"status": "abort", ..., "messages": ["", "Date/Time: ... Duplicate for N0CALL<br>"]}
          when a QSO with the same call, station call, band and mode is within
          15 minutes (import_check: +-15 min around the minute)
      401 {"status": "failed", "reason": "missing or wrong api key"}
  POST /index.php/api/get_contacts_adif  {key, station_id, fetchfromid}
      200 {"status": "successful", "lastfetchedid", "exported_qsos", "adif"}

Switches a test can flip: down (503), rate (429 for the next N), and
records every call it got (without the key).
"""
import json
import re

from aiohttp import web

TAG = re.compile(r"<([A-Za-z0-9_]+):(\d+)(?::[A-Za-z])?>")


def fields(rec):
    out, pos = {}, 0
    while True:
        m = TAG.search(rec, pos)
        if not m:
            return out
        n = int(m.group(2))
        out[m.group(1).upper()] = rec[m.end():m.end() + n]
        pos = m.end() + n


def records(text):
    body = re.split(r"(?i)<eoh>", text, maxsplit=1)[-1]
    return [fields(r) for r in re.split(r"(?i)<eor>", body) if TAG.search(r)]


def minutes(f):
    d, t = f.get("QSO_DATE", "00000000"), (f.get("TIME_ON", "0000") + "00")[:4]
    import calendar
    import time
    return calendar.timegm(time.strptime(d + t, "%Y%m%d%H%M")) // 60


class FakeWavelog:
    def __init__(self, key, station_id=1):
        self.key, self.station_id = key, station_id
        self.qsos = []                    # [{"id", "f"}]
        self.next_id = 100
        self.calls = []                   # [(path, adif string or fetchfromid)]
        self.down = False
        self.rate = 0

    def add(self, f):
        self.next_id += 1
        self.qsos.append({"id": self.next_id, "f": dict(f)})
        return self.next_id

    def app(self):
        a = web.Application()
        a.router.add_post("/index.php/api/qso", self.qso)
        a.router.add_post("/index.php/api/get_contacts_adif", self.contacts)
        return a

    async def qso(self, request):
        if self.down:
            return web.Response(status=503, text="down")
        if self.rate:
            self.rate -= 1
            return web.json_response({"status": "failed", "reason": "rate limited"}, status=429)
        d = await request.json()
        self.calls.append(("qso", d.get("string")))
        if d.get("key") != self.key:
            return web.json_response({"status": "failed", "reason": "missing or wrong api key"}, status=401)
        if d.get("station_profile_id") != self.station_id:
            return web.json_response({"status": "failed", "reason": "station id does not belong to the API key owner."},
                                     status=401)
        errors, msgs, n = 0, [], 0
        for f in records(d.get("string") or ""):
            if not f.get("CALL"):
                continue
            n += 1
            dup = [q for q in self.qsos if q["f"].get("CALL") == f.get("CALL")
                   and q["f"].get("STATION_CALLSIGN") == f.get("STATION_CALLSIGN")
                   and q["f"].get("BAND", "").lower() == f.get("BAND", "").lower()
                   and q["f"].get("MODE") == f.get("MODE") and abs(minutes(q["f"]) - minutes(f)) <= 15]
            if dup:
                errors += 1
                msgs.append(f"Date/Time: {f.get('QSO_DATE')} {f.get('TIME_ON')} Callsign: {f.get('CALL')} "
                            f"Band: {f.get('BAND')} Duplicate for {f.get('STATION_CALLSIGN')}<br>")
            else:
                self.add(f)
        out = {"type": "adif", "string": "", "adif_count": n, "adif_errors": errors, "messages": [""] + msgs}
        if errors:
            return web.json_response({"status": "abort", **out}, status=400)
        return web.json_response({"status": "created", **out}, status=201)

    async def contacts(self, request):
        if self.down:
            return web.Response(status=503, text="down")
        d = await request.json()
        self.calls.append(("get_contacts_adif", d.get("fetchfromid")))
        if d.get("key") != self.key:
            return web.json_response({"status": "failed", "reason": "missing api key"}, status=401)
        start = int(d.get("fetchfromid") or 0)
        rows = [q for q in self.qsos if q["id"] > start]
        if not rows:
            return web.json_response({"status": "successful", "message": "No new QSOs available.",
                                      "lastfetchedid": start, "exported_qsos": 0, "adif": None})
        text = "Wavelog ADIF export\n<ADIF_VER:5>3.1.6\n<PROGRAMID:7>Wavelog\n<EOH>\n"
        for q in rows:
            text += " ".join(f"<{k}:{len(v)}>{v}" for k, v in q["f"].items()) + " <EOR>\n"
        return web.json_response({"status": "successful", "message": "Export successful",
                                  "lastfetchedid": max(q["id"] for q in rows), "exported_qsos": len(rows),
                                  "adif": text})


if __name__ == "__main__":
    import sys
    w = FakeWavelog(sys.argv[1] if len(sys.argv) > 1 else "test-key")
    web.run_app(w.app(), host="127.0.0.1", port=int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    print(json.dumps(w.qsos))
