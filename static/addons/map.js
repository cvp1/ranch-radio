"use strict";

// The Map tab: Maidenhead grids you have worked (blue) and confirmed (green),
// on the band JTDX is on or all bands; who JTDX is hearing now at their grids,
// coloured by need (click one to answer it, as on the Roster tab); your own
// grid; the gray line; POTA activators spotted on the band. OpenStreetMap
// tiles fetched by the browser (as on the Mesh map); without internet the
// grids and dots still draw on a plain background. Drag to pan, wheel or +/-
// to zoom. No library: Web Mercator in world pixels, an SVG over the tiles.
//
// Server: addons/map.py ({map: {me, band, showing, grids, decodes}, pota}).
// Uses common.js (esc) and send().

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
  // The night side as a polygon of [lon, lat], -180..180.
  function night(d) {
    const s = subsolar(d), pts = [];
    const tanD = Math.abs(Math.tan(s.lat * rad)) < 1e-4 ? (s.lat >= 0 ? 1e-4 : -1e-4) : Math.tan(s.lat * rad);
    for (let lon = -180; lon <= 180; lon += 2) {
      const h = (lon - s.lon) * rad;
      pts.push([lon, Math.atan(-Math.cos(h) / tanD) / rad]);   // near an equinox: almost a meridian, as it should be
    }
    const pole = s.lat > 0 ? -90 : 90;            // the pole in winter's night
    return [[-180, pole], ...pts, [180, pole]];
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
  return { subsolar, night, isNight, gridLL };
})();

