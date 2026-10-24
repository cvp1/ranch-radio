"""OpenGD77 add-on: a job never shares the port with the watch's last call.

Cancelling the watch stops the waiting, not the blocking radio call it had
started in a thread. If that call was open(), it used to finish after the
job had taken the port, reopen or drain it, and eat the job's replies
(2026-10-23, found by de-flaking opengd77_control_test: a 19-byte reply
vanished into the drain). The job must wait for that call to return.

In-process and deterministic: a stub radio whose open() takes 1 s, a job
started 0.1 s into it, and a log of every port action in order.
"""
import asyncio
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from addons.opengd77 import OpenGD77Addon  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:60} got {got!r}" + ("" if ok else f" want {want!r}"))


class Host:
    def notice(self, msg, level="info", ws=None):
        pass

    def _broadcast(self, payload):
        pass

    def _send(self, ws, payload):
        pass


events = []
ev_lock = threading.Lock()


def log(what):
    with ev_lock:
        events.append(what)


class SlowRadio:
    """Just enough of OpenGD77 for the watch: open() is slow, and every
    port action is logged."""
    port = "stub"

    def __init__(self):
        self.lock = threading.Lock()
        self.fd = None

    def open(self):
        log("watch open: start")
        time.sleep(1.0)                    # a drain on a busy line
        self.fd = 1
        log("watch open: end")

    def close(self):
        if self.fd is not None:
            log("close")
        self.fd = None

    def info(self):
        return {"model": "stub", "revision": 0, "built": "", "screen": False}

    def layout(self):
        return None

    def vfos(self):
        return None

    def memory(self):
        raise OSError("not in this test")


async def main():
    a = OpenGD77Addon(Host(), {"port": "stub"})
    a.radio = SlowRadio()
    a.restart_wait = 0
    await a.start()
    await asyncio.sleep(0.1)               # the watch is inside open()

    def job():
        log("job: start")
        time.sleep(1.5)                    # outlasts the watch's open()
        log("job: end")
        return "done"

    ok = await a._job(None, "test job", job, kind="control")
    await a.stop()
    return ok


ok = asyncio.run(main())
check("the job ran", ok, True)
order = [e for e in events if e in ("watch open: end", "job: start", "job: end")]
check("the watch's open() returned before the job began", order[:2], ["watch open: end", "job: start"])
during = events[events.index("job: start") + 1:events.index("job: end")] if "job: start" in events else ["no job"]
check("no port action by anyone else during the job", during, [])
print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
