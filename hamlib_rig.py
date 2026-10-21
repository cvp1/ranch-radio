"""Any Hamlib-supported radio, through rigctld's TCP protocol.

Either starts `rigctld` for you (--hamlib-model + --port) or connects to one
that is already running (--rigctld host:port). Hamlib reports what the radio
supports (`dump_caps`), and each poll is tried once at connect time, so the
UI only offers controls this radio really has. There is no spectrum scope
through Hamlib.

Values are converted to the same Icom-style units the rest of the app uses.
"""

import asyncio
import logging
import shutil

from radio import Radio, settable

log = logging.getLogger("hamlib")

HAMLIB_MODES = {"CWR": "CW-R", "RTTYR": "RTTY-R"}
TO_HAMLIB = {v: k for k, v in HAMLIB_MODES.items()}
AGC_LABELS = {1: "SUPER", 2: "FAST", 5: "MID", 3: "SLOW", 6: "AUTO"}   # Hamlib codes; OFF/USER skipped


def _interp(x, pts):
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (x - x0) * (y1 - y0) / (x1 - x0)
    return pts[-1][1]


# Hamlib units -> Icom meter units.
def strength_raw(db):
    return int(max(0, (db + 54) / 54 * 120) if db <= 0 else min(241, 120 + db / 60 * 121))


def po_raw(frac):
    return int(_interp(frac * 100, [(0, 0), (50, 143), (100, 213)]))


def swr_raw(swr):
    return int(_interp(swr, [(1, 0), (1.5, 48), (2, 80), (3, 120), (9.9, 255)]))


