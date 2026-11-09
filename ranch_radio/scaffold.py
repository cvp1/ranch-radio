"""`ranch-radio new-addon NAME`: the files a new add-on starts from.

    create(root, name, label, title, group="tools", css=False) -> [paths written]
    snippets(name, label, title)                                -> str, the entries to add by hand

Writes, and refuses to overwrite:
    addons/NAME.py              an Addon subclass with every hook stubbed and explained
    static/addons/NAME.js       its page script (addons.register)
    static/addons/NAME.css      with css=True
    tests/NAME_addon_test.py    loads it in the registry; checks the catalog and
                                manifest know it, so it FAILS until they do
Then the two entries only a person should write (ranch_radio/catalog.py and
ranch_radio/manifest.py) are printed, with the rest of the checklist. Nothing
here edits an existing file.
"""
import re
from pathlib import Path

NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")          # addons.NAME
GROUPS = ("radios", "messaging", "mesh", "tools")      # static/nav.js


def class_name(name):
    return "".join(p.capitalize() for p in name.split("_"))


def files(root, name, css=False):
    root = Path(root)
    out = [root / "addons" / f"{name}.py", root / "static" / "addons" / f"{name}.js",
           root / "tests" / f"{name}_addon_test.py"]
    if css:
        out.insert(2, root / "static" / "addons" / f"{name}.css")
    return out


def check(root, name, group):
    """None, or why the add-on can't be created."""
    if not NAME.match(name):
        return f"not an add-on name: {name!r} (lowercase, digits and _, starting with a letter, up to 32)"
    if group not in GROUPS:
        return f"group must be one of {', '.join(GROUPS)}, got {group!r}"
    taken = [str(p.relative_to(root)) for p in files(root, name, css=True) if p.exists()]
    if taken:
        return "already there: " + ", ".join(taken)
    for src, pat in (("ranch_radio/catalog.py", rf'"name":\s*"{name}"'), ("ranch_radio/manifest.py", rf'^\s*"{name}":\s*\{{')):
        p = Path(root) / src
        if p.exists() and re.search(pat, p.read_text(), re.M):
            return f"{src} already has a module {name!r}"
    return None


def create(root, name, label, title, group="tools", css=False):
    why = check(root, name, group)
    if why:
        raise ValueError(why)
    cls = class_name(name)
    texts = {
        f"addons/{name}.py": PY.format(name=name, cls=cls, label=label, title=title, group=group, css=css),
        f"static/addons/{name}.js": JS.format(name=name, label=label, title=title),
        f"tests/{name}_addon_test.py": TEST.format(name=name, label=label, title=title, group=group),
    }
    if css:
        texts[f"static/addons/{name}.css"] = CSS.format(name=name)
    written = []
    for rel, text in texts.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        written.append(rel)
    return written


def snippets(name, label, title):
    return SNIPPETS.format(name=name, label=label, title=title)


PY = '''"""{label} add-on: {title}.

Options (the host file's addons block):

    "{name}": {{"enabled": true}}

Say here what it does, what it needs on the machine, how it fails and what
the page shows. This docstring is the add-on's documentation: the catalog
entry points at this file.
"""
import logging

from . import Addon

log = logging.getLogger("addons.{name}")


class {cls}(Addon):
    name = "{name}"
    label = "{label}"
    title = "{title}"
    group = "{group}"            # radios | messaging | mesh | tools (static/nav.js)
    css = {css}                  # static/addons/{name}.css exists

    def __init__(self, host, opts):
        super().__init__(host, opts)
        # Check opts here and raise ValueError naming the field. A bad block
        # then shows as "not loaded", with the reason, and the server runs on.

    async def start(self):
        """Take the device and begin: switched on, or at startup."""

    async def stop(self):
        """Release everything: switched off, or at shutdown. Must not raise."""

    def snapshot(self):
        """Everything a page opened now needs; the page gets it as hello()."""
        return {{}}

    async def command(self, ws, verb, m):
        """A page's {{"t": "addon", "addon": "{name}", "do": verb, ...}}, only while on."""
        if verb == "ping":
            self.emit({{"event": "pong"}}, ws)
            return
        await super().command(ws, verb, m)     # "unknown command", as a notice

    def can_stop(self):
        """None, or why it can't be switched off right now (a session running)."""
        return None


ADDON = {cls}
'''

