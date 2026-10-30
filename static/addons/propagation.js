"use strict";

// Propagation add-on (addons/propagation.py). Two places:
//  - beside the radio page's band buttons: a heat badge on each band heard in
//    the last hour, and a one-line space weather strip under them;
//  - its own tab: Conditions (K index, flares, solar flux, each with a scale
//    and what it means on the air) and Bands (per band: how busy, where from,
//    who heard you), with the same numbers as a table for screen readers.
// A band with no decodes says "no data", never "closed": a receiver only
// hears the band it is tuned to.

(() => {
  let snap = {}, body = null, observer = null, tip = null, showTable = false;

  // Regions keep one color everywhere (fixed slots, validated for colour
  // vision deficiency on the dark panel; see propagation.css).
  const REGIONS = [
    ["JA", "Japan"], ["NA_EAST", "NA east"], ["EU", "Europe"], ["SA", "S America"],
    ["VK_ZL", "VK/ZL"], ["NA_WEST", "NA west"], ["OCEANIA_DEEP", "Pacific"],
  ];
  const SLOT = Object.fromEntries(REGIONS.map(([k], i) => [k, i + 1]));
  const regionName = (r) => (REGIONS.find(([k]) => k === r) || [r, r.replace(/_/g, " ")])[1];
  const regionSlot = (r) => SLOT[r] || 8;          // anything else shares slot 8 ("other")
  const SOURCE = { jtdx: "JTDX", wsjtx: "WSJT-X", skimmer: "skimmer", js8: "JS8Call" };
  const HF = BANDS.filter((b) => b.hi <= 54e6);
  const key = (b) => (b.label || b.n + "m");

  const fmtN = (n) => (n >= 10000 ? `${Math.round(n / 1000)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
  const level = (n) => (n >= 500 ? 4 : n >= 100 ? 3 : n >= 10 ? 2 : n > 0 ? 1 : 0);
  const LEVEL = ["", "a trickle", "some activity", "busy", "very busy"];
  const ago = (ts) => {
    if (!ts) return "";
    const s = Math.max(0, Date.now() / 1000 - ts);
    return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
  };

  // ------------------------------------------------ what the numbers mean
  function kpRead(kp) {
    if (kp == null) return null;
    if (kp >= 7) return { st: "critical", icon: "■", word: "Severe storm", say: "HF badly degraded, polar paths closed. Aurora likely on 6m and 2m." };
    if (kp >= 5) return { st: "serious", icon: "▲", word: "Storm", say: "Expect weak, fluttery signals, worst on high-latitude paths. Aurora possible on 6m." };
    if (kp >= 4) return { st: "warning", icon: "▲", word: "Active", say: "Some absorption on polar paths; mid-latitude paths mostly fine." };
    return { st: "good", icon: "●", word: kp >= 3 ? "Unsettled" : "Quiet", say: "Stable ionosphere: normal HF, polar paths open." };
  }
  function xrayRead(c) {
    if (!c) return null;
    const L = c[0];
    if (L === "X") return { st: "critical", icon: "■", word: "Major flare", say: "HF blackout likely on the sunlit side of the Earth." };
    if (L === "M") return { st: "warning", icon: "▲", word: "Flare", say: "Short HF fadeouts possible on daytime paths." };
    if (L === "C") return { st: "good", icon: "●", word: "Minor flares", say: "Little effect on HF." };
    return { st: "good", icon: "●", word: "Quiet sun", say: "No flares: daytime bands unaffected." };
  }
  function sfiRead(sfi) {
    if (sfi == null) return null;
    if (sfi >= 170) return { word: "Excellent", say: "10m wide open by day; 6m F2 possible." };
    if (sfi >= 120) return { word: "Good", say: "15m to 10m open by day." };
    if (sfi >= 80) return { word: "Moderate", say: "20m to 15m best by day; 10m patchy." };
    return { word: "Low", say: "40m to 20m carry the load; upper HF weak." };
  }

  // A horizontal scale: zones (from, to, class), ticks, and a marker at value.
  function gauge(min, max, value, zones, ticks, log = false) {
    const pos = (v) => {
      const t = log ? (Math.log10(v) - Math.log10(min)) / (Math.log10(max) - Math.log10(min)) : (v - min) / (max - min);
      return Math.max(0, Math.min(100, t * 100));
    };
    const z = zones.map(([a, b, cls]) => `<i class="pg-zone ${cls}" style="left:${pos(a)}%;width:${pos(b) - pos(a)}%"></i>`).join("");
    const t = ticks.map(([v, label]) => `<span class="pg-tick" style="left:${pos(v)}%">${esc(label)}</span>`).join("");
    const m = value == null ? "" : `<b class="pg-mark" style="left:${pos(value)}%"></b>`;
    return `<div class="pg-gauge" aria-hidden="true"><div class="pg-track">${z}${m}</div><div class="pg-ticks">${t}</div></div>`;
  }

  function condCard(label, value, read, gaugeHtml, ts, stale) {
    const pill = read && read.st
      ? `<span class="pg-pill st-${read.st}"><i>${read.icon}</i>${esc(read.word)}</span>`
      : read ? `<span class="pg-pill">${esc(read.word)}</span>` : "";
    return `<article class="pg-cond${read && read.st ? ` st-${read.st}` : ""}">
      <header><span class="label">${esc(label)}</span>${pill}</header>
      <div class="pg-value">${value == null ? "—" : esc(String(value))}${stale ? `<small title="Older than 30 minutes">stale</small>` : ""}</div>
      ${gaugeHtml}
      <p class="pg-say">${read ? esc(read.say) : "Waiting for NOAA…"}</p>
      <footer>${ts ? `NOAA · ${esc(ago(ts))}` : ""}</footer>
    </article>`;
  }

  function conditions() {
    if (!snap.swpc) return "";
    const kp = snap.kp?.kp, xc = snap.xray?.class, flux = snap.xray?.flux, sfi = snap.f107?.sfi;
    return `<section class="pg-section">
      <h3 class="pg-h">Conditions</h3>
      <div class="pg-conds">
        ${condCard("K index (geomagnetic)", kp, kpRead(kp),
          gauge(0, 9, kp, [[0, 4, "z-good"], [4, 5, "z-warning"], [5, 7, "z-serious"], [7, 9, "z-critical"]],
                [[0, "0"], [3, "3"], [5, "5"], [7, "7"], [9, "9"]]), snap.kp?.ts, snap.kp_stale)}
        ${condCard("X-ray (solar flares)", xc, xrayRead(xc),
          gauge(1e-8, 1e-3, flux, [[1e-8, 1e-5, "z-good"], [1e-5, 1e-4, "z-warning"], [1e-4, 1e-3, "z-critical"]],
                [[1e-8, "A"], [1e-7, "B"], [1e-6, "C"], [1e-5, "M"], [1e-4, "X"]], true), snap.xray?.ts)}
        ${condCard("Solar flux (SFI)", sfi, sfiRead(sfi),
          gauge(60, 250, sfi, [[60, 250, "z-flux"]], [[60, "60"], [100, "100"], [150, "150"], [200, "200"], [250, "250"]]),
          snap.f107?.ts)}
      </div>
      ${snap.space_error ? `<p class="pg-note">${esc(snap.space_error)}; showing the last values it gave.</p>` : ""}
    </section>`;
  }

  // ------------------------------------------------ bands
  const tunedBand = () => {
    const v = document.querySelector("#bands .chip.on")?.dataset.v;
    const b = v && BANDS.find((x) => x.n === v);
    return b ? key(b) : null;
  };

  function regionBar(d) {
    const rs = d.regions || [];
    const total = rs.reduce((a, [, n]) => a + n, 0);
    if (!total) return `<div class="pg-rbar empty">no regions known</div>`;
    const segs = rs.map(([r, n]) => {
      const pct = (100 * n) / total;
      return `<i class="rs-${regionSlot(r)}" style="flex:${n}" data-tip="${esc(regionName(r))}: ${n} decodes (${Math.round(pct)}%)"></i>`;
    }).join("");
    const lead = rs.slice(0, 2).map(([r, n]) => `<span><i class="sw rs-${regionSlot(r)}"></i>${esc(regionName(r))} ${n}</span>`).join("");
    return `<div class="pg-rbar" role="img" aria-label="${esc(rs.map(([r, n]) => `${regionName(r)} ${n}`).join(", "))}">${segs}</div>
      <div class="pg-rlead">${lead}${rs.length > 2 ? `<span class="more">+${rs.length - 2} more</span>` : ""}</div>`;
  }

  function youCell(b) {
    if (!snap.pskreporter) return "";
    const l = `<em class="pg-you-l">Heard you</em>`;
    if (snap.psk_error) return `<div class="pg-you dim">${l}unknown</div>`;
    const y = (snap.psk || {})[key(b)];
    if (!y) return `<div class="pg-you dim">${l}nobody</div>`;
    const cs = (y.countries || []).slice(0, 3).map(([c, n]) => `${esc(c)} ${n}`).join(" · ");
    return `<div class="pg-you">${l}<b>${y.receivers}</b><span>station${y.receivers === 1 ? "" : "s"}</span>
      <small>${y.max_km != null ? `${y.max_km.toLocaleString()} km` : ""}${y.best_snr != null ? ` · ${y.best_snr} dB` : ""}</small>
      <small class="c">${cs}</small></div>`;
  }

  // ------------------------------------------------ next openings (skywatch's forecast)
  const hhmm = (ts) => new Date(ts * 1000).toISOString().slice(11, 16) + "Z";
  const FORECAST_STALE_S = 36 * 3600;     // skywatch issues one a day

  function openingsSection() {
    // No forecast measurement at all: no section (that is "not set up", not
    // "no openings"). Unreachable: unknown, said so.
    if (!snap.skywatch || (!snap.forecast_issued && !snap.forecast_error)) return "";
    const h = snap.forecast_h || 12;
    const head = `<h3 class="pg-h">Next openings <small>forecast, next ${h} hours</small></h3>`;
    if (snap.forecast_error) {
      return `<section class="pg-section">${head}<p class="pg-lede dim">${esc(snap.forecast_error)}: the forecast is unknown.</p></section>`;
    }
    const now = Date.now() / 1000;
    const ws = snap.forecast || [];
    const rows = ws.map((w) => {
      const live = w.start <= now;
      const when = live ? `now to ${hhmm(w.end)}` : `${hhmm(w.start)} to ${hhmm(w.end)}`;
      return `<li class="pg-open${live ? " live" : ""}">
        <b class="pg-open-band">${esc(w.band)}</b>
        <span class="pg-open-region"><i class="sw rs-${regionSlot(w.region)}"></i>${esc(regionName(w.region))}</span>
        <span class="pg-open-when">${esc(when)}</span>
        <span class="pg-open-p" data-tip="Forecast chance the path is open at its best hour">${Math.round(w.peak * 100)}%</span>
      </li>`;
    }).join("");
    const age = now - snap.forecast_issued;
    const note = `<p class="pg-note">Forecast ${age > FORECAST_STALE_S ? "<b>out of date</b>, " : ""}issued ${esc(ago(snap.forecast_issued))}
      by skywatch. A path's chance of being open, from its history; a forecast, not a promise.</p>`;
    return `<section class="pg-section">${head}
      ${rows ? `<ul class="pg-opens">${rows}</ul>` : `<p class="pg-lede dim">No band is forecast open in the next ${h} hours.</p>`}
      ${note}</section>`;
  }

  function bandsSection() {
    const heardOK = snap.skywatch && !snap.heard_error;
    const bands = heardOK ? snap.bands || {} : {};
    const psk = snap.pskreporter && !snap.psk_error ? snap.psk || {} : {};
    if (!snap.skywatch && !snap.pskreporter) {
      return `<section class="pg-section"><h3 class="pg-h">Bands</h3><div class="st-empty">No decode source is set.
        Name a skywatch InfluxDB ("skywatch": {"url", "db"}) or a PSKReporter callsign ("pskreporter": {"call"})
        in the host file's propagation block to see what the bands are doing.</div></section>`;
    }
    const tuned = tunedBand();
    const max = Math.max(1, ...Object.values(bands).map((d) => d.n));
    const active = HF.filter((b) => bands[key(b)] || psk[key(b)]);
    const idle = HF.filter((b) => !bands[key(b)] && !psk[key(b)]);

    // the headline: where to go
    const ranked = Object.entries(bands).sort((a, b) => b[1].n - a[1].n).slice(0, 3);
    const youRanked = Object.entries(psk).sort((a, b) => b[1].receivers - a[1].receivers).slice(0, 2);
    let lede = "";
    if (heardOK && ranked.length) {
      lede += `<p class="pg-lede">Busiest now: ${ranked.map(([b, d]) => `<b>${esc(b)}</b> <span>${fmtN(d.n)}</span>`).join(" · ")}`;
      if (youRanked.length) lede += `<br>Heard you: ${youRanked.map(([b, y]) => `<b>${esc(b)}</b> <span>${y.receivers} stations</span>`).join(" · ")}`;
      lede += `</p>`;
    } else if (heardOK) {
      lede = `<p class="pg-lede dim">Nothing decoded in the last ${snap.window_min} minutes.</p>`;
    }

    // a legend for the regions on screen
    const present = [...new Set(Object.values(bands).flatMap((d) => (d.regions || []).map(([r]) => regionSlot(r))))].sort();
    const legend = present.length ? `<div class="pg-legend">${present.map((s) => {
      const name = s === 8 ? "Other" : REGIONS[s - 1][1];
      return `<span><i class="sw rs-${s}"></i>${esc(name)}</span>`;
    }).join("")}</div>` : "";

    const rows = active.map((b) => {
      const k = key(b), d = bands[k];
      const pct = d ? Math.max(4, Math.round((100 * Math.log10(1 + d.n)) / Math.log10(1 + max))) : 0;
      const src = d ? Object.keys(d.sources || {}).map((s) => SOURCE[s] || s).join(" + ") : "";
      const heard = !heardOK ? `<div class="pg-busy dim">${snap.skywatch ? "unknown" : ""}</div>`
        : !d ? `<div class="pg-busy dim">no decodes</div>`
        : `<div class="pg-busy" data-tip="${d.n} decodes in the last ${snap.window_min} min, heard by ${esc(src)}">
             <div class="pg-count"><b>${fmtN(d.n)}</b><span>${LEVEL[level(d.n)]}</span></div>
             <div class="pg-meter"><i class="lv${level(d.n)}" style="width:${pct}%"></i></div>
           </div>`;
      const stats = d ? `<div class="pg-stats">
          ${d.best_snr != null ? `<span><small>best</small>${d.best_snr > 0 ? "+" : ""}${d.best_snr} dB</span>` : ""}
          ${d.max_km != null ? `<span><small>farthest</small>${d.max_km.toLocaleString()} km</span>` : ""}
          <span class="src">${esc(src)}</span></div>` : `<div class="pg-stats"></div>`;
      return `<div class="pg-row${k === tuned ? " tuned" : ""}">
        <div class="pg-band"><b>${esc(k)}</b>${k === tuned ? `<span class="pg-tuned">tuned</span>` : ""}</div>
        ${heard}
        <div class="pg-regions">${d ? regionBar(d) : ""}</div>
        ${stats}
        ${youCell(b)}
      </div>`;
    }).join("");

    const head = `<div class="pg-row pg-headrow" aria-hidden="true"><div>Band</div><div>Decodes, last ${snap.window_min} min</div>
      <div>Heard from</div><div>Signals</div>${snap.pskreporter ? "<div>Heard you</div>" : ""}</div>`;
    const quiet = idle.length ? `<p class="pg-idle"><span>No decodes:</span> ${idle.map((b) => `<i>${esc(key(b))}</i>`).join("")}
      <small>Usually means nobody listened there, not that the band is closed.</small></p>` : "";
    const errs = [snap.skywatch && snap.heard_error ? `${snap.heard_error}: what this station heard is unknown, not zero.` : "",
                  snap.pskreporter && snap.psk_error ? `${snap.psk_error}: who heard you is unknown.` : ""].filter(Boolean);
    const foot = `<p class="pg-note">${errs.map(esc).join(" ")}
      ${heardOK ? `Decodes updated ${esc(ago(snap.heard_checked))}.` : ""}
      ${snap.pskreporter && !snap.psk_error ? `PSKReporter reports of ${esc(snap.pskreporter)} checked ${esc(ago(snap.psk_checked))}.` : ""}
      <button class="chip chip-mini pg-tbl">${showTable ? "Hide table" : "Table view"}</button></p>`;

    return `<section class="pg-section">
      <h3 class="pg-h">Bands <small>last ${snap.window_min} minutes</small></h3>
      ${lede}${legend}
      ${rows ? `<div class="pg-board${snap.pskreporter ? " with-you" : ""}">${head}${rows}</div>` : ""}
      ${quiet}${foot}${showTable ? table() : ""}
    </section>`;
  }

  // The same numbers as a plain table (screen readers, copying).
  function table() {
    const bands = snap.skywatch && !snap.heard_error ? snap.bands || {} : {};
    const psk = snap.pskreporter && !snap.psk_error ? snap.psk || {} : {};
    const rows = HF.map((b) => {
      const k = key(b), d = bands[k], y = psk[k];
      return `<tr><th scope="row">${esc(k)}</th><td>${d ? d.n : "no data"}</td><td>${d?.best_snr ?? ""}</td>
        <td>${d?.max_km ?? ""}</td><td>${d ? (d.regions || []).map(([r, n]) => `${esc(regionName(r))} ${n}`).join(", ") : ""}</td>
        ${snap.pskreporter ? `<td>${y ? `${y.receivers} (${(y.countries || []).map(([c, n]) => `${esc(c)} ${n}`).join(", ")})` : ""}</td>` : ""}</tr>`;
    }).join("");
    return `<table class="prop-table"><thead><tr><th scope="col">Band</th><th scope="col">Decodes</th><th scope="col">Best SNR (dB)</th>
      <th scope="col">Farthest (km)</th><th scope="col">Regions</th>${snap.pskreporter ? `<th scope="col">Heard you (stations)</th>` : ""}</tr></thead>
      <tbody>${rows}</tbody></table>`;
  }

  // ------------------------------------------------ the band buttons
  function spaceLine() {
    if (!snap.swpc) return "";
    const parts = [];
    const k = kpRead(snap.kp?.kp), x = xrayRead(snap.xray?.class);
    if (snap.kp) {
      parts.push(`<span class="prop-sw st-${k.st}" title="Planetary K index (NOAA), ${esc(ago(snap.kp.ts))}${snap.kp_stale ? ": STALE" : ""}. ${esc(k.say)}">`
        + `Kp ${snap.kp.kp}${snap.kp_stale ? "?" : ""} <small>${esc(k.word.toLowerCase())}</small></span>`);
    }
    if (snap.xray) {
      parts.push(`<span class="prop-sw st-${x.st}" title="GOES X-ray flux, ${esc(ago(snap.xray.ts))}. ${esc(x.say)}">`
        + `X-ray ${esc(snap.xray.class)}</span>`);
    }
    if (snap.f107) {
      parts.push(`<span class="prop-sw" title="10.7 cm solar flux, ${esc(ago(snap.f107.ts))}. ${esc(sfiRead(snap.f107.sfi).say)}">SFI ${snap.f107.sfi}</span>`);
    }
    if (!parts.length) parts.push(`<span class="prop-sw dim">${esc(snap.space_error || "space weather loading…")}</span>`);
    else if (snap.space_error) parts.push(`<span class="prop-sw dim" title="Showing the last values NOAA gave">${esc(snap.space_error)}</span>`);
    return parts.join("");
  }

  function badgeBands() {
    const chips = document.getElementById("bands");
    if (!chips) return;
    const bands = snap.skywatch && !snap.heard_error ? snap.bands || {} : null;
    for (const c of chips.querySelectorAll("button[data-v]")) {
      const b = BANDS.find((x) => x.n === c.dataset.v);
      let i = c.querySelector(".prop-badge");
      const d = bands && b ? bands[key(b)] : null;
      const y = b && snap.pskreporter ? (snap.psk || {})[key(b)] : null;
      const you = y ? `\nheard you: ${y.receivers} station${y.receivers === 1 ? "" : "s"}` +
        (y.max_km != null ? `, farthest ${y.max_km} km` : "") : "";
      if (!d) {
        i?.remove();
        c.classList.remove("prop-heard");
        c.title = bands && b && b.hi <= 54e6
          ? `${key(b)}: no decodes in the last ${snap.window_min} min (may not have been monitored)` + you : you.trim();
        continue;
      }
      if (!i) { i = document.createElement("i"); i.className = "prop-badge"; c.append(i); }
      i.className = `prop-badge l${level(d.n)}`;
      i.textContent = fmtN(d.n);
      c.classList.add("prop-heard");
      const rs = (d.regions || []).slice(0, 4).map(([r, n]) => `${regionName(r)} ${n}`).join(", ");
      c.title = `${key(b)}: ${d.n} decodes in the last ${snap.window_min} min` +
        (d.best_snr != null ? `, best ${d.best_snr} dB` : "") + (d.max_km != null ? `, farthest ${d.max_km} km` : "") +
        (rs ? `\n${rs}` : "") + you;
    }
  }

  function strip() {
    const panel = document.querySelector(".panel.bandmode");
    const chips = document.getElementById("bands");
    if (!panel || !chips) return;
    let s = panel.querySelector(".prop-strip");
    if (!s) {
      s = document.createElement("div");
      s.className = "prop-strip";
      chips.after(s);
    }
    let heard = "";
    if (snap.skywatch) {
      heard = snap.heard_error
        ? `<span class="prop-sw dim">${esc(snap.heard_error)}: band activity unknown</span>`
        : `<a class="prop-sw dim" href="#/propagation" title="Decodes heard per band in the last ${snap.window_min} minutes. A band without a badge had no decodes: usually nobody was listening there.">bands, last ${snap.window_min} min →</a>`;
    }
    if (snap.pskreporter) {
      const top = Object.entries(snap.psk || {}).sort((a, b) => b[1].receivers - a[1].receivers).slice(0, 2);
      heard += snap.psk_error
        ? `<span class="prop-sw dim">${esc(snap.psk_error)}</span>`
        : top.length
          ? `<a class="prop-sw dim" href="#/propagation" title="Stations that reported ${esc(snap.pskreporter)} to PSKReporter in the last ${snap.window_min} minutes">heard you: ${top.map(([b, y]) => `${esc(b)} ${y.receivers}`).join(", ")}</a>`
          : `<span class="prop-sw dim" title="No PSKReporter reports of ${esc(snap.pskreporter)} in the last ${snap.window_min} minutes (have you transmitted?)">heard you: none</span>`;
    }
    s.innerHTML = spaceLine() + heard;
  }

  function unbadge() {
    document.querySelectorAll("#bands .prop-badge").forEach((i) => i.remove());
    document.querySelectorAll("#bands .prop-heard").forEach((c) => c.classList.remove("prop-heard"));
    document.querySelector(".panel.bandmode .prop-strip")?.remove();
  }

  function watchBands() {
    const chips = document.getElementById("bands");
    if (!chips || observer) return;
    // New band buttons (a radio change): badge them again. The tuned band
    // moved (a button's class): mark it on the tab.
    observer = new MutationObserver((ms) => {
      if (ms.some((m) => m.type === "childList" && [...m.addedNodes].some((n) => n.dataset && n.dataset.v))) badgeBands();
      if (ms.some((m) => m.type === "attributes")) render();
    });
    observer.observe(chips, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
  }

  // ------------------------------------------------ hover tips
  function tips(root) {
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "pg-tip";
      tip.hidden = true;
      document.body.append(tip);
    }
    root.addEventListener("mousemove", (e) => {
      const t = e.target.closest("[data-tip]");
      if (!t || !root.contains(t)) { tip.hidden = true; return; }
      tip.textContent = t.dataset.tip;
      tip.hidden = false;
      const x = Math.min(window.innerWidth - tip.offsetWidth - 8, e.clientX + 14);
      tip.style.left = `${x}px`;
      tip.style.top = `${e.clientY + 16}px`;
    });
    root.addEventListener("mouseleave", () => { tip.hidden = true; });
  }

  // ------------------------------------------------ the tab
  let lastHtml = "";
  function render() {
    if (!body) return;
    const html = conditions() + openingsSection() + bandsSection();
    if (html === lastHtml) return;          // the tuned-band observer fires often
    lastHtml = html;
    body.innerHTML = `<div class="pg">${html}</div>`;
    body.querySelector(".pg-tbl")?.addEventListener("click", () => { showTable = !showTable; lastHtml = ""; render(); });
  }

  function update() { render(); badgeBands(); strip(); watchBands(); }

  addons.register("propagation", {
    mount(b) { body = b; tips(b); update(); },
    hello(s) { snap = s || {}; update(); },
    event(m) { if (m.state) { snap = m.state; update(); } },
    off() { snap = {}; lastHtml = ""; if (body) body.innerHTML = ""; unbadge(); },
    tab() {
      const k = kpRead(snap.kp?.kp);
      return { dot: !k ? "off" : k.st === "good" ? "ok" : "tx",
               title: snap.kp ? `Kp ${snap.kp.kp} (${k.word})${snap.xray ? `, X-ray ${snap.xray.class}` : ""}` : "Space weather and band activity" };
    },
    card() {
      const lines = [];
      const k = kpRead(snap.kp?.kp);
      if (snap.kp) lines.push(`Kp ${snap.kp.kp} ${esc(k.word.toLowerCase())}${snap.xray ? ` · X-ray ${esc(snap.xray.class)}` : ""}`);
      const top = Object.entries(snap.bands || {}).sort((a, b) => b[1].n - a[1].n).slice(0, 3);
      if (top.length) lines.push(`busiest: ${top.map(([b, d]) => `${esc(b)} ${fmtN(d.n)}`).join(", ")}`);
      const next = (snap.forecast || [])[0];
      if (next) lines.push(`next opening: ${esc(next.band)} ${esc(regionName(next.region))} ${next.start <= Date.now() / 1000 ? "now" : esc(hhmm(next.start))}`);
      return { state: snap.heard_error || (k ? k.word.toLowerCase() : "loading"), lines };
    },
  });
})();
