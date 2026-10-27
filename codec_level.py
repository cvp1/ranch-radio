"""The USB codec's output level, per use: data (Winlink) or voice.

ARDOP into the IC-7300 needs the codec far below its factory output (-38 dB
keeps ALC near 0 at full set power; 0 dB drove ALC to 0.92), while voice from
the page wants the level it had. Arming a Winlink session sets
--winlink-codec-db; disarming, and the server stopping, put back whatever the
codec was at just before.

The right level differs by band (the operator on air, 2026-10-15, hand-tuned to
ALC zero: 80 m -38 dB, 40 m -44 dB, 20 m -50 dB), so --winlink-codec-db is
one number or a per-band table, "default=-38,80m=-38,40m=-44,20m=-50". The
band is the session's target frequency (the connect URL's freq=). A level the operator changes by hand during a
session is logged on disarm, never kept: the table stays the only source.

Voice from the page (--voice-codec-db, same table form) is the second use:
set when the page's own PTT keys, restored when it unkeys. Apps keying over
rigctl (JTDX) never touch it, so their own level stays theirs. One use holds
the codec at a time. The page can step the level for a band and use (never
below --codec-min-db, never above 0 dB), live while that use holds the codec,
and save it: the save hook (--codec-save) writes it into the host file.

Backends:
  - Linux: ALSA through `amixer` (--winlink-codec-mixer CARD:CONTROL; the
    card defaults to the one in --audio-device, the control to PCM)
  - macOS: CoreAudio through ctypes, the output volume in dB of the device
    named by --winlink-codec-mixer (default: --audio-device)
  - --sim with no mixer named: a level held in memory, so the page and the
    tests see it move
  - CODEC_LEVEL_BACKEND=alsa|coreaudio in the environment forces a backend
    (the tests pin ALSA + a fake amixer so they run, and never touch
    CoreAudio, on macOS too)
"""

import asyncio
import ctypes
import ctypes.util
import logging
import math
import os
import re
import struct
import sys

log = logging.getLogger("codec")

MACOS = {"alsa": False, "coreaudio": True}.get(os.environ.get("CODEC_LEVEL_BACKEND", "").lower(),
                                                sys.platform == "darwin")
DEFAULT_CONTROL = "PCM"
DEFAULT_CARD = "CODEC"
ADJUST_TOLERANCE_DB = 0.5     # a hand-moved level beyond this is logged on disarm


class CodecError(Exception):
    pass


def alsa_spec(device):
    """--audio-device -> CARD:CONTROL for amixer (plughw:CARD=CODEC,DEV=0 -> CODEC:PCM)."""
    m = re.search(r"CARD=([^,]+)", device or "")
    return f"{m.group(1) if m else DEFAULT_CARD}:{DEFAULT_CONTROL}"


class SimLevel:
    def __init__(self, db=0.0):
        self.db = db

    async def get(self):
        return self.db

    async def set(self, db):
        self.db = float(db)


class AlsaLevel:
    def __init__(self, spec):
        card, _, control = spec.partition(":")
        self.card = card or DEFAULT_CARD
        self.control = control or DEFAULT_CONTROL

    async def _amixer(self, *args):
        try:
            p = await asyncio.create_subprocess_exec(
                "amixer", "-c", self.card, *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            raise CodecError(f"amixer: {e}") from None
        out, err = await p.communicate()
        if p.returncode != 0:
            raise CodecError((err or out).decode(errors="replace").strip() or f"amixer exit {p.returncode}")
        return out.decode(errors="replace")

    async def get(self):
        out = await self._amixer("sget", self.control)
        m = re.search(r"\[(-?\d+(?:\.\d+)?)dB\]", out)
        if not m:
            raise CodecError(f"no dB level in amixer's answer for {self.card}:{self.control}")
        return float(m.group(1))

    async def set(self, db):
        await self._amixer("-q", "--", "sset", self.control, f"{float(db):.2f}dB")


def _fourcc(s):
    return struct.unpack(">I", s.encode())[0]


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                ("element", ctypes.c_uint32)]


