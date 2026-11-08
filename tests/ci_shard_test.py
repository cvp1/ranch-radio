"""CI sharding: every run of tests/run_all.sh lands on exactly one shard.

A run dropped by the split would never run in CI and nothing would say so.

    python3 tests/ci_shard_test.py
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARD = ROOT / "tests" / "ci" / "shard.py"
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def shard(*args, stdin=""):
    return subprocess.run([sys.executable, str(SHARD), *args], input=stdin,
                          capture_output=True, text=True)


names = subprocess.run(["bash", str(ROOT / "tests" / "run_all.sh")], env={**os.environ, "RR_LIST": "1"},
                       capture_output=True, text=True, check=True).stdout.split()
check("the run list holds every *_test.py plus the four extra runs",
      len(names), len(list((ROOT / "tests").glob("*_test.py"))) + 4)
check("this test is in the run list", "ci_shard_test" in names, True)

text = "\n".join(names + ["brand_new_test"])   # one with no recorded duration
for n in (1, 4, 7):
    picks = [shard("pick", str(i), str(n), stdin=text).stdout.split() for i in range(1, n + 1)]
    flat = [x for p in picks for x in p]
    check(f"{n} shard(s): every run exactly once", sorted(flat), sorted(names + ["brand_new_test"]))
    check(f"{n} shard(s): no shard empty", all(picks), True)
check("the split is the same on every call",
      shard("pick", "2", "4", stdin=text).stdout, shard("pick", "2", "4", stdin=text).stdout)
check("the split ignores input order",
      shard("pick", "3", "4", stdin="\n".join(reversed(text.split()))).stdout,
      shard("pick", "3", "4", stdin=text).stdout)
check("shard 0 is refused", shard("pick", "0", "4", stdin=text).returncode != 0, True)
check("shard 5 of 4 is refused", shard("pick", "5", "4", stdin=text).returncode != 0, True)

bad = subprocess.run(["bash", str(ROOT / "tests" / "run_all.sh")], env={**os.environ, "RR_SHARD": "two"},
                     capture_output=True, text=True)
check("run_all.sh refuses a malformed RR_SHARD", (bad.returncode, "RR_SHARD wants I/N" in bad.stdout), (2, True))
bad = subprocess.run(["bash", str(ROOT / "tests" / "run_all.sh")], env={**os.environ, "RR_SHARD": "5/4"},
                     capture_output=True, text=True)
check("run_all.sh refuses a shard past the count", bad.returncode, 2)

print("PASS" if fails == 0 else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
