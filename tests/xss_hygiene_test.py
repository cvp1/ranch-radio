"""The pages stay runnable under webguard.CSP (script-src 'self'), and the
escape helpers escape all five HTML-special characters.

  - no inline <script> body and no on*= handler attribute in static/**/*.html
    (the policy would block either; a page would silently half-start)
  - esc() in common.js and packet.js turns & < > " ' into entities; run in
    node when it is installed, otherwise the replace pattern is checked

    python3 tests/xss_hygiene_test.py
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:62} got {got!r}" + ("" if ok else f" want {want!r}"))


INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", re.I)
HANDLER = re.compile(r"<[a-z][^>]*\son[a-z]+\s*=", re.I)
pages = sorted((ROOT / "static").rglob("*.html"))
check("there are pages to check", len(pages) >= 3, True)
for p in pages:
    s = p.read_text()
    rel = p.relative_to(ROOT)
    check(f"{rel}: no inline <script>", INLINE.findall(s), [])
    check(f"{rel}: no on*= handler attributes", HANDLER.findall(s), [])

PAYLOAD = "<img src=x onerror=alert(1)> & \"q\" 'a'"
WANT = "&lt;img src=x onerror=alert(1)&gt; &amp; &quot;q&quot; &#39;a&#39;"
node = shutil.which("node")
for f in ("static/common.js", "static/packet.js"):
    src = (ROOT / f).read_text()
    m = re.search(r"^const esc = .*$", src, re.M)
    check(f"{f}: has esc", bool(m), True)
    if not m:
        continue
    if node:
        prog = m.group(0) + f"\nprocess.stdout.write(esc({json.dumps(PAYLOAD)}));\n"
        out = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=20)
        check(f"{f}: esc escapes & < > \" ' (node)", out.stdout, WANT)
        out = subprocess.run([node, "-e", m.group(0) + "\nprocess.stdout.write(esc(null) + '|' + esc(undefined));\n"],
                             capture_output=True, text=True, timeout=20)
        check(f"{f}: esc(null), esc(undefined) are empty", out.stdout, "|")
    else:
        check(f"{f}: esc's pattern names all five", all(c in m.group(0) for c in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;")),
              True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
