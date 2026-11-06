"use strict";

// The tab bar, in groups (both pages: tabs.js on the radio page, station.js on
// the station page). Every radio, service and add-on belongs to one group:
//
//   Radios     the HF radios, the UV-PRO, the MD-UV390
//   Messaging  Winlink, Packet (APRS)
//   Mesh       Meshtastic, MeshCore
//   Tools      Propagation, and any add-on that names no group
//
// The bar grew a flat tab per add-on and got crowded (2026-11-05). Now the
// group holding the open view shows its members in place; every other group
// is one tab that opens the member last used there (kept in localStorage).
// A group with one member is just that member's tab, so a station with only a
// radio and Winlink looks as it always did.
//
// An add-on names its group in addons/<name>.py (Addon.group); the server
// lists it in config.addons. Uses common.js ($, esc).

const navGroups = (() => {
  const GROUPS = [
    { id: "radios", label: "Radios" },
    { id: "messaging", label: "Messaging" },
    { id: "mesh", label: "Mesh" },
    { id: "tools", label: "Tools" },
  ];
  const KNOWN = new Set(GROUPS.map((g) => g.id));
  const KEY = "rr-nav-last";
  let last = {};
  try { last = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch { last = {}; }

  function remember(group, id) {
    if (last[group] === id) return;
    last[group] = id;
    try { localStorage.setItem(KEY, JSON.stringify(last)); } catch { /* private mode: forget on reload */ }
  }

  const groupOf = (it) => (KNOWN.has(it.group) ? it.group : "tools");

  // {id, label, sub, dot, off, badge, title} -> <a class="st-tab">
  function tab(it, view, label = it.label) {
    const a = document.createElement("a");
    a.href = `#/${it.id}`;
    a.className = "st-tab" + (view === it.id ? " on" : "") + (it.off ? " off" : "");
    a.innerHTML = (it.dot ? `<i class="st-dot ${it.dot}"></i>` : "") + `<span>${esc(label)}</span>` +
      (it.sub ? `<small>${esc(it.sub)}</small>` : "") +
      (it.badge ? `<b class="st-badge">${it.badge}</b>` : "");
    if (it.title) a.title = it.title;
    return a;
  }

  // The collapsed group's dot: on the air beats connected beats off.
  function dotOf(members) {
    const dots = members.map((m) => m.dot || "");
    return dots.includes("tx") ? "tx" : dots.includes("ok") ? "ok" : dots.includes("off") ? "off" : "";
  }

  // Draw the bar. items: [{id, label, short?, group, dot, off, badge, title, sub}]
  // in the order wanted within each group; first/end: ungrouped tabs before and
  // after the groups (Overview, Modules). tuck: an item that is off leaves the
  // bar unless it is the open view. Returns the labels it tucked away.
  function render(nav, { view, first = [], items = [], end = [], tuck = true }) {
    nav.innerHTML = "";
    const tucked = [];
    for (const it of first) nav.append(tab(it, view));
    for (const g of GROUPS) {
      const members = items.filter((it) => groupOf(it) === g.id).filter((it) => {
        if (tuck && it.off && it.id !== view) { tucked.push(it.label); return false; }
        return true;
      });
      if (!members.length) continue;
      if (members.length === 1) { nav.append(tab(members[0], view)); continue; }
      const open = members.find((m) => m.id === view);
      if (open) {
        remember(g.id, open.id);
        const box = document.createElement("span");
        box.className = "st-group on";
        box.setAttribute("role", "group");
        box.setAttribute("aria-label", g.label);
        box.innerHTML = `<span class="st-group-name">${esc(g.label)}</span>`;
        for (const m of members) box.append(tab(m, view, m.short || m.label));
        nav.append(box);
      } else {
        const to = members.find((m) => m.id === last[g.id]) || members[0];
        const badge = members.reduce((n, m) => n + (parseInt(m.badge, 10) || 0), 0);
        const a = tab({ id: to.id, label: g.label, dot: dotOf(members), badge: badge || "",
          title: `${g.label}: ${members.map((m) => m.short || m.label).join(", ")}` }, view);
        a.classList.add("st-group-tab");
        a.dataset.group = g.id;
        nav.append(a);
      }
    }
    for (const it of end) nav.append(tab(it, view));
    return tucked;
  }

  // Order anything (overview cards) by group: radios first, tools last.
  const rank = (group) => GROUPS.findIndex((g) => g.id === (KNOWN.has(group) ? group : "tools"));

  return { render, rank, GROUPS };
})();
