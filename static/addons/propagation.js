"use strict";

// Propagation add-on (addons/propagation.py). Two places:
//  - beside the radio page's band buttons: a heat badge on each band heard in
//    the last hour, and a one-line space weather strip under them;
//  - its own tab: space weather tiles and a per-band table.
// A band with no decodes says "no data", never "closed": a receiver only
// hears the band it is tuned to.

(() => {
  let snap = {}, body = null, observer = null;

  const REGION = { JA: "Japan", VK_ZL: "VK/ZL", EU: "Europe", SA: "S America", NA_EAST: "NA east",
                   NA_WEST: "NA west", OCEANIA_DEEP: "Pacific", AF: "Africa", AS: "Asia" };
  const SOURCE = { jtdx: "JTDX", wsjtx: "WSJT-X", skimmer: "skimmer", js8: "JS8Call" };
  const HF = BANDS.filter((b) => b.hi <= 54e6);
  const key = (b) => (b.label || b.n + "m");

  const fmtN = (n) => (n >= 10000 ? `${Math.round(n / 1000)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
  const level = (n) => (n >= 500 ? 4 : n >= 100 ? 3 : n >= 10 ? 2 : n > 0 ? 1 : 0);
  const region = (r) => REGION[r] || r.replace(/_/g, " ");
  const ago = (ts) => {
    if (!ts) return "";
    const s = Math.max(0, Date.now() / 1000 - ts);
    return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
  };
  const kpClass = (kp) => (kp == null ? "" : kp >= 5 ? "bad" : kp >= 4 ? "warn" : "ok");
  const xrClass = (c) => (!c ? "" : c[0] === "X" ? "bad" : c[0] === "M" ? "warn" : "ok");

  function spaceLine() {
    if (!snap.swpc) return "";
    const parts = [];
    if (snap.kp) {
      parts.push(`<span class="prop-sw ${kpClass(snap.kp.kp)}" title="Planetary K index (NOAA, 1-minute estimate), ${esc(ago(snap.kp.ts))}${snap.kp_stale ? ": STALE" : ""}">`
        + `Kp ${snap.kp.kp}${snap.kp_stale ? "?" : ""} <small>${esc(snap.kp.label)}</small></span>`);
    }
    if (snap.xray) {
      parts.push(`<span class="prop-sw ${xrClass(snap.xray.class)}" title="GOES X-ray flux, 0.1-0.8 nm: flare class, ${esc(ago(snap.xray.ts))}">`
        + `X-ray ${esc(snap.xray.class)}</span>`);
    }
    if (snap.f107) {
      parts.push(`<span class="prop-sw" title="10.7 cm solar flux (Penticton), ${esc(ago(snap.f107.ts))}">SFI ${snap.f107.sfi}</span>`);
    }
    if (!parts.length) parts.push(`<span class="prop-sw dim">${esc(snap.space_error || "space weather loading…")}</span>`);
    else if (snap.space_error) parts.push(`<span class="prop-sw dim" title="Showing the last values NOAA gave">${esc(snap.space_error)}</span>`);
    return parts.join("");
  }

  // ------------------------------------------------ the band buttons
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
      const rs = (d.regions || []).map(([r, n]) => `${region(r)} ${n}`).join(", ");
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
        : `<a class="prop-sw dim" href="#/propagation" title="Decodes heard per band in the last ${snap.window_min} minutes. A band without a badge had no decodes: usually nobody was listening there.">heard, last ${snap.window_min} min →</a>`;
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
    // The page rebuilds its band buttons when the radio changes: badge them again.
    observer = new MutationObserver((ms) => {
      if (ms.some((m) => [...m.addedNodes].some((n) => n.dataset && n.dataset.v))) badgeBands();
    });
    observer.observe(chips, { childList: true });
  }

  // ------------------------------------------------ the tab
  function render() {
    if (!body) return;
    const bands = snap.bands || {};
    const max = Math.max(1, ...Object.values(bands).map((d) => d.n));
    let table = "";
    const psk = snap.pskreporter && !snap.psk_error ? snap.psk || {} : null;
    const youCell = (b) => {
      if (!snap.pskreporter) return "";
      if (snap.psk_error) return `<td class="prop-you dim">unknown</td>`;
      const y = psk[key(b)];
      if (!y) return `<td class="prop-you dim">—</td>`;
      const cs = (y.countries || []).map(([c, n]) => `${esc(c)} <small>${n}</small>`).join(" · ");
      return `<td class="prop-you"><b>${y.receivers}</b> <small>stn</small>${y.max_km != null ? ` · ${y.max_km.toLocaleString()} km` : ""}${y.best_snr != null ? ` · ${y.best_snr} dB` : ""}<div>${cs}</div></td>`;
    };
    if (!snap.skywatch && !snap.pskreporter) {
      table = `<div class="st-empty">No decode source is set. Name a skywatch InfluxDB in the host file's
        propagation block ("skywatch": {"url": …, "db": "skywatch"}) to see what this station hears per band.</div>`;
    } else {
      const rows = HF.map((b) => {
        const d = snap.skywatch && !snap.heard_error ? bands[key(b)] : null;
        if (!d) {
          const why = !snap.skywatch ? "" : snap.heard_error ? "unknown" : "no data <small>(no decodes; likely not monitored)</small>";
          const you = youCell(b);
          if (!why && (!you || you.includes("dim"))) return "";
          return `<tr class="prop-none${you && !you.includes("dim") ? " prop-you-only" : ""}"><th>${esc(key(b))}</th><td colspan="5">${why}</td>${you}</tr>`;
        }
        const pct = Math.max(3, Math.round((100 * Math.log10(1 + d.n)) / Math.log10(1 + max)));
        const rs = (d.regions || []).map(([r, n]) => `${esc(region(r))} <small>${n}</small>`).join(" · ");
        const src = Object.keys(d.sources || {}).map((s) => esc(SOURCE[s] || s)).join(", ");
        return `<tr><th>${esc(key(b))}</th>
          <td class="prop-bar"><i class="l${level(d.n)}" style="width:${pct}%"></i><span>${fmtN(d.n)}</span></td>
          <td>${d.best_snr != null ? `${d.best_snr} dB` : ""}</td>
          <td>${d.max_km != null ? `${d.max_km.toLocaleString()} km` : ""}</td>
          <td>${rs}</td><td class="prop-src">${src}</td>${youCell(b)}</tr>`;
      }).join("");
      table = `<table class="prop-table"><thead><tr><th>Band</th><th>Decodes</th><th>Best SNR</th><th>Farthest</th>
        <th>Regions heard</th><th>Heard by</th>${snap.pskreporter ? `<th>Heard you <small>(PSKReporter)</small></th>` : ""}</tr></thead><tbody>${rows}</tbody></table>
        <p class="prop-note">Last ${snap.window_min} minutes.
        ${snap.skywatch ? (snap.heard_error ? `${esc(snap.heard_error)}: what this station heard is unknown, not zero. ` : `Decodes updated ${esc(ago(snap.heard_checked))}. "No data" means no decodes: a receiver hears only the band it's tuned to, so that's usually an unmonitored band, not a closed one. `) : ""}
        ${snap.pskreporter ? (snap.psk_error ? `${esc(snap.psk_error)}. ` : `Stations reporting ${esc(snap.pskreporter)} to PSKReporter, checked ${esc(ago(snap.psk_checked))} (every 10 minutes).`) : ""}</p>`;
    }
    const tiles = !snap.swpc ? "" : `<div class="prop-tiles">
        <div class="prop-tile ${kpClass(snap.kp?.kp)}"><span class="label">K index</span>
          <b>${snap.kp ? snap.kp.kp : "—"}${snap.kp_stale ? "?" : ""}</b><small>${esc(snap.kp?.label || "")} ${snap.kp ? esc(ago(snap.kp.ts)) : ""}</small></div>
        <div class="prop-tile ${xrClass(snap.xray?.class)}"><span class="label">X-ray (flares)</span>
          <b>${esc(snap.xray?.class || "—")}</b><small>${snap.xray ? esc(ago(snap.xray.ts)) : ""}</small></div>
        <div class="prop-tile"><span class="label">Solar flux</span>
          <b>${snap.f107 ? snap.f107.sfi : "—"}</b><small>${snap.f107 ? esc(ago(snap.f107.ts)) : ""}</small></div>
      </div>${snap.space_error ? `<p class="prop-note">${esc(snap.space_error)}; showing the last values it gave.</p>` : ""}`;
    body.innerHTML = tiles + table;
  }

  function update() { render(); badgeBands(); strip(); watchBands(); }

  addons.register("propagation", {
    mount(b) { body = b; update(); },
    hello(s) { snap = s || {}; update(); },
    event(m) { if (m.state) { snap = m.state; update(); } },
    off() { snap = {}; if (body) body.innerHTML = ""; unbadge(); },
    tab() {
      const kp = snap.kp?.kp;
      return { dot: kp == null ? "off" : kp >= 5 ? "tx" : "ok",
               title: snap.kp ? `Kp ${kp} (${snap.kp.label})${snap.xray ? `, X-ray ${snap.xray.class}` : ""}` : "Space weather and band activity" };
    },
    card() {
      const lines = [];
      if (snap.kp) lines.push(`Kp ${snap.kp.kp} ${esc(snap.kp.label)}${snap.xray ? ` · X-ray ${esc(snap.xray.class)}` : ""}`);
      const top = Object.entries(snap.bands || {}).sort((a, b) => b[1].n - a[1].n).slice(0, 3);
      if (top.length) lines.push(`heard: ${top.map(([b, d]) => `${esc(b)} ${fmtN(d.n)}`).join(", ")}`);
      return { state: snap.heard_error || (snap.kp ? snap.kp.label : "loading"), lines };
    },
  });
})();