JS = '''"use strict";

// The {label} panel: {title}. The server side is addons/{name}.py; the
// registry (static/addons.js) gives this a panel with an on/off switch and
// routes its messages. Uses common.js (esc, toast).

(() => {{
  let body = null, snap = {{}}, api = null;

  function render() {{
    if (!body) return;
    body.innerHTML = `<div class="{name}">
        <div class="stat">${{esc(JSON.stringify(snap))}}</div>
        <button class="chip chip-mini {name}-ping">Ping</button>
      </div>`;
    body.querySelector(".{name}-ping").onclick = () => api.send("ping");
  }}

  addons.register("{name}", {{
    mount(b, a) {{ body = b; api = a; render(); }},          // once, when the panel is built
    hello(s) {{ snap = s || {{}}; render(); }},                // the server's snapshot (connect, switched on)
    event(m) {{ if (m.event === "pong") toast("{label}: pong"); }},
    off() {{ snap = {{}}; render(); }},                       // switched off: forget the device's state
    tab() {{ return {{ dot: "ok", title: "{title}" }}; }},    // the nav: dot ok|off|tx, badge, title
    card() {{ return {{ state: "on", lines: [] }}; }},        // the station's Overview card
  }});
}})();
'''

CSS = '''/* {name} add-on panel (loaded with static/addons/{name}.js) */
.{name} {{ display: grid; gap: 0.5rem; }}
'''

TEST = '''"""The {label} add-on: {title}.

  - a good block loads, on, in the {group} group, with no error, and starts
  - its snapshot is what a page needs
  - it is in the module catalog and the manifest (this FAILS until both
    entries are added: `ranch-radio new-addon` printed them)

    python3 tests/{name}_addon_test.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from addons import Addons  # noqa: E402
from ranch_radio import catalog, manifest  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{{'ok  ' if ok else 'FAIL'}} {{name}}" + ("" if ok else f"\\n     got:  {{got!r}}\\n     want: {{want!r}}"))


class Host:
    def notice(self, *a):
        pass

    def _broadcast(self, payload):
        pass

    async def _send(self, ws, payload):
        pass


reg = Addons(Host(), {{"{name}": {{"enabled": True}}}}, state_path=None)
d = {{x["name"]: x for x in reg.describe()}}["{name}"]
check("loads, on, in its group, no error", (d["label"], d["group"], d["on"], d["error"]), ("{label}", "{group}", True, None))
check("its snapshot", reg.snapshot()["{name}"], {{"on": True, "error": None}})
asyncio.run(reg.start())
check("starts", reg.items["{name}"]["running"], True)
asyncio.run(reg.stop())
check("stops", reg.items["{name}"]["running"], False)

check("in the catalog (ranch_radio/catalog.py)", "{name}" in catalog.by_name(), True)
check("in the manifest, an add-on in the addons block (ranch_radio/manifest.py)",
      (manifest.MANIFESTS.get("{name}") or {{}}).get("block"), ("addons", "{name}"))

print(f"\\n{{'all passed' if not fails else f'{{fails}} FAILED'}}")
sys.exit(1 if fails else 0)
'''

SNIPPETS = '''
Now add, by hand:

1. ranch_radio/catalog.py, in MODULES (the add-ons at the end):

    {{"name": "{name}", "kind": "addon", "title": "{title}",
     "code": "addons/{name}.py, static/addons/{name}.js",
     "platforms": ["macos", "linux"],
     "ships": "the host file's addons block"}},

2. ranch_radio/manifest.py, in MANIFESTS (fields the operator must give: ASK, with a rule):

    "{name}": {{
        "label": "{label}",
        "family": "addon",
        "block": ("addons", "{name}"),
        "os": ["macos", "linux"],
        "template": {{"any": {{"enabled": True}}}},
        "ask": {{}},
        "needs": [],
    }},

3. README.md, the Modules table: one row.
4. CHANGELOG.md, under Unreleased.

Then: python3 tests/{name}_addon_test.py   (passes once 1 and 2 are in)
      ranch-radio add {name} --apply && ranch-radio update --apply
'''
