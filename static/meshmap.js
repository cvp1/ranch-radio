"use strict";

// Mesh map: the nodes that report a position, over the background picked
// from its menu (basemaps.js: terrain, topo, imagery, streets, the Earth; tiles
// fetched by the browser; without internet the dots still show on a plain
// grid). Activity pulses on a node when a packet from it is heard (red, at
// this node, when a message of ours goes out: pulse(id, "tx")), and the
// last traceroute is drawn as its path there (solid) and back (dashed).
// Drag to pan, wheel or +/- to zoom, Fit to frame the active nodes again.
// The APRS page (aprs.js) also uses: a selected station's track (setTrack),
// a clickable "me" (meSelectable), a text glyph and a kind class per node
// (glyph, kind), an ambiguity box (box_deg, degrees on a side), and noFit.
// The MeshCore tab (addons/meshcore.js) also draws links between nodes
// (setLinks: a repeater's neighbours, each with its SNR as the label; a
// link's cls styles it, and setLinks' legend says what the styles mean).
// Nodes at the very same position (a channel shares positions only to a grid
// cell, so a site's nodes all land on its centre) are fanned out in a small
// ring around it, so each one and the links between them can be seen.
// A page may say more of a node (shape, ring, glyph, pin, tip: drawNodes), ask
// for distance rings around this node (rings), keep a list of its own beside
// the map (side) and offer a button for more room (onExpand); mesh.js does all four.
// No library: Web Mercator tiles in a div, an SVG on top in world pixels.

