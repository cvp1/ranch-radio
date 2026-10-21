"""Adding and removing a module in a host file: one implementation for the
CLI (`ranch-radio add` / `remove`) and the page's Modules tab.

    plan_add(text, name, sets, source)  -> Plan
    plan_remove(text, name)             -> Plan
    apply(path, plan, state_path)       -> None, or why not

A Plan is the whole proposed change: the new text, its diff, the checks, the
files install will leave behind, and a refusal when it can't be done. apply
writes it under the one host-file lock, and only if the file is still the text
the plan was made from (base_sha). Nothing here installs or restarts.

What the page may set is narrower than the CLI (review 2026-10-21, G1/G5):
only a module's ASK fields, each checked against its typed rule in the
manifest. Paths, ports, labels, transmit and every other default come from
the template, and Winlink's lanes are assigned here.
"""

import difflib
import hashlib
import json
import os
import shlex
import time
from dataclasses import dataclass, field

from . import doctor, hostfile, hostlock, manifest, render
from .render import HostError, artifacts


@dataclass
class Plan:
    module: str
    do: str                                   # add | remove
    host: str = ""
    new_text: str = ""
    diff: str = ""
    base_sha: str = ""
    checks: list = field(default_factory=list)
    orphans: list = field(default_factory=list)   # commands that take out what install leaves
    asks: list = field(default_factory=list)      # ASK fields still unanswered
    errors: dict = field(default_factory=dict)    # field -> why its value was refused (page)
    refusal: str = ""
    notes: list = field(default_factory=list)

    def public(self):
        """What a page sees (not the full new text)."""
        return {"module": self.module, "do": self.do, "diff": self.diff,
                "checks": [{"status": c.status, "what": c.what, "fix": c.fix} for c in self.checks],
                "orphans": self.orphans, "asks": self.asks, "errors": self.errors,
                "refusal": self.refusal, "notes": self.notes}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _diff(host, old, new):
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        f"hosts/{host}.json", f"hosts/{host}.json (new)"))


def free_ports(h, n, start=4537):
    """n ports no radio or Winlink block of h already uses, from start up."""
    used = set()
    for r in h["radios"]:
        used |= {r.get(k) for k in ("web", "rigctl", "lane")}
    used |= {v for v in (h.get("winlink") or {}).values() if isinstance(v, int)}
    out, p = [], start
    while len(out) < n:
        if p not in used:
            out.append(p)
        p += 1
    return out


def _value(s):
    """A CLI --set value: JSON when it parses (numbers, true, lists), else the text."""
    try:
        return json.loads(s)
    except ValueError:
        return s


def _set_cli(block, key, val, name):
    """--set: only the template's own fields (a key it lacks, like a mesh
    "python", would name a program to run; add those by hand)."""
    cur = block
    parts = key.split(".")
    for k in parts[:-1]:
        if not isinstance(cur.get(k), dict):
            return f"{name} has no field {key!r} (fields: {', '.join(manifest.fields(block))})"
        cur = cur[k]
    if parts[-1] not in cur or isinstance(cur[parts[-1]], dict):
        return f"{name} has no field {key!r} (fields: {', '.join(manifest.fields(block))})"
    cur[parts[-1]] = _value(val) if isinstance(val, str) else val
    return None


def _set_page(block, key, val, name, m):
    """From the page: an ASK field only, its value fitting the field's rule."""
    if key not in manifest.asked(block):
        return "not settable from the page (set it at the machine)"
    rule = (m.get("rules") or {}).get(key)
    if rule is None:
        return "this field has no rule for the page; set it at the machine"
    val = manifest.normalize(rule, val)
    why = manifest.check_ask(rule, val)
    if why:
        return why
    cur = block
    parts = key.split(".")
    for k in parts[:-1]:
        cur = cur[k]
    cur[parts[-1]] = val
    return None


