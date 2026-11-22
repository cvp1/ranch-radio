"use strict";

// Map backgrounds, shared by every map on the pages: meshmap.js (Mesh,
// MeshCore, APRS) and addons/map.js (the Map tab). Each map has a small menu
// to pick one; the choice is kept in the browser, one for the world map and
// one for the local maps.
//
//   Earth      NASA's Blue Marble: relief and sea floor, the whole world, to
//              zoom 8 (and Night lights, which the Map tab lays over the
//              night side)
//   Terrain    OpenTopoMap: relief shading, contours, peaks, roads, the world
//   Topo       USGS The National Map: contours and hydrography, the United
//              States (the rest of the world only in outline)
//   Imagery    USGS aerial imagery, the United States
//   Streets    OpenStreetMap's own map
//   Dark       the same, inverted: the look these maps had before 2026-11-15
//
// The tiles are fetched by the browser, straight from each provider; this
// server asks for none and sends nothing of ours. Checked 2026-11-15, each on
// its own pages: NASA GIBS is open and asks for an acknowledgement; The
// National Map is public domain, "no restrictions", with a credit line;
// OpenTopoMap is CC-BY-SA and may be embedded as long as its server isn't
// overloaded; OpenStreetMap's tile policy wants its attribution in view.
// Every map shows the attribution of the background it is drawing.
// (USGS's Imagery Topo layer is not here: USGS retires it on 2026-11-16.)
//
// A background has a deepest zoom. Past it the map keeps zooming and the
// deepest tiles are drawn larger, so a node map can go to street level over
// the Blue Marble without going blank. Without internet no tile loads and the
// maps draw on their plain background, as before.

