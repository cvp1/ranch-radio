"""The theme picker (static/theme.js, static/themes.css) in headless Chromium,
on a simulated radio page (tools/readme_screenshots.py):

  - a browser with no choice gets the default (Midnight blue): the <html>
    data-theme, the accent CSS variable and the theme-color meta all say so
  - the top bar's picker lists every theme, the current one ticked
  - picking one changes the accent at once and closes the menu
  - the choice outlives a reload (localStorage "rr.theme")
  - an unknown stored value falls back to the default, not to no theme
  - Mint (classic) is app.css's own palette (no override)

    python3 tests/theme_page_test.py
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from cdp_browser import Browser  # noqa: E402
import readme_screenshots as RS  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


STATE = """(() => ({
  theme: document.documentElement.dataset.theme,
  accent: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),
  meta: document.querySelector('meta[name="theme-color"]').content,
  open: !!document.querySelector('.theme-menu'),
}))()"""

tmp = tempfile.mkdtemp(prefix="theme-page-")
sp = RS.free_port()
RS.stand_ins(sp)
p, port = RS.serve(f"http://127.0.0.1:{sp}", tmp)
b = Browser(RS.CHROME, RS.free_port(), size="1400,900")
url = f"http://127.0.0.1:{port}/"
try:
    b.goto(url)
    b.wait("!!document.querySelector('.theme-pick button')")
    s = b.js(STATE)
    check("no choice: the default theme", s["theme"], "midnight")
    check("  its accent", s["accent"], "#6ea8ff")
    check("  the theme-color meta follows", s["meta"], "#060a18")

    b.js("document.querySelector('.theme-pick > button').click()")
    menu = b.js("[...document.querySelectorAll('.theme-menu [data-theme-id]')].map((e) => [e.dataset.themeId, e.classList.contains('on')])")
    check("the menu lists every theme", [m[0] for m in menu],
          ["midnight", "classic", "amber", "sandstone", "aurora", "contrast"])
    check("  the current one ticked", [m[0] for m in menu if m[1]], ["midnight"])

    b.js("document.querySelector('.theme-menu [data-theme-id=\"amber\"]').click()")
    s = b.js(STATE)
    check("picking Amber applies it", (s["theme"], s["accent"]), ("amber", "#ffb547"))
    check("  and closes the menu", s["open"], False)
    check("  stored", b.js("localStorage.getItem('rr.theme')"), "amber")

    b.goto(url)
    b.wait("!!document.querySelector('.theme-pick button')")
    check("a reload keeps it", b.js(STATE)["theme"], "amber")

    b.js("localStorage.setItem('rr.theme', 'neon-nonsense')")
    b.goto(url)
    b.wait("!!document.querySelector('.theme-pick button')")
    check("an unknown stored theme falls back to the default", b.js(STATE)["theme"], "midnight")

    b.js("theme.set('classic')")
    check("Mint (classic) is app.css's own accent", b.js(STATE)["accent"], "#39e1c6")
finally:
    b.close()
    p.terminate()
    p.wait(10)
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
