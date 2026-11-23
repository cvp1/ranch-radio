"use strict";

// The Roster tab: who JTDX is hearing now (the logbook sync's intake), each
// coloured by what the station still needs (logsync_needs: new DXCC, new on
// this band or in this mode, new grid, not yet confirmed, new call), with a
// LoTW marker, distance and bearing. Click a row to ask JTDX to answer that
// station: a Reply, exactly as a double-click in JTDX would be. Ranch Radio
// never enables transmit; JTDX's own double-click setting decides that.
//
// Server: addons/roster.py ({"roster": {band, mode, dial, rows, needs}} in
// the snapshot and in pushes). Uses common.js (esc, toast) and send().

(() => {
  let body = null, snap = {}, alerted = new Set();
  const opt = Object.assign({ cq: false, needs: false, alert: "off" },
    JSON.parse(localStorage.getItem("roster.opts") || "{}"));
  const NEED = {
    dxcc: ["New DXCC", "rs-dxcc"], dxcc_band: ["New band", "rs-band"], dxcc_mode: ["New mode", "rs-mode"],
    grid: ["New grid", "rs-grid"], grid_band: ["Grid on band", "rs-gridband"], grid_mode: ["Grid in mode", "rs-gridmode"],
    dxcc_unconfirmed: ["Unconfirmed", "rs-unconf"], call_band_mode: ["New call", "rs-newcall"],
  };
  const RANK = ["dxcc", "dxcc_band", "dxcc_mode", "grid", "grid_band", "grid_mode", "dxcc_unconfirmed", "call_band_mode"];
  const ALERTS = { off: -1, dxcc: 0, band: 1, grid: 5, any: 7 };   // indexes into RANK: "grid" takes grid_mode too

  // Strong needs on the decoding band (UX plan PR 3a, 2026-11-19): one filter
  // for this tab's Shack card and the band strip (via data()). A
  // row keeps the band and mode it arrived on and outlives a band change
  // (logsync_roster.py Roster.add), so only rows on the roster's current band
  // and mode count, and only fresh ones: KEEP_S there is 120 s, and a snapshot
  // ages here while JTDX is quiet. null while the needs index is unavailable
  // (still building, or no logbook): unknown, never zero.
  const STRONG = ["dxcc", "dxcc_band", "dxcc_mode", "grid", "grid_band", "grid_mode"];   // grid_mode: 2026-11-20
  const FRESH_S = 120;
  let snapAt = 0;
  function needCounts() {
    const R = snap.roster;
    if (!R || !R.needs) return null;
    const late = (Date.now() - snapAt) / 1000;
    const rows = (R.rows || []).filter((r) => r.band === R.band && r.mode === R.mode && (r.age || 0) + late <= FRESH_S);
    return { band: R.band, mode: R.mode, strong: rows.filter((r) => STRONG.includes(r.need)),
             unconfirmed: rows.filter((r) => r.need === "dxcc_unconfirmed").length,
             new_calls: rows.filter((r) => r.need === "call_band_mode").length };
  }
  function take(s) { snap = s; snapAt = Date.now(); }

  // A Reply to one decode: the only way this page asks JTDX to answer a
  // station. A row click here and a click in the Shack's Now row (UX
  // plan PR 3b, 2026-11-19, through window.roster) both come through it, so
  // the two send the same message and JTDX's outcome comes back the same way
  // (addons/roster.py's notice, a toast). It sends nothing else.
  function reply(key, call) { send({ t: "addon", addon: "roster", do: "reply", key, call }); }
  function tipFor(r) {
    return r.answer === "cq" ? `Click to answer ${r.call}'s CQ in JTDX`
      : r.answer === "line" ? `${r.call} isn't calling CQ: JTDX answers this only if its UDP reply filter accepts any message`
      : `Nothing fresh from ${r.call} to answer`;
  }
  window.roster = { reply };

  function save() { localStorage.setItem("roster.opts", JSON.stringify(opt)); }

  function beep() {
    try {
      const a = new (window.AudioContext || window.webkitAudioContext)(), o = a.createOscillator(), g = a.createGain();
      o.frequency.value = 880; g.gain.value = 0.08; o.connect(g); g.connect(a.destination);
      o.start(); o.stop(a.currentTime + 0.18);
    } catch (e) { /* no audio: the toast still shows */ }
  }

  function alertFor(rows) {
    const lim = ALERTS[opt.alert];
    if (lim < 0) return;
    for (const r of rows) {
      const k = `${r.call}|${r.band}|${r.mode}`;
      if (alerted.has(k) || r.need == null || RANK.indexOf(r.need) > lim) continue;
      alerted.add(k);
      toast(`Roster: ${r.call} (${r.entity || "?"}) — ${(NEED[r.need] || [r.need])[0]}`, "info");
      beep();
      if (window.Notification && Notification.permission === "granted") {
        try { new Notification(`${r.call}: ${(NEED[r.need] || [r.need])[0]}`, { body: r.message }); } catch (e) { /* ignore */ }
      }
    }
  }

  function render() {
    if (!body) return;
    if (snap.error) { body.innerHTML = `<div class="st-empty">${esc(snap.error)}</div>`; return; }
    const R = snap.roster;
    if (!R) { body.innerHTML = `<div class="st-empty">Waiting for JTDX…</div>`; return; }
    let rows = R.rows || [];
    if (opt.cq) rows = rows.filter((r) => r.cq || r.to_me);
    if (opt.needs) rows = rows.filter((r) => r.need || r.to_me);
    const n = R.needs;
    const head = `<div class="rs-bar stat">
        <span><b>${esc(R.band || "?")}</b> ${esc(R.mode || "")} ${R.dial ? (R.dial / 1e6).toFixed(3) : ""}</span>
        <span>${(R.rows || []).length} heard</span>
        ${n ? `<span title="worked / confirmed">DXCC ${n.dxcc_worked}/${n.dxcc_confirmed} · grids ${n.grids_worked}/${n.grids_confirmed}</span>` : `<span class="lb-bad">needs: building…</span>`}
        <label><input type="checkbox" class="rs-o rr-check" data-o="cq" ${opt.cq ? "checked" : ""}> CQ only</label>
        <label><input type="checkbox" class="rs-o rr-check" data-o="needs" ${opt.needs ? "checked" : ""}> needs only</label>
        <label>alert <select class="rs-alert rr-select">${Object.keys(ALERTS).map((k) => `<option${k === opt.alert ? " selected" : ""}>${k}</option>`).join("")}</select></label>
      </div>`;
    const tr = rows.map((r) => {
      const [label, cls] = NEED[r.need] || ["", "rs-none"];
      const tip = tipFor(r);
      return `<tr class="rs-row ${cls}${r.to_me ? " rs-me" : ""}${r.answer === "cq" ? "" : " rs-nocq"}" data-key="${esc(r.key)}" data-call="${esc(r.call)}" title="${esc(tip)}">
        <td class="rs-call">${esc(r.call)}${r.lotw ? ` <span class="rs-lotw" title="uses LoTW">L</span>` : ""}${window.callcard ? callcard.button(r) : ""}</td>
        <td>${r.cq ? `CQ${r.cq_word ? " " + esc(r.cq_word) : ""}` : r.to_me ? "<b>you</b>" : esc(r.to || "")}</td>
        <td class="rs-c-entity">${esc(r.entity)}</td><td class="rs-c-grid">${esc(r.grid)}</td>
        <td class="rs-num">${r.km != null ? `${r.km.toLocaleString()} km ${r.bearing}°` : ""}</td>
        <td class="rs-num">${r.snr}</td><td class="rs-num rs-c-df">${r.df}</td>
        <td class="rs-need">${esc(label)}</td><td class="rs-msg rs-c-msg">${esc(r.message)}</td><td class="rs-num rs-c-age">${r.age}s</td></tr>`;
    }).join("");
    // A click's Reply is easy to miss in a tooltip, and JTDX may refuse it
    // (2026-11-19, UX plan PR 0.3): say so on the page once there are rows.
    const hint = `<div class="rs-hint">Click a station to answer it in JTDX (it answers a CQ; other messages only if its settings allow).</div>`;
    body.innerHTML = head + (rows.length ? `${hint}<table class="rs-t"><tr><th>Call</th><th>To</th><th class="rs-c-entity">Entity</th><th class="rs-c-grid">Grid</th>
        <th>Distance</th><th>dB</th><th class="rs-c-df">DF</th><th>Need</th><th class="rs-c-msg">Message</th><th class="rs-c-age">Age</th></tr>${tr}</table>`
      : `<div class="st-empty">Nothing ${opt.cq || opt.needs ? "matching the filters " : ""}heard in the last two minutes.</div>`);
    for (const c of body.querySelectorAll(".rs-o")) c.onchange = () => { opt[c.dataset.o] = c.checked; save(); render(); };
    const al = body.querySelector(".rs-alert");
    al.onchange = () => {
      opt.alert = al.value; save();
      if (opt.alert !== "off" && window.Notification && Notification.permission === "default") Notification.requestPermission();
    };
    for (const row of body.querySelectorAll(".rs-row")) {
      row.onclick = (e) => {
        if (e.target.closest(".cc-info")) return;      // the call card's button is never a Reply (2026-11-19, PR 4b)
        reply(row.dataset.key, row.dataset.call);
      };
    }
    window.callcard?.wire(body);
  }

  addons.register("roster", {
    mount(b) { body = b; render(); },
    hello(s) { take(s || {}); render(); },
    event(m) {
      if (m.callcard) { window.callcard?.answer(m.callcard); return; }     // a call card's answer (static/callcard.js)
      if (m.roster) { take({ roster: m.roster }); alertFor(m.roster.rows || []); render(); }
    },
    off() { snap = {}; if (body) body.innerHTML = ""; },
    // UX plan 5.3: the snapshot plus the strong-needs counts (null: unknown),
    // and the first five of those rows for the Shack's Now row (PR 3b),
    // in the server's order (to you, then need, then signal), each with its
    // need's name and the click's tooltip as this tab gives them.
    data() {
      const c = needCounts();
      return { ...snap, need_counts: c && { ...c, strong: c.strong.length,
        now: c.strong.slice(0, 5).map((r) => ({ key: r.key, call: r.call, need: r.need, label: (NEED[r.need] || [r.need])[0],
          entity: r.entity, snr: r.snr, to_me: r.to_me, tip: tipFor(r) })) } };
    },
    tab() {
      const rows = snap.roster?.rows || [];
      const top = rows.find((r) => r.need === "dxcc");
      return { dot: !snap.roster ? "off" : top ? "tx" : "ok", title: top ? `${top.call}: new DXCC` : `${rows.length} heard` };
    },
    card() {
      // The band strip's filter (PR 3a). It was a count of every row with
      // any need, which counted rows kept from the previous band and the
      // weak levels (unconfirmed, new call) alike.
      if (!snap.roster) return { state: "off", lines: [] };
      const c = needCounts();
      if (!c) return { state: "needs unknown", lines: ["the needs index is not ready"] };
      // The tag names what it counts (2026-11-23, was "N strong needs").
      return { state: `${c.strong.length} new DXCC or grid`, lines: c.strong.slice(0, 3).map((r) => `${esc(r.call)} — ${esc((NEED[r.need] || [r.need])[0])}`) };
    },
  });
})();
