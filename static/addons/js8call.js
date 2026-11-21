"use strict";

// The JS8 tab: what the station's JS8 engines hear, per radio, and sending
// while armed.
//
//   status     engine connected, the rig's frequency and mode, TX disarmed
//              or armed (time left), ON AIR when the radio's server says it
//              is keyed; a red UNSAFE banner if an engine reports TX,
//              auto-reply or heartbeat on without an arm
//   send       Arm for N minutes (refused, and why, when the radio can't
//              key); then HB, CQ, the directed queries (SNR? GRID? INFO?
//              STATUS? HEARING?) to the station picked by clicking it in
//              Heard, Messages or Band activity, a message box (to that
//              station, or everyone), offset and speed; Disarm. Shown
//              disabled until armed. The arm is this page's: closing it
//              disarms (js8_bridge.py holds the gates)
//   activity   one row per audio offset (50 Hz bins), the latest text there
//   messages   complete decodes: from, to, text, newest first
//   heard      stations heard: call, SNR, grid, when, offset; newest first
//
// Server: addons/js8call.py relays js8_bridge.py: hello {ready, why?,
// radios: {id: {name, status, frames, heard}}}; events {event: "frame" |
// "status", radio, ...}; the page sends api.send("arm", {radio, minutes}),
// ("send", {radio, text, offset_hz?, speed?}), ("hb", {radio}),
// ("disarm", {radio}). Every string from the air
// goes through textContent: JS8 text can hold < and >.

