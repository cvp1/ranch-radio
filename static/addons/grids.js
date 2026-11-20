"use strict";

// The Grids tab: the grid squares you still need, by operating mode and band
// (GridTracker2's grid tracking, from the station's own logbook).
//
//   chips     the mode (All, FT8, FT4, SSB, CW... whatever the log holds) and
//             the band (All or one); "Count" says whether a square is done when
//             worked or only when confirmed (LoTW or QRZ)
//   tally     per mode, on the chosen band: Lower-48 squares confirmed, worked
//             and missing, and squares worked/confirmed anywhere; click a row
//             to pick that mode
//   lattice   the 488 Lower-48 squares where they lie: green confirmed, blue
//             worked (not yet confirmed), dark missing, an amber ring on a
//             missing square someone is calling from right now; your own
//             square outlined. Hover a square for what you have there.
//   missing   the missing squares, on the air now first, then nearest first
//   alert     off (the default), chime or voice: a missing square coming on
//             the air (as the chips filter it) sounds once per square, band
//             and mode per ALERT_AGAIN_MS, with a toast and, if the browser
//             allows, a notification. What is on the air when the page opens
//             counts as seen. Voice spells the square ("D M 5 3, 20 metres").
//
// Server: addons/grids.py ({ready, cells: {grid: [[band, mode, 0|1]]}, qsos,
// sets: {l48: {label, grids}}, heard: [{grid, call, band, mode, snr}], me},
// whole on hello, in parts as the index or the roster changes). Read-only.
// Uses common.js (esc, store).

