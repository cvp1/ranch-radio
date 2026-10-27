"""One lock for every writer of a host file (review 2026-10-21, G4).

The page's codec Save (levels.save), the page's module apply and the CLI's
add/remove all read-modify-write hosts/<host>.json. They used two lock files
and none, so they could lose each other's edits. Now one lock covers them:
a threading.Lock (two worker threads in one server) around flock on
a .host.lock beside the host file (the CLI and a server are different processes).

Callers in a server run inside asyncio.to_thread: holding this never blocks
the event loop, so the radio's safety loop keeps its cadence.
"""

import fcntl
import threading
from contextlib import contextmanager
from pathlib import Path

_local = threading.Lock()


@contextmanager
def locked(host_dir):
    """host_dir: the directory holding the host file (paths.find(...).parent)."""
    path = Path(host_dir) / ".host.lock"
    with _local, open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)    # released when the file closes
        yield
