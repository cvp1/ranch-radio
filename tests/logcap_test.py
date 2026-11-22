"""logcap: a launchd-style console log (stdout and stderr appending to one
file) is copied to .1 and truncated once it passes the cap; older copies
shift, at most `keep`; a child writing to the inherited descriptor carries on
at the new end (no hole); a file opened without O_APPEND, or a pipe, is left
alone. Plus `ranch-radio logs` resolves where each rendered service logs.

    python3 tests/logcap_test.py
"""
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


CHILD = r"""
import os, subprocess, sys
sys.path.insert(0, sys.argv[1])
import logcap
cap = int(sys.argv[2])
def burst(n):
    sys.stdout.write("o" * n + "\n"); sys.stdout.flush()
    sys.stderr.write("e" * n + "\n"); sys.stderr.flush()
for _ in range(4):                       # four rotations' worth
    burst(cap)
    print("rotated", logcap.check(max_bytes=cap, keep=2), file=sys.stderr, flush=True)
# a child inherits the descriptors, as rigctld and Pat do
subprocess.run([sys.executable, "-c", "import sys; sys.stderr.write('child-line\\n')"])
"""

tmp = tempfile.mkdtemp(prefix="logcap-")
log = os.path.join(tmp, "server.log")
cap = 4096
fd_out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
fd_err = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)   # launchd opens each path itself
p = subprocess.run([sys.executable, "-c", CHILD, str(ROOT), str(cap)], stdout=fd_out, stderr=fd_err, timeout=60)
os.close(fd_out)
os.close(fd_err)
check("the child ran", p.returncode, 0)
names = sorted(os.listdir(tmp))
check("rotated copies kept: .1 and .2, never .3", names, ["server.log", "server.log.1", "server.log.2"])
text = pathlib.Path(log).read_text()
check("the live file is small again", os.path.getsize(log) < 2 * cap, True)
check("no hole: the live file has no NUL bytes", "\0" in text, False)
check("a grandchild's write lands in the live file", "child-line" in text, True)
check("each copy holds a full burst", os.path.getsize(log + ".1") > 2 * cap, True)

# Not opened for append: truncating would leave a hole at the old offset.
plain = os.path.join(tmp, "plain.log")
fd = os.open(plain, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
p = subprocess.run([sys.executable, "-c", CHILD, str(ROOT), str(cap)], stdout=fd, stderr=fd, timeout=60)
os.close(fd)
check("a file without O_APPEND is never rotated", os.path.exists(plain + ".1"), False)

# A pipe (journald on Linux is a socket): nothing to do, nothing raised.
p = subprocess.run([sys.executable, "-c", CHILD, str(ROOT), str(cap)], capture_output=True, text=True, timeout=60)
check("a pipe: nothing rotated", "rotated ['" in p.stderr, False)
check("  and the child still ran", p.returncode, 0)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