const meshMap = (() => {
  const TILE = 256, ZMIN = 2, ZMAX = 17;
  const LIVE_S = 15 * 60, RECENT_S = 24 * 3600;
  const SVGNS = "http://www.w3.org/2000/svg";
  const FAN_PX = 14;                 // radius of the ring nodes sharing a position are drawn on

  // rings: distance rings around this node (km). onExpand: a button that asks
  // the page for more room (the page resizes the map; it redraws itself); with
  // it, a click on the small map (not a drag, not a node) asks too, and the
  // page says which it is with setBig (2026-11-19: "click it to drive around").
  function create(el, { onSelect, meSelectable = false, rings = false, onExpand = null }) {
    el.innerHTML = `<div class="mm-tiles"></div><svg class="mm-svg"><g class="mm-world">` +
      `<g class="mm-rings"></g><g class="mm-boxes"></g><g class="mm-links"></g><g class="mm-track"></g><g class="mm-trace"></g><g class="mm-nodes"></g><g class="mm-pulses"></g></g></svg>` +
      `<div class="mm-note"></div><div class="mm-side" hidden></div>` +
      `<div class="mm-ctl"><button class="chip chip-mini" data-z="1" title="Zoom in">+</button>` +
      `<button class="chip chip-mini" data-z="-1" title="Zoom out">−</button>` +
      `<button class="chip chip-mini" data-fit title="Frame the nodes heard in the last day">Fit</button>` +
      (onExpand ? `<button class="chip chip-mini" data-big title="Give the map the room (or click the map)">⤢</button>` : "") + `</div>` +
      `<a class="mm-attr" target="_blank" rel="noopener"></a>` +
      (onExpand ? `<div class="mm-open-hint" aria-hidden="true">Click to open the map</div>` : "");
    let big = false;
    const tilesEl = el.querySelector(".mm-tiles"), svg = el.querySelector(".mm-svg");
    // The background (basemaps.js): one choice for all the local maps, with its menu beside Fit.
    const base = basemaps.layer(tilesEl, { key: "local", def: "terrain", cls: "mm-tile", attr: el.querySelector(".mm-attr"),
                                           onChange: () => render() });
    el.querySelector(".mm-ctl").append(base.control());
    const world = el.querySelector(".mm-world"), traceG = el.querySelector(".mm-trace");
    const nodesG = el.querySelector(".mm-nodes"), pulsesG = el.querySelector(".mm-pulses");
    const boxesG = el.querySelector(".mm-boxes"), trackG = el.querySelector(".mm-track");
    const linksG = el.querySelector(".mm-links"), ringsG = el.querySelector(".mm-rings");
    const side = el.querySelector(".mm-side");      // the page's own list beside the map (mesh.js: who hears whom)
    let nodes = [], myId = null, selected = null, trace = null, track = [], links = [], legend = "";
    let fan = new Map();               // node id -> [dx, dy] screen px, for nodes sharing a position
    let z = 12, cx = 0, cy = 0, moved = false, w = 0, h = 0;

    // ---------- projection (world pixels at zoom z)

    const px = (lon, zz = z) => ((lon + 180) / 360) * TILE * 2 ** zz;
    const py = (lat, zz = z) => {
      const s = Math.sin((Math.max(-85, Math.min(85, lat)) * Math.PI) / 180);
      return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * TILE * 2 ** zz;
    };
    const placed = () => nodes.filter((n) => n.lat != null && n.lon != null && (n.lat || n.lon));
    const byId = (id) => nodes.find((n) => n.id === id);
    const key = (n) => `${n.lat},${n.lon}`;
    function refan() {
      const groups = new Map();
      for (const n of placed()) groups.set(key(n), [...(groups.get(key(n)) || []), n]);
      fan = new Map();
      for (const g of groups.values()) {
        if (g.length < 2) continue;
        g.sort((a, b) => (a.id < b.id ? -1 : 1)).forEach((n, i) => {
          const t = (2 * Math.PI * i) / g.length - Math.PI / 2;
          fan.set(n.id, [FAN_PX * Math.cos(t), FAN_PX * Math.sin(t)]);
        });
      }
    }
    // Where a node is drawn: its position, moved onto its ring when it shares one.
    const spot = (n) => { const d = fan.get(n.id) || [0, 0]; return [px(n.lon) + d[0], py(n.lat) + d[1]]; };
    const age = (n) => (n.last_heard ? Date.now() / 1000 - n.last_heard : Infinity);
    // How much of the right edge the page's list covers (nothing when it is shut, or on a phone, where it sits below).
    const sideWidth = () => (!side.hidden && !side.classList.contains("shut") && w > 700 ? side.offsetWidth : 0);

    function fit() {
      // noFit: shown, but not framed (APRS-IS stations far from here).
      let pts = placed().filter((n) => !n.noFit && (n.me || age(n) < RECENT_S));
      if (!pts.length) pts = placed().filter((n) => !n.noFit);
      if (!pts.length) pts = placed();
      if (!pts.length || !w || !h) return false;
      const lons = pts.map((n) => n.lon), lats = pts.map((n) => n.lat);
      const [lo0, lo1, la0, la1] = [Math.min(...lons), Math.max(...lons), Math.min(...lats), Math.max(...lats)];
      const pad = sideWidth() ? sideWidth() + 8 : 0;      // clear of the list on the right
      z = 13;
      while (z > ZMIN && (px(lo1) - px(lo0) > w - pad - 60 || py(la0) - py(la1) > h - 60)) z--;
      cx = (px(lo0) + px(lo1)) / 2 + pad / 2;
      cy = (py(la0) + py(la1)) / 2;
      return true;
    }

    // ---------- drawing

    function render() {
      w = el.clientWidth;
      h = el.clientHeight;
      if (!w || !h) return;                       // in a hidden tab: draw when shown
      if (!moved) fit();
      const x0 = cx - w / 2, y0 = cy - h / 2;
      base.render(z, cx, cy, w, h);
      world.setAttribute("transform", `translate(${-x0} ${-y0})`);
      drawRings();
      drawNodes();
      drawLinks();
      drawTrace();
      drawTrack();
      const all = nodes.filter((x) => !x.me).length, on = placed().filter((x) => !x.me).length;
      el.querySelector(".mm-note").textContent = !placed().length ? "No node has reported a position yet"
        : `${on} of ${all} nodes on the map${legend ? ` · ${legend}` : ""}`;
    }

    // Distance rings around this node: the three largest round distances that
    // fit the view, so the map has a scale and a link's length can be read off.
    function drawRings() {
      ringsG.innerHTML = "";
      const me = rings && placed().find((n) => n.me);
      if (!me) return;
      const perKm = (TILE * 2 ** z) / (40075.017 * Math.cos((me.lat * Math.PI) / 180));
      const fits = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000].filter((km) => km * perKm >= 34 && km * perKm <= Math.max(w, h) * 0.6);
      for (const km of fits.slice(-3)) {
        const r = km * perKm, x = px(me.lon), y = py(me.lat);
        ringsG.innerHTML += `<circle class="mm-range" cx="${x}" cy="${y}" r="${r}"></circle>` +
          `<text class="mm-range-t" x="${x}" y="${y - r - 3}" text-anchor="middle">${km} km</text>`;
      }
    }

    // A node may say more about itself (all optional): shape "diamond" (a
    // router), ring "in" / "out" / "both" (it hears us, we hear it), glyph (a
    // character in the mark: its hops), pin (label it before the others), tip
    // (the hover text, lines and all), kind (a class).
    function drawNodes() {
      nodesG.innerHTML = "";
      boxesG.innerHTML = "";
      const pts = placed().sort((a, b) => age(b) - age(a));       // freshest on top
      // Labels: this node, the selected one and the pinned ones first, then the
      // rest freshest first, each only where it would not cover one already
      // placed. A crowd of names is no map; unnamed nodes still say who they are on hover.
      const few = pts.length <= 40 || z >= 11;
      const rank = (n) => (n.me ? 0 : n.id === selected ? 1 : n.pin ? 2 : 3);
      const boxes = [], named = new Set();
      for (const nd of [...pts].sort((a, b) => rank(a) - rank(b) || age(a) - age(b))) {
        if (rank(nd) === 3 && !few) continue;
        const [sx, sy] = spot(nd), wd = String(nd.shortName || nd.id).length * 6.8 + 6;
        const box = [sx + 7, sy - 8, sx + 7 + wd, sy + 6];
        if (rank(nd) > 1 && boxes.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1])) continue;
        boxes.push(box);
        named.add(nd.id);
      }
      for (const nd of pts) {
        const a = age(nd);
        const cls = nd.me ? "me" : a < LIVE_S ? "live" : a < RECENT_S ? "recent" : "old";
        const g = document.createElementNS(SVGNS, "g");
        g.setAttribute("class", `mm-node ${cls}${nd.id === selected ? " sel" : ""}${nd.fav ? " fav" : ""}` +
          (nd.kind ? ` k-${nd.kind}` : "") + (nd.shape ? ` s-${nd.shape}` : ""));
        const [sx, sy] = spot(nd);
        g.setAttribute("transform", `translate(${sx} ${sy})`);
        if (nd.box_deg > 0) {                     // position ambiguity: the area it could be in
          const d = nd.box_deg / 2, r = document.createElementNS(SVGNS, "rect");
          const x0 = px(nd.lon - d), x1 = px(nd.lon + d), y0 = py(nd.lat + d), y1 = py(nd.lat - d);
          r.setAttribute("class", "mm-box");
          r.setAttribute("x", x0); r.setAttribute("y", y0);
          r.setAttribute("width", x1 - x0); r.setAttribute("height", y1 - y0);
          boxesG.append(r);
        }
        const r = nd.me || nd.id === selected ? 6 : nd.shape === "diamond" ? 5.5 : 4.5;
        g.innerHTML = (nd.ring ? `<circle class="mm-ring ${esc(nd.ring)}" r="${r + 3.5}"></circle>` : "") +
          (nd.shape === "diamond" ? `<path class="mm-mark" d="M0,${-r - 1} L${r + 1},0 L0,${r + 1} L${-r - 1},0 Z"></path>`
            : `<circle class="mm-mark" r="${r}"></circle>`) +
          (nd.glyph ? `<text class="mm-glyph" x="0" y="2.6" text-anchor="middle">${esc(nd.glyph)}</text>` : "") +
          (named.has(nd.id) ? `<text x="${r + 4.5}" y="4">${esc(nd.shortName || nd.id)}</text>` : "") +
          `<title>${esc(nd.tip || `${nd.longName || ""} ${nd.id || ""}`)}</title>`;
        g.onclick = (e) => { e.stopPropagation(); if (!nd.me || meSelectable) onSelect(nd.id); };
        nodesG.append(g);
      }
    }

    function drawTrace() {
      traceG.innerHTML = "";
      if (!trace || trace.state !== "done") return;
      const path = (ids) => ids.map(byId).filter((n) => n && n.lat != null && (n.lat || n.lon))
        .map((n) => spot(n).join(",")).join(" ");
      const out = path([myId, ...(trace.route || []), trace.to]);
      const back = trace.route_back ? path([trace.to, ...trace.route_back, myId]) : "";
      if (back) traceG.innerHTML += `<polyline class="mm-back" points="${back}"></polyline>`;
      if (out) traceG.innerHTML += `<polyline class="mm-out" points="${out}"></polyline>`;
    }

    function drawLinks() {
      linksG.innerHTML = "";
      const at = (id) => { const n = byId(id); return n && n.lat != null && (n.lat || n.lon) ? n : null; };
      // A shared position: a faint ring, so the fan reads as one place.
      const rings = new Map();
      for (const n of placed()) if (fan.has(n.id)) rings.set(key(n), [...(rings.get(key(n)) || []), n]);
      for (const g of rings.values()) {
        linksG.innerHTML += `<circle class="mm-fan" cx="${px(g[0].lon)}" cy="${py(g[0].lat)}" r="${FAN_PX}">` +
          `<title>${esc(`${g.map((n) => n.shortName || n.id).join(", ")} report the same position ` +
          "(a channel shares positions only to a grid cell); drawn apart so each can be seen")}</title></circle>`;
      }
      for (const l of links || []) {
        const a = at(l.from), b = at(l.to);
        if (!a || !b) continue;
        const [x1, y1] = spot(a), [x2, y2] = spot(b);
        // Its SNR is written on the line only where the line has room for it
        // (nodes a ring apart had theirs piled on one another); the hover text has it always.
        const room = Math.hypot(x2 - x1, y2 - y1) >= 56;
        linksG.innerHTML += `<line class="mm-link${l.cls ? ` ${l.cls}` : ""}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}">` +
          (l.title ? `<title>${esc(l.title)}</title>` : "") + `</line>` +
          (l.label && room ? `<text class="mm-link-label" x="${(x1 + x2) / 2}" y="${(y1 + y2) / 2 - 3}" text-anchor="middle">${esc(l.label)}</text>` : "");
      }
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
      if (e.target.closest(".mm-ctl, .mm-attr, .mm-node, .mm-side")) return;
      drag = { x: e.clientX, y: e.clientY, cx, cy, moved: false };
      el.setPointerCapture(e.pointerId);
      el.classList.add("dragging");
    });
    el.addEventListener("pointermove", (e) => {
      if (!drag) return;
      cx = drag.cx - (e.clientX - drag.x);
      cy = drag.cy - (e.clientY - drag.y);
      if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 3) { moved = true; drag.moved = true; }
      render();
    });
    const endDrag = () => { drag = null; el.classList.remove("dragging"); };
    // A click (no drag) on the small map opens it big; a node's own click selects it.
    el.addEventListener("pointerup", (e) => {
      const click = drag && !drag.moved;
      endDrag();
      if (click && onExpand && !big && !e.target.closest(".mm-ctl, .mm-attr, .mm-node, .mm-side")) onExpand();
    });
    el.addEventListener("pointercancel", endDrag);
    basemaps.wheel(el, { zoom: (dz, x, y) => zoomBy(dz, x, y),
      pan: (dx, dy) => { cx += dx; cy += dy; moved = true; render(); },
      skip: (e) => !!e.target.closest(".mm-side") });   // the list scrolls
    el.addEventListener("dblclick", (e) => {
      if (e.target.closest(".mm-side, .mm-ctl")) return;
      const r = el.getBoundingClientRect();
      zoomBy(1, e.clientX - r.left, e.clientY - r.top);
    });
    el.querySelector(".mm-ctl").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.fit !== undefined) { moved = false; pulsesG.innerHTML = ""; render(); }
      else if (b.dataset.big !== undefined) onExpand();
      else zoomBy(Number(b.dataset.z));
    });
    new ResizeObserver(render).observe(el);

    // ---------- from mesh.js

    return {
      // The page made the map big (or small): the ⤢ becomes a way back.
      setBig(b) {
        big = !!b;
        el.classList.toggle("mm-big", big);
        const btn = el.querySelector("[data-big]");
        if (btn) {
          btn.textContent = big ? "✕ Close map" : "⤢";
          btn.title = big ? "Back to the conversation (Esc)" : "Give the map the room (or click the map)";
        }
      },
      setNodes(list, me) { nodes = list || []; myId = me; refan(); render(); },
      select(id) { selected = id; drawNodes(); },
      setTrace(rec) { trace = rec; drawTrace(); },
      setTrack(points) { track = points || []; drawTrack(); },
      setLinks(list, note = "") { links = list || []; legend = note; render(); },
      pulse(id, cls = "") {             // cls "tx": a packet of ours going out (red), not one heard
        const nd = byId(id);
        if (!nd || nd.lat == null || (!nd.lat && !nd.lon) || !w) return;
        const c = document.createElementNS(SVGNS, "circle");
        c.setAttribute("class", `mm-pulse${cls ? ` ${cls}` : ""}`);
        const [sx, sy] = spot(nd);
        c.setAttribute("cx", sx);
        c.setAttribute("cy", sy);
        c.setAttribute("r", 5);
        pulsesG.append(c);
        setTimeout(() => c.remove(), 1600);
      },
      // Bring a node into view if it is off the map (a click in the page's list).
      show(id) {
        const nd = byId(id);
        if (!nd || nd.lat == null || (!nd.lat && !nd.lon) || !w) return;
        const [sx, sy] = spot(nd), x = sx - cx + w / 2, y = sy - cy + h / 2;
        const right = w - sideWidth() - 30;
        if (x >= 30 && x <= right && y >= 30 && y <= h - 30) return;
        cx = sx + sideWidth() / 2;
        cy = sy;
        moved = true;
        render();
      },
      side,
      render,
    };
  }

  return { create };
})();
