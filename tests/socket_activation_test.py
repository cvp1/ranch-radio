"""F10b: the rigctl port stays open across a restart (systemd socket activation).

The test plays systemd: it owns a listening socket and hands it to server.py as
fd 3 with LISTEN_FDS=1 / LISTEN_PID=<pid>. A client that connects while no
server is running must be answered once the next server starts, the way JTDX's
single immediate retry lands in the restart gap on linux-station (2026-10-14 11:02).

    .venv/bin/python tests/socket_activation_test.py [rig]
"""
import os
import random
import signal
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RIG = sys.argv[1] if len(sys.argv) > 1 else "ic7300"
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:56} got {got!r}" + ("" if ok else f" want {want!r}"))


def start(lsock, rport):
    """server.py with the listening socket as fd 3, like systemd does it."""
    web = 18000 + random.randrange(2000)
    fd = lsock.fileno()
    proc = subprocess.Popen(
        ["sh", "-c", 'LISTEN_PID=$$ LISTEN_FDS=1 LISTEN_FDNAMES=rigctl exec "$0" "$@"',
         sys.executable, "server.py", "--sim", "--rig", RIG, "--no-audio",
         "--listen", str(web), "--host", "127.0.0.1",
         "--rigctl-host", "127.0.0.1", "--rigctl-port", str(rport)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        pass_fds=(3,) if fd == 3 else (fd,),
        preexec_fn=None if fd == 3 else (lambda: os.dup2(fd, 3)))
    return proc


def stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()[0]


def ask(sock, line, timeout):
    sock.settimeout(timeout)
    sock.sendall((line + "\n").encode())
    buf = b""
    try:
        while not buf.endswith(b"\n"):
            chunk = sock.recv(100)
            if not chunk:
                return "EOF"
            buf += chunk
    except socket.timeout:
        return "timeout"
    return buf.decode().strip()


def wait_reply(rport, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            c = socket.create_connection(("127.0.0.1", rport), timeout=0.5)
            r = ask(c, "f", 1.0)
            c.close()
            if r.isdigit():
                return r
        except OSError:
            pass
        time.sleep(0.1)
    return None


def main():
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(16)
    os.set_inheritable(lsock.fileno(), True)
    rport = lsock.getsockname()[1]

    print(f"\n# F10b rigctl port held across a restart ({RIG})")
    a = start(lsock, rport)
    check("first server answers on the inherited socket", wait_reply(rport) is not None, True)
    out_a = stop(a)
    # Server down. JTDX's retry connects now; it must not be refused or dropped.
    try:
        gap = socket.create_connection(("127.0.0.1", rport), timeout=1)
        connected = True
    except OSError as e:
        gap, connected = None, type(e).__name__
    check("connect during the restart gap succeeds", connected, True)
    if gap:
        gap.sendall(b"f\n")
    time.sleep(0.5)
    b = start(lsock, rport)
    if gap:
        gap.settimeout(8)
        buf = b""
        try:
            while not buf.endswith(b"\n"):
                chunk = gap.recv(100)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            pass
        r = buf.decode().strip()
        check("the gap client's command is answered by the new server", r.isdigit(), True)
        gap.close()
    out_b = stop(b)
    for name, out in (("first", out_a), ("second", out_b)):
        check(f"{name} server: no Traceback", "Traceback" in out, False)
        check(f"{name} server: used the inherited socket", "inherited" in out, True)
    lsock.close()


if __name__ == "__main__":
    main()
    print("\nPASS" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)
