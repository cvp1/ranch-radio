"""Keep a launchd console log from growing without end.

On a Mac, launchd points a service's stdout and stderr at one file
(render._macos: StandardOutPath = StandardErrorPath = server.log) and never
rotates it. Everything lands there: this process's log lines, tracebacks, and
what its children write (rigctld, ardop, Pat inherit the descriptors). A radio
server that runs for months fills it (server.log past 200 MB on a long-running
host, review plan PR 1, 2026-11-21).

So the process caps the file itself, the way logrotate's copytruncate does:
when the file passes `max_bytes`, copy it to <file>.1 (older copies shift to
.2 ... .<keep>), then truncate it to zero. launchd opened it O_APPEND, so every
writer, children included, carries on at the new end; no descriptor is
reopened. Lines written between the copy and the truncate are lost: at most a
few, once per rotation.

Only a regular file opened for append is touched. On Linux stderr is the
journal (a socket), and a file opened without O_APPEND would leave a hole at
the old offset after truncation; both are left alone, reported once.

    logcap.start(loop)      # from a server's startup: checks now, then every 5 min
"""
import asyncio
import fcntl
import logging
import os
import shutil
import stat

log = logging.getLogger("logcap")

MAX_BYTES = 20 * 1024 * 1024
KEEP = 3
EVERY_S = 300


def _target(fd):
    """The path fd writes to, when it is a regular file opened O_APPEND; else (None, why)."""
    try:
        st = os.fstat(fd)
    except OSError as e:
        return None, f"fd {fd}: {e}"
    if not stat.S_ISREG(st.st_mode):
        return None, "not a file (the journal, a pipe or a terminal)"
    if not fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_APPEND:
        return None, "not opened for append; truncating would leave a hole"
    path = None
    if hasattr(fcntl, "F_GETPATH"):                        # macOS
        try:
            path = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0").decode()
        except OSError:
            path = None
    else:                                                   # Linux
        try:
            path = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            path = None
    if not path or not os.path.exists(path) or os.stat(path).st_ino != st.st_ino:
        return None, "its path is gone (moved or deleted)"
    return path, ""


def rotate(path, keep=KEEP):
    """<path> -> <path>.1, shifting older copies up to .<keep>; then truncate <path>."""
    for i in range(keep - 1, 0, -1):
        older = f"{path}.{i}"
        if os.path.exists(older):
            os.replace(older, f"{path}.{i + 1}")
    shutil.copyfile(path, f"{path}.1")
    os.truncate(path, 0)


def check(fds=(1, 2), max_bytes=MAX_BYTES, keep=KEEP, _said=set()):
    """Rotate each distinct file behind fds that is over max_bytes. Returns
    the paths rotated. A file both fds share is rotated once."""
    done = []
    seen = set()
    for fd in fds:
        path, why = _target(fd)
        if not path:
            if why and (fd, why) not in _said:
                _said.add((fd, why))
                log.debug("console log on fd %s not capped: %s", fd, why)
            continue
        if path in seen:
            continue
        seen.add(path)
        try:
            if os.path.getsize(path) > max_bytes:
                rotate(path, keep)
                done.append(path)
                log.info("console log %s passed %d MB: rotated to %s.1 (keeping %d)",
                         path, max_bytes // (1024 * 1024), path, keep)
        except OSError as e:
            log.warning("console log %s: could not rotate (%s)", path, e)
    return done


def _limits():
    mb = os.environ.get("RANCH_RADIO_LOG_MAX_MB")
    try:
        return (int(float(mb) * 1024 * 1024) if mb else MAX_BYTES), int(os.environ.get("RANCH_RADIO_LOG_KEEP", KEEP))
    except ValueError:
        return MAX_BYTES, KEEP


async def _loop(every_s):
    max_bytes, keep = _limits()
    while True:
        try:
            check(max_bytes=max_bytes, keep=keep)
        except Exception:                                   # noqa: BLE001 - never take the server down
            log.exception("console log check failed")
        await asyncio.sleep(every_s)


def start(every_s=EVERY_S):
    """Check now and every every_s seconds, on the running loop. Returns the task."""
    return asyncio.ensure_future(_loop(every_s))
