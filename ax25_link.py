"""AX.25 v2.0 connected mode (modulo 8), one link per remote station.

This is the data-link layer a Winlink RMS gateway expects: SABM/UA to connect,
numbered I frames acknowledged with RR/REJ, DISC/UA to end. The radio's KISS
TNC only moves frames; everything here runs in the hub.

Design notes:
- Go-back-N retransmission: on REJ, or when a poll is answered while frames
  are still unacknowledged, everything from V(A) is sent again.
- T1 (retry timer) scales with the bytes still in flight, because at 1200 baud
  a full window takes seconds of air time.
- N2 retries without progress ends the link ("retry count exceeded").
- Everything is bounded: the send queue, the window (k) and the frame size.

The link never touches the radio itself. It hands frames to `send_frame`,
which goes through the hub's transmit gate; a refusal ends the link.
"""

import asyncio
import logging

from ax25 import (Address, Frame, S_REJ, S_RNR, S_RR, U_DISC, U_DM, U_FRMR, U_SABM,
                  U_SABME, U_UA)
log = logging.getLogger("ax25")

DISCONNECTED, CONNECTING, CONNECTED, DISCONNECTING = "disconnected", "connecting", "connected", "disconnecting"

DEFAULTS = {
    "k": 4,            # window: unacknowledged I frames in flight
    "paclen": 128,     # bytes of data per I frame
    "n2": 10,          # retries before giving up
    "frack": 3.0,      # base T1, seconds
    "baud": 1200,      # on-air rate, for T1 scaling
    "t3": 180.0,       # idle link check
    "ack_delay": 0.6,  # hold an RR this long, to ack a burst once
    "max_queue": 256 * 1024,
}


class TxRefused(Exception):
    pass


