"""Audio bridge between the IC-7300's USB sound card and browser clients.

Receive audio is captured and fanned out to every listening client as 16 kHz
mono signed 16-bit PCM in 20 ms chunks; transmit audio from the browser
microphone is played into the same card. Linux uses `arecord`/`aplay`; macOS
uses CoreAudio through the `sounddevice` package (ffmpeg's AVFoundation input
drops ~20% of samples). Capture only runs while someone is listening.
"""

import array
import asyncio
import logging
import math
import random
import sys
import threading

log = logging.getLogger("audio")

RATE = 16_000
CHUNK = RATE // 50 * 2   # 20 ms of s16le mono
MACOS = sys.platform == "darwin"
DEFAULT_DEVICE = "USB Audio CODEC" if MACOS else "plughw:CARD=CODEC,DEV=0"


def _pipewire(device, kind):
    """`pipewire:<capture-node>[,<playback-node>]` -> (alsa device, env prefix).

    Going through PipeWire instead of plughw leaves the sound card shared, so
    JTDX/WSJT-X can record the radio while the browser listens. The ALSA
    "pipewire" PCM picks its node from PIPEWIRE_NODE; capture and playback
    need different nodes (the card's source vs its sink), so each command
    gets its own. With no playback node, playback is refused rather than
    sent to whatever the default sink is.
    """
    if not device.startswith("pipewire:"):
        return device, []
    nodes = device[len("pipewire:"):].split(",")
    node = nodes[0] if kind == "capture" else (nodes[1] if len(nodes) > 1 else "")
    if not node:
        raise ValueError("no PipeWire playback node in --audio-device "
                         "(use pipewire:<source>,<sink>)")
    return "pipewire", ["env", f"PIPEWIRE_NODE={node}"]


def capture_cmd(device):
    dev, env = _pipewire(device, "capture")
    return env + ["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", str(RATE), "-c", "1",
                  "-t", "raw", "--buffer-time=100000"]


def playback_cmd(device):
    dev, env = _pipewire(device, "playback")
    return env + ["aplay", "-q", "-D", dev, "-f", "S16_LE", "-r", str(RATE), "-c", "1",
                  "-t", "raw", "--buffer-time=120000"]


def _sd_device(name, kind):
    import sounddevice as sd
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == name and d[key] > 0:
            return i
    raise RuntimeError(f"no CoreAudio {kind} device named {name!r}")


class _CoreAudioPlayer:
    """Callback output stream fed from a small buffer (drops backlog over 300 ms)."""

    def __init__(self, device):
        import sounddevice as sd
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.stream = sd.RawOutputStream(device=_sd_device(device, "output"), samplerate=RATE,
                                         channels=1, dtype="int16", blocksize=CHUNK // 2,
                                         callback=self._cb)
        self.stream.start()

    def _cb(self, outdata, frames, _time, _status):
        n = len(outdata)
        with self._lock:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
        outdata[:len(chunk)] = chunk
        outdata[len(chunk):] = bytes(n - len(chunk))

    def write(self, pcm):
        with self._lock:
            self._buf += pcm
            if len(self._buf) > RATE * 2 * 3 // 10:
                del self._buf[:-CHUNK * 5]

    def close(self):
        self.stream.stop()
        self.stream.close()


class AudioBridge:
    def __init__(self, device, sim=None):
        self.device = device
        self.sim = sim
        self.error = None
        self._listeners = set()
        self._capture = None
        self._player = None

    def subscribe(self):
        q = asyncio.Queue(maxsize=25)
        self._listeners.add(q)
        if self._capture is None or self._capture.done():
            self._capture = asyncio.create_task(self._capture_loop(), name="audio-capture")
        return q

    def unsubscribe(self, q):
        self._listeners.discard(q)

    def _fan_out(self, chunk):
        for q in self._listeners:
            if q.full():          # slow client: drop its oldest audio
                q.get_nowait()
            q.put_nowait(chunk)

    async def _capture_loop(self):
        if self.sim:
            await self._sim_loop()
            return
        if MACOS:
            await self._coreaudio_loop()
            return
        while self._listeners:
            proc = await asyncio.create_subprocess_exec(
                *capture_cmd(self.device), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                while self._listeners:
                    self._fan_out(await proc.stdout.readexactly(CHUNK))
                    self.error = None
            except asyncio.IncompleteReadError:
                err = (await proc.stderr.read()).decode(errors="replace").strip()
                self.error = err or "capture ended"
                log.warning("audio capture on %s failed: %s", self.device, self.error)
                await asyncio.sleep(3)
            finally:
                if proc.returncode is None:
                    proc.terminate()
                    await proc.wait()

    async def _coreaudio_loop(self):
        import sounddevice as sd
        loop = asyncio.get_running_loop()

        def cb(indata, _frames, _time, _status):
            loop.call_soon_threadsafe(self._fan_out, bytes(indata))

        while self._listeners:
            try:
                with sd.RawInputStream(device=_sd_device(self.device, "input"), samplerate=RATE,
                                       channels=1, dtype="int16", blocksize=CHUNK // 2, callback=cb):
                    self.error = None
                    while self._listeners:
                        await asyncio.sleep(0.25)
            except Exception as e:
                self.error = str(e)
                log.warning("audio capture on %s failed: %s", self.device, e)
                await asyncio.sleep(3)

    async def _sim_loop(self):
        """Band noise, a CW/data tone when a signal is in the passband, voice-ish hiss for SSB."""
        n = CHUNK // 2
        lp = bp1 = bp2 = 0.0
        phase = 0.0
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        while self._listeners:
            hint = self.sim.audio_hint
            noise, tone, voice = hint["noise"], hint["tone"], hint["voice"]
            out = array.array("h", bytes(CHUNK))
            for i in range(n):
                w = random.uniform(-1, 1)
                lp += 0.35 * (w - lp)
                bp1 += 0.12 * (w - bp1)
                bp2 += 0.5 * (bp1 - bp2)
                phase += 2 * math.pi * 650 / RATE
                s = lp * noise + math.sin(phase) * tone * 0.35 + (bp1 - bp2) * voice * 2.2
                out[i] = int(max(-1.0, min(1.0, s)) * 26000)
            phase %= 2 * math.pi
            self._fan_out(out.tobytes())
            next_t += 0.02
            await asyncio.sleep(max(0, next_t - loop.time()))

    async def play(self, pcm: bytes):
        """Send microphone audio to the radio's USB audio input."""
        if self.sim:
            return
        if MACOS:
            try:
                if self._player is None:
                    self._player = _CoreAudioPlayer(self.device)
                self._player.write(pcm)
            except Exception as e:
                log.warning("audio playback on %s failed: %s", self.device, e)
                self._player = None
            return
        if self._player is None or self._player.returncode is not None:
            try:
                cmd = playback_cmd(self.device)
            except ValueError as e:
                log.warning("audio playback refused: %s", e)
                return
            self._player = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            self._player.stdin.write(pcm)
            await self._player.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self._player = None

    async def stop(self):
        if self._capture:
            self._capture.cancel()
        if isinstance(self._player, _CoreAudioPlayer):
            self._player.close()
        elif self._player and self._player.returncode is None:
            self._player.terminate()
