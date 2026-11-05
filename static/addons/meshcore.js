"use strict";

// MeshCore add-on (addons/meshcore.py): a MeshCore companion node on its own
// tab. Top: the node (name, model, firmware, radio, battery, location).
// Left: messages on the node's channels and direct messages, with a send box
// for the selected channel. Right: the nodes it has heard advertise, newest
// first, with how far away each says it is. MeshCore and Meshtastic are
// separate meshes; this tab never shows the Mesh tab's nodes.

(() => {
  let snap = {}, body = null, api = null, view = 0, unread = {};
  let cfgOpen = false, cfgSec = null, draft = {};    // Settings: the open section and unsaved edits
  const TYPE = { chat: "Companion", repeater: "Repeater", room: "Room server", sensor: "Sensor" };

  const ago = (ts) => {
    if (!ts) return "never";
    const s = Math.max(0, Date.now() / 1000 - ts);
    return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago`
      : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
  };
  const km = (a, b) => {
    if (a?.lat == null || a?.lon == null || b?.lat == null || b?.lon == null) return null;
    const r = Math.PI / 180, dLat = (b.lat - a.lat) * r, dLon = (b.lon - a.lon) * r;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLon / 2) ** 2;
    return 12742 * Math.asin(Math.sqrt(h));
  };
  const me = () => snap.info?.me || {};
  const isDm = (v) => typeof v === "string";       // view: a channel index, or a contact key prefix

  function head() {
    const s = snap.status || {}, i = snap.info || {}, r = i.radio || {}, d = i.device || {}, m = me();
    const state = s.connected ? `<span class="mc-ok">Connected</span>`
      : `<span class="mc-off">${esc(s.error || "Not connected")}</span>`;
    const bat = snap.battery_mv ? `${(snap.battery_mv / 1000).toFixed(2)} V` : "";
    const radio = r.freq ? `${r.freq} MHz · ${r.bw} kHz · SF${r.sf} · CR${r.cr} · ${r.tx_power} dBm` : "";
    const loc = m.lat != null ? `${m.lat.toFixed(3)}, ${m.lon.toFixed(3)}${m.share_loc ? " (shared)" : " (private)"}` : "no location";
    return `<div class="mc-head">
        <span class="mc-name">${esc(m.name || "—")}</span>${state}
        <span class="stat">${esc([d.model, d.fw].filter(Boolean).join(" · "))}</span>
        <span class="spacer"></span>
        <button class="chip chip-mini${cfgOpen ? " on" : ""}" data-cfg title="The node's settings: name, location, radio, Wi-Fi, Bluetooth">Settings</button>
        <button class="chip chip-mini" data-advert="0" title="Announce this node to the nodes in direct range (one packet on the air)">Advert</button>
        <button class="chip chip-mini" data-advert="1" title="Announce this node across the mesh: repeaters pass it on">Flood advert</button>
      </div>
      <div class="mc-sub stat">${esc([radio, bat, loc, linkLine()].filter(Boolean).join("  ·  "))}</div>`;
  }

  function linkLine() {
    const c = snap.config;
    if (!c) return "";
    const w = c.wifi;
    const link = c.link?.startsWith("tcp:") ? "via Wi-Fi" : c.link?.startsWith("ble:") ? "via Bluetooth" : "via USB";
    return w ? `${link} · Wi-Fi ${w.status || "?"}${w.ip ? ` (${w.ip})` : ""}` : link;
  }

  // ---------------------------------------------------------------- settings
  const sections = () => snap.config?.sections || [];
  const current = (sec) => (snap.config?.values || {})[sec] || {};
  const valueOf = (sec, f) => (draft[sec] && f in draft[sec]) ? draft[sec][f] : current(sec)[f];

  function field(sec, f) {
    const v = valueOf(sec, f.name), id = `mc-f-${sec}-${f.name}`;
    const unit = f.unit ? `<span class="stat">${esc(f.unit)}</span>` : "";
    let input;
    if (f.type === "bool") {
      input = `<input type="checkbox" id="${id}" data-f="${f.name}" ${v ? "checked" : ""}>`;
    } else if (f.type === "enum") {
      input = `<select id="${id}" data-f="${f.name}">${f.options.map((o) =>
        `<option value="${esc(String(o))}" ${String(o) === String(v) ? "selected" : ""}>${esc(String(o))}</option>`).join("")}</select>`;
    } else if (f.write_only) {
      input = `<input type="password" id="${id}" data-f="${f.name}" autocomplete="new-password" value="${esc(v || "")}">`;
    } else {
      const num = f.type === "int" || f.type === "float";
      input = `<input ${num ? `type="number" step="${f.type === "int" ? 1 : "any"}"` : `type="text"`} id="${id}"
        data-f="${f.name}" value="${esc(v == null ? "" : String(v))}"${f.min != null ? ` min="${f.min}"` : ""}${f.max != null ? ` max="${f.max}"` : ""}>`;
    }
    const hint = f.hint ? `<small class="stat">${esc(f.hint)}</small>` : "";
    return `<label class="mc-field" for="${id}"><span>${esc(f.label)}${f.risky ? " ⚠" : ""}</span>${input}${unit}${hint}</label>`;
  }

  function settings() {
    const secs = sections();
    if (!secs.length) return `<div class="mc-cfg st-empty">The node's settings appear once it is connected.</div>`;
    if (!secs.some((x) => x.name === cfgSec)) cfgSec = secs[0].name;
    const sec = secs.find((x) => x.name === cfgSec);
    const dirty = Object.keys(draft[cfgSec] || {}).length > 0;
    const presets = cfgSec === "radio" ? `<div class="mc-presets"><span class="stat">Presets</span>${
      (snap.presets || []).map((p, i) => `<button type="button" class="chip chip-mini" data-preset="${i}"
        title="${esc(`${p.freq} MHz · ${p.bw} kHz · SF${p.sf} · CR${p.cr}`)}">${esc(p.label)}</button>`).join("")}</div>` : "";
    return `<div class="mc-cfg">
      <div class="seg mc-cfg-tabs">${secs.map((x) => `<button class="chip${x.name === cfgSec ? " on" : ""}" data-sec="${x.name}">${
        esc(x.label)}${Object.keys(draft[x.name] || {}).length ? " •" : ""}</button>`).join("")}</div>
      ${presets}
      <form class="mc-cfg-form" autocomplete="off">${sec.fields.map((f) => field(cfgSec, f)).join("")}</form>
      <div class="wl-row mc-cfg-actions">
        <button class="chip on" data-save ${dirty ? "" : "disabled"}>Save to node</button>
        <button class="chip" data-revert ${dirty ? "" : "disabled"}>Revert</button>
        <span class="spacer"></span>
        <button class="chip chip-mini" data-reboot title="Restart the node: Wi-Fi and Bluetooth changes take effect">Reboot node</button>
      </div></div>`;
  }

  function readField(f, el) {
    if (f.type === "bool") return el.checked;
    if (f.type === "int") return el.value === "" ? null : parseInt(el.value, 10);
    if (f.type === "float") return el.value === "" ? null : parseFloat(el.value);
    if (f.type === "enum") return f.options.find((o) => String(o) === el.value);
    return el.value;
  }

  function onEdit(e) {
    const el = e.target, name = el.dataset.f;
    const f = sections().find((x) => x.name === cfgSec)?.fields.find((x) => x.name === name);
    if (!f) return;
    const v = readField(f, el), d = (draft[cfgSec] ||= {});
    if (f.write_only ? v === "" : v === current(cfgSec)[name]) delete d[name]; else d[name] = v;
    if (!Object.keys(d).length) delete draft[cfgSec];
    const dirty = !!draft[cfgSec];
    body.querySelector("[data-save]").disabled = !dirty;
    body.querySelector("[data-revert]").disabled = !dirty;
  }

  function save() {
    const sec = sections().find((x) => x.name === cfgSec), vals = draft[cfgSec];
    if (!sec || !vals) return;
    const risky = sec.fields.filter((f) => f.risky && f.name in vals);
    if (risky.length && !confirm(risky.map((f) => `${f.label}: ${f.risky}`).join("\n\n") + "\n\nSave anyway?")) return;
    api.send("set", { section: cfgSec, values: vals });
    delete draft[cfgSec];                // the node's read-back replaces it
    render();
  }

  function tabs() {
    const ch = (snap.channels || []).map((c) =>
      `<button class="chip${view === c.index ? " on" : ""}" data-view="${c.index}">${esc(c.name)}${
        unread[c.index] ? ` <span class="mc-unread">${unread[c.index]}</span>` : ""}</button>`);
    const dms = new Set((snap.messages || []).filter((x) => x.dm).map((x) => x.mine ? x.to : x.from).filter(Boolean));
    const dm = [...dms].map((k) => {
      const c = (snap.contacts || []).find((c) => (c.key || "").startsWith(k));
      return `<button class="chip${view === k ? " on" : ""}" data-view="${esc(k)}">✉ ${esc(c?.name || k.slice(0, 8))}${
        unread[k] ? ` <span class="mc-unread">${unread[k]}</span>` : ""}</button>`;
    });
    return `<div class="seg mc-tabs">${[...ch, ...dm].join("")}</div>`;
  }

  function messages() {
    const list = (snap.messages || []).filter((x) => isDm(view)
      ? x.dm && (x.mine ? x.to : x.from) === view : !x.dm && x.ch === view);
    if (!list.length) return `<div class="mc-empty st-empty">No messages here yet.</div>`;
    return list.slice(-150).map((x) => {
      const meta = [new Date(x.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
        x.mine ? "" : x.hops ? `${x.hops} hop${x.hops === 1 ? "" : "s"}` : "direct",
        x.snr != null ? `SNR ${x.snr}` : "", x.mine ? x.ack + (x.why ? `: ${x.why}` : "") : ""].filter(Boolean).join(" · ");
      return `<div class="mc-msg${x.mine ? " mine" : ""}"><b>${esc(x.mine ? "You" : x.name || (x.from || "?").slice(0, 8))}</b>
        <span>${esc(x.text)}</span><small>${esc(meta)}</small></div>`;
    }).join("");
  }

  function contacts() {
    const list = snap.contacts || [];
    if (!list.length) return `<div class="mc-empty st-empty">No nodes heard yet. A node shows up here when it
      advertises; press Advert so nearby nodes learn this one.</div>`;
    return list.map((c) => {
      const d = km(me(), c), heard = (snap.heard || {})[c.key];
      return `<div class="mc-node" title="Message this node" data-key="${esc((c.key || "").slice(0, 12))}">
        <b>${esc(c.name || (c.key || "").slice(0, 8))}</b><span class="stat">${esc(TYPE[c.type] || c.type)}</span>
        <small>${esc([c.hops == null ? "flood" : c.hops === 0 ? "direct" : `${c.hops} hop${c.hops === 1 ? "" : "s"}`,
          d != null ? `${d < 10 ? d.toFixed(1) : Math.round(d)} km` : "", `advert ${ago(heard || c.last_advert)}`].filter(Boolean).join(" · "))}</small>
      </div>`;
    }).join("");
  }

  function render() {
    if (!body) return;
    const max = snap.text_max || 140;
    body.innerHTML = `${head()}${cfgOpen ? settings() : ""}
      <div class="mc-cols">
        <div class="mc-left">${tabs()}<div class="mc-list">${messages()}</div>
          <form class="wl-row mc-form"><input class="wl-url mc-text" maxlength="${max * 2}" autocomplete="off"
            placeholder="${isDm(view) ? "Direct message" : "Message to the channel"}"><span class="stat mc-count"></span>
            <button type="submit" class="chip on" ${snap.status?.connected ? "" : "disabled"}>Send</button></form>
        </div>
        <div class="mc-right"><div class="label">Heard (${(snap.contacts || []).length})</div>
          <div class="mc-nodes">${contacts()}</div></div>
      </div>`;
    const list = body.querySelector(".mc-list");
    list.scrollTop = list.scrollHeight;
    const input = body.querySelector(".mc-text"), count = body.querySelector(".mc-count");
    input.oninput = () => {
      const n = new TextEncoder().encode(input.value.trim()).length;
      count.textContent = n ? `${n}/${max}` : "";
      count.classList.toggle("mc-over", n > max);
    };
    body.querySelector(".mc-form").onsubmit = (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      api.send("send", isDm(view) ? { text, to: view } : { text, ch: view });
      input.value = "";
      count.textContent = "";
    };
    for (const b of body.querySelectorAll("[data-view]")) b.onclick = () => {
      const v = b.dataset.view;
      view = /^\d+$/.test(v) ? Number(v) : v;
      delete unread[view];
      render();
      api.changed();
    };
    for (const b of body.querySelectorAll("[data-advert]")) b.onclick = () => {
      const flood = b.dataset.advert === "1";
      if (flood && !confirm("Flood an advert? Every repeater in range passes it on.")) return;
      api.send("advert", { flood });
    };
    body.querySelector("[data-cfg]").onclick = () => { cfgOpen = !cfgOpen; render(); };
    if (cfgOpen) {
      const form = body.querySelector(".mc-cfg-form");
      if (form) { form.oninput = onEdit; form.onchange = onEdit; form.onsubmit = (e) => { e.preventDefault(); save(); }; }
      for (const b of body.querySelectorAll("[data-sec]")) b.onclick = () => { cfgSec = b.dataset.sec; render(); };
      for (const b of body.querySelectorAll("[data-preset]")) b.onclick = () => {
        const p = snap.presets[+b.dataset.preset], d = (draft.radio ||= {});
        for (const k of ["freq", "bw", "sf", "cr"]) if (p[k] !== current("radio")[k]) d[k] = p[k]; else delete d[k];
        if (!Object.keys(d).length) delete draft.radio;
        render();
      };
      const sv = body.querySelector("[data-save]"); if (sv) sv.onclick = save;
      const rv = body.querySelector("[data-revert]"); if (rv) rv.onclick = () => { delete draft[cfgSec]; render(); };
      const rb = body.querySelector("[data-reboot]");
      if (rb) rb.onclick = () => { if (confirm("Reboot the node? It is off the air for a few seconds.")) api.send("reboot"); };
    }
    for (const n of body.querySelectorAll(".mc-node")) n.onclick = () => {
      view = n.dataset.key;
      render();
    };
  }

  addons.register("meshcore", {
    mount(el, a) { body = el; api = a; render(); },
    hello(s) {
      snap = { ...s };
      if (!(snap.channels || []).some((c) => c.index === view) && !isDm(view)) view = snap.channels?.[0]?.index ?? 0;
      render();
    },
    off() { snap = { status: { connected: false, error: "Switched off" } }; render(); },
    event(m) {
      if (m.status) snap.status = m.status;
      if (m.info) snap.info = m.info;
      if (m.config) snap.config = m.config;
      if (m.presets) snap.presets = m.presets;
      if (m.channels) snap.channels = m.channels;
      if (m.contacts) snap.contacts = m.contacts;
      if ("battery_mv" in m) snap.battery_mv = m.battery_mv;
      if (m.heard) snap.heard = { ...(snap.heard || {}), [m.heard.key]: m.heard.t };
      if (m.msg) {
        (snap.messages ||= []).push(m.msg);
        const where = m.msg.dm ? m.msg.from : m.msg.ch;
        if (!m.msg.mine && where !== view) unread[where] = (unread[where] || 0) + 1;
      }
      if (m.update) {
        const i = (snap.messages || []).findIndex((x) => x.mine && x.t === m.update.t);
        if (i >= 0) snap.messages[i] = m.update;
      }
      const typing = body && document.activeElement === body.querySelector(".mc-text");
      if (typing && !m.msg && !m.status) return;     // don't wipe a half-typed message for a contact refresh
      if (cfgOpen && body?.querySelector(".mc-cfg-form")?.contains(document.activeElement) && !m.config) return;
      const keep = typing ? body.querySelector(".mc-text").value : null;
      render();
      if (keep != null) { const i = body.querySelector(".mc-text"); i.value = keep; i.focus(); i.oninput(); }
    },
    tab() {
      const n = Object.values(unread).reduce((a, b) => a + b, 0);
      return { dot: snap.status?.connected ? "ok" : "off", off: !snap.status?.connected, badge: n || "" };
    },
    card() {
      const c = snap.contacts || [];
      return { state: snap.status?.connected ? "ok" : "off",
        lines: [esc(me().name || "MeshCore"), `${c.length} node${c.length === 1 ? "" : "s"} heard`, "Open →"] };
    },
  });
})();