class Link:
    def __init__(self, local: str, remote: str, send_frame, events, digis=(), **opts):
        """
        local/remote: 'N9ABC-10' style callsigns.
        send_frame(Frame): queue a frame for the radio; raises TxRefused if the
            hub's transmit gate says no.
        events: object with on_connected(link, inbound), on_data(link, bytes),
            on_disconnected(link, reason).
        """
        self.local = Address.from_text(local)
        self.remote = Address.from_text(remote)
        self.digis = [Address.from_text(d) for d in digis]
        self.send_frame = send_frame
        self.events = events
        self.o = {**DEFAULTS, **{k: v for k, v in opts.items() if v is not None}}
        self.state = DISCONNECTED
        self.inbound = False
        self._reset_vars()
        self.sendq = bytearray()
        self._t1 = self._t3 = self._ack = None
        self.rc = 0
        self.closed_reason = None
        self.stats = {"i_sent": 0, "i_resent": 0, "i_recv": 0, "bytes_in": 0, "bytes_out": 0}

    def _reset_vars(self):
        self.vs = self.vr = self.va = 0
        self.unacked = {}           # ns -> data
        self.peer_busy = False
        self.rej_sent = False
        self.recovery = False       # waiting for an F=1 answer to our poll

    # ------------------------------------------------------------ public

    @property
    def key(self):
        return (str(self.local), str(self.remote))

    def outstanding(self):
        """Frames not yet acknowledged plus frames' worth still queued (AGWPE 'Y')."""
        p = self.o["paclen"]
        return len(self.unacked) + (len(self.sendq) + p - 1) // p

    def connect(self):
        if self.state != DISCONNECTED:
            return
        self._reset_vars()
        self.rc = 0
        self.state = CONNECTING
        self._u(U_SABM, pf=True, command=True)
        self._start_t1()

    def accept(self, sabm: Frame):
        """Answer an inbound SABM with UA (caller decided we want it)."""
        self._reset_vars()
        self.inbound = True
        self.state = CONNECTED
        self._u(U_UA, pf=sabm.pf, command=False)
        self._start_t3()
        self.events.on_connected(self, inbound=True)

    def send(self, data: bytes):
        if self.state not in (CONNECTED, CONNECTING):
            raise ConnectionError("link not connected")
        if len(self.sendq) + len(data) > self.o["max_queue"]:
            raise BufferError("send queue full")
        self.sendq += data
        self.stats["bytes_out"] += len(data)
        if self.state == CONNECTED:
            self._pump()

    def disconnect(self, reason="local disconnect"):
        if self.state == DISCONNECTED:
            return
        if self.state == CONNECTING:
            self._finish(reason)
            return
        # Let queued data go first? AGWPE clients flush ('Y' == 0) before 'd',
        # so anything left here is abandoned on purpose.
        self.sendq.clear()
        self.state = DISCONNECTING
        self.rc = 0
        self._stop_t3()
        self._u(U_DISC, pf=True, command=True)
        self._start_t1()

    def abort(self, reason):
        """End without telling the peer (radio gone, transmit refused)."""
        self._finish(reason)

    # ----------------------------------------------------------- receive

    def handle(self, f: Frame):
        try:
            self._handle(f)
        except TxRefused as e:
            self._finish(f"transmit refused: {e}")

    def _handle(self, f: Frame):
        if f.kind == "U":
            self._handle_u(f)
            return
        if self.state == CONNECTING or self.state == DISCONNECTING:
            # A peer in connected state answering our SABM/DISC with I or S
            # frames: ignore; T1 retries the U frame.
            return
        if self.state != CONNECTED:
            return
        self._restart_t3()
        if not self._ack_nr(f.nr):
            log.warning("%s: N(R)=%d outside V(A)=%d..V(S)=%d, resetting link",
                        self.key, f.nr, self.va, self.vs)
            self._finish("link error (bad N(R))", send_dm=True)
            return
        if f.kind == "I":
            self._recv_i(f)
        else:
            self._recv_s(f)
        self._pump()

    def _handle_u(self, f):
        u = f.utype
        if u in (U_SABM, U_SABME):
            if u == U_SABME:
                self._u(U_DM, pf=f.pf, command=False)    # we only do modulo 8
                return
            if self.state == CONNECTING:
                # Both ends called at once: answer, and their UA will follow.
                self._u(U_UA, pf=f.pf, command=False)
                return
            if self.state == CONNECTED:
                # Usually our UA was lost and the peer is still calling. Its
                # counters restart at 0, so anything sent but not acknowledged
                # goes back to the front of the queue, in order, and is resent
                # under the new numbering (2026-10-14: dropping it here lost a
                # Winlink banner and deadlocked both ends).
                log.info("%s: peer reset the link", self.key)
                pending = b"".join(self.unacked[(self.va + i) & 7]
                                   for i in range((self.vs - self.va) & 7))
                self.sendq[:0] = pending
                self._stop_t1()
                self._cancel_ack()
                self._reset_vars()
                self._u(U_UA, pf=f.pf, command=False)
                self._restart_t3()
                self._pump()
                return
        elif u == U_UA:
            if self.state == CONNECTING:
                self._stop_t1()
                self.state = CONNECTED
                self.rc = 0
                self._start_t3()
                self.events.on_connected(self, inbound=False)
                self._pump()
            elif self.state == DISCONNECTING:
                self._finish("disconnected")
        elif u == U_DM:
            if self.state == CONNECTING:
                self._finish("busy (DM)")
            elif self.state in (CONNECTED, DISCONNECTING):
                self._finish("disconnected by peer (DM)")
        elif u == U_DISC:
            self._u(U_UA, pf=f.pf, command=False)
            if self.state != DISCONNECTED:
                self._finish("disconnected by peer")
        elif u == U_FRMR:
            self._finish("frame reject (FRMR) from peer")

    def _recv_i(self, f):
        if f.ns == self.vr:
            self.vr = (self.vr + 1) & 7
            self.rej_sent = False
            self.stats["i_recv"] += 1
            self.stats["bytes_in"] += len(f.info)
            if f.info:
                self.events.on_data(self, f.info)
            if f.pf:
                self._s(S_RR, pf=True, command=False)
            else:
                self._schedule_ack()
        else:
            if not self.rej_sent:
                self.rej_sent = True
                self._cancel_ack()
                self._s(S_REJ, pf=f.pf, command=False)
            elif f.pf:
                self._s(S_RR, pf=True, command=False)

    def _recv_s(self, f):
        self.peer_busy = f.stype == S_RNR
        if f.command and f.pf:
            self._cancel_ack()
            self._s(S_RR, pf=True, command=False)
        resend = f.stype == S_REJ or (self.recovery and not f.command and f.pf)
        if not f.command and f.pf and self.recovery:
            self.recovery = False
            self.rc = 0
            if not self.unacked:
                self._stop_t1()
        if resend and self.unacked:
            self._go_back()

    # --------------------------------------------------------------- send

    def _ack_nr(self, nr):
        """Acknowledge everything below N(R). False if N(R) is out of range."""
        n_out = (self.vs - self.va) & 7
        if ((nr - self.va) & 7) > n_out:
            return False
        while self.va != nr:
            self.unacked.pop(self.va, None)
            self.va = (self.va + 1) & 7
            self.rc = 0
        if self.unacked:
            if not self.recovery:
                self._start_t1()
        elif not self.recovery:
            self._stop_t1()
        return True

    def _go_back(self):
        """Resend every unacknowledged frame from V(A)."""
        ns = self.va
        while ns != self.vs:
            data = self.unacked[ns]
            self._i(ns, data)
            self.stats["i_resent"] += 1
            ns = (ns + 1) & 7
        self._start_t1()

    def _pump(self):
        if self.state != CONNECTED or self.peer_busy or self.recovery:
            return
        p = self.o["paclen"]
        sent = False
        while self.sendq and len(self.unacked) < self.o["k"]:
            chunk = bytes(self.sendq[:p])
            del self.sendq[:p]
            ns = self.vs
            self.unacked[ns] = chunk
            self.vs = (self.vs + 1) & 7
            self._i(ns, chunk)
            self.stats["i_sent"] += 1
            sent = True
        if sent:
            self._start_t1()

    def _i(self, ns, data):
        self._cancel_ack()          # N(R) rides on the I frame
        self.send_frame(Frame(self.remote, self.local, self.digis, "I", ns=ns, nr=self.vr,
                              pid=0xF0, info=data, command=True))

    def _s(self, stype, pf=False, command=False):
        self.send_frame(Frame(self.remote, self.local, self.digis, "S", nr=self.vr, pf=pf,
                              stype=stype, command=command))

    def _u(self, utype, pf=False, command=True):
        self.send_frame(Frame(self.remote, self.local, self.digis, "U", utype=utype, pf=pf,
                              command=command))

    # ------------------------------------------------------------- timers

    def _t1_seconds(self):
        in_flight = sum(len(d) + 20 for d in self.unacked.values()) or 20
        air = in_flight * 8 / self.o["baud"]
        hops = 2 * len(self.digis) + 1
        return self.o["frack"] * hops + 2 * air

    def _start_t1(self):
        self._stop_t1()
        self._t1 = asyncio.get_running_loop().call_later(self._t1_seconds(), self._on_t1)

    def _stop_t1(self):
        if self._t1:
            self._t1.cancel()
            self._t1 = None

    def _start_t3(self):
        self._stop_t3()
        self._t3 = asyncio.get_running_loop().call_later(self.o["t3"], self._on_t3)

    def _restart_t3(self):
        if self.state == CONNECTED:
            self._start_t3()

    def _stop_t3(self):
        if self._t3:
            self._t3.cancel()
            self._t3 = None

    def _schedule_ack(self):
        if self._ack is None:
            self._ack = asyncio.get_running_loop().call_later(self.o["ack_delay"], self._on_ack)

    def _cancel_ack(self):
        if self._ack:
            self._ack.cancel()
            self._ack = None

    def _on_ack(self):
        self._ack = None
        if self.state == CONNECTED:
            self._guard(lambda: self._s(S_RR, pf=False, command=False))

    def _on_t1(self):
        self._t1 = None
        self.rc += 1
        if self.rc > self.o["n2"]:
            if self.state == CONNECTING:
                self._finish("no answer (retry count exceeded)")
            elif self.state == DISCONNECTING:
                self._finish("disconnected (no UA)")
            else:
                self._finish("retry count exceeded", send_dm=True)
            return
        if self.state == CONNECTING:
            self._guard(lambda: self._u(U_SABM, pf=True, command=True))
        elif self.state == DISCONNECTING:
            self._guard(lambda: self._u(U_DISC, pf=True, command=True))
        elif self.state == CONNECTED:
            # Ask where the peer is (RR, P=1); its F=1 answer drives go-back-N.
            self.recovery = True
            self._guard(lambda: self._s(S_RR, pf=True, command=True))
        if self.state != DISCONNECTED:
            self._start_t1()

    def _on_t3(self):
        self._t3 = None
        if self.state == CONNECTED and not self._t1:
            self.recovery = True
            self.rc = 0
            self._guard(lambda: self._s(S_RR, pf=True, command=True))
            self._start_t1()

    def _guard(self, fn):
        try:
            fn()
        except TxRefused as e:
            self._finish(f"transmit refused: {e}")

    # -------------------------------------------------------------- finish

    def _finish(self, reason, send_dm=False):
        if self.state == DISCONNECTED and self.closed_reason:
            return
        if send_dm:
            try:
                self._u(U_DM, pf=False, command=False)
            except TxRefused:
                pass
        self._stop_t1()
        self._stop_t3()
        self._cancel_ack()
        self.state = DISCONNECTED
        self.closed_reason = reason
        self.sendq.clear()
        self.unacked.clear()
        log.info("%s: link closed: %s", self.key, reason)
        self.events.on_disconnected(self, reason)