def plan_add(text, name, sets, source="cli"):
    """sets: [(field, value)] (CLI strings) or {field: value} (page)."""
    p = Plan(module=name, do="add", base_sha=sha(text))
    m = manifest.MANIFESTS.get(name)
    if m is None:
        p.refusal = f"{name!r} is not a module you can add (these are: {', '.join(manifest.MANIFESTS)})"
        return p
    try:
        h = json.loads(text)
        render.validate(h)
    except (ValueError, HostError) as e:
        p.refusal = f"the host file doesn't load: {e}"
        return p
    p.host = h["host"]
    present = doctor.on_modules(h)
    if name in present:
        p.refusal = f"{name} is already on {h['host']}"
        return p
    if h["os"] not in m["os"]:
        p.refusal = f"{name} ships on {', '.join(m['os'])}, not on {h['host']} ({h['os']})"
        return p
    missing = [d for d in m.get("requires", []) if d not in present]
    if missing:
        p.refusal = f"{name} needs {', '.join(missing)} first"
        return p
    block = manifest.template(name, h["os"], present)
    items = sets.items() if isinstance(sets, dict) else sets
    for key, val in items:
        why = _set_page(block, key, val, name, m) if source == "page" else _set_cli(block, key, val, name)
        if why:
            p.errors[key] = why
    if p.errors:
        return p
    p.asks = manifest.asked(block)
    if p.asks:
        if source == "page":
            p.errors = {f: "required" for f in p.asks}
        return p
    try:
        new = hostfile.put(text, m["block"], block)
        for fld, how in (m.get("radios") or {}).items():
            need = [i for i, r in enumerate(h["radios"]) if not r.get(fld)]
            ports = free_ports(h, len(need)) if how == "free_port" else []
            for i, port in zip(need, ports):
                new = hostfile.put(new, ("radios", i, fld), port)
                p.notes.append(f"{h['radios'][i]['id']} gets {fld} {port}")
        new_h = json.loads(new)
        render.validate(new_h)
        artifacts(new_h)                      # it must render, too
    except (hostfile.EditError, HostError, ValueError, KeyError) as e:
        p.refusal = str(e)
        return p
    p.new_text, p.diff = new, _diff(h["host"], text, new)
    p.checks = doctor.module_checks(new_h, name, local=False)
    return p


def leaning_on(h, name):
    present = doctor.on_modules(h)
    out = []
    for n in present:
        mn = manifest.MANIFESTS[n]
        if name in mn.get("requires", []):
            out.append(n)
        # A block that took this module's extras at add time (uvpro's "hub")
        # leans on it as surely as a declared requirement.
        extra = (mn.get("with") or {}).get(name) or {}
        blk = doctor.block_of(h, n) or {}
        if any(k in blk for k in extra):
            out.append(f"{n} (its {', '.join(k for k in extra if k in blk)})")
    return out


def orphan_commands(old_h, new_h):
    """Files install will no longer render but leaves in place, as commands
    that take them out (quoted: meant to be pasted)."""
    gone = sorted({x.path for x in artifacts(old_h, home="/HOME")}
                  - {x.path for x in artifacts(new_h, home="/HOME")})
    out = []
    for p in gone:
        name = p.rsplit("/", 1)[-1]
        if p.startswith(render.AGENTS):
            out.append(f"launchctl bootout gui/$(id -u)/{shlex.quote(name[:-len('.plist')])}; "
                       f"mv ~/{shlex.quote(p)} ~/.Trash/")
        elif p.startswith(render.UNIT_DIR):
            out.append(f"systemctl --user disable --now {shlex.quote(name)}; mv ~/{shlex.quote(p)} ~/.Trash/")
        else:
            out.append(f"mv ~/{shlex.quote(p)} ~/.Trash/")
    return out


def plan_remove(text, name):
    p = Plan(module=name, do="remove", base_sha=sha(text))
    m = manifest.MANIFESTS.get(name)
    if m is None:
        p.refusal = f"{name!r} is not a module you can remove (these are: {', '.join(manifest.MANIFESTS)})"
        return p
    try:
        h = json.loads(text)
        render.validate(h)
    except (ValueError, HostError) as e:
        p.refusal = f"the host file doesn't load: {e}"
        return p
    p.host = h["host"]
    if name not in doctor.on_modules(h):
        p.refusal = f"{name} is not on {h['host']}"
        return p
    leaning = leaning_on(h, name)
    if leaning:
        p.refusal = f"{', '.join(leaning)} leans on {name}; remove or change that first"
        return p
    try:
        new = hostfile.drop(text, m["block"])
        new_h = json.loads(new)
        render.validate(new_h)
    except (hostfile.EditError, HostError, ValueError) as e:
        p.refusal = str(e)
        return p
    p.new_text, p.diff = new, _diff(h["host"], text, new)
    p.orphans = orphan_commands(h, new_h)
    if m.get("radios"):
        p.notes.append(f"Each radio keeps its {', '.join(m['radios'])} port: the transmit lane is core.")
    return p


