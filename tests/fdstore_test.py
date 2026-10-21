"""tnc_hub's systemd fd-store plumbing (keep the radio's link across restarts):
sd_notify hands a socket to the notify socket under its name, stored_socket
adopts the one named "radio" from LISTEN_FDS and closes the rest, and nothing
happens outside systemd. The real Bluetooth adoption is checked on linux-station.

    python3 tests/fdstore_test.py
"""
import os
import socket
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tnc_hub  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


# --- outside systemd: no notify socket, no handed fds
os.environ.pop("NOTIFY_SOCKET", None)
check("no NOTIFY_SOCKET: nothing sent", tnc_hub.sd_notify("FDSTORE=1"), False)
check("no LISTEN_FDS: nothing adopted", tnc_hub.stored_socket(), None)

# --- FDSTORE: the fd reaches systemd with its name
d = tempfile.mkdtemp()
path = os.path.join(d, "notify")
sysd = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
sysd.settimeout(3)                     # a lost message fails the test, never hangs it
sysd.bind(path)
os.environ["NOTIFY_SOCKET"] = path
a, b = socket.socketpair()
check("FDSTORE sent", tnc_hub.sd_notify("FDSTORE=1\nFDNAME=radio", [a.fileno()]), True)
msg, fds, _, _ = socket.recv_fds(sysd, 1024, 4)
check("  message", msg, b"FDSTORE=1\nFDNAME=radio")
stored = socket.socket(fileno=fds[0])
b.send(b"still here")
check("  the stored copy is the same connection", stored.recv(64), b"still here")
a.close()
b.send(b"after the hub's copy closed")
check("  closing the hub's copy keeps the connection", stored.recv(64), b"after the hub's copy closed")
check("FDSTOREREMOVE sent", tnc_hub.sd_notify("FDSTOREREMOVE=1\nFDNAME=radio"), True)
check("  message", socket.recv_fds(sysd, 1024, 4)[0], b"FDSTOREREMOVE=1\nFDNAME=radio")

# --- the next start: LISTEN_FDS hands it back as fd 3 (with another, closed)
other_r, other_w = os.pipe()
os.dup2(stored.fileno(), 3)
os.dup2(other_r, 4)
os.environ.update(LISTEN_PID=str(os.getpid()), LISTEN_FDS="2", LISTEN_FDNAMES="radio:other")
got = tnc_hub.stored_socket()
check("adopted the fd named radio", got is not None and got.fileno(), 3)
b.send(b"hello new process")
check("  and it is the live connection", got.recv(64), b"hello new process")
try:
    os.fstat(4)
    closed = False
except OSError:
    closed = True
check("  other handed fds are closed", closed, True)
check("  LISTEN_* cleared (not inherited)", [k for k in os.environ if k.startswith("LISTEN_")], [])
os.environ.update(LISTEN_PID="1", LISTEN_FDS="1", LISTEN_FDNAMES="radio")
check("LISTEN_PID for another process: ignored", tnc_hub.stored_socket(), None)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
