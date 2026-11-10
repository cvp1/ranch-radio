#!/usr/bin/env python3
"""Refresh tests/fixtures/render-golden.json: what every example host renders.

    python3 tools/render_golden.py            # rewrite the fixture
    python3 tools/render_golden.py --check    # exit 1 if the fixture is stale

The fixture pins each example host's rendered artifacts (vara_module_test
checks them). Refresh it only when a render change is deliberate, in the same
commit, and say so in the CHANGELOG: a surprise diff here is a regression.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ranch_radio import render  # noqa: E402

HOME = "/Users/you"
FIXTURE = ROOT / "tests/fixtures/render-golden.json"


def rendered(h):
    return [{"path": a.path, "fmt": a.fmt, "text": a.text()} for a in render.artifacts(h, home=HOME)]


def golden():
    out = {}
    for f in sorted((ROOT / "examples/hosts").glob("[!._]*.json")):
        h = render.load(f)
        out[h["host"]] = rendered(h)
    return out


def main(argv):
    g = golden()
    if "--check" in argv:
        cur = json.loads(FIXTURE.read_text()) if FIXTURE.exists() else {}
        stale = [k for k in set(g) | set(cur) if g.get(k) != cur.get(k)]
        print("stale: " + ", ".join(sorted(stale)) if stale else "fixture matches")
        return 1 if stale else 0
    FIXTURE.write_text(json.dumps(g, indent=1, sort_keys=True) + "\n")
    print(f"wrote {FIXTURE.relative_to(ROOT)} ({len(g)} hosts)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
