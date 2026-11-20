"""A populated Roster and Map for the README's Operate screenshot: made-up
stations in the tests' own style (no real calls, no real locators), around
FN31 as the other page tests are. tests/operate_page_test.py loads it only when
it is taking screenshots, after every check has run.

    import readme_fixture as F; F.ROSTER, F.MAP
"""
import math as _m, time as _t
from addons.propagation import grid_latlon as _gl, psk_receivers as _pr
_ME = "FN31"
_mlat, _mlon = _gl(_ME)
def _kb(g):
    la, lo = _gl(g); p1, p2 = _m.radians(_mlat), _m.radians(la); dl = _m.radians(lo - _mlon)
    d = 2 * 6371 * _m.asin(_m.sqrt(_m.sin((p2 - p1) / 2) ** 2 + _m.cos(p1) * _m.cos(p2) * _m.sin(dl / 2) ** 2))
    br = (_m.degrees(_m.atan2(_m.sin(dl) * _m.cos(p2), _m.cos(p1) * _m.sin(p2) - _m.sin(p1) * _m.cos(p2) * _m.cos(dl))) + 360) % 360
    return round(d), round(br)
_HEARD = [("JA1ABC", "PM95", "Japan", "AS", -14, "dxcc"), ("VK2ABC", "QF56", "Australia", "OC", -19, "dxcc"),
          ("ZL1XYZ", "RF72", "New Zealand", "OC", -21, "dxcc_band"), ("KH6XYZ", "BL11", "Hawaii", "OC", -8, "grid"),
          ("W1ABC", "FN42", "United States", "NA", -3, None), ("K4XYZ", "EM73", "United States", "NA", 2, None),
          ("VE3ABC", "FN03", "Canada", "NA", -6, "grid_band"), ("XE1ABC", "EK09", "Mexico", "NA", -1, None),
          ("LU1ABC", "GF05", "Argentina", "SA", -17, "dxcc_band"), ("PY2XYZ", "GG66", "Brazil", "SA", -12, "grid"),
          ("EA4XYZ", "IN80", "Spain", "EU", -16, "grid_band"), ("G4ABC", "IO91", "England", "EU", -11, None),
          ("DL1XYZ", "JO62", "Germany", "EU", -18, "grid"), ("KL7ABC", "BP51", "Alaska", "NA", -15, "grid")]
_ROW = {"answer": "cq", "cq": True, "cq_word": None, "to_me": False, "to": None, "dt": 0.1, "lotw": True, "band": "20m", "mode": "FT8"}
_rows = []
for i, (c, g, ent, cont, snr, need) in enumerate(_HEARD):
    km, br = _kb(g)
    _rows.append({**_ROW, "key": f"JTDX|1|{600 + i * 110}|CQ {c} {g}", "call": c, "grid": g, "message": f"CQ {c} {g}",
                  "entity": ent, "cont": cont, "km": km, "bearing": br, "snr": snr, "df": 600 + i * 110, "age": i * 9,
                  "need": need, "reasons": [need] if need else []})
_rows.sort(key=lambda r: ({"dxcc": 0, "dxcc_band": 1, "grid": 3, "grid_band": 4}.get(r["need"], 9), -r["snr"]))
ROSTER = {"band": "20m", "mode": "FT8", "dial": 14074000, "rows": _rows,
          "needs": {"qsos": 7856, "dxcc_worked": 156, "dxcc_confirmed": 98, "grids_worked": 812, "grids_confirmed": 455, "lotw_users": 0}}
_NOW = int(_t.time())
_RX = [("JA1ABC", "PM95vq", -17), ("VK2ABC", "QF56od", -22), ("KH6XYZ", "BL11bh", -10), ("G4ABC", "IO91wm", -15),
       ("W1ABC", "FN42lj", 4), ("VE7XYZ", "CN89ld", -2), ("K0ABC", "EN34ft", 1), ("CE3XYZ", "FF46pn", -18),
       ("HB9XYZ", "JN47nh", -19), ("OH2ABC", "KP20lf", -23), ("JR6XYZ", "PL36ef", -20), ("ZS6ABC", "KG33vt", -24),
       ("EA8XYZ", "IL18rk", -16), ("VE1ABC", "FN74ip", -9)]
_xml = '<?xml version="1.0"?><receptionReports>' + "".join(
    f'<receptionReport receiverCallsign="{c}" receiverLocator="{l}" senderCallsign="N0CALL" senderLocator="FN31pr" '
    f'frequency="{14075000 + i * 37}" flowStartSeconds="{_NOW - 60 - i * 70}" mode="FT8" receiverDXCC="" sNR="{s}" />'
    for i, (c, l, s) in enumerate(_RX)) + "</receptionReports>"
_recv = _pr(_xml)
_grids = {}
for i, (a, b) in enumerate([(f, n) for f in "CDEF" for n in "MNL"]):
    for x in range(10):
        for y in range(10):
            if (x * 7 + y * 3 + i) % 4 == 0:
                _grids[f"{a}{b}{x}{y}"] = "c" if (x + y) % 3 else "w"
for g in ("PM95", "QF56", "IO91", "JO62", "IN80", "GG66", "RF72", "BL11", "KP20", "JN47"):
    _grids[g] = "c"
_heard = [{"call": r["call"], "grid": r["grid"], "band": "20m", "mode": "FT8", "snr": r["snr"], "best": r["snr"],
           "t": _NOW - r["age"], "n": 1, "need": r["need"], "entity": r["entity"], "km": r["km"], "bearing": r["bearing"]} for r in _rows]
MAP = {"now": _NOW, "map": {"me": _ME, "band": "20m", "showing": "20m", "grids": _grids,
       "decodes": [{k: r[k] for k in ("key", "answer", "call", "grid", "need", "reasons", "snr", "cq", "to_me", "entity", "km",
                                       "bearing", "age", "band", "mode", "message", "lotw")} for r in _rows]},
       "heard": _heard, "hears_me": {"call": "N0CALL", "window_min": 60, "every_s": 300, "receivers": _recv,
       "locator": "FN31pr", "error": None, "checked": _NOW - 30}, "hears_me_why": None, "pota": []}
