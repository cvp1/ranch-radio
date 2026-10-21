"""F10c: an app's rigctl connection survives a server restart.

The test plays systemd: it owns the listening socket, runs a NOTIFY_SOCKET
that keeps the fds the server stores (FDSTORE=1 / FDSTOREREMOVE=1), and hands
listening socket + stored connections to the next server as LISTEN_FDS with
LISTEN_FDNAMES, as systemd's fd store does for g90-remote.service.

JTDX showed "IO error while getting correct VFO" on every restart that broke
its session (linux-station 2026-10-14 11:02, 11:23, 11:58). Here a client keeps one
connection through a SIGTERM restart and through a crash (SIGKILL), and its
emulated split survives with it, so a key after the handover still hops to
the split TX frequency.

    .venv/bin/python tests/handover_test.py [rig]
"""
import array
import os
import random
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RIG = sys.argv[1] if len(sys.argv) > 1 else "g90"
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


class FakeSystemd:
    """Listening socket + NOTIFY_SOCKET fd store; starts server.py like systemd."""

    def __init__(self):
        self.lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(16)
        self.port = self.lsock.getsockname()[1]
        self.tmp = tempfile.mkdtemp(prefix="handover-")
        self.notify_path = os.path.join(self.tmp, "notify")
        self.runtime = os.path.join(self.tmp, "run")
        os.mkdir(self.runtime)
        self.nsock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.nsock.bind(self.notify_path)
        self.store = {}              # name -> fd
        self.messages = []
        self.lock = threading.Lock()
        self._stop = False
        threading.Thread(target=self._notify_loop, daemon=True).start()
        self.proc = None

    def _notify_loop(self):
        self.nsock.settimeout(0.2)
        while not self._stop:
            try:
                msg, fds, _flags, _addr = socket.recv_fds(self.nsock, 4096, 16)
            except socket.timeout:
                continue
            except OSError:
                return
            text = msg.decode()
            kv = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            with self.lock:
                self.messages.append(text)
                if kv.get("FDSTORE") == "1" and fds:
                    old = self.store.pop(kv.get("FDNAME", "stored"), None)
                    if old is not None:
                        os.close(old)
                    self.store[kv.get("FDNAME", "stored")] = fds[0]
                    for extra in fds[1:]:
                        os.close(extra)
                elif kv.get("FDSTOREREMOVE") == "1":
                    fd = self.store.pop(kv.get("FDNAME", ""), None)
                    if fd is not None:
                        os.close(fd)
                else:
                    for fd in fds:
                        os.close(fd)

    def radio_state(self):
        """The radio's real state, from the browser socket's hello."""
        import asyncio, json, aiohttp

        async def go():
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(f"http://127.0.0.1:{self.web}/ws") as ws:
                    return json.loads((await ws.receive(timeout=3)).data)["state"]
        return asyncio.run(go())

    def start(self):
        web = self.web = 18000 + random.randrange(2000)
        with self.lock:
            stored = list(self.store.items())
        names = ["g90-remote.socket"] + [n for n, _ in stored]
        srcs = [self.lsock.fileno()] + [fd for _, fd in stored]
        n = len(srcs)

        def place():
            # dup every source above the target range, then down to 3..3+n-1
            tmp = [os.dup(fd) for fd in srcs]
            for i, fd in enumerate(tmp):
                os.dup2(fd, 3 + i)
                os.close(fd)

        env = dict(os.environ, NOTIFY_SOCKET=self.notify_path, RUNTIME_DIRECTORY=self.runtime,
                   LISTEN_FDS=str(n), LISTEN_FDNAMES=":".join(names))
        self.proc = subprocess.Popen(
            ["sh", "-c", 'LISTEN_PID=$$ exec "$0" "$@"',
             sys.executable, "server.py", "--sim", "--rig", RIG, "--no-audio",
             "--listen", str(web), "--host", "127.0.0.1",
             "--rigctl-host", "127.0.0.1", "--rigctl-port", str(self.port)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            pass_fds=tuple(range(3, 3 + n)), preexec_fn=place)
        time.sleep(1.5)

    def stop(self, sig=signal.SIGTERM):
        self.proc.send_signal(sig)
        try:
            out = self.proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out = self.proc.communicate()[0]
        return out

    def close(self):
        self._stop = True
        for fd in self.store.values():
            os.close(fd)
        self.nsock.close()
        self.lsock.close()


class Client:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.f = self.s.makefile("r")

    def cmd(self, line, n=1):
        try:
            self.s.sendall((line + "\n").encode())
            out = []
            for _ in range(n):
                got = self.f.readline()
                if not got:
                    return ["EOF"]
                out.append(got.strip())
            return out
        except OSError as e:
            return [type(e).__name__]

    def close(self):
        self.f.close()
        self.s.close()


def main():
    sd = FakeSystemd()
    outs = []
    try:
        print(f"\n# F10c rigctl session survives restarts ({RIG})")
        sd.start()
        c = Client(sd.port)
        check("first server answers", c.cmd("f")[0].isdigit(), True)
        c.cmd("F 14074000")
        c.cmd("S 1 VFOB")
        c.cmd("I 14076000")
        time.sleep(0.3)
        with sd.lock:
            check("connection stored in the fd store", len(sd.store), 1)

        outs.append(sd.stop(signal.SIGTERM))
        sd.start()
        check("same connection answered after a restart", c.cmd("f"), ["14074000"])
        if RIG == "g90":     # emulated split is server state; the IC-7300's lives in the radio
            check("split still on after the restart", c.cmd("s", 2), ["1", "VFOB"])
            check("split TX freq kept", c.cmd("i"), ["14076000"])

        outs.append(sd.stop(signal.SIGKILL))
        sd.start()
        check("same connection answered after a crash", c.cmd("f"), ["14074000"])
        if RIG == "g90":
            check("split still on after the crash", c.cmd("s", 2), ["1", "VFOB"])
        check("T 1 keys after the handover", c.cmd("T 1"), ["RPRT 0"])
        if RIG == "g90":     # emulated split: the dial is on the TX freq while keyed
            time.sleep(0.4)
            st = sd.radio_state()
            check("keyed on the split TX freq after the handover",
                  (st["tx"], st["freq"]), (True, 14076000))
        c.cmd("T 0")
        c.cmd("S 0 VFOA")
        c.close()
        time.sleep(0.5)
        with sd.lock:
            check("a client that leaves is removed from the store", len(sd.store), 0)

        # A fresh start with nothing stored must not resurrect an old split.
        c2 = Client(sd.port)
        c2.cmd("S 1 VFOB")
        c2.cmd("I 14076000")
        c2.close()
        time.sleep(0.4)
        outs.append(sd.stop(signal.SIGTERM))
        sd.start()
        c3 = Client(sd.port)
        check("new client after a restart: split off", c3.cmd("s", 2), ["0", "VFOA"])
        c3.close()
        outs.append(sd.stop(signal.SIGTERM))
    finally:
        if sd.proc and sd.proc.poll() is None:
            outs.append(sd.stop(signal.SIGTERM))
        sd.close()
    tb = [o for o in outs if "Traceback" in o]
    check("no Traceback in any server log", len(tb), 0)
    if tb:
        print(tb[0][-1500:])


if __name__ == "__main__":
    main()
    print("\nPASS" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)
