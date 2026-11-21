"use strict";

// The JS8 tab: what the station's JS8 engines hear, per radio. Receive only.
//
//   status     engine connected, the rig's frequency and mode, TX disarmed;
//              a red UNSAFE banner if an engine reports TX, auto-reply or
//              heartbeat on (js8_engine.py disarms it at every start)
//   activity   one row per audio offset (50 Hz bins), the latest text there
//   messages   complete decodes: from, to, text, newest first
//   heard      stations heard: call, SNR, grid, when, offset; newest first
//
// Server: addons/js8call.py relays js8_bridge.py: hello {ready, why?,
// radios: {id: {name, status, frames, heard}}}; events {event: "frame" |
// "status", radio, ...}. Every string from the air goes through
// textContent: JS8 text can hold < and >.

(() => {
  const BIN_HZ = 50, MAX_ROWS = 60, MAX_MSGS = 100, FRESH_MS = 10 * 60 * 1000;
  const FRAME_CALL = /^\s*([A-Z0-9]{1,3}[0-9][A-Z0-9/]{0,8}):/;
  let body = null, snap = { ready: false }, pick = null;
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
    box.textContent = `Listening · ${f} ${s.radio_mode || ""} · ${s.callsign || "no call"} ${s.grid || ""} · receive only`;
    return box;
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
      const grid = el("div", "js8-grid");
      for (const [title, fn] of [["Band activity", activity], ["Messages", messages], ["Heard", heard]]) {
        const p = el("section", "js8-pane");
        p.append(el("h3", null, title), fn(r));
        grid.appendChild(p);
      }
      root.appendChild(grid);
    }
    body.appendChild(root);
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
    mount(b) { body = b; render(); },
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
      const up = rs.some((r) => r.status && r.status.connected);
      const n = fresh();
      return { dot: up ? "ok" : "off", badge: n || undefined,
               title: up ? `JS8: ${n} station(s) heard in the last 10 minutes` : "JS8: no engine connected" };
    },
    card() {
      if (!snap.ready) return { state: "off", lines: [snap.why || "not set up"] };
      return { state: "on", lines: Object.entries(radios()).map(([id, r]) =>
        `${r.name || id}: ${(r.status && r.status.connected) ? "listening" : "engine down"}, ${(r.heard || []).length} heard`) };
    },
  });
})();
