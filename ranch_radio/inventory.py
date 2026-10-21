"""What the page's Modules tab shows: every module, its state, what it needs.

    build(h, runtime, files, checks) -> [item]

Pure: the caller gathers
    h        the host file (render.load)
    runtime  {name: {"on", "error", "loaded"}} from the process's Addons.items
             (not describe(): Mesh and Winlink have no panel and are missing there)
    files    [(artifact, path, state)] from render.installed_states
    checks   doctor.static(h) + whatever doctor.machine last found (may be stale)

Two families of state, so nothing claims what its process can't know (review
2026-10-21, Grok G2):
    add-ons this process runs     on | off | broken | pending | removed
    services and integrations     installed | install_pending | configured
    either                        available | unsupported
An add-on with an error is broken whatever its switch says (G3).
"""

import rigs

from . import catalog
from .doctor import OK, UNCHECKED, block_of, on_modules
from .manifest import ASK, MANIFESTS, asked, fields, template

DETAIL = {
    "pending": "In the host file, but not running in this server. Install, then restart it.",
    "removed": "Running, but no longer in the host file. It stops at the next restart.",
    "install_pending": "The installed files differ from the host file. Run install.",
    "configured": "In the host file; installed by its own script.",
    "available": "Not on this host.",
}


def _checks_for(checks, name):
    return [{"status": c.status, "what": c.what, "fix": c.fix} for c in checks if c.module == name]


def _files_state(files, name):
    mine = [s for art, _, s in files if name in art.module.split(",")]
    if not mine:
        return "configured"
    return "installed" if all(s == "same" for s in mine) else "install_pending"


def _command(h, name):
    sets = " ".join(f"--set {f}=..." for f in asked(template(name, h["os"], on_modules(h)) or {}))
    return f"./ranch-radio add {name} --host {h['host']}" + (f" {sets}" if sets else "") + " --apply"


def _asks(h, name):
    m = MANIFESTS[name]
    block = template(name, h["os"], on_modules(h)) or {}
    rules = m.get("rules") or {}
    return [{"field": f, "help": (m.get("ask") or {}).get(f, ""), "rule": rules.get(f, "text")}
            for f in asked(block)]


def build(h, runtime, files, checks):
    items = []
    title = {m["name"]: m["title"] for m in catalog.MODULES}

    core = _checks_for(checks, "core")
    items.append({"name": "core", "kind": "core", "family": "core", "title": "Ranch Radio core",
                  "state": "installed" if all(c["status"] in (OK, UNCHECKED, "warn") for c in core)
                  else "broken", "detail": "", "switch": None, "needs": core})

    for r in h["radios"]:
        rig = r.get("rig", "ic7300")
        prof = rigs.PROFILES.get(rig, {})
        state = _files_state(files, r["id"])
        items.append({"name": r["id"], "kind": "radio", "family": "files",
                      "title": r["name"] if prof.get("name", rig) in (r["name"], f"ICOM {r['name']}")
                      else f"{r['name']} ({prof.get('name', rig)})",
                      "about": "" if prof.get("tested") else "Profile built from the references; not yet run on a real radio.",
                      "state": state, "detail": DETAIL.get(state, ""), "switch": None,
                      "needs": _checks_for(checks, r["id"])})

    present = set(on_modules(h))
    for name, m in MANIFESTS.items():
        item = {"name": name, "kind": catalog.by_name().get(name, {}).get("kind", "addon"),
                "family": m["family"], "title": m.get("label") or title.get(name, name),
                "about": title.get(name, ""), "switch": None,
                "needs": _checks_for(checks, name), "command": None, "asks": None, "fields": None}
        rt = runtime.get(name)
        if h["os"] not in m["os"] and name not in present:
            item["state"], item["detail"] = "unsupported", f"Ships on {', '.join(m['os'])}."
        elif m["family"] == "addon" and rt is not None:
            enabled = bool((block_of(h, name) or {}).get("enabled", True)) if name in present else None
            if rt.get("error"):
                state, detail = "broken", rt["error"]
            elif rt.get("on"):
                state, detail = "on", ""
            else:
                state = "off"
                detail = ("Switched off on the page. The host file says on; the page's switch wins "
                          "until you switch it on." if enabled else "Switched off.")
            if name not in present:
                state, detail = "removed", DETAIL["removed"]
            item.update(state=state, detail=detail,
                        switch={"on": bool(rt.get("on")), "loaded": bool(rt.get("loaded")),
                                "enabled": enabled})
        elif name in present:
            state = "pending" if m["family"] == "addon" else _files_state(files, name)
            item.update(state=state, detail=DETAIL.get(state, ""))
        else:
            item.update(state="available", detail=DETAIL["available"], command=_command(h, name),
                        asks=_asks(h, name), fields=fields(template(name, h["os"], present) or {}))
        items.append(item)
    return items


def report(h, runtime, files, checks, checked_at=None, checking=False):
    """The {"t": "modules"} message."""
    return {"t": "modules", "host": h["host"], "os": h["os"], "host_file": True,
            "items": build(h, runtime, files, checks),
            "checked_at": checked_at, "checking": checking}


__all__ = ["build", "report", "ASK"]