class HamlibRadio(Radio):
    def __init__(self, caps, model=None, port=None, baud=None, rigctld=None):
        super().__init__(dict(caps))
        self.model, self.port, self.baud = model, port, baud
        if rigctld:
            host, _, tcp = rigctld.rpartition(":")
            self.host, self.tcp, self.spawn = host or "127.0.0.1", int(tcp), False
        else:
            if not (model and port):
                raise SystemExit("hamlib needs --hamlib-model and --port, or --rigctld host:port")
            self.host, self.tcp, self.spawn = "127.0.0.1", 4599, True
        self._proc = self._task = None
        self._reader = self._writer = None
        self._lock = asyncio.Lock()
        self._caps_cbs = []
        self._ops = []          # pending user commands (dedupe by key)
        self._slow = self._rx = self._tx = []
        self._preamp_db = []
        self._att_db = None

    def on_caps(self, cb):
        self._caps_cbs.append(cb)

    async def start(self):
        self._task = asyncio.create_task(self._run(), name="hamlib")

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self.state["tx"] and self._writer:
            try:
                await asyncio.wait_for(self._cmd("T 0"), 1)
            except Exception:
                pass
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()

    # -- connection --

    async def _spawn(self):
        exe = shutil.which("rigctld") or "/opt/homebrew/bin/rigctld"
        args = [exe, "-m", str(self.model), "-r", self.port, "-t", str(self.tcp), "-T", "127.0.0.1"]
        if self.baud:
            args += ["-s", str(self.baud)]
        log.info("starting %s", " ".join(args))
        self._proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        for _ in range(40):
            try:
                return await asyncio.open_connection(self.host, self.tcp)
            except OSError:
                if self._proc.returncode is not None:
                    raise OSError(f"rigctld exited ({self._proc.returncode}); check model/port/baud")
                await asyncio.sleep(0.25)
        raise OSError("rigctld did not start listening")

    async def _run(self):
        while True:
            try:
                if self.spawn:
                    self._reader, self._writer = await self._spawn()
                else:
                    self._reader, self._writer = await asyncio.open_connection(self.host, self.tcp)
                await self._probe()
                self._update(connected=True)
                await self._loop()
            except (OSError, ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError) as e:
                log.warning("rigctld: %s (retrying)", e)
            self._update(connected=False)
            if self._writer:
                self._writer.close()
                self._writer = None
            if self._proc and self._proc.returncode is None:
                self._proc.terminate()
                await self._proc.wait()
            await asyncio.sleep(3)

    async def _cmd(self, line, timeout=3.0):
        """Send one command in extended mode; return (status, raw reply lines)."""
        async with self._lock:
            self._writer.write(f"+{line}\n".encode())
            await self._writer.drain()
            values = []
            while True:
                raw = await asyncio.wait_for(self._reader.readline(), timeout)
                if not raw:
                    raise ConnectionError("rigctld closed the connection")
                text = raw.decode(errors="replace").rstrip("\n")
                if text.startswith("RPRT"):
                    return int(text.split()[1]), values[1:]   # first line echoes the command
                values.append(text)

    # -- discovery --

    async def _probe(self):
        status, lines = await self._cmd("\\dump_caps", timeout=8)
        caps = self.caps
        info = {}
        for ln in lines:
            k, _, v = ln.partition(":")
            info.setdefault(k.strip(), v.strip())
        if info.get("Model name"):
            caps["name"] = f"{info.get('Mfg name', '')} {info['Model name']}".strip()
            self.name = caps["name"]
        modes = info.get("Mode list", "").split()
        if modes:
            caps["modes"] = [HAMLIB_MODES.get(m, m) for m in modes
                             if HAMLIB_MODES.get(m, m) in ("LSB", "USB", "AM", "CW", "CW-R", "RTTY", "RTTY-R", "FM")]
            caps["data_modes"] = [m[3:] for m in modes if m in ("PKTUSB", "PKTLSB", "PKTFM")]
        self._preamp_db = [int(x.rstrip("dB")) for x in info.get("Preamp", "").split() if x.rstrip("dB").isdigit()]
        att = [int(x.rstrip("dB")) for x in info.get("Attenuator", "").split() if x.rstrip("dB").isdigit()]
        self._att_db = att[0] if att else None
        caps["preamp"] = ["OFF"] + [f"{d} dB" for d in self._preamp_db] if self._preamp_db else ["OFF"]
        caps["att_label"] = f"{self._att_db} dB" if self._att_db else "ON"
        # e.g. "AGC levels: 0=OFF 1=SUPERFAST 2=FAST 5=MEDIUM 3=SLOW 6=AUTO 4=USER"
        agc = [int(t.split("=")[0]) for t in info.get("AGC levels", "").split() if t.split("=")[0].isdigit()]
        caps["agc"] = [[c, AGC_LABELS[c]] for c in sorted(AGC_LABELS, key=list(AGC_LABELS).index) if c in agc]

        async def works(cmd):
            try:
                st, _ = await self._cmd(cmd)
                return st == 0
            except asyncio.TimeoutError:
                return False

        levels = {"af": "AF", "rf": "RF", "sql": "SQL", "power": "RFPOWER"}
        caps["levels"] = [k for k, h in levels.items() if await works(f"l {h}")]
        meters = {"smeter": "STRENGTH", "po": "RFPOWER_METER", "swr": "SWR", "alc": "ALC"}
        caps["meters"] = [k for k, h in meters.items() if await works(f"l {h}")]
        feats = [f for f, h in (("nb", "NB"), ("nr", "NR"), ("tuner", "TUNER"))
                 if await works(f"u {h}")]
        if await works("s"):
            feats.append("split")
        caps["features"] = feats
        if not await works("l PREAMP"):
            caps["preamp"] = ["OFF"]
        has_att = self._att_db is not None and await works("l ATT")
        has_agc = caps["agc"] and await works("l AGC")
        if not has_agc:
            caps["agc"] = []

        self._slow = ["f", "m", "t"] + [f"l {levels[k]}" for k in caps["levels"]]
        self._slow += [f"u {h}" for f, h in (("nb", "NB"), ("nr", "NR"), ("tuner", "TUNER")) if f in feats]
        self._slow += (["s"] if "split" in feats else []) + (["l PREAMP"] if len(caps["preamp"]) > 1 else [])
        self._slow += (["l ATT"] if has_att else []) + (["l AGC"] if has_agc else [])
        self._rx = ["l STRENGTH"] if "smeter" in caps["meters"] else ["t"]
        self._tx = [f"l {meters[m]}" for m in ("po", "swr", "alc") if m in caps["meters"]] or ["t"]

        self._settable = settable(caps)
        log.info("%s: levels=%s meters=%s features=%s", caps["name"], caps["levels"],
                 caps["meters"], caps["features"])
        for cb in self._caps_cbs:
            cb(caps)

    # -- polling --

    async def _loop(self):
        i = 0
        while True:
            while self._ops:
                _, line, after = self._ops.pop(0)
                st, _ = await self._cmd(line)
                if st != 0:
                    log.warning("%r rejected (RPRT %d)", line, st)
                if after:
                    await self._read(after)
            i += 1
            if i % 2:
                meters = self._tx if self.state["tx"] else self._rx
                await self._read(meters[(i // 2) % len(meters)])
            else:
                await self._read(self._slow[(i // 2) % len(self._slow)])
            await asyncio.sleep(0.02)

    async def _read(self, line):
        st, vals = await self._cmd(line)
        if st != 0 or not vals:
            return
        v = vals[0].split(": ", 1)[-1].strip()   # "Frequency: 14074000" or a bare value
        try:
            if line == "f":
                self._update(freq=int(float(v)))
            elif line == "m":
                data = v.startswith("PKT")
                mode = HAMLIB_MODES.get(v[3:] if data else v, v[3:] if data else v)
                self._update(mode=mode, data=data)
            elif line == "t":
                self._update(tx=v not in ("0", ""))
                if not self.state["tx"]:
                    self._update(po=0, swr=0, alc=0)
            elif line == "s":
                self._update(split=v == "1")
            elif line == "l STRENGTH":
                self._update(smeter=strength_raw(float(v)))
            elif line == "l RFPOWER_METER":
                self._update(po=po_raw(float(v)))
            elif line == "l SWR":
                self._update(swr=swr_raw(float(v)))
            elif line == "l ALC":
                self._update(alc=int(float(v) * 120))
            elif line == "l PREAMP":
                db = int(float(v))
                self._update(preamp=self._preamp_db.index(db) + 1 if db in self._preamp_db else 0)
            elif line == "l ATT":
                self._update(att=int(float(v)) > 0)
            elif line == "l AGC":
                self._update(agc=int(float(v)))
            elif line.startswith("l "):
                key = {"AF": "af", "RF": "rf", "SQL": "sql", "RFPOWER": "power"}[line[2:]]
                self._update(**{key: round(float(v) * 255)})
            elif line.startswith("u "):
                key = {"NB": "nb", "NR": "nr", "TUNER": "tuner"}[line[2:]]
                self._update(**{key: (int(v) if key == "tuner" else v == "1")})
        except (ValueError, KeyError):
            log.debug("could not parse %r -> %r", line, vals)

    # -- commands --

    def _queue(self, key, line, after=None):
        self._ops = [op for op in self._ops if op[0] != key] + [(key, line, after)]

    async def _apply(self, key, v):
        s = self.state
        if key == "freq":
            self._queue(key, f"F {v}", "f")
        elif key in ("mode", "data"):
            mode = v if key == "mode" else s["mode"]
            data = v if key == "data" else s["data"] and mode in self.caps["data_modes"]
            name = TO_HAMLIB.get(mode, mode)
            self._queue("mode", f"M {'PKT' + name if data else name} 0", "m")
        elif key in ("af", "rf", "sql", "power"):
            h = {"af": "AF", "rf": "RF", "sql": "SQL", "power": "RFPOWER"}[key]
            self._queue(key, f"L {h} {v / 255:.3f}")
        elif key in ("nb", "nr"):
            self._queue(key, f"U {key.upper()} {int(v)}")
        elif key == "tuner":
            self._queue(key, "G TUNE" if v == 2 else f"U TUNER {int(bool(v))}", "u TUNER")
        elif key == "tx":
            self._queue(key, f"T {int(v)}", "t")
        elif key == "split":
            self._queue(key, f"S {int(v)} {'VFOB' if v else 'VFOA'}", "s")
        elif key == "preamp":
            self._queue(key, f"L PREAMP {self._preamp_db[v - 1] if v else 0}")
        elif key == "att":
            self._queue(key, f"L ATT {self._att_db if v else 0}")
        elif key == "agc":
            self._queue(key, f"L AGC {v}")
        self._update(**{key: v})

    async def swap_vfo(self):
        raise ValueError(f"{self.name}: VFO swap isn't available through Hamlib here")

    async def power(self, on: bool):
        self._queue("power", f"\\set_powerstat {int(on)}")
