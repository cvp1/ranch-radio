"use strict";

// APRS on the packet page (APRS plan M2): Stations, Map, Messages and Raw
// tabs over the hub's store. Stations and objects arrive as deltas (?rev=),
// with tombstones for what expired. Uses packet.html's $ and esc, and
// meshmap.js.
//
// Messages (plan 2026-11-07, 3.5): threads per correspondent and compose.
// Sending is like PTT on the radio pages: TX ARMED in this browser first
// (never remembered across a reload), and compose stays disabled while
// transmit is off or stopped, a hold is on, or messaging is paused. The hub
// checks all of it again; the page only saves a refused click.

(() => {
  const POLL_MS = 5000;
  const stations = new Map(), objects = new Map();
  let stRev = 0, obRev = 0, me = null, mycall = "", selected = null, map = null, track = [];
  let sortKey = "last", sortDir = -1, chip = "all";

  // ---------- small helpers

  const ago = (t) => {
    if (!t) return "";
    const s = Math.max(0, Math.round(Date.now() / 1000 - t));
    return s < 60 ? s + "s" : s < 3600 ? Math.round(s / 60) + "m" : s < 86400 ? Math.round(s / 3600) + "h"
      : Math.round(s / 86400) + "d";
  };
  const when = (t) => (t ? new Date(t * 1000).toLocaleString([], { hour12: false }) : "—");
  const getJSON = async (url) => { const r = await fetch(url); if (!r.ok) throw new Error(r.status); return r.json(); };

  // A text glyph per APRS symbol (no sprite set yet: its licence is unchecked).
  const GLYPHS = { "-": "⌂", ">": "C", "#": "D", "&": "G", "_": "W", "k": "T", "v": "V", "u": "T", "b": "B",
    "[": "P", "Y": "Y", "s": "S", "'": "A", "^": "A", "O": "O", "r": "R", "l": "L", "y": "Y", "a": "+",
    "f": "F", "j": "J", "R": "R", "U": "U", "=": "=", "<": "M", "X": "X", "/": "•" };
  function glyph(sym) {
    if (!sym || sym.length < 2) return "?";
    const table = sym[0], code = sym[1];
    if (table !== "/" && table !== "\\" && /[0-9A-Z]/.test(table)) return table;   // overlay
    return GLYPHS[code] || code;
  }
  function kindOf(s) {
    if (s.weather || (s.symbol || "").endsWith("_")) return "wx";
    if (!s.rf_last && s.is_last) return "is";
    return "rf";
  }
  function via(s) {
    if (!s.rf_last) return s.is_last ? "via APRS-IS" : "";
    if (s.direct) return "direct";
    const used = (s.rf_path || "").split(",").find((p) => p.endsWith("*"));
    return used ? "via " + used.slice(0, -1) : "digi";
  }
  function distBearing(s) {
    if (!me || s.lat == null) return null;
    const R = 3958.8, rad = Math.PI / 180;
    const [la1, lo1, la2, lo2] = [me.lat * rad, me.lon * rad, s.lat * rad, s.lon * rad];
    const a = Math.sin((la2 - la1) / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin((lo2 - lo1) / 2) ** 2;
    const d = 2 * R * Math.asin(Math.sqrt(a));
    const y = Math.sin(lo2 - lo1) * Math.cos(la2);
    const x = Math.cos(la1) * Math.sin(la2) - Math.sin(la1) * Math.cos(la2) * Math.cos(lo2 - lo1);
    const b = (Math.atan2(y, x) / rad + 360) % 360;
    return { d, b, txt: `${d < 10 ? d.toFixed(1) : Math.round(d)} mi ${["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(b / 45) % 8]}` };
  }

  // ---------- tabs

  const TABS = ["stations", "map", "messages", "raw"];
  function showTab(name) {
    if (!TABS.includes(name)) name = "stations";
    document.querySelectorAll("[data-tab]").forEach((el) => {
      if (el.tagName === "BUTTON") el.classList.toggle("cur", el.dataset.tab === name);
      else el.hidden = el.dataset.tab !== name;
    });
    // One map: beside the list on Stations, full size on Map.
    const box = $("aprs-mapbox");
    const slot = name === "map" ? $("map-full") : $("st-mapslot");
    if (box.parentElement !== slot) slot.append(box);
    if (name === "map" || name === "stations") drawMap();
    if (name === "messages") loadMessages();
    if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  }
  $("tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) showTab(b.dataset.tab); });
  window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

  // ---------- data

  function apply(store, res, key) {
    if (res.full) store.clear();
    // Tombstones first: a row that came back after it expired is current.
    for (const g of res.gone || []) store.delete(Array.isArray(g) ? g.join("|") : g);
    for (const r of res.rows) store.set(key(r), r);
  }
  async function poll() {
    try {
      const [st, ob] = await Promise.all([getJSON(`api/aprs/stations?rev=${stRev}`), getJSON(`api/aprs/objects?rev=${obRev}`)]);
      apply(stations, st, (r) => r.call);
      apply(objects, ob, (r) => `${r.name}|${r.owner}`);
      const changed = st.rows.length || ob.rows.length || (st.gone || []).length || (ob.gone || []).length || st.full;
      stRev = st.rev; obRev = ob.rev;
      if (changed) { drawStations(); if (!$("aprs-map").hidden || !$("aprs-stations").hidden) drawMap(); }
      if (selected && st.rows.some((r) => r.call === selected) && !$("aprs-detail").hidden) openDetail(selected, true);
    } catch (e) {
      $("st-note").textContent = "APRS store not answering";
    }
  }
  async function loadMe() {
    try {
      const s = await getJSON("api/status");
      me = s.aprs && s.aprs.me;
      mycall = s.mycall || "";
    } catch (e) { /* the page's own refresh shows the hub is down */ }
  }

  // ---------- Stations

  const COLS = [
    ["call", "station"], ["symbol", ""], ["last", "heard"], ["dist", "distance"], ["via", "path"], ["comment", "comment / status"],
  ];
  const CHIPS = [["all", "All"], ["direct", "Direct"], ["digi", "Via digi"], ["is", "APRS-IS"], ["wx", "Weather"], ["pos", "On the map"]];
  $("st-chips").innerHTML = CHIPS.map(([k, t]) => `<button data-chip="${k}">${t}</button>`).join(" ");
  $("st-chips").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    chip = b.dataset.chip; drawStations();
  });
  $("st-filter").addEventListener("input", () => drawStations());
  $("st-table").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-k]");
    if (th) {
      const k = th.dataset.k;
      sortDir = sortKey === k ? -sortDir : (k === "call" || k === "dist" ? 1 : -1);
      sortKey = k; drawStations(); return;
    }
    const tr = e.target.closest("tr[data-call]");
    if (tr) openDetail(tr.dataset.call);
  });

  function matches(s, q) {
    if (chip === "direct" && !s.direct) return false;
    if (chip === "digi" && (!s.rf_last || s.direct)) return false;
    if (chip === "is" && !(s.is_last && !s.rf_last)) return false;
    if (chip === "wx" && kindOf(s) !== "wx") return false;
    if (chip === "pos" && s.lat == null) return false;
    if (!q) return true;
    return [s.call, s.comment, s.status, s.rf_path, s.device].some((x) => (x || "").toLowerCase().includes(q));
  }
  function sortVal(s) {
    switch (sortKey) {
      case "call": return s.call;
      case "symbol": return s.symbol || "";
      case "dist": { const d = distBearing(s); return d ? d.d : Infinity; }
      case "via": return via(s);
      case "comment": return (s.status || s.comment || "").toLowerCase();
      default: return s.last || 0;
    }
  }
  function drawStations() {
    document.querySelectorAll("#st-chips button").forEach((b) => b.classList.toggle("cur", b.dataset.chip === chip));
    const q = $("st-filter").value.trim().toLowerCase();
    const list = [...stations.values()].filter((s) => matches(s, q));
    list.sort((a, b) => { const x = sortVal(a), y = sortVal(b); return (x < y ? -1 : x > y ? 1 : 0) * sortDir; });
    $("st-note").textContent = `${list.length} of ${stations.size} stations · ${[...objects.values()].filter((o) => o.live).length} objects`;
    const head = "<tr>" + COLS.map(([k, t]) => `<th data-k="${k}" class="${k === sortKey ? "sorted" : ""}">${t}${k === sortKey ? (sortDir > 0 ? " ▲" : " ▼") : ""}</th>`).join("") + "</tr>";
    $("st-table").innerHTML = list.length ? head + list.map((s) => {
      const db = distBearing(s);
      const heard = [s.rf_last ? `RF ${ago(s.rf_last)}` : "", s.is_last ? `IS ${ago(s.is_last)}` : ""].filter(Boolean).join(" · ") || ago(s.last);
      return `<tr class="st-row${s.call === selected ? " sel" : ""}" data-call="${esc(s.call)}">` +
        `<td class="mono">${esc(s.call)}</td><td><span class="glyph k-${kindOf(s)}" title="${esc(s.symbol || "")}">${esc(glyph(s.symbol))}</span></td>` +
        `<td>${heard}</td><td>${db ? db.txt : ""}</td><td class="via">${esc(via(s))}</td>` +
        `<td>${esc((s.status || s.comment || "").slice(0, 90))}</td></tr>`;
    }).join("") : `<tr><td class="empty">${stations.size ? "no station matches" : "nothing heard yet"}</td></tr>`;
  }

  // ---------- Map

  function mapNodes() {
    const out = [];
    for (const s of stations.values()) {
      if (s.lat == null) continue;
      out.push({ id: s.call, shortName: s.call, longName: s.status || s.comment || "", last_heard: s.last,
        lat: s.lat, lon: s.lon, glyph: glyph(s.symbol), kind: kindOf(s), box_deg: s.box_deg || 0,
        noFit: !s.rf_last });                     // heard only via APRS-IS: may be anywhere
    }
    for (const o of objects.values()) {
      if (!o.live || o.lat == null) continue;
      out.push({ id: `obj:${o.name}|${o.owner}`, shortName: o.name, longName: `${o.kind} by ${o.owner}: ${o.comment || ""}`,
        last_heard: o.t, lat: o.lat, lon: o.lon, glyph: glyph(o.symbol), kind: "obj", box_deg: 0 });
    }
    if (me) out.push({ id: mycall || "me", shortName: mycall || "me", longName: "this station", me: true, lat: me.lat, lon: me.lon });
    return out;
  }
  function drawMap() {
    if (!map) map = meshMap.create($("aprs-mapbox"), { onSelect: (id) => openDetail(id), meSelectable: true });
    map.setNodes(mapNodes(), mycall || "me");
    map.select(selected);
    map.setTrack(track);
  }

  // ---------- Station detail

  function dl(pairs) {
    return "<dl>" + pairs.filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("") + "</dl>";
  }
  function closeDetail() {
    $("aprs-detail").hidden = true;
    selected = null;
    track = [];
    if (map) { map.select(null); map.setTrack([]); }
    drawStations();
  }
  async function openDetail(id, quiet) {
    const el = $("aprs-detail");
    if (id.startsWith("obj:")) {
      const o = objects.get(id.slice(4));
      if (!o) return;
      selected = id;
      track = [];
      el.innerHTML = `<h3>${esc(o.name)} <span class="glyph k-obj">${esc(glyph(o.symbol))}</span>` +
        `<button class="close">Close</button></h3>` + dl([["kind", esc(o.kind)], ["owner", esc(o.owner)],
        ["live", o.live ? "yes" : "killed"], ["killed by", o.killed_by ? esc(o.killed_by) + " (not the owner: not applied)" : ""],
        ["heard", `${when(o.t)} (${ago(o.t)} ago)`], ["position", `${o.lat?.toFixed(5)}, ${o.lon?.toFixed(5)}`],
        ["comment", esc(o.comment || "")]]);
      el.hidden = false;
      el.querySelector(".close").onclick = closeDetail;
      if (map) { map.select(id); map.setTrack([]); }
      return;
    }
    let s;
    try { s = await getJSON(`api/aprs/station/${encodeURIComponent(id)}`); }
    catch (e) {
      if (quiet) return;
      el.innerHTML = `<h3>${esc(id)}<button class="close">Close</button></h3><p class="empty">not heard yet</p>`;
      el.hidden = false; el.querySelector(".close").onclick = closeDetail; return;
    }
    selected = s.call;
    track = s.track;
    const db = distBearing(s);
    const wx = s.weather ? Object.entries(s.weather).filter(([k]) => k !== "t").map(([k, v]) => `${esc(k)} ${esc(v)}`).join(" · ") : "";
    const tel = s.telemetry ? s.telemetry.channels.map((c) => `${esc(c.name)} ${esc(c.value)}${c.unit ? " " + esc(c.unit) : ""}`).join(" · ") +
      (s.telemetry.bits ? ` · bits ${esc(s.telemetry.bits)}` : "") : "";
    el.innerHTML = `<h3>${esc(s.call)} <span class="glyph k-${kindOf(s)}">${esc(glyph(s.symbol))}</span>` +
      `<button class="close">Close</button></h3>` +
      dl([["heard on RF", s.rf_last ? `${when(s.rf_last)} (${ago(s.rf_last)} ago)` : "not on RF"],
          ["direct", s.direct_last ? `${when(s.direct_last)} (${ago(s.direct_last)} ago)` : "never direct"],
          ["APRS-IS", s.is_last ? `${when(s.is_last)} (${ago(s.is_last)} ago)` : ""],
          ["packets", s.count], ["position", s.lat != null ? `${s.lat.toFixed(5)}, ${s.lon.toFixed(5)}` +
            (s.ambiguity ? ` (ambiguous: ${s.ambiguity} digit${s.ambiguity > 1 ? "s" : ""})` : "") +
            (s.pos_port ? ` · ${s.pos_port.toUpperCase()}` : "") : ""],
          ["fix time", s.pos_t ? when(s.pos_reported_t || s.pos_t) : ""],
          ["distance", db ? db.txt : ""], ["symbol", esc(s.symbol || "")], ["device", esc(s.device || "")],
          ["status", esc(s.status || "")], ["comment", esc(s.comment || "")], ["weather", wx], ["telemetry", tel]]) +
      (s.paths.length ? `<h4>Heard on these paths</h4><table>${s.paths.map((p) =>
        `<tr><td class="mono">${esc(p.path || "(direct)")}</td><td>${p.count}</td><td>${ago(p.last)}</td></tr>`).join("")}</table>` : "") +
      `<h4>Track</h4><p class="muted" style="margin:0">${s.track.length} point${s.track.length === 1 ? "" : "s"}` +
      `${s.track.length > 1 ? " (drawn on the map)" : ""}</p>` +
      `<h4>Last packets</h4><table>${s.packets.slice(0, 25).map((p) =>
        `<tr><td>${ago(p.t)}</td><td>${esc(p.port)}</td><td>${esc(p.decoded?.type || "")}</td>` +
        `<td class="mono">${esc((p.decoded?.info || "").slice(0, 80))}</td></tr>`).join("")}</table>` +
      `<h4>Messages</h4><div id="detail-msgs" class="muted">…</div>`;
    el.hidden = false;
    el.querySelector(".close").onclick = closeDetail;
    if (map) { map.select(s.call); map.setTrack(s.track); }
    drawStations();
    try {
      const m = await getJSON(`api/aprs/messages?with=${encodeURIComponent(s.call)}`);
      const box = document.getElementById("detail-msgs");
      if (box) box.innerHTML = m.messages.length ? msgTable(m.messages.slice(-20)) : "none";
    } catch (e) { /* ignore */ }
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("aprs-detail").hidden) closeDetail(); });

  // ---------- Messages: heard (all), threads, compose

  function msgTable(list) {
    return `<table><tr><th>time</th><th>from</th><th>to</th><th>message</th><th>#</th><th>state</th></tr>` +
      list.map((m) => `<tr><td>${when(m.t)}</td><td class="mono">${esc(m.src)}</td><td class="mono">${esc(m.dst)}</td>` +
        `<td>${esc(m.text)}</td><td class="mono">${esc(m.msgno || "")}</td><td>${esc(m.state)}${m.port === "is" ? " · IS" : ""}</td></tr>`).join("") +
      `</table>`;
  }
  async function loadMessages() {
    const w = $("msg-with").value.trim();
    try {
      const m = await getJSON("api/aprs/messages" + (w ? `?with=${encodeURIComponent(w)}` : ""));
      $("msg-table").innerHTML = m.messages.length ? msgTable(m.messages.slice().reverse()).replace(/^<table>|<\/table>$/g, "")
        : `<tr><td class="empty">no messages heard</td></tr>`;
    } catch (e) {
      $("msg-table").innerHTML = `<tr><td class="empty">APRS store not answering</td></tr>`;
    }
    loadThreads();
  }
  $("msg-with").addEventListener("change", loadMessages);

  let armed = false, peer = null, mst = null;     // armed: this browser only, never stored
  const SEEN = "aprs-msg-seen";
  const seen = () => { try { return JSON.parse(localStorage.getItem(SEEN) || "{}"); } catch (e) { return {}; } };
  const BAD = /[|~{}]|[^\x20-\x7e]/;

  function stateText(m) {
    if (m.dir === "in") return m.state === "acked" ? "received · acked" : "received";
    const why = m.why ? ` · ${m.why}` : "";
    if (m.state === "pending") return m.slot ? `pending · slot ${m.slot}` : `pending · not sent yet${why}`;
    if (m.state === "sending") return "sending";
    if (m.state === "queued") return "queued (one at a time per station)";
    return m.state + (m.state === "failed" || m.state === "rejected" ? why : "");
  }
  function why() {
    if (!mst) return "hub not answering";
    const m = mst.aprs.messaging || {}, tx = mst.tx;
    if (m.effective === "off") return "messaging is off" + (m.why ? ` (${m.why})` : "");
    if (!tx.enabled) return tx.inhibit ? "transmit stopped by the operator" : "transmit is off";
    if ((tx.holds || []).length) return `held: ${tx.holds[0].why || "off the APRS channel"}`;
    if (m.pause) return `paused: ${m.pause}`;
    if (!armed) return "arm TX in this browser to send";
    return "";
  }
  function drawCompose() {
    const reason = why(), text = $("msg-text").value, to = $("msg-to").value.trim();
    const bad = BAD.test(text) ? "not allowed: | ~ { } and anything outside plain ASCII" : text.length > 67 ? "too long" : "";
    const ok = !reason;
    $("msg-text").disabled = $("msg-to").disabled = !ok;
    $("msg-send").disabled = !ok || !!bad || !text.trim() || !to;
    $("msg-count").textContent = `${text.length}/67`;
    $("msg-count").className = bad ? "bad" : "muted";
    $("msg-pause").textContent = reason;
    $("msg-note").textContent = bad;
    const b = $("msg-arm");
    b.classList.toggle("on", armed); b.setAttribute("aria-checked", armed);
    b.querySelector("b").textContent = armed ? "ARMED" : "SAFE";
    if (!mst) return;
    const m = mst.aprs.messaging || {};
    const eff = m.effective || "off";
    $("msg-mode").className = "pill " + (eff === "off" ? "muted" : eff === "full" ? "ok" : "warn");
    $("msg-mode").querySelector("span").textContent = `messaging ${eff}` + (m.mode && m.mode !== eff ? ` (set: ${m.mode})` : "") +
      (eff === "send" ? " · no acks sent" : "");
    $("msg-mode").title = m.why || "";
    $("msg-holds").innerHTML = (mst.tx.holds || []).map((h) =>
      `<div class="msg-hold" data-by="${esc(h.by)}"><span>Held by <b>${esc(h.by)}</b>: ${esc(h.why || "off the APRS channel")}` +
      `${h.back ? ` · back to ${esc(h.back)}` : ""} · since ${when(h.t)}. No APRS message or ack goes out.</span>` +
      `<button class="msg-release" data-by="${esc(h.by)}" data-back="${esc(h.back || "")}">Release</button></div>`).join("");
  }
  $("msg-holds").addEventListener("click", async (e) => {
    const b = e.target.closest("button.msg-release"); if (!b) return;
    const by = b.dataset.by, back = b.dataset.back;
    if (!confirm(`Release the hold set by ${by}?\n\nOnly if the radio is back on its APRS channel` +
                 `${back ? ` (${back})` : ""}: the hub sends APRS messages and acks again at once.`)) return;
    const r = await fetch("api/hold/release", { method: "POST", headers: { "content-type": "application/json", "X-Ranch-Radio": "1" },
      body: JSON.stringify({ by, operator: true }) });
    if (!r.ok) alert((await r.json()).error || "refused");
    msgStatus();
  });
  $("msg-arm").onclick = () => {
    if (!armed && !confirm("Arm sending APRS messages from this browser, under your callsign?")) return;
    armed = !armed; drawCompose(); loadProbes();
  };
  ["input", "change"].forEach((ev) => { $("msg-text").addEventListener(ev, drawCompose); $("msg-to").addEventListener(ev, drawCompose); });
  $("msg-compose").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (why()) return;
    const to = $("msg-to").value.trim().toUpperCase(), text = $("msg-text").value;
    $("msg-send").disabled = true;
    try {
      const r = await fetch("api/aprs/message", { method: "POST", headers: { "content-type": "application/json", "X-Ranch-Radio": "1" },
        body: JSON.stringify({ to, text }) });
      const d = await r.json();
      if (!r.ok) { $("msg-note").textContent = d.error || "refused"; return; }
      $("msg-text").value = "";
      openThread(to);
    } catch (err) {
      $("msg-note").textContent = "hub not answering";
    } finally { drawCompose(); }
  });
  async function loadThreads() {
    try {
      const d = await getJSON("api/aprs/thread");
      const s = seen();
      let unread = 0;
      const rows = d.threads.map((t) => {
        const n = t.last > (s[t.call] || 0) && t.received ? 1 : 0;
        unread += n;
        return `<tr data-call="${esc(t.call)}" class="${t.call === peer ? "sel" : ""}"><td class="mono">${esc(t.call)}${n ? '<span class="badge">new</span>' : ""}</td>` +
          `<td>${ago(t.last)}</td></tr>`;
      });
      $("msg-threads").innerHTML = rows.length ? rows.join("") : `<tr><td class="empty">no conversations yet</td></tr>`;
      const tab = document.querySelector('nav.tabs button[data-tab="messages"]');
      tab.innerHTML = "Messages" + (unread ? `<span class="badge" id="msg-unread">${unread}</span>` : "");
    } catch (e) { /* the store isn't answering: the heard list says so */ }
  }
  $("msg-threads").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-call]"); if (tr) openThread(tr.dataset.call); });
  async function openThread(call) {
    peer = call; $("msg-to").value = call; $("msg-with-h").textContent = `With ${call}`;
    try {
      const d = await getJSON(`api/aprs/thread?with=${encodeURIComponent(call)}`);
      $("msg-thread").innerHTML = d.messages.length ? "<tr><th>time</th><th></th><th>message</th><th>state</th></tr>" +
        d.messages.slice().reverse().map((m) => `<tr class="msg-row" data-id="${m.id}" data-state="${esc(m.state || "")}">` +
          `<td>${when(m.t)}</td><td>${m.dir === "out" ? "→ " + esc(m.dst) : "← " + esc(m.src)}${m.via === "internet" ? ' <span class="muted">via internet</span>' : ""}</td>` +
          `<td>${esc(m.text)}</td><td class="st-${esc(m.state || "")}">${esc(stateText(m))}</td></tr>`).join("")
        : `<tr><td class="empty">nothing yet</td></tr>`;
      const s = seen(); s[call] = Date.now() / 1000; localStorage.setItem(SEEN, JSON.stringify(s));
    } catch (e) { $("msg-thread").innerHTML = `<tr><td class="empty">APRS store not answering</td></tr>`; }
    loadThreads(); drawCompose();
  }
  async function msgStatus() {
    try { mst = await getJSON("api/status"); } catch (e) { mst = null; }
    drawCompose();
    if (peer && !$("aprs-messages").hidden) openThread(peer);
    loadProbes();
  }

  // ---------- the round-trip probe (plan 2026-11-07, section 4)

  const LEG_NAMES = { L1: "sent", L2: "digipeated", L3: "APRS-IS via RF", L4: "reply on RF", L5: "acked" };
  function probeWhy() {
    const pr = mst && mst.aprs.probe;
    if (!pr) return "messaging is off";
    if (!armed) return "arm TX in this browser to probe";
    return pr.refuse || "";
  }
  async function loadProbes() {
    let d;
    try { d = await getJSON("api/aprs/probes"); } catch (e) { return; }
    const pr = d.probe;
    $("probe-target").textContent = pr && pr.target ? `to ${pr.target}` : "";
    const reason = probeWhy();
    $("probe-btn").disabled = !!reason;
    $("probe-why").textContent = reason;
    $("probe-table").innerHTML = d.probes.length ? "<tr><th>started</th><th>to</th><th>state</th>" +
      Object.values(LEG_NAMES).map((n) => `<th>${n}</th>`).join("") + "</tr>" +
      d.probes.map((p) => `<tr class="probe-row" data-id="${p.id}"><td>${when(p.t)}</td><td class="mono">${esc(p.target)} ` +
        `<span class="muted">${esc(p.token)}</span></td><td>${esc(p.state)}</td>` +
        Object.keys(LEG_NAMES).map((k) => { const l = (p.legs || {})[k] || { v: "waiting" };
          const tip = l.evidence || l.why || "";
          return `<td><span class="leg v-${l.v.replace(" ", "-")}" data-leg="${k}" title="${esc(tip)}">${esc(l.v === "unknown" ? "unknown ?" : l.v)}</span></td>`; }).join("") +
        "</tr>").join("") : `<tr><td class="empty">no probes yet</td></tr>`;
  }
  $("probe-btn").onclick = async () => {
    if (probeWhy()) return;
    if (!confirm("Send one probe message now (it retries for up to 23 minutes until acked)?")) return;
    const r = await fetch("api/aprs/probe", { method: "POST", headers: { "content-type": "application/json", "X-Ranch-Radio": "1" },
      body: JSON.stringify({ armed: true }) });
    if (!r.ok) $("probe-why").textContent = (await r.json()).error || "refused";
    loadProbes();
  };

  // ---------- start

  window.aprsPage = { poll, openDetail, stations, objects, showTab, openThread, msgStatus };   // for the page tests
  loadMe().then(() => { showTab(location.hash.slice(1)); poll(); msgStatus(); loadThreads(); });
  setInterval(poll, POLL_MS);
  setInterval(() => { if (!$("aprs-messages").hidden) loadMessages(); else loadThreads(); }, 15000);
  setInterval(() => { if (!$("aprs-messages").hidden) msgStatus(); }, 2000);
  setInterval(loadMe, 60000);
})();
