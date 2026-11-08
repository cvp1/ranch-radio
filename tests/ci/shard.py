"""Split the suite's runs across CI shards by measured duration.

  shard.py pick I N < names     the names shard I of N runs (1-based)
  shard.py plan N < names       every shard's names and estimated seconds
  shard.py refresh < ci.log     rewrite durations.json from a CI log's
                                "PASS name (12s)" lines (gh run view --log)
  shard.py check TSV... < names after CI: fail if any run reported no time;
                                warn on a run much slower than recorded, a new
                                run over NEW_SLOW s, or shards SPREAD apart.
                                One TSV per shard (run_all.sh durations.tsv).

Longest first onto the least-loaded shard, ties by name, so every shard
computes the same split. A run missing from durations.json counts as
DEFAULT seconds until the next refresh.
"""
import json
import os
import re
import sys
from pathlib import Path

DURATIONS = Path(__file__).with_name("durations.json")
DEFAULT = 30
NEW_SLOW = 90     # a run with no recorded time that takes longer than this warns
SLOWER = 1.5      # a recorded run warns when it takes SLOWER x as long ...
SLOWER_BY = 30    # ... and at least this many seconds more
SPREAD = 0.2      # shards warn when the longest exceeds the shortest by this share


def split(names, n):
    secs = json.loads(DURATIONS.read_text()) if DURATIONS.exists() else {}
    shards = [[0, []] for _ in range(n)]
    for name in sorted(set(names), key=lambda x: (-secs.get(x, DEFAULT), x)):
        load = min(range(n), key=lambda i: (shards[i][0], i))
        shards[load][0] += secs.get(name, DEFAULT)
        shards[load][1].append(name)
    return shards


def check(names, tsvs):
    """Return (errors, warnings) for one CI run's per-shard duration files."""
    secs = json.loads(DURATIONS.read_text()) if DURATIONS.exists() else {}
    got, totals = {}, []
    for tsv in tsvs:
        total = 0
        for line in Path(tsv).read_text().splitlines():
            name, _, t = line.partition(" ")
            got[name] = int(t)
            total += int(t)
        totals.append(total)
    errors = [f"{n} reported no time: it ran on no shard" for n in sorted(set(names) - set(got))]
    warnings = []
    for n, t in sorted(got.items()):
        if n not in secs:
            if t > NEW_SLOW:
                warnings.append(f"{n} took {t}s, over the {NEW_SLOW}s budget for a new run")
        elif t > secs[n] * SLOWER and t - secs[n] >= SLOWER_BY:
            warnings.append(f"{n} took {t}s, recorded {secs[n]}s")
    if len(totals) > 1 and max(totals) and (max(totals) - min(totals)) / max(totals) > SPREAD:
        warnings.append(f"shards took {'/'.join(map(str, totals))}s; refresh the split: "
                        "gh run view <run-id> --log | python3 tests/ci/shard.py refresh")
    return errors, warnings


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "refresh":
        got = {m[1]: int(m[2]) for m in re.finditer(r"\b(?:PASS|FAIL) (\S+) \((\d+)s\)", sys.stdin.read())}
        if not got:
            sys.exit("no 'PASS name (Ns)' lines on stdin")
        DURATIONS.write_text(json.dumps(dict(sorted(got.items())), indent=0) + "\n")
        print(f"{len(got)} durations written to {DURATIONS.name}")
        return
    names = sys.stdin.read().split()
    if cmd == "check" and len(argv) > 2:
        errors, warnings = check(names, argv[2:])
        gh = bool(os.environ.get("GITHUB_ACTIONS"))
        for kind, msgs in (("error", errors), ("warning", warnings)):
            for m in msgs:
                print(f"::{kind} title=CI time::{m}" if gh else f"{kind.upper()} {m}")
        sys.exit(1 if errors else 0)
    if cmd == "pick" and len(argv) == 4:
        i, n = int(argv[2]), int(argv[3])
        if not 1 <= i <= n:
            sys.exit(f"shard {i} of {n}?")
        print("\n".join(split(names, n)[i - 1][1]))
    elif cmd == "plan" and len(argv) == 3:
        for i, (secs, picked) in enumerate(split(names, int(argv[2])), 1):
            print(f"shard {i}: ~{secs}s, {len(picked)} runs: {' '.join(picked)}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
