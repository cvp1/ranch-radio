"use strict";

// The JS8 tab: what the station's JS8 engines hear, per radio, and chatting
// while armed. Rebuilt 2026-11-24 from a reading layout (three lists and a
// send form) into what JS8Call is for, a keyboard conversation, using the
// chat view the Mesh tabs share (chat.js). Top to bottom:
//
//   1 status card   what the radio is doing, in one word (Listening, TX
//                   armed, ON AIR, STOP TX, Engine down, UNSAFE), the
//                   frequency, your call and grid, the JS8 bands (a click
//                   retunes, USB data, through the station's padlock; not
//                   while armed); on the right, Transmit: 5/10/30 min and
//                   Arm TX, or the time left and Disarm; when it can't arm, why
//   2 waterfall     the engine's audio, 0-3000 Hz, newest on top. The rows
//                   arrive flattened (js8_bridge.Flatten: each bin in dB over
//                   its own noise, 0..60; -1 outside the radio's filter, drawn
//                   as a flat grey so the filter's edges show). Floor and
//                   Range (kept per browser) set what is black and what is
//                   white, and redraw the kept history. Every decode is
//                   boxed where it was on the air: its offset, its speed's
//                   width (Normal 50 Hz, Fast 80, Turbo 160, Slow 25) and its
//                   on-air time, labelled with the sender. A strip above
//                   names who has been on each offset lately. Hover reads the
//                   offset and the station under the cursor; your TX offset
//                   is a bracket as wide as your signal; dashed lines mark the
//                   JS8 cycles. A click on a box or a name opens that
//                   station; a click elsewhere puts that offset in Offset
//                   (sends nothing)
//   3 chat          chat.js's three columns. Rail: All stations, Talking with
//                   you (called you, or you called), Heard (newest first,
//                   with dB, grid and offset). The conversation: complete
//                   messages as bubbles; yours on the right with what
//                   happened to them (queued, on the air, sent, not sent),
//                   which only the radio server's word (radio_tx) moves;
//                   frames still arriving from that station as a
//                   "receiving" line. Composer under it: HB, CQ, the
//                   queries for the open station, offset and speed, and the
//                   text (Enter sends). Disabled, saying why, until this
//                   page arms. Side: Band activity, JS8Call's offset list
//
// Sent lines live in this page only (the engine does not decode itself):
// a reload shows what was heard, not what you sent.
//
// The arm is this page's: closing it disarms (js8_bridge.py holds every gate).
// What is on the air is the radio server's word (radio_tx), never the engine's.
//
// Server: addons/js8call.py relays js8_bridge.py: hello {ready, why?,
// radios: {id: {name, status, frames, heard}}}; events {event: "frame" |
// "status" | "spectrum", radio, ...}; the page sends api.send("arm", {radio,
// minutes}), ("send", {radio, text, offset_hz?, speed?}), ("hb", {radio}),
// ("disarm", {radio}). Text from the air goes through textContent or
// chat.js's esc(): JS8 text can hold < and >.