(() => {
  const BIN_HZ = 50, MAX_ROWS = 60, MAX_MSGS = 100, FRESH_MS = 10 * 60 * 1000;
  const FRAME_CALL = /^\s*([A-Z0-9]{1,3}[0-9][A-Z0-9/]{0,8}):/;
  const MAX_SEND = 160, ARM_MINUTES = [5, 10, 30];
  let body = null, api = null, snap = { ready: false }, pick = null, tx = null, ticker = null, to = null;
  const grids = {};                               // call -> grid, from messages that carried one

  const radios = () => snap.radios || {};
  const cur = () => radios()[pick] || null;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  const hhmmss = (utc) => (String(utc || "").match(/T(\d\d:\d\d:\d\d)/) || [, ""])[1];
  const snr = (v) => (typeof v === "number" ? (v > 0 ? `+${v}` : `${v}`) : "");

  function statusLine(r) {
    const s = (r && r.status) || {};
    const box = el("div", "js8-status");
    if (s.unsafe && s.unsafe.length) {
      box.classList.add("unsafe");
      box.textContent = `UNSAFE: the engine reports ${s.unsafe.join(", ")} on. It should start disarmed; stop it and check js8_engine.py.`;
      return box;
    }
    if (!s.connected) {
      box.classList.add("down");
      box.textContent = `Engine not connected${s.error ? ": " + s.error : ""}`;
      return box;
    }
    const f = typeof s.radio_freq_khz === "number" ? `${(s.radio_freq_khz / 1000).toFixed(3)} MHz` : "frequency unknown";
    const state = s.armed ? `TX armed, ${left(s)} left` : "TX disarmed";
    if (s.armed) box.classList.add(s.radio_tx ? "onair" : "armed");
    box.textContent = `${s.armed && s.radio_tx ? "ON AIR · " : "Listening · "}${f} ${s.radio_mode || ""} · ${s.callsign || "no call"} ${s.grid || ""} · ${state}`;
    return box;
  }

  function left(s) {
    const n = Math.max(0, Math.round((Number(s.armed_until) || 0) - Date.now() / 1000));
    return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
  }

  // The send bar is built once and updated in place: a re-render on every
  // frame must not drop what's being typed or take the focus away. Every
  // control shows whether armed or not; the ones that transmit are disabled
  // until this page arms.
  const QUERIES = ["SNR?", "GRID?", "INFO?", "STATUS?", "HEARING?"];
  const SPEEDS = [["", "Speed: as set"], ["normal", "Normal"], ["fast", "Fast"], ["turbo", "Turbo"], ["slow", "Slow"]];

  function pickTo(call) {
    to = call && call !== to ? call : null;     // a second click on the same station clears it
    render();
  }

  function txBar() {
    if (tx) return tx;
    const bar = el("div", "js8-tx");
    const row1 = el("div", "js8-row"), row2 = el("div", "js8-row"), row3 = el("div", "js8-row");
    const mins = el("select", "js8-mins");
    for (const m of ARM_MINUTES) {
      const o = el("option", null, `${m} min`);
      o.value = m;
      if (m === 10) o.selected = true;
      mins.appendChild(o);
    }
    const arm = el("button", "btn js8-arm", "Arm TX");
    const disarm = el("button", "btn js8-disarm", "Disarm");
    const why = el("span", "js8-why");
    const queue = el("span", "js8-queue");
    row1.append(mins, arm, disarm, why, queue);

    const toBox = el("span", "js8-to");
    const hb = el("button", "btn js8-hb", "HB");
    hb.title = "One heartbeat now (@HB HEARTBEAT and your grid), on a clear offset in the heartbeat sub-band";
    const cq = el("button", "btn js8-cq", "CQ");
    cq.title = "@ALLCALL CQ CQ CQ and your grid";
    const qs = QUERIES.map((q) => {
      const b = el("button", "btn js8-q", q);
      b.dataset.q = q;
      b.onclick = () => to && sendText(`${to} ${q}`);
      return b;
    });
    row2.append(toBox, hb, cq, ...qs);

    const prefix = el("span", "js8-prefix");
    const input = el("input", "js8-text");
    input.type = "text";
    input.maxLength = MAX_SEND;
    input.placeholder = "Message";
    input.autocomplete = "off";
    const sendB = el("button", "btn js8-send", "Send");
    const offset = el("input", "js8-offset");
    offset.type = "number";
    offset.min = 400; offset.max = 2600; offset.step = 10;
    offset.placeholder = "Offset Hz";
    offset.title = "Audio offset to transmit on, 400-2600 Hz; empty keeps the engine's";
    const speed = el("select", "js8-speed");
    for (const [v, label] of SPEEDS) {
      const o = el("option", null, label);
      o.value = v;
      speed.appendChild(o);
    }
    row3.append(prefix, input, sendB, offset, speed);
    bar.append(row1, row2, row3);

    function sendText(text) {
      if (!api || !pick || !text) return;
      const x = { radio: pick, text };
      const off = Number(offset.value);
      if (offset.value !== "" && Number.isFinite(off)) x.offset_hz = off;
      if (speed.value) x.speed = speed.value;
      api.send("send", x);
    }
    const send = () => {
      let text = input.value.trim().toUpperCase();
      if (!text) return;
      if (to && !text.startsWith(to + " ")) text = `${to} ${text}`;
      sendText(text);
      input.value = "";
    };
    arm.onclick = () => api && pick && api.send("arm", { radio: pick, minutes: Number(mins.value) });
    disarm.onclick = () => api && pick && api.send("disarm", { radio: pick });
    hb.onclick = () => api && pick && api.send("hb", { radio: pick });
    cq.onclick = () => {
      const g = String(((cur() || {}).status || {}).grid || "").slice(0, 4);
      sendText(`@ALLCALL CQ CQ CQ${g ? " " + g : ""}`);
    };
    sendB.onclick = send;
    input.onkeydown = (e) => { if (e.key === "Enter") send(); };
    tx = { bar, mins, arm, disarm, why, queue, toBox, hb, cq, qs, prefix, input, sendB, offset, speed };
    return tx;
  }

  function updateTx(r) {
    const t = txBar(), s = (r && r.status) || {};
    const armed = !!s.armed, off = armed ? "" : "Arm TX first";
    t.mins.hidden = t.arm.hidden = armed;
    t.disarm.hidden = !armed;
    t.arm.disabled = !!s.why_not || !s.connected;
    t.why.textContent = !armed && s.why_not ? `Can't arm: ${s.why_not}` : "";
    t.queue.textContent = s.queue ? `${s.queue} frame(s) queued` : "";
    for (const b of [t.hb, t.cq, t.sendB, t.input]) {
      if (b.dataset.tip === undefined) b.dataset.tip = b.title || "";
      b.disabled = !armed;
      b.title = off || b.dataset.tip;
    }
    for (const b of t.qs) {
      b.disabled = !armed || !to;
      b.title = !armed ? "Arm TX first" : !to ? "Pick a station: click it in Heard, Messages or Band activity" : `${to} ${b.dataset.q}`;
    }
    t.toBox.replaceChildren();
    if (to) {
      t.toBox.append(el("span", "call", `To ${to}`));
      const x = el("button", "chip chip-mini js8-clear", "×");
      x.title = "Clear: send to everyone";
      x.onclick = () => pickTo(null);
      t.toBox.appendChild(x);
    } else {
      t.toBox.append(el("span", "hint", "Pick a station to query"));
    }
    t.prefix.textContent = to ? `${to} ` : "";
    t.input.placeholder = to ? `Message to ${to}` : "Message to all (or pick a station)";
    t.bar.classList.toggle("armed", armed);
    if (armed && !ticker) {
      ticker = setInterval(() => {
        const c = cur(), st = (c && c.status) || {};
        if (!st.armed) { clearInterval(ticker); ticker = null; return; }
        const box = body && body.querySelector(".js8-status");
        if (box) box.replaceWith(statusLine(c));
      }, 1000);
    }
  }

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
      const call = (String(f.text || "").match(FRAME_CALL) || [])[1];
      if (call) {
        tr.classList.add("pickable");
        if (call === to) tr.classList.add("sel");
        tr.onclick = () => pickTo(call);
      }
      tr.appendChild(el("td", "t", hhmmss(f.utc)));
      tr.appendChild(el("td", "n", snr(f.snr)));
      tr.appendChild(el("td", "n", bin));
      tr.appendChild(el("td", "txt", f.text));
    });
    if (!rows.size) tb.insertRow().appendChild(el("td", "empty", "Nothing heard yet.")).colSpan = 4;
    return t;
  }

  function messages(r) {
    const list = el("ul", "js8-msgs");
    const msgs = (r.frames || []).filter((f) => f.kind === "message").slice(-MAX_MSGS).reverse();
    for (const m of msgs) {
      const li = el("li");
      if (m.from) {
        li.classList.add("pickable");
        if (m.from === to) li.classList.add("sel");
        li.onclick = () => pickTo(m.from);
      }
      li.appendChild(el("span", "t", hhmmss(m.utc)));
      li.appendChild(el("b", "from", m.from || "?"));
      li.appendChild(el("span", "to", m.to ? `→ ${m.to}` : ""));
      li.appendChild(el("span", "n", snr(m.snr)));
      li.appendChild(el("span", "txt", m.text));
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
      tr.classList.add("pickable");
      if (h.call === to) tr.classList.add("sel");
      tr.onclick = () => pickTo(h.call);
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

  function render() {
    if (!body) return;
    if (tx && tx.bar.contains(document.activeElement)) {
      // Typing: the bar stays put; only what's around it is rebuilt.
      const root = body.querySelector(".js8call");
      const r = cur();
      if (root && r) {
        root.querySelector(".js8-status").replaceWith(statusLine(r));
        root.querySelector(".js8-grid").replaceWith(panes(r));
        updateTx(r);
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
      const chips = el("div", "js8-radios");
      for (const id of ids) {
        const c = el("button", "chip chip-mini" + (id === pick ? " on" : ""), radios()[id].name || id);
        c.dataset.radio = id;
        c.onclick = () => { pick = id; render(); };
        chips.appendChild(c);
      }
      root.appendChild(chips);
    }
    const r = cur();
    if (r) {
      root.appendChild(statusLine(r));
      root.appendChild(txBar().bar);
      updateTx(r);
      root.appendChild(panes(r));
    }
    body.appendChild(root);
  }

  function panes(r) {
    const grid = el("div", "js8-grid");
    for (const [title, fn] of [["Band activity", activity], ["Messages", messages], ["Heard", heard]]) {
      const p = el("section", "js8-pane");
      p.append(el("h3", null, title), fn(r));
      grid.appendChild(p);
    }
    return grid;
  }

  function upsertHeard(r, f) {
    const call = f.from || (String(f.text || "").match(FRAME_CALL) || [])[1];
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
