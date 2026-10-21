#!/usr/bin/env python3
"""Rebuild aprs_devices.json from the APRS device identification database.

Source: aprs-deviceid (https://github.com/aprsorg/aprs-deviceid), published
at https://aprs-deviceid.aprsfoundation.org/tocalls.dense.json. The database
is licensed CC BY-SA 2.0; aprs_devices.json is a derived copy under the same
licence, credited in the README. Keeps what the decoder shows (vendor, model,
class) for tocalls, Mic-E suffixes and legacy Mic-E (Kenwood) markers.

    curl -fsSLo /tmp/aprs-deviceid/tocalls.dense.json \\
        https://aprs-deviceid.aprsfoundation.org/tocalls.dense.json
    python3 -I tools/vendor_aprs_devices.py /tmp/aprs-deviceid/tocalls.dense.json
"""
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "aprs_devices.json"
KEEP = ("vendor", "model", "class")


def slim(table, extra=()):
    out = {}
    for k, v in sorted(table.items()):
        if isinstance(v, dict):
            out[k] = {f: v[f] for f in (*KEEP, *extra) if isinstance(v.get(f), str)}
    return out


def main(path):
    d = json.load(open(path, encoding="utf-8"))
    out = {
        "source": "aprs-deviceid, https://github.com/aprsorg/aprs-deviceid (CC BY-SA 2.0)",
        "generated": (d.get("meta") or {}).get("generation_time"),
        "tocalls": slim(d["tocalls"]),
        "mice": slim(d["mice"]),
        "micelegacy": slim(d["micelegacy"], extra=("prefix", "suffix")),
    }
    OUT.write_text(json.dumps(out, indent=0, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"{len(out['tocalls'])} tocalls, {len(out['mice'])} Mic-E, "
          f"{len(out['micelegacy'])} legacy Mic-E (generated {out['generated']}) -> {OUT.name}")


if __name__ == "__main__":
    main(sys.argv[1])
