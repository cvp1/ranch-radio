"use strict";

// Mission Control's band strip (UX plan PR 3a, 2026-11-19): one cell per
// amateur band at or below 54 MHz that the focus radio covers (its
// config.caps.freq), each with three facts kept apart, each with its own
// source, window and unknown states, never merged into one "open/closed":
//
//   heard here  skywatch's decodes on the band, last window_min minutes
//   hears you   PSKReporter stations that reported your call on the band
//   forecast    skywatch's forecast windows covering now; a window is one
//               path (band and region), never the whole band
//
// and, on the band JTDX is decoding, the Roster's strong needs.
//
// From R2 (needs-all-bands, 2026-11-21) a fourth fact on every band:
//
//   need near you  stations that receivers in your grid decoded in the last
//                  15 minutes (PSKReporter, "near"), judged by the logbook's
//                  needs index; only the strong levels count, as on the Roster.
//                  A button under the cells (near-dxcc-only, 2026-11-21) narrows
//                  it to DXCC needs (new DXCC, on the band, in the mode); the
//                  choice is this browser's (store "bands.near"), default all.
//
// From PR 4c (2026-11-19) each cell's band name is a button that tunes the
// focus radio to that band (its band stack: where it was last left there,
// else the band's default), through the tune the station gives in render()'s
// context (station.tuneFocus, the one way that page retunes a radio). The
// rest of a cell stays display: its facts keep their own titles.
//
// Reads addons.data("propagation") and addons.data("roster") (UX plan 5.3).
// station.js calls render(el, {range, radio, autoTune, tune}) whenever
// Mission Control redraws, and for Operate's strip (PR 2b); each strip keeps
// its own ctx; the strip also redraws itself when the UTC hour
// turns, with or without an event, since a window "now to 05Z" ends on the
// hour and the next one starts. Uses common.js (BANDS, esc, regionName,
// fmtFreq, bandChangeNote) and addons.js.

