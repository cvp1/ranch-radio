"""Split the suite's runs across CI shards by measured duration.

  shard.py pick I N < names     the names shard I of N runs (1-based)
  shard.py plan N < names       every shard's names and estimated seconds
  shard.py refresh < ci.log     rewrite durations.json from a CI log's
                                "PASS name (12s)" lines (gh run view --log)

Longest first onto the least-loaded shard, ties by name, so every shard
computes the same split. A run missing from durations.json counts as
DEFAULT seconds until the next refresh.
"""
import json
import re
import sys
from pathlib import Path

DURATIONS = Path(__file__).with_name("durations.json")
DEFAULT = 30


def split(names, n):
    secs = json.loads(DURATIONS.read_text()) if DURATIONS.exists() else {}
    shards = [[0, []] for _ in range(n)]
    for name in sorted(set(names), key=lambda x: (-secs.get(x, DEFAULT), x)):
        load = min(range(n), key=lambda i: (shards[i][0], i))
        shards[load][0] += secs.get(name, DEFAULT)
        shards[load][1].append(name)
    return shards


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
