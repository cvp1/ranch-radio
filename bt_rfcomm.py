"""Bluetooth Classic serial (RFCOMM) on macOS, through IOBluetooth (PyObjC).

Why not /dev/cu.UV-PRO: on macOS 27 (mac-shack, 2026-10-14) opening that device
never brought the Bluetooth link up, even with a blocking open and a write;
System Information kept showing the radio "Not Connected" and nothing was
received. Opening the RFCOMM channel through IOBluetooth connects at once.

Why a child process: on macOS 27 (a Mac mini, 2026-11-04, a UV-50PRO) the
RFCOMM open only succeeds on a process's MAIN thread. The same
openRFCOMMChannelSync from a background thread with its own run loop fails
with kIOReturnError (0xE00002BC) every time, and IOBluetooth is now a bridge
over CoreBluetooth, which wants the main run loop. The hub's main thread runs
asyncio, so each link is a child process (this file, run with --child): its
main thread owns the channel and spins the run loop; received bytes come up
its stdout, bytes to send go down its stdin. The child exits when the link
ends, so a stuck link never takes the hub with it. A failed open costs more
than a retry: the radio's KISS service refuses every open after a dropped
link until it is power-cycled (the Linux station, 2026-11-04), so the hub dials once
per radio start and never probes.

    link = RfcommLink("38-d2-00-00-ee-18", channel="auto", on_data=..., on_close=...)
    link.open(timeout=20)     # blocks until open or raises OSError
    link.write(b"...")
    link.close()
"""

import os
import queue
import subprocess
import sys
import threading
import time

SPP_UUID16 = 0x1101

_DELEGATE_CLASS = None


def _delegate_class():
    """The IOBluetooth channel delegate, defined once per process.

    Objective-C class names are global and can't be redefined, and a plain
    "Delegate" already exists in Apple's frameworks (2026-10-14), so the name
    is unique and the class is built on first use only."""
    global _DELEGATE_CLASS
    if _DELEGATE_CLASS is None:
        from Foundation import NSObject

        class TncHubRfcommDelegate(NSObject):
            def rfcommChannelData_data_length_(self, ch, data, length):
                b = bytes(data.bytes()[:length]) if hasattr(data, "bytes") else bytes(data[:length])
                self.link.on_data(b)

            def rfcommChannelClosed_(self, ch):
                self.link._stop.set()
                self.link._finish("Bluetooth channel closed by the radio")

        _DELEGATE_CLASS = TncHubRfcommDelegate
    return _DELEGATE_CLASS


def available():
    try:
        import IOBluetooth  # noqa: F401
        return True
    except ImportError:
        return False


def find_spp_channel(address, timeout=15.0):
    """The RFCOMM channel of the device's Serial Port service (class 0x1101), by SDP.

    Waits for a fresh query to complete: reading dev.services() straight after
    performSDPQuery returned a stale list on mac-shack and picked a channel the
    radio refused (2026-10-14). The channel is not fixed: a UV-50PRO put its
    "SPP Dev" on 1 after one power-cycle and on 4 after another (2026-11-04),
    so it is looked up at every dial, never remembered."""
    import IOBluetooth
    from Foundation import NSDate, NSRunLoop
    dev = IOBluetooth.IOBluetoothDevice.deviceWithAddressString_(address)
    if dev is None:
        raise OSError(f"no Bluetooth device {address}")
    before = dev.getLastServicesUpdate()
    rc = dev.performSDPQuery_(None)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
        after = dev.getLastServicesUpdate()
        if after is not None and (before is None or after.timeIntervalSinceDate_(before) > 0):
            break
    else:
        if rc != 0 or not dev.services():
            raise OSError(f"{address}: SDP query did not complete (radio off or out of range?)")
    rec = dev.getServiceRecordForUUID_(IOBluetooth.IOBluetoothSDPUUID.uuid16_(SPP_UUID16))
    if rec is None:
        raise OSError(f"{address}: no Serial Port service in its SDP records")
    rc, ch = rec.getRFCOMMChannelID_(None)
    if rc != 0:
        raise OSError(f"{address}: Serial Port service has no RFCOMM channel")
    return int(ch)


