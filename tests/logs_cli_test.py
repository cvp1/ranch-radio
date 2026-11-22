"""`ranch-radio logs`: lists where every rendered service logs (a Mac agent's
console file, from its plist; a Linux unit's journal), and tails one by its
name or module: tail -n N [-F] on a Mac, journalctl --user -u UNIT on Linux.
The commands are stand-ins here; the example host files are the input.

    python3 tests/logs_cli_test.py
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = 0
ARGV = os.path.join(tempfile.mkdtemp(prefix="logscli-"), "argv")     # prints its arguments (echo eats -n)
with open(ARGV, "w") as f:
    f.write('#!/bin/sh\nprintf "%s\\n" "$*"\n')
os.chmod(ARGV, 0o755)


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def rr(*args):
    env = dict(os.environ, RANCH_RADIO_HOSTS=os.path.join(ROOT, "examples", "hosts"),
               RANCH_RADIO_TAIL=ARGV, RANCH_RADIO_JOURNALCTL=ARGV)
    p = subprocess.run([sys.executable, os.path.join(ROOT, "ranch-radio"), "logs", *args, "--home", "/Users/you"],
                       capture_output=True, text=True, env=env, timeout=60)
    return p.returncode, p.stdout.strip()


rc, out = rr("--host", "mac-shack")
check("mac: the list", rc, 0)
check("  the radio's console is the checkout's server.log",
      any("org.ranch-radio.ic7300-remote" in ln and ln.endswith("/Users/you/ranch-radio/server.log") for ln in out.splitlines()),
      True)
check("  the station's", "/Users/you/Library/Logs/ranch-radio-station.log" in out, True)
check("  the widget has none", any("ic7300-widget" in ln and ln.endswith("(none)") for ln in out.splitlines()), True)
check("mac: by module, tail", rr("ic7300", "--host", "mac-shack"), (0, "-n 50 /Users/you/ranch-radio/server.log"))
check("mac: -f follows, -n counts", rr("station", "-f", "-n", "5", "--host", "mac-shack"),
      (0, "-n 5 -F /Users/you/Library/Logs/ranch-radio-station.log"))
rc, out = rr("--host", "linux-station")
check("linux: units, no files", ("g90-remote.service" in out, "/Library/" in out), (True, False))
check("linux: by module, the journal", rr("g90", "-f", "--host", "linux-station"),
      (0, "--user -u g90-remote.service -n 50 --no-pager -f"))
check("an unknown name: exit 2, nothing tailed", rr("nope", "--host", "linux-station")[0], 2)
check("an ambiguous name: exit 2", rr("remote", "--host", "linux-station")[0], 2)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
