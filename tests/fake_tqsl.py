#!/usr/bin/env python3
"""A fake TQSL for the logbook sync tests. Never the real one: the suite
must not sign or upload anything.

Behaves as lotw-help/cmdline describes TQSL in batch mode (-x/-q): status on
stderr, the last line "Final Status: <text> (<code>)", exit codes 0 all
uploaded, 8 none processed (duplicates or out of date range), 9 some
ignored (the same), 2 rejected, 11 network, others errors. -v prints a
version.

It reads what to do from the JSON file named by $FAKE_TQSL_CONF:
    {"mode": "upload", "store": PATH, "range_from": "YYYYMMDD"}
        "uploads" every record with QSO_DATE >= range_from to the store
        (tests/fake_lotw.py reads it) and exits 0, 9 or 8 the way TQSL does
        (-a compliant: out-of-range and duplicate QSOs are not signed)
    {"mode": "exit", "exit": N}       exits N
    {"mode": "hang"}                  starts a child, then never exits (a dialog)
    {"mode": "stdin"}                 reads stdin first (a prompt): EOF -> exit 4
and appends {"argv", "adif", "pid", "child"} to "log".
"""
import json
import os
import re
import subprocess
import sys
import time

TAG = re.compile(r"<([A-Za-z0-9_]+):(\d+)(?::[A-Za-z])?>")


def records(text):
    out = []
    body = re.split(r"(?i)<eoh>", text, maxsplit=1)[-1]
    for chunk in re.split(r"(?i)<eor>", body):
        f, pos = {}, 0
        while (m := TAG.search(chunk, pos)):
            n = int(m.group(2))
            f[m.group(1).upper()] = chunk[m.end():m.end() + n]
            pos = m.end() + n
        if f.get("CALL"):
            out.append(f)
    return out


def final(text, code):
    print(time.strftime("%I:%M:%S %p") + f": Final Status: {text} ({code})", file=sys.stderr)
    sys.exit(code)


def main():
    argv = sys.argv[1:]
    if argv == ["-v"]:
        print("TQSL Version 2.8.2 [fake]")
        return 0
    conf = json.load(open(os.environ["FAKE_TQSL_CONF"]))
    path = argv[-1] if argv else ""
    try:
        adif = open(path).read()
    except OSError:
        adif = ""
    entry = {"argv": argv, "adif": adif, "pid": os.getpid(), "pgid": os.getpgid(0), "child": None}
    mode = conf.get("mode", "exit")
    if mode == "hang":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        entry["child"] = child.pid
    with open(conf["log"], "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(time.strftime("%I:%M:%S %p") + ": Signing using Callsign N0CALL, DXCC Entity UNITED STATES OF AMERICA",
          file=sys.stderr)
    if mode == "hang":
        time.sleep(600)
        return 0
    if mode == "stdin":
        if not sys.stdin.read(1):
            final("TQSL error", 4)
        time.sleep(600)
    if mode == "exit":
        final(conf.get("text", "fake exit"), int(conf["exit"]))
    store = conf["store"]
    have = set()
    if os.path.exists(store):
        for x in open(store):
            r = json.loads(x)
            have.add((r["CALL"], r.get("BAND", "").lower(), r["QSO_DATE"], r["TIME_ON"]))
    recs = records(adif)
    skipped, done = [], 0
    with open(store, "a") as out:
        for r in recs:
            k = (r["CALL"], r.get("BAND", "").lower(), r["QSO_DATE"], r["TIME_ON"])
            if r["QSO_DATE"] < conf.get("range_from", "00000000"):
                skipped.append(f"{r['CALL']} {r['QSO_DATE']}: QSO date out of range")
                continue
            if k in have:
                skipped.append(f"{r['CALL']} {r['QSO_DATE']}: duplicate")
                continue
            out.write(json.dumps({**r, "APP_LOTW_RXQSO": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}) + "\n")
            done += 1
    for s in skipped:
        print(time.strftime("%I:%M:%S %p") + f": {path}: {s}", file=sys.stderr)
    if not done:
        final("No QSOs were processed since some QSOs were duplicates or out of date range", 8)
    if skipped:
        final("Some QSOs were duplicates or out of date range", 9)
    print(time.strftime("%I:%M:%S %p") + f": {path}: Log uploaded successfully", file=sys.stderr)
    final("Success", 0)


if __name__ == "__main__":
    sys.exit(main() or 0)
