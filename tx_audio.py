"""The page's transmit audio: voice from the page's own PTT and the TX level control.

A mixin of server.Station (it uses the station's PTT owner, radio, codec and
notices). Split out of server.py after the 2026-10-18 review, which found the
transmit paths hard to follow in one class; behaviour is unchanged.

  - voice_key: the page's PTT. With the page's mic on: the voice codec level
    and, on a rig that has one, the SSB audio source switched to USB (IC-7300
    DATA OFF MOD, QMX SS), both set after the page owns PTT and before it
    keys, and put back after the unkey (_voice_restore, called from unkey).
  - codec_*: the TX level row (Winlink / Voice, step, save) and its broadcasts.

Apps keying over rigctl use Station.key() and never reach this code.
"""

import array
import asyncio
import json
import logging
import math
import sys
import time

import codec_level

log = logging.getLogger("server")

VOICE_PREP_S = 5.0          # page voice: codec level + audio source before keying
# Amateur bands (US) the page's PTT may key on: HF and 6 m as server.HAM_BANDS,
# plus 2 m to 23 cm for VHF/UHF rigs.
TX_BANDS = [(1.8e6, 2.0e6), (3.5e6, 4.0e6), (5.25e6, 5.45e6), (7.0e6, 7.3e6), (10.1e6, 10.15e6),
            (14.0e6, 14.35e6), (18.068e6, 18.168e6), (21.0e6, 21.45e6), (24.89e6, 24.99e6),
            (28.0e6, 29.7e6), (50.0e6, 54.0e6), (144e6, 148e6), (222e6, 225e6), (420e6, 450e6),
            (1240e6, 1300e6)]


def in_band(hz):
    return any(lo <= hz <= hi for lo, hi in TX_BANDS)


