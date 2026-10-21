"""What a public Ranch Radio tree must not carry (no personal list needed).

  - an MIT LICENSE
  - no real host files: only examples/hosts/*.json (a machine's own host file
    lives in ~/.config/ranch-radio/hosts, outside the checkout)
  - no home directories other than the placeholder (/Users/you, /home/you)
  - no LAN addresses other than the 192.168.1.x examples
  - no personal launchd labels (only org.ranch-radio.*)
  - no committed logs, packet captures or certificates

    python3 tests/public_hygiene_test.py
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


files = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True).stdout.split()
lic = (ROOT / "LICENSE").read_text() if (ROOT / "LICENSE").exists() else ""
check("an MIT LICENSE", lic.startswith("MIT License"), True)
check("no real host files tracked (examples only)",
      [f for f in files if f.endswith(".json") and "/hosts/" in f"/{f}" and not f.startswith("examples/")], [])
check("no logs, packet captures or certificates tracked",
      [f for f in files if re.search(r"(\.log$|^packet-logs/|^certs/|\.pem$|\.key$)", f)], [])

HOME = re.compile(r"(/Users|/home)/(?!you\b)[a-z][a-z0-9_-]+")
LAN = re.compile(r"\b(192\.168\.(?!1\.)\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)\b")
LABEL = re.compile(r"\bcom\.[a-z0-9-]+\.(ic7300|ardopcf|uvpro|radio|widget)")
hits = {"home": [], "lan": [], "label": []}
for f in files:
    p = ROOT / f
    try:
        text = p.read_text()
    except (UnicodeDecodeError, OSError):
        continue
    for n, line in enumerate(text.splitlines(), 1):
        for key, pat in (("home", HOME), ("lan", LAN), ("label", LABEL)):
            if pat.search(line) and "ranch-radio: allow" not in line:
                hits[key].append(f"{f}:{n}: {line.strip()[:100]}")
check("no home directories but the placeholder", hits["home"], [])
check("no LAN addresses but 192.168.1.x", hits["lan"], [])
check("no personal launchd labels", hits["label"], [])

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