def apply(path, plan, state_path=None):
    """Write plan into the host file at path under the one lock. None when done,
    else why not (the file changed since the plan, or the plan can't apply).
    Adding a module also drops a stale run-time "off" for it from the add-on
    state file, so the host file's enabled wins at the next start (G3)."""
    if plan.refusal or plan.errors or plan.asks or not plan.new_text:
        return plan.refusal or "the plan isn't complete"
    path = str(path)
    with hostlock.locked(os.path.dirname(path)):
        with open(path) as f:
            now = f.read()
        if sha(now) != plan.base_sha:
            return "the host file changed since this plan was made; plan it again"
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            f.write(plan.new_text)
        os.replace(tmp, path)
    if plan.do == "add" and state_path and manifest.MANIFESTS[plan.module]["family"] == "addon":
        _clear_state(os.path.expanduser(state_path), plan.module)
    return None


def _clear_state(state_path, name):
    try:
        with open(state_path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return
    if isinstance(d, dict) and name in d:
        del d[name]
        tmp = f"{state_path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(tmp, state_path)


def next_commands(host, os_name):
    """What makes an applied change take effect, at the machine (never drift
    first: it fails by design until install has run, N1)."""
    return [f"./ranch-radio install {host}", f"./ranch-radio install {host} --apply"]


def next_note(os_name):
    return ("Then run the reload lines install prints" +
            (", on the Mac itself, not over SSH." if os_name == "macos" else "."))


class Plans:
    """The page's plans, by id: one per page, kept for ten minutes."""
    TTL_S = 600

    def __init__(self, state_path=None):
        self.by_id = {}
        self.state_path = state_path

    def _sweep(self):
        now = time.monotonic()
        for k in [k for k, (_, _, t) in self.by_id.items() if now - t > self.TTL_S]:
            del self.by_id[k]

    async def handle(self, svc, ws, m):
        import asyncio
        self._sweep()
        if m["t"] == "module_plan":
            do, name = m.get("do"), str(m.get("module", ""))
            sets = m.get("set") or {}
            if not isinstance(sets, dict) or len(sets) > 16:
                raise ValueError("set must be an object of at most 16 fields")

            def make():
                text = svc.path.read_text()
                if do == "add":
                    return plan_add(text, name, sets, source="page")
                if do == "remove":
                    return plan_remove(text, name)
                return Plan(module=name, do=str(do), refusal="do must be add or remove")

            plan = await asyncio.to_thread(make)
            for k in [k for k, (w, _, _) in self.by_id.items() if w is ws]:
                del self.by_id[k]                 # one plan per page
            pid = os.urandom(8).hex()
            if not (plan.refusal or plan.errors or plan.asks):
                self.by_id[pid] = (ws, plan, time.monotonic())
            await svc.send(ws, json.dumps({"t": "module_plan", "id": pid, **plan.public()}))
        else:
            pid = str(m.get("plan", ""))
            entry = self.by_id.pop(pid, None)
            if entry is None or entry[0] is not ws:
                why = "that plan is gone (ten minutes, or another plan replaced it); plan it again"
                await svc.send(ws, json.dumps({"t": "module_applied", "ok": False, "refusal": why}))
                return
            plan = entry[1]
            reg = svc.registry()
            state = self.state_path or getattr(reg, "state_path", None)
            why = await asyncio.to_thread(apply, svc.path, plan, state)
            os_name = json.loads(plan.new_text).get("os", "")
            await svc.send(ws, json.dumps({"t": "module_applied", "ok": why is None, "refusal": why,
                                           "module": plan.module, "do": plan.do,
                                           "next": next_commands(svc.host, os_name), "note": next_note(os_name),
                                           "orphans": plan.orphans}))
            await svc.push()
