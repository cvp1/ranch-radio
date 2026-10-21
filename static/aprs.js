"use strict";

// APRS on the packet page (APRS plan M2): Stations, Map, Messages and Raw
// tabs over the hub's store. Read only: nothing here can transmit. Stations
// and objects arrive as deltas (?rev=), with tombstones for what expired.
// Uses packet.html's $ and esc, and meshmap.js.

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

  // ---------- Messages (read only until M3)

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
  }
  $("msg-with").addEventListener("change", loadMessages);

  // ---------- start

  window.aprsPage = { poll, openDetail, stations, objects, showTab };   // for the page test
  loadMe().then(() => { showTab(location.hash.slice(1)); poll(); });
  setInterval(poll, POLL_MS);
  setInterval(() => { if (!$("aprs-messages").hidden) loadMessages(); }, 15000);
  setInterval(loadMe, 60000);
})();
