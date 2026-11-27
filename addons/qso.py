"""QSO add-on: log a contact by hand, phone or CW, from the station page.

The tab fills in what code can know: the frequency, band and mode from the
radio (the station's watch of each radio, station.rigs_view), the time in UTC,
the station's DXCC and what it would be new for (the logbook sync's index),
the earlier QSOs with that call (the ledger), name, QTH and grid from QRZ.com
(XML lookup, a QRZ subscription), and the POTA park when the call is spotted
(api.pota.app). The operator types the call and the reports and presses Log.

A logged QSO goes into the logbook sync's ledger (logsync.LogSync.record,
source "manual") exactly as a JTDX QSO does: the safety copy first, then
one sender per destination, each waiting until it can reach its logbook. A
station away from its LAN, with no route to Wavelog, keeps every QSO pending
and sends them when it is back; nothing is lost and nothing needs a second
step. With no logbook sync on the station the tab says so and logs nothing.

Options (the host file's addons block):

    "qso": {"enabled": true,
            "radio": "g90",                       # the radio the tab starts on (default: the first one)
            "qrz_user": "N0CALL",                 # default: the logbook sync's call
            "qrz_password_file": "~/.config/ranch-radio-creds/qrz-xml-password",
            "pota": true}                         # false: no POTA spots fetched

The QRZ login is set from the tab itself: when there is no password file, or
QRZ refuses it, the tab shows a login form. The password is written to its
file (mode 600, atomically) and never sent back to a page, logged, or kept
anywhere else. QRZ answers are kept in memory for an hour, never on disk.

POTA spots are fetched every minute while the add-on is on and copied to the
state directory, so a station that loses the internet still matches against
the last list, shown with its age. SOTA references are typed in: the SOTA
API's terms bar AI-written software without prior approval (qso_lookup.py).

Failure stays local: no QRZ (offline, no login, no subscription) leaves the
card to the ledger and the DXCC; no spots leaves POTA to be typed in. The tab
says which, in a line under the call.
"""
import asyncio
import json
import logging
import os
import secrets as _secrets
import time

from . import Addon

log = logging.getLogger("addons.qso")

POTA_URL = "https://api.pota.app/spot/activator"
POTA_EVERY_S = 60
POTA_MAX_BYTES = 2 << 20
RIG_PUSH_S = 0.25
DEFAULT_PASSWORD_FILE = "~/.config/ranch-radio-creds/qrz-xml-password"
MAX_PASSWORD = 128