class CoreAudioLevel:
    """Output volume (dB) of a CoreAudio device, by name. The main element if
    the device has one, otherwise channels 1 and 2 together."""

    SYSTEM = 1
    DEVICES = _fourcc("dev#")
    NAME = _fourcc("lnam")
    VOLUME_DB = _fourcc("vold")
    GLOBAL = _fourcc("glob")
    OUTPUT = _fourcc("outp")
    UTF8 = 0x08000100

    def __init__(self, name):
        self.name = name
        self._ca = self._cf = None

    def _libs(self):
        if self._ca is None:
            path = ctypes.util.find_library("CoreAudio")
            cf = ctypes.util.find_library("CoreFoundation")
            if not (path and cf):
                raise CodecError("CoreAudio is not available here")
            self._ca, self._cf = ctypes.CDLL(path), ctypes.CDLL(cf)
            self._ca.AudioObjectHasProperty.restype = ctypes.c_bool   # Boolean, not OSStatus
            self._cf.CFStringGetCString.restype = ctypes.c_bool
            self._cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                    ctypes.c_long, ctypes.c_uint32]
            self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        return self._ca, self._cf

    def _check(self, status, what):
        if status != 0:
            raise CodecError(f"CoreAudio {what} failed (OSStatus {status})")

    def _device(self):
        ca, cf = self._libs()
        addr = _Address(self.DEVICES, self.GLOBAL, 0)
        size = ctypes.c_uint32(0)
        self._check(ca.AudioObjectGetPropertyDataSize(self.SYSTEM, ctypes.byref(addr), 0, None,
                                                      ctypes.byref(size)), "device list")
        ids = (ctypes.c_uint32 * (size.value // 4))()
        self._check(ca.AudioObjectGetPropertyData(self.SYSTEM, ctypes.byref(addr), 0, None,
                                                  ctypes.byref(size), ids), "device list")
        for dev in ids:
            naddr = _Address(self.NAME, self.GLOBAL, 0)
            ref = ctypes.c_void_p()
            nsize = ctypes.c_uint32(ctypes.sizeof(ref))
            if ca.AudioObjectGetPropertyData(dev, ctypes.byref(naddr), 0, None,
                                             ctypes.byref(nsize), ctypes.byref(ref)) or not ref:
                continue
            buf = ctypes.create_string_buffer(256)
            ok = cf.CFStringGetCString(ref, buf, len(buf), self.UTF8)
            cf.CFRelease(ref)
            if ok and buf.value.decode() == self.name and self._elements(dev):
                return dev
        raise CodecError(f"no CoreAudio output device named {self.name!r} with a dB volume")

    def _elements(self, dev):
        ca, _ = self._libs()
        for els in ((0,), (1, 2)):
            if all(ca.AudioObjectHasProperty(dev, ctypes.byref(_Address(self.VOLUME_DB, self.OUTPUT, e)))
                   for e in els):
                return els
        return ()

    def _get(self):
        ca, _ = self._libs()
        dev = self._device()
        el = self._elements(dev)[0]
        v = ctypes.c_float()
        size = ctypes.c_uint32(ctypes.sizeof(v))
        self._check(ca.AudioObjectGetPropertyData(dev, ctypes.byref(_Address(self.VOLUME_DB, self.OUTPUT, el)),
                                                  0, None, ctypes.byref(size), ctypes.byref(v)), "volume read")
        return round(v.value, 2)

    def _set(self, db):
        ca, _ = self._libs()
        dev = self._device()
        v = ctypes.c_float(db)
        for el in self._elements(dev):
            self._check(ca.AudioObjectSetPropertyData(dev, ctypes.byref(_Address(self.VOLUME_DB, self.OUTPUT, el)),
                                                      0, None, ctypes.sizeof(v), ctypes.byref(v)), "volume set")

    async def get(self):
        return await asyncio.to_thread(self._get)

    async def set(self, db):
        await asyncio.to_thread(self._set, float(db))


def _db(text, what):
    try:
        v = float(text)
    except ValueError:
        raise ValueError(f"{what}: {text.strip()!r} is not a number (dB)") from None
    if not math.isfinite(v):
        raise ValueError(f"{what}: {text.strip()!r} is not a finite level")
    return v


def parse_levels(text, bands):
    """--winlink-codec-db -> {"default": dB, "<band>": dB, ...}.

    "-38" is the default alone; "default=-38,80m=-38,20 m=-50" is a table
    (band names as in `bands`, with or without the space). A table needs a
    default. Raises ValueError saying what is wrong."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty level")
    if "=" not in text:
        return {"default": _db(text, "level")}
    canon = {b.replace(" ", "").lower(): b for b in bands}
    levels = {}
    for part in text.split(","):
        key, eq, val = part.partition("=")
        key = key.strip().replace(" ", "").lower()
        if not eq or not key:
            raise ValueError(f"{part.strip()!r} is not BAND=DB")
        name = "default" if key == "default" else canon.get(key)
        if name is None:
            raise ValueError(f"unknown band {part.partition('=')[0].strip()!r} "
                             f"(bands: {', '.join(bands)})")
        if name in levels:
            raise ValueError(f"{name} given twice")
        levels[name] = _db(val, name)
    if "default" not in levels:
        raise ValueError("a per-band table needs default=DB for the other bands")
    return levels


USES = ("winlink", "voice")
LABEL = {"winlink": "Winlink", "voice": "voice"}
MAX_DB = 0.0                  # a codec's top: the level apps get between uses


def make(args, saver=None):
    """The codec profile for these server arguments (None: leave the codec alone)."""
    voice = getattr(args, "voice_codec_db", None)
    if args.winlink_codec_db is None and voice is None:
        return None
    if args.winlink_codec_mixer:
        mixer = (CoreAudioLevel if MACOS else AlsaLevel)(args.winlink_codec_mixer)
    elif args.sim:
        mixer = SimLevel()
    elif MACOS:
        mixer = CoreAudioLevel(args.audio_device)
    else:
        mixer = AlsaLevel(alsa_spec(args.audio_device))
    return CodecProfile(mixer, args.winlink_codec_db, voice=voice,
                        min_db=getattr(args, "codec_min_db", None),
                        max_db=getattr(args, "codec_max_db", None), saver=saver)


def _table(levels):
    if levels is None:
        return None
    if not isinstance(levels, dict):
        levels = {"default": levels}
    return {k: float(v) for k, v in levels.items()}


class CodecProfile:
    """The codec at a use's level (Winlink while armed, voice while the page
    keys), the codec's own level otherwise."""

    def __init__(self, mixer, levels, voice=None, min_db=None, saver=None, max_db=None):
        self.mixer = mixer
        self.tables = {u: t for u, t in (("winlink", _table(levels)), ("voice", _table(voice))) if t}
        self.stored = {u: dict(t) for u, t in self.tables.items()}   # as in the host file
        self.min_db = None if min_db is None else float(min_db)
        self.max_db = MAX_DB if max_db is None else min(MAX_DB, float(max_db))
        self.saver = saver           # saver(use, table) persists a table; raises on failure
        self.use = None              # the use holding the codec now
        self.saved = None            # the level to put back; None = nothing to restore
        self.applied = None          # (band, dB) set by enter(), for the disarm check
        self.current = None          # last level read or set (for the page)

    @property
    def levels(self):
        return self.tables.get("winlink", {})

    def has(self, use):
        return use in self.tables

    def level_for(self, band, use="winlink"):
        """dB for this band (None/unlisted: the default)."""
        t = self.tables[use]
        return t.get(band, t["default"])

    async def refresh(self):
        try:
            self.current = await self.mixer.get()
        except CodecError as e:
            log.warning("codec level not readable: %s", e)

    async def enter(self, band=None, use="winlink"):
        """Set this use's level for this band, remembering the old one.
        Raises CodecError (codec unchanged), also when another use holds it."""
        if self.saved is not None:
            if self.use == use:
                return
            raise CodecError(f"the codec is at the {LABEL[self.use]} level right now")
        if use not in self.tables:
            raise CodecError(f"no {LABEL[use]} level configured")
        db = self.level_for(band, use)
        before = await self.mixer.get()
        try:
            await self.mixer.set(db)
        except CodecError:
            try:
                await self.mixer.set(before)
            except CodecError:
                pass
            raise
        self.saved, self.current, self.applied, self.use = before, db, (band, db), use
        label = band or "band unknown"
        if band not in self.tables[use]:
            label += " (default)"
        log.info("codec: %s: %g dB for %s (was %g dB)", label, db, LABEL[use], before)

    async def leave(self, use=None):
        """Put back the level from before enter(). use: only if that use holds
        the codec. Returns an error text, or None."""
        if self.saved is None or (use is not None and use != self.use):
            return None
        before = self.saved
        band, db = self.applied or (None, None)
        try:
            now = await self.mixer.get()
        except CodecError as e:
            log.warning("codec: level not readable before restoring: %s", e)
        else:
            if db is not None and abs(now - db) > ADJUST_TOLERANCE_DB:
                log.info("codec: operator adjusted to %g dB on %s during session (profile %g dB)",
                         now, band or "unknown band", db)
        try:
            await self.mixer.set(before)
        except CodecError as e:
            # Keep what must be restored (review 2026-10-18, Sol): forgetting it
            # made the residual level the next session's "original". The use
            # still holds the codec, so no other use takes it at the wrong
            # baseline, and the next leave() retries.
            log.error("codec: could not restore %g dB (kept; will retry): %s", before, e)
            return str(e)
        self.saved, self.applied, self.use = None, None, None
        self.current = before
        log.info("codec: restored %g dB", before)
        return None

    # --- the page's level control

    def _key(self, use, band):
        """The table key a step or save for this band edits."""
        return band or "default"

    def snapshot(self, use, band):
        """What the page shows for this use on this band."""
        if use not in self.tables:
            return None
        k = self._key(use, band)
        db = self.level_for(band, use)
        stored = self.stored[use].get(k, self.stored[use]["default"])
        return {"use": use, "band": band, "db": db, "own": k in self.tables[use],
                "unsaved": abs(db - stored) > 1e-9 or (k in self.tables[use]) != (k in self.stored[use]),
                "live": self.use == use and (self.applied or (None,))[0] == band,
                # The band's saved (on-air tuned) level: the page warns above it.
                "saved_db": stored,
                "min_db": self.min_db, "max_db": self.max_db, "save": self.saver is not None}

    async def step(self, use, band, delta):
        """Move this use's level for this band by delta dB, within the floor
        and 0 dB. Applied to the codec now if that use holds it on this band.
        Returns the new level. Raises CodecError (nothing changed)."""
        if use not in self.tables:
            raise CodecError(f"no {LABEL[use]} level configured")
        old = self.level_for(band, use)
        new = min(self.max_db, old + float(delta))
        if self.min_db is not None:
            new = max(self.min_db, new)
        if new == old:
            return old
        if self.use == use and self.applied and self.applied[0] == band:
            await self.mixer.set(new)            # CodecError: table unchanged too
            self.applied, self.current = (band, new), new
        self.tables[use][self._key(use, band)] = new
        log.info("codec: %s %s set to %g dB from the page (was %g dB, not saved)",
                 LABEL[use], band or "default", new, old)
        return new

    async def save(self, use):
        """Persist this use's table through the save hook. Raises CodecError."""
        if self.saver is None:
            raise CodecError("saving is not enabled on this server (--codec-save)")
        table = dict(self.tables[use])
        try:
            await asyncio.to_thread(self.saver, use, table)
        except Exception as e:
            raise CodecError(str(e)) from None
        self.stored[use] = table
        log.info("codec: %s levels saved: %s", LABEL[use], table)
