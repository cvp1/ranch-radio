#!/usr/bin/env python3
"""Build tests/fixtures/aprs/rx-sample.jsonl from the hub's receive log.

A stratified sample of what a UV-PRO hears at a station, so the decoder's
tests have real traffic even where packet-logs/ (gitignored, rolling) is
absent: up to PER_TYPE packets of each APRS data type (the first info byte;
third-party packets by their inner type too), every packet of the rare types,
and the packets the tests pin by name (PINNED). Deterministic: the same log
gives the same sample.

    python3 tools/aprs_fixture_sample.py [packet-logs/rx.jsonl ...]
"""
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.path.expanduser("~/.config/ranch-radio/tests/fixtures/aprs/rx-sample.jsonl"))   # private: it says where the station is
PER_TYPE = 40
# Substrings of packets the decoder tests pin (see the build plan, M1).
PINNED = [
    "HELIO>APNU19",            # PHG and a UIDIGI comment
    ":ack00}",                 # reply-ack capable ack (ANSRVR)
    "{97919\r",                # msgno with a trailing CR
    "{YP}00",                  # reply-ack in a message
    "EQNS.0,0.075,0,0,0.1953", # an empty EQNS coefficient (,,)
    "3320.  N/11136.  W",      # position ambiguity
    "!/=XO:28$k4  C",          # compressed, no cs data, /A= in the comment
    ":TC 0",                   # TNC text, not telemetry
    "_332/003g007t072",        # weather: wind DDD/SSS after the _ symbol
    "_PHG5930 Weather on Pinal",  # _ symbol with a plain comment
    "KJ7ROX-0>",               # inner header with an explicit -0 SSID
]


def kind(text):
    info = text.split(":", 1)[1] if ":" in text else ""
    k = info[:1] or "(empty)"
    if k == "}":
        inner = info[1:].split(":", 1)
        k = "}" + (inner[1][:1] if len(inner) == 2 else "?")
    return k


def main(paths):
    rows = []
    for p in paths:
        for line in open(p, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r.get("text"), str):
                rows.append({"t": r.get("t"), "text": r["text"]})
    by = {}
    for r in rows:
        by.setdefault(kind(r["text"]), []).append(r)
    rng = random.Random(20261019)
    keep = {}
    for k in sorted(by):
        group = by[k]
        pick = group if len(group) <= PER_TYPE else rng.sample(group, PER_TYPE)
        for r in pick:
            keep[r["text"]] = r
    for needle in PINNED:
        hit = next((r for r in rows if needle in r["text"]), None)
        if hit is None:
            print(f"pinned packet not in the log: {needle!r}", file=sys.stderr)
        else:
            keep[hit["text"]] = hit
    out = sorted(keep.values(), key=lambda r: (r["t"] or 0, r["text"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    print(f"{len(out)} packets from {len(rows)} ({len(by)} kinds) -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main(sys.argv[1:] or [p for p in (ROOT / "packet-logs/rx.jsonl.1", ROOT / "packet-logs/rx.jsonl") if p.exists()])
