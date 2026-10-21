"""Where the APRS receive sample is: it's real traffic heard at a station,
so it says where that station is, and it lives with the station's private
configs, not in this repo.

    sample() -> Path or None
RANCH_RADIO_APRS_SAMPLE, the checkout's tests/fixtures/aprs/rx-sample.jsonl,
or ~/.config/ranch-radio/tests/fixtures/aprs/rx-sample.jsonl. Tests that need
it skip those checks without it.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sample():
    for c in (os.environ.get("RANCH_RADIO_APRS_SAMPLE"), ROOT / "tests/fixtures/aprs/rx-sample.jsonl",
              Path(os.path.expanduser("~/.config/ranch-radio/tests/fixtures/aprs/rx-sample.jsonl"))):
        if c and Path(c).exists():
            return Path(c)
    return None