(() => {
  const BIN_HZ = 50, MAX_ROWS = 60, FRESH_MS = 10 * 60 * 1000, UNREAD_MS = 60 * 60 * 1000;
  const FRAME_CALL = /^\s*([A-Z0-9]{1,3}[0-9][A-Z0-9/]{0,8}):\s*/;
  const MAX_SEND = 160, ARM_MINUTES = [5, 10, 30];
  const QUERIES = [["SNR?", "How strong am I?"], ["GRID?", "Where are you?"], ["INFO?", "Your station info"],
                   ["STATUS?", "Your status"], ["HEARING?", "Who do you hear?"]];
  // JS8Call's own default dial frequencies (Hz), the ones stock stations sit on.
  const DIALS = [["160", 1_842_000], ["80", 3_578_000], ["40", 7_078_000], ["30", 10_130_000],
                 ["20", 14_078_000], ["17", 18_104_000], ["15", 21_078_000], ["12", 24_922_000],
                 ["10", 28_078_000], ["6", 50_318_000]];
  const SPEEDS = [["", "As set"], ["normal", "Normal"], ["fast", "Fast"], ["turbo", "Turbo"], ["slow", "Slow"]];
  // JS8's submodes: 8 tones (bandwidth), 79 symbols (on-air seconds), the cycle.
  // Normal 6.25 Hz x 8 = 50 Hz, 79 x 0.16 s = 12.64 s in a 15 s cycle; Fast
  // 10 Hz, 0.1 s, 10 s; Turbo 20 Hz, 0.05 s, 6 s; Slow 3.125 Hz, 0.32 s, 30 s.
  const SUB = { normal: { bw: 50, dur: 12.64, period: 15 }, fast: { bw: 80, dur: 7.9, period: 10 },
                turbo: { bw: 160, dur: 3.95, period: 6 }, slow: { bw: 25, dur: 25.28, period: 30 } };
  const sub = (name) => SUB[String(name || "").toLowerCase()] || SUB.normal;

  // ---------- waterfall geometry and colours
  const WF_TOP_HZ = 3000, WF_W = 600, WF_H = 240, LANE_MS = 5 * 60 * 1000, LANES = 3;
  const WF_KEY = "js8.wf2";
  // The radio page's waterfall colours (mission.js LUT): dark blue to white.
  const WF_LUT = (() => {
    const stops = [[0, [4, 6, 10]], [0.22, [10, 30, 72]], [0.42, [14, 96, 140]], [0.58, [32, 200, 180]],
      [0.74, [245, 215, 80]], [0.88, [255, 120, 60]], [1, [255, 250, 240]]];
    const lut = new Uint8ClampedArray(256 * 3);
    for (let i = 0; i < 256; i++) {
      const t = i / 255;
      let k = 1; while (k < stops.length - 1 && stops[k][0] < t) k++;
      const [t0, c0] = stops[k - 1], [t1, c1] = stops[k], u = (t - t0) / (t1 - t0);
      for (let j = 0; j < 3; j++) lut[i * 3 + j] = c0[j] + (c1[j] - c0[j]) * u;
    }
    return lut;
  })();
  // Floor: dB over the noise that is still black. Range: dB above Floor that
  // reaches white. Flattened, the band's noise sits at ~5 dB (90% under 12)
  // and JS8 traces at 15-35 (an IC-7300, 2026-11-24): 6 and 20 keep the noise
  // dark and put a 16 dB trace mid-map. Kept under a new key, so the 4/24
  // tried against the unflattened rows doesn't carry over.
  const WF_OFF = [14, 16, 22];                      // outside the radio's filter
  const wfCfg = (() => {
    const d = { floor: 6, range: 20 };
    try { return Object.assign(d, JSON.parse(localStorage.getItem(WF_KEY)) || {}); } catch { return d; }
  })();

  let body = null, api = null, snap = { ready: false }, pick = null, to = null, ticker = null;
  let armMins = 10, speed = "", ui = null, wf = null, txSeq = 0;
  const grids = {};                     // call -> grid, from messages that carried one
  const outbox = {};                    // radio -> [{key, t, to, text, sub, state, why, at}]
  const seen = {};                      // radio -> {call: ms that conversation was last open}

  const radios = () => snap.radios || {};
  const cur = () => radios()[pick] || null;
  const st = () => ((cur() || {}).status) || {};
  const myCall = () => String(st().callsign || "").toUpperCase();

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  const hhmmss = (utc) => (String(utc || "").match(/T(\d\d:\d\d:\d\d)/) || [, ""])[1];
  const snr = (v) => (typeof v === "number" ? (v > 0 ? `+${v}` : `${v}`) : "");
  const callOf = (f) => f.from || (String(f.text || "").match(FRAME_CALL) || [])[1] || null;
  const ms = (utc) => { const t = Date.parse(utc); return Number.isFinite(t) ? t : 0; };
  const ago = (t) => {
    const s = Math.max(0, (Date.now() - t) / 1000);
    return s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h`;
  };
  const markSeen = (call) => { if (call) (seen[pick] || (seen[pick] = {}))[call] = Date.now(); };

  // Addressed to this station: "to" is my call, or my call is a word in the text after the sender.
  function toMe(f) {
    const me = myCall();
    if (!me) return false;
    if (String(f.to || "").toUpperCase() === me) return true;
    const rest = String(f.text || "").toUpperCase().replace(FRAME_CALL, "");
    return rest.split(/[^A-Z0-9/]+/).includes(me);
  }

  // A message's words without "FROM:" and its "TO" (both are shown beside it).
  function bodyOf(m) {
    let t = String(m.text || "").replace(FRAME_CALL, "").trim();
    const dest = String(m.to || "").toUpperCase();
    if (dest && t.toUpperCase().startsWith(dest + " ")) t = t.slice(dest.length).trim();
    return t || String(m.text || "");
  }

  function left(s) {
    const n = Math.max(0, Math.round((Number(s.armed_until) || 0) - Date.now() / 1000));
    return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
  }

  // ---------- 1 status card

  // One word for what the radio is doing, and its pill colour.
  function state(s) {
    if (s.unsafe && s.unsafe.length) return ["UNSAFE", "bad"];
    if (!s.connected) return ["Engine down", "ghost"];
    if (s.armed && s.radio_tx) return ["ON AIR", "bad"];
    if (s.armed) return [`TX armed · ${left(s)}`, "wait"];
    if (/STOP TX/.test(s.why_not || "")) return ["STOP TX", "bad"];
    return ["Listening", "ok"];
  }

  function statusCard(r) {
    const s = (r && r.status) || {};
    const card = el("section", "panel js8-head");
    const [word, tone] = state(s);
    card.classList.toggle("onair", word === "ON AIR");
    card.classList.toggle("armed", !!s.armed && word !== "ON AIR");

    const info = el("div", "js8-info");
    const pill = el("span", `pill pill-${tone} js8-state`);
    pill.append(el("i"), word);
    info.appendChild(pill);
    if (s.unsafe && s.unsafe.length) {
      info.appendChild(el("div", "js8-status unsafe",
        `UNSAFE: the engine reports ${s.unsafe.join(", ")} on. It should start disarmed; stop it and check js8_engine.py.`));
    } else if (!s.connected) {
      info.appendChild(el("div", "js8-status down", `Engine not connected${s.error ? ": " + s.error : ""}`));
    } else {
      const f = typeof s.radio_freq_khz === "number" ? `${(s.radio_freq_khz / 1000).toFixed(3)} MHz` : "frequency unknown";
      const line = el("div", "js8-status");
      line.append(el("b", "js8-freq", f), el("span", null, ` ${s.radio_mode || ""} · ${s.submode_name || "Normal"}`));
      info.appendChild(line);
      const me = el("div", "js8-me");
      me.append(el("span", "label", "You"), el("b", null, s.callsign || "no call set"), el("span", null, s.grid || ""));
      info.appendChild(me);
      info.appendChild(bandRow(s));
    }
    card.appendChild(info);
    card.appendChild(armBlock(s));
    return card;
  }

  // The JS8 calling frequencies, one button each. A click retunes the radio
  // through the station's own path (api.tune: the padlock, the focus radio,
  // the radio server's refusals), USB with data on, as FT8's band cells do;
  // with auto-tune on, the radio server tunes the ATU on the new band (a
  // short carrier). Not while armed: the arm was given for the band it's on.
  function bandRow(s) {
    const row = el("div", "js8-bands");
    row.appendChild(el("span", "label", "Band"));
    const seg = el("div", "seg");
    const khz = typeof s.radio_freq_khz === "number" ? s.radio_freq_khz : null;
    const can = !!(api && api.canTune && api.canTune());
    for (const [band, f] of DIALS) {
      const on = khz !== null && Math.abs(khz * 1000 - f) < 1000;
      const c = el("button", "chip" + (on ? " on" : ""), `${band} m`);
      c.dataset.band = band;
      c.disabled = !!s.armed || !can;
      c.title = s.armed ? "Disarm first: a band change isn't made while JS8 is armed"
        : !can ? "No tuning from this page"
        : `${(f / 1e6).toFixed(3)} MHz USB, data on. With auto-tune on, the ATU tunes the new band: a short carrier.`;
      c.onclick = () => {
        if (!api || !pick || s.armed) return;
        Promise.resolve(api.tune({ band, freq: f, mode: "USB", data: true, radio: pick })).then((res) => {
          // The station says its own refusals (a toast, or the padlock); this
          // says only the ones it never sees.
          if (res && !res.ok && /not in the|no tuning|exact dials/.test(res.why || "") && typeof toast === "function") {
            toast(`Not retuned: ${res.why}`, "warn");
          }
        });
      };
      seg.appendChild(c);
    }
    row.appendChild(seg);
    return row;
  }

  function armBlock(s) {
    const box = el("div", "js8-armbox");
    box.appendChild(el("span", "label", "Transmit"));
    const row = el("div", "js8-armrow");
    if (s.armed) {
      row.appendChild(el("span", "js8-left", `${left(s)} left`));
      if (s.queue) row.appendChild(el("span", "js8-queue", `${s.queue} frame(s) queued`));
      const d = el("button", "chip js8-disarm", "Disarm");
      d.title = "Stop sending: halts anything queued and turns transmit off";
      d.onclick = () => api && pick && api.send("disarm", { radio: pick });
      row.appendChild(d);
    } else {
      const seg = el("div", "seg js8-mins");
      for (const m of ARM_MINUTES) {
        const c = el("button", "chip" + (m === armMins ? " on" : ""), `${m} min`);
        c.dataset.min = m;
        c.title = `Allow sending for ${m} minutes`;
        c.onclick = () => { armMins = m; refreshHead(); };
        seg.appendChild(c);
      }
      const a = el("button", "chip js8-arm", "Arm TX");
      a.disabled = !!s.why_not || !s.connected;
      a.title = a.disabled ? `Can't arm: ${s.why_not || "the engine isn't connected"}` :
        `Allow this page to transmit for ${armMins} minutes. Closing the page or STOP TX disarms.`;
      a.onclick = () => api && pick && api.send("arm", { radio: pick, minutes: armMins });
      row.append(seg, a);
    }
    box.appendChild(row);
    box.appendChild(el("div", "js8-why", !s.armed && s.why_not ? `Can't arm: ${s.why_not}` : ""));
    return box;
  }

  function refreshHead() {
    const head = ui && ui.root.querySelector(".js8-head");
    if (head && cur()) head.replaceWith(statusCard(cur()));
  }

  // ---------- 2 waterfall: built once and kept, so its history survives a re-render

  function wfPanel() {
    if (wf) return wf;
    const panel = el("section", "panel js8-wf");
    const head = el("div", "js8-panel-head js8-wf-head");
    head.append(el("h3", null, "Waterfall"),
                el("span", "js8-sub", "Each box is a decode, named for its sender: click to talk. Click a quiet spot to send there."));
    const ctl = el("div", "js8-wf-ctl");
    const slider = (name, label, min, max, tip) => {
      const w = el("label", "js8-wf-slider");
      w.title = tip;
      const i = el("input");
      i.type = "range"; i.min = min; i.max = max; i.step = 1; i.value = wfCfg[name];
      i.dataset.wf = name;
      const v = el("b", null, `${wfCfg[name]} dB`);
      i.oninput = () => {
        wfCfg[name] = Number(i.value);
        v.textContent = `${i.value} dB`;
        try { localStorage.setItem(WF_KEY, JSON.stringify(wfCfg)); } catch { /* private mode */ }
        wfRedraw();
      };
      w.append(el("span", "label", label), i, v);
      return w;
    };
    ctl.append(slider("floor", "Floor", 0, 20, "dB over the noise that is still black: raise it to hide the noise"),
               slider("range", "Range", 8, 60, "dB over the floor that reaches white: lower it to make weak signals bright"));
    head.appendChild(ctl);

    const lanes = el("div", "js8-wf-lanes");
    const wrap = el("div", "js8-wf-wrap");
    const canvas = el("canvas", "js8-wf-canvas");
    canvas.width = WF_W; canvas.height = WF_H;
    const grid = el("div", "js8-wf-grid");
    for (let hz = 500; hz < WF_TOP_HZ; hz += 500) {
      const g = el("i");
      g.style.left = `${hz / WF_TOP_HZ * 100}%`;
      grid.appendChild(g);
    }
    const marks = el("div", "js8-wf-marks");
    const tx = el("div", "js8-wf-tx");
    tx.appendChild(el("span", null, "TX"));
    const cursor = el("div", "js8-wf-cursor");
    const hover = el("div", "js8-wf-hover");
    cursor.hidden = hover.hidden = tx.hidden = true;
    const note = el("div", "js8-wf-note", "Waiting for the engine's audio");
    wrap.append(canvas, grid, marks, tx, cursor, hover, note);
    const scale = el("div", "js8-wf-scale");
    for (let hz = 100; hz < WF_TOP_HZ; hz += 100) {
      const major = hz % 500 === 0, tk = el("i", major ? "major" : null);
      tk.style.left = `${hz / WF_TOP_HZ * 100}%`;
      scale.appendChild(tk);
      if (!major) continue;
      const t = el("span", null, String(hz));
      t.style.left = tk.style.left;
      scale.appendChild(t);
    }
    panel.append(head, lanes, wrap, scale);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#04060a"; ctx.fillRect(0, 0, WF_W, WF_H);

    const at = (e) => {
      const r = wrap.getBoundingClientRect();
      return { hz: (e.clientX - r.left) / r.width * WF_TOP_HZ, y: (e.clientY - r.top) / r.height * WF_H,
               px: e.clientX - r.left, py: e.clientY - r.top, w: r.width };
    };
    const under = (p) => wf.boxes.find((b) => b.pos && p.hz >= b.hz - 8 && p.hz <= b.hz + b.bw + 8
      && p.y >= b.pos[0] - 2 && p.y <= b.pos[1] + 2);
    wrap.onmousemove = (e) => {
      const p = at(e), b = under(p);
      cursor.hidden = hover.hidden = false;
      cursor.style.left = `${p.hz / WF_TOP_HZ * 100}%`;
      cursor.style.width = `${sub(speed || st().submode_name).bw / WF_TOP_HZ * 100}%`;
      hover.textContent = `${Math.round(p.hz / 10) * 10} Hz` + (b ? ` · ${b.call || "?"} ${snr(b.snr)} dB · ${ago(b.t1)} ago` : "");
      hover.style.left = `${Math.max(0, Math.min(p.px + 12, p.w - 190))}px`;
      hover.style.top = `${Math.max(0, p.py - 26)}px`;
      wrap.classList.toggle("over-box", !!b);
    };
    wrap.onmouseleave = () => { cursor.hidden = hover.hidden = true; };
    wrap.onclick = (e) => {
      const p = at(e), b = under(p);
      if (b && b.call) return select(b.call);
      setOffset(Math.round(p.hz / 10) * 10);
    };
    wf = { panel, canvas, ctx, lanes, marks, tx, note, rows: [], total: 0, radio: null, at: 0, boxes: [], cycle: 0 };
    return wf;
  }

  // One row of dB-over-floor bins (10 Hz each) as WF_W pixels, through Floor and Range.
  function paintRow(img, y, r) {
    const n = r.row.length, lo = wfCfg.floor, span = Math.max(1, wfCfg.range);
    for (let x = 0; x < WF_W; x++) {
      const v = Number(r.row[Math.min(n - 1, Math.floor(x * n / WF_W))]) || 0;   // nearest bin: a one-bin tone keeps its strength
      const k = (Math.max(0, Math.min(1, (v - lo) / span)) * 255) | 0;
      const o = (y * WF_W + x) * 4;
      let R = WF_LUT[k * 3], G = WF_LUT[k * 3 + 1], B = WF_LUT[k * 3 + 2];
      if (v < 0) [R, G, B] = WF_OFF;
      if (r.tick && x % 6 < 3) { R = Math.max(R, 70); G = Math.max(G, 80); B = Math.max(B, 100); }   // a cycle line
      img.data[o] = R; img.data[o + 1] = G; img.data[o + 2] = B; img.data[o + 3] = 255;
    }
  }

  function wfRedraw() {
    const w = wfPanel();
    const img = w.ctx.createImageData(WF_W, WF_H);
    for (let y = 0; y < WF_H; y++) {
      if (w.rows[y]) { paintRow(img, y, w.rows[y]); continue; }
      for (let x = 0; x < WF_W; x++) {
        const o = (y * WF_W + x) * 4;
        img.data[o] = 4; img.data[o + 1] = 6; img.data[o + 2] = 10; img.data[o + 3] = 255;
      }
    }
    w.ctx.putImageData(img, 0, 0);
  }

  function wfRow(radio, row) {
    const w = wfPanel();
    if (w.radio !== radio) {                 // another radio picked: its own history
      w.radio = radio; w.rows = []; w.total = 0;
      for (const b of w.boxes) b.el && b.el.remove();
      w.boxes = [];
      w.ctx.fillStyle = "#04060a"; w.ctx.fillRect(0, 0, WF_W, WF_H);
    }
    const now = Date.now(), cycle = Math.floor(now / (sub(st().submode_name).period * 1000));
    const r = { t: now, row: row.slice(0, WF_TOP_HZ / 10), tick: w.cycle !== 0 && cycle !== w.cycle };
    w.cycle = cycle;
    w.rows.unshift(r);
    if (w.rows.length > WF_H) w.rows.length = WF_H;
    w.ctx.drawImage(w.canvas, 0, 0, WF_W, WF_H - 1, 0, 1, WF_W, WF_H - 1);
    const img = w.ctx.createImageData(WF_W, 1);
    paintRow(img, 0, r);
    w.ctx.putImageData(img, 0, 0);
    w.total++; w.at = now;
    w.canvas.dataset.rows = w.total;
    w.note.hidden = true;
    wfPlace();
  }

  // Rows (from the top) newer than time t: where t is on the canvas.
  function yOf(t) {
    const rows = wf.rows;
    if (!rows.length || t >= rows[0].t) return 0;
    if (t < rows[rows.length - 1].t) return rows.length < WF_H ? rows.length : WF_H;
    let lo = 0, hi = rows.length - 1;           // newest first: the first row not newer than t
    while (lo < hi) { const m = (lo + hi) >> 1; if (rows[m].t <= t) hi = m; else lo = m + 1; }
    return lo;
  }

  // A decode, boxed where it was on the air. Its utc is when it went on
  // the air (js8_bridge: jf8call's on-air stamp, #157), and a decode lands
  // a few seconds after the signal ends; if the two disagree by more than
  // 15 s (this browser's clock is off), it is placed as ending 2 s ago.
  function wfBox(radio, f, call) {
    const w = wfPanel();
    if (typeof f.freq_hz !== "number") return;
    if (w.radio === null) w.radio = radio;
    if (radio !== w.radio) return;
    const s = sub(f.submode), now = Date.now();
    let t0 = ms(f.utc);
    if (!t0 || Math.abs(now - (t0 + s.dur * 1000)) > 15_000) t0 = now - (s.dur + 2) * 1000;
    const t1 = t0 + s.dur * 1000;
    const bin = Math.round(f.freq_hz / BIN_HZ);
    if (w.boxes.some((b) => b.bin === bin && Math.abs(b.t0 - t0) < 2000)) return;   // one transmission, told twice
    w.boxes.push({ call, hz: f.freq_hz, bw: s.bw, bin, t0, t1, snr: f.snr, me: toMe(f), el: null, pos: null });
    while (w.boxes.length > 80) { const b = w.boxes.shift(); b.el && b.el.remove(); }
    wfPlace();
  }

  // Put each box at its rows; drop the ones scrolled off. Positions are
  // percentages, so the canvas can be any height on the page.
  function wfPlace() {
    const w = wfPanel();
    const oldest = w.rows.length ? w.rows[w.rows.length - 1].t : -Infinity;
    w.boxes = w.boxes.filter((b) => {
      if (w.rows.length >= WF_H && b.t1 < oldest) { b.el && b.el.remove(); return false; }
      return true;
    });
    for (const b of w.boxes) {
      if (!b.el) {
        b.el = el("div", "js8-wf-box");
        const lab = el("button", "js8-wf-call", b.call || "?");
        lab.type = "button";
        if (b.call) {
          lab.dataset.call = b.call;
          lab.title = `${b.call}, ${snr(b.snr)} dB at ${Math.round(b.hz)} Hz: click to talk`;
          lab.onclick = (e) => { e.stopPropagation(); select(b.call); };
        }
        b.el.appendChild(lab);
        w.marks.appendChild(b.el);
      }
      const top = yOf(b.t1), bot = Math.max(top + 4, yOf(b.t0));
      b.pos = w.rows.length ? [top, bot] : null;
      b.el.hidden = !b.pos;
      b.el.classList.toggle("sel", !!b.call && b.call === to);
      b.el.classList.toggle("me", b.me);
      b.el.classList.toggle("top", top < 16);         // no room above it: the name goes beside
      b.el.style.left = `${b.hz / WF_TOP_HZ * 100}%`;
      b.el.style.width = `${b.bw / WF_TOP_HZ * 100}%`;
      b.el.style.top = `${top / WF_H * 100}%`;
      b.el.style.height = `${(Math.min(bot, WF_H) - top) / WF_H * 100}%`;
    }
  }

  // The strip above: who has been on each offset in the last LANE_MS,
  // stacked into LANES rows so names never sit on each other. And the TX bracket.
  function wfMarks() {
    const w = wfPanel(), r = cur();
    const now = Date.now();
    const width = w.lanes.getBoundingClientRect().width || 1000;
    const ends = new Array(LANES).fill(-Infinity);
    const talk = talking();
    const hs = ((r && r.heard) || []).filter((h) => typeof h.freq_hz === "number" && h.freq_hz >= 0
      && h.freq_hz <= WF_TOP_HZ && now - ms(h.utc) <= LANE_MS).sort((a, b) => a.freq_hz - b.freq_hz);
    w.lanes.replaceChildren();
    let used = 1;
    for (const h of hs) {
      const x = h.freq_hz / WF_TOP_HZ * width, cw = String(h.call).length * 7.5 + 14;
      let lane = ends.findIndex((e) => e < x - cw / 2 - 2);
      if (lane < 0) lane = ends.indexOf(Math.min(...ends));
      ends[lane] = x + cw / 2;
      used = Math.max(used, lane + 1);
      const b = el("button", "js8-wf-lane" + (h.call === to ? " sel" : "") + (talk.has(h.call) ? " talk" : ""), h.call);
      b.type = "button";
      b.style.left = `${h.freq_hz / WF_TOP_HZ * 100}%`;
      b.style.top = `${lane * 20}px`;
      b.style.opacity = String(Math.max(0.45, 1 - (now - ms(h.utc)) / LANE_MS));
      b.dataset.call = h.call;
      b.title = `${h.call} at ${Math.round(h.freq_hz)} Hz, ${snr(h.snr)} dB, ${ago(ms(h.utc))} ago: click to talk`;
      b.onclick = () => select(h.call);
      w.lanes.appendChild(b);
    }
    w.lanes.style.height = `${hs.length ? used * 20 + 2 : 0}px`;
    const off = ui && ui.offset.value !== "" ? Number(ui.offset.value) : NaN;
    w.tx.hidden = !Number.isFinite(off);
    if (Number.isFinite(off)) {
      w.tx.style.left = `${off / WF_TOP_HZ * 100}%`;
      w.tx.style.width = `${sub(speed || st().submode_name).bw / WF_TOP_HZ * 100}%`;
      w.tx.classList.toggle("armed", !!st().armed);
      w.tx.title = `You send at ${off} Hz`;
    }
    if (w.at && now - w.at > 5000) { w.note.hidden = false; w.note.textContent = "No audio from the engine in 5 s"; }
    wfPlace();
  }

  function setOffset(hz) {
    if (!ui) return;
    ui.offset.value = Math.max(400, Math.min(2600, hz));
    wfMarks();
  }

  // ---------- 3 chat: the conversations

  const outs = () => outbox[pick] || (outbox[pick] = []);
  const msgs = (r) => ((r && r.frames) || []).filter((f) => f.kind === "message" && f.from);

  // Stations you're in a conversation with: they addressed you, or you them.
  function talking() {
    const s = new Set();
    for (const m of msgs(cur())) if (toMe(m)) s.add(m.from);
    for (const o of outs()) if (o.to) s.add(o.to);
    return s;
  }

  function unread(call) {
    const since = ((seen[pick] || {})[call]) || 0, now = Date.now();
    return msgs(cur()).filter((m) => m.from === call && toMe(m) && ms(m.utc) > since && now - ms(m.utc) < UNREAD_MS).length;
  }

  function select(call) {
    to = call || null;
    markSeen(to);
    render();
    if (ui && to && !ui.text.disabled) ui.text.focus({ preventScroll: true });
  }

  function railRows(r) {
    const talk = talking();
    const lastFrom = new Map(), lastTo = new Map(), calls = new Map();
    for (const m of msgs(r)) lastFrom.set(m.from, m);
    for (const o of outs()) if (o.to) lastTo.set(o.to, o);
    for (const h of (r.heard || []).slice(0, 200)) calls.set(h.call, h);
    for (const c of talk) if (!calls.has(c)) calls.set(c, { call: c });
    const row = (h) => {
      const m = lastFrom.get(h.call), o = lastTo.get(h.call);
      const mt = m ? ms(m.utc) / 1000 : 0, ot = o ? o.t : 0, ht = h.utc ? ms(h.utc) / 1000 : 0;
      const stats = [snr(h.snr) && `${snr(h.snr)} dB`, h.grid || grids[h.call], typeof h.freq_hz === "number" && `${Math.round(h.freq_hz)} Hz`]
        .filter(Boolean).join(" · ");
      // Talking: the last thing said. Heard: how they come in, JS8Call's Call Activity.
      const last = talk.has(h.call)
        ? (ot > mt ? { who: "You", text: o.text, t: ot } : m ? { who: "", text: bodyOf(m), t: mt } : { who: "", text: stats, t: ht })
        : { who: "", text: stats || (m ? bodyOf(m) : ""), t: Math.max(ht, mt) };
      return { id: h.call, title: h.call, seed: h.call, last: last.t ? last : null, unread: unread(h.call), on: to === h.call,
               hint: `${h.call}${stats ? ": " + stats : ""}`, sort: Math.max(mt, ot, ht) };
    };
    const all = [...calls.values()].map(row);
    const tk = all.filter((x) => talk.has(x.id)).sort((a, b) => b.sort - a.sort);
    const rest = all.filter((x) => !talk.has(x.id)).sort((a, b) => b.sort - a.sort).slice(0, MAX_ROWS);
    const lm = msgs(r).at(-1);
    const band = [{ id: "*", title: "All stations", glyph: "ALL", seed: "@ALLCALL", on: !to,
      last: lm ? { who: lm.from, text: bodyOf(lm), t: ms(lm.utc) / 1000 } : null, empty: "Nothing decoded yet",
      hint: "Everything decoded on this frequency; what you send here goes to everyone" }];
    return [["Band", band, ""], ["Talking with you", tk, "Stations that call you, or that you call, show here."],
            ["Heard", rest, "No stations yet."]];
  }

  // Messages, your lines, and frames still arriving, as chat.js items.
  function threadItems(r) {
    const items = [];
    for (const m of msgs(r)) {
      if (to && m.from !== to) continue;            // a station's thread: what it sent, and your lines to it
      const mine = toMe(m);
      items.push({ key: `rx:${m.utc}:${m.from}:${Math.round(m.freq_hz || 0)}`, mine: false, t: ms(m.utc) / 1000,
        who: m.from, seed: m.from, peer: m.from !== to, tag: m.to ? `→ ${mine ? "you" : m.to}` : "",
        whoTitle: `Talk to ${m.from}`, text: bodyOf(m),
        sub: [snr(m.snr) && `${snr(m.snr)} dB`, typeof m.freq_hz === "number" && `${Math.round(m.freq_hz)} Hz`].filter(Boolean).join(" · ") });
    }
    for (const o of outs()) {
      if (to && o.to !== to) continue;
      const [glyph, why, cls] = { queued: ["⏳", "Queued in the engine: it goes out at the next cycle", "queued"],
        air: ["📡", "On the air now", "relayed"], sent: ["✓", "Sent: the radio keyed and released", "delivered"],
        fail: ["✗", o.why || "Not sent", "failed"] }[o.state];
      items.push({ key: o.key, mine: true, t: o.t, who: "me", text: o.text, sub: o.sub || "", ack: { glyph, why, cls } });
    }
    // Frames from the open station since its last complete message: still arriving.
    if (to) {
      const lastMsg = Math.max(0, ...msgs(r).filter((m) => m.from === to).map((m) => ms(m.utc)));
      const h = (r.heard || []).find((x) => x.call === to);
      const bin = h && typeof h.freq_hz === "number" ? Math.round(h.freq_hz / BIN_HZ) : null;
      const parts = (r.frames || []).filter((f) => f.kind === "frame" && ms(f.utc) > lastMsg && Date.now() - ms(f.utc) < 60_000
        && (callOf(f) === to || (bin !== null && typeof f.freq_hz === "number" && Math.round(f.freq_hz / BIN_HZ) === bin)));
      if (parts.length) {
        items.push({ key: `part:${to}`, mine: false, t: ms(parts.at(-1).utc) / 1000, who: to, seed: to, peer: false,
          text: parts.map((f) => String(f.text || "").replace(FRAME_CALL, "")).join("").trim() || "…", sub: "receiving…" });
      }
    }
    return items.sort((a, b) => a.t - b.t);
  }

  function chatHead(r) {
    const h = ui.head;
    h.replaceChildren();
    const t = el("div", "chat-head-text");
    if (!to) {
      const s = r.status || {};
      h.insertAdjacentHTML("afterbegin", chat.avatar("All stations", "@ALLCALL", "ALL"));
      t.append(el("b", null, "All stations"),
        el("span", null, `Everything decoded${typeof s.radio_freq_khz === "number" ? ` on ${(s.radio_freq_khz / 1000).toFixed(3)} MHz` : ""}. Click a name to talk to that station.`));
      h.appendChild(t);
      return;
    }
    const x = (r.heard || []).find((y) => y.call === to) || {};
    h.insertAdjacentHTML("afterbegin", chat.avatar(to, to));
    const bits = [snr(x.snr) && `${snr(x.snr)} dB`, x.grid || grids[to], typeof x.freq_hz === "number" && `${Math.round(x.freq_hz)} Hz`,
                  x.submode, x.utc && `heard ${ago(ms(x.utc))} ago`].filter(Boolean);
    t.append(el("b", "js8-thread-call", to), el("span", null, bits.join(" · ") || "Not heard on this frequency yet"));
    h.appendChild(t);
    if (typeof x.freq_hz === "number") {
      const near = el("button", "chip chip-mini js8-near", `Send on ${Math.round(x.freq_hz / 10) * 10} Hz`);
      near.type = "button";
      near.title = "Put your offset on this station's, so you answer where it listens";
      near.onclick = () => setOffset(Math.round(x.freq_hz / 10) * 10);
      h.appendChild(near);
    }
    const all = el("button", "chip chip-mini js8-back", "All stations");
    all.type = "button";
    all.title = "Back to everything on the band";
    all.onclick = () => select(null);
    h.appendChild(all);
  }

  // ---------- the chat columns and composer: built once, updated in place,
  // so a frame arriving mid-typing keeps the text, the focus and the scroll.

  function skeleton() {
    const cols = el("div", "chat-cols js8-cols");
    const rail = el("nav", "chat-rail js8-rail");
    rail.setAttribute("aria-label", "Stations");
    const main = el("div", "chat-main js8-main");
    const head = el("div", "chat-head js8-chat-head");
    const pane = el("div", "chat-pane");
    const list = el("div", "js8-list");
    list.setAttribute("role", "log"); list.setAttribute("aria-live", "polite"); list.setAttribute("aria-label", "Messages");
    pane.appendChild(list);

    const compose = el("div", "chat-compose js8-sendpanel");
    const gate = el("div", "js8-gate");
    const tools = el("div", "js8-tools");
    const hb = el("button", "chip js8-hb", "HB");
    hb.type = "button";
    hb.dataset.tip = "Heartbeat: tells everyone listening you're on, with your grid. jf8call picks a clear spot.";
    const cq = el("button", "chip js8-cq", "CQ");
    cq.type = "button";
    cq.dataset.tip = "Call CQ: @ALLCALL CQ CQ CQ and your grid";
    const askLabel = el("span", "label js8-ask", "Ask");
    const qs = QUERIES.map(([q, what]) => {
      const b = el("button", "chip js8-q", q);
      b.type = "button";
      b.dataset.q = q; b.dataset.what = what;
      b.onclick = () => to && transmit(`${to} ${q}`, to);
      return b;
    });
    const offset = el("input", "js8-offset");
    offset.type = "number"; offset.min = 400; offset.max = 2600; offset.step = 10; offset.placeholder = "auto";
    offset.title = "Your audio offset, Hz (400-2600). Blank lets the engine pick. Or click the waterfall.";
    offset.oninput = () => wfMarks();
    const speedSeg = el("div", "seg js8-speed");
    const speedBtns = SPEEDS.map(([v, label]) => {
      const c = el("button", "chip" + (v === speed ? " on" : ""), label);
      c.type = "button";
      c.dataset.speed = v;
      c.title = v ? `Send at ${label}: ${sub(v).bw} Hz wide, a ${sub(v).period} s cycle` : "The engine's speed";
      c.onclick = () => { speed = v; for (const x of speedBtns) x.classList.toggle("on", x.dataset.speed === speed); wfMarks(); };
      speedSeg.appendChild(c);
      return c;
    });
    const ofw = el("label", "js8-offwrap");
    ofw.append(el("span", "label", "Offset"), offset);
    tools.append(hb, cq, el("span", "js8-sep"), askLabel, ...qs, el("span", "js8-grow"), ofw, speedSeg);

    const toBar = el("div", "mesh-to js8-to");
    const form = el("form", "chat-form js8-form");
    const prefix = el("span", "js8-prefix");
    const text = el("textarea", "js8-text");
    text.rows = 1; text.maxLength = MAX_SEND; text.autocomplete = "off"; text.spellcheck = false;
    text.setAttribute("aria-label", "Message");
    const count = el("span", "stat js8-count");
    const sendB = el("button", "chat-send js8-send", "Send");
    sendB.type = "submit";
    sendB.dataset.tip = "Send (Enter)";
    form.append(prefix, text, count, sendB);
    compose.append(gate, tools, toBar, form);
    main.append(head, pane, compose);

    const side = el("div", "chat-side js8-side");
    const actHead = el("div", "js8-panel-head");
    actHead.append(el("h3", null, "Band activity"));
    const actSub = el("p", "js8-sub", "What each offset said last, as JS8Call lists it. Click a line to talk to its sender.");
    const act = el("div", "js8-actwrap");
    side.append(actHead, actSub, act);
    cols.append(rail, main, side);

    const comp = chat.composer({ form, text, onInput: () => {
      count.textContent = text.value.length ? `${text.value.length}/${MAX_SEND}` : "";
    }, onEscape: () => select(null) });
    form.onsubmit = (e) => {
      e.preventDefault();
      let t = text.value.replace(/\s+/g, " ").trim().toUpperCase();
      if (!t || sendB.disabled) return;
      if (to && !t.startsWith(to + " ")) t = `${to} ${t}`;
      if (transmit(t, to)) comp.clear();
    };
    hb.onclick = () => {
      if (!api || !pick) return;
      api.send("hb", { radio: pick });
      queue(`@HB HEARTBEAT ${String(st().grid || "").slice(0, 4)}`.trim(), null, "heartbeat");
    };
    cq.onclick = () => {
      const g = String(st().grid || "").slice(0, 4);
      transmit(`@ALLCALL CQ CQ CQ${g ? " " + g : ""}`, null);
    };
    return { root: null, cols, rail, list, head, compose, gate, hb, cq, qs, askLabel, offset,
             toBar, prefix, text, count, sendB, act };
  }

  // Hand the text to the engine, and keep it as your line in the conversation.
  function transmit(text, toCall) {
    if (!api || !pick || !text) return false;
    const x = { radio: pick, text };
    const off = Number(ui.offset.value);
    if (ui.offset.value !== "" && Number.isFinite(off)) x.offset_hz = off;
    if (speed) x.speed = speed;
    api.send("send", x);
    queue(text, toCall, [x.offset_hz && `${x.offset_hz} Hz`, speed].filter(Boolean).join(" · "));
    return true;
  }

  function queue(text, toCall, subText) {
    outs().push({ key: `tx:${++txSeq}`, t: Date.now() / 1000, at: Date.now(), to: toCall || null, text,
                  sub: subText || "", state: "queued" });
    if (outs().length > 200) outs().shift();
    render();
  }

  // What became of your lines, by the radio server's word, oldest first and
  // one at a time (the engine sends its queue in order): the radio keys (on
  // the air), then releases with nothing queued, or stays released 20 s
  // (sent). Disarmed first (not sent); the line at the head of the queue
  // with no key for 45 s (not sent: the station's notice says why). A line
  // waiting behind another starts its 45 s when it reaches the head.
  function track(radio, s) {
    const box = outbox[radio];
    if (!box || !box.length) return false;
    let changed = false;
    const now = Date.now();
    const air = box.find((o) => o.state === "air");
    if (air && s.radio_tx) air.at = now;
    else if (air && (!s.queue || now - air.at > 20_000)) {
      air.state = "sent"; changed = true;
      const next = box.find((o) => o.state === "queued");
      if (next) next.at = now;
    }
    const head = box.find((o) => o.state === "queued");
    if (head && s.radio_tx && !box.some((o) => o.state === "air")) { head.state = "air"; head.at = now; changed = true; }
    for (const o of box) {
      if (o.state !== "queued") continue;
      if (!s.armed) { o.state = "fail"; o.why = "Not sent: transmit was disarmed first"; changed = true; }
      else if (o === head && !box.some((x) => x.state === "air") && now - o.at > 45_000) {
        o.state = "fail"; o.why = "Not sent: the radio never keyed in 45 s (see the notice)"; changed = true;
        const next = box.find((x) => x.state === "queued");
        if (next) next.at = now;
      }
    }
    return changed;
  }

  function updateCompose(r) {
    const t = ui, s = (r && r.status) || {};
    const armed = !!s.armed;
    t.compose.classList.toggle("off", !armed);
    t.gate.textContent = armed ? `Armed · ${left(s)} left` : s.why_not ? `Can't send: ${s.why_not}` : "Arm TX above to send";
    t.gate.classList.toggle("armed", armed);
    for (const b of [t.hb, t.cq, t.sendB, t.text]) {
      b.disabled = !armed;
      b.title = armed ? (b.dataset.tip || "") : "Arm TX first";
    }
    for (const b of t.qs) {
      b.disabled = !armed || !to;
      b.hidden = !to;
      b.title = !armed ? "Arm TX first" : `${to} ${b.dataset.q} (${b.dataset.what})`;
    }
    t.askLabel.hidden = !to;
    t.askLabel.textContent = to ? `Ask ${to}` : "Ask";
    t.toBar.replaceChildren();
    t.toBar.hidden = !to;
    if (to) {
      t.toBar.append(el("span", null, "To "), el("b", "call", to));
      const x = el("button", "chip chip-mini js8-clear", "× Everyone");
      x.type = "button";
      x.title = "Talk to everyone instead";
      x.onclick = () => select(null);
      t.toBar.appendChild(x);
    }
    t.prefix.textContent = to || "";
    t.prefix.hidden = !to;
    t.text.placeholder = !armed ? "Arm TX to send" : to ? `Message to ${to} (Enter sends)` : "Message to everyone (Enter sends)";
  }

  // ---------- Band activity: JS8Call's offset list

  function activity(r) {
    const rows = new Map();                       // bin -> the latest frame or message there
    for (const f of (r.frames || [])) {
      if (typeof f.freq_hz !== "number") continue;
      rows.set(Math.round(f.freq_hz / BIN_HZ) * BIN_HZ, f);
    }
    const t = el("table", "js8-activity");
    const hd = t.createTHead().insertRow();
    for (const h of ["Offset", "Age", "dB", "Text"]) hd.appendChild(el("th", null, h));
    const tb = t.createTBody();
    [...rows.entries()].sort((a, b) => a[0] - b[0]).slice(0, MAX_ROWS).forEach(([bin, f]) => {
      const tr = tb.insertRow();
      tr.dataset.bin = bin;
      const call = callOf(f);
      if (call) {
        tr.classList.add("pickable");
        if (call === to) tr.classList.add("sel");
        tr.title = `Talk to ${call}`;
        tr.onclick = () => select(call);
      }
      const mine = toMe(f);
      if (mine) tr.classList.add("to-me");
      tr.appendChild(el("td", "n", bin));
      const age = el("td", "t", ms(f.utc) ? ago(ms(f.utc)) : "");
      age.title = hhmmss(f.utc);
      tr.appendChild(age);
      tr.appendChild(el("td", "n", snr(f.snr)));
      const txt = el("td", "txt");
      if (mine) txt.appendChild(el("span", "js8-you", "YOU"));
      txt.appendChild(el("span", "air", f.text));
      tr.appendChild(txt);
    });
    if (!rows.size) tb.insertRow().appendChild(el("td", "empty", "Nothing heard yet.")).colSpan = 4;
    return t;
  }

  // ---------- render

  function render() {
    if (!body) return;
    if (!snap.ready) {
      ui = null;
      const root = el("div", "js8call");
      root.appendChild(el("div", "js8-status down", snap.why || "JS8 isn't running on this station."));
      body.replaceChildren(root);
      return;
    }
    const ids = Object.keys(radios());
    if (!ids.includes(pick)) { pick = ids[0] || null; to = null; }
    const r = cur();
    if (!ui || !body.contains(ui.root)) {
      ui = skeleton();
      ui.root = el("div", "js8call");
      body.replaceChildren(ui.root);
    }
    const root = ui.root;
    // The radio chips and the status card are rebuilt; the waterfall and the
    // chat columns are kept (history, scroll place, what's being typed).
    root.querySelector(".js8-radios")?.remove();
    root.querySelector(".js8-head")?.remove();
    if (ids.length > 1) {
      const chips = el("div", "seg js8-radios");
      for (const id of ids) {
        const c = el("button", "chip" + (id === pick ? " on" : ""), radios()[id].name || id);
        c.dataset.radio = id;
        c.onclick = () => { pick = id; to = null; render(); };
        chips.appendChild(c);
      }
      root.prepend(chips);
    }
    if (!r) { ui.cols.hidden = true; return; }
    const head = statusCard(r);
    const chips = root.querySelector(".js8-radios");
    if (chips) chips.after(head); else root.prepend(head);
    if (!root.contains(wfPanel().panel)) head.after(wf.panel);
    if (!root.contains(ui.cols)) root.appendChild(ui.cols);
    ui.cols.hidden = false;

    chat.rail(ui.rail, railRows(r), (id) => select(id === "*" ? null : id));
    chatHead(r);
    chat.render(ui.list, threadItems(r), {
      conv: `${pick}:${to || "*"}`,
      empty: `<div class="wl-empty chat-empty">${to ? "Nothing between you and this station yet. Ask SNR? or say hello."
        : "No complete messages yet. Band activity, right, shows frames as they arrive."}</div>`,
      onWho: (it) => select(it.who),
    });
    updateCompose(r);
    ui.act.replaceChildren(activity(r));
    wfMarks();
    tick();
  }

  // Once a second while armed or a line of yours is in flight: the countdown,
  // and what became of the lines.
  function tick() {
    const busy = () => st().armed || (outbox[pick] || []).some((o) => o.state === "queued" || o.state === "air");
    if (ticker || !busy()) return;
    ticker = setInterval(() => {
      if (!body || !busy()) { clearInterval(ticker); ticker = null; return; }
      refreshHead();
      if (ui && st().armed) ui.gate.textContent = `Armed · ${left(st())} left`;
      if (track(pick, st())) render();
    }, 1000);
  }

  function upsertHeard(r, f) {
    const call = callOf(f);
    if (!call) return;
    if (f.grid) grids[call] = f.grid;
    r.heard = (r.heard || []).filter((h) => h.call !== call);
    r.heard.unshift({ call, utc: f.utc, snr: f.snr, freq_hz: f.freq_hz, submode: f.submode, grid: f.grid || grids[call] || "" });
    r.heard.length = Math.min(r.heard.length, 200);
  }

  // A frame's sender, or the station last heard on its offset (a middle
  // frame doesn't say who it is).
  function senderAt(r, f) {
    const c = callOf(f);
    if (c || typeof f.freq_hz !== "number") return c;
    const h = (r.heard || []).find((x) => typeof x.freq_hz === "number" && Math.abs(x.freq_hz - f.freq_hz) < 15);
    return h ? h.call : null;
  }

  function fresh() {
    const now = Date.now();
    let n = 0;
    for (const r of Object.values(radios())) {
      for (const h of (r.heard || [])) { if (now - ms(h.utc) < FRESH_MS) n++; }
    }
    return n;
  }

  addons.register("js8call", {
    mount(b, a) { body = b; api = a || null; ui = null; render(); },
    hello(s) { snap = s || { ready: false }; render(); },
    event(m) {
      const r = radios()[m.radio];
      if (!r) return;
      if (m.event === "spectrum" && Array.isArray(m.row)) {
        // ~4 a second: draw the row only, never the whole tab.
        if (m.radio === pick && body && body.isConnected !== false) wfRow(m.radio, m.row);
        return;
      }
      if (m.event === "frame" && m.frame) {
        r.frames = (r.frames || []).concat([m.frame]).slice(-300);
        const call = senderAt(r, m.frame);
        upsertHeard(r, m.frame);
        if (m.radio === pick) {
          wfBox(m.radio, m.frame, call);
          if (to && m.frame.kind === "message" && m.frame.from === to) markSeen(to);   // read as it arrives
        }
      } else if (m.event === "status" && m.status) {
        r.status = m.status;
        track(m.radio, m.status);
      }
      if (m.radio === pick) render();
    },
    off() { snap = { ready: false }; render(); },
    tab() {
      const rs = Object.values(radios());
      if (rs.some((r) => r.status && r.status.unsafe && r.status.unsafe.length)) {
        return { dot: "tx", title: "JS8: an engine reports TX on (UNSAFE)" };
      }
      if (rs.some((r) => r.status && r.status.armed)) {
        return { dot: "tx", title: "JS8: TX armed" };
      }
      const up = rs.some((r) => r.status && r.status.connected);
      const n = fresh();
      return { dot: up ? "ok" : "off", badge: n || undefined,
               title: up ? `JS8: ${n} station(s) heard in the last 10 minutes` : "JS8: no engine connected" };
    },
    card() {
      if (!snap.ready) return { state: "off", lines: [snap.why || "not set up"] };
      return { state: "on", lines: Object.entries(radios()).map(([id, r]) =>
        `${r.name || id}: ${(r.status && r.status.connected) ? (r.status.armed ? "TX armed" : "listening") : "engine down"}, ${(r.heard || []).length} heard`) };
    },
  });
})();
