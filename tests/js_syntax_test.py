"""Every page script parses: `node --check` on each static/**/*.js (the app
has no build step, so a syntax error only shows when a page fails to start).
Skips when node is not installed.

    python3 tests/js_syntax_test.py
"""
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
node = shutil.which("node")
if not node:
    print("skip  no node on PATH")
    sys.exit(0)
files = sorted((ROOT / "static").rglob("*.js"))
fails = 0
for f in files:
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True, timeout=30)
    ok = r.returncode == 0
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {f.relative_to(ROOT)}" + ("" if ok else "\n" + r.stderr.strip()[-600:]))
ok = len(files) >= 20
fails += not ok
print(f"{'ok  ' if ok else 'FAIL'}  found the page scripts ({len(files)})")
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
