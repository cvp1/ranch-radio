"use strict";

// Mesh map: the nodes that report a position, over OpenStreetMap tiles
// (fetched by the browser; without internet the dots still show on a plain
// grid). Activity pulses on a node when a packet from it is heard, and the
// last traceroute is drawn as its path there (solid) and back (dashed).
// Drag to pan, wheel or +/- to zoom, Fit to frame the active nodes again.
// The APRS page (aprs.js) also uses: a selected station's track (setTrack),
// a clickable "me" (meSelectable), a text glyph and a kind class per node
// (glyph, kind), an ambiguity box (box_deg, degrees on a side), and noFit.
// No library: Web Mercator tiles in a div, an SVG on top in world pixels.

const meshMap = (() => {
  const TILE = 256, ZMIN = 2, ZMAX = 17;
  const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  const LIVE_S = 15 * 60, RECENT_S = 24 * 3600;
  const SVGNS = "http://www.w3.org/2000/svg";

  function create(el, { onSelect, meSelectable = false }) {
    el.innerHTML = `<div class="mm-tiles"></div><svg class="mm-svg"><g class="mm-world">` +
      `<g class="mm-boxes"></g><g class="mm-track"></g><g class="mm-trace"></g><g class="mm-nodes"></g><g class="mm-pulses"></g></g></svg>` +
      `<div class="mm-note"></div>` +
      `<div class="mm-ctl"><button class="chip chip-mini" data-z="1" title="Zoom in">+</button>` +
      `<button class="chip chip-mini" data-z="-1" title="Zoom out">−</button>` +
      `<button class="chip chip-mini" data-fit title="Frame the nodes heard in the last day">Fit</button></div>` +
      `<a class="mm-attr" href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© OpenStreetMap</a>`;
    const tilesEl = el.querySelector(".mm-tiles"), svg = el.querySelector(".mm-svg");
    const world = el.querySelector(".mm-world"), traceG = el.querySelector(".mm-trace");
    const nodesG = el.querySelector(".mm-nodes"), pulsesG = el.querySelector(".mm-pulses");
    const boxesG = el.querySelector(".mm-boxes"), trackG = el.querySelector(".mm-track");
    const tiles = new Map();
    let nodes = [], myId = null, selected = null, trace = null, track = [];
    let z = 12, cx = 0, cy = 0, moved = false, w = 0, h = 0;

    // ---------- projection (world pixels at zoom z)

    const px = (lon, zz = z) => ((lon + 180) / 360) * TILE * 2 ** zz;
    const py = (lat, zz = z) => {
      const s = Math.sin((Math.max(-85, Math.min(85, lat)) * Math.PI) / 180);
      return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * TILE * 2 ** zz;
    };
    const placed = () => nodes.filter((n) => n.lat != null && n.lon != null && (n.lat || n.lon));
    const byId = (id) => nodes.find((n) => n.id === id);
    const age = (n) => (n.last_heard ? Date.now() / 1000 - n.last_heard : Infinity);

    function fit() {
      // noFit: shown, but not framed (APRS-IS stations far from here).
      let pts = placed().filter((n) => !n.noFit && (n.me || age(n) < RECENT_S));
      if (!pts.length) pts = placed().filter((n) => !n.noFit);
      if (!pts.length) pts = placed();
      if (!pts.length || !w || !h) return false;
      const lons = pts.map((n) => n.lon), lats = pts.map((n) => n.lat);
      const [lo0, lo1, la0, la1] = [Math.min(...lons), Math.max(...lons), Math.min(...lats), Math.max(...lats)];
      z = 13;
      while (z > ZMIN && (px(lo1) - px(lo0) > w - 60 || py(la0) - py(la1) > h - 60)) z--;
      cx = (px(lo0) + px(lo1)) / 2;
      cy = (py(la0) + py(la1)) / 2;
      return true;
    }

    // ---------- drawing

    function render() {
      w = el.clientWidth;
      h = el.clientHeight;
      if (!w || !h) return;                       // in a hidden tab: draw when shown
      if (!moved) fit();
      const x0 = cx - w / 2, y0 = cy - h / 2, n = 2 ** z;
      const want = new Set();
      for (let ty = Math.floor(y0 / TILE); ty <= Math.floor((y0 + h) / TILE); ty++) {
        if (ty < 0 || ty >= n) continue;
        for (let tx = Math.floor(x0 / TILE); tx <= Math.floor((x0 + w) / TILE); tx++) {
          const key = `${z}/${tx}/${ty}`;
          want.add(key);
          let img = tiles.get(key);
          if (!img) {
            img = new Image();
            img.className = "mm-tile";
            img.alt = "";
            img.onerror = () => { img.style.visibility = "hidden"; };
            img.src = TILES.replace("{z}", z).replace("{x}", ((tx % n) + n) % n).replace("{y}", ty);
            tiles.set(key, img);
            tilesEl.append(img);
          }
          img.style.transform = `translate(${tx * TILE - x0}px, ${ty * TILE - y0}px)`;
        }
      }
      for (const [key, img] of tiles) if (!want.has(key)) { img.remove(); tiles.delete(key); }
      world.setAttribute("transform", `translate(${-x0} ${-y0})`);
      drawNodes();
      drawTrace();
      drawTrack();
      const all = nodes.filter((x) => !x.me).length, on = placed().filter((x) => !x.me).length;
      el.querySelector(".mm-note").textContent = !placed().length ? "No node has reported a position yet"
        : `${on} of ${all} nodes on the map`;
    }

    function drawNodes() {
      nodesG.innerHTML = "";
      boxesG.innerHTML = "";
      const pts = placed().sort((a, b) => age(b) - age(a));       // freshest on top
      const labels = pts.length <= 40 || z >= 12;
      for (const nd of pts) {
        const a = age(nd);
        const cls = nd.me ? "me" : a < LIVE_S ? "live" : a < RECENT_S ? "recent" : "old";
        const g = document.createElementNS(SVGNS, "g");
        g.setAttribute("class", `mm-node ${cls}${nd.id === selected ? " sel" : ""}${nd.fav ? " fav" : ""}` +
          (nd.kind ? ` k-${nd.kind}` : ""));
        g.setAttribute("transform", `translate(${px(nd.lon)} ${py(nd.lat)})`);
        if (nd.box_deg > 0) {                     // position ambiguity: the area it could be in
          const d = nd.box_deg / 2, r = document.createElementNS(SVGNS, "rect");
          const x0 = px(nd.lon - d), x1 = px(nd.lon + d), y0 = py(nd.lat + d), y1 = py(nd.lat - d);
          r.setAttribute("class", "mm-box");
          r.setAttribute("x", x0); r.setAttribute("y", y0);
          r.setAttribute("width", x1 - x0); r.setAttribute("height", y1 - y0);
          boxesG.append(r);
        }
        g.innerHTML = `<circle r="${nd.me || nd.id === selected ? 6 : 4.5}"></circle>` +
          (nd.glyph ? `<text class="mm-glyph" x="0" y="3.5" text-anchor="middle">${esc(nd.glyph)}</text>` : "") +
          (labels || nd.me || nd.id === selected ? `<text x="8" y="4">${esc(nd.shortName || nd.id)}</text>` : "") +
          `<title>${esc(`${nd.longName || ""} ${nd.id || ""}`)}</title>`;
        g.onclick = (e) => { e.stopPropagation(); if (!nd.me || meSelectable) onSelect(nd.id); };
        nodesG.append(g);
      }
    }

    function drawTrace() {
      traceG.innerHTML = "";
      if (!trace || trace.state !== "done") return;
      const path = (ids) => ids.map(byId).filter((n) => n && n.lat != null && (n.lat || n.lon))
        .map((n) => `${px(n.lon)},${py(n.lat)}`).join(" ");
      const out = path([myId, ...(trace.route || []), trace.to]);
      const back = trace.route_back ? path([trace.to, ...trace.route_back, myId]) : "";
      if (back) traceG.innerHTML += `<polyline class="mm-back" points="${back}"></polyline>`;
      if (out) traceG.innerHTML += `<polyline class="mm-out" points="${out}"></polyline>`;
    }

    function drawTrack() {
      trackG.innerHTML = "";
      const pts = (track || []).filter((p) => p.lat != null && p.lon != null);
      if (pts.length < 2) return;
      trackG.innerHTML = `<polyline class="mm-trk" points="${pts.map((p) => `${px(p.lon)},${py(p.lat)}`).join(" ")}">` +
        `</polyline>` + pts.map((p) => `<circle class="mm-trk-pt" r="2" cx="${px(p.lon)}" cy="${py(p.lat)}"></circle>`).join("");
    }

    // ---------- interaction

    function zoomBy(dz, mx = w / 2, my = h / 2) {
      const nz = Math.max(ZMIN, Math.min(ZMAX, z + dz));
      if (nz === z) return;
      const f = 2 ** (nz - z);
      cx = (cx + mx - w / 2) * f - (mx - w / 2);
      cy = (cy + my - h / 2) * f - (my - h / 2);
      z = nz;
      moved = true;
      pulsesG.innerHTML = "";
      render();
    }

    let drag = null;
    el.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".mm-ctl, .mm-attr, .mm-node")) return;
      drag = { x: e.clientX, y: e.clientY, cx, cy };
      el.setPointerCapture(e.pointerId);
      el.classList.add("dragging");
    });
    el.addEventListener("pointermove", (e) => {
      if (!drag) return;
      cx = drag.cx - (e.clientX - drag.x);
      cy = drag.cy - (e.clientY - drag.y);
      if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 3) moved = true;
      render();
    });
    const endDrag = () => { drag = null; el.classList.remove("dragging"); };
    el.addEventListener("pointerup", endDrag);
    el.addEventListener("pointercancel", endDrag);
    el.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      zoomBy(e.deltaY < 0 ? 1 : -1, e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    el.addEventListener("dblclick", (e) => {
      const r = el.getBoundingClientRect();
      zoomBy(1, e.clientX - r.left, e.clientY - r.top);
    });
    el.querySelector(".mm-ctl").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.fit !== undefined) { moved = false; pulsesG.innerHTML = ""; render(); }
      else zoomBy(Number(b.dataset.z));
    });
    new ResizeObserver(render).observe(el);

    // ---------- from mesh.js

    return {
      setNodes(list, me) { nodes = list || []; myId = me; render(); },
      select(id) { selected = id; drawNodes(); },
      setTrace(rec) { trace = rec; drawTrace(); },
      setTrack(points) { track = points || []; drawTrack(); },
      pulse(id) {
        const nd = byId(id);
        if (!nd || nd.lat == null || (!nd.lat && !nd.lon) || !w) return;
        const c = document.createElementNS(SVGNS, "circle");
        c.setAttribute("class", "mm-pulse");
        c.setAttribute("cx", px(nd.lon));
        c.setAttribute("cy", py(nd.lat));
        c.setAttribute("r", 5);
        pulsesG.append(c);
        setTimeout(() => c.remove(), 1600);
      },
      render,
    };
  }

  return { create };
})();
