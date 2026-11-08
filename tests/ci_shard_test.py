"""CI sharding: every run of tests/run_all.sh lands on exactly one shard.

A run dropped by the split would never run in CI and nothing would say so.

    python3 tests/ci_shard_test.py
"""
import os
import json
import subprocess
import sys
import tempfile
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

# check: a CI run that matches the recorded times is silent; each budget trips alone.
recorded = json.loads((ROOT / "tests" / "ci" / "durations.json").read_text())
listed = "\n".join(names)
plan = [shard("pick", str(i), "4", stdin=listed).stdout.split() for i in range(1, 5)]
tmp = Path(tempfile.mkdtemp())
env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}


def run_check(times, label):
    tsvs = []
    for i, part in enumerate(plan, 1):
        f = tmp / f"{label}-{i}.tsv"
        f.write_text("".join(f"{n} {times[n]}\n" for n in part if n in times))
        tsvs.append(str(f))
    r = subprocess.run([sys.executable, str(SHARD), "check", *tsvs], input=listed,
                       capture_output=True, text=True, env=env)
    return r.returncode, r.stdout.splitlines()


base = {n: recorded.get(n, 30) for n in names}
check("check: times as recorded pass silently", run_check(base, "base"), (0, []))
slow = min((n for n in base if 15 <= base[n] <= 40), key=lambda n: (-base[n], n))   # big enough to trip, too small to unbalance
rc, out = run_check({**base, slow: base[slow] * 3}, "slower")
check("check: a run 3x its recorded time warns, and only that",
      (rc, [l.split()[:2] for l in out]), (0, [["WARNING", slow]]))
rc, out = run_check({k: v for k, v in base.items() if k != "ci_shard_test"}, "missing")
check("check: a run with no time fails the gate",
      (rc, any(l.startswith("ERROR ci_shard_test") for l in out)), (1, True))
fresh = [n for n in names if n not in recorded]
if fresh:
    rc, out = run_check({**base, fresh[0]: 120}, "newslow")
    check("check: a new run over 90 s warns", (rc, [l.split()[1] for l in out]), (0, [fresh[0]]))
lopsided = {n: (base[n] * 2 if n in plan[0] else base[n]) for n in names}
rc, out = run_check(lopsided, "spread")
check("check: shards that drifted apart warn with the refresh command",
      (rc, any("refresh" in l for l in out)), (0, True))

print("PASS" if fails == 0 else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
