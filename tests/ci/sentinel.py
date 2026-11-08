"""Prove the suite never touches the real HOME: plant decoy station state there,
run the suite, then fail if any file under the app's directories changed.

    python3 tests/ci/sentinel.py plant  HOME SNAPSHOT
    python3 tests/ci/sentinel.py check  HOME SNAPSHOT

Containers and CI only. plant refuses a HOME that already holds station state,
so it can never write over a live station's files."""
import hashlib
import json
import os
import sys

DIRS = [".local/state/radio-remote", ".local/state/ranch-radio", ".config/ranch-radio",
        ".config/pat", ".local/share/pat", ".local/state/pat", ".local/share/opengd77"]
DECOYS = {
    # A listen record whose owner is dead: what Winlink's start-up recovery ends.
    ".local/state/radio-remote/winlink-listen.json":
        {"stage": "listening", "server_pid": 999999, "pid": 0, "hub": None},
    ".local/state/radio-remote/addons.json": {},
    ".local/state/ranch-radio/update.json":
        {"pending": [], "failed": {}, "notified": "", "running": ""},
}


def snapshot(home):
    out = {}
    for d in DIRS:
        for root, _, files in os.walk(os.path.join(home, d)):
            for n in files:
                p = os.path.join(root, n)
                with open(p, "rb") as f:
                    out[os.path.relpath(p, home)] = hashlib.sha256(f.read()).hexdigest()
    return out


def main(cmd, home, snap):
    if cmd == "plant":
        if snapshot(home):
            sys.exit(f"sentinel: {home} already holds station state; refusing to plant over it")
        for rel, body in DECOYS.items():
            p = os.path.join(home, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                json.dump(body, f)
        with open(snap, "w") as f:
            json.dump(snapshot(home), f)
        print(f"sentinel: planted {len(DECOYS)} decoys under {home}")
        return 0
    with open(snap) as f:
        before = json.load(f)
    after = snapshot(home)
    bad = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    for k in bad:
        what = "deleted" if k not in after else "created" if k not in before else "changed"
        print(f"FAIL sentinel: {what} {os.path.join(home, k)}")
    if not bad:
        print("ok   sentinel: the real HOME's station state is untouched")
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("plant", "check"):
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))