class TxAudio:
    async def voice_key(self, ws, on, mic=True):
        """The page's own PTT. With the page's mic on, before keying: the codec
        at the voice level (--voice-codec-db), and on a rig that has one, the
        SSB audio source switched to USB (IC-7300 DATA OFF MOD, QMX SS). Both go
        back after the unkey. With the mic off the radio is left as it is (its
        own mic). Apps keying over rigctl use key() and never change either.

        The page takes PTT ownership BEFORE changing anything, so an app's key
        during the preparation is refused instead of transmitting through it,
        and checks it still owns it before keying, since anyone may unkey
        (review 2026-10-18, Sol and Grok)."""
        if not on or getattr(ws, "ptt_latched", False):
            # Latched (F17): key() ignores it; never touch the radio's source or
            # the codec for keep-alives that will not key.
            return await self.key(ws, on)
        if self.ptt_owner is ws:              # the page's keep-alive while held
            return await self.key(ws, True)
        f = self.radio.state.get("freq")
        if f and not in_band(f):
            # mac-shack, 2026-10-18: page voice on 14.369 MHz keyed nothing (the
            # IC-7300 won't transmit outside the bands) and looked like a dead mic.
            self.notice(f"{f / 1e6:.3f} MHz is outside the amateur bands: the radio won't transmit there. "
                        "Tune into a band first.", "warn", ws)
            return
        if self.winlink_armed:
            # The session owns the radio between its frames too (as _hold_busy
            # says); a page key-down here went out on the session frequency at
            # the Winlink drive (review 2026-10-18, Grok).
            self.notice("A Winlink session is armed: the page's PTT waits until it ends.", "warn", ws)
            return
        codec, s, caps = self.codec, self.radio.state, self.radio.caps
        if not mic:
            # mac-shack, 2026-10-18: PTT with the page's mic off switched the
            # IC-7300 to USB audio and sent none: 0 W, front mic cut off too.
            log.info("voice: page PTT without the page's mic: the radio's own audio source")
            return await self.key(ws, True)
        voice_level = codec is not None and codec.has("voice")
        modes = ("USB", "LSB", "AM", "FM") if caps.get("data_off_mod") else ("USB", "LSB")
        usb_src = (callable(getattr(self.radio, "ssb_from_usb", None))
                   and not s.get("data") and s.get("mode") in modes)
        if (caps.get("tx_audio_needs_data") and s.get("mode") in caps.get("data_modes", [])
                and not s.get("data")):
            # G90: the Digirig's audio reaches the transmitter only in USB-D /
            # LSB-D; plain USB keyed at 0 W (linux-station, 2026-10-17).
            self.notice(f"Turn DATA on ({s.get('mode')}-D) first: in plain {s.get('mode')} audio from "
                        "this page doesn't reach the transmitter.", "warn", ws)
            return
        if not (voice_level or usb_src):
            return await self.key(ws, True)
        why = await self.can_key(ws)
        if why:
            self.notice(why, "warn", ws)
            return
        # Own PTT now (not keyed yet): can_key refuses everyone else from here.
        self.ptt_owner = ws
        self.ptt_seen = self.ptt_started = time.monotonic()
        self._voice_preparing = ws
        try:
            try:
                await asyncio.wait_for(self._voice_prepare(voice_level, usb_src), VOICE_PREP_S)
            except (codec_level.CodecError, ValueError, asyncio.TimeoutError) as e:
                why = str(e) or f"the radio did not get ready within {VOICE_PREP_S:g} s"
                log.warning("voice: not transmitting: %s", why)
                self.notice(f"Not transmitting: {why}.", "warn", ws)
                if self.ptt_owner is ws:
                    self.ptt_owner = None
                await self._voice_restore()
                return
        finally:
            self._voice_preparing = None
        if self.ptt_owner is not ws:
            # Unkeyed (by anyone) while preparing: nothing went on air; put back.
            log.info("voice: released while preparing; nothing keyed")
            await self._voice_restore()
            return
        self.ptt_seen = self.ptt_started = time.monotonic()
        self._voice_since, self.voice_bytes, self.voice_rx_bytes = time.monotonic(), 0, 0
        self._vm = {"po": [], "alc": [], "sq": 0.0, "n": 0, "peak": 0}
        asyncio.ensure_future(self._voice_meters(self._voice_since))
        await self.radio.set("tx", True)
        log.info("voice: page PTT keyed (%s%s%s)", s.get("mode"), " DATA" if s.get("data") else "",
                 f", codec {codec.current:g} dB" if voice_level else "")

    async def _voice_prepare(self, voice_level, usb_src):
        s = self.radio.state
        if voice_level:
            await self.codec.enter(self.band_of(s.get("freq")) if s.get("freq") else None, use="voice")
            self._codec_broadcast("voice")
        if usb_src:
            self._voice_usb_src = True        # set first: a timeout mid-switch still restores
            await self.radio.ssb_from_usb(True)
            self._voice_src_broadcast(True)

    async def _voice_restore(self):
        """After the page's voice unkeys: the codec level and the radio's
        DATA OFF MOD source back as they were."""
        if self.codec is not None and self.codec.use == "voice":
            err = await self.codec.leave("voice")
            if err:
                self.notice(f"Could not put the codec's level back after voice ({err}).", "warn")
            self._codec_broadcast("voice")
        if getattr(self, "_voice_usb_src", False):
            self._voice_usb_src = False
            await self.radio.ssb_from_usb(False)
            self._voice_src_broadcast(False)

    def codec_band(self, use):
        """The band a level step or save for this use applies to: the armed
        session's band for Winlink, else the radio's band."""
        c = self.codec
        if c is not None and c.use == use and c.applied:
            return c.applied[0]
        f = self.radio.state.get("freq")
        return self.band_of(f) if f else None

    def codec_snapshot(self, use):
        if self.codec is None or not self.codec.has(use):
            return None
        return {**self.codec.snapshot(use, self.codec_band(use)), "current": self.codec.current}

    def _codec_broadcast(self, use):
        snap = self.codec_snapshot(use)
        if snap:
            self._broadcast(json.dumps({"t": "codec", **snap}))

    def _voice_src_broadcast(self, on):
        """The page's badge: the radio's audio source is USB for this voice."""
        self._broadcast(json.dumps({"t": "voice", "usb_src": bool(on)}))

    async def codec_command(self, ws, m):
        use = m.get("use")
        if self.codec is None or not self.codec.has(use):
            self.notice("No transmit level is configured for that on this radio.", "warn", ws)
            return
        try:
            if m["t"] == "codec_step":
                await self.codec.step(use, self.codec_band(use), float(m.get("db", 0)))
            elif m["t"] == "codec_save":
                await self.codec.save(use)
                later = ("systemd has the new unit for the next start" if sys.platform != "darwin"
                         else "the next start picks it up after Stop/Start from the menu bar")
                self.notice(f"Saved the {codec_level.LABEL[use]} levels to the host file (uncommitted); "
                            f"in use now, and {later}.", "info", ws)
        except (codec_level.CodecError, ValueError) as e:
            log.warning("codec: %s from the page refused: %s", m["t"], e)
            self.notice(f"Transmit level not changed: {e}", "warn", ws)
        if m["t"] == "codec_get":
            snap = self.codec_snapshot(use)
            await ws.send_str(json.dumps({"t": "codec", **snap}))
        else:
            self._codec_broadcast(use)

    # --- what a page transmission did (mac-shack, 2026-10-18: "hard to tell")

    async def _voice_meters(self, since):
        """Sample the radio's PO and ALC while this page transmission lasts."""
        while getattr(self, "_voice_since", None) == since:
            st = self.radio.state
            if st.get("tx"):
                self._vm["po"].append(st.get("po") or 0)
                self._vm["alc"].append(st.get("alc") or 0)
            await asyncio.sleep(0.1)

    def voice_audio(self, pcm):
        """Level of the page's audio (16-bit mono) while its voice is keyed."""
        vm = getattr(self, "_vm", None)
        if vm is None or getattr(self, "_voice_since", None) is None or not self.radio.state.get("tx"):
            return
        a = array.array("h")
        a.frombytes(pcm[: len(pcm) // 2 * 2])
        for v in a:
            vm["sq"] += v * v
            vm["peak"] = max(vm["peak"], abs(v))
        vm["n"] += len(a)

    def voice_summary(self):
        """', PO peak 62 W avg 31 W, ALC peak 40% avg 12%, audio -18 dBFS RMS (peak -6)'."""
        vm = getattr(self, "_vm", None)
        if not vm:
            return ""
        out = []
        if vm["po"]:
            w = self.radio.caps.get("max_watts") or 100
            pct = [_po_pct(r) for r in vm["po"]]
            out.append(f"PO peak {max(pct) * w / 100:.0f} W avg {sum(pct) / len(pct) * w / 100:.0f} W")
            alc = [min(100.0, a / 120 * 100) for a in vm["alc"]]
            out.append(f"ALC peak {max(alc):.0f}% avg {sum(alc) / len(alc):.0f}%")
        if vm["n"]:
            rms = math.sqrt(vm["sq"] / vm["n"]) / 32768
            out.append(f"audio {_dbfs(rms)} dBFS RMS (peak {_dbfs(vm['peak'] / 32768)})")
        self._vm = None
        return (", " + ", ".join(out)) if out else ""


def _po_pct(raw):
    """The radio's PO meter (0..213 = 100 %, Icom's curve) in percent, as the page shows it."""
    return raw / 143 * 50 if raw <= 143 else 50 + (raw - 143) / 70 * 50


def _dbfs(x):
    return f"{20 * math.log10(x):.0f}" if x > 0 else "-inf"
