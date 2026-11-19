"use strict";

// The Map tab: who you hear, who hears you, and what the station is doing.
//
//   ●  a station JTDX decoded (kept an hour), filled by what you still need;
//      bigger is stronger, fainter is older
//   ◇  a station that reported hearing you to PSKReporter (minutes old by
//      nature: every one carries its age and the SNR it gave you)
//   ◎  both, on the same band: a station you can work
//
// Each has its great-circle path to you: blue in, amber out, white both ways.
// The panel at the top left says what is on the air now: each radio (keyed or
// not, frequency, mode, from its own server) and JTDX (transmitting, listening,
// calling whom). While you transmit, rings leave your marker and a beam runs
// to the station JTDX is calling; a fresh decode pulses at its station and
// runs in along its path. Click a station for its card (both directions,
// distance, bearing, what it is to you) and to answer it, as on the Roster tab.
//
// Also: Maidenhead grids worked (blue) and confirmed (green), your own grid,
// the gray line, POTA activators on the band. The background is picked from
// a menu (basemaps.js: NASA's Blue Marble by default, with the night side's
// city lights; terrain, topo, imagery, streets), its tiles fetched by the
// browser; without internet everything still draws on a plain background. Drag to pan, wheel, double-click or +/- to zoom.
// No library: Web Mercator in world pixels, an SVG over the tiles. Everything
// is placed in the copy of the world centred on you, so a path over the
// Pacific is one line and not two halves.
//
// Server: addons/map.py ({map: {me, band, showing, grids, decodes}, heard,
// rig, radios, hears_me, pota}, whole on hello and in parts as each changes).
// Uses common.js (esc, store) and send().

const rrMapMath = (() => {
  const rad = Math.PI / 180;
  // The subsolar point (lat, lon) at a Date: the sun's declination and the
  // equation of time from the NOAA low-precision formulae (±0.1°, ample here).
  function subsolar(d) {
    const jd = d.getTime() / 86400000 + 2440587.5, n = jd - 2451545.0;
    const L = (280.46 + 0.9856474 * n) % 360, g = ((357.528 + 0.9856003 * n) % 360) * rad;
    const lam = (L + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * rad, eps = (23.439 - 0.0000004 * n) * rad;
    const dec = Math.asin(Math.sin(eps) * Math.sin(lam)) / rad;
    const ra = Math.atan2(Math.cos(eps) * Math.sin(lam), Math.cos(lam)) / rad;
    const gmst = (280.46061837 + 360.98564736629 * n) % 360;
    let lon = ((ra - gmst + 540) % 360) - 180;
    return { lat: dec, lon };
  }
  // The night side as a polygon of [lon, lat] over lo..hi degrees of longitude
  // (the terminator repeats every 360, so a map three worlds wide just asks for more).
  function night(d, lo = -180, hi = 180) {
    const s = subsolar(d), pts = [];
    const tanD = Math.abs(Math.tan(s.lat * rad)) < 1e-4 ? (s.lat >= 0 ? 1e-4 : -1e-4) : Math.tan(s.lat * rad);
    for (let lon = lo; lon <= hi; lon += 2) {
      const h = (lon - s.lon) * rad;
      pts.push([lon, Math.atan(-Math.cos(h) / tanD) / rad]);   // near an equinox: almost a meridian, as it should be
    }
    const pole = s.lat > 0 ? -90 : 90;            // the pole in winter's night
    return [[lo, pole], ...pts, [hi, pole]];
  }
  function isNight(d, lat, lon) {
    const s = subsolar(d);
    const cosz = Math.sin(lat * rad) * Math.sin(s.lat * rad) + Math.cos(lat * rad) * Math.cos(s.lat * rad) * Math.cos((lon - s.lon) * rad);
    return cosz < 0;
  }
  function gridLL(g) {                             // a grid's south-west corner and size (4 or 6 characters: the square)
    g = (g || "").toUpperCase().slice(0, 4);
    if (!/^[A-R]{2}[0-9]{2}$/.test(g)) return null;
    return { lon: (g.charCodeAt(0) - 65) * 20 - 180 + Number(g[2]) * 2, lat: (g.charCodeAt(1) - 65) * 10 - 90 + Number(g[3]), w: 2, h: 1 };
  }
  // The centre of a locator: of its subsquare when it has six characters (5 km), else of its square.
  function gridCentre(g) {
    const b = gridLL(g);
    if (!b) return null;
    const sub = /^[A-Xa-x]{2}$/.test((g || "").slice(4, 6)) ? g.slice(4, 6).toUpperCase() : null;
    if (!sub) return { lat: b.lat + 0.5, lon: b.lon + 1 };
    return { lat: b.lat + (sub.charCodeAt(1) - 65) / 24 + 1 / 48, lon: b.lon + (sub.charCodeAt(0) - 65) / 12 + 1 / 24 };
  }
  // Great circle from a to b ({lat, lon}): distance and the bearing to set out on.
  function path(a, b) {
    const p1 = a.lat * rad, p2 = b.lat * rad, dl = (b.lon - a.lon) * rad;
    const hav = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
    const brg = Math.atan2(Math.sin(dl) * Math.cos(p2), Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl));
    return { km: 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(hav))), bearing: (brg / rad + 360) % 360 };
  }
  // n+1 points [lat, lon] along it. Longitude runs on from a's without the
  // jump at ±180 (Tokyo from Arizona ends near -220, not +140): one line.
  function arc(a, b, n = 24) {
    const p1 = a.lat * rad, l1 = a.lon * rad, p2 = b.lat * rad, l2 = b.lon * rad;
    const d = path(a, b).km / 6371, out = [];
    let prev = a.lon;
    for (let i = 0; i <= n; i++) {
      const f = i / n;
      let lat, lon;
      if (d < 1e-6 || Math.PI - d < 1e-6) { lat = a.lat + (b.lat - a.lat) * f; lon = a.lon + (b.lon - a.lon) * f; }
      else {
        const A = Math.sin((1 - f) * d) / Math.sin(d), B = Math.sin(f * d) / Math.sin(d);
        const x = A * Math.cos(p1) * Math.cos(l1) + B * Math.cos(p2) * Math.cos(l2);
        const y = A * Math.cos(p1) * Math.sin(l1) + B * Math.cos(p2) * Math.sin(l2);
        lat = Math.atan2(A * Math.sin(p1) + B * Math.sin(p2), Math.hypot(x, y)) / rad;
        lon = Math.atan2(y, x) / rad;
      }
      lon += 360 * Math.round((prev - lon) / 360);
      prev = lon;
      out.push([lat, lon]);
    }
    return out;
  }
  return { subsolar, night, isNight, gridLL, gridCentre, path, arc };
})();