class Qso(Addon):
    name = "qso"
    label = "QSO"
    title = "Log a contact: frequency and mode from the radio, the call looked up, POTA filled from the spots"
    group = "radios"
    css = True

    def __init__(self, host, opts):
        super().__init__(host, opts)
        for k in ("radio", "qrz_user", "qrz_password_file"):
            v = opts.get(k)
            if v is not None and (not isinstance(v, str) or not v.strip()):
                raise ValueError(f"{k} must be a non-empty string")
        if not isinstance(opts.get("pota", True), bool):
            raise ValueError("pota must be true or false")
        self.radio = opts.get("radio")
        self.want_pota = opts.get("pota", True)
        self.password_file = opts.get("qrz_password_file") or DEFAULT_PASSWORD_FILE
        self.user_opt = (opts.get("qrz_user") or "").strip().upper() or None
        self.clock = time.time
        self.spots, self.spots_at, self.spots_error = [], 0.0, None
        self.my = {}                       # sticky: my_pota, my_sota, my_grid, and a qrz_user set from the tab
        self.qrz = None
        self.http = None
        self._tasks = []
        self._rig_handle = None

    # -- lifecycle

    def _state_file(self, name):
        return os.path.join(self.state_dir, name) if self.state_dir else None

    def _own_call(self):
        ls = getattr(self.host, "logsync", None)
        return getattr(getattr(ls, "cfg", None), "call", None) or ""

    def _user(self):
        return self.my.get("qrz_user") or self.user_opt or self._own_call()

    async def start(self):
        import qso_lookup as Q
        self._load()
        if os.environ.get("RANCH_RADIO_OFFLINE") != "1":          # tests: never the internet
            import aiohttp
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8),
                                              headers={"User-Agent": Q.QRZ_AGENT})
        self.qrz = Q.Qrz(self._user(), self.password_file, self._post)
        if self.http is not None and self.want_pota:
            self._tasks.append(asyncio.ensure_future(self._pota_loop()))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._rig_handle is not None:
            self._rig_handle.cancel()
            self._rig_handle = None
        if self.http is not None:
            try:
                await self.http.close()
            except Exception:                     # noqa: BLE001 — stop must not raise
                pass
            self.http = None

    async def _post(self, url, form):
        """A form POST (qso_lookup: QRZ reads credentials only from a POST body)."""
        if self.http is None:
            raise ConnectionError("offline")
        async with self.http.post(url, data=form) as r:
            return r.status, (await r.content.read(1 << 17)).decode("utf-8", "replace")

    def _load(self):
        import qso_lookup as Q
        try:
            with open(self._state_file("qso.json")) as f:
                d = json.load(f)
            self.my = {k: str(v)[:40] for k, v in d.get("my", {}).items()
                       if k in ("my_pota", "my_sota", "my_grid", "qrz_user")}
        except (OSError, TypeError, ValueError, AttributeError):
            self.my = {}
        try:
            with open(self._state_file("qso-pota.json")) as f:
                d = json.load(f)
            self.spots, self.spots_at = Q.pota_spots(d.get("raw")), float(d.get("at", 0))
        except (OSError, TypeError, ValueError, AttributeError):
            pass

    def _save(self, name, obj):
        p = self._state_file(name)
        if not p:
            return
        tmp = f"{p}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(obj, f)
            os.replace(tmp, p)
        except OSError as e:
            log.warning("qso: %s not saved: %s", name, type(e).__name__)

    async def _pota_loop(self):
        import qso_lookup as Q
        while True:
            try:
                async with self.http.get(POTA_URL) as r:
                    if r.status != 200:
                        raise ValueError(f"HTTP {r.status}")
                    raw = await r.content.read(POTA_MAX_BYTES + 1)
                if len(raw) > POTA_MAX_BYTES:
                    raise ValueError("too large")
                items = json.loads(raw)
                self.spots, self.spots_at, self.spots_error = Q.pota_spots(items), self.clock(), None
                self._save("qso-pota.json", {"at": self.spots_at, "raw": items})
            except asyncio.CancelledError:
                raise
            except Exception as e:                 # keep the last list; say why
                self.spots_error = f"POTA spots: {type(e).__name__}"
            await asyncio.sleep(POTA_EVERY_S)

    # -- what the page sees

    def _rigs(self):
        rv = getattr(self.host, "rigs_view", None)
        try:
            rigs = rv() if callable(rv) else []
        except Exception:                          # noqa: BLE001 — a broken watch must not break the tab
            rigs = []
        return [{"id": r.get("id"), "name": r.get("name"), "present": bool(r.get("present")),
                 "connected": bool(r.get("connected")), "freq": r.get("freq"), "mode": r.get("mode"),
                 "data": bool(r.get("data"))} for r in rigs]

    def _my_view(self):
        return {k: v for k, v in self.my.items() if k != "qrz_user"}

    def _qrz_view(self):
        q = self.qrz
        return {"user": self._user(), "set": bool(q and q._password()), "state": q.state if q else "no_login",
                "says": q.says if q else None}

    def snapshot(self):
        ls = getattr(self.host, "logsync", None)
        return {"rigs": self._rigs(), "radio": self.radio, "my": self._my_view(),
                "qrz": self._qrz_view(), "own": self._own_call(),
                "logsync": bool(ls is not None and getattr(ls, "ledger", None) is not None),
                "spots": {"n": len(self.spots), "age_s": int(self.clock() - self.spots_at) if self.spots_at else None,
                          "error": self.spots_error, "on": self.want_pota}}

    def push_rig(self):
        """The station's rig_changed: the radios' dials, at most every RIG_PUSH_S."""
        if self._rig_handle is not None:
            return

        def fire():
            self._rig_handle = None
            self.emit({"event": "rigs", "rigs": self._rigs()})
        try:
            self._rig_handle = asyncio.get_running_loop().call_later(RIG_PUSH_S, fire)
        except RuntimeError:
            pass

    def degraded(self):
        out = []
        if self.want_pota and self.spots_error:
            out.append(f"qso: {self.spots_error}")
        if self.qrz and self.qrz.state in ("bad_login", "no_subscription"):
            out.append(f"qso: QRZ {self.qrz.state.replace('_', ' ')}")
        return out

    # -- commands

    async def command(self, ws, verb, m):
        if verb == "lookup":
            self.emit(await self.lookup(m), ws)
        elif verb == "log":
            self.emit(self.log_qso(m), ws)
        elif verb == "login":
            self.emit(await self.login(m), ws)
        elif verb == "my":
            self.set_my(m)
            self.emit({"event": "my", "my": self._my_view()}, ws)
        else:
            await super().command(ws, verb, m)

    async def lookup(self, m):
        import logsync_roster as R
        import qso_lookup as Q
        rid = m.get("id")
        if isinstance(rid, bool) or not isinstance(rid, (int, str)) or len(str(rid)) > 40:
            raise ValueError("a lookup needs a request id")
        out = {"event": "lookup", "id": rid}
        call = R.lookup_call(m.get("call"))
        if call is None:
            out["error"] = "not a callsign"
            return out
        out["call"] = call
        try:
            khz = float(m.get("khz")) if m.get("khz") not in (None, "") else None
        except (TypeError, ValueError):
            khz = None
        ls = getattr(self.host, "logsync", None)
        needs = getattr(ls, "needs", None)
        cty = getattr(needs, "cty", None)
        ent = cty.lookup(call) if cty is not None else None
        out["dxcc"] = ({"dxcc": ent.dxcc, "name": ent.name, "cont": ent.cont, "cq": ent.cq, "itu": ent.itu}
                       if ent else None)
        ledger = getattr(ls, "ledger", None)
        out["qsos"] = []
        if ledger is not None:
            try:
                out["qsos"] = ledger.history(call)
            except Exception as e:                 # a locked ledger: the card runs on without it
                log.error("qso: ledger history: %s", type(e).__name__)
        now = self.clock()
        matches = Q.match(self.spots, call, khz, now)
        out["pota"] = [{k: s[k] for k in ("ref", "name", "loc", "khz", "mode", "age_s", "on_freq")} for s in matches[:6]]
        out["pota_ref"] = Q.suggest(matches)
        out["spots_age_s"] = int(now - self.spots_at) if self.spots_at else None
        if self.qrz is not None:
            state, data = await self.qrz.lookup(call)
        else:
            state, data = "no_login", None
        out["qrz_state"], out["qrz"] = state, data
        grid = (data or {}).get("grid") or next((s["grid"] for s in matches if s.get("grid")), "")
        out["need"] = None
        band, mode = m.get("band"), m.get("mode")
        if (needs is not None and grid and isinstance(band, str) and 0 < len(band) <= 8
                and isinstance(mode, str) and 0 < len(mode) <= 12):
            level, reasons = needs.need(call, grid[:4], band, mode)
            out["need"] = {"level": level, "reasons": list(reasons)}
        out["qrz_login"] = self._qrz_view()
        return out

    def log_qso(self, m):
        import qso_lookup as Q
        rid = m.get("id")
        out = {"event": "logged", "id": rid if isinstance(rid, (int, str)) and not isinstance(rid, bool) else None}
        ls = getattr(self.host, "logsync", None)
        if ls is None or getattr(ls, "ledger", None) is None:
            out["error"] = "the logbook sync isn't running on this station: nothing was logged"
            return out
        form = m.get("qso")
        try:
            fields = Q.adif_fields(form, self._own_call(), self.clock())
        except ValueError as e:
            out["error"] = str(e)
            return out
        try:
            qid, what = ls.record(fields, "manual")
        except Exception as e:                     # a broken ledger: say so; the safety copy may have it
            log.error("qso: record: %s", type(e).__name__)
            out["error"] = f"not logged: {type(e).__name__}"
            return out
        out.update({"qid": qid, "what": what, "call": fields["CALL"], "band": fields["BAND"],
                    "mode": fields["SUBMODE"] or fields["MODE"], "time": fields["TIME_ON"][:4]})
        if isinstance(form, dict):
            self.set_my({k: form.get(k) for k in ("my_pota", "my_sota", "my_grid")})
        return out

    def set_my(self, m):
        import qso_lookup as Q
        for k, pat in (("my_pota", Q.POTA_REF), ("my_sota", Q.SOTA_REF), ("my_grid", Q.GRID)):
            if k not in m:
                continue
            v = Q.clean(m.get(k)).upper().replace(" ", "")
            if not v:
                self.my.pop(k, None)
            elif all(pat.match(x) for x in v.split(",")):
                self.my[k] = v[:40]
        self._save("qso.json", {"my": self.my})

    async def login(self, m):
        """{"user", "password"}: write the password file (600, atomically),
        then try it. The answer carries the state, never the password."""
        import logsync_roster as R
        out = {"event": "login"}
        user = R.lookup_call(m.get("user")) if m.get("user") else self._user()
        pw = m.get("password")
        if not user:
            out["error"] = "QRZ username: your callsign"
        elif not isinstance(pw, str) or not pw.strip():
            out["error"] = "the password is empty: nothing written"
        elif len(pw) > MAX_PASSWORD or any(ord(c) < 32 or ord(c) == 127 for c in pw):
            out["error"] = f"the password: one line, at most {MAX_PASSWORD} characters"
        else:
            try:
                write_secret(self.password_file, pw.strip())
            except OSError as e:
                out["error"] = f"not saved: {type(e).__name__}"
            else:
                if user != (self.user_opt or self._own_call()):
                    self.my["qrz_user"] = user
                else:
                    self.my.pop("qrz_user", None)
                self._save("qso.json", {"my": self.my})
                self.qrz.user, self.qrz.key, self.qrz.cache, self.qrz.down_until = user, None, {}, 0.0
                await self.qrz.login(force=True)
        out["qrz_login"] = self._qrz_view()
        return out


def write_secret(path, value):
    """Atomically, mode 600, its directory 700 if this creates it."""
    p = os.path.expanduser(str(path))
    d = os.path.dirname(p) or "."
    if not os.path.isdir(d):
        os.makedirs(d, mode=0o700)
    tmp = os.path.join(d, f".{os.path.basename(p)}.{os.getpid()}.{_secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


ADDON = Qso
