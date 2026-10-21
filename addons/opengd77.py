"""OpenGD77 add-on: a TYT MD-UV380 / MD-UV390 running OpenGD77, on the page.

Watch (always on, reads only): the radio (model, firmware revision and
build), what it is on now (channel mode: zone, channel number and name,
power, read off its screen every few seconds by opengd77_screen, since
nothing else over USB is live), VFO A and B as the radio holds them right
now, the radio's own zone and channel tables, its stored settings, and, on
request, a picture of its screen.

Control (on request): OpenGD77 has no CAT, so a change is written into the
records the radio starts on (opengd77_mem: last-used channels, settings,
VFOs) and the radio restarts on them: save (C 6 2), back up both sectors,
write only what changed and check the whole sector, restart (C 6 1), and
check what it came back on (apply, _apply_job). Every value is range-checked
first; only the pinned firmware build is changed; Restore puts a backup
back; the clock is set without a restart (C 6 7). opengd77.refusal keeps
everything else off the port. Nothing keys the radio.

Program (on request): the codeplug, through qdmr's dmrconf (qdmr.py). The
codeplug is a qdmr YAML file. "Read" saves a dated copy under "codeplugs";
the page edits it as forms (qdmr_yaml: the file as a model, opened only
when it round-trips to the same dmrconf encoding) or as YAML, and "Write"
sends it:
  0. on a radio whose channel slots have gaps (read from the radio's own
     in-use bitmaps, not through qdmr), refused unless the file's zones point
     where the radio's zones do (qdmr.zones_match). qdmr 0.15.1 renumbers
     sparse slots 1..N but reads zone members as slot numbers; a "Read"
     rebuilds the zones from the radio's own zone table (qdmr.rebuild_zones)
     and drops the 'APRS Channel' qdmr makes up (qdmr.drop_aprs_phantom), so
     a file from a fresh read passes. qdmr's own file is kept beside it.
  1. refused unless dmrconf has the MD-UV380 OpenGD77 driver (qdmr.writable;
     reads need it too: the GD-77 driver doesn't know this radio's layout)
     and the radio reports itself as an MD-UV380/390 (radio-info type 6)
  2. needs "confirm": the page asks first; a codeplug changes what transmits
  3. dmrconf verify and dmrconf encode check the file offline (verify
     crashes on 0.15.1 with openuv380 and is logged; encode applies the
     radio's limits and refuses a bad file before the radio is touched)
  4. a fresh backup is read from the radio and kept, put right the same way;
     if dmrconf reports problems the sparse slots don't explain
     (qdmr.read_problems), stop
  5. dmrconf write, in update mode (settings not in the file are kept). It
     numbers the channels 1..N and rebuilds the zone table, so one write
     onto sparse slots closes the gaps for good
  6. the radio restarts (OpenGD77 does after a write) and is read back once
     it is back on USB; its channels are compared with what was sent,
     and the radio's own memory is read: channels in slots 1..N with the
     file's names, zones by those numbers (qdmr.compacted)
While dmrconf runs, the add-on lets go of the port (one owner at a time)
and the VFO watch pauses. The OpenGD77 CPS or the qdmr app needs the add-on
switched off, the same way.

Config (station.json "addons", or the host file's "addons" on a Mac):

    "opengd77": {
      "enabled": true,
      "port": "auto",              # found by USB ID 1FC9:0094; or a device path
      "label": "MD-UV390",
      "poll": 2,                   # seconds between VFO reads
      "screen_bgr": false,         # true if the screen picture's red and blue are swapped
      "dmrconf": "/opt/homebrew/bin/dmrconf",   # qdmr 0.15.1+ (brew install qdmr)
      "codeplugs": "~/.local/state/radio-remote/opengd77",   # memory backups in its memory/
      "restart_wait": 30           # seconds to wait for the radio after a restart
    }
"""

import asyncio
import base64
import logging
import os
import time

import opengd77_mem as mem
import opengd77_screen
import qdmr
import qdmr_yaml
from addons import Addon
from opengd77 import OpenGD77, OpenGD77Error, SCREEN_H, SCREEN_W, find_port

log = logging.getLogger("opengd77")

RETRY_S = 3.0
MIN_POLL_S = 0.5
SCREEN_MIN_S = 1.0           # a page can't ask for screens faster than this
NOW_S = 4.0                  # how often the screen is read for "on the radio now"
MAX_CODEPLUG = 2_000_000     # bytes of YAML a page may send
REBOOT_TRIES = 4             # reads after a write, while the radio restarts
REBOOT_WAIT_S = 5.0
UV380_TYPE = 6               # radio-info radioType of the MD-UV380 / MD-UV390
RESTART_GONE_S = 1.5         # the radio leaves USB ~0.5 s after C 6 1; don't catch it on the way out
RESTART_WAIT_S = 30.0        # and is back in ~5 s; give up (and say so) after this
MEMORY_BACKUPS = 30          # radio-memory backups kept (one per change)


def _ranges(nums):
    """[9, 10, 11, 13] -> "9-11, 13"."""
    out, start = [], None
    for i, n in enumerate(nums):
        if start is None:
            start = n
        if i + 1 == len(nums) or nums[i + 1] != n + 1:
            out.append(str(start) if start == n else f"{start}-{n}")
            start = None
    return ", ".join(out)


