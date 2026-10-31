"use strict";

// Single-radio page tabs: the radio, Winlink, Mesh and each add-on get their
// own view, as on the station page. The bar appears only when this server runs
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

  function tab(id, label, opts = {}) {
    const a = document.createElement("a");
    a.href = `#/${id}`;
    a.className = "st-tab" + (route() === id ? " on" : "") + (opts.off ? " off" : "");
    a.innerHTML = (opts.dot ? `<i class="st-dot ${opts.dot}"></i>` : "") + `<span>${esc(label)}</span>` +
      (opts.badge ? `<b class="st-badge">${opts.badge}</b>` : "");
    if (opts.title) a.title = opts.title;
    return a;
  }

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
    nav.innerHTML = "";
    const tucked = [];
    const add = (id, label, opts = {}) => {
      if (opts.off && on("modules") && id !== view) tucked.push(label);
      else nav.append(tab(id, label, opts));
    };
    add("radio", $(".brand-name").textContent || "Radio");
    if (on("winlink")) {
      add("winlink", "Winlink", { dot: wl.classList.contains("wl-armed") ? "tx" : "",
        title: "Winlink mail over HF (ARDOP)" });
    }
    if (on("mesh")) {
      const connected = !mesh.classList.contains("mesh-off");
      add("mesh", "Mesh", { dot: connected ? "ok" : "off", off: !connected,
        badge: view === "mesh" ? "" : meshUnread() || "" });
    }
    for (const d of addons.list()) add(d.name, d.label, addons.tab(d.name) || {});
    if (on("modules")) {
      nav.append(tab("modules", "Modules", { title: "Every module: what's on, what it needs, add or remove" +
        (tucked.length ? `\nOff or not connected: ${tucked.join(", ")}` : "") }));
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
