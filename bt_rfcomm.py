"""Bluetooth Classic serial (RFCOMM) on macOS, through IOBluetooth (PyObjC).

Why not /dev/cu.UV-PRO: on macOS 27 (mac-shack, 2026-10-14) opening that device
never brought the Bluetooth link up, even with a blocking open and a write;
System Information kept showing the radio "Not Connected" and nothing was
received. Opening the RFCOMM channel through IOBluetooth connects at once.

IOBluetooth delivers channel events on the run loop of the thread that opened
the channel, so each link runs its own thread: it opens the channel, spins
that thread's run loop, hands received bytes to `on_data`, and performs the
writes queued by `write()`. `on_close` fires once when the link ends.

    link = RfcommLink("38-d2-00-00-ee-18", channel="auto", on_data=..., on_close=...)
    link.open(timeout=20)     # blocks until open or raises OSError
    link.write(b"...")
    link.close()
"""

import queue
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
    radio refused (2026-10-14)."""
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
    def __init__(self, address, channel="auto", on_data=None, on_close=None):
        self.address = address.replace(":", "-").lower()
        self.channel = channel
        self.on_data = on_data or (lambda b: None)
        self.on_close = on_close or (lambda reason: None)
        self._writes = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._opened = threading.Event()
        self._open_error = None
        self._closed_once = threading.Lock()
        self._closed = False
        self._thread = None
        self.mtu = None

    # -- public --

    def open(self, timeout=20.0):
        self._thread = threading.Thread(target=self._run, name=f"rfcomm-{self.address}",
                                        daemon=True)
        self._thread.start()
        if not self._opened.wait(timeout):
            self._stop.set()
            raise OSError(f"Bluetooth open of {self.address} timed out")
        if self._open_error:
            raise OSError(self._open_error)

    def write(self, data: bytes):
        if self._closed:
            raise OSError("Bluetooth link closed")
        try:
            self._writes.put_nowait(bytes(data))
        except queue.Full:
            raise OSError("Bluetooth write queue full") from None

    def close(self):
        self._stop.set()

    # -- thread --

    def _finish(self, reason):
        with self._closed_once:
            if self._closed:
                return
            self._closed = True
        self.on_close(reason)

    def _run(self):
        try:
            self._run_inner()
        except Exception as e:      # noqa: BLE001 - report, never die silently
            if not self._opened.is_set():
                self._open_error = f"Bluetooth thread failed: {type(e).__name__}: {e}"
                self._opened.set()
            self._finish(f"Bluetooth thread failed: {e}")

    def _run_inner(self):
        try:
            import IOBluetooth
            from Foundation import NSDate, NSRunLoop
        except ImportError as e:
            self._open_error = f"PyObjC IOBluetooth not installed ({e})"
            self._opened.set()
            return
        dev = IOBluetooth.IOBluetoothDevice.deviceWithAddressString_(self.address)
        if dev is None:
            self._open_error = f"no paired Bluetooth device {self.address}"
            self._opened.set()
            return
        try:
            ch_id = find_spp_channel(self.address) if self.channel == "auto" else int(self.channel)
        except OSError as e:
            self._open_error = str(e)
            self._opened.set()
            return
        delegate = _delegate_class().alloc().init()
        delegate.link = self
        rc, chan = dev.openRFCOMMChannelSync_withChannelID_delegate_(None, ch_id, delegate)
        if rc != 0 or chan is None:
            self._open_error = (f"Bluetooth RFCOMM open of {self.address} channel {ch_id} failed "
                                f"(IOReturn {rc:#x}); is the radio on, in range, and not "
                                f"connected to another device?")
            self._opened.set()
            return
        self.mtu = chan.getMTU() or 127
        self.channel_id = ch_id
        self._opened.set()
        try:
            while not self._stop.is_set():
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
                while True:
                    try:
                        data = self._writes.get_nowait()
                    except queue.Empty:
                        break
                    for i in range(0, len(data), self.mtu):
                        part = data[i:i + self.mtu]
                        wrc = chan.writeSync_length_(part, len(part))
                        if wrc != 0:
                            self._stop.set()
                            self._finish(f"Bluetooth write failed (IOReturn {wrc:#x})")
                            break
                if not dev.isConnected():
                    self._finish("Bluetooth link dropped")
                    break
        finally:
            try:
                chan.closeChannel()
                dev.closeConnection()
            except Exception:      # noqa: BLE001 - best effort on the way out
                pass
            self._finish("closed")