class OpenGD77Addon(Addon):
    name = "opengd77"
    css = True

    def __init__(self, host, opts):
        super().__init__(host, opts)
        port = opts.get("port")
        if not port:
            raise ValueError('needs "port" (the radio\'s USB serial device)')
        self.label = str(opts.get("label") or "MD-UV390")
        self.title = f"{self.label} (OpenGD77): front panel, settings and codeplug"
        self.poll = max(MIN_POLL_S, float(opts.get("poll", 2)))
        self.bgr = bool(opts.get("screen_bgr", False))
        self.radio = OpenGD77(port)
        self.dmrconf = qdmr.Dmrconf(opts.get("dmrconf", "/opt/homebrew/bin/dmrconf"))
        self.codeplugs = os.path.expanduser(opts.get("codeplugs", "~/.local/state/radio-remote/opengd77"))
        self.task = None
        self.busy = None             # what a codeplug job is doing, or None
        self.codeplug = None         # the last codeplug read or written: {file, at, channels}
        self._fidelity = {}          # path -> None (round-trips exactly) or why the editor can't take it
        self.memory_dir = os.path.join(self.codeplugs, "memory")
        self.restart_wait = float(opts.get("restart_wait", RESTART_WAIT_S))
        self.last_change = None      # what the last change did: {at, msg, ok}
        self._reset()

    def _reset(self):
        self.connected = False
        self.error = None
        self.info = None
        self.vfos = None
        self.read_at = None
        self._screen_at = 0.0
        self.layout = None           # the radio's own slots, names and zones, read on connect
        self.control = None          # the radio's memory: settings, last-used channels, stored VFOs
        self._raw = None             # and its raw bytes, to check a change against before sending it
        self.now = None              # what the radio's screen says it is on (opengd77_screen.state)
        self._now_at = 0.0

    async def start(self):
        self._reset()
        self.task = asyncio.ensure_future(self._supervise())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None
        await asyncio.to_thread(self._close)
        self._reset()

    def _close(self):
        with self.radio.lock:
            self.radio.close()

    async def _supervise(self):
        while True:
            try:
                await asyncio.to_thread(self.radio.open)
                self.info = await asyncio.to_thread(self.radio.info)
                log.info("%s: %s, firmware %s built %s", self.radio.port, self.info["model"],
                         self.info["revision"], self.info["built"])
                self.connected, self.error = True, None
                self._emit_status()
                self.emit({"info": self.info})
                try:
                    self.layout = await asyncio.to_thread(self.radio.layout)
                except (OSError, OpenGD77Error, ValueError) as e:
                    self.layout = None
                    log.warning("%s: channel and zone tables not read: %s", self.radio.port, e)
                self.emit({"layout": self._layout_out()})
                await self._read_memory()
                while True:
                    vfos = await asyncio.to_thread(self.radio.vfos)
                    self.read_at = time.time()
                    if vfos != self.vfos:
                        self.vfos = vfos
                        self.emit({"vfos": vfos})
                    if self.info.get("screen") and time.monotonic() - self._now_at >= NOW_S:
                        await self._read_now()
                    await asyncio.sleep(self.poll)
            except asyncio.CancelledError:
                raise
            except (OSError, OpenGD77Error, ValueError) as e:
                why = f"{type(e).__name__}: {e}" if not isinstance(e, OpenGD77Error) else str(e)
                if self.connected or self.error != why:
                    log.warning("%s: %s", self.radio.port, why)
                self.connected, self.error = False, why
                self._emit_status()
            await asyncio.to_thread(self._close)
            await asyncio.sleep(RETRY_S)

    async def _read_now(self, raw=None):
        """What the radio is on, read off its screen (the only live view of
        zone, channel and power over USB). Sent to pages when it changes."""
        self._now_at = time.monotonic()
        if raw is None:
            raw = await asyncio.to_thread(self.radio.screen)
        now = await asyncio.to_thread(opengd77_screen.state, opengd77_screen.pixels(raw), self.layout)
        now["at"] = time.time()
        if {k: v for k, v in now.items() if k != "at"} != {k: v for k, v in (self.now or {}).items() if k != "at"}:
            self.now = now                       # "at": when the screen first showed this
            self.emit({"now": now})

    def _emit_status(self):
        self.emit({"status": self.status_out()})

    def status_out(self):
        return {"connected": self.connected, "error": self.error, "port": self.radio.port,
                "read_at": self.read_at, "busy": self.busy}

    def snapshot(self):
        return {"status": self.status_out(), "info": self.info, "vfos": self.vfos, "now": self.now,
                "screen_size": [SCREEN_W, SCREEN_H], "screen_bgr": self.bgr,
                "codeplug": self._codeplug_out(), "layout": self._layout_out(), "control": self.control,
                "radio": self._radio_facts(), "last_change": self.last_change}

    def can_stop(self):
        return f"{self.label}: wait, {self.busy}." if self.busy else None

    async def command(self, ws, verb, m):
        if verb == "screen":
            await self._screen(ws)
        elif verb == "codeplug_read":
            await self._job(ws, "reading the codeplug", self._read_job)
        elif verb == "codeplug_write":
            await self._write(ws, m)
        elif verb == "codeplug_get":
            self._send_yaml(ws)
        elif verb == "codeplug_model":
            await self._send_model(ws)
        elif verb == "codeplug_write_model":
            await self._write_model(ws, m)
        elif verb == "codeplug_load":
            self._load_file(ws, m)
        elif verb == "codeplug_render":
            self._render_model(ws, m)
        elif verb == "apply":
            await self._apply(ws, m)
        elif verb == "memory_refresh":
            await self._job(ws, "reading the radio's settings", self._refresh_job, kind="control")
        elif verb == "clock_sync":
            await self._job(ws, "setting the radio's clock", self._clock_job, kind="control")
        elif verb == "memory_restore":
            await self._restore(ws, m)
        else:
            self.notice(f"{self.label}: {verb!r} isn't something this radio can do over USB. "
                        "OpenGD77 has no CAT: there is no command to key, tune live or read the S-meter.",
                        "warn", ws)

    # ---------------------------------------------------------- codeplug (dmrconf)

    def _codeplug_out(self):
        c = self.codeplug
        return c and {k: c.get(k) for k in ("file", "at", "channels", "kind", "problems")}

    def _send_yaml(self, ws):
        c = self.codeplug
        if not c:
            self.notice(f"{self.label}: no codeplug read yet.", "warn", ws)
            return
        try:
            with open(c["path"]) as f:
                self.emit({"codeplug_yaml": f.read(), "file": c["file"]}, ws)
        except OSError as e:
            self.notice(f"{self.label}: {c['file']} not read: {e}", "warn", ws)

    def _model_check(self, path, text):
        """None when the structured editor can take this file: qdmr_yaml reads
        it, and what it writes back encodes (dmrconf, offline) to the very
        same binary as the file itself. Else why not; the YAML editor stays."""
        if path in self._fidelity:
            return self._fidelity[path]
        try:
            model = qdmr_yaml.load(text)
            again = qdmr_yaml.dump(model)
            if qdmr_yaml.load(again) != model:
                raise qdmr_yaml.YamlError("it doesn't read back the same")
            work = self._stamp("roundtrip")
            with open(work, "w") as f:
                f.write(again)
            try:
                a = self.dmrconf.encode(path, work + ".orig.bin")
                b = self.dmrconf.encode(work, work + ".back.bin")
            finally:
                for x in (work, work + ".orig.bin", work + ".back.bin"):
                    if os.path.exists(x):
                        os.remove(x)
            why = None if a == b else "written back, it would encode differently"
        except (qdmr_yaml.YamlError, qdmr.QdmrError, OSError) as e:
            why = str(e)[:300]
        self._fidelity[path] = why
        return why

    async def _send_model(self, ws):
        c = self.codeplug
        if not c:
            self.notice(f"{self.label}: read the codeplug from the radio first.", "warn", ws)
            return
        try:
            with open(c["path"]) as f:
                text = f.read()
        except OSError as e:
            self.notice(f"{self.label}: {c['file']} not read: {e}", "warn", ws)
            return
        why = await asyncio.to_thread(self._model_check, c["path"], text)
        if why:
            self.emit({"codeplug_model": None, "file": c["file"], "model_error": why}, ws)
            return
        model = qdmr_yaml.load(text)
        self.emit({"codeplug_model": qdmr_yaml.to_json(model), "file": c["file"],
                   "model_problems": qdmr.model_problems(model)}, ws)

    def _load_file(self, ws, m):
        """A qdmr YAML file from the page becomes the codeplug being edited
        (kept as *-loaded.yaml). Nothing goes to the radio until Write."""
        text, name = m.get("yaml"), str(m.get("name") or "file")[:80]
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_CODEPLUG:
            self.notice(f"{self.label}: that file is empty or too big.", "warn", ws)
            return
        if self.busy:
            self.notice(f"{self.label}: busy {self.busy}.", "warn", ws)
            return
        path = self._stamp("loaded")
        with open(path, "w") as f:
            f.write(text)
        self._keep(path, f"loaded from {name}")
        self.emit({"codeplug": self._codeplug_out()})
        self.notice(f"{self.label}: {name} loaded ({len(self.codeplug['channels'])} channels). "
                    "Nothing is on the radio until you write it.", "info", ws)

    def _render_model(self, ws, m):
        """The page's edited model as YAML (for Download)."""
        try:
            text = qdmr_yaml.dump(qdmr_yaml.from_json(m.get("model")))
        except (qdmr_yaml.YamlError, RecursionError) as e:
            self.notice(f"{self.label}: the edited codeplug isn't valid ({e}).", "warn", ws)
            return
        self.emit({"codeplug_yaml": text, "file": "codeplug-edited.yaml", "rendered": True}, ws)

    async def _write_model(self, ws, m):
        """The page's edited model -> YAML -> the same checked write as YAML."""
        try:
            model = qdmr_yaml.from_json(m.get("model"))
            probs = qdmr.model_problems(model)
            text = qdmr_yaml.dump(model)
        except (qdmr_yaml.YamlError, RecursionError) as e:
            self.notice(f"{self.label}: not written: the edited codeplug isn't valid ({e}).", "warn", ws)
            return
        if probs:
            self.notice(f"{self.label}: not written: {'; '.join(probs[:4])}"
                        f"{f' (+{len(probs) - 4} more)' if len(probs) > 4 else ''}.", "warn", ws)
            return
        await self._write(ws, {"yaml": text, "confirm": m.get("confirm")})

    def _stamp(self, what):
        os.makedirs(self.codeplugs, exist_ok=True)
        return os.path.join(self.codeplugs, time.strftime(f"%Y-%m-%d_%H%M%S-{what}.yaml"))

    def _keep(self, path, kind, problems=()):
        with open(path) as f:
            text = f.read()
        self.codeplug = {"path": path, "file": os.path.basename(path), "at": time.time(),
                         "channels": qdmr.channels(text), "kind": kind, "problems": list(problems)[:20]}
        return text

    def _device(self):
        """dmrconf's --device: macOS lists the radio as both cu.* and tty.*,
        and dmrconf won't guess between them."""
        p = self.radio.port
        return os.path.basename(p) if p and p != "auto" else None

    async def _job(self, ws, what, fn, *args, kind="codeplug"):
        """Run a job with the watch stopped and the port released, one job at
        a time: a dmrconf job (kind "codeplug") or one on our own link
        (kind "control", which opens the port itself)."""
        if self.busy:
            self.notice(f"{self.label}: busy {self.busy}.", "warn", ws)
            return False
        self.busy = what
        if self.task:                                  # let go of the port
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None
        await asyncio.to_thread(self._close)
        self.connected = False
        self._emit_status()
        ok, msg = False, f"{what} was interrupted"
        try:
            msg = await asyncio.to_thread(fn, *args)
            ok = True
        except (qdmr.QdmrError, OpenGD77Error, OSError, ValueError) as e:
            msg = f"{what} failed: {e}"
            log.warning("%s: %s", what, e)
        finally:
            await asyncio.to_thread(self._close)
            self.busy = None                       # free before the notice: a page may act on it at once
            if kind == "control":
                self.last_change = {"at": time.time(), "what": what, "msg": msg, "ok": ok}
            self.task = asyncio.ensure_future(self._supervise())
            self._emit_status()
            self.emit({"codeplug": self._codeplug_out(), "control": self.control, "last_change": self.last_change})
            self.notice(f"{self.label}: {msg}", "info" if ok else "warn", ws)
        return ok

    def _read_job(self):
        why = self.dmrconf.writable()          # the GD-77 driver would misread this radio's layout
        if why:
            raise qdmr.QdmrError(why.replace("it would write with the GD-77 command, which this radio "
                                             "acknowledges and ignores", "its GD-77 driver doesn't know "
                                             "this radio's codeplug layout"))
        layout = self._layout()
        path = self._stamp("read")
        out = self.dmrconf.read(path, device=self._device())
        problems, fixed = self._put_right(path, out, layout)
        self._keep(path, "read", problems)
        msg = f"codeplug read: {len(self.codeplug['channels'])} channels, saved as {self.codeplug['file']}."
        if fixed:
            msg += " " + fixed
        if problems:
            msg += (f" qdmr could not decode it cleanly ({len(problems)} problems, e.g. {problems[0]}): "
                    "writing is blocked until it can.")
        return msg

    def _after_reboot(self, fn):
        """fn(), retried while the radio restarts. OpenGD77 restarts the radio
        when a codeplug write ends, so it leaves USB for a few seconds and the
        first read after a write fails (mac-shack, 2026-10-17: "Device not
        configured" 16 s into dmrconf's read-back, back on USB 4 s later).
        Bounded: REBOOT_TRIES tries, REBOOT_WAIT_S apart."""
        for i in range(REBOOT_TRIES):
            if i:
                time.sleep(REBOOT_WAIT_S)
            try:
                return fn()
            except qdmr.QdmrError as e:
                last = e
                log.info("after the write, try %d: %s", i + 1, str(e)[:200])
        raise last

    def _layout(self):
        """The radio's own channel slots, names and zones, read over our
        link while no one else has the port (inside a job)."""
        try:
            self.radio.open()
            return self.radio.layout()
        except OpenGD77Error as e:
            raise qdmr.QdmrError(f"the radio's channel and zone tables weren't read ({e})")
        finally:
            self._close()

    def _put_right(self, path, out, layout):
        """Make dmrconf's read at path say what the radio holds: drop qdmr's
        made-up APRS channel, and on sparse slots rebuild the zones from the
        radio's zone table. qdmr's own file is kept as *-qdmr.yaml. Returns
        (problems left, what was done or "")."""
        with open(path) as f:
            text = f.read()
        problems = qdmr.read_problems(out)
        slots = layout["slots"]
        gaps = slots != list(range(1, len(slots) + 1))
        if problems and not gaps:
            return problems, ""                          # not the sparse-slot bug: leave it blocked
        done = []
        fixed, pinned = qdmr.pin_default_radio_id(text)
        if pinned:
            done.append(f"{pinned} channels name the default DMR ID (qdmr would otherwise write an override of 0)")
        lost = qdmr.power_losses(layout)
        if lost:
            problems = problems + [f"{len(lost)} channels sit at a power level qdmr can't write back "
                                   f"(e.g. {lost[0][0]!r} at level {lost[0][1]}; it writes only "
                                   f"{', '.join(map(str, qdmr.QDMR_POWER_LEVELS))}): a write would change "
                                   "their power. Set them at the radio first"]
        fixed, dropped = qdmr.drop_aprs_phantom(fixed, len(slots))
        if dropped:
            done.append("dropped the 'APRS Channel' qdmr makes up for the radio's unset APRS frequency")
        if gaps:
            fixed = qdmr.rebuild_zones(fixed, layout)
            done.append(f"zones rebuilt from the radio's own zone table (its channels sit in slots "
                        f"{_ranges(slots)}, which qdmr misnumbers in zones)")
            problems = []
        why = qdmr.zones_match(fixed, layout)
        if why:
            problems = problems + [f"zones differ from the radio's: {why}"]
        if fixed != text:
            os.replace(path, path[:-len(".yaml")] + "-qdmr.yaml")
            with open(path, "w") as f:
                f.write(fixed)
        msg = "; ".join(done)
        return problems, (msg[:1].upper() + msg[1:] + ".") if msg else ""

    async def _write(self, ws, m):
        text = m.get("yaml")
        if not isinstance(text, str) or not text.strip():
            self.notice(f"{self.label}: no codeplug to write.", "warn", ws)
            return
        if len(text.encode()) > MAX_CODEPLUG:
            self.notice(f"{self.label}: codeplug too big.", "warn", ws)
            return
        if m.get("confirm") is not True:
            self.notice(f"{self.label}: writing a codeplug changes what the radio transmits; confirm first.", "warn", ws)
            return
        if (self.info or {}).get("type") != UV380_TYPE:
            self.notice(f"{self.label}: not written: the radio has not reported itself as an MD-UV380/390 "
                        f"({(self.info or {}).get('model') or 'not connected'}).", "warn", ws)
            return
        why = await asyncio.to_thread(self.dmrconf.writable)
        if why:
            self.notice(f"{self.label}: not written: {why}", "warn", ws)
            return
        try:
            layout = await asyncio.to_thread(self.radio.layout)
        except (OSError, OpenGD77Error) as e:
            self.notice(f"{self.label}: not written: the radio's channel and zone tables weren't read ({e}).",
                        "warn", ws)
            return
        slots = layout["slots"]
        if slots != list(range(1, len(slots) + 1)):
            why = qdmr.zones_match(text, layout)
            if why:
                gaps = sorted(set(range(1, (slots or [0])[-1] + 1)) - set(slots))
                self.notice(f"{self.label}: not written: the radio's channels sit in slots with gaps (empty: "
                            f"{_ranges(gaps)}), and this file's zones don't point where the radio's do ({why}). "
                            "Read the codeplug again: a read puts the zones right, and the write closes the gaps.",
                            "warn", ws)
                return
        if any(c.get("name") == qdmr.APRS_PHANTOM for c in qdmr.channels(text)):
            self.notice(f"{self.label}: not written: the file has qdmr's made-up '{qdmr.APRS_PHANTOM}', which "
                        "would become a real channel. Read the codeplug again (a read drops it).", "warn", ws)
            return
        text, _ = qdmr.pin_default_radio_id(text)     # a "!default" radioId would be written as override 0
        await self._job(ws, "writing the codeplug", self._write_job, text, layout)

    def _write_job(self, text, layout):
        sent = self._stamp("to-write")
        with open(sent, "w") as f:
            f.write(text)
        want = qdmr.channels(text)
        if self.dmrconf.verify(sent) == "crashed":
            log.warning("dmrconf verify crashed (qdmr 0.15.1, openuv380); encode checks it instead")
        try:                                       # offline: parses the file and applies the radio's limits
            self.dmrconf.encode(sent, sent + ".bin")
        finally:
            if os.path.exists(sent + ".bin"):
                os.remove(sent + ".bin")
        dev = self._device()
        backup = self._stamp("before-write")
        out = self.dmrconf.read(backup, device=dev)                  # a copy of what the radio held
        try:
            problems, _ = self._put_right(backup, out, layout)
        except qdmr.QdmrError as e:
            problems = [str(e)]
        if problems:
            raise qdmr.QdmrError(f"not written: qdmr can't read this radio's codeplug cleanly "
                                 f"({len(problems)} problems, e.g. {problems[0]}), so a write would damage it. "
                                 f"Nothing was sent.")
        self.dmrconf.write(sent, device=dev)
        prev = os.path.basename(backup)
        after = self._stamp("read-back")
        try:
            self._after_reboot(lambda: self.dmrconf.read(after, device=self._device()))
        except qdmr.QdmrError as e:
            raise qdmr.QdmrError(f"written, but not read back: the radio didn't answer after its restart "
                                 f"({str(e)[:300]}). Read the codeplug to check it. "
                                 f"The radio's previous codeplug is {prev}.")
        self._keep(after, "read back after write")
        ok, diff = qdmr.same_channels(want, self.codeplug["channels"])
        if not ok:
            raise qdmr.QdmrError(f"written, but the read-back differs: {diff}. "
                                 f"The radio's previous codeplug is {prev}.")
        why = qdmr.compacted(text, self._after_reboot(self._layout))
        if why:
            raise qdmr.QdmrError(f"written, but the radio's own memory differs: {why}. "
                                 f"The radio's previous codeplug is {prev}.")
        return (f"codeplug written and read back: {len(want)} channels match, in slots 1-{len(want)} with "
                f"the zones as sent. Previous codeplug kept as {os.path.basename(backup)}.")

    # ---------------------------------------------------------- control (the radio's memory)

    def _layout_out(self):
        """The radio's own zones and channels, for the page's channel picker."""
        lay = self.layout
        if not lay:
            return None
        return {"zones": lay["zones"], "slots": lay["slots"],
                "channels": {str(n): {**(lay.get("channels", {}).get(n) or {}), "name": lay["names"].get(n),
                                      "power": lay["power"].get(n)} for n in lay["slots"]}}

    def _radio_facts(self):
        return {"power_names": list(mem.POWER_NAMES), "steps_hz": list(mem.STEPS_HZ),
                "bands_hz": [list(b) for b in mem.BANDS_HZ], "ctcss": [t / 10 for t in mem.CTCSS],
                "dcs": ["%03X" % d for d in mem.DCS], "schema": mem.settings_schema()}

    def _why_not_control(self, info=None):
        info = info or self.info or {}
        if info.get("type") != UV380_TYPE:
            return f"the radio has not reported itself as an MD-UV380/390 ({info.get('model') or 'not connected'})"
        if info.get("revision") not in mem.PINNED_REVISIONS:
            return (f"OpenGD77 {info.get('revision')} is not the build whose memory layout was checked "
                    f"({', '.join(sorted(mem.PINNED_REVISIONS))}, R20260131); changing settings is off")
        return None

    def _memory_view(self, raw, saved):
        """raw (opengd77.OpenGD77.memory) as the page sees it."""
        self._raw = raw
        return {"settings": mem.settings(raw["settings"]), "lucz": mem.lucz(raw["lucz"]),
                "vfo": [mem.vfo(raw["vfo"][:mem.CHANNEL_SIZE]), mem.vfo(raw["vfo"][mem.CHANNEL_SIZE:])],
                "at": time.time(), "saved": saved, "why_not": self._why_not_control(),
                "backups": self._backups()[-5:]}

    def _backups(self):
        try:
            return sorted(n for n in os.listdir(self.memory_dir) if n.endswith("-memory.bin"))
        except OSError:
            return []

    async def _read_memory(self):
        """The radio's memory as stored (no save: the watch writes nothing).
        A setting changed at the keypad since the radio last saved shows its
        old value until a Refresh or a change."""
        why = self._why_not_control()
        try:
            raw = await asyncio.to_thread(self.radio.memory)
            same = raw == self._raw and bool((self.control or {}).get("saved"))   # just saved (a change, Refresh)
            self.control = self._memory_view(raw, same)
        except (OSError, OpenGD77Error, ValueError) as e:
            self.control = {"why_not": why or f"the radio's settings were not read ({e})"}
            log.warning("%s: settings not read: %s", self.radio.port, e)
        self.emit({"control": self.control})

    def _open_checked(self):
        """Open our link for a change; the radio must be the pinned build."""
        self.radio.open()
        info = self.radio.info()
        why = self._why_not_control(info)
        if why:
            raise OpenGD77Error(why)
        return info

    def _refresh_job(self):
        self._open_checked()
        self.radio.save()
        raw = self.radio.memory()
        mem.check_settings(raw["settings"])
        self.control = self._memory_view(raw, True)
        return "settings read (the radio saved them first, so these are what it is on now)."

    def _clock_job(self):
        self._open_checked()
        t = int(time.time())
        self.radio.set_clock(t)
        return f"clock set to {time.strftime('%H:%M:%S', time.gmtime(t))} UTC."

    def _backup_memory(self):
        """The writable sectors, kept before every change (newest MEMORY_BACKUPS)."""
        os.makedirs(self.memory_dir, exist_ok=True)
        secs = self.radio.sectors()
        path = os.path.join(self.memory_dir, time.strftime("%Y-%m-%d_%H%M%S-memory.bin"))
        with open(path, "wb") as f:
            for a in sorted(secs):
                f.write(secs[a])
        old = sorted(n for n in os.listdir(self.memory_dir) if n.endswith("-memory.bin"))
        for n in old[:-MEMORY_BACKUPS]:
            os.remove(os.path.join(self.memory_dir, n))
        return path, secs

    def _live_lucz(self):
        """Where the radio is now, from its screen: (zone index, channel index
        in zone) or ("all", channel number), or None. Its last-used-channel
        table is saved only at power off, so the flash copy may be behind."""
        n, lay = self.now or {}, self.layout or {}
        if n.get("mode") != "channel" or not n.get("confirmed") or not lay:
            return None
        names = [z["name"] for z in lay.get("zones") or []]
        if n.get("zone") in names:
            return names.index(n["zone"]), n["number"] - 1
        return "all", n.get("slot")

    def _plan(self, m):
        """The page's change, checked: (settings changes, VFO changes {0|1: {...}},
        go-to (zone, index) or None). Raises ValueError with what is wrong."""
        sets = m.get("settings") or {}
        vfos = {int(k): v for k, v in (m.get("vfos") or {}).items()}
        if not isinstance(sets, dict) or any(k not in (0, 1) or not isinstance(v, dict) for k, v in vfos.items()):
            raise ValueError("malformed change")
        goto = m.get("goto")
        if goto is not None:
            zones = (self.layout or {}).get("zones")
            if zones is None:
                raise ValueError("the radio's zone table was not read")
            z, i = goto.get("zone"), goto.get("index")
            if z == "all":
                if i not in (self.layout or {}).get("slots", []):
                    raise ValueError(f"channel {i} is not in use on the radio")
            elif isinstance(z, int) and 0 <= z < len(zones):
                if not isinstance(i, int) or not 0 <= i < len(zones[z]["slots"]):
                    raise ValueError(f"zone {zones[z]['name']!r} has no channel {i!r}")
            else:
                raise ValueError(f"zone {z!r}")
            goto = (z, i)
        if not sets and not vfos and goto is None:
            raise ValueError("nothing to change")
        return sets, vfos, goto

    async def _apply(self, ws, m):
        why = self._why_not_control()           # (the job opens the port itself: a page may act
        if why:                                 # while the watch is still reconnecting after a change)
            self.notice(f"{self.label}: not changed: {why}.", "warn", ws)
            return
        try:
            plan = self._plan(m)
            self._dry_run(plan)
            live = self._live_lucz()
        except (ValueError, TypeError, AttributeError, KeyError) as e:
            self.notice(f"{self.label}: not changed: {e}.", "warn", ws)
            return
        await self._job(ws, "changing the radio", self._apply_job, plan, live, kind="control")

    def _dry_run(self, plan):
        """Every value checked on the last memory read, before anything is sent."""
        raw = self._raw
        if not raw:
            return
        sets, vfos, goto = plan
        changes = dict(sets)
        if goto:
            changes.update(zone=0, mode="channel")
        mem.patch_settings(raw["settings"], changes)
        for k, ch in vfos.items():
            mem.patch_vfo(raw["vfo"][k * mem.CHANNEL_SIZE:(k + 1) * mem.CHANNEL_SIZE], ch)

    def _apply_job(self, plan, live):
        sets, vfos, goto = plan
        n_zones = len((self.layout or {}).get("zones") or [])
        self._open_checked()
        self.radio.save()                                  # flash = what the radio is on now
        raw = self.radio.memory()
        mem.check_settings(raw["settings"])
        backup, secs = self._backup_memory()
        st, lu, vf = raw["settings"], raw["lucz"], raw["vfo"]

        changes = dict(sets)
        if goto:
            z, i = goto
            changes.update(zone=n_zones if z == "all" else z, mode="channel")
        new_st = mem.patch_settings(st, changes)
        if new_st[80] > new_st[78]:
            raise ValueError("the off brightness can't be above the day brightness")
        new_lu = lu
        if live and not (goto and goto[0] == live[0]):     # keep where the radio is now
            lz, li = live
            new_lu = mem.patch_lucz(new_lu, all_channel=li) if lz == "all" else mem.patch_lucz(new_lu, lz, li)
        if goto:
            z, i = goto
            new_lu = mem.patch_lucz(new_lu, all_channel=i) if z == "all" else mem.patch_lucz(new_lu, z, i)
        new_vf = bytearray(vf)
        for k, ch in vfos.items():
            a = k * mem.CHANNEL_SIZE
            new_vf[a:a + mem.CHANNEL_SIZE] = mem.patch_vfo(vf[a:a + mem.CHANNEL_SIZE], ch)
        new_vf = bytes(new_vf)

        spans = (mem.spans(mem.LUCZ_ADDR, lu, new_lu) + mem.spans(mem.SETTINGS_ADDR, st, new_st)
                 + mem.spans(mem.VFO_ADDR, vf, new_vf))
        if not spans:
            self.control = self._memory_view(raw, True)
            os.remove(backup)
            return "nothing to change: the radio is already set that way."
        log.info("changing the radio: %s; backup %s", ", ".join(f"{a:#x}+{len(d)}" for a, d in spans),
                 os.path.basename(backup))
        self.radio.write_flash(spans, before=secs)       # raises (no restart) if anything else moved
        self.radio.restart()
        self._close()
        t0 = time.monotonic()
        back = self._wait_back()
        after = self.radio.memory()
        self.control = self._memory_view(after, True)
        drift = self._drift(new_lu, new_st, new_vf, after)
        what = self._describe(sets, vfos, goto)
        if drift:
            raise OpenGD77Error(f"{what} written, but after its restart the radio holds something else: "
                                f"{drift}. The memory before the change is {os.path.basename(backup)} (Restore).")
        return f"{what}: done, the radio restarted in {back - t0 + RESTART_GONE_S:.0f} s."

    def _wait_back(self):
        """After a restart: wait until the radio answers again (bounded), and
        leave the link open on it. Returns when it answered (monotonic)."""
        time.sleep(RESTART_GONE_S)
        end = time.monotonic() + self.restart_wait
        last = None
        while time.monotonic() < end:
            try:
                if self.radio.want != "auto" or find_port():
                    self.radio.open()
                    self.info = self.radio.info()
                    return time.monotonic()
            except (OSError, OpenGD77Error) as e:
                last = e
                self._close()
            time.sleep(1.0)
        raise OpenGD77Error(f"the radio did not come back on USB within {self.restart_wait:.0f} s after its "
                            f"restart ({last or 'not seen'}). Check it is on; the change was written first.")

    def _drift(self, lu, st, vf, after):
        """What the radio holds after its restart that differs from what was written."""
        out = []
        for name, want, got in (("last-used channels", lu, after["lucz"]), ("settings", st, after["settings"]),
                                ("VFOs", vf, after["vfo"])):
            if want != got:
                out.append(name)
        try:                                  # and the VFOs it is running on, not only the stored ones
            live = self.radio.read(1, mem.VFO_ADDR, mem.VFO_LEN + 8)[:mem.VFO_LEN]
            if live != vf and "VFOs" not in out:
                out.append("the VFOs it is running on")
        except OpenGD77Error as e:
            out.append(f"the live VFOs ({e})")
        return ", ".join(out)

    def _describe(self, sets, vfos, goto):
        parts = []
        if goto:
            z, i = goto
            lay = self.layout or {}
            if z == "all":
                parts.append(f"channel {i} {lay.get('names', {}).get(i, '')!r} in All Channels")
            else:
                zone = lay["zones"][z]
                slot = zone["slots"][i]
                parts.append(f"{zone['name']} Ch {i + 1} {lay.get('names', {}).get(slot, '')!r}")
        for k, ch in vfos.items():
            parts.append(f"VFO {'AB'[k]}" + (f" {ch['rx_hz'] / 1e6:.5f} MHz" if "rx_hz" in ch else ""))
        labels = {s["key"]: s["label"] for s in mem.settings_schema()}
        named = [labels.get(k, {"mode": "mode", "vfo": "active VFO", "zone": "zone", "tz_minutes": "timezone",
                                "tz_local": "clock display"}.get(k, k)) for k in sets]
        if named:
            parts.append(", ".join(named))
        return "; ".join(parts) or "change"

    async def _restore(self, ws, m):
        if m.get("confirm") is not True:
            self.notice(f"{self.label}: restoring puts the radio's settings, VFOs and channel back; confirm first.",
                        "warn", ws)
            return
        names = self._backups()
        name = m.get("file") or (names[-1] if names else None)
        if not name or name not in names:
            self.notice(f"{self.label}: no memory backup to restore.", "warn", ws)
            return
        await self._job(ws, f"restoring {name}", self._restore_job, os.path.join(self.memory_dir, name),
                        kind="control")

    def _restore_job(self, path):
        with open(path, "rb") as f:
            img = f.read()
        secs = sorted({a - a % mem.SECTOR for a, _ in mem.WRITABLE})
        if len(img) != len(secs) * mem.SECTOR:
            raise ValueError(f"{os.path.basename(path)} is not a memory backup")
        pick = lambda a, n: img[secs.index(a - a % mem.SECTOR) * mem.SECTOR + a % mem.SECTOR:][:n]  # noqa: E731
        want = {a: pick(a, n) for a, n in mem.WRITABLE}
        mem.check_settings(want[mem.SETTINGS_ADDR])
        self._open_checked()
        self.radio.save()
        raw = self.radio.memory()
        now = {mem.LUCZ_ADDR: raw["lucz"], mem.SETTINGS_ADDR: raw["settings"], mem.VFO_ADDR: raw["vfo"]}
        spans = [s for a, _ in mem.WRITABLE for s in mem.spans(a, now[a], want[a])]
        if not spans:
            return f"the radio already holds what {os.path.basename(path)} has."
        _, secs = self._backup_memory()
        self.radio.write_flash(spans, before=secs)
        self.radio.restart()
        self._close()
        self._wait_back()
        after = self.radio.memory()
        self.control = self._memory_view(after, True)
        drift = self._drift(want[mem.LUCZ_ADDR], want[mem.SETTINGS_ADDR], want[mem.VFO_ADDR], after)
        if drift:
            raise OpenGD77Error(f"restored, but after its restart the radio holds something else: {drift}")
        return f"restored {os.path.basename(path)}; the radio restarted."

    async def _screen(self, ws):
        if not self.connected:
            self.notice(f"{self.label} is not connected.", "warn", ws)
            return
        if not (self.info or {}).get("screen"):
            self.notice(f"No screen picture for a {self.info.get('model')}.", "warn", ws)
            return
        now = time.monotonic()
        if now - self._screen_at < SCREEN_MIN_S:
            return
        self._screen_at = now
        try:
            raw = await asyncio.to_thread(self.radio.screen)
        except (OSError, OpenGD77Error) as e:
            self.notice(f"{self.label}: screen not read: {e}", "warn", ws)
            return
        self.emit({"screen": base64.b64encode(raw).decode(), "screen_at": time.time()}, ws)
        await self._read_now(raw)


ADDON = OpenGD77Addon
