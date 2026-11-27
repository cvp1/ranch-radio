"use strict";

// The tab bar, in groups (both pages: tabs.js on the radio page, station.js on
// the station page). Every radio, service and add-on belongs to one group:
//
//   Radios     the HF radios, the UV-PRO, the MD-UV390
//   Messaging  Winlink, Packet (APRS)
//   Mesh       Meshtastic, MeshCore, HaLow
//   Tools      Propagation, and any add-on that names no group
//
// The bar grew a flat tab per add-on and got crowded (2026-11-05). A group
// with more than one member is one split button (UX plan PR 2a, 2026-11-19):
// its label opens the member last used there (kept in localStorage); its
// chevron opens a menu of every member with its dot and badge. The group
// holding the open view reads "Tools · Map". Nothing expands in place, so the
// bar keeps the same items, in the same places, on every route.
// A group with one member is just that member's tab, so a station with only a
// radio and Winlink looks as it always did.
//
// The open view's group also gets a row of its members under the bar (sub:
// an element the page gives, the station page's #st-subnav; 2026-11-22: "it is
// difficult to move between options under each section, tools specifically"):
// one click from Map to Roster, without the chevron. The bar itself is still
// the same items in the same places.
//
// render() draws only when something drawn changed. The pages call it on every
// status change and add-on event (several a second: the JS8 waterfall), and a
// redraw replaces the tab under the pointer, so a click whose press and
// release straddled one went nowhere (measured 2026-11-22: 48 redraws in 10 s
// on the Shack).
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

  // The collapsed group's dot: on the air beats part-down (Mesh with one
  // node of several not connected) beats connected beats off.
  function dotOf(members) {
    const dots = members.map((m) => m.dot || "");
    return dots.includes("tx") ? "tx" : dots.includes("warn") ? "warn" : dots.includes("ok") ? "ok" : dots.includes("off") ? "off" : "";
  }

  // The group whose menu is open, if any. Kept here, not in the DOM: the pages
  // redraw the bar often (status changes), and an open menu must survive that.
  let openMenu = null;

  // Show the open menu (if any) under its chevron and hide the rest. The menu
  // is position: fixed because the bar scrolls sideways and would clip it.
  function applyMenus() {
    for (const m of document.querySelectorAll(".st-menu")) {
      const on = m.dataset.group === openMenu;
      m.hidden = !on;
      const chev = m.parentElement?.querySelector(".st-chev");
      chev?.setAttribute("aria-expanded", on ? "true" : "false");
      if (on && chev) {
        const r = chev.getBoundingClientRect(), w = Math.max(m.offsetWidth, 160);
        m.style.left = `${Math.max(8, Math.min(r.right - w, innerWidth - w - 8))}px`;
        m.style.top = `${r.bottom + 4}px`;
      }
    }
  }

  // One set of document listeners for every bar on the page: the chevron
  // toggles its menu; a member, an outside click, Escape or a scroll of the
  // bar closes it.
  document.addEventListener("click", (e) => {
    const chev = e.target.closest?.(".st-chev");
    if (chev) { openMenu = openMenu === chev.dataset.group ? null : chev.dataset.group; applyMenus(); return; }
    if (!openMenu) return;
    if (e.target.closest?.(".st-menu a") || !e.target.closest?.(".st-menu")) { openMenu = null; applyMenus(); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && openMenu) { openMenu = null; applyMenus(); }
  });
  document.addEventListener("scroll", () => { if (openMenu) { openMenu = null; applyMenus(); } }, true);

  // A member in a group's menu: the tab's dot, label and badge, but its own
  // class, so the bar's tab count stays one per group.
  function menuItem(m, view) {
    const a = tab(m, view, m.short || m.label);
    a.className = "st-mi" + (view === m.id ? " on" : "") + (m.off ? " off" : "");
    a.setAttribute("role", "menuitem");
    return a;
  }

  // Draw the bar. items: [{id, label, short?, group, dot, off, badge, title, sub}]
  // in the order wanted within each group; first/end: ungrouped tabs before and
  // after the groups (the Shack, Modules). tuck: an item that is off leaves the
  // bar unless it is the open view. Returns the labels it tucked away.
  // The last drawing of each bar: what it was drawn from, and what it tucked.
  const drawn = new WeakMap();

  function render(nav, { view, first = [], items = [], end = [], tuck = true, sub = null }) {
    const key = JSON.stringify([view, first, items, end, tuck, last]);
    const was = drawn.get(nav);
    if (was && was.key === key) return was.tucked.slice();
    nav.innerHTML = "";
    const tucked = [];
    let row = null;                       // the open view's group, for the sub row
    for (const it of first) nav.append(tab(it, view));
    for (const g of GROUPS) {
      const members = items.filter((it) => groupOf(it) === g.id).filter((it) => {
        if (tuck && it.off && it.id !== view) { tucked.push(it.label); return false; }
        return true;
      });
      if (!members.length) continue;
      if (members.length === 1) { nav.append(tab(members[0], view)); continue; }
      const open = members.find((m) => m.id === view);
      if (open) { remember(g.id, open.id); row = { g, members }; }
      const to = open || members.find((m) => m.id === last[g.id]) || members[0];
      const badge = members.reduce((n, m) => n + (parseInt(m.badge, 10) || 0), 0);
      const names = members.map((m) => m.short || m.label);
      const split = document.createElement("span");
      split.className = "st-split" + (open ? " on" : "");
      split.dataset.group = g.id;
      // Open, the button stands for that member: its title (a radio's channel,
      // say) and whether it is off; closed, for the group.
      const a = tab({ id: to.id, label: open ? `${g.label} · ${open.short || open.label}` : g.label,
        dot: dotOf(members), badge: badge || "", off: !!open?.off,
        title: open?.title || `${g.label}: ${names.join(", ")}` }, view);
      if (open) a.classList.add("on");
      a.classList.add("st-group-tab");
      a.dataset.group = g.id;
      const chev = document.createElement("button");
      chev.type = "button";
      chev.className = "st-chev";
      chev.dataset.group = g.id;
      chev.title = `All of ${g.label}`;
      chev.setAttribute("aria-label", `${g.label}: ${names.join(", ")}`);
      chev.setAttribute("aria-haspopup", "menu");
      const menu = document.createElement("div");
      menu.className = "st-menu";
      menu.dataset.group = g.id;
      menu.setAttribute("role", "menu");
      menu.setAttribute("aria-label", g.label);
      for (const m of members) menu.append(menuItem(m, view));
      split.append(a, chev, menu);
      nav.append(split);
    }
    for (const it of end) nav.append(tab(it, view));
    if (sub) {
      sub.innerHTML = "";
      sub.hidden = !row;
      if (row) {
        sub.setAttribute("aria-label", row.g.label);
        sub.dataset.group = row.g.id;
        for (const m of row.members) sub.append(tab(m, view, m.short || m.label));
      }
    }
    applyMenus();
    // Keyed after drawing: remember() above may have changed `last`.
    drawn.set(nav, { key: JSON.stringify([view, first, items, end, tuck, last]), tucked });
    return tucked.slice();
  }

  // Order anything (overview cards) by group: radios first, tools last.
  const rank = (group) => GROUPS.findIndex((g) => g.id === (KNOWN.has(group) ? group : "tools"));

  return { render, rank, GROUPS };
})();