const basemaps = (() => {
  const TILE = 256;
  const GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best";
  const USGS = "https://basemap.nationalmap.gov/arcgis/rest/services";
  const NASA = ["NASA GIBS", "https://www.earthdata.nasa.gov/engage/open-data-services-software/earthdata-developer-portal/gibs-api"];
  const TNM = ["USGS The National Map", "https://www.usgs.gov/programs/national-geospatial-program/national-map"];
  const OSM = ["© OpenStreetMap", "https://www.openstreetmap.org/copyright"];
  // url: {z} {x} {y} as each provider orders them. max: its deepest zoom.
  const MAPS = {
    earth: { label: "Earth", title: "NASA Blue Marble: relief and sea floor", max: 8, attr: NASA,
             url: `${GIBS}/BlueMarble_ShadedRelief_Bathymetry/default/GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpeg` },
    lights: { label: "Night lights", title: "NASA: the Earth's city lights", max: 8, attr: NASA,
              url: `${GIBS}/VIIRS_CityLights_2012/default/GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpeg` },
    terrain: { label: "Terrain", title: "OpenTopoMap: relief, contours, peaks and roads", max: 15,
               attr: ["© OpenStreetMap contributors, SRTM | © OpenTopoMap (CC-BY-SA)", "https://opentopomap.org/about"],
               url: "https://tile.opentopomap.org/{z}/{x}/{y}.png" },
    topo: { label: "Topo (US)", title: "USGS topographic map: contours and water, United States", max: 16, attr: TNM,
            url: `${USGS}/USGSTopo/MapServer/tile/{z}/{y}/{x}` },
    imagery: { label: "Imagery (US)", title: "USGS aerial imagery, United States", max: 16, attr: TNM,
               url: `${USGS}/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}` },
    streets: { label: "Streets", title: "OpenStreetMap", max: 19, attr: OSM, url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png" },
    dark: { label: "Dark", title: "OpenStreetMap, inverted", max: 19, attr: OSM, url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png" },
  };
  const CHOICES = ["earth", "terrain", "topo", "imagery", "streets", "dark"];
  // The choice, in the browser's own storage (the packet page has no common.js).
  const keep = {
    get(k, d) { try { return localStorage.getItem(`rr-${k}`) || d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(`rr-${k}`, v); } catch { /* private mode: forgotten on reload */ } },
  };

  const url = (m, z, x, y) => m.url.replace("{z}", z).replace("{x}", x).replace("{y}", y);

  // One background in a container. key: which choice it follows ("world",
  // "local"); def: the one drawn until the operator picks; fixed: always this
  // one (an overlay, no menu); cls: a class for its tiles; attr: an <a> kept
  // showing the attribution; onChange: redraw after a pick.
  function layer(host, { key = "local", def = "streets", fixed = null, cls = "", attr = null, onChange = () => {} } = {}) {
    const saved = fixed || keep.get(`basemap.${key}`, def);
    let id = MAPS[saved] ? saved : def;
    const tiles = new Map();
    host.classList.add("bm-host");

    function credit() {
      host.dataset.basemap = id;
      if (!attr) return;
      attr.textContent = MAPS[id].attr[0];
      attr.href = MAPS[id].attr[1];
    }

    // The tiles covering a view w by h whose centre is world pixel (cx, cy) at zoom z.
    function render(z, cx, cy, w, h) {
      const m = MAPS[id], tz = Math.min(z, m.max), size = TILE * 2 ** (z - tz), n = 2 ** tz;
      const x0 = cx - w / 2, y0 = cy - h / 2, want = new Set();
      for (let ty = Math.floor(y0 / size); ty <= Math.floor((y0 + h) / size); ty++) {
        if (ty < 0 || ty >= n) continue;
        for (let tx = Math.floor(x0 / size); tx <= Math.floor((x0 + w) / size); tx++) {
          const k = `${id}/${tz}/${tx}/${ty}`;
          want.add(k);
          let img = tiles.get(k);
          if (!img) {
            img = new Image();
            img.className = `bm-tile${cls ? ` ${cls}` : ""}`;
            img.alt = "";
            img.draggable = false;
            img.style.cssText = "position:absolute;left:0;top:0;max-width:none";   // placed by its transform alone
            img.onerror = () => { img.style.visibility = "hidden"; };
            // The pages send Referrer-Policy: same-origin (webguard.py), which strips the
            // Referer from cross-site requests; OpenStreetMap refuses tiles without one
            // (its tile usage policy). A tile sends the station's origin only, never a path.
            img.referrerPolicy = "strict-origin-when-cross-origin";
            img.src = url(m, tz, ((tx % n) + n) % n, ty);
            tiles.set(k, img);
            host.append(img);
          }
          // Whole pixels, one over: a fraction leaves hairlines between tiles.
          img.style.width = img.style.height = `${Math.ceil(size) + 1}px`;
          img.style.transform = `translate(${Math.round(tx * size - x0)}px, ${Math.round(ty * size - y0)}px)`;
        }
      }
      for (const [k, img] of tiles) if (!want.has(k)) { img.remove(); tiles.delete(k); }
    }

    function pick(next) {
      if (!MAPS[next] || next === id) return;
      id = next;
      if (!fixed) keep.set(`basemap.${key}`, id);
      for (const img of tiles.values()) img.remove();
      tiles.clear();
      credit();
      onChange(id);
    }

    // The menu, for the map's own row of controls.
    function control() {
      const sel = document.createElement("select");
      sel.className = "bm-pick";
      sel.title = "The map's background";
      sel.setAttribute("aria-label", "Map background");
      sel.innerHTML = CHOICES.map((c) => `<option value="${c}" title="${MAPS[c].title}"${c === id ? " selected" : ""}>${MAPS[c].label}</option>`).join("");
      sel.onchange = () => { sel.blur(); pick(sel.value); };
      for (const ev of ["pointerdown", "wheel", "dblclick"]) sel.addEventListener(ev, (e) => e.stopPropagation());   // not a drag or a zoom of the map
      return sel;
    }

    credit();
    return { render, pick, control, id: () => id, max: () => MAPS[id].max };
  }

  // The wheel on a map, the same on every one (2026-11-19). Before, every
  // wheel event was a whole zoom level, and one with no vertical part (a
  // trackpad's sideways swipe) counted as out: the Map tab "would not hold a
  // zoom and kept zooming out" (five sideways swipes took it from 5 to 1).
  // Now a vertical scroll adds up to a level per mouse notch or deliberate
  // swipe (so a trackpad's momentum tail is not ten levels), a pinch
  // (ctrlKey) zooms in finer steps, and a mostly sideways swipe pans.
  //   zoom(dz, x, y): a level in (1) or out (-1) about x, y in the map's pixels
  //   pan(dx, dy):    move the view by that many pixels
  //   skip(e):        true for a part of the map that scrolls itself (a list)
  const STEP = 100, PINCH_STEP = 24, GESTURE_GAP_MS = 220;
  function wheel(el, { zoom, pan, skip = () => false }) {
    let acc = 0, last = 0;
    el.addEventListener("wheel", (e) => {
      if (skip(e)) return;
      e.preventDefault();
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? el.clientHeight || 600 : 1;
      const dx = e.deltaX * unit, dy = e.deltaY * unit;
      if (!dx && !dy) return;
      const now = performance.now();
      if (now - last > GESTURE_GAP_MS) acc = 0;   // a new gesture starts from nothing
      last = now;
      if (!e.ctrlKey && Math.abs(dx) > Math.abs(dy)) { pan(dx, dy); return; }
      acc += dy;
      const step = e.ctrlKey ? PINCH_STEP : STEP;
      const r = el.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
      while (Math.abs(acc) >= step) {
        zoom(acc < 0 ? 1 : -1, x, y);
        acc -= Math.sign(acc) * step;
      }
    }, { passive: false });
  }

  return { layer, MAPS, CHOICES, wheel };
})();