(() => {
  const BAND_ORDER = ["2200m", "630m", "160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m", "4m", "2m", "1.25m", "70cm"];
  const MODE_FIRST = ["FT8", "FT4", "SSB", "CW", "JS8", "RTTY"];
  const CW = 22, CH = 14;                         // a square drawn 2:1 wide-ish: 2° of longitude, 1° of latitude, at ~35°N
  let body = null, api = null, snap = {};
  const pick = { mode: store.get("grids.mode", "All"), band: store.get("grids.band", "All"), basis: store.get("grids.basis", "worked"),
                 alert: store.get("grids.alert", "off") };
  const ALERT_AGAIN_MS = 30 * 60 * 1000;
  const alerted = new Map();                      // "grid|band|mode" -> when it last sounded (or was seen at page open)

  function gridXY(g) {                            // Maidenhead square -> (column, row) in 2°x1° steps
    return { x: (g.charCodeAt(0) - 65) * 10 + Number(g[2]), y: (g.charCodeAt(1) - 65) * 10 + Number(g[3]) };
  }
  function centre(g) {
    if (!/^[A-R]{2}[0-9]{2}/i.test(g || "")) return null;
    const p = gridXY(g.toUpperCase());
    return { lat: p.y - 90 + 0.5, lon: p.x * 2 - 180 + 1 };
  }
  function km(a, b) {
    const r = Math.PI / 180, h = Math.sin((b.lat - a.lat) * r / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin((b.lon - a.lon) * r / 2) ** 2;
    return 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  // grid -> "c" | "w" for one mode and band ("All" for either), from the cells
  function status(mode, band) {
    const out = {};
    for (const [g, list] of Object.entries(snap.cells || {})) {
      for (const [b, m, c] of list) {
        if ((mode !== "All" && m !== mode) || (band !== "All" && b !== band)) continue;
        if (c) { out[g] = "c"; break; }
        out[g] = "w";
      }
    }
    return out;
  }
  const done = (s) => (pick.basis === "confirmed" ? s === "c" : !!s);

  function modes() {
    const seen = new Set();
    for (const list of Object.values(snap.cells || {})) for (const [, m] of list) seen.add(m);
    const rank = (m) => (MODE_FIRST.includes(m) ? MODE_FIRST.indexOf(m) : 99);
    return ["All", ...[...seen].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))];
  }
  function bands() {
    const seen = new Set();
    for (const list of Object.values(snap.cells || {})) for (const [b] of list) seen.add(b);
    const rank = (b) => (BAND_ORDER.includes(b) ? BAND_ORDER.indexOf(b) : 99);
    return ["All", ...[...seen].sort((a, b) => rank(a) - rank(b))];
  }
  function target() { return new Set(((snap.sets || {}).l48 || {}).grids || []); }

  // missing squares someone on the roster is in now, on the chosen mode and band
  function onAir(missing) {
    const out = {};
    for (const h of snap.heard || []) {
      if (!missing.has(h.grid)) continue;
      if (pick.mode !== "All" && (h.mode || "").toUpperCase() !== pick.mode) continue;
      if (pick.band !== "All" && h.band !== pick.band) continue;
      (out[h.grid] = out[h.grid] || []).push(h);
    }
    return out;
  }

  function tally(l48) {
    return modes().map((m) => {
      const st = status(m, pick.band), vals = Object.entries(st);
      const inL = vals.filter(([g]) => l48.has(g));
      const c = inL.filter(([, s]) => s === "c").length, w = inL.length;
      return { mode: m, c, w, miss: l48.size - (pick.basis === "confirmed" ? c : w),
               world_w: vals.length, world_c: vals.filter(([, s]) => s === "c").length };
    });
  }

  function chips(list, cur, key, label) {
    return `<div class="grids-chips" role="group" aria-label="${esc(label)}"><span class="grids-k">${esc(label)}</span>` +
      list.map((v) => `<button class="chip chip-mini${v === cur ? " on" : ""}" data-${key}="${esc(v)}" aria-pressed="${v === cur}">${esc(v)}</button>`).join("") + "</div>";
  }

  function lattice(l48, st, air, me) {
    const pts = [...l48].map(gridXY);
    const x0 = Math.min(...pts.map((p) => p.x)), x1 = Math.max(...pts.map((p) => p.x));
    const y0 = Math.min(...pts.map((p) => p.y)), y1 = Math.max(...pts.map((p) => p.y));
    const W = (x1 - x0 + 1) * CW, H = (y1 - y0 + 1) * CH;
    const cells = [...l48].sort().map((g) => {
      const p = gridXY(g), s = st[g], x = (p.x - x0) * CW, y = (y1 - p.y) * CH;
      const cls = s === "c" ? "c" : s === "w" ? "w" : "m";
      const what = s === "c" ? "confirmed" : s === "w" ? "worked, not confirmed" : "missing";
      const on = air[g] ? ` — on the air: ${air[g].map((h) => h.call).join(", ")}` : "";
      const bandsHere = [...new Set((snap.cells[g] || []).filter(([b, m]) => pick.mode === "All" || m === pick.mode).map(([b]) => b))];
      const tip = `${g}: ${what}${bandsHere.length ? ` (${pick.mode}: ${bandsHere.join(" ")})` : ""}${on}`;
      return `<g class="gq ${cls}${air[g] ? " air" : ""}${g === me ? " me" : ""}" data-g="${g}"><title>${esc(tip)}</title>` +
        `<rect x="${x + 0.5}" y="${y + 0.5}" width="${CW - 1}" height="${CH - 1}" rx="2"/>` +
        `<text x="${x + CW / 2}" y="${y + CH / 2 + 2}">${g}</text></g>`;
    }).join("");
    return `<svg class="grids-lattice" viewBox="0 0 ${W} ${H}" role="img" aria-label="The 488 Lower-48 grid squares, by status">${cells}</svg>`;
  }

  function render() {
    if (!body) return;
    if (snap.error) { body.innerHTML = `<div class="grids"><p class="grids-note">${esc(snap.error)}</p></div>`; return; }
    if (!snap.ready) { body.innerHTML = `<div class="grids"><p class="grids-note">The logbook's needs index is still being built; the grids follow in a moment.</p></div>`; return; }
    const ms = modes(), bs = bands();
    if (!ms.includes(pick.mode)) pick.mode = "All";
    if (!bs.includes(pick.band)) pick.band = "All";
    const l48 = target(), st = status(pick.mode, pick.band);
    const missing = new Set([...l48].filter((g) => !done(st[g])));
    const air = onAir(missing), me = (snap.me || "").slice(0, 4).toUpperCase(), here = centre(me);
    const list = [...missing].map((g) => ({ g, km: here ? km(here, centre(g)) : null, air: air[g] }))
      .sort((a, b) => (!!b.air - !!a.air) || ((a.km ?? 0) - (b.km ?? 0)) || a.g.localeCompare(b.g));
    const rows = tally(l48);
    const sel = rows.find((r) => r.mode === pick.mode) || rows[0];
    const scope = `${pick.mode === "All" ? "any mode" : pick.mode}, ${pick.band === "All" ? "any band" : pick.band}`;

    body.innerHTML = `<div class="grids">
      <div class="grids-bar">
        ${chips(ms, pick.mode, "mode", "Mode")}
        ${chips(bs, pick.band, "band", "Band")}
        ${chips(["worked", "confirmed"], pick.basis, "basis", "Count")}
        ${chips(["off", "chime", "voice"], pick.alert, "alert", "Alert")}
      </div>
      <div class="grids-head">
        <div class="grids-big"><b>${sel.miss}</b><span>Lower-48 squares missing<br>${esc(scope)}, counting ${esc(pick.basis)}</span></div>
        <div class="grids-big"><b>${sel.c}</b><span>confirmed</span></div>
        <div class="grids-big"><b>${sel.w}</b><span>worked</span></div>
        <div class="grids-big"><b>${Object.keys(air).length}</b><span>missing, on the air now</span></div>
      </div>
      <div class="grids-main">
        <figure class="grids-map">
          ${lattice(l48, st, air, me)}
          <figcaption class="grids-legend">
            <span><i class="c"></i>confirmed</span><span><i class="w"></i>worked, not confirmed</span>
            <span><i class="m"></i>missing</span><span><i class="air"></i>missing, on the air now</span><span><i class="me"></i>you (${esc(me || "?")})</span>
          </figcaption>
        </figure>
        <div class="grids-side">
          <table class="grids-tally">
            <caption>By mode, ${esc(pick.band === "All" ? "all bands" : pick.band)}</caption>
            <thead><tr><th>Mode</th><th title="Lower-48 squares confirmed">L48 conf</th><th title="Lower-48 squares worked">L48 wkd</th><th title="Lower-48 squares missing (by the Count setting)">Missing</th><th title="Squares worked / confirmed anywhere">World</th></tr></thead>
            <tbody>${rows.map((r) => `<tr data-mode="${esc(r.mode)}" class="${r.mode === pick.mode ? "on" : ""}" tabindex="0">
              <td>${esc(r.mode)}</td><td>${r.c}</td><td>${r.w}</td><td><b>${r.miss}</b></td><td>${r.world_w} / ${r.world_c}</td></tr>`).join("")}</tbody>
          </table>
          <div class="grids-missing">
            <div class="grids-k">Missing, ${esc(scope)}: ${list.length}${here ? " (nearest first)" : ""}</div>
            <ol>${list.map((x) => `<li class="${x.air ? "air" : ""}"><b>${x.g}</b>${x.km != null ? `<span>${Math.round(x.km)} km</span>` : ""}` +
              (x.air ? `<em>${esc(x.air.map((h) => `${h.call} ${h.band} ${h.mode} ${h.snr ?? ""}`).join(" · "))}</em>` : "") + "</li>").join("")}</ol>
          </div>
        </div>
      </div>
      <p class="grids-note">${snap.qsos} QSOs in the logbook. Worked and confirmed come from the logbook sync (confirmed: LoTW or QRZ); the Lower 48 is ARRL's FFMA set of 488 squares.</p>
    </div>`;

    const set = (k, v) => { pick[k] = v; store.set(`grids.${k}`, v); render(); };
    body.querySelectorAll("[data-mode]").forEach((el) => {
      el.onclick = () => set("mode", el.dataset.mode);
      if (el.tagName === "TR") el.onkeydown = (e) => { if (e.key === "Enter") set("mode", el.dataset.mode); };
    });
    body.querySelectorAll("[data-band]").forEach((el) => { el.onclick = () => set("band", el.dataset.band); });
    body.querySelectorAll("[data-basis]").forEach((el) => { el.onclick = () => set("basis", el.dataset.basis); });
    body.querySelectorAll("[data-alert]").forEach((el) => {
      el.onclick = () => {
        set("alert", el.dataset.alert);
        if (pick.alert !== "off") {
          chime();                                // the click is the gesture a browser wants before it plays sound
          if (window.Notification && Notification.permission === "default") Notification.requestPermission();
        }
      };
    });
  }

  function airNow() {                            // [{grid, band, mode, call, snr}]: missing squares on the air, as filtered
    if (!snap.ready) return [];
    const l48 = target(), st = status(pick.mode, pick.band);
    return Object.entries(onAir(new Set([...l48].filter((g) => !done(st[g]))))).flatMap(([g, hs]) => hs.map((h) => ({ ...h, grid: g })));
  }

  function chime() {                              // two rising notes: not the Roster's single beep
    try {
      const a = new (window.AudioContext || window.webkitAudioContext)(), g = a.createGain();
      g.gain.value = 0.08; g.connect(a.destination);
      [[660, 0], [990, 0.16]].forEach(([f, t]) => {
        const o = a.createOscillator(); o.frequency.value = f; o.connect(g);
        o.start(a.currentTime + t); o.stop(a.currentTime + t + 0.14);
      });
    } catch (e) { /* no audio: the toast still shows */ }
  }

  function say(h) {
    try {
      const words = `Wanted grid ${h.grid.split("").join(" ")}, ${(h.band || "").replace(/m$/, " metres")} ${h.mode || ""}`;
      speechSynthesis.speak(new SpeechSynthesisUtterance(words));
    } catch (e) { chime(); }
  }

  // seen: true at a snapshot (page open, switched on): remember, don't sound
  function alertFor(seen) {
    const now = Date.now();
    for (const h of airNow()) {
      const k = `${h.grid}|${h.band}|${h.mode}`;
      if (now - (alerted.get(k) ?? -Infinity) < ALERT_AGAIN_MS) continue;
      alerted.set(k, now);
      if (seen || pick.alert === "off") continue;
      toast(`Grids: ${h.grid} on the air — ${h.call} ${h.band} ${h.mode} ${h.snr ?? ""}`.trim(), "info");
      if (pick.alert === "voice" && window.speechSynthesis) say(h); else chime();
      if (window.Notification && Notification.permission === "granted") {
        try { new Notification(`Wanted grid ${h.grid}`, { body: `${h.call} ${h.band} ${h.mode}` }); } catch (e) { /* ignore */ }
      }
    }
    if (alerted.size > 2000) for (const [k, t] of alerted) if (now - t > ALERT_AGAIN_MS) alerted.delete(k);
  }

  function missingOnAir() {
    if (!snap.ready) return 0;
    const l48 = target(), st = status(pick.mode, pick.band);
    return Object.keys(onAir(new Set([...l48].filter((g) => !done(st[g]))))).length;
  }

  addons.register("grids", {
    mount(b, a) { body = b; api = a; render(); },
    hello(s) { snap = s || {}; alertFor(true); render(); },
    event(m) { snap = { ...snap, ...m }; delete snap.t; delete snap.addon; alertFor(false); render(); },
    off() { snap = {}; render(); },
    tab() {
      if (!snap.ready) return { dot: "off", title: "Grids: waiting for the logbook" };
      const n = missingOnAir();
      return { dot: n ? "tx" : "ok", title: n ? `${n} missing grid${n > 1 ? "s" : ""} on the air now` : "The grids you still need" };
    },
    card() {
      if (!snap.ready) return { state: "off", lines: [] };
      const l48 = target(), st = status(pick.mode, pick.band);
      const miss = [...l48].filter((g) => !done(st[g])).length, n = missingOnAir();
      return { state: "on", lines: [`${miss} Lower-48 missing (${pick.mode}, ${pick.band})`, ...(n ? [`${n} on the air now`] : [])] };
    },
  });
})();
