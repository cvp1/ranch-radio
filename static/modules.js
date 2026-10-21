"use strict";

// The Modules tab (radio page on a Mac, station page on Linux): every module
// Ranch Radio has, its state here, what it still needs, and adding or removing
// one in the host file. The server (ranch_radio/pageapi.py) does every check;
// this only shows its answers and sends the operator's choices.
//
//   {"t":"modules"}            -> {"t":"modules", host, items, checked_at, checking}
//   {"t":"modules_check"}      Refresh: doctor runs again, every page gets the result
//   {"t":"addon", do:"power"}  the add-on registry's own switch (the same one its panel has)
//   {"t":"module_plan", do, module, set} -> {"t":"module_plan", id, diff, refusal, errors, ...}
//   {"t":"module_apply", plan}           -> {"t":"module_applied", ok, refusal, next}
//
// Applying writes only the host file. Installing and restarting stay at the
// machine: the page shows the exact commands. Uses common.js ($, esc, toast)
// and the page's send().

const modules = (() => {
  let el = null, on = false, data = null, plan = null, applied = null, form = null;
  let host = { changed: () => {}, visible: () => true };
  const GROUPS = [
    ["attention", "Needs attention"],
    ["on", "On this host"],
    ["off", "Switched off"],
    ["available", "Available to add"],
  ];
  const STATE = {
    on: ["On", "ok"], off: ["Off", "off"], broken: ["Broken", "bad"], pending: ["Not running yet", "warn"],
    removed: ["Removed, still running", "warn"], installed: ["Installed", "ok"],
    install_pending: ["Install pending", "warn"], configured: ["Configured", "ok"],
    available: ["Available", "idle"], unsupported: ["Not for this OS", "idle"],
  };
  const MARK = { ok: "✓", missing: "✗", warn: "!", unchecked: "…" };
  // A fix that is a command to run (shown with Copy), not advice in words.
  const CMD = /^(\.\/|~\/|\/|sudo |python3? |brew |apt |pip )/;

  function group(i) {
    const bad = (i.needs || []).some((c) => c.status === "missing");
    if (["broken", "pending", "removed", "install_pending"].includes(i.state) || (bad && i.state !== "available")) return "attention";
    if (i.state === "off") return "off";
    if (i.state === "available") return "available";
    if (i.state === "unsupported") return "unsupported";
    return "on";
  }

  function ago(ts) {
    if (!ts) return "not checked yet";
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    return s < 60 ? "checked just now" : s < 3600 ? `checked ${Math.round(s / 60)} min ago` : `checked ${Math.round(s / 3600)} h ago`;
  }

  function copyable(text) {
    const id = `mc-${Math.random().toString(36).slice(2)}`;
    return `<span class="mod-cmd"><code id="${id}">${esc(text)}</code>` +
      (navigator.clipboard ? `<button class="chip chip-mini mod-copy" data-copy="${id}" title="Copy">Copy</button>` : "") + `</span>`;
  }

  function needsHtml(needs) {
    if (!needs || !needs.length) return "";
    return `<ul class="mod-needs">` + needs.map((c) =>
      `<li class="mod-need ${esc(c.status)}"><b>${MARK[c.status] || "?"}</b> ${esc(c.what)}` +
      (c.fix && c.status !== "ok" ? `<div class="mod-fix">${CMD.test(c.fix) ? copyable(c.fix) : esc(c.fix)}</div>` : "") +
      `</li>`).join("") + `</ul>`;
  }

  function addForm(i) {
    const asks = i.asks || [];
    return `<div class="mod-form" data-module="${esc(i.name)}">` +
      (asks.length ? asks.map((a) => `<label class="mod-field"><span>${esc(a.field)}</span>` +
        `<input name="${esc(a.field)}" autocomplete="off" spellcheck="false" placeholder="${esc(a.rule.replace(/_/g, " "))}">` +
        `<small>${esc(a.help)}</small><em class="mod-err" data-err="${esc(a.field)}"></em></label>`).join("")
        : `<p class="mod-note">Nothing to fill in: it starts from its defaults.</p>`) +
      `<p class="mod-note">Paths, ports and transmit settings come from its defaults. Change those at the machine.</p>` +
      `<div class="mod-actions"><button class="chip mod-plan" data-do="add">Show the change</button>` +
      `<button class="chip chip-mini mod-cancel">Cancel</button></div></div>`;
  }

  function planHtml(p) {
    if (p.refusal) {
      return `<div class="mod-plan-box refused"><b>Can't ${esc(p.do)} ${esc(p.module)}:</b> ${esc(p.refusal)}` +
        `<div class="mod-actions"><button class="chip chip-mini mod-cancel">Close</button></div></div>`;
    }
    return `<div class="mod-plan-box"><div class="label">The change to hosts/${esc(data.host)}.json</div>` +
      `<pre class="mod-diff">${esc(p.diff || "")}</pre>` +
      needsHtml(p.checks) +
      (p.orphans && p.orphans.length ? `<p class="mod-note">After install, these are no longer rendered; ` +
        `install leaves them in place, so take them out at the machine:</p>` + p.orphans.map(copyable).join("") : "") +
      `<p class="mod-note">Applying writes the host file only. Nothing is installed or restarted.</p>` +
      `<div class="mod-actions"><button class="chip mod-apply">Apply to the host file</button>` +
      `<button class="chip chip-mini mod-cancel">Cancel</button></div></div>`;
  }

  function appliedHtml(a) {
    if (!a.ok) {
      return `<div class="mod-plan-box refused"><b>Not applied:</b> ${esc(a.refusal || "")}` +
        `<div class="mod-actions"><button class="chip chip-mini mod-cancel">Close</button></div></div>`;
    }
    return `<div class="mod-plan-box done"><b>${esc(a.module)}: ${a.do === "add" ? "added to" : "removed from"} ` +
      `hosts/${esc(data.host)}.json.</b> To make it take effect, on ${esc(data.host)}:` +
      (a.next || []).map(copyable).join("") +
      (a.note ? `<p class="mod-note">${esc(a.note)}</p>` : "") +
      (a.orphans && a.orphans.length ? `<p class="mod-note">Then take out what install leaves behind:</p>` +
        a.orphans.map(copyable).join("") : "") +
      `<div class="mod-actions"><button class="chip chip-mini mod-cancel">Done</button></div></div>`;
  }

  function row(i) {
    const [label, tone] = STATE[i.state] || [i.state, "idle"];
    const sw = i.switch && i.switch.loaded && !["broken", "removed"].includes(i.state)
      ? `<button class="chip chip-mini mod-power" data-name="${esc(i.name)}" data-on="${i.switch.on ? 1 : 0}">${i.switch.on ? "Switch off" : "Switch on"}</button>` : "";
    const canRemove = !["core", "radio"].includes(i.kind) && !["available", "unsupported"].includes(i.state);
    const busy = plan || form || applied;
    const actions = i.state === "available"
      ? `<button class="chip chip-mini mod-add" data-name="${esc(i.name)}"${busy ? " disabled" : ""}>Add…</button>`
      : canRemove ? `<button class="chip chip-mini mod-remove" data-name="${esc(i.name)}"${busy ? " disabled" : ""}>Remove…</button>` : "";
    let below = "";
    if (form === i.name) below = addForm(i);
    else if (plan && plan.module === i.name) below = planHtml(plan);
    else if (applied && applied.module === i.name) below = appliedHtml(applied);
    return `<div class="mod-row ${esc(i.state)}" data-name="${esc(i.name)}">` +
      `<div class="mod-head"><span class="mod-title"${i.about ? ` title="${esc(i.about)}"` : ""}>${esc(i.title)}</span>` +
      `<span class="mod-state ${tone}">${esc(label)}</span><span class="spacer"></span>${sw}${actions}</div>` +
      (i.detail ? `<div class="mod-detail">${esc(i.detail)}</div>` : "") +
      needsHtml(i.needs) +
      (i.state === "available" && i.command && form !== i.name ? `<div class="mod-fix">At the machine: ${copyable(i.command)}</div>` : "") +
      below + `</div>`;
  }

  function render() {
    if (!el) return;
    const body = el.querySelector(".mod-body");
    if (!data) { body.innerHTML = `<div class="st-empty">Loading…</div>`; return; }
    el.querySelector(".mod-host").textContent = data.host ? `on ${data.host}` : "";
    el.querySelector(".mod-when").textContent = data.checking ? "checking this machine…" : ago(data.checked_at);
    el.querySelector(".mod-refresh").disabled = !!data.checking;
    if (data.error) { body.innerHTML = `<div class="st-empty">${esc(data.error)}</div>`; return; }
    const by = {};
    for (const i of data.items || []) (by[group(i)] = by[group(i)] || []).push(i);
    let html = "";
    for (const [g, title] of GROUPS) {
      if (!by[g]) continue;
      html += `<div class="mod-group"><div class="label">${esc(title)}</div>${by[g].map(row).join("")}</div>`;
    }
    if (by.unsupported) {
      html += `<details class="mod-group"><summary class="label">Not for this OS (${by.unsupported.length})</summary>` +
        by.unsupported.map(row).join("") + `</details>`;
    }
    // A push (doctor done, a switch flipped elsewhere) must not wipe what the
    // operator is typing into an add form.
    const typed = {};
    for (const inp of body.querySelectorAll(".mod-form input[name]")) typed[inp.name] = inp.value;
    const focused = document.activeElement && document.activeElement.name;
    body.innerHTML = html;
    for (const inp of body.querySelectorAll(".mod-form input[name]")) {
      if (inp.name in typed) inp.value = typed[inp.name];
      if (inp.name === focused) inp.focus();
    }
    host.changed();
  }

  function request() { if (on) send({ t: "modules" }); }

  function collect(f) {
    const set = {};
    for (const inp of f.querySelectorAll("input[name]")) set[inp.name] = inp.value.trim();
    return set;
  }

  function click(e) {
    const b = e.target.closest("button");
    if (!b || b.disabled) return;
    if (b.classList.contains("mod-copy")) {
      const t = $(`#${b.dataset.copy}`);
      navigator.clipboard.writeText(t.textContent).then(() => toast("Copied."), () => toast("Couldn't copy.", "warn"));
    } else if (b.classList.contains("mod-refresh")) {
      send({ t: "modules_check" });
      if (data) { data.checking = true; render(); }
    } else if (b.classList.contains("mod-power")) {
      const want = b.dataset.on !== "1";
      if (!want && !confirm(`Switch ${b.dataset.name} off? Its link to the device is closed until you switch it on again.`)) return;
      send({ t: "addon", addon: b.dataset.name, do: "power", on: want });
    } else if (b.classList.contains("mod-add")) {
      form = b.dataset.name; plan = null; applied = null; render();
      const first = el.querySelector(".mod-form input");
      if (first) first.focus();
    } else if (b.classList.contains("mod-remove")) {
      form = null; applied = null;
      send({ t: "module_plan", do: "remove", module: b.dataset.name, set: {} });
    } else if (b.classList.contains("mod-plan")) {
      const f = b.closest(".mod-form");
      send({ t: "module_plan", do: "add", module: f.dataset.module, set: collect(f) });
    } else if (b.classList.contains("mod-apply")) {
      b.disabled = true;
      send({ t: "module_apply", plan: plan.id });
    } else if (b.classList.contains("mod-cancel")) {
      form = null; plan = null; applied = null; render();
    }
  }

  function mount(container, h = {}) {
    el = container;
    Object.assign(host, h);
    el.innerHTML = `<div class="wl-head mod-top"><span class="label">Modules</span>` +
      `<span class="stat mod-host"></span><span class="spacer"></span><span class="stat mod-when"></span>` +
      `<button class="chip chip-mini mod-refresh" title="Check this machine again (programs, Python packages, devices)">Refresh</button></div>` +
      `<div class="mod-body"></div>`;
    el.addEventListener("click", click);
    render();
  }

  return {
    mount,
    enabled: () => on,
    hello(cfg) {
      on = !!(cfg && cfg.modules);
      if (el) el.hidden = !on;
      if (on && host.visible()) request();
      host.changed();
    },
    shown() { request(); },
    event(m) {
      if (m.t === "modules") {
        data = m;
      } else if (m.t === "module_plan") {
        if (m.errors && Object.keys(m.errors).length) {
          for (const e of el.querySelectorAll(".mod-err")) e.textContent = m.errors[e.dataset.err] || "";
          return;
        }
        plan = m; form = null; applied = null;
      } else if (m.t === "module_applied") {
        applied = m; plan = null;
      }
      render();
    },
    addonChanged() { if (on && data && host.visible()) request(); },
  };
})();
