"use strict";

// Single-radio page tabs: the radio, Winlink, Mesh and each add-on get their
// own view, as on the station page, grouped in the bar by nav.js. The bar appears only when this server runs
// Winlink, has a Meshtastic node (winlink.js / mesh.js un-hide their panels) or
// lists add-ons (addons.js), and never inside the station page's frame
// (station.js has its own tabs there).
// A module that is switched off, broken or not connected leaves the bar (the
// bar got crowded): the Modules tab lists it and switches it back on. Its tab
// stays while it is the open view, so it never vanishes from under you, and
// stays dimmed when there is no Modules tab to reach it from.
// Hiding a panel is only layout: transmit safety stays on the server.

(() => {
  if (document.documentElement.classList.contains("embedded")) return;
  const nav = $("#rr-nav");
  const wl = $("#winlink"), mesh = $("#mesh");
  const VIEWS = ["radio", "winlink", "mesh", "modules"];

  const addonNames = () => addons.list().map((d) => d.name);
  const on = (v) => (v === "winlink" ? !wl.hidden : v === "mesh" ? !mesh.hidden
    : v === "modules" ? modules.enabled()
    : VIEWS.includes(v) ? true : addonNames().includes(v));
  const route = () => {
    const r = location.hash.replace(/^#\/?/, "") || "radio";
    return (VIEWS.includes(r) || addonNames().includes(r)) && on(r) ? r : "radio";
  };

  function meshUnread() {
    let n = 0;
    for (const b of mesh.querySelectorAll("#mesh-tabs .mesh-unread")) n += parseInt(b.textContent, 10) || 0;
    return n;
  }

  function render() {
    const any = on("winlink") || on("mesh") || on("modules") || addonNames().length > 0;
    nav.hidden = !any;
    const view = any ? route() : "radio";
    document.body.dataset.view = view;
    const isAddon = addonNames().includes(view);
    document.body.classList.toggle("addon-view", isAddon);
    for (const p of document.querySelectorAll(".panel.addon")) p.classList.toggle("addon-current", p.dataset.addon === view);
    if (!any) return;
    // Grouped (nav.js): Radios, Messaging, Mesh, Tools; Modules last.
    const connected = !mesh.classList.contains("mesh-off");
    const items = [{ id: "radio", label: $(".brand-name").textContent || "Radio", group: "radios" }];
    if (on("winlink")) {
      items.push({ id: "winlink", label: "Winlink", group: "messaging",
        dot: wl.classList.contains("wl-armed") ? "tx" : "", title: "Winlink mail over HF (ARDOP)" });
    }
    if (on("mesh")) {
      items.push({ id: "mesh", label: "Mesh", short: "Meshtastic", group: "mesh", title: "Meshtastic node",
        dot: connected ? "ok" : "off", off: !connected, badge: view === "mesh" ? "" : meshUnread() || "" });
    }
    for (const d of addons.list()) items.push({ id: d.name, label: d.label, group: d.group, ...(addons.tab(d.name) || {}) });
    const tucked = navGroups.render(nav, { view, items, tuck: on("modules") });
    if (on("modules")) {
      nav.append(Object.assign(document.createElement("a"), {
        href: "#/modules", className: "st-tab" + (view === "modules" ? " on" : ""),
        title: "Every module: what's on, what it needs, add or remove" +
          (tucked.length ? `\nOff or not connected: ${tucked.join(", ")}` : ""),
        innerHTML: "<span>Modules</span>" }));
    }
  }

  // Re-render when a panel appears, Winlink arms, the node connects or a
  // message arrives. Batched to one render per frame.
  let queued = false;
  const later = () => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; render(); }); } };
  addons.setHost({ place: (name, panel) => $("main.grid").append(panel), changed: later });
  modules.mount($("#modules"), { changed: later, visible: () => route() === "modules" });
  const mo = new MutationObserver(later);
  for (const el of [wl, mesh]) mo.observe(el, { attributes: true, attributeFilter: ["hidden", "class"] });
  mo.observe($("#mesh-tabs"), { childList: true, subtree: true, characterData: true });
  mo.observe($(".brand-name"), { childList: true, characterData: true, subtree: true });
  window.addEventListener("hashchange", () => {
    render();
    window.scrollTo(0, 0);
    if (route() === "modules") modules.shown();
  });
  render();
})();