(() => {
  const TILE = 256, ZMIN = 1, ZMAX = 8, SVGNS = "http://www.w3.org/2000/svg";
  const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  const NEED = { dxcc: "#e5484d", dxcc_band: "#f76b15", dxcc_mode: "#ffc53d", grid: "#5b7fff", grid_band: "#5b7fff",
                 dxcc_unconfirmed: "#a970e0", call_band_mode: "#30a46c" };
  let body = null, snap = {}, z = 3, cx = null, cy = null, w = 0, h = 0, built = false, drag = null, centred = false;
  const tiles = new Map();
  const px = (lon, zz = z) => ((lon + 180) / 360) * TILE * 2 ** zz;
  const py = (lat, zz = z) => {
    const s = Math.sin((Math.max(-85, Math.min(85, lat)) * Math.PI) / 180);
    return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * TILE * 2 ** zz;
  };

  function build() {
    body.innerHTML = `<div class="rm-bar stat"></div><div class="rm-wrap"><div class="rm-tiles"></div><svg class="rm-svg">
        <g class="rm-world"><g class="rm-night"></g><g class="rm-grids"></g><g class="rm-pota"></g><g class="rm-dec"></g><g class="rm-me"></g></g></svg>
        <div class="rm-ctl"><button class="chip chip-mini" data-z="1" title="Zoom in">+</button><button class="chip chip-mini" data-z="-1" title="Zoom out">−</button></div>
        <a class="rm-attr" href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© OpenStreetMap</a></div>`;
    const wrap = body.querySelector(".rm-wrap");
    for (const b of body.querySelectorAll("[data-z]")) b.onclick = () => zoom(Number(b.dataset.z));
    // Drawn while the tab is hidden the map has no size; draw again the moment it has one.
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => draw()).observe(wrap);
    wrap.addEventListener("wheel", (e) => { e.preventDefault(); zoom(e.deltaY < 0 ? 1 : -1); }, { passive: false });
    wrap.addEventListener("pointerdown", (e) => { if (e.target.closest(".rm-d, .rm-ctl")) return; drag = { x: e.clientX, y: e.clientY, cx, cy }; wrap.setPointerCapture(e.pointerId); });
    wrap.addEventListener("pointermove", (e) => { if (!drag) return; cx = drag.cx - (e.clientX - drag.x); cy = drag.cy - (e.clientY - drag.y); draw(); });
    wrap.addEventListener("pointerup", () => { drag = null; });
    built = true;
  }

  function zoom(dz) {
    const nz = Math.max(ZMIN, Math.min(ZMAX, z + dz));
    if (nz === z) return;
    const f = 2 ** (nz - z);
    cx *= f; cy *= f; z = nz; draw();
  }

  function el(tag, attrs, parent) {
    const e = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    parent.appendChild(e);
    return e;
  }

  function drawTiles() {
    const host = body.querySelector(".rm-tiles"), n = 2 ** z;
    const x0 = Math.floor((cx - w / 2) / TILE), x1 = Math.floor((cx + w / 2) / TILE);
    const y0 = Math.max(0, Math.floor((cy - h / 2) / TILE)), y1 = Math.min(n - 1, Math.floor((cy + h / 2) / TILE));
    const want = new Set();
    for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) {
      const k = `${z}/${x}/${y}`; want.add(k);
      let img = tiles.get(k);
      if (!img) {
        img = document.createElement("img"); img.className = "rm-tile"; img.alt = ""; img.draggable = false;
        img.src = TILES.replace("{z}", z).replace("{x}", ((x % n) + n) % n).replace("{y}", y);
        img.onerror = () => { img.style.visibility = "hidden"; };
        tiles.set(k, img); host.appendChild(img);
      }
      img.style.left = `${x * TILE - cx + w / 2}px`; img.style.top = `${y * TILE - cy + h / 2}px`;
    }
    for (const [k, img] of tiles) if (!want.has(k)) { img.remove(); tiles.delete(k); }
  }

  function draw() {
    if (!body || !built) return;
    const wrap = body.querySelector(".rm-wrap");
    w = wrap.clientWidth; h = wrap.clientHeight;
    if (!w || !h) return;                         // hidden: nothing to place yet
    const M = snap.map;
    const me = M && rrMapMath.gridLL(M.me);
    if (cx == null || (!centred && me)) { cx = px(me ? me.lon + 1 : -100); cy = py(me ? me.lat + 0.5 : 35); centred = !!me; }
    drawTiles();
    body.querySelector(".rm-world").setAttribute("transform", `translate(${w / 2 - cx},${h / 2 - cy})`);
    for (const g of body.querySelectorAll(".rm-world > g")) g.innerHTML = "";
    const nightG = body.querySelector(".rm-night"), gridsG = body.querySelector(".rm-grids");
    const potaG = body.querySelector(".rm-pota"), decG = body.querySelector(".rm-dec"), meG = body.querySelector(".rm-me");
    const pts = rrMapMath.night(new Date()).map(([lo, la]) => `${px(lo).toFixed(1)},${py(la).toFixed(1)}`).join(" ");
    el("polygon", { points: pts, class: "rm-nightp" }, nightG);
    if (!M) return;
    for (const [g, st] of Object.entries(M.grids || {})) {
      const b = rrMapMath.gridLL(g); if (!b) continue;
      el("rect", { x: px(b.lon), y: py(b.lat + b.h), width: px(b.lon + b.w) - px(b.lon), height: py(b.lat) - py(b.lat + b.h),
                   class: st === "c" ? "rm-gc" : "rm-gw" }, gridsG).appendChild(Object.assign(document.createElementNS(SVGNS, "title"), { textContent: `${g}: ${st === "c" ? "confirmed" : "worked"}` }));
    }
    for (const s of snap.pota || []) {
      const x = px(s.lon), y = py(s.lat);
      const t = el("path", { d: `M${x},${y - 6} L${x + 5},${y + 4} L${x - 5},${y + 4} Z`, class: "rm-potap" }, potaG);
      t.appendChild(Object.assign(document.createElementNS(SVGNS, "title"), { textContent: `${s.call} POTA ${s.ref} ${s.name} — ${s.khz} kHz ${s.mode}` }));
    }
    for (const d of M.decodes || []) {
      const b = rrMapMath.gridLL(d.grid); if (!b) continue;
      const c = el("circle", { cx: px(b.lon + 1), cy: py(b.lat + 0.5), r: d.need ? 6 : 4, fill: NEED[d.need] || "#9aa0a6",
                               class: `rm-d${d.to_me ? " rm-tome" : ""}`, "data-key": d.key, "data-call": d.call }, decG);
      c.appendChild(Object.assign(document.createElementNS(SVGNS, "title"), { textContent: `${d.call} ${d.grid} ${d.entity || ""} ${d.snr} dB${d.need ? " — " + d.need.replace(/_/g, " ") : ""} (click to answer)` }));
      c.onclick = () => send({ t: "addon", addon: "map", do: "reply", key: d.key, call: d.call });
    }
    if (me) el("circle", { cx: px(me.lon + 1), cy: py(me.lat + 0.5), r: 7, class: "rm-mep" }, meG);
  }

  function bar() {
    const el2 = body && body.querySelector(".rm-bar"); if (!el2) return;
    const M = snap.map || {}, n = Object.values(M.grids || {});
    const sel = ["current", "all", "160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]
      .map((b) => `<option${(b === "current" ? !snap.chosen : snap.chosen === b) ? " selected" : ""}>${b}</option>`).join("");
    el2.innerHTML = `<span>grids on <select class="rm-band">${sel}</select> (${esc(M.showing || "")}): ${n.length} worked, of them ${n.filter((s) => s === "c").length} confirmed</span>
      <span>${(M.decodes || []).length} heard</span>
      <span>${snap.pota ? `${snap.pota.length} POTA on ${esc(M.showing || "")}` : ""}${snap.pota_error ? ` <span class="lb-bad">${esc(snap.pota_error)}</span>` : ""}</span>
      <span class="rm-key"><i class="rm-kw"></i>worked <i class="rm-kc"></i>confirmed <i class="rm-kn"></i>night</span>`;
    el2.querySelector(".rm-band").onchange = (e) => { snap.chosen = e.target.value === "current" ? null : e.target.value; send({ t: "addon", addon: "map", do: "show", band: e.target.value }); };
  }

  function render() {
    if (!body) return;
    if (snap.error) { body.innerHTML = `<div class="st-empty">${esc(snap.error)}</div>`; built = false; return; }
    if (!built) build();
    bar(); draw();
  }

  window.rrMapMath = rrMapMath;               // the tests check the gray line against reference points
  addons.register("map", {
    mount(b) { body = b; built = false; centred = false; cx = null; render(); },
    hello(s) { snap = Object.assign({ chosen: snap.chosen }, s || {}); render(); },
    event(m) { if (m.map || m.error) { snap = Object.assign({ chosen: snap.chosen }, m); render(); } },
    off() { snap = {}; if (body) body.innerHTML = ""; built = false; },
    tab() { return { dot: snap.map ? "ok" : "off", title: snap.map ? `${Object.keys(snap.map.grids || {}).length} grids` : "Map" }; },
    card() { const M = snap.map; return { state: M ? `${(M.decodes || []).length} heard` : "off", lines: [] }; },
  });
})();