class RfcommLink:
    """One RFCOMM link, carried by a child process (see the module docstring).

    open() starts the child and waits for its first line: "OPEN <channel> <mtu>"
    or "ERR <why>". After that the child's stdout is the radio's byte stream
    and its stdin the bytes to send."""

    def __init__(self, address, channel="auto", on_data=None, on_close=None):
        self.address = address.replace(":", "-").lower()
        self.channel = channel
        self.on_data = on_data or (lambda b: None)
        self.on_close = on_close or (lambda reason: None)
        self._proc = None
        self._thread = None
        self._closed_once = threading.Lock()
        self._closed = False
        self._stderr = b""
        self.mtu = None
        self.channel_id = None

    # -- public --

    def open(self, timeout=20.0):
        if not available():
            raise OSError("PyObjC IOBluetooth not installed")
        self._proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--child", self.address, str(self.channel)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        header = {}

        def read_header():
            header["line"] = self._proc.stdout.readline()
        t = threading.Thread(target=read_header, daemon=True)
        t.start()
        t.join(timeout)
        line = header.get("line")
        if line is None:
            self._kill()
            raise OSError(f"Bluetooth open of {self.address} timed out")
        words = line.decode("utf-8", "replace").strip().split(" ", 2)
        if not words or words[0] != "OPEN":
            why = words[1] if len(words) > 1 else ""
            if len(words) > 2:
                why += " " + words[2]
            self._kill()
            raise OSError(why or f"Bluetooth open of {self.address} failed (no answer from the link process)")
        self.channel_id = int(words[1])
        self.mtu = int(words[2]) or 127
        threading.Thread(target=self._drain_stderr, name=f"rfcomm-err-{self.address}", daemon=True).start()
        self._thread = threading.Thread(target=self._pump, name=f"rfcomm-{self.address}", daemon=True)
        self._thread.start()

    def write(self, data: bytes):
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise OSError("Bluetooth link closed")
        try:
            self._proc.stdin.write(bytes(data))
            self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise OSError(f"Bluetooth write failed: {e}") from e

    def close(self):
        p = self._proc
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()          # the child closes the channel and exits
        except OSError:
            pass
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            self._kill()
        self._finish("closed")

    # -- parent side --

    def _kill(self):
        p = self._proc
        if p is not None and p.poll() is None:
            p.kill()
            try:
                p.wait(2)
            except subprocess.TimeoutExpired:
                pass

    def _drain_stderr(self):
        try:
            for chunk in iter(lambda: self._proc.stderr.read(4096), b""):
                self._stderr = (self._stderr + chunk)[-2000:]
        except (OSError, ValueError):
            pass

    def _pump(self):
        p = self._proc
        try:
            while True:
                data = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(4096)
                if not data:
                    break
                self.on_data(data)
        except (OSError, ValueError):
            pass
        rc = p.wait()
        why = self._stderr.decode("utf-8", "replace").strip().splitlines()
        self._finish(why[-1] if why else f"Bluetooth link process exited ({rc})")

    def _finish(self, reason):
        with self._closed_once:
            if self._closed:
                return
            self._closed = True
        self.on_close(reason)


# ---------------------------------------------------------------- the child

def _child_main(address, channel):
    """Own the RFCOMM channel on this process's main thread. Protocol on stdout:
    one header line, then the radio's raw bytes; stdin is bytes to send; stderr
    is why it ended."""
    out = sys.stdout.buffer
    err = sys.stderr

    def say(line):
        out.write((line + "\n").encode()); out.flush()

    try:
        import IOBluetooth
        from Foundation import NSDate, NSRunLoop
    except ImportError as e:
        say(f"ERR PyObjC IOBluetooth not installed ({e})"); return 2
    dev = IOBluetooth.IOBluetoothDevice.deviceWithAddressString_(address)
    if dev is None:
        say(f"ERR no paired Bluetooth device {address}"); return 2
    try:
        ch_id = find_spp_channel(address) if channel == "auto" else int(channel)
    except OSError as e:
        say(f"ERR {e}"); return 2
    stop = threading.Event()

    class Link:            # what the delegate expects: on_data, _stop, _finish
        def on_data(self, b):
            out.write(b); out.flush()
        _stop = stop

        def _finish(self, why):
            print(why, file=err, flush=True)
    link = Link()
    delegate = _delegate_class().alloc().init()
    delegate.link = link
    rc, chan = dev.openRFCOMMChannelSync_withChannelID_delegate_(None, ch_id, delegate)
    if rc != 0 or chan is None:
        say(f"ERR Bluetooth RFCOMM open of {address} channel {ch_id} failed (IOReturn {rc:#x}); "
            "is the radio on, in range, not connected to another device, and power-cycled since its last dropped link?")
        return 2
    mtu = chan.getMTU() or 127
    say(f"OPEN {ch_id} {mtu}")

    writes = queue.Queue(maxsize=256)

    def read_stdin():
        fd = sys.stdin.fileno()
        while True:
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b""
            if not data:
                stop.set(); return
            writes.put(data)
    threading.Thread(target=read_stdin, daemon=True).start()
    try:
        while not stop.is_set():
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
            while True:
                try:
                    data = writes.get_nowait()
                except queue.Empty:
                    break
                for i in range(0, len(data), mtu):
                    part = data[i:i + mtu]
                    wrc = chan.writeSync_length_(part, len(part))
                    if wrc != 0:
                        print(f"Bluetooth write failed (IOReturn {wrc:#x})", file=err, flush=True)
                        stop.set(); break
            if not dev.isConnected():
                print("Bluetooth link dropped", file=err, flush=True)
                break
    finally:
        try:
            chan.closeChannel()
            dev.closeConnection()
        except Exception:      # noqa: BLE001 - best effort on the way out
            pass
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        sys.exit(_child_main(sys.argv[2], sys.argv[3]))
    sys.exit("usage: bt_rfcomm.py --child ADDRESS CHANNEL|auto  (run by RfcommLink)")
