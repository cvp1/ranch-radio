"use strict";

// Add-ons on a page (the radio page and the station page). The server lists
// its add-ons in config.addons ([{name, label, title, css, on, error}]); this
// loads each one's static/addons/<name>.js (and <name>.css), gives it a panel
// with its name and an on/off switch, and routes {"t": "addon"} messages to it.
//
// An add-on script calls addons.register(name, impl), where impl may have:
//   mount(body, api)   build the panel's content once (api.send(verb, extra))
//   hello(snapshot)    the server's state for it (on connect, and when switched on)
//   event(m)           a {"t": "addon", addon: name, ...} message
//   off()              it was switched off: forget the device's state
//   tab()              {dot: "ok"|"off"|"tx", off: bool, badge, title} for the nav
//   card()             {state, lines: [html]} for the station's Overview
// The page (tabs.js / station.js) sets a host: {place(name, panel), changed()}.
// Uses common.js ($, esc, toast) and the page's send().

const addons = (() => {
  const desc = {}, impl = {}, snaps = {}, panels = {}, loaded = {};
  // Messages for an add-on whose script hasn't registered yet: they are
  // newer than its hello snapshot, so register() replays them after it.
  // Dropping them left a panel on the snapshot (UV-PRO connected while its
  // script loaded: no battery, no channels until a reload; 2026-10-30).
  const early = {}, EARLY_MAX = 500;
  let host = { place: () => {}, changed: () => {} };

  const api = (name) => ({
    send: (verb, extra = {}) => send({ t: "addon", addon: name, do: verb, ...extra }),
    changed: () => host.changed(),
  });

  function load(d) {
    if (loaded[d.name]) return;
    loaded[d.name] = true;
    if (d.css) {
      const l = document.createElement("link");
      l.rel = "stylesheet";
      l.href = `static/addons/${encodeURIComponent(d.name)}.css`;
      document.head.append(l);
    }
    const s = document.createElement("script");
    s.src = `static/addons/${encodeURIComponent(d.name)}.js`;
    s.onerror = () => { desc[d.name].error = "page script missing"; renderHead(d.name); host.changed(); };
    document.head.append(s);
  }

  function panel(d) {
    if (panels[d.name]) return panels[d.name];
    const p = document.createElement("section");
    p.className = `panel addon addon-${d.name}`;
    p.dataset.addon = d.name;
    p.innerHTML = `<div class="wl-head addon-head">
        <span class="label">${esc(d.label)}</span><span class="addon-state stat"></span>
        <span class="spacer"></span>
        <button class="chip chip-mini addon-power"></button>
      </div>
      <div class="addon-msg st-empty" hidden></div>
      <div class="addon-body"></div>`;
    p.querySelector(".addon-power").onclick = () => {
      const want = !desc[d.name].on;
      if (!want && !confirm(`Switch ${d.label} off? Its link to the device is closed until you switch it on again.`)) return;
      send({ t: "addon", addon: d.name, do: "power", on: want });
    };
    panels[d.name] = p;
    host.place(d.name, p);
    return p;
  }

  function renderHead(name) {
    const d = desc[name], p = panels[name];
    if (!d || !p) return;
    const btn = p.querySelector(".addon-power");
    btn.textContent = d.on ? "Switch off" : "Switch on";
    btn.title = d.on ? `Close ${d.label}'s link and stop it` : `Start ${d.label}`;
    btn.disabled = !!d.error && !d.on && d.error.startsWith("not loaded");
    const msg = p.querySelector(".addon-msg"), body = p.querySelector(".addon-body");
    const text = d.error ? `${d.label}: ${d.error}` : !d.on ? `${d.label} is switched off. Its device is free for other apps.` : "";
    msg.textContent = text;
    msg.hidden = !text;
    body.hidden = !d.on || !impl[name];
    p.classList.toggle("addon-off", !d.on);
  }

  function hello(list, snapshots) {
    for (const d of list || []) {
      desc[d.name] = { ...d };
      snaps[d.name] = (snapshots || {})[d.name] || null;
      delete early[d.name];            // a fresh snapshot supersedes them
      panel(d);
      renderHead(d.name);
      load(d);
      if (impl[d.name] && d.on && snaps[d.name]) impl[d.name].hello?.(snaps[d.name]);
    }
    host.changed();
  }

  function event(m) {
    const name = m.addon, d = desc[name];
    if (!d) return;
    if ("on" in m) {
      d.on = !!m.on;
      d.error = m.error || null;
      if (d.on && m.snapshot) { snaps[name] = m.snapshot; delete early[name]; impl[name]?.hello?.(m.snapshot); }
      if (!d.on) impl[name]?.off?.();
      renderHead(name);
    } else if (impl[name]) {
      impl[name].event?.(m);
    } else {
      const q = (early[name] ||= []);
      q.push(m);
      if (q.length > EARLY_MAX) q.shift();
    }
    host.changed();
  }

  function register(name, i) {
    impl[name] = i;
    const p = panels[name];
    if (p) i.mount?.(p.querySelector(".addon-body"), api(name));
    if (desc[name]?.on && snaps[name]) i.hello?.(snaps[name]);
    const q = early[name] || [];
    delete early[name];
    if (desc[name]?.on) for (const m of q) i.event?.(m);
    renderHead(name);
    host.changed();
  }

  return {
    register, hello, event,
    setHost(h) { host = h; for (const [n, p] of Object.entries(panels)) h.place(n, p); },
    list: () => Object.values(desc),
    tab(name) {
      const d = desc[name];
      if (!d) return null;
      if (d.error) return { dot: "off", off: true, title: d.error };
      if (!d.on) return { dot: "off", off: true, title: `${d.label} is switched off` };
      return { title: d.title, ...(impl[name]?.tab?.() || {}) };
    },
    card(name) {
      const d = desc[name];
      if (!d) return null;
      if (d.error) return { state: "off", lines: [esc(d.error)] };
      if (!d.on) return { state: "off", lines: ["Switched off", "Open →"] };
      return impl[name]?.card?.() || { state: "ok", lines: ["Open →"] };
    },
  };
})();
