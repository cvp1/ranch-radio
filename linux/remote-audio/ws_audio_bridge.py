#!/usr/bin/env python3
"""Bridge a Radio Remote server's /audio websocket to two local PipeWire
devices, so JTDX, JS8Call or WSJT-X on this machine can use a radio whose
server runs elsewhere (mac-shack's IC-7300 is on macOS: no PipeWire to tunnel).

The devices come from ic7300-mac-shack-pipes.conf (libpipewire-module-pipe-tunnel):
  RX: frames from the server -> rx FIFO -> source "IC-7300 RX (mac-shack)"
  TX: sink "IC-7300 TX (mac-shack)" -> tx FIFO -> frames to the server
The server plays TX audio only while the radio is transmitting (keyed by the
app over rigctl, under the server's time limit), and drops it otherwise.
Audio is 16 kHz mono s16le both ways, the server's native format.

    ws_audio_bridge.py --url wss://mac-shack:7300/audio \\
        --rx-fifo /run/user/1000/ic7300-mac-shack-rx.fifo \\
        --tx-fifo /run/user/1000/ic7300-mac-shack-tx.fifo
"""
import argparse
import asyncio
import errno
import logging
import os
import ssl

import aiohttp

log = logging.getLogger("bridge")
CHUNK = 640                  # 20 ms at 16 kHz s16le mono, as the server sends


def open_fifo(path, flags):
    if not os.path.exists(path):
        os.mkfifo(path)
    return os.open(path, flags | os.O_NONBLOCK)


class Bridge:
    def __init__(self, args):
        self.args = args
        self.ws = None
        self.rx_fd = None
        self.tx_fd = open_fifo(args.tx_fifo, os.O_RDONLY)
        self.tx_buf = b""

    def rx_write(self, data):
        """Hand server audio to the RX source. Never block: if the source
        isn't reading (no app open) or is behind, drop the chunk."""
        if self.rx_fd is None:
            try:
                self.rx_fd = open_fifo(self.args.rx_fifo, os.O_WRONLY)
            except OSError as e:
                if e.errno == errno.ENXIO:     # PipeWire isn't reading it yet
                    return
                raise
        try:
            os.write(self.rx_fd, data)
        except BlockingIOError:
            pass
        except BrokenPipeError:
            os.close(self.rx_fd)
            self.rx_fd = None

    def tx_readable(self):
        """Always drain the TX sink so PipeWire never stalls; forward non-silent
        audio while connected (the server ignores it unless transmitting)."""
        try:
            data = os.read(self.tx_fd, 65536)
        except BlockingIOError:
            return
        if not data:                           # writer closed: reopen
            asyncio.get_running_loop().remove_reader(self.tx_fd)
            os.close(self.tx_fd)
            self.tx_fd = open_fifo(self.args.tx_fifo, os.O_RDONLY)
            asyncio.get_running_loop().add_reader(self.tx_fd, self.tx_readable)
            return
        self.tx_buf += data
        while len(self.tx_buf) >= CHUNK:
            chunk, self.tx_buf = self.tx_buf[:CHUNK], self.tx_buf[CHUNK:]
            if self.ws is not None and not self.ws.closed and any(chunk):
                asyncio.ensure_future(self.ws.send_bytes(chunk))

    async def run(self):
        loop = asyncio.get_running_loop()
        loop.add_reader(self.tx_fd, self.tx_readable)
        ctx = ssl.create_default_context()
        if self.args.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        url = self.args.url + (f"?token={self.args.token}" if self.args.token else "")
        while True:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(url, ssl=ctx, heartbeat=10) as ws:
                        log.info("connected to %s", self.args.url)
                        self.ws = ws
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.BINARY:
                                self.rx_write(msg.data)
                log.warning("server closed the audio socket")
            except (TimeoutError, aiohttp.ClientError, OSError) as e:
                log.warning("audio socket: %s", e)
            finally:
                self.ws = None
            await asyncio.sleep(5)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", required=True, help="the server's /audio websocket")
    p.add_argument("--rx-fifo", required=True)
    p.add_argument("--tx-fifo", required=True)
    p.add_argument("--token", default=os.environ.get("IC7300_TOKEN"))
    p.add_argument("--insecure", action="store_true",
                   help="skip TLS verification (the server's self-signed cert)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(Bridge(args).run())


if __name__ == "__main__":
    main()