(() => {
  const TILE = 256, ZMIN = 1, ZMAX = 10, SVGNS = "http://www.w3.org/2000/svg";
  const NEED = { dxcc: "#e5484d", dxcc_band: "#f76b15", dxcc_mode: "#ffc53d", grid: "#5b7fff", grid_band: "#5b7fff",
                 dxcc_unconfirmed: "#a970e0", call_band_mode: "#00d8ff" };
  const NEED_WORD = { dxcc: "new DXCC", dxcc_band: "new DXCC on this band", dxcc_mode: "new DXCC in this mode", grid: "new grid",
                      grid_band: "new grid on this band", dxcc_unconfirmed: "DXCC worked, never confirmed", call_band_mode: "new call" };
  const LAYERS = [["hear", "You hear", "Stations JTDX decoded"], ["heard", "Hears you", "Stations that reported you to PSKReporter"],
                  ["paths", "Paths", "The great-circle path to each station"], ["grids", "Grids", "Grids worked and confirmed"],
                  ["pota", "POTA", "Parks on the Air activators on the band"], ["night", "Night", "The gray line"]];
  const WINDOWS = [15, 30, 60];
  const LIVE_S = 120;                            // the roster's own memory: a decode this fresh may be answered
  const FX_MAX = 40;                             // decodes animated per push (a busy band gives a hundred a cycle)
  const keep = typeof store !== "undefined" ? store : { get: (k, d) => d, set() {} };

  let body = null, snap = {}, z = 3, cx = null, cy = null, w = 0, h = 0, built = false, drag = null, centred = false;
  let sel = null, skew = 0, stations = [], me = null, timer = null, fitted = false;
  let layers = { hear: true, heard: true, paths: true, grids: true, pota: true, night: true, ...keep.get("map.layers", {}) };
  let windowMin = WINDOWS.includes(keep.get("map.window", 60)) ? keep.get("map.window", 60) : 60;
  let listOpen = keep.get("map.list", window.innerWidth > 700);   // on a phone the map comes first
  // The list's tab (2026-11-19, UX plan PR 0.2): it opened on Two-way even when
  // that was empty and "You hear" was full. Two values now: the operator's
  // preference, set only by a click on a tab and kept, and the tab shown. With
  // no preference the shown tab is the first of Two-way, Hear you, You hear
  // that has rows, worked out again each time the stations are. A click on the
  // map moves only the shown tab, and only while that station stays selected.
  const TABS = ["both", "tx", "rx"];
  let tabPref = TABS.includes(keep.get("map.tab")) ? keep.get("map.tab") : null;
  let listTab = tabPref || "both", tabHeld = null;
  const seen = new Set();
  let base = null, lights = null, nightPts = [];   // the background (basemaps.js), its night side, the terminator
  const px = (lon, zz = z) => ((lon + 180) / 360) * TILE * 2 ** zz;
  const py = (lat, zz = z) => {
    const s = Math.sin((Math.max(-85, Math.min(85, lat)) * Math.PI) / 180);
    return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * TILE * 2 ** zz;
  };
  const nowS = () => Date.now() / 1000 + skew;   // the server's clock: ages are its, whatever this browser's says
  const ref = () => (me ? me.lon : -100);
  const near = (lon) => lon + 360 * Math.round((ref() - lon) / 360);   // the copy of the world centred on you
  const $$ = (s) => body.querySelector(s);
  const db = (v) => (v == null ? "?" : `${v > 0 ? "+" : ""}${v}`);
  const km = (v) => (v == null ? "" : `${Math.round(v).toLocaleString()} km`);
  const compass = (deg) => ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8];
  function ago(t) {
    const s = Math.max(0, nowS() - t);
    return s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`;
  }

  // ------------------------------------------------------------ the model

  // A station given only a four-character grid sits somewhere in a square 2°
  // by 1°; a dozen in one square would be one dot. Each is put at its own
  // spot in the square (from its callsign, so it stays put).
  function inSquare(call, grid) {
    const b = rrMapMath.gridLL(grid);
    if (!b) return null;
    let hsh = 2166136261;
    for (const c of call) hsh = Math.imul(hsh ^ c.charCodeAt(0), 16777619) >>> 0;
    return { lat: b.lat + 0.15 + ((hsh & 0xffff) / 0xffff) * 0.7, lon: b.lon + 0.25 + (((hsh >>> 16) & 0xffff) / 0xffff) * 1.5 };
  }

  // One row per station: rx (you hear it), tx (it hears you), live (a decode
  // fresh enough to answer), and two-way when both are on one band.
  function model() {
    const M = snap.map || {}, band = M.showing || "all", now = nowS(), maxAge = windowMin * 60;
    const hb = snap.hears_me;
    me = rrMapMath.gridCentre(M.me) || rrMapMath.gridCentre(hb && hb.locator);
    const by = new Map();
    const row = (call) => by.get(call) || by.set(call, { call, rx: null, tx: null, live: null }).get(call);
    const inBand = (b) => band === "all" || b === band;
    for (const hd of snap.heard || []) {
      if (!inBand(hd.band) || now - hd.t > maxAge) continue;
      const r = row(hd.call);
      if (!r.rx || hd.t > r.rx.t) r.rx = hd;
    }
    for (const d of M.decodes || []) {
      const r = row(d.call);
      r.live = d;
      if (!r.rx) r.rx = { call: d.call, grid: d.grid, band: d.band, mode: d.mode, snr: d.snr, best: d.snr, t: now - (d.age || 0), n: 1, need: d.need, entity: d.entity };
    }
    const reports = new Map();
    for (const rc of (hb && hb.receivers) || []) {
      if (!inBand(rc.band) || now - rc.t > maxAge) continue;
      row(rc.call);
      reports.set(rc.call, [...(reports.get(rc.call) || []), rc]);
    }
    for (const [call, rs] of reports) {
      // With every band showing, the report on the band you hear it on is the one that makes it two-way.
      const r = by.get(call), same = r.rx ? rs.filter((x) => x.band === r.rx.band) : [];
      r.tx = (same.length ? same : rs).reduce((x, y) => (y.t > x.t ? y : x));
    }
    stations = [];
    for (const r of by.values()) {
      const at = r.tx ? { lat: r.tx.lat, lon: r.tx.lon } : inSquare(r.call, r.rx.grid);
      if (!at) continue;
      r.both = !!(r.rx && r.tx && r.rx.band === r.tx.band);
      r.kind = r.both ? "both" : r.rx ? "rx" : "tx";
      r.lat = at.lat;
      r.t = Math.max(r.rx ? r.rx.t : 0, r.tx ? r.tx.t : 0);
      if (me) {
        const p = rrMapMath.path(me, at);
        r.km = p.km; r.bearing = p.bearing;
        r.arc = rrMapMath.arc(me, at, Math.max(2, Math.min(32, Math.ceil(p.km / 350))));
        r.lon = r.arc[r.arc.length - 1][1];       // where its path ends: the same copy of the world as the line
      } else { r.km = null; r.bearing = null; r.arc = null; r.lon = near(at.lon); }
      stations.push(r);
    }
    if (sel && !by.has(sel)) sel = null;
    if (!(tabHeld && tabHeld === sel)) {
      tabHeld = null;
      const c = counts();
      listTab = tabPref || TABS.find((k) => c[k]) || "both";
    }
  }
  const shown = (r) => (r.rx && layers.hear) || (r.tx && layers.heard);
  const counts = () => ({ rx: stations.filter((r) => r.rx).length, tx: stations.filter((r) => r.tx).length,
                          both: stations.filter((r) => r.both).length });

  // What is on the air: each radio by its own server's word, and JTDX by its Status.
  function onAir() {
    const radios = (snap.radios || []).filter((r) => r.present);
    const rig = snap.rig || {};
    const keyed = radios.filter((r) => r.connected && r.tx);
    const jt = !!(rig.alive && rig.transmitting);
    const dx = rig.alive && rig.dx_call ? rig.dx_call : "";
    return { radios, rig, keyed, jt, tx: jt || keyed.length > 0, dx };
  }
  // Where the station JTDX is calling is: on the map already, or at the grid JTDX has for it.
  function dxTarget(a) {
    if (!a.dx || !me) return null;
    const r = stations.find((s) => s.call === a.dx);
    if (r && r.arc) return { call: a.dx, arc: r.arc };
    const at = rrMapMath.gridCentre(a.rig.dx_grid);
    return at ? { call: a.dx, arc: rrMapMath.arc(me, at, 24) } : null;
  }

  // ------------------------------------------------------------ building the page

  function build() {
    body.innerHTML = `<div class="rm-bar stat"></div><div class="rm-wrap"><div class="rm-tiles"></div><div class="rm-tiles rm-lights" hidden></div><svg class="rm-svg">
        <g class="rm-world"><g class="rm-night"></g><g class="rm-grids"></g><g class="rm-paths"></g><g class="rm-pota"></g>
        <g class="rm-beam"></g><g class="rm-st"></g><g class="rm-me"></g><g class="rm-fx"></g></g></svg>
        <div class="rm-hud"></div><div class="rm-card" hidden></div>
        <div class="rm-list"></div>
        <div class="rm-ctl"><button class="chip chip-mini" data-z="1" title="Zoom in">+</button><button class="chip chip-mini" data-z="-1" title="Zoom out">−</button>
          <button class="chip chip-mini" data-fit title="Frame you and every station shown">Fit</button>
          <button class="chip chip-mini" data-home title="Centre on your station">Me</button></div>
        <a class="rm-attr" target="_blank" rel="noopener"></a></div>`;
    const wrap = $$(".rm-wrap");
    // The background, with its menu beside the zoom buttons. Over the Earth
    // one, the night side is the Earth's own city lights.
    base = basemaps.layer($$(".rm-tiles"), { key: "world", def: "earth", cls: "rm-tile", attr: $$(".rm-attr"), onChange: () => draw() });
    lights = basemaps.layer($$(".rm-lights"), { fixed: "lights", cls: "rm-light" });
    $$(".rm-ctl").append(base.control());
    for (const b of body.querySelectorAll("[data-z]")) b.onclick = () => zoom(Number(b.dataset.z));
    $$("[data-fit]").onclick = () => { fit(); draw(); };
    $$("[data-home]").onclick = () => { if (me) { cx = px(ref()); cy = py(me.lat); draw(); } };
    // Drawn while the tab is hidden the map has no size; draw again the moment it has one.
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => draw()).observe(wrap);
    const at = (e) => { const r = wrap.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    basemaps.wheel(wrap, { zoom: (dz, x, y) => zoom(dz, x, y),
      pan: (dx, dy) => { if (cx == null) return; cx += dx; cy += dy; draw(); },
      skip: (e) => !!e.target.closest(".rm-list, .rm-card") });     // those scroll
    wrap.addEventListener("dblclick", (e) => { if (!e.target.closest(".rm-list, .rm-card, .rm-hud, .rm-ctl, .rm-s")) zoom(1, ...at(e)); });
    wrap.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".rm-s, .rm-ctl, .rm-list, .rm-card, .rm-hud, .rm-attr")) return;
      drag = { x: e.clientX, y: e.clientY, cx, cy, moved: false };
      wrap.setPointerCapture(e.pointerId);
    });
    wrap.addEventListener("pointermove", (e) => {
      if (!drag) return;
      if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 3) drag.moved = true;
      cx = drag.cx - (e.clientX - drag.x); cy = drag.cy - (e.clientY - drag.y);
      pan();
    });
    const end = () => { if (drag && !drag.moved && sel) select(null); drag = null; };
    wrap.addEventListener("pointerup", end);
    wrap.addEventListener("pointercancel", () => { drag = null; });
    $$(".rm-st").addEventListener("click", (e) => { const g = e.target.closest(".rm-s"); if (g) select(g.dataset.call); });
    $$(".rm-list").addEventListener("click", (e) => {
      const t = e.target.closest("[data-tab]"), r = e.target.closest("[data-call]");
      if (e.target.closest("[data-toggle]")) { listOpen = !listOpen; keep.set("map.list", listOpen); list(); }
      else if (t) { listTab = tabPref = t.dataset.tab; tabHeld = null; keep.set("map.tab", tabPref); list(); }
      else if (r) { select(r.dataset.call, true); }
    });
    $$(".rm-card").addEventListener("click", (e) => {
      if (e.target.closest("[data-x]")) select(null);
      const a = e.target.closest("[data-answer]");
      if (a) send({ t: "addon", addon: "map", do: "reply", key: a.dataset.answer, call: a.dataset.call });
    });
    built = true;
    if (timer) clearInterval(timer);
    timer = setInterval(() => { if (body && built && snap.map) { model(); bar(); list(); card(); draw(); } }, 30000);   // ages fade, the gray line moves
  }

  function zoom(dz, mx = w / 2, my = h / 2) {
    const nz = Math.max(ZMIN, Math.min(ZMAX, z + dz));
    if (nz === z || cx == null) return;
    const f = 2 ** (nz - z);
    cx = (cx + mx - w / 2) * f - (mx - w / 2);
    cy = (cy + my - h / 2) * f - (my - h / 2);
    z = nz; draw();
  }

  // Frame you and every station shown, clear of the list on the right.
  function fit() {
    // The stations and, below the Arctic, their paths (the way to Japan from the
    // States runs up over Alaska). A path over the pole is not framed: Mercator
    // stretches it without end, and the world would shrink to a stamp.
    const pts = stations.filter(shown).flatMap((r) => [[r.lat, r.lon], ...(r.arc && layers.paths ? r.arc.filter((p) => Math.abs(p[0]) < 66) : [])]);
    if (me) pts.push([me.lat, ref()]);
    if (!pts.length || !w || !h) return;
    const lats = pts.map((p) => p[0]), lons = pts.map((p) => p[1]);
    const [la0, la1, lo0, lo1] = [Math.min(...lats), Math.max(...lats), Math.min(...lons), Math.max(...lons)];
    const side = listOpen && w > 700 ? 300 : 0;
    z = 7;
    while (z > ZMIN && (px(lo1) - px(lo0) > w - side - 80 || py(la0) - py(la1) > h - 80)) z--;
    cx = (px(lo0) + px(lo1)) / 2 + side / 2;
    cy = (py(la0) + py(la1)) / 2;
  }

  function el(tag, attrs, parent, title) {
    const e = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (title) e.appendChild(Object.assign(document.createElementNS(SVGNS, "title"), { textContent: title }));
    parent.appendChild(e);
    return e;
  }

  // Over the Earth background the night side is drawn as what is there: its city lights.
  const litNight = () => base.id() === "earth" && layers.night;

  function drawTiles() {
    base.render(z, cx, cy, w, h);
    const host = $$(".rm-lights");
    host.hidden = !litNight();
    if (host.hidden) return;
    lights.render(z, cx, cy, w, h);
    // Only the night side of it: the terminator, in this view's pixels.
    host.style.clipPath = `polygon(${nightPts.map(([lo, la]) => `${(px(lo) - cx + w / 2).toFixed(0)}px ${(py(la) - cy + h / 2).toFixed(0)}px`).join(",")})`;
  }

  // A drag only moves what is drawn; nothing is rebuilt.
  function pan() {
    drawTiles();
    $$(".rm-world").setAttribute("transform", `translate(${w / 2 - cx},${h / 2 - cy})`);
  }

  const line = (pts) => "M" + pts.map(([la, lo]) => `${px(lo).toFixed(1)},${py(la).toFixed(1)}`).join("L");
  // Stronger is bigger; the whole world on one screen wants smaller marks than one country.
  const radius = (snr) => (snr == null ? 4.5 : snr >= 0 ? 6.5 : snr >= -10 ? 5.5 : snr >= -18 ? 4.5 : 3.5) * (z <= 2 ? 0.62 : z === 3 ? 0.8 : 1);
  const fade = (t, live) => (live ? 1 : Math.max(0.4, 1 - (0.6 * Math.max(0, nowS() - t)) / (windowMin * 60)).toFixed(2));
  function tip(r) {
    return [r.call, r.rx ? `you hear it ${db(r.rx.snr)} dB, ${ago(r.rx.t)}` : "", r.tx ? `it hears you ${db(r.tx.snr)} dB, ${ago(r.tx.t)}` : "",
            r.km != null ? km(r.km) : ""].filter(Boolean).join(" · ");
  }

  function draw() {
    if (!body || !built) return;
    const wrap = $$(".rm-wrap");
    w = wrap.clientWidth; h = wrap.clientHeight;
    if (!w || !h) return;                         // hidden: nothing to place yet
    const M = snap.map;
    if (cx == null || (!centred && me)) { cx = px(ref()); cy = py(me ? me.lat : 35); centred = !!me; }
    if (!fitted && me && stations.some(shown)) { fitted = true; fit(); }     // the first stations: frame them, once
    nightPts = rrMapMath.night(new Date(), ref() - 540, ref() + 540);
    pan();
    const g = (c) => { const x = $$(`.rm-${c}`); x.innerHTML = ""; return x; };
    const nightG = g("night"), gridsG = g("grids"), pathsG = g("paths"), potaG = g("pota"), stG = g("st");
    if (layers.night) {
      const pts = nightPts.map(([lo, la]) => `${px(lo).toFixed(1)},${py(la).toFixed(1)}`).join(" ");
      el("polygon", { points: pts, class: `rm-nightp${litNight() ? " lit" : ""}` }, nightG);
    }
    if (!M) return;
    if (layers.grids) for (const [gr, st] of Object.entries(M.grids || {})) {
      const b = rrMapMath.gridLL(gr); if (!b) continue;
      const lon = near(b.lon + 1) - 1;
      el("rect", { x: px(lon), y: py(b.lat + b.h), width: px(lon + b.w) - px(lon), height: py(b.lat) - py(b.lat + b.h),
                   class: st === "c" ? "rm-gc" : "rm-gw" }, gridsG, `${gr}: ${st === "c" ? "confirmed" : "worked"}`);
    }
    if (layers.pota) for (const s of snap.pota || []) {
      const x = px(near(s.lon)), y = py(s.lat);
      el("path", { d: `M${x},${y - 6} L${x + 5},${y + 4} L${x - 5},${y + 4} Z`, class: "rm-potap" }, potaG,
         `${s.call} POTA ${s.ref} ${s.name} — ${s.khz} kHz ${s.mode}`);
    }
    const vis = stations.filter(shown).sort((a, b) => (a.both - b.both) || (a.t - b.t));     // two-way and freshest on top
    const labels = z >= 6 || vis.length <= 30;
    const thin = Math.max(0.3, Math.min(1, Math.sqrt(40 / Math.max(1, vis.length))));       // hundreds of paths: each fainter
    for (const r of vis) {
      const live = !!r.live, on = r.call === sel;
      if (r.arc && (layers.paths || on)) {
        el("path", { d: line(r.arc), class: `rm-p rm-p-${r.kind}${on ? " sel" : ""}`, "stroke-opacity": on ? 1 : (fade(r.t, live) * (r.both ? 0.8 : 0.45) * thin).toFixed(2) }, pathsG);
      }
      const gs = el("g", { class: `rm-s rm-s-${r.kind}${on ? " sel" : ""}${r.live && r.live.to_me ? " rm-tome" : ""}${live ? " live" : ""}`,
                           "data-call": r.call, transform: `translate(${px(r.lon).toFixed(1)},${py(r.lat).toFixed(1)})`, opacity: on ? 1 : fade(r.t, live) }, stG, tip(r));
      const need = (r.live && r.live.need) || (r.rx && r.rx.need);
      if (r.rx) {
        const rr = radius(r.rx.snr);
        if (r.both) el("circle", { r: rr + (z <= 2 ? 2.2 : 3.5), class: "rm-ring" }, gs);
        el("circle", { r: rr, fill: NEED[need] || "#9aa0a6", class: "rm-d" }, gs);
      } else {
        const d = radius(r.tx.snr) + 1;
        el("path", { d: `M0,${-d} L${d},0 L0,${d} L${-d},0 Z`, class: "rm-dia" }, gs);
      }
      if (labels || on || (r.live && r.live.to_me)) el("text", { x: 9, y: 4, class: "rm-lab" }, gs).textContent = r.call;
    }
    drawAir();
  }

  // What is on the air: your marker, the beam, the panel. A VFO being turned
  // changes the frequency many times a second; the stations are not redrawn for it.
  function drawAir() {
    if (!body || !built || !w || !h) return;
    const clear = (c) => { const x = $$(`.rm-${c}`); x.innerHTML = ""; return x; };
    const beamG = clear("beam"), meG = clear("me");
    const a = onAir();
    if (me) {
      const x = px(ref()), y = py(me.lat);
      const t = a.jt ? dxTarget(a) : null;          // only JTDX's own transmission has a known target (not a keyed mic)
      if (t) {
        el("path", { d: line(t.arc), class: "rm-beamp" }, beamG);
        const [la, lo] = t.arc[t.arc.length - 1];
        el("text", { x: px(lo) + 10, y: py(la) - 13, class: "rm-beamt" }, beamG).textContent = `→ ${t.call}`;
      }
      const gm = el("g", { class: `rm-meg${a.tx ? " tx" : ""}${a.rig.alive && a.rig.decoding ? " dec" : ""}`, transform: `translate(${x},${y})` }, meG,
                    a.tx ? "You are transmitting" : "Your station");
      if (a.tx) for (const i of [0, 1, 2]) el("circle", { r: 8, class: "rm-txring", style: `animation-delay:${i * 0.6}s` }, gm);
      el("circle", { r: 7, class: "rm-mep" }, gm);
    }
    hud(a);
  }

  // A fresh decode: a ring at its station, and a dash running in along its path to you.
  function receive(decodes) {
    if (!body || !built || !w || !layers.hear) { for (const d of decodes) seen.add(d.key); return; }
    if (seen.size > 4000) seen.clear();
    const fx = $$(".rm-fx");
    let n = 0;
    for (const d of decodes) {
      if (seen.has(d.key)) continue;
      seen.add(d.key);
      const r = stations.find((s) => s.call === d.call);
      if (!r || (d.age || 0) > 20 || n++ >= FX_MAX) continue;
      const made = [el("circle", { cx: px(r.lon), cy: py(r.lat), r: 6, class: "rm-pulse" }, fx)];
      if (r.arc) made.push(el("path", { d: line([...r.arc].reverse()), pathLength: 100, class: "rm-in" }, fx));
      setTimeout(() => made.forEach((e) => e.remove()), 1700);
    }
  }

  // ------------------------------------------------------------ the panels over the map

  function hud(a) {
    const mhz = (hz) => (hz ? (hz / 1e6).toFixed(3) : "");
    const rows = a.radios.map((r) => {
      const st = !r.connected ? ["off", r.connected === false ? "no rig link" : "…"] : r.tx ? ["tx", "TX"] : ["ok", "RX"];
      return `<div class="rm-air${st[0] === "tx" ? " tx" : ""}" title="${esc(r.name)}: what its own server says"><i class="st-dot ${st[0]}"></i><b>${esc(r.name)}</b>
        <span>${r.connected ? `${mhz(r.freq)} ${esc(r.mode || "")}${r.data ? "-D" : ""}` : ""}</span><em>${st[1]}</em></div>`;
    });
    const j = a.rig;
    if (j.alive) {
      const what = j.transmitting ? (a.dx ? `transmitting → ${esc(a.dx)}` : "transmitting")
        : j.tx_enabled ? (a.dx ? `listening · will call ${esc(a.dx)}` : "listening · TX enabled") : "listening";
      rows.push(`<div class="rm-air${j.transmitting ? " tx" : ""}" title="JTDX's own Status"><i class="st-dot ${j.transmitting ? "tx" : "ok"}"></i><b>JTDX</b>
        <span>${esc(j.band || "")} ${esc(j.mode || "")}</span><em>${what}${j.decoding && !j.transmitting ? " · decoding" : ""}</em></div>`);
    } else {
      rows.push(`<div class="rm-air dim" title="No Status from JTDX in the last 45 seconds"><i class="st-dot off"></i><b>JTDX</b><span></span><em>not heard from</em></div>`);
    }
    $$(".rm-hud").innerHTML = `<div class="rm-hud-h${a.tx ? " tx" : ""}">${a.tx ? "ON THE AIR" : "Listening"}</div>${rows.join("")}`;
  }

  function bar() {
    const el2 = body && $$(".rm-bar"); if (!el2) return;
    if (el2.contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;   // a menu is open: leave it be
    const M = snap.map || {}, n = Object.values(M.grids || {}), c = counts();
    const sel2 = ["current", "all", "160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]
      .map((b) => `<option${(b === "current" ? !snap.chosen : snap.chosen === b) ? " selected" : ""}>${b}</option>`).join("");
    const far = (k) => stations.filter((r) => r[k] && r.km != null).sort((x, y) => y.km - x.km)[0];
    const fr = far("rx"), ft = far("tx");
    el2.innerHTML = `<span>band <select class="rm-band rr-select">${sel2}</select> (${esc(M.showing || "")})</span>
      <span>last <select class="rm-win rr-select">${WINDOWS.map((m) => `<option value="${m}"${m === windowMin ? " selected" : ""}>${m} min</option>`).join("")}</select></span>
      <span class="rm-sum"><b class="rm-n-rx">${c.rx}</b> you hear${fr ? ` <small>(farthest ${esc(fr.call)} ${km(fr.km)})</small>` : ""}</span>
      <span class="rm-sum"><b class="rm-n-tx">${snap.hears_me ? c.tx : "–"}</b> hear you${ft ? ` <small>(farthest ${esc(ft.call)} ${km(ft.km)})</small>` : ""}</span>
      <span class="rm-sum"><b class="rm-n-both">${snap.hears_me ? c.both : "–"}</b> two-way</span>
      <span class="rm-layers">${LAYERS.map(([k, t, why]) => `<button class="chip chip-mini${layers[k] ? " on" : ""}" data-layer="${k}" title="${esc(why)}">${t}</button>`).join("")}</span>
      <span class="rm-dim">${n.length} grids worked, ${n.filter((s) => s === "c").length} confirmed${snap.pota ? ` · ${snap.pota.length} POTA` : ""}${snap.pota_error ? ` <span class="lb-bad">${esc(snap.pota_error)}</span>` : ""}</span>
      <span class="rm-key"><i class="rm-kd"></i>you hear <i class="rm-kdia"></i>hears you <i class="rm-kboth"></i>two-way
        ${Object.entries({ dxcc: "DXCC", dxcc_band: "band", dxcc_mode: "mode", grid: "grid", dxcc_unconfirmed: "unconfirmed", call_band_mode: "new call" })
          .map(([k, t]) => `<i class="rm-dot" style="background:${NEED[k]}"></i>${t}`).join(" ")}</span>`;
    // blur: a menu that keeps the focus after its choice would hold the bar still (above) for good
    el2.querySelector(".rm-band").onchange = (e) => { e.target.blur(); snap.chosen = e.target.value === "current" ? null : e.target.value; send({ t: "addon", addon: "map", do: "show", band: e.target.value }); };
    el2.querySelector(".rm-win").onchange = (e) => { e.target.blur(); windowMin = Number(e.target.value); keep.set("map.window", windowMin); refresh(); };
    for (const b of el2.querySelectorAll("[data-layer]")) b.onclick = () => { layers[b.dataset.layer] = !layers[b.dataset.layer]; keep.set("map.layers", layers); refresh(); };
  }

  // The stations by name: two-way, hear you, you hear; farthest first.
  function list() {
    const box = body && $$(".rm-list"); if (!box) return;
    const c = counts(), hb = snap.hears_me;
    box.classList.toggle("shut", !listOpen);
    if (!listOpen) { box.innerHTML = `<button class="chip chip-mini" data-toggle title="Show the stations by name">Stations</button>`; return; }
    const tabs = [["both", "Two-way", c.both], ["tx", "Hear you", c.tx], ["rx", "You hear", c.rx]];
    const pick = stations.filter((r) => (listTab === "both" ? r.both : r[listTab])).sort((a, b) => (b.km || 0) - (a.km || 0));
    const rows = pick.slice(0, 200).map((r) => `<button class="rm-row${r.call === sel ? " sel" : ""}${r.live ? " live" : ""}" data-call="${esc(r.call)}" title="${esc(tip(r))}">
        <b>${esc(r.call)}</b><span class="rm-where">${esc((r.rx && r.rx.entity) || (r.tx && r.tx.country) || "")}</span>
        <span class="rm-num">${r.km != null ? km(r.km) : ""}</span>
        <span class="rm-num rm-in-t" title="You hear it at">${r.rx ? `▼${db(r.rx.snr)}` : ""}</span>
        <span class="rm-num rm-out-t" title="It hears you at">${r.tx ? `▲${db(r.tx.snr)}` : ""}</span></button>`).join("");
    const empty = { both: "No station both hears you and is heard, yet.", tx: "Nobody has reported hearing you in this window.",
                    rx: "Nothing heard in this window." }[listTab];
    const src = !hb ? `<div class="rm-src warn">${esc(snap.hears_me_why || "")}</div>`
      : `<div class="rm-src${hb.error ? " warn" : ""}">Hears you: PSKReporter reports of ${esc(hb.call)}, last ${Math.min(windowMin, hb.window_min)} min` +
        `${hb.checked ? `, asked ${ago(hb.checked)}` : ", not asked yet"} (every ${Math.round(hb.every_s / 60)} min)${hb.error ? `. ${esc(hb.error)}: showing the last answer.` : "."}</div>`;
    const top = box.querySelector(".rm-rows")?.scrollTop || 0;
    box.innerHTML = `<div class="rm-list-h"><span class="seg">${tabs.map(([k, t, n]) => `<button class="chip chip-mini${k === listTab ? " on" : ""}" data-tab="${k}">${t} ${k !== "rx" && !hb ? "–" : n}</button>`).join("")}</span>
        <button class="chip chip-mini" data-toggle title="Hide the list">✕</button></div>
      <div class="rm-rows">${rows || `<div class="rm-none">${empty}</div>`}</div>${src}`;
    box.querySelector(".rm-rows").scrollTop = top;      // redrawn on every decode: stay where the operator scrolled
  }

  function card() {
    const box = body && $$(".rm-card"); if (!box) return;
    const r = sel && stations.find((s) => s.call === sel);
    box.hidden = !r;
    if (!r) return;
    const d = r.live, need = (d && d.need) || (r.rx && r.rx.need);
    const where = [(r.rx && r.rx.entity) || (r.tx && r.tx.country), (r.tx && r.tx.grid) || (r.rx && r.rx.grid),
                   r.km != null ? `${km(r.km)} at ${Math.round(r.bearing)}° ${compass(r.bearing)}` : ""].filter(Boolean).join(" · ");
    const rx = r.rx ? `<b>${db(r.rx.snr)} dB</b> ${esc(r.rx.band)} ${esc(r.rx.mode)} · ${ago(r.rx.t)}${r.rx.n > 1 ? ` · ${r.rx.n} decodes, best ${db(r.rx.best)}` : ""}`
      : `<span class="rm-dim">not in the last ${windowMin} min</span>`;
    const tx = r.tx ? `<b>${db(r.tx.snr)} dB</b> ${esc(r.tx.band)} ${esc(r.tx.mode)} · ${ago(r.tx.t)} <span class="rm-dim">(PSKReporter)</span>`
      : `<span class="rm-dim">${snap.hears_me ? `no report in the last ${windowMin} min` : "not known: PSKReporter isn't set up"}</span>`;
    const can = d && d.answer;
    const why = !d ? "Nothing fresh from it to answer" : d.answer === "cq" ? `Answer ${r.call}'s CQ in JTDX`
      : d.answer === "line" ? `${r.call} isn't calling CQ: JTDX answers this only if its UDP reply filter accepts any message` : "Nothing fresh from it to answer";
    box.innerHTML = `<div class="rm-card-h"><b>${esc(r.call)}</b>${r.both ? `<span class="rm-tag both">two-way</span>` : ""}${d && d.to_me ? `<span class="rm-tag me">calling you</span>` : ""}
        ${d && d.lotw ? `<span class="rm-tag" title="uses LoTW">LoTW</span>` : ""}<span class="spacer"></span><button class="chip chip-mini" data-x title="Close">✕</button></div>
      <div class="rm-card-w">${esc(where)}</div>
      <div class="rm-dir"><span class="rm-in-t">▼ You hear it</span><span>${rx}</span></div>
      <div class="rm-dir"><span class="rm-out-t">▲ It hears you</span><span>${tx}</span></div>
      ${d ? `<div class="rm-msg">${esc(d.message || "")}</div>` : ""}
      ${window.callcard ? callcard.block("map", "map", r.call, d || r.rx || {}) : ""}
      <div class="rm-card-f">${need ? `<span class="rm-need"><i class="rm-dot" style="background:${NEED[need]}"></i>${esc(NEED_WORD[need] || need)}</span>` : "<span></span>"}
        <button class="chip chip-mini${can ? " on" : ""}" ${can ? `data-answer="${esc(d.key)}" data-call="${esc(r.call)}"` : "disabled"} title="${esc(why)}">${d && d.answer === "cq" ? "Answer CQ" : "Answer"}</button></div>`;
  }

  function select(call, show = false) {
    sel = call && call !== sel ? call : (show ? call : null);
    const r = sel && stations.find((s) => s.call === sel);
    if (r && show && w) {                         // from the list: bring it into view if it is off the map
      const x = px(r.lon) - cx + w / 2, y = py(r.lat) - cy + h / 2;
      if (x < 40 || x > w - (listOpen ? 340 : 40) || y < 40 || y > h - 40) { cx = px(r.lon) + (listOpen && w > 700 ? 150 : 0); cy = py(r.lat); }
    }
    if (r && !show && !(listTab === "both" ? r.both : r[listTab])) { listTab = r.kind; tabHeld = r.call; }    // from the map: the list follows to its tab
    card(); list(); draw();
    if (r && !show) $$(".rm-row.sel")?.scrollIntoView({ block: "nearest" });
  }

  function refresh() { model(); bar(); list(); card(); draw(); }

  function render() {
    if (!body) return;
    if (snap.error) { body.innerHTML = `<div class="st-empty">${esc(snap.error)}</div>`; built = false; return; }
    if (!built) build();
    refresh();
  }

  function take(m) {
    if (m.now) skew = m.now - Date.now() / 1000;
    Object.assign(snap, m);
  }

  window.rrMapMath = rrMapMath;               // the tests check the gray line and the paths against reference points
  window.rrMap = { stations: () => stations, onAir };       // and what the page worked out
  addons.register("map", {
    mount(b) { body = b; built = false; centred = false; fitted = false; cx = null; render(); },
    hello(s) { snap = { chosen: snap.chosen }; take(s || {}); for (const d of (snap.map && snap.map.decodes) || []) seen.add(d.key); render(); },
    event(m) {
      if (m.callcard) { window.callcard?.answer(m.callcard); return; }     // the station card's call card (static/callcard.js)
      if (m.error) { snap = { chosen: snap.chosen, error: m.error }; render(); return; }
      if (!("map" in m || "rig" in m || "hears_me" in m || "pota" in m)) return;
      delete snap.error;
      take(m);
      if (!body) return;
      if (!built) { render(); return; }
      if ("rig" in m && !("map" in m)) { drawAir(); return; }       // keyed or unkeyed: the marker, the beam, the panel
      render();
      if (m.map) receive(m.map.decodes || []);
    },
    off() { snap = {}; stations = []; sel = null; if (timer) { clearInterval(timer); timer = null; } if (body) body.innerHTML = ""; built = false; },
    tab() {
      if (!snap.map) return { dot: "off", title: "Map" };
      const a = onAir(), c = counts();
      return { dot: a.tx ? "tx" : "ok", title: `${a.tx ? "Transmitting. " : ""}You hear ${c.rx}${snap.hears_me ? `, ${c.tx} hear you, ${c.both} two-way` : ""}` };
    },
    card() {
      const M = snap.map;
      if (!M) return { state: "off", lines: [] };
      const a = onAir(), c = counts();
      return { state: a.tx ? "TX" : `${c.rx} heard`,
               lines: [snap.hears_me ? `${c.tx} hear you · ${c.both} two-way` : "who hears you: not set up", a.tx && a.dx ? `calling ${esc(a.dx)}` : ""].filter(Boolean) };
    },
  });
})();
