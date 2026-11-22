"use strict";

// The JS8 tab: what the station's JS8 engines hear, per radio, and sending
// while armed. Laid out in the order you use it:
//
//   1 status card   what the radio is doing, in one word (Listening, TX
//                   armed, ON AIR, STOP TX, Engine down, UNSAFE), the
//                   frequency, your call and grid, the JS8 bands (a click
//                   retunes, USB data, through the station's padlock; with
//                   auto-tune the ATU tunes the band; not while armed); on
//                   the right, Transmit:
//                   pick 5/10/30 min and Arm TX, or the time left and Disarm;
//                   when it can't arm, why
//   1b waterfall    the engine's audio, 0-3000 Hz, newest row on top (the
//                   station relays ~4 rows a second); the stations heard in
//                   the last WF_LABEL_MS labelled at their offsets (a click on
//                   one picks it); your TX offset marked; a click elsewhere
//                   puts that offset in Options (sends nothing)
//   2 send panel    To: everyone, or the station picked by clicking its call
//                   in any list below; Quick: HB, CQ; Ask <call>: SNR?
//                   GRID? INFO? STATUS? HEARING?; Message: a line of text
//                   and Send; Options (folded): offset and speed. Disabled,
//                   with a banner saying so, until this page arms
//   3 lists         Band activity (latest text per audio offset), Messages
//                   (complete decodes), Stations heard; each says what it is
//                   and that a click picks the station. Anything addressed
//                   to your call is marked YOU
//
// The arm is this page's: closing it disarms (js8_bridge.py holds every gate).
// What is on the air is the radio server's word (radio_tx), never the engine's.
//
// Server: addons/js8call.py relays js8_bridge.py: hello {ready, why?,
// radios: {id: {name, status, frames, heard}}}; events {event: "frame" |
// "status", radio, ...}; the page sends api.send("arm", {radio, minutes}),
// ("send", {radio, text, offset_hz?, speed?}), ("hb", {radio}),
// ("disarm", {radio}). Every string from the air goes through textContent:
// JS8 text can hold < and >.

