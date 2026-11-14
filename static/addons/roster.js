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
    grid: ["New grid", "rs-grid"], grid_band: ["Grid on band", "rs-gridband"],
    dxcc_unconfirmed: ["Unconfirmed", "rs-unconf"], call_band_mode: ["New call", "rs-newcall"],
  };
  const RANK = ["dxcc", "dxcc_band", "dxcc_mode", "grid", "grid_band", "dxcc_unconfirmed", "call_band_mode"];
  const ALERTS = { off: -1, dxcc: 0, band: 1, grid: 4, any: 6 };

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
        <label><input type="checkbox" class="rs-o" data-o="cq" ${opt.cq ? "checked" : ""}> CQ only</label>
        <label><input type="checkbox" class="rs-o" data-o="needs" ${opt.needs ? "checked" : ""}> needs only</label>
        <label>alert <select class="rs-alert">${Object.keys(ALERTS).map((k) => `<option${k === opt.alert ? " selected" : ""}>${k}</option>`).join("")}</select></label>
      </div>`;
    const tr = rows.map((r) => {
      const [label, cls] = NEED[r.need] || ["", "rs-none"];
      const tip = r.answer === "cq" ? `Click to answer ${r.call}'s CQ in JTDX`
        : r.answer === "line" ? `${r.call} isn't calling CQ: JTDX answers this only if its UDP reply filter accepts any message`
        : `Nothing fresh from ${r.call} to answer`;
      return `<tr class="rs-row ${cls}${r.to_me ? " rs-me" : ""}${r.answer === "cq" ? "" : " rs-nocq"}" data-key="${esc(r.key)}" data-call="${esc(r.call)}" title="${esc(tip)}">
        <td class="rs-call">${esc(r.call)}${r.lotw ? ` <span class="rs-lotw" title="uses LoTW">L</span>` : ""}</td>
        <td>${r.cq ? `CQ${r.cq_word ? " " + esc(r.cq_word) : ""}` : r.to_me ? "<b>you</b>" : esc(r.to || "")}</td>
        <td>${esc(r.entity)}</td><td>${esc(r.grid)}</td>
        <td class="rs-num">${r.km != null ? `${r.km.toLocaleString()} km ${r.bearing}°` : ""}</td>
        <td class="rs-num">${r.snr}</td><td class="rs-num">${r.df}</td>
        <td class="rs-need">${esc(label)}</td><td class="rs-msg">${esc(r.message)}</td><td class="rs-num">${r.age}s</td></tr>`;
    }).join("");
    body.innerHTML = head + (rows.length ? `<table class="rs-t"><tr><th>Call</th><th>To</th><th>Entity</th><th>Grid</th>
        <th>Distance</th><th>dB</th><th>DF</th><th>Need</th><th>Message</th><th>Age</th></tr>${tr}</table>`
      : `<div class="st-empty">Nothing ${opt.cq || opt.needs ? "matching the filters " : ""}heard in the last two minutes.</div>`);
    for (const c of body.querySelectorAll(".rs-o")) c.onchange = () => { opt[c.dataset.o] = c.checked; save(); render(); };
    const al = body.querySelector(".rs-alert");
    al.onchange = () => {
      opt.alert = al.value; save();
      if (opt.alert !== "off" && window.Notification && Notification.permission === "default") Notification.requestPermission();
    };
    for (const row of body.querySelectorAll(".rs-row")) {
      row.onclick = () => send({ t: "addon", addon: "roster", do: "reply", key: row.dataset.key, call: row.dataset.call });
    }
  }

  addons.register("roster", {
    mount(b) { body = b; render(); },
    hello(s) { snap = s || {}; render(); },
    event(m) { if (m.roster) { snap = { roster: m.roster }; alertFor(m.roster.rows || []); render(); } },
    off() { snap = {}; if (body) body.innerHTML = ""; },
    tab() {
      const rows = snap.roster?.rows || [];
      const top = rows.find((r) => r.need === "dxcc");
      return { dot: !snap.roster ? "off" : top ? "tx" : "ok", title: top ? `${top.call}: new DXCC` : `${rows.length} heard` };
    },
    card() {
      const rows = (snap.roster?.rows || []).filter((r) => r.need);
      return { state: snap.roster ? `${rows.length} needed` : "off", lines: rows.slice(0, 3).map((r) => `${esc(r.call)} — ${esc((NEED[r.need] || [r.need])[0])}`) };
    },
  });
})();
