"""mesh_bridge.py gives the node this host's time, without a node or the meshtastic package.

  - at connect the node is given the host's time, and again every
    CLOCK_EVERY_S; a clock that sets says nothing in the log
  - a host that says its own clock is not synchronised (Linux's adjtimex
    TIME_ERROR: a Pi that booted without a network) sets nothing, says so
    once, looks again every CLOCK_RETRY_S, and sets the node's clock once its
    own is right, saying that once too
  - a write that fails is said once and tried again; the loop goes on
  - a meshtastic package too old to set the time: said once, nothing sent
  - the loop ends when the link is lost
  - host_clock_unsynced: only TIME_ERROR counts; no answer (a Mac, a failed
    call) and every other state do not. adjtimex() itself answers None off
    Linux and a state from 0 to 5 on it, and changes nothing

    python3 tests/mesh_bridge_clock_test.py
"""
import logging
import os
import sys
import threading
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from testkit import wait_for  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"),
          file=sys.__stderr__)


import mesh_bridge as mb  # noqa: E402  (it redirects stdout to stderr)

said = []


class Said(logging.Handler):
    def emit(self, record):
        said.append(record.getMessage())


mb.log.addHandler(Said())
mb.log.setLevel(logging.DEBUG)
mb.log.propagate = False


class Node:
    """The local node: records each time it is given; `fail` makes the write raise."""

    def __init__(self):
        self.times = []
        self.fail = False

    def setTime(self, t=0):
        if self.fail:
            raise OSError("write failed")
        self.times.append((t, time.time()))


def bridge(node):
    return mb.Bridge(SimpleNamespace(myInfo=SimpleNamespace(my_node_num=1), localNode=node), {})


def run(b):
    t = threading.Thread(target=b.clock_loop, daemon=True)
    t.start()
    return t


def stop(b, t):
    b.lost.set()
    t.join(5)
    return not t.is_alive()


real_adjtimex = mb.adjtimex
state = {"adjtimex": None}                 # what the host's kernel says; None: nothing to ask
mb.adjtimex = lambda: state["adjtimex"]
mb.CLOCK_EVERY_S = mb.CLOCK_RETRY_S = 0.05

# --- a host with a good clock: set at connect, and again every round
node = Node()
b = bridge(node)
t = run(b)
check("the node is given the time at connect", wait_for(lambda: len(node.times) >= 1), True)
check("  and again every CLOCK_EVERY_S", wait_for(lambda: len(node.times) >= 3), True)
check("  the host's time, in whole seconds", all(isinstance(g, int) and abs(g - at) < 2 for g, at in node.times), True)
check("  with nothing said in the log", said, [])
check("the loop ends when the link is lost", stop(b, t), True)
n = len(node.times)
time.sleep(0.2)                            # a quiet window: nothing more is sent after the link is gone
check("  and sends nothing after", len(node.times), n)

# --- the host's own clock is not synchronised: nothing sent, said once, then set
said.clear()
state["adjtimex"] = mb.TIME_ERROR
node = Node()
b = bridge(node)
t = run(b)
check("host clock unsynchronised: said", wait_for(lambda: len(said) >= 1), True)
time.sleep(0.3)                            # several rounds of CLOCK_RETRY_S
check("  nothing is sent to the node", node.times, [])
check("  said once, not every round", len(said), 1)
check("  the line says why", "not synchronised" in said[0], True)
state["adjtimex"] = 0                      # the host's clock is set now (NTP came up)
check("  the node's clock is set once the host's is right", wait_for(lambda: len(node.times) >= 1), True)
check("  and that is said once", wait_for(lambda: said[1:] == ["node clock set"]), True)
wait_for(lambda: len(node.times) >= 3)
check("  with nothing more said after", said[1:], ["node clock set"])
stop(b, t)

# --- a write that fails: said once, tried again, the loop goes on
said.clear()
node = Node()
node.fail = True
b = bridge(node)
t = run(b)
check("a failed write is said", wait_for(lambda: len(said) >= 1), True)
time.sleep(0.3)
check("  once", len(said), 1)
check("  with the error", "OSError: write failed" in said[0], True)
check("  the loop is still running", t.is_alive(), True)
node.fail = False
check("  and the next try gets through", wait_for(lambda: len(node.times) >= 1), True)
stop(b, t)

# --- a meshtastic package without setTime
said.clear()
b = bridge(SimpleNamespace())
t = run(b)
t.join(5)
check("a package too old to set the time: the loop returns", t.is_alive(), False)
check("  having said so once", len(said) == 1 and "cannot set it" in said[0], True)

# --- which hosts count as unsynchronised
for st, want in ((None, False), (0, False), (1, False), (4, False), (mb.TIME_ERROR, True)):
    state["adjtimex"] = st
    check(f"host_clock_unsynced when adjtimex says {st}", mb.host_clock_unsynced(), want)

# --- the real adjtimex(): None off Linux, a clock state on it; it must not change the clock
got = real_adjtimex()
if sys.platform.startswith("linux"):
    check("adjtimex() on Linux answers a clock state", got in (0, 1, 2, 3, 4, 5), True)
else:
    check("adjtimex() off Linux has nothing to ask", got, None)
before = time.time()
real_adjtimex()
check("  and asking does not move the clock", abs(time.time() - before) < 1, True)

print("\nPASS" if not fails else f"\n{fails} FAILED", file=sys.__stderr__)
sys.exit(1 if fails else 0)