(() => {
  const BIN_HZ = 50, MAX_ROWS = 60, MAX_MSGS = 100, FRESH_MS = 10 * 60 * 1000;
  const FRAME_CALL = /^\s*([A-Z0-9]{1,3}[0-9][A-Z0-9/]{0,8}):/;
  const MAX_SEND = 160, ARM_MINUTES = [5, 10, 30];
  const QUERIES = [["SNR?", "How strong am I?"], ["GRID?", "Where are you?"], ["INFO?", "Your station info"],
                   ["STATUS?", "Your status"], ["HEARING?", "Who do you hear?"]];
  // JS8Call's own default dial frequencies (Hz), the ones stock stations sit on.
  const DIALS = [["160", 1_842_000], ["80", 3_578_000], ["40", 7_078_000], ["30", 10_130_000],
                 ["20", 14_078_000], ["17", 18_104_000], ["15", 21_078_000], ["12", 24_922_000],
                 ["10", 28_078_000], ["6", 50_318_000]];
  const SPEEDS = [["", "As set"], ["normal", "Normal"], ["fast", "Fast"], ["turbo", "Turbo"], ["slow", "Slow"]];
  let body = null, api = null, snap = { ready: false }, pick = null, tx = null, ticker = null, to = null, wf = null;
  const WF_ROWS = 160, WF_TOP_HZ = 3000, WF_LABEL_MS = 3 * 60 * 1000, WF_RANGE = 60;
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
  let armMins = 10, speed = "";
  const grids = {};                               // call -> grid, from messages that carried one

  const radios = () => snap.radios || {};
  const cur = () => radios()[pick] || null;
  const myCall = () => String((((cur() || {}).status || {}).callsign) || "").toUpperCase();

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  const hhmmss = (utc) => (String(utc || "").match(/T(\d\d:\d\d:\d\d)/) || [, ""])[1];
  const snr = (v) => (typeof v === "number" ? (v > 0 ? `+${v}` : `${v}`) : "");
  const callOf = (f) => f.from || (String(f.text || "").match(FRAME_CALL) || [])[1] || null;

  // Addressed to this station: "to" is my call, or my call is a word in the text after the sender.
  function toMe(f) {
    const me = myCall();
    if (!me) return false;
    if (String(f.to || "").toUpperCase() === me) return true;
    const rest = String(f.text || "").toUpperCase().replace(FRAME_CALL, "");
    return rest.split(/[^A-Z0-9/]+/).includes(me);
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
          if (res && !res.ok && /not in the|no tuning|exact dials/.test(res.why || "")) toast(`Not retuned: ${res.why}`, "warn");
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
        c.onclick = () => { armMins = m; render(); };
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

  // ---------- 1b waterfall: built once and kept, so its history survives a re-render

  function wfPanel() {
    if (wf) return wf;
    const panel = el("section", "panel js8-wf");
    const head = el("div", "js8-panel-head");
    head.append(el("h3", null, "Waterfall"),
                el("span", "js8-sub", "The engine's audio, 0-3000 Hz. Click a quiet spot to send there; click a call to talk to it."));
    const wrap = el("div", "js8-wf-wrap");
    const canvas = el("canvas", "js8-wf-canvas");
    canvas.width = WF_TOP_HZ / 10; canvas.height = WF_ROWS;
    const labels = el("div", "js8-wf-labels");
    const mark = el("div", "js8-wf-tx");
    const scale = el("div", "js8-wf-scale");
    for (const hz of [500, 1000, 1500, 2000, 2500]) {
      const t = el("span", null, String(hz));
      t.style.left = `${hz / WF_TOP_HZ * 100}%`;
      scale.appendChild(t);
    }
    const note = el("div", "js8-wf-note", "Waiting for the engine's audio");
    wrap.append(canvas, labels, mark, note);
    panel.append(head, wrap, scale);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#04060a"; ctx.fillRect(0, 0, canvas.width, canvas.height);
    canvas.onclick = (e) => {
      const r = canvas.getBoundingClientRect();
      const hz = Math.round((e.clientX - r.left) / r.width * WF_TOP_HZ / 10) * 10;
      const t = sendPanel();
      t.offset.value = Math.max(400, Math.min(2600, hz));
      t.opts.open = true;
      wfMarks();
    };
    wf = { panel, canvas, ctx, labels, mark, note, rows: 0, radio: null, at: 0 };
    return wf;
  }

  function wfRow(radio, row) {
    const w = wfPanel();
    if (w.radio !== radio) {                 // another radio picked: its own history
      w.radio = radio; w.rows = 0;
      w.ctx.fillStyle = "#04060a"; w.ctx.fillRect(0, 0, w.canvas.width, w.canvas.height);
    }
    const n = Math.min(row.length, w.canvas.width);
    w.ctx.drawImage(w.canvas, 0, 0, w.canvas.width, WF_ROWS - 1, 0, 1, w.canvas.width, WF_ROWS - 1);
    const img = w.ctx.createImageData(w.canvas.width, 1);
    for (let i = 0; i < n; i++) {
      const v = (Math.max(0, Math.min(WF_RANGE, Number(row[i]) || 0)) / WF_RANGE * 255) | 0;
      img.data[i * 4] = WF_LUT[v * 3]; img.data[i * 4 + 1] = WF_LUT[v * 3 + 1];
      img.data[i * 4 + 2] = WF_LUT[v * 3 + 2]; img.data[i * 4 + 3] = 255;
    }
    w.ctx.putImageData(img, 0, 0);
    w.rows++; w.at = Date.now();
    w.canvas.dataset.rows = w.rows;
    w.note.hidden = true;
  }

  // Labels and the TX mark: who was heard where lately, and where you'd send.
  function wfMarks() {
    const w = wfPanel(), r = cur();
    w.labels.replaceChildren();
    const now = Date.now();
    for (const h of ((r && r.heard) || [])) {
      if (typeof h.freq_hz !== "number" || h.freq_hz < 0 || h.freq_hz > WF_TOP_HZ || now - Date.parse(h.utc) > WF_LABEL_MS) continue;
      const b = el("button", "js8-wf-call" + (h.call === to ? " sel" : ""), h.call);
      b.style.left = `${h.freq_hz / WF_TOP_HZ * 100}%`;
      b.dataset.call = h.call;
      b.title = `${h.call} at ${Math.round(h.freq_hz)} Hz, ${snr(h.snr)} dB: click to talk to it`;
      b.onclick = (e) => { e.stopPropagation(); pickTo(h.call); };
      w.labels.appendChild(b);
    }
    const off = tx && tx.offset.value !== "" ? Number(tx.offset.value) : NaN;
    w.mark.hidden = !Number.isFinite(off);
    if (Number.isFinite(off)) {
      w.mark.style.left = `${off / WF_TOP_HZ * 100}%`;
      w.mark.title = `You send at ${off} Hz`;
    }
    if (w.at && now - w.at > 5000) { w.note.hidden = false; w.note.textContent = "No audio from the engine in 5 s"; }
  }

  // ---------- 2 send panel: built once, updated in place, so a frame
  // arriving mid-typing keeps the text and the focus.

  function pickTo(call) {
    to = call && call !== to ? call : null;     // a second click on the same station clears it
    render();
  }

  function field(label, ...kids) {
    const row = el("div", "js8-field");
    row.appendChild(el("span", "label", label));
    const k = el("div", "js8-ctl");
    k.append(...kids);
    row.appendChild(k);
    return row;
  }

  function sendPanel() {
    if (tx) return tx;
    const panel = el("section", "panel js8-sendpanel");
    const head = el("div", "js8-panel-head");
    head.append(el("h3", null, "Send"), el("span", "js8-sub js8-gate", ""));
    panel.appendChild(head);

    const toBox = el("div", "js8-to");
    panel.appendChild(field("To", toBox));

    const hb = el("button", "chip js8-hb", "HB");
    hb.dataset.tip = "Heartbeat: tells everyone listening you're on, with your grid. jf8call picks a clear spot.";
    const cq = el("button", "chip js8-cq", "CQ");
    cq.dataset.tip = "Call CQ: @ALLCALL CQ CQ CQ and your grid";
    panel.appendChild(field("Quick", hb, cq));

    const qs = QUERIES.map(([q, what]) => {
      const b = el("button", "chip js8-q", q);
      b.dataset.q = q;
      b.dataset.what = what;
      b.onclick = () => to && sendText(`${to} ${q}`);
      return b;
    });
    const askRow = field("Ask", ...qs);
    const askLabel = askRow.querySelector(".label");
    panel.appendChild(askRow);

    const prefix = el("span", "js8-prefix");
    const input = el("input", "js8-text");
    input.type = "text";
    input.maxLength = MAX_SEND;
    input.autocomplete = "off";
    input.spellcheck = false;
    const sendB = el("button", "chip js8-send", "Send");
    sendB.dataset.tip = "Send this message (Enter)";
    const msgWrap = el("div", "js8-msgline");
    msgWrap.append(prefix, input, sendB);
    panel.appendChild(field("Message", msgWrap));

    const opts = el("details", "js8-opts");
    opts.appendChild(el("summary", null, "Options: offset and speed"));
    const offset = el("input", "js8-offset");
    offset.type = "number";
    offset.min = 400; offset.max = 2600; offset.step = 10;
    offset.placeholder = "engine's";
    const speedSeg = el("div", "seg js8-speed");
    const speedBtns = SPEEDS.map(([v, label]) => {
      const c = el("button", "chip" + (v === speed ? " on" : ""), label);
      c.dataset.speed = v;
      c.onclick = () => { speed = v; for (const x of speedBtns) x.classList.toggle("on", x.dataset.speed === speed); };
      speedSeg.appendChild(c);
      return c;
    });
    offset.oninput = () => wfMarks();
    opts.append(field("Offset", offset, el("span", "js8-hint", "Hz, 400-2600. Blank keeps the engine's. Pick a quiet spot in Band activity.")),
                field("Speed", speedSeg, el("span", "js8-hint", "Normal is what most stations use.")));
    panel.appendChild(opts);

    function sendText(text) {
      if (!api || !pick || !text) return;
      const x = { radio: pick, text };
      const off = Number(offset.value);
      if (offset.value !== "" && Number.isFinite(off)) x.offset_hz = off;
      if (speed) x.speed = speed;
      api.send("send", x);
    }
    const send = () => {
      let text = input.value.trim().toUpperCase();
      if (!text) return;
      if (to && !text.startsWith(to + " ")) text = `${to} ${text}`;
      sendText(text);
      input.value = "";
    };
    hb.onclick = () => api && pick && api.send("hb", { radio: pick });
    cq.onclick = () => {
      const g = String(((cur() || {}).status || {}).grid || "").slice(0, 4);
      sendText(`@ALLCALL CQ CQ CQ${g ? " " + g : ""}`);
    };
    sendB.onclick = send;
    input.onkeydown = (e) => { if (e.key === "Enter") send(); };
    tx = { panel, gate: head.querySelector(".js8-gate"), toBox, hb, cq, qs, askLabel, prefix, input, sendB, offset, opts };
    return tx;
  }

  function updateSend(r) {
    const t = sendPanel(), s = (r && r.status) || {};
    const armed = !!s.armed;
    t.panel.classList.toggle("off", !armed);
    t.gate.textContent = armed ? `Armed · ${left(s)} left` : "Arm TX above to send";
    for (const b of [t.hb, t.cq, t.sendB, t.input]) {
      b.disabled = !armed;
      b.title = armed ? (b.dataset.tip || "") : "Arm TX first";
    }
    for (const b of t.qs) {
      b.disabled = !armed || !to;
      b.title = !armed ? "Arm TX first" : !to ? "Pick a station first: click its call in any list below"
        : `${to} ${b.dataset.q} (${b.dataset.what})`;
    }
    t.askLabel.textContent = to ? `Ask ${to}` : "Ask";
    t.toBox.replaceChildren();
    if (to) {
      const p = el("span", "pill pill-ok js8-target");
      p.append(el("span", "call", to));
      const x = el("button", "js8-clear", "×");
      x.title = "Clear: talk to everyone";
      x.onclick = () => pickTo(null);
      p.appendChild(x);
      t.toBox.appendChild(p);
    } else {
      t.toBox.append(el("span", "pill pill-ghost js8-target", "Everyone"),
                     el("span", "js8-hint", "Click a call sign in any list below to talk to that station."));
    }
    t.prefix.textContent = to || "";
    t.prefix.hidden = !to;
    t.input.placeholder = to ? `Message to ${to}` : "Message to everyone";
    if (armed && !ticker) {
      ticker = setInterval(() => {
        const c = cur(), st = (c && c.status) || {};
        if (!st.armed) { clearInterval(ticker); ticker = null; return; }
        const head = body && body.querySelector(".js8-head");
        if (head) head.replaceWith(statusCard(c));
        if (tx) tx.gate.textContent = `Armed · ${left(st)} left`;
      }, 1000);
    }
  }

  // ---------- 3 lists

  function pickable(node, call) {
    if (!call) return;
    node.classList.add("pickable");
    if (call === to) node.classList.add("sel");
    node.title = call === to ? `${call} picked: click to clear` : `Click to talk to ${call}`;
    node.onclick = () => pickTo(call);
  }
  const youTag = () => el("span", "js8-you", "YOU");

  function activity(r) {
    const rows = new Map();                       // bin -> latest text there
    for (const f of (r.frames || [])) {
      if (typeof f.freq_hz !== "number" || f.kind !== "frame") continue;
      rows.set(Math.round(f.freq_hz / BIN_HZ) * BIN_HZ, f);
    }
    const t = el("table", "js8-activity");
    const hd = t.createTHead().insertRow();
    for (const h of ["UTC", "dB", "Offset", "Text"]) hd.appendChild(el("th", null, h));
    const tb = t.createTBody();
    [...rows.entries()].sort((a, b) => a[0] - b[0]).slice(0, MAX_ROWS).forEach(([bin, f]) => {
      const tr = tb.insertRow();
      tr.dataset.bin = bin;
      pickable(tr, callOf(f));
      const mine = toMe(f);
      if (mine) tr.classList.add("to-me");
      tr.appendChild(el("td", "t", hhmmss(f.utc)));
      tr.appendChild(el("td", "n", snr(f.snr)));
      tr.appendChild(el("td", "n", bin));
      const txt = el("td", "txt");
      if (mine) txt.appendChild(youTag());
      txt.appendChild(el("span", "air", f.text));
      tr.appendChild(txt);
    });
    if (!rows.size) tb.insertRow().appendChild(el("td", "empty", "Nothing heard yet.")).colSpan = 4;
    return t;
  }

  function messages(r) {
    const list = el("ul", "js8-msgs");
    const msgs = (r.frames || []).filter((f) => f.kind === "message").slice(-MAX_MSGS).reverse();
    for (const m of msgs) {
      const li = el("li");
      pickable(li, m.from);
      const mine = toMe(m);
      if (mine) li.classList.add("to-me");
      li.appendChild(el("span", "t", hhmmss(m.utc)));
      li.appendChild(el("b", "from", m.from || "?"));
      li.appendChild(el("span", "to", m.to ? `→ ${m.to}` : ""));
      li.appendChild(el("span", "n", snr(m.snr)));
      const txt = el("span", "txt");
      if (mine) txt.appendChild(youTag());
      txt.appendChild(el("span", "air", m.text));
      li.appendChild(txt);
      list.appendChild(li);
    }
    if (!msgs.length) list.appendChild(el("li", "empty", "No complete messages yet."));
    return list;
  }

  function heard(r) {
    const t = el("table", "js8-heard");
    const hd = t.createTHead().insertRow();
    for (const h of ["Call", "dB", "Grid", "UTC", "Offset", "Speed"]) hd.appendChild(el("th", null, h));
    const tb = t.createTBody();
    for (const h of (r.heard || []).slice(0, MAX_ROWS)) {
      const tr = tb.insertRow();
      pickable(tr, h.call);
      tr.appendChild(el("td", "call", h.call));
      tr.appendChild(el("td", "n", snr(h.snr)));
      tr.appendChild(el("td", null, h.grid || grids[h.call] || ""));
      tr.appendChild(el("td", "t", hhmmss(h.utc)));
      tr.appendChild(el("td", "n", typeof h.freq_hz === "number" ? Math.round(h.freq_hz) : ""));
      tr.appendChild(el("td", null, h.submode || ""));
    }
    if (!(r.heard || []).length) tb.insertRow().appendChild(el("td", "empty", "No stations yet.")).colSpan = 6;
    return t;
  }

  function forMe(r) {
    const now = Date.now();
    return (r.frames || []).filter((f) => toMe(f) && now - Date.parse(f.utc) < FRESH_MS).length;
  }

  function panes(r) {
    const grid = el("div", "js8-grid");
    const n = forMe(r);
    for (const [title, sub, fn, cls] of [
      ["Band activity", "Latest text on each audio offset. Click a line to talk to its sender.", activity, "act"],
      ["Messages", "Complete messages, newest first.", messages, "msgs"],
      ["Stations heard", "Newest first. Click a station to talk to it.", heard, "heard"]]) {
      const p = el("section", `panel js8-pane js8-pane-${cls}`);
      const head = el("div", "js8-panel-head");
      head.appendChild(el("h3", null, title));
      if (cls === "msgs" && n) head.appendChild(el("span", "js8-you js8-youcount", `${n} for you`));
      p.append(head, el("p", "js8-sub", sub), fn(r));
      grid.appendChild(p);
    }
    return grid;
  }

  function render() {
    if (!body) return;
    if (tx && tx.panel.contains(document.activeElement)) {
      // Typing: the send panel stays put; only what's around it is rebuilt.
      const root = body.querySelector(".js8call");
      const r = cur();
      if (root && r && root.querySelector(".js8-head")) {
        root.querySelector(".js8-head").replaceWith(statusCard(r));
        root.querySelector(".js8-grid").replaceWith(panes(r));
        updateSend(r);
        wfMarks();
        return;
      }
    }
    body.replaceChildren();
    const root = el("div", "js8call");
    if (!snap.ready) {
      root.appendChild(el("div", "js8-status down", snap.why || "JS8 isn't running on this station."));
      body.appendChild(root);
      return;
    }
    const ids = Object.keys(radios());
    if (!ids.includes(pick)) pick = ids[0] || null;
    if (ids.length > 1) {
      const chips = el("div", "seg js8-radios");
      for (const id of ids) {
        const c = el("button", "chip" + (id === pick ? " on" : ""), radios()[id].name || id);
        c.dataset.radio = id;
        c.onclick = () => { pick = id; to = null; render(); };
        chips.appendChild(c);
      }
      root.appendChild(chips);
    }
    const r = cur();
    if (r) {
      root.appendChild(statusCard(r));
      root.appendChild(wfPanel().panel);
      root.appendChild(sendPanel().panel);
      updateSend(r);
      wfMarks();
      root.appendChild(panes(r));
    }
    body.appendChild(root);
  }

  function upsertHeard(r, f) {
    const call = callOf(f);
    if (!call) return;
    if (f.grid) grids[call] = f.grid;
    r.heard = (r.heard || []).filter((h) => h.call !== call);
    r.heard.unshift({ call, utc: f.utc, snr: f.snr, freq_hz: f.freq_hz, submode: f.submode, grid: f.grid || grids[call] || "" });
    r.heard.length = Math.min(r.heard.length, 200);
  }

  function fresh() {
    const now = Date.now();
    let n = 0;
    for (const r of Object.values(radios())) {
      for (const h of (r.heard || [])) { if (now - Date.parse(h.utc) < FRESH_MS) n++; }
    }
    return n;
  }

  addons.register("js8call", {
    mount(b, a) { body = b; api = a || null; render(); },
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
        upsertHeard(r, m.frame);
      } else if (m.event === "status" && m.status) {
        r.status = m.status;
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
