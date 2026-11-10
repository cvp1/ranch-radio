"""tests/fake_kiss_radio.py never hands a hub a pty that a leftover process
is bound to.

A hub from a killed run keeps reopening its --tnc by name, and the system
gives a freed pty name to the next pty: the leftover then reads the frames
meant for a later run's hub. Covers:

  - bound_elsewhere: finds a process started on a path, by whole word only,
    and never this process
  - open_pty: a held name is parked (kept open) and another taken; when every
    name is held it raises and leaves nothing open
  - the real thing: a stray process on the next pty name, and FakeKissRadio
    gets a different one; stop() lets the parked name go

    python3 tests/fake_kiss_radio_test.py
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_kiss_radio as F  # noqa: E402
from testkit import wait_for  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


def stray(path):
    """A process whose command line names `path` the way a hub's does."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "--tnc", path])
    wait_for(lambda: p.pid in F.bound_elsewhere(path), 5)
    return p


def next_name():
    """The name the next pty will get (the system reuses the one just freed)."""
    m, s = os.openpty()
    name = os.ttyname(s)
    os.close(m); os.close(s)
    return name


def clean_name():
    """(name, held): the next pty name nothing on this machine is started on.
    A machine with a real leftover must not fail this test: its names stay
    held (open) until the test ends."""
    held = []
    while True:
        m, s = os.openpty()
        if not F.bound_elsewhere(os.ttyname(s)):
            name = os.ttyname(s)
            os.close(m); os.close(s)
            return name, held
        held += [m, s]


def is_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


# --- bound_elsewhere
name, held_fds = clean_name()
p = stray(name)
try:
    check("a process started on the path is found", F.bound_elsewhere(name), [p.pid])
    check("  a longer path that only starts the same is not", F.bound_elsewhere(name[:-1]), [])
    check("  this process never counts", os.getpid() in F.bound_elsewhere(sys.argv[0]), False)
finally:
    p.kill(); p.wait()
check("  gone once it exits", F.bound_elsewhere(name), [])

# --- open_pty, with the answer injected
seen = []


def first_held(path):
    seen.append(path)
    return [4242] if len(seen) == 1 else []


m, s, parked = F.open_pty(bound=first_held)
check("a held name: another pty is taken", os.ttyname(s) != seen[0], True)
check("  the held one is parked open (its name can't come round again)",
      (len(parked), all(map(is_open, parked))), (2, True))
for fd in (m, s, *parked):
    os.close(fd)

held = []
try:
    F.open_pty(bound=lambda path: held.append(path) or [4242], tries=3)
    raised = False
except RuntimeError:
    raised = True
check("every name held: raises after the tries", (raised, len(held), len(set(held))), (True, 3, 3))
check("  and leaves the names free again", next_name() in held, True)

# --- the real thing
if next_name() != name:
    print("skip  a stray on the next pty name: this system does not reuse a freed name at once")
else:
    radio = F.FakeKissRadio()
    got = radio.path
    radio.stop()
    check("positive control: with no stray the radio gets the next name", got, name)
    p = stray(name)
    try:
        radio = F.FakeKissRadio()
        check("a stray on the next name: the radio is somewhere else", radio.path != name, True)
        check("  the stray's name is parked", len(radio._parked), 2)
        parked = list(radio._parked)
        radio.stop()
        check("  stop() lets it go", any(map(is_open, parked)), False)
    finally:
        p.kill(); p.wait()

for fd in held_fds:
    os.close(fd)
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
