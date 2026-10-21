"""ax25_link.Link against itself over a channel that drops chosen frames.

Fast and deterministic: two Links in one event loop, a channel that can drop
frames by rule (first UA, every REJ, the first copy of each I frame, ...) or
at random with a fixed seed. Each case checks both byte streams arrive intact
and in order, and that both ends end up disconnected.

    .venv/bin/python tests/ax25_link_test.py
"""
import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ax25 import Frame  # noqa: E402
from ax25_link import CONNECTED, DISCONNECTED, Link  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'ok  ' if cond else 'FAIL'}  {name:64} {detail}")


class End:
    def __init__(self, name):
        self.name = name
        self.rx = bytearray()
        self.events = []
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()

    def on_connected(self, link, inbound):
        self.events.append("connected")
        self.connected.set()

    def on_data(self, link, data):
        self.rx += data

    def on_disconnected(self, link, reason):
        self.events.append(reason)
        self.closed.set()


class Channel:
    """Delivers frames between two links after a short delay, dropping by rule."""

    def __init__(self, drop=None, loss=0.0, seed=0, delay=0.005):
        self.drop = drop or (lambda f, n: False)
        self.loss, self.rng, self.delay = loss, random.Random(seed), delay
        self.n = 0
        self.log = []
        self.ends = {}

    def sender(self, me):
        def send(f: Frame):
            raw = f.encode()                  # through the real encoder/decoder
            self.n += 1
            dropped = self.drop(f, self.n) or (self.loss and self.rng.random() < self.loss)
            self.log.append((me, f.name(), dropped))
            if not dropped:
                other = self.ends[me]
                asyncio.get_running_loop().call_later(
                    self.delay, lambda: other.handle(Frame.decode(raw)) if other.state != "x" else None)
        return send


async def run_case(name, drop=None, loss=0.0, seed=0, a_data=b"", b_data=b"", timeout=60,
                   a_banner=b"", opts_a=None, opts_b=None, quiet=False):
    ch = Channel(drop, loss, seed)
    ea, eb = End("A"), End("B")
    opts = dict(frack=0.05, baud=10_000_000, n2=30, ack_delay=0.01, t3=5.0)
    a = Link("N9ABC-10", "W7TST-10", ch.sender("A"), ea, **{**opts, **(opts_a or {})})
    b_holder = {}

    def make_b(sabm):
        b = Link("W7TST-10", "N9ABC-10", ch.sender("B"), eb, **{**opts, **(opts_b or {})})
        b_holder["b"] = b
        ch.ends["A"] = b
        b.accept(sabm)
        if a_banner:
            b.send(a_banner)
        if b_data:
            b.send(b_data)

    class Listener:          # B before any link exists: answers the first SABM
        state = "listen"

        def handle(self, f):
            if "b" in b_holder:
                b_holder["b"].handle(f)
            elif f.kind == "U" and f.name() == "SABM":
                make_b(f)

    ch.ends["A"] = Listener()
    ch.ends["B"] = a
    a.connect()
    try:
        await asyncio.wait_for(ea.connected.wait(), timeout)
        if a_data:
            a.send(a_data)
        want_a = a_banner + b_data
        want_b = a_data
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if len(ea.rx) >= len(want_a) and len(eb.rx) >= len(want_b) and not a.outstanding() \
                    and ("b" in b_holder and not b_holder["b"].outstanding()):
                break
            await asyncio.sleep(0.02)
        a.disconnect()
        await asyncio.wait_for(ea.closed.wait(), timeout)
        await asyncio.sleep(0.3)
        b = b_holder.get("b")
        ok = bytes(ea.rx) == want_a and bytes(eb.rx) == want_b
        if not ok or not quiet:
            print(f"      {name}: A closed with {ea.events[-1:]}, B {eb.events[-1:]}")
        check(f"{name}: both streams intact", ok,
              f"A got {len(ea.rx)}/{len(want_a)}, B got {len(eb.rx)}/{len(want_b)}")
        check(f"{name}: both ends disconnected",
              a.state == DISCONNECTED and b is not None and b.state == DISCONNECTED,
              f"A {a.state} ({ea.events[-1:]}), B {b.state if b else None} ({eb.events[-1:]})")
    except asyncio.TimeoutError:
        check(f"{name}: finished", False,
              f"timed out; A {a.state}, rx {len(ea.rx)}; B rx {len(eb.rx)}; last frames {ch.log[-8:]}")


def first_of(kind_name, who):
    seen = set()

    def drop(f, n):
        key = (who, kind_name)
        if f.name().startswith(kind_name) and key not in seen and _sender(f) == who:
            seen.add(key)
            return True
        return False
    return drop


def _sender(f):
    return "A" if str(f.src) == "N9ABC-10" else "B"


def first_copy_of_every_i():
    seen = set()

    def drop(f, n):
        if f.kind == "I":
            k = (_sender(f), f.info)
            if k not in seen:
                seen.add(k)
                return True
        return False
    return drop


def every(kind_name, limit=3):
    count = {"n": 0}

    def drop(f, n):
        if f.name().startswith(kind_name) and count["n"] < limit:
            count["n"] += 1
            return True
        return False
    return drop


async def main():
    rng = random.Random(42)
    blob = lambda n: bytes(rng.randrange(256) for _ in range(n))   # noqa: E731
    banner = b"[WL2K-5.0-B2FWIHJM$]\rCMS via W7TST >\r"
    await run_case("clean", a_data=blob(2000), b_data=blob(2000), a_banner=banner)
    await run_case("first UA lost (acceptor already sent banner)", drop=first_of("UA", "B"),
                   a_banner=banner, a_data=blob(500))
    await run_case("first SABM lost", drop=first_of("SABM", "A"), a_data=blob(500), a_banner=banner)
    await run_case("REJs lost", drop=every("REJ"), loss=0.1, seed=3, a_data=blob(3000))
    await run_case("first copy of every I frame lost", drop=first_copy_of_every_i(),
                   a_data=blob(1500), b_data=blob(1500), a_banner=banner)
    await run_case("first RR lost each way", drop=every("RR", 2), a_data=blob(1000), b_data=blob(1000))
    await run_case("DISC's UA lost", drop=lambda f, n: f.name() == "UA" and _sender(f) == "B" and n > 3,
                   a_data=blob(300))
    ok = 0
    for seed in range(40):
        before = fails
        await run_case(f"random 25% loss, seed {seed}", loss=0.25, seed=seed,
                       a_data=blob(1200), b_data=blob(1200), a_banner=banner)
        ok += fails == before
    print(f"      random-loss sweep: {ok}/40 clean")


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)
