"""Shared test plumbing: ports the OS hands out, and waiting on a condition
instead of sleeping. A fixed sleep before a check is a guess about how fast
the machine is; CI's two-CPU runners guess wrong (2026-10-23)."""
import socket
import time


def free_port():
    """A TCP port nothing on this machine is listening on, from the OS (bind
    to port 0), not a random pick from a range that two tests can share."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(pred, timeout=4.0, step=0.05):
    """True as soon as pred() holds, False if it still doesn't after timeout."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())