const bandStrip = (() => {
  // Each element it draws into: its context and the HTML last written. Two
  // now (UX plan PR 2b, 2026-11-19): Mission Control's and Operate's; one
  // shared "last HTML" would leave the second blank when both say the same.
  const drawn = new Map();
  let ctx = {}, lastHour = null;
  const FORECAST_STALE_S = 36 * 3600;          // skywatch issues one a day (propagation.js)

  const key = (b) => b.label || `${b.n}m`;
  const fmtN = (n) => (n >= 10000 ? `${Math.round(n / 1000)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const hh = (ts) => `${new Date(ts * 1000).toISOString().slice(11, 13)}Z`;
  const ago = (ts) => {
    const s = Math.max(0, api.now() - ts);
    return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
  };
  // A forecast window's peak is a 0..1 fraction (addons/propagation.py
  // parse_forecast) and the best hour of the window, not the chance now.
  const pct = (p) => `${Math.round(p * 100)}%`;

  // The bands the focus radio covers; every HF band and 6 m until its config is in.
  function bandsFor(range) {
    const hf = BANDS.filter((b) => b.hi <= 54e6);
    if (!Array.isArray(range) || range.length !== 2) return hf;
    return hf.filter((b) => b.lo <= range[1] && b.hi >= range[0]);
  }

  // ---------- the three facts: {state, text, title}
  // state: ok | none (the source answered: nothing on this band) |
  //        wait (no answer yet) | err (the source failed) | stale | off (not set up)

  function heard(P, k) {
    if (!P) return { state: "off", text: "", title: "Propagation is not on: what this station heard is unknown" };
    if (!P.skywatch) return { state: "off", text: "", title: "Heard here: not set up (no skywatch database)" };
    if (P.heard_error) return { state: "err", text: "heard: ?", title: `${P.heard_error}: what this station heard on ${k} is unknown, not zero` };
    if (!P.heard_checked) return { state: "wait", text: "heard: …", title: "Waiting for skywatch's first answer" };
    const d = (P.bands || {})[k], w = P.window_min;
    if (!d || !d.n) return { state: "none", text: "no decodes", title: `${k}: no decodes in the last ${w} min: may not have been monitored` };
    return { state: "ok", text: `<b>${fmtN(d.n)}</b> decodes`, title: `${k}: ${fmtN(d.n)} decodes, last ${w} min` };
  }

  function hearsYou(P, k) {
    if (!P || !P.pskreporter) return { state: "off", text: "", title: "Hears you: not set up (no PSKReporter callsign)" };
    const call = P.pskreporter, w = P.window_min;
    if (P.psk_error) {
      // The server drops its counts on an error; propagation.js keeps the last
      // good ones (psk_last). Shown, marked stale with their age, never as now.
      const y = P.psk_last?.bands?.[k];
      if (y && y.receivers) {
        return { state: "stale", text: `✓ ${y.receivers}`,
          title: `${P.psk_error}: stale. ${plural(y.receivers, "station", "stations")} reported ${call} on ${k}, checked ${ago(P.psk_last.checked)}` };
      }
      return { state: "err", text: "", title: `${P.psk_error}: who hears you on ${k} is unknown` };
    }
    if (!P.psk_checked) return { state: "wait", text: "", title: "Waiting for PSKReporter's first answer" };
    const y = (P.psk || {})[k];
    if (!y || !y.receivers) return { state: "none", text: "", title: `No PSKReporter reports of ${call} on ${k} in the last ${w} min (have you transmitted?)` };
    return { state: "ok", text: `✓ ${y.receivers}`,
      title: `${plural(y.receivers, "station", "stations")} reported ${call} on ${k} in the last ${w} min (PSKReporter)` };
  }

  function forecast(P, k, now) {
    if (!P || !P.skywatch) return { state: "off", text: "", title: "Forecast: not set up (no skywatch database)" };
    if (P.forecast_error) return { state: "err", text: "forecast: ?", title: `${P.forecast_error}: the forecast is unknown` };
    if (!P.heard_checked) return { state: "wait", text: "", title: "Waiting for skywatch's first answer" };
    // skywatch answered and there is no forecast issue: no forecast
    // measurement there, which is "not set up", not "nothing open".
    if (!P.forecast_issued) return { state: "off", text: "", title: "Forecast: skywatch has no forecast measurement" };
    const ws = (P.forecast || []).filter((w) => w.band === k);
    // end is exclusive: a window ending now has closed.
    const cur = ws.filter((w) => w.start <= now && now < w.end).sort((a, b) => b.peak - a.peak);
    const issued = `issued ${ago(P.forecast_issued)}`;
    if (!cur.length) {
      const next = ws.filter((w) => w.start > now).sort((a, b) => a.start - b.start)[0];
      return { state: "none", text: next ? `next: ${esc(regionName(next.region))} ${hh(next.start)}` : "",
        title: `${k}: no path forecast open now${next ? `; next ${regionName(next.region)} from ${hh(next.start)}` : ""} (forecast ${issued})` };
    }
    const w = cur[0];
    const line = (x) => `${regionName(x.region)}, now to ${hh(x.end)}, peak ${pct(x.peak)}`;
    return { state: "ok",
      text: esc(line(w)) + (cur.length > 1 ? ` <small>+${plural(cur.length - 1, "path", "paths")}</small>` : ""),
      title: `${k} forecast open now, by path:\n${cur.map(line).join("\n")}\n` +
             `Peak: the forecast chance at the window's best hour, not the chance now. Forecast ${issued}.` };
  }

  // Needed near you (R2): {state, text, title} like the three facts above.
  const NEED_WORD = { dxcc: "new DXCC", dxcc_band: "new DXCC on this band", dxcc_mode: "new DXCC in this mode",
    grid: "new grid", grid_band: "new grid on this band", grid_mode: "new grid in this mode" };
  const DXCC = ["dxcc", "dxcc_band", "dxcc_mode"];
  const nearDxcc = () => store.get("bands.near", "all") === "dxcc";
  function nearNeeds(P, k) {
    if (!P || !P.near) return { state: "off", text: "", title: 'Needed near you: not set up ("pskreporter": {"near": true})' };
    const g = P.near_grid || "your grid", w = P.near_window_min;
    if (P.near_error) return { state: "err", text: "need: ?", title: `${P.near_error}: who you need on ${k} near ${g} is unknown` };
    if (!P.near_checked) return { state: "wait", text: "", title: "Waiting for PSKReporter's first answer about your grid" };
    if (!P.near_bands) return { state: "wait", text: "need: ?",
      title: "The needs index is not ready (building, or no logbook): who you need is unknown, not zero" };
    const b = P.near_bands[k];
    if (!b) return { state: "none", text: "", title: `Nobody reported decoded near ${g} on ${k} in the last ${w} min` };
    const only = nearDxcc();
    const n = only ? DXCC.reduce((t, l) => t + ((b.levels || {})[l] || 0), 0) : b.strong;
    const what = only ? "DXCC" : "DXCC or a grid";
    if (!n) return { state: "none", text: "",
      title: `${plural(b.heard, "station", "stations")} decoded near ${g} on ${k} in the last ${w} min; none you need for ${what}` };
    const top = (b.top || []).filter((x) => !only || DXCC.includes(x.need)).map((x) => `${x.call} (${x.grid}, ${x.mode}${x.snr != null ? ` ${x.snr} dB` : ""}): ${NEED_WORD[x.need] || x.need}`);
    return { state: "ok", text: `${n} need`,
      title: `${plural(n, "station", "stations")} you need${only ? " for DXCC" : ""}, of ${b.heard} decoded near ${g} on ${k} in the last ${w} min (PSKReporter):\n` +
             top.join("\n") + (n > top.length ? `\n+${n - top.length} more` : "") };
  }

  // The Roster's strong needs, for the band JTDX decodes on. null: no roster here.
  function needs() {
    const R = addons.data("roster");
    if (!R || R.error || !R.roster || !R.roster.band) return null;
    const c = R.need_counts;
    if (!c) return { band: R.roster.band, state: "wait", text: "strong needs: unknown",
      title: "The needs index is not ready (building, or no logbook): unknown, not zero" };
    const text = `${plural(c.strong, "recent station", "recent stations")} with strong needs` +
      (c.unconfirmed ? ` · ${c.unconfirmed} unconfirmed` : "");
    return { band: c.band, state: c.strong ? "ok" : "none", text,
      title: `JTDX on ${c.band} ${c.mode}, the last two minutes: ${text}; ${plural(c.new_calls, "new call", "new calls")}` };
  }

  // The band's name: a tune button when the station gave a tune (PR 4c).
  // Its title says what a click does and, as every band-changing control
  // does (PR 1d), that on a station with auto-tune a band change transmits a
  // tune carrier (common.js bandChangeNote, for the focus radio).
  function bandEl(c) {
    if (typeof ctx.tune !== "function") return `<b class="bs-band">${esc(c.k)}</b>`;
    const t = `Tune the ${ctx.radio || "focus radio"} to ${c.k}: where it was last left there, else ` +
      `${fmtFreq(c.b.f)} ${c.b.m}. Tuning must be unlocked (the rig bar's padlock). ${bandChangeNote(!!ctx.autoTune)}`;
    return `<button type="button" class="bs-band bs-tune" data-n="${esc(c.b.n)}" title="${esc(t)}" ` +
      `aria-label="${esc(t)}">${esc(c.k)}</button>`;
  }

  const fact = (cls, f) => `<div class="bs-fact ${cls}" data-state="${f.state}" title="${esc(f.title)}">${f.text}</div>`;

  // One line for each fact whose state is the same on every band (not set
  // up, waiting, failed): said once here, not eleven times in the cells.
  function notes(P, cells, N, shown) {
    const out = [];
    const h = cells[0]?.h, y = cells[0]?.y, f = cells[0]?.f, n = cells[0]?.n;
    if (!P) out.push("Propagation is off: heard here, hears you and the forecast are unknown");
    else {
      if (h && (h.state === "ok" || h.state === "none")) out.push(`Heard here: decodes, last ${P.window_min} min`);
      else if (h) out.push(h.title.replace(/ on \S+ is unknown/, " is unknown"));
      if (P.pskreporter && P.psk_error) out.push(`${P.psk_error}: who hears you is unknown${cells.some((c) => c.y.state === "stale") ? " (✓ last known, stale)" : ""}`);
      else if (y && y.state !== "off" && y.state !== "wait") out.push(`✓ hears you: PSKReporter reports of ${P.pskreporter}, last ${P.window_min} min`);
      else if (y) out.push(y.title);
      if (f && (f.state === "ok" || f.state === "none")) {
        const old = api.now() - P.forecast_issued > FORECAST_STALE_S;
        out.push(`Forecast: skywatch, ${old ? "out of date, " : ""}issued ${ago(P.forecast_issued)}; peak is a path's best hour, not the chance now`);
      } else if (f) out.push(f.title);
      if (n && (n.state === "ok" || n.state === "none")) out.push(`Need: stations decoded near ${P.near_grid}, last ${P.near_window_min} min (PSKReporter), ${nearDxcc() ? "DXCC only" : "new DXCC or grid"}`);
      else if (n && n.state !== "off") out.push(n.title.replace(/ on \S+ near \S+ is unknown/, " is unknown"));
    }
    if (N && !shown) out.push(`JTDX on ${N.band}: ${N.text}`);
    return out;
  }

  function html() {
    const P = addons.data("propagation"), N = needs(), now = api.now();
    const cells = bandsFor(ctx.range).map((b) => {
      const k = key(b);
      return { b, k, h: heard(P, k), y: hearsYou(P, k), f: forecast(P, k, now), n: nearNeeds(P, k) };
    });
    const shown = N && cells.some((c) => c.k === N.band);
    const body = cells.map((c) => {
      const dec = N && c.k === N.band;
      return `<div class="bs-cell${dec ? " bs-decoding" : ""}" data-band="${esc(c.k)}">
        <div class="bs-head">${bandEl(c)}${fact("bs-you", c.y)}</div>
        ${fact("bs-heard", c.h)}${fact("bs-fc", c.f)}${fact("bs-near", c.n)}
        ${dec ? `<div class="bs-needs" data-state="${N.state}" title="${esc(N.title)}">${esc(N.text)}</div>` : ""}
      </div>`;
    }).join("");
    const ns = notes(P, cells, N, shown);
    // The DXCC-only switch, once the count has something to narrow.
    const sw = cells.some((c) => c.n.state === "ok" || c.n.state === "none")
      ? `<button type="button" class="bs-near-mode" aria-pressed="${nearDxcc()}" title="${nearDxcc()
          ? "Counting DXCC needs only: click to count new grids too" : "Counting new DXCC and new grids: click to count DXCC only"}">${nearDxcc() ? "Need: DXCC only" : "Need: DXCC + grids"}</button>` : "";
    return `<div class="bs-cells" role="list" aria-label="Bands">${body}</div>` +
      (ns.length || sw ? `<div class="bs-notes">${ns.map((n) => `<span>${esc(n)}</span>`).join("")}${sw}</div>` : "");
  }

  // target: the strip's element; c: {range: the focus radio's caps.freq or null}.
  function render(target, c = {}) {
    if (!target) return;
    const d = drawn.get(target) || { html: "" };
    d.ctx = c;
    drawn.set(target, d);
    ctx = c;
    // One listener per strip, reading that strip's newest ctx at click time
    // (Mission Control's and Operate's each have their own): a click tunes
    // whatever radio has focus then (tuneFocus reads it again).
    if (!target.dataset.tuneBound) {
      target.dataset.tuneBound = "1";
      target.addEventListener("click", (e) => {
        if (e.target.closest?.(".bs-near-mode")) {          // a display choice: redraw every strip, tune nothing
          try { store.set("bands.near", nearDxcc() ? "all" : "dxcc"); } catch { /* storage off: stays all */ }
          for (const [t, d] of drawn) render(t, d.ctx);
          return;
        }
        const btn = e.target.closest?.(".bs-tune");
        const t = drawn.get(target)?.ctx?.tune;
        if (btn && target.contains(btn) && typeof t === "function") t(btn.dataset.n);
      });
    }
    lastHour = Math.floor(api.now() / 3600);
    // Nothing to say without the add-ons that feed it.
    const here = addons.list().some((x) => x.name === "propagation" || x.name === "roster");
    target.hidden = !here;
    if (!here) { d.html = ""; target.innerHTML = ""; return; }
    const h = html();
    if (h === d.html) return;              // Mission Control redraws often (S-meters)
    d.html = h;
    target.innerHTML = h;
  }

  // The hour turned: redraw every strip without waiting for an event.
  setInterval(() => {
    if (!drawn.size || Math.floor(api.now() / 3600) === lastHour) return;
    for (const [t, d] of drawn) render(t, d.ctx);
  }, 5000);

  // now(): epoch seconds; the tests replace it to cross an hour.
  const api = { now: () => Date.now() / 1000, render };
  return api;
})();
