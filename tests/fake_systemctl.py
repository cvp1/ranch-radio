"""A stand-in for `systemctl` (RANCH_RADIO_SYSTEMCTL): records each call as
one JSON line in FAKE_SYSTEMCTL_LOG and succeeds. Nothing is reloaded."""
import json
import os
import sys

with open(os.environ["FAKE_SYSTEMCTL_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
