"""Run under a venv with aprslib (GPL; a test oracle only, never shipped):
reads TNC2 lines (JSON strings, one per line) on stdin, writes aprslib's
reading of each as one JSON line. tests/aprs_test.py compares it to aprs.py.

    ~/.local/share/venvs/aprslib-oracle/bin/python tests/aprs_oracle.py < lines
"""
import json
import sys

import aprslib

for line in sys.stdin:
    text = json.loads(line)
    try:
        p = aprslib.parse(text.rstrip("\r\n"))
        out = {k: p.get(k) for k in ("format", "latitude", "longitude", "course", "speed",
                                      "altitude", "addresse", "msgNo", "response", "symbol",
                                      "symbol_table", "posambiguity")}
        if "weather" in p:
            out["weather"] = p["weather"]
    except Exception as e:  # noqa: BLE001 - the oracle's refusals are data too
        out = {"error": f"{type(e).__name__}: {e}"[:120]}
    print(json.dumps(out, default=str))
