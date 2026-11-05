"use strict";

// MeshCore add-on (addons/meshcore.py): a MeshCore companion node on its own
// tab, laid out like the Mesh tab (static/mesh.js) and using its styles.
// Top: the node (name, model, firmware, radio, battery, location, link) and
// its own radio counters (noise floor, airtime, packets, what it hears).
// Left: the node's channels and direct messages, a send box, and the map
// (meshmap.js) of the nodes that share a location; the route to the
// selected node is drawn on it, and a repeater's neighbours too.
// Right: the nodes it has heard (find, recent, favourites first), and the
// selected node's card: message, favourite, path discovery, trace, forget
// the route, remove; for a repeater or room server, log in, status,
// telemetry, neighbours, owner, set its clock and a remote command line.
// MeshCore and Meshtastic are separate meshes: this tab never shows the
// Mesh tab's nodes.

(() => {
  const TYPE = { chat: "Companion", repeater: "Repeater", room: "Room server", sensor: "Sensor" };
  const TAG = { chat: "", repeater: "RPT", room: "ROOM", sensor: "SENS" };
  const GLYPH = { repeater: "R", room: "S", sensor: "T" };
  const SERVER = (c) => c && (c.type === "repeater" || c.type === "room");
  const LIVE_S = 15 * 60, RECENT_S = 24 * 3600;
  const ACK = { sending: ["…", "Sending"], sent: ["✓?", "Sent, waiting for an ACK"],
    delivered: ["✓✓", "Delivered"], failed: ["✗", "Not delivered"],
    unknown: ["?", "ACK unknown (the server or node restarted)"] };
  const RISKY_CLI = /^\s*(erase|reboot|start ota|password|setperm|set\s+(radio|freq|bw|sf|cr|tx|repeat|name|lat|lon|guest\.password))\b/i;

  let snap = {}, body = null, api = null, map = null;
  let view = 0;                    // a channel index, or "dm"
  let dmTo = null;                 // key prefix (12 hex) of the node a direct message goes to
  let selected = null;             // full key of the node whose card is open
  let recentOnly = false;
  const unread = {}, waiting = {}; // waiting: key -> {kind: t} for requests on the air
  let cfgOpen = false, cfgSec = null, draft = {};
  let consoleOpen = false;

  const $b = (sel) => body.querySelector(sel);
  const now = () => Date.now() / 1000;
  const me = () => snap.info?.me || {};
  const contacts = () => snap.contacts || [];
  const pending = () => (snap.pending || []).map((c) => ({ ...c, pending: true }));
  const all = () => [...pending(), ...contacts()];
  const byKey = (k) => (k ? all().find((c) => (c.key || "").startsWith(k)) : null);
  const name = (k) => byKey(k)?.name || (k || "?").slice(0, 8);
  const p12 = (k) => (k || "").slice(0, 12);
  // When this host last heard it; else the time in its advert (its own clock, so only if not in the future).
  const heardAt = (c) => Math.max((snap.heard || {})[c.key] || 0,
    c.last_advert && c.last_advert < now() + 3600 ? c.last_advert : 0) || null;
  const bytes = (s) => new TextEncoder().encode(s).length;
  const hhmm = (t) => new Date(t * 1000).toTimeString().slice(0, 5);
  const connected = () => !!snap.status?.connected;

  function ago(t) {
    if (!t) return "";
    const s = Math.max(0, now() - t);
    if (s < 90) return "now";
    if (s < 3600) return `${Math.round(s / 60)}m`;
    if (s < 86400) return `${Math.round(s / 3600)}h`;
    return `${Math.round(s / 86400)}d`;
  }
  const when = (t) => (!t ? "never" : ago(t) === "now" ? "just now" : `${ago(t)} ago`);
  const km = (a, b) => {
    if (a?.lat == null || a?.lon == null || b?.lat == null || b?.lon == null) return null;
    const r = Math.PI / 180, dLat = (b.lat - a.lat) * r, dLon = (b.lon - a.lon) * r;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLon / 2) ** 2;
    return 12742 * Math.asin(Math.sqrt(h));
  };
  const kmText = (d) => (d == null ? "" : `${d < 10 ? d.toFixed(1) : Math.round(d)} km`);
  const dur = (s) => {
    if (s == null) return "";
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  };
  const hopsText = (c) => (c.pending ? "" : c.hops == null ? "flood" : c.hops === 0 ? "direct" : `${c.hops} hop${c.hops > 1 ? "s" : ""}`);

  // A route hop is a repeater's key hash: the repeater it names, when only one fits.
  function hop(hash) {
    if (!hash) return null;
    const fits = contacts().filter((c) => SERVER(c) && (c.key || "").startsWith(hash));
    return fits.length === 1 ? fits[0] : null;
  }
  const hopName = (hash) => hop(hash)?.name || `?${hash}`;
  const hopId = (hash) => hop(hash)?.key || `?${hash}`;

  // ---------------------------------------------------------------- skeleton

  function skeleton() {
    body.innerHTML = `
      <div class="mc-head"></div>
      <div class="mc-sub stat"></div>
      <div class="mc-stats stat"></div>
      <div class="mc-cfg-wrap" hidden></div>
      <div class="wl-cols mesh-cols mc-main">
        <div class="wl-mail">
          <div class="wl-bar"><div class="seg mc-tabs"></div>
            <button class="chip chip-mini" data-join title="Join a #hashtag channel: its key is the name's hash, so anyone using the same name hears it">+ #channel</button>
            <button class="chip chip-mini" data-leave hidden title="Leave this channel (frees the slot on the node)">Leave</button></div>
          <div class="mesh-list mc-list"></div>
          <div class="mesh-to mc-to" hidden></div>
          <form class="wl-row mesh-form mc-form">
            <input class="wl-url mc-text" placeholder="Message" autocomplete="off" maxlength="400">
            <span class="stat mc-count"></span>
            <button type="submit" class="chip on mc-send" disabled>Send</button>
          </form>
          <div class="mesh-map mc-map"></div>
        </div>
        <div class="wl-session">
          <div class="wl-bar">
            <input class="wl-url mc-find" placeholder="Find a node" autocomplete="off" spellcheck="false">
            <button class="chip chip-mini mc-recent" title="Only nodes heard in the last day">Recent</button>
            <span class="stat mc-count-nodes"></span>
          </div>
          <div class="mesh-card mc-card" hidden></div>
          <div class="wl-gateways mesh-nodes mc-nodes"></div>
        </div>
      </div>`;
    map = typeof meshMap !== "undefined" ? meshMap.create($b(".mc-map"), { onSelect: (k) => select(k, true) }) : null;
    $b(".mc-form").onsubmit = submit;
    $b(".mc-text").oninput = count;
    $b(".mc-find").oninput = renderNodes;
    $b(".mc-recent").onclick = () => { recentOnly = !recentOnly; $b(".mc-recent").classList.toggle("on", recentOnly); renderNodes(); };
    $b("[data-join]").onclick = () => {
      const n = prompt("Join a hashtag channel (anyone on the same #name hears it):", "#");
      if (n && n.trim() && n.trim() !== "#") api.send("channel_join", { name: n.trim() });
    };
    $b("[data-leave]").onclick = () => {
      const ch = (snap.channels || []).find((c) => c.index === view);
      if (ch && confirm(`Leave ${ch.name}? The node stops hearing it; join again to come back.`)) api.send("channel_leave", { index: ch.index });
    };
    setInterval(() => { if (body) { renderNodes(); renderStats(); map?.render(); } }, 60000);
  }

  function renderAll() {
    if (!body) return;
    renderHead();
    renderStats();
    renderCfg();
    renderTabs();
    renderMessages();
    renderCompose();
    renderNodes();
    renderMap();
  }

  // ---------------------------------------------------------------- header

  function renderHead() {
    const s = snap.status || {}, i = snap.info || {}, r = i.radio || {}, d = i.device || {}, m = me();
    const state = s.connected ? `<span class="mc-ok">Connected</span>`
      : `<span class="mc-off">${esc(s.error || "Not connected")}</span>`;
    $b(".mc-head").innerHTML = `
        <span class="mc-name">${esc(m.name || "—")}</span>${state}
        <span class="stat">${esc([d.model, d.fw].filter(Boolean).join(" · "))}</span>
        <span class="spacer"></span>
        <button class="chip chip-mini${cfgOpen ? " on" : ""}" data-cfg title="The node's settings: name, location, radio, Wi-Fi, Bluetooth">Settings</button>
        <button class="chip chip-mini" data-advert="0" title="Announce this node to the nodes in direct range (one packet on the air)">Advert</button>
        <button class="chip chip-mini" data-advert="1" title="Announce this node across the mesh: repeaters pass it on">Flood advert</button>`;
    const bat = snap.battery_mv ? `${(snap.battery_mv / 1000).toFixed(2)} V` : "";
    const radio = r.freq ? `${r.freq} MHz · ${r.bw} kHz · SF${r.sf} · CR${r.cr} · ${r.tx_power} dBm` : "";
    const loc = m.lat != null ? `${m.lat.toFixed(3)}, ${m.lon.toFixed(3)}${m.share_loc ? " (shared)" : " (private)"}` : "no location";
    $b(".mc-sub").textContent = [radio, bat, loc, linkLine()].filter(Boolean).join("  ·  ");
    body.classList.toggle("mesh-off", !s.connected);
    $b("[data-cfg]").onclick = () => { cfgOpen = !cfgOpen; renderHead(); renderCfg(); };
    for (const b of body.querySelectorAll("[data-advert]")) b.onclick = () => {
      const flood = b.dataset.advert === "1";
      if (flood && !confirm("Flood an advert? Every repeater in range passes it on.")) return;
      api.send("advert", { flood });
    };
  }

  function linkLine() {
    const c = snap.config;
    if (!c) return "";
    const w = c.wifi;
    const link = c.link?.startsWith("tcp:") ? "via Wi-Fi" : c.link?.startsWith("ble:") ? "via Bluetooth" : "via USB";
    return w ? `${link} · Wi-Fi ${w.status || "?"}${w.ip ? ` (${w.ip})` : ""}` : link;
  }

  function renderStats() {
    const st = snap.stats || {}, air = snap.air || {}, last = air.last;
    const parts = [];
    if (st.noise_floor != null) parts.push(`noise ${st.noise_floor} dBm`);
    if (st.last_rssi != null) parts.push(`last RSSI ${st.last_rssi} dBm, SNR ${st.last_snr}`);
    if (st.tx_air_secs != null) parts.push(`airtime TX ${dur(st.tx_air_secs)} · RX ${dur(st.rx_air_secs)}`);
    if (st.recv != null) parts.push(`packets RX ${st.recv} · TX ${st.sent}` +
      (st.flood_rx != null ? ` (flood ${st.flood_rx}/${st.flood_tx}, direct ${st.direct_rx}/${st.direct_tx})` : ""));
    if (st.uptime_secs != null) parts.push(`up ${dur(st.uptime_secs)}`);
    if (air.n != null) parts.push(`heard ${air.n} packet${air.n === 1 ? "" : "s"} in ${Math.round((air.window_s || 600) / 60)} min` +
      (last ? ` (last ${last.type || "?"} ${ago(last.t)}${last.hops ? ` via ${hopName(last.last)}` : ""})` : ""));
    const el = $b(".mc-stats");
    el.textContent = parts.join("  ·  ");
    el.hidden = !parts.length;
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
      const ph = sec === "bluetooth" ? (snap.config?.pin_fixed ? "fixed PIN set" : "random PIN") : "unchanged";
      input = `<input type="password" id="${id}" data-f="${f.name}" autocomplete="new-password"
        ${f.type === "int" ? 'inputmode="numeric"' : ""} placeholder="${ph}" value="${esc(v == null ? "" : String(v))}">`;
    } else {
      const num = f.type === "int" || f.type === "float";
      input = `<input ${num ? `type="number" step="${f.type === "int" ? 1 : "any"}"` : `type="text"`} id="${id}"
        data-f="${f.name}" value="${esc(v == null ? "" : String(v))}"${f.min != null ? ` min="${f.min}"` : ""}${f.max != null ? ` max="${f.max}"` : ""}>`;
    }
    const hint = f.hint ? `<small class="stat">${esc(f.hint)}</small>` : "";
    return `<label class="mc-field" for="${id}"><span>${esc(f.label)}${f.risky ? " ⚠" : ""}</span>${input}${unit}${hint}</label>`;
  }

  function renderCfg() {
    const wrap = $b(".mc-cfg-wrap");
    wrap.hidden = !cfgOpen;
    $b(".mc-main").hidden = cfgOpen;
    if (!cfgOpen) return;
    const secs = sections();
    if (!secs.length) { wrap.innerHTML = `<div class="mc-cfg st-empty">The node's settings appear once it is connected.</div>`; return; }
    if (!secs.some((x) => x.name === cfgSec)) cfgSec = secs[0].name;
    const sec = secs.find((x) => x.name === cfgSec);
    const dirty = Object.keys(draft[cfgSec] || {}).length > 0;
    const presets = cfgSec === "radio" ? `<div class="mc-presets"><span class="stat">Presets</span>${
      (snap.presets || []).map((p, i) => `<button type="button" class="chip chip-mini" data-preset="${i}"
        title="${esc(`${p.freq} MHz · ${p.bw} kHz · SF${p.sf} · CR${p.cr}`)}">${esc(p.label)}</button>`).join("")}</div>` : "";
    wrap.innerHTML = `<div class="mc-cfg">
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
    const form = wrap.querySelector(".mc-cfg-form");
    form.oninput = onEdit; form.onchange = onEdit; form.onsubmit = (e) => { e.preventDefault(); save(); };
    for (const b of wrap.querySelectorAll("[data-sec]")) b.onclick = () => { cfgSec = b.dataset.sec; renderCfg(); };
    for (const b of wrap.querySelectorAll("[data-preset]")) b.onclick = () => {
      const p = snap.presets[+b.dataset.preset], d = (draft.radio ||= {});
      for (const k of ["freq", "bw", "sf", "cr"]) if (p[k] !== current("radio")[k]) d[k] = p[k]; else delete d[k];
      if (!Object.keys(d).length) delete draft.radio;
      renderCfg();
    };
    wrap.querySelector("[data-save]").onclick = save;
    wrap.querySelector("[data-revert]").onclick = () => { delete draft[cfgSec]; renderCfg(); };
    wrap.querySelector("[data-reboot]").onclick = () => { if (confirm("Reboot the node? It is off the air for a few seconds.")) api.send("reboot"); };
  }

  function readField(f, el) {
    if (f.type === "bool") return el.checked;
    if (f.type === "int") return el.value === "" ? null : parseInt(el.value, 10);
    if (f.type === "float") return el.value === "" ? null : parseFloat(el.value);
    if (f.type === "enum") return f.options.find((o) => String(o) === el.value);
    return el.value;
  }

  function onEdit(e) {
    const el = e.target, fname = el.dataset.f;
    const f = sections().find((x) => x.name === cfgSec)?.fields.find((x) => x.name === fname);
    if (!f) return;
    const v = readField(f, el), d = (draft[cfgSec] ||= {});
    if (f.write_only ? (v === "" || v === null) : v === current(cfgSec)[fname]) delete d[fname]; else d[fname] = v;
    if (!Object.keys(d).length) delete draft[cfgSec];
    const dirty = !!draft[cfgSec];
    $b("[data-save]").disabled = !dirty;
    $b("[data-revert]").disabled = !dirty;
  }

  function save() {
    const sec = sections().find((x) => x.name === cfgSec), vals = draft[cfgSec];
    if (!sec || !vals) return;
    const risky = sec.fields.filter((f) => f.risky && f.name in vals);
    if (risky.length && !confirm(risky.map((f) => `${f.label}: ${f.risky}`).join("\n\n") + "\n\nSave anyway?")) return;
    api.send("set", { section: cfgSec, values: vals });
    delete draft[cfgSec];                // the node's read-back replaces it
    renderCfg();
  }

  // ---------------------------------------------------------------- channels and messages

  const viewOf = (m) => (m.dm ? "dm" : m.ch);

  function renderTabs() {
    const tabs = (snap.channels || []).map((c) => [c.index, c.name]);
    tabs.push(["dm", "Direct"]);
    if (!tabs.some(([v]) => v === view)) view = tabs.length > 1 ? tabs[0][0] : "dm";
    $b(".mc-tabs").innerHTML = tabs.map(([v, label]) =>
      `<button class="chip${v === view ? " on" : ""}" data-view="${esc(String(v))}">${esc(label)}${
        unread[v] ? ` <span class="mesh-unread mc-unread">${unread[v]}</span>` : ""}</button>`).join("");
    for (const b of body.querySelectorAll("[data-view]")) b.onclick = () => {
      const v = b.dataset.view;
      view = v === "dm" ? "dm" : Number(v);
      delete unread[view];
      renderTabs(); renderMessages(); renderCompose();
      api.changed();
    };
    $b("[data-leave]").hidden = view === "dm";
  }

  function row(x) {
    const div = document.createElement("div");
    div.className = "mesh-msg mc-msg" + (x.mine ? " mine" : "");
    const peer = x.mine ? x.to : x.from;
    const sender = !x.mine && !x.dm && x.name ? contacts().find((c) => c.name === x.name) : null;
    const who = x.mine ? (x.dm ? `→ ${name(x.to)}` : "me") : (x.dm ? `${x.name || name(x.from)} → me` : x.name || "?");
    let glyph = "", why = "";
    if (x.mine) {
      if (!x.dm && x.ack === "sent") [glyph, why] = ["✓", "Sent to the channel (a channel has no ACK)"];
      else [glyph, why] = ACK[x.ack] || ["", ""];
      if (x.ack === "delivered") why += [x.tries > 1 ? ` after ${x.tries} tries` : "", x.flood ? " (flooded)" : "",
        x.trip_ms ? `, round trip ${(x.trip_ms / 1000).toFixed(1)} s` : ""].join("");
      if (x.why) why += `: ${x.why}`;
    }
    const meta = x.mine ? "" : [x.hops == null ? "" : x.hops === 0 ? "direct" : `${x.hops} hop${x.hops === 1 ? "" : "s"}`,
      x.snr != null ? `SNR ${x.snr}` : ""].filter(Boolean).join(" · ");
    div.innerHTML = `<span class="mesh-time">${hhmm(x.t)}</span>` +
      `<button class="mesh-who" title="${esc(x.mine ? "" : peer || sender?.key || "")}">${esc(who)}</button>` +
      `<span class="mesh-text">${esc(x.text)}</span>` +
      `<span class="mesh-meta">${esc(meta)}${x.mine ? `<span class="mesh-ack ${esc(x.ack || "")}" title="${esc(why)}">${glyph}</span>` : ""}</span>`;
    const target = x.dm ? peer : sender?.key;
    if (target) div.querySelector(".mesh-who").onclick = () => directTo(target);
    return div;
  }

  function renderMessages() {
    const list = $b(".mc-list");
    const shown = (snap.messages || []).filter((x) => viewOf(x) === view).slice(-300);
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    list.innerHTML = shown.length ? "" : `<div class="wl-empty mc-empty">No messages${view === "dm" ? " (pick a node to message it)" : " here yet"}</div>`;
    for (const x of shown) list.append(row(x));
    if (atBottom || shown.length < 2) list.scrollTop = list.scrollHeight;
  }

  function directTo(key) {
    dmTo = p12(key);
    view = "dm";
    delete unread.dm;
    renderTabs(); renderMessages(); renderCompose();
    $b(".mc-text").focus();
  }

  function renderCompose() {
    const to = $b(".mc-to");
    if (view === "dm") {
      to.hidden = false;
      const c = byKey(dmTo);
      to.innerHTML = dmTo
        ? `To <b>${esc(name(dmTo))}</b> <span class="wl-dim">${esc(c ? TYPE[c.type] || c.type : "")} ${esc(dmTo)}</span> <button class="chip chip-mini" data-to-x>✕</button>`
        : `<span class="wl-dim">Pick a node on the right (or a sender) to message it</span>`;
      const x = to.querySelector("[data-to-x]");
      if (x) x.onclick = () => { dmTo = null; renderCompose(); };
    } else {
      to.hidden = true;
    }
    $b(".mc-text").placeholder = view === "dm" ? "Direct message" : "Message to the channel";
    count();
  }

  function count() {
    const max = snap.text_max || 140, n = bytes($b(".mc-text").value.trim()), c = $b(".mc-count");
    c.textContent = n ? `${n}/${max}` : "";
    c.classList.toggle("mc-over", n > max);
    $b(".mc-send").disabled = !connected() || !n || n > max || (view === "dm" && !dmTo);
  }

  function submit(e) {
    e.preventDefault();
    const text = $b(".mc-text").value.trim();
    if (!text || $b(".mc-send").disabled) return;
    api.send("send", view === "dm" ? { text, to: dmTo } : { text, ch: view });
    $b(".mc-text").value = "";
    count();
  }

  // ---------------------------------------------------------------- nodes

  function renderNodes() {
    const q = $b(".mc-find").value.trim().toLowerCase(), t = now();
    const list = all()
      .filter((c) => c.pending || !recentOnly || q || c.fav || (heardAt(c) && t - heardAt(c) < RECENT_S))
      .filter((c) => !q || [c.name, c.key, TYPE[c.type]].some((s) => String(s || "").toLowerCase().includes(q)))
      .sort((a, b) => (!!b.pending - !!a.pending) || (!!b.fav - !!a.fav) || ((heardAt(b) || 0) - (heardAt(a) || 0)));
    const el = $b(".mc-nodes");
    el.innerHTML = list.length ? "" : `<div class="wl-empty mc-empty">${all().length ? "No nodes heard in the last day"
      : "No nodes heard yet. A node shows up here when it advertises; press Advert so nearby nodes learn this one."}</div>`;
    for (const c of list) {
      const b = document.createElement("button"), h = heardAt(c), live = h && t - h < LIVE_S;
      b.className = "mesh-node mc-node" + (c.key === selected ? " on" : "");
      b.dataset.key = c.key;
      b.title = `${c.name || ""} · ${TYPE[c.type] || c.type} · ${p12(c.key)}: click for details`;
      const sub = [c.lat != null ? "📍" : "", c.pending ? "not added" : "",
        snap.logged_in?.[c.key] ? (snap.logged_in[c.key].admin ? "admin" : "guest") : ""].filter(Boolean).join(" ");
      b.innerHTML = `<span class="mesh-short"><span class="mesh-fav${c.fav ? " on" : ""}" title="${c.pending ? "Add to contacts"
        : c.fav ? "Favourite: click to remove" : "Make a favourite"}">${c.pending ? "+" : c.fav ? "★" : "☆"}</span>${esc(TAG[c.type] || "")}</span>` +
        `<span class="mesh-long">${esc(c.name || p12(c.key))}<span class="mesh-sub">${esc(sub)}</span></span>` +
        `<span class="wl-dim">${esc(hopsText(c))}</span>` +
        `<span class="wl-dim">${esc(kmText(km(me(), c)))}</span>` +
        `<span class="wl-dim${live ? " mesh-live" : ""}">${esc(ago(h))}</span>`;
      b.onclick = () => select(c.key);
      b.querySelector(".mesh-fav").onclick = (e) => {
        e.stopPropagation();
        if (!connected()) return;
        if (c.pending) api.send("add", { key: c.key });
        else api.send("fav", { key: c.key, fav: !c.fav });
      };
      el.append(b);
    }
    const n = all().length;
    $b(".mc-count-nodes").textContent = recentOnly && !q ? `${list.length} of ${n}` : `${n} known`;
    renderCard();
  }

  function select(key, fromMap = false) {
    if (byKey(key) == null) return;               // "me" on the map
    selected = selected === key && !fromMap ? null : key;
    consoleOpen = false;
    renderNodes();
    renderMap();
    if (fromMap && selected) body.querySelector(`.mc-nodes [data-key="${selected}"]`)?.scrollIntoView({ block: "nearest" });
  }

  // ---------------------------------------------------------------- a node's card

  const result = (key, kind) => (snap.results || {})[key]?.[kind];
  const busy = (key, kind) => !!waiting[key]?.[kind];
  const failed = (label, r) => `<div class="mesh-trace failed">${label}: ${esc(r.error)} <span class="wl-dim">${esc(when(r.t))}</span></div>`;

  function routeHtml(c) {
    if (c.hops == null) return `<span class="wl-dim">no route known: messages flood</span>`;
    if (!c.path?.length) return "direct";
    return [me().name || "me", ...c.path.map(hopName), c.name].map((x) => `<span class="mesh-hop">${esc(x)}</span>`).join(" → ");
  }

  function pathHtml(r) {
    if (!r) return "";
    if (!r.ok) return failed("Path", r);
    const line = (hs) => hs.length ? hs.map((h) => `<span class="mesh-hop" title="${esc(h)}">${esc(hopName(h))}</span>`).join(" → ") : "direct";
    return `<div class="mesh-trace mc-path"><div><span class="wl-dim">there</span>${line(r.data.out || [])}</div>` +
      `<div><span class="wl-dim">back</span>${line(r.data.back || [])}</div><div class="wl-dim">path found ${esc(when(r.t))}</div></div>`;
  }

  function traceHtml(r) {
    if (!r) return "";
    if (!r.ok) return failed("Trace", r);
    const hs = r.data.hops || [];
    const out = hs.map((h) => `<span class="mesh-hop">${esc(h.hash ? hopName(h.hash) : "me")}` +
      ` <span class="wl-dim">(${h.snr == null ? "?" : `${h.snr} dB`})</span></span>`).join(" → ");
    return `<div class="mesh-trace mc-trace"><div><span class="wl-dim">trace</span>${out || "no hops"}</div>` +
      `<div class="wl-dim">each hop's SNR as it heard the one before · ${esc(when(r.t))}</div></div>`;
  }

  function statusHtml(r) {
    if (!r) return "";
    if (!r.ok) return failed("Status", r);
    const d = r.data || {};
    const f = [["battery", d.bat ? `${(d.bat / 1000).toFixed(2)} V` : ""], ["up", dur(d.uptime)],
      ["airtime", dur(d.airtime)], ["noise", d.noise_floor != null ? `${d.noise_floor} dBm` : ""],
      ["last RSSI", d.last_rssi != null ? `${d.last_rssi} dBm` : ""], ["last SNR", d.last_snr != null ? `${d.last_snr} dB` : ""],
      ["received", d.nb_recv != null ? `${d.nb_recv}` : ""], ["sent", d.nb_sent != null ? `${d.nb_sent}` : ""],
      ["flood", d.sent_flood != null ? `TX ${d.sent_flood} · RX ${d.recv_flood}` : ""],
      ["direct", d.sent_direct != null ? `TX ${d.sent_direct} · RX ${d.recv_direct}` : ""],
      ["dups", d.flood_dups != null ? `flood ${d.flood_dups} · direct ${d.direct_dups}` : ""],
      ["queue", d.tx_queue_len != null ? `${d.tx_queue_len}` : ""], ["full events", d.full_evts ? `${d.full_evts}` : ""]]
      .filter(([, v]) => v);
    return `<div class="mc-res mc-status"><div class="wl-dim">status ${esc(when(r.t))}</div>` +
      `<div class="mesh-card-grid">${f.map(([k, v]) => `<span>${k} <b>${esc(v)}</b></span>`).join("")}</div></div>`;
  }

  function telemetryHtml(r) {
    if (!r) return "";
    if (!r.ok) return failed("Telemetry", r);
    const lpp = r.data?.lpp || [];
    const val = (v) => (v && typeof v === "object" ? Object.entries(v).map(([k, x]) => `${k} ${x}`).join(", ") : String(v));
    return `<div class="mc-res mc-telemetry"><div class="wl-dim">telemetry ${esc(when(r.t))}</div>` +
      (lpp.length ? `<div class="mesh-card-grid">${lpp.map((x) => `<span>${esc(String(x.type))}${x.channel > 1 ? ` #${x.channel}` : ""} <b>${esc(val(x.value))}</b></span>`).join("")}</div>`
        : `<div class="wl-dim">no sensors</div>`) + `</div>`;
  }

  function neighboursHtml(r) {
    if (!r) return "";
    if (!r.ok) return failed("Neighbours", r);
    const ns = (r.data?.neighbours || []).slice().sort((a, b) => (b.snr ?? -99) - (a.snr ?? -99));
    return `<div class="mc-res mc-neighbours"><div class="wl-dim">${ns.length} neighbour${ns.length === 1 ? "" : "s"}${
      r.data.total > ns.length ? ` of ${r.data.total}` : ""} it hears directly · ${esc(when(r.t))}</div>` +
      `<div class="mesh-card-grid">${ns.map((n) => `<span title="${esc(n.key)}">${esc(byKey(n.key)?.name || n.key)} <b>${esc(String(n.snr))} dB</b> <span class="wl-dim">${esc(ago(now() - (n.secs_ago || 0)))}</span></span>`).join("")}</div></div>`;
  }

  function ownerHtml(r) {
    if (!r) return "";
    if (!r.ok) return failed("Owner", r);
    return `<div class="mc-res"><span class="wl-dim">owner</span> <b>${esc(r.data?.owner || "(not set)")}</b></div>`;
  }

  function consoleHtml(c) {
    const lines = (snap.cli || {})[c.key] || [];
    return `<div class="mc-console"><div class="mc-console-out">${lines.map((l) =>
      `<div class="mc-cli-${esc(l.way)}"><span class="wl-dim">${hhmm(l.t)}</span> ${l.way === "out" ? "&gt; " : ""}${esc(l.text)}</div>`).join("") ||
      `<div class="wl-dim">Commands go to the repeater; its replies show here. Try <b>ver</b>, <b>get radio</b>, <b>neighbors</b>.</div>`}</div>
      <form class="wl-row mc-cli-form"><input class="wl-url mc-cli" placeholder="Command for ${esc(c.name)}" autocomplete="off" spellcheck="false" maxlength="160">
      <button type="submit" class="chip chip-mini on">Send</button></form></div>`;
  }

  function renderCard() {
    const card = $b(".mc-card"), c = byKey(selected);
    card.hidden = !c;
    if (!c) return;
    const focused = card.contains(document.activeElement) ? [...document.activeElement.classList].find((k) => k.startsWith("mc-")) : null;
    const keep = { pwd: card.querySelector(".mc-pwd")?.value || "", cli: card.querySelector(".mc-cli")?.value || "" };
    const login = snap.logged_in?.[c.key], saved = (snap.logins || []).some((p) => c.key.startsWith(p));
    const on = connected(), srv = SERVER(c);
    const btn = (act, label, title, dis = false) =>
      `<button class="chip chip-mini" data-act="${act}" title="${esc(title)}"${!on || dis || busy(c.key, act) ? " disabled" : ""}>${busy(c.key, act) ? "… " : ""}${esc(label)}</button>`;
    // An advert carries the sender's clock: far from when this host heard it, that clock is wrong
    // (a repeater without a battery-backed clock falls back to its build date after a power cut).
    const heardHere = (snap.heard || {})[c.key], skew = heardHere && c.last_advert ? c.last_advert - heardHere : null;
    const clock = skew == null ? "" : Math.abs(skew) < 600 ? "ok" : `${skew < 0 ? "behind" : "ahead"} by ${dur(Math.abs(skew))}`;
    const fields = [["type", TYPE[c.type] || c.type], ["distance", kmText(km(me(), c))],
      ["heard", when(heardAt(c))], ["its clock", clock],
      ["position", c.lat != null ? `${c.lat.toFixed(4)}, ${c.lon.toFixed(4)}` : "not shared"],
      ["login", login ? (login.admin ? "admin" : "guest") : ""]].filter(([, v]) => v);
    card.innerHTML = `<div class="mesh-card-head"><b>${esc(c.name || p12(c.key))}</b><span>${esc(TYPE[c.type] || c.type)}</span>` +
      `<span class="wl-dim" title="${esc(c.key)}">${esc(p12(c.key))}</span>` +
      `<span class="spacer"></span><button class="chip chip-mini" data-act="close" title="Close">✕</button></div>` +
      `<div class="mesh-card-grid">${fields.map(([k, v]) => `<span${k === "its clock" && v !== "ok" ? ' class="mc-warn" title="Set its clock (admin login) or sync it from a companion app"' : ""}>${k} <b>${esc(v)}</b></span>`).join("")}</div>` +
      (c.pending ? `<div class="wl-row">${btn("add", "Add to contacts", "Add this node to the node's contacts")}</div>` :
      `<div class="mesh-trace"><span class="wl-dim">route</span>${routeHtml(c)}</div>` +
      `<div class="wl-row">` +
        (c.type !== "repeater" ? `<button class="chip chip-mini" data-act="dm">Message</button>` : "") +
        btn("fav", c.fav ? "★ Unfavourite" : "☆ Favourite", "Favourites stay at the top of the list") +
        btn("path", "Find path", "Ask the mesh for the route to this node and back (sent as a flood)") +
        btn("trace", "Trace", "Send a trace along the known route: each repeater adds the SNR it heard it at", c.hops == null) +
        btn("reset_path", "Forget route", "Forget the route: the next message floods and learns a new one", c.hops == null) +
        btn("remove", "Remove", "Remove this node from the node's contacts") +
      `</div>` +
      (srv ? `<div class="wl-row mc-admin">` +
        (saved ? btn("login", login ? "Log in again" : "Log in", "Log in with the password saved on this host")
          : `<input type="password" class="wl-url mc-pwd" placeholder="password" autocomplete="off" maxlength="15">` +
            btn("login", "Log in", "Log in: admin password for full control, guest password to read")) +
        btn("status", "Status", "Its battery, uptime, noise floor, airtime and packet counts") +
        btn("telemetry", "Telemetry", "Its sensors (voltage, temperature, ...)") +
        btn("neighbours", "Neighbours", "The repeaters it hears directly, with SNR (drawn on the map)") +
        btn("owner", "Owner", "Its owner info") +
        btn("clock", "Set clock", "Set its clock to this host's (it loses the time on a power cut)", !login?.admin) +
        `<button class="chip chip-mini${consoleOpen ? " on" : ""}" data-act="console" title="A command line on the repeater (admin login)"${login?.admin ? "" : " disabled"}>Console</button>` +
      `</div>` : "") +
      pathHtml(result(c.key, "path")) + traceHtml(result(c.key, "trace")) + statusHtml(result(c.key, "status")) +
      telemetryHtml(result(c.key, "telemetry")) + neighboursHtml(result(c.key, "neighbours")) + ownerHtml(result(c.key, "owner")) +
      (srv && consoleOpen && login?.admin ? consoleHtml(c) : ""));
    const pwd = card.querySelector(".mc-pwd"), cli = card.querySelector(".mc-cli");
    if (pwd) pwd.value = keep.pwd;
    if (cli) cli.value = keep.cli;
    if (focused) card.querySelector(`.${focused}`)?.focus();
    const out = card.querySelector(".mc-console-out");
    if (out) out.scrollTop = out.scrollHeight;
    card.onclick = (e) => {
      const act = e.target.closest("button")?.dataset.act;
      if (!act) return;
      if (act === "close") return select(c.key);
      if (act === "dm") return directTo(c.key);
      if (act === "console") { consoleOpen = !consoleOpen; return renderCard(); }
      if (act === "remove" && !confirm(`Remove ${c.name} from the node's contacts? It comes back when it advertises again.`)) return;
      if (act === "fav") return api.send("fav", { key: c.key, fav: !c.fav });
      if (act === "login") {
        const p = card.querySelector(".mc-pwd");
        api.send("login", { key: c.key, ...(p ? { password: p.value } : {}) });
        if (p) p.value = "";
        return;
      }
      api.send(act, { key: c.key });
    };
    const f = card.querySelector(".mc-cli-form");
    if (f) f.onsubmit = (e) => {
      e.preventDefault();
      const text = cli.value.trim();
      if (!text) return;
      if (RISKY_CLI.test(text) && !confirm(`Send this to ${c.name}?\n\n${text}\n\nIt changes the repeater and may take it off the mesh.`)) return;
      api.send("cli", { key: c.key, text });
      cli.value = "";
    };
  }

  // ---------------------------------------------------------------- map

  function renderMap() {
    if (!map) return;
    const m = me(), nodes = [];
    if (m.key && m.lat != null) nodes.push({ id: m.key, me: true, shortName: m.name, longName: m.name, lat: m.lat, lon: m.lon, last_heard: now() });
    for (const c of contacts()) {
      nodes.push({ id: c.key, shortName: c.name || p12(c.key), longName: `${c.name} · ${TYPE[c.type] || c.type}`,
        lat: c.lat, lon: c.lon, last_heard: heardAt(c), fav: c.fav, glyph: GLYPH[c.type] || "" });
    }
    map.setNodes(nodes, m.key);
    map.select(selected);
    const c = byKey(selected);
    let trace = null, links = [];
    if (c && !c.pending) {
      const p = result(c.key, "path");
      if (p?.ok) trace = { state: "done", to: c.key, route: (p.data.out || []).map(hopId), route_back: (p.data.back || []).map(hopId) };
      else if (c.hops != null) trace = { state: "done", to: c.key, route: (c.path || []).map(hopId), route_back: null };
      const n = result(c.key, "neighbours");
      if (n?.ok) links = (n.data.neighbours || []).map((x) => ({ from: c.key, to: byKey(x.key)?.key, label: `${x.snr} dB` })).filter((l) => l.to);
    }
    map.setTrace(trace);
    map.setLinks?.(links);
  }

  function pulse(key) {
    const c = byKey(key);
    if (c && map) map.pulse(c.key);
  }

  // ---------------------------------------------------------------- from the server

  function hello(s) {
    snap = { ...s };
    renderAll();
  }

  function event(m) {
    if (m.status) snap.status = m.status;
    if (m.info) snap.info = m.info;
    if (m.config) snap.config = m.config;
    if (m.presets) snap.presets = m.presets;
    if (m.channels) snap.channels = m.channels;
    if (m.contacts) snap.contacts = m.contacts;
    if (m.pending) snap.pending = m.pending;
    if ("battery_mv" in m) snap.battery_mv = m.battery_mv;
    if (m.stats) snap.stats = m.stats;
    if (m.rx) snap.air = { ...(snap.air || {}), n: m.rx.n, last: m.rx };
    if (m.heard) snap.heard = { ...(snap.heard || {}), [m.heard.key]: m.heard.t };
    if (m.logged_in) snap.logged_in = m.logged_in;
    if (m.request) (waiting[m.request.key] ||= {})[m.request.kind] = m.request.t;
    if (m.result) {
      const r = m.result;
      if (waiting[r.key]) delete waiting[r.key][r.kind];
      ((snap.results ||= {})[r.key] ||= {})[r.kind] = r;
    }
    if (m.cli) {
      const q = ((snap.cli ||= {})[m.cli.key] ||= []);
      q.push(m.cli);
      if (q.length > 40) q.shift();
      if (m.cli.way !== "out" && waiting[m.cli.key]) { delete waiting[m.cli.key].cli; delete waiting[m.cli.key].clock; }
    }
    if (m.msg) {
      (snap.messages ||= []).push(m.msg);
      if (snap.messages.length > 300) snap.messages.shift();
    }
    if (m.update) {
      const i = (snap.messages || []).findIndex((x) => x.mine && x.t === m.update.t);
      if (i >= 0) snap.messages[i] = m.update;
    }
    if (!body) return;

    if (m.status) { renderHead(); count(); renderCard(); }
    if (m.info) { renderHead(); renderMap(); }
    if (m.config) { renderHead(); if (!$b(".mc-cfg-form")?.contains(document.activeElement)) renderCfg(); }
    if (m.channels) { renderTabs(); renderMessages(); renderCompose(); }
    if (m.contacts || m.pending || m.logged_in) { renderNodes(); renderMap(); }
    if ("battery_mv" in m) renderHead();
    if (m.stats || m.rx) renderStats();
    if (m.rx?.last) { const h = hop(m.rx.last); if (h) pulse(h.key); }
    if (m.heard) { pulse(m.heard.key); renderNodes(); }
    if (m.request && m.request.key === selected) renderCard();
    if (m.result && m.result.key === selected) { renderCard(); renderMap(); }
    if (m.cli && m.cli.key === selected) renderCard();
    if (m.msg) {
      const v = viewOf(m.msg);
      if (v === view) renderMessages();
      else if (!m.msg.mine) { unread[v] = (unread[v] || 0) + 1; renderTabs(); }
      if (!m.msg.mine) {
        if (m.msg.from) pulse(m.msg.from);
        if (m.msg.dm && typeof toast === "function") toast(`MeshCore · ${m.msg.name || name(m.msg.from)}: ${m.msg.text}`, "info");
      }
    }
    if (m.update && viewOf(m.update) === view) renderMessages();
  }

  addons.register("meshcore", {
    mount(el, a) { body = el; api = a; skeleton(); renderAll(); },
    hello,
    off() { snap = { status: { connected: false, error: "Switched off" } }; if (body) renderAll(); },
    event,
    tab() {
      const n = Object.values(unread).reduce((a, b) => a + b, 0);
      return { dot: connected() ? "ok" : "off", off: !connected(), badge: n || "" };
    },
    card() {
      const c = contacts();
      return { state: connected() ? "ok" : "off",
        lines: [esc(me().name || "MeshCore"), `${c.length} node${c.length === 1 ? "" : "s"} heard`, "Open →"] };
    },
  });
})();
