"use strict";

// OpenGD77 add-on panel (addons/opengd77.py): a TYT MD-UV380/390 running
// OpenGD77, controlled like the radio itself and programmed like its CPS.
//
//   Radio     what it is on (read off its screen), a picture of the screen,
//             zones and channels to jump to, VFO A/B to tune and store, the
//             channel/VFO mode. OpenGD77 has no CAT, so each change is
//             written into the radio's memory and the radio restarts on it
//             (about 8 s); nothing here keys the transmitter.
//   Settings  the radio's own Options menus (power, squelch, backlight,
//             sound, DMR filters, scan, timezone), applied the same way;
//             the clock is set without a restart; Restore puts back the
//             memory from before a change.
//   Codeplug  channels, zones, contacts, group lists, radio ID and boot
//             text as forms over the qdmr codeplug the server reads;
//             Write goes through the server's checked dmrconf write
//             (encode, backup, write, read-back). Raw YAML stays available.
//
// Messages: do "screen" | "apply" {settings, vfos, goto} | "memory_refresh" |
// "clock_sync" | "memory_restore" {file, confirm} | "codeplug_read" |
// "codeplug_get" | "codeplug_model" | "codeplug_write_model" {model, confirm} |
// "codeplug_load" {yaml, name} | "codeplug_render" {model} |
// "codeplug_write" {yaml, confirm}.

(() => {
  // qdmr's channel power names (README: it writes only these levels)
  const QPOWER = [["", "Default (master)"], ["Min", "Min · 50 mW"], ["Low", "Low · 500 mW"], ["Mid", "Mid · 2 W"],
    ["High", "High · 10 W"], ["Max", "Max · +W-"]];
  const TIMEOUTS = ["infinite", "30 s", "45 s", "60 s", "90 s", "120 s", "180 s", "300 s"];
  const OFFSETS = [["0", "simplex"], ["-600000", "−0.6"], ["600000", "+0.6"], ["-5000000", "−5"], ["5000000", "+5"]];
  const RESTART_S = 9;                       // what a change usually takes, for the progress bar

  let S = {}, api = null, root = null, live = null, editorWanted = false, busyTimer = null;
  const ui = {
    tab: "radio", zone: null, vfo: null, vdraft: [{}, {}], sdraft: {},
    cp: null, cpDirty: false, cpTab: "channels", cpSel: { channels: 0, zones: 0, contacts: 0, groupLists: 0 },
    cpFilter: "", yaml: false, modelAsked: null, busySince: 0,
  };

  const q = (s) => root.querySelector(s);
  const qa = (s) => [...root.querySelectorAll(s)];
  const connected = () => !!S.status?.connected;
  const busy = () => !!S.status?.busy;
  const facts = () => S.radio || {};
  const ctl = () => S.control || {};
  const canChange = () => !busy() && !!S.info && !!ctl().settings && !ctl().why_not;
  const mhz = (hz, d = 5) => (hz / 1e6).toFixed(d);
  const hhmm = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const opt = (v, label, sel) => `<option value="${esc(v)}"${sel ? " selected" : ""}>${esc(label)}</option>`;
  const powerName = (lvl) => (lvl === 0 ? "Master" : (facts().power_names || [])[lvl - 1] || `level ${lvl}`);
  const sqName = (v) => (v ? `${(v - 1) * 5}%` : "Default");
  const inBand = (hz) => !!hz && (facts().bands_hz || []).some(([lo, hi]) => hz >= lo && hz <= hi);
  const typing = (sel) => {
    const a = document.activeElement;
    return a && root?.contains(a) && a.closest(sel) && /INPUT|SELECT|TEXTAREA/.test(a.tagName);
  };

  function offsetText(rx, tx, rxOnly) {
    if (rxOnly) return "RX only";
    const d = tx - rx;
    if (!d) return "simplex";
    return `${d > 0 ? "+" : "−"}${(Math.abs(d) / 1e6).toFixed(3)}`;
  }

  function toneText(c, short) {
    if (!c) return "";
    if (c.mode === "DMR") return `CC${c.colour_code} · TS${c.timeslot}`;
    if (short && c.rx_tone && c.rx_tone === c.tx_tone) return `TSQ ${c.rx_tone}`;
    const t = [c.rx_tone && `${short ? "R" : "RX"} ${c.rx_tone}`, c.tx_tone && `${short ? "T" : "TX"} ${c.tx_tone}`].filter(Boolean);
    return t.join(short ? " " : " · ") || "no tone";
  }

  // ---------------------------------------------------------------- what the radio is on

  function vfoOnScreen() {
    const n = S.now, v = S.vfos;
    if (!n || n.mode === "channel" || !v) return null;
    // the VFO screen reads ">R 146.52000 MHz" then the TX line (mac-shack, 2026-10-20): the first frequency is RX
    const x = ((n.lines || []).join(" ").match(/\d{3}\.\d{3,5}/) || [])[0];
    if (!x) return null;
    const hits = [0, 1].filter((i) => v[i] && Math.abs(parseFloat(x) * 1e6 - v[i].rx_hz) < 600);
    if (!hits.length) return null;
    const stored = ctl().settings?.vfo;
    return hits.includes(stored) ? stored : hits[0];
  }

  function current() {
    const n = S.now, lay = S.layout;
    if (n?.mode === "channel") {
      const zones = lay?.zones || [];
      const zi = zones.findIndex((z) => z.name === n.zone);
      const slot = n.slot ?? (zi >= 0 ? zones[zi].slots[n.number - 1] : n.number);
      return { mode: "channel", zone: zi >= 0 ? zi : "all", index: zi >= 0 ? n.number - 1 : slot, slot,
        ch: lay?.channels?.[String(slot)] || null, n };
    }
    const vi = vfoOnScreen();
    if (vi !== null) return { mode: "vfo", vfo: vi, n };
    return { mode: "other", n };
  }

  function contactLine(cur) {
    // what else the channel screen shows: the talkgroup or contact (DMR), or nothing on FM
    const n = cur.n, skip = new Set([...(n.bar || [])]);
    const rest = (n.lines || []).filter((l) => l !== n.channel && !/ Ch:\d+$/.test(l) && !l.split(/\s+/).every((w) => skip.has(w)));
    return rest[0] || "";
  }

  function badges(cur) {
    const out = [];
    const c = cur.mode === "channel" ? cur.ch : cur.mode === "vfo" ? S.vfos?.[cur.vfo] : null;
    if (c?.mode) out.push(`<span class="badge ${c.mode === "DMR" ? "badge-dmr" : "badge-fm"}">${c.mode}</span>`);
    if (c && c.mode === "DMR") out.push(`<span class="badge">CC ${esc(c.colour_code)}</span><span class="badge">TS ${esc(c.timeslot)}</span>`);
    if (c && c.mode !== "DMR" && c.bandwidth) out.push(`<span class="badge">${esc(c.bandwidth)}</span>`);
    if (c?.rx_only) out.push(`<span class="badge badge-rx">RX ONLY</span>`);
    if (cur.n?.power) out.push(`<span class="badge badge-pwr">${esc(cur.n.power)}</span>`);
    const batt = (cur.n?.bar || []).find((w) => /^\d+%$/.test(w));
    if (batt) out.push(`<span class="badge" title="Battery">${esc(batt)}</span>`);
    return out.join("");
  }

  function renderFront() {
    const el = q(".gd-front");
    if (!connected() && !busy()) {
      el.innerHTML = `<div class="gd-head"><span class="label">On the radio</span></div>
        <div class="gd-big dim">${esc(S.status?.error || "Not connected")}</div>
        <div class="gd-note">Switch the radio on; it shows up on USB only while it is on.</div>`;
      return;
    }
    const cur = current(), st = ctl().settings;
    let main;
    if (cur.mode === "channel") {
      const c = cur.ch;
      main = `<div class="gd-zone-line">${esc(cur.n.zone)} · CH ${cur.n.number}</div>
        <div class="gd-big" title="${esc(cur.n.channel || "")}">${esc(cur.n.channel || c?.name || `Channel ${cur.n.number}`)}</div>
        ${contactLine(cur) ? `<div class="gd-freqs"><span><span class="k">${c?.mode === "DMR" ? "TALKGROUP" : "CONTACT"}</span><span class="v">${esc(contactLine(cur))}</span></span></div>` : ""}
        ${c ? `<div class="gd-freqs"><span><span class="k">RX</span><span class="v">${mhz(c.rx_hz)}</span></span>
          <span><span class="k">TX</span><span class="v">${mhz(c.tx_hz)}</span> <span class="o">${esc(offsetText(c.rx_hz, c.tx_hz, c.rx_only))}</span></span>
          <span class="o">${esc(toneText(c))}</span></div>` : ""}`;
    } else if (cur.mode === "vfo") {
      const v = S.vfos[cur.vfo];
      main = `<div class="gd-zone-line">VFO ${"AB"[cur.vfo]}</div>
        <div class="gd-big mono">${mhz(v.rx_hz)}<span class="unit">MHz</span></div>
        <div class="gd-freqs"><span><span class="k">TX</span><span class="v">${mhz(v.tx_hz)}</span> <span class="o">${esc(offsetText(v.rx_hz, v.tx_hz, v.rx_only))}</span></span>
          <span class="o">${esc(toneText(v))}</span></div>`;
    } else {
      const lines = cur.n?.lines || [];
      main = `<div class="gd-zone-line">Screen</div>
        <div class="gd-big dim">${esc(lines.length ? lines.slice(0, 3).join(" · ") : busy() ? "Restarting…" : "Reading the radio's screen…")}</div>
        <div class="gd-note">Not on a channel or VFO screen (a menu, a call or a message). The picture shows what it shows.</div>`;
    }
    const mode = cur.mode === "channel" ? "channel" : cur.mode === "vfo" ? "vfo" : st?.mode;
    const dis = canChange() ? "" : " disabled";
    const zoneSlots = cur.mode === "channel"
      ? (cur.zone === "all" ? S.layout?.slots || [] : S.layout?.zones?.[cur.zone]?.slots || []) : [];
    const activeVfo = cur.mode === "vfo" ? cur.vfo : st?.vfo;
    el.innerHTML = `<div class="gd-head"><span class="label">On the radio</span><span class="spacer"></span>
        <span class="badges">${badges(cur)}</span></div>
      ${main}
      <div class="gd-front-foot">
        <div class="seg gd-mode" title="Channel or VFO mode (the radio restarts on it)">
          <button class="chip${mode === "channel" ? " on" : ""}" data-mode="channel"${dis}>CHANNEL</button>
          <button class="chip${mode === "vfo" ? " on" : ""}" data-mode="vfo"${dis}>VFO</button></div>
        ${cur.mode === "channel" && zoneSlots.length > 1 ? `<div class="gd-knob" title="Previous / next channel in this zone, like the knob">
          <button class="chip" data-step="-1"${dis}>◀</button><button class="chip" data-step="1"${dis}>▶</button></div>` : ""}
        ${mode === "vfo" ? `<div class="seg" title="Which VFO the radio uses">
          ${[0, 1].map((i) => `<button class="chip${activeVfo === i ? " on" : ""}" data-avfo="${i}"${dis}>VFO ${"AB"[i]}</button>`).join("")}</div>` : ""}
        <span class="spacer"></span>
        <span class="gd-since">${cur.n?.at ? `since ${hhmm(cur.n.at)}` : ""}</span>
      </div>`;
    el.querySelectorAll("[data-mode]").forEach((b) => (b.onclick = () => {
      if (b.dataset.mode === mode) return;
      apply({ settings: { mode: b.dataset.mode } });
    }));
    el.querySelectorAll("[data-avfo]").forEach((b) => (b.onclick = () => {
      if (+b.dataset.avfo === activeVfo && cur.mode === "vfo") return;
      apply({ settings: { mode: "vfo", vfo: +b.dataset.avfo } });
    }));
    el.querySelectorAll("[data-step]").forEach((b) => (b.onclick = () => {
      const pos = cur.zone === "all" ? zoneSlots.indexOf(cur.slot) : cur.index;
      const next = (pos + +b.dataset.step + zoneSlots.length) % zoneSlots.length;
      apply({ goto: { zone: cur.zone, index: cur.zone === "all" ? zoneSlots[next] : next } });
    }));
  }

  // ---------------------------------------------------------------- zones and channels

  function renderZones() {
    const el = q(".gd-zones"), lay = S.layout;
    if (!lay) {
      el.innerHTML = `<div class="gd-head"><span class="label">Zones and channels</span></div>
        <div class="gd-empty">${!connected() ? "Not connected" : !S.radio ? "The channel picker needs the radio server restarted (it runs the previous add-on)." : "Reading the radio's zone table…"}</div>`;
      return;
    }
    const cur = current();
    const zones = lay.zones || [];
    if (ui.zone === null && cur.mode === "channel") ui.zone = cur.zone;
    const sel = ui.zone === "all" || (ui.zone !== null && ui.zone < zones.length) ? ui.zone : zones.length ? 0 : "all";
    const slots = sel === "all" ? lay.slots : zones[sel].slots;
    const dis = canChange() ? "" : " disabled";
    el.innerHTML = `<div class="gd-head"><span class="label">Zones and channels</span>
        <span class="wl-dim">${slots.length} channel${slots.length === 1 ? "" : "s"} · click one to go to it</span></div>
      <div class="gd-zone-chips">${zones.map((z, i) => `<button class="chip${sel === i ? " on" : ""}" data-zone="${i}">${esc(z.name)}</button>`).join("")}
        <button class="chip${sel === "all" ? " on" : ""}" data-zone="all">All Channels</button></div>
      <div class="gd-chans">${slots.map((slot, i) => {
        const c = lay.channels?.[String(slot)] || {};
        const isCur = cur.mode === "channel" && cur.slot === slot && cur.zone === sel;
        const idx = sel === "all" ? slot : i;
        return `<button class="gd-ch${isCur ? " cur" : ""}" data-goto="${idx}"${dis} title="${esc(c.name || "")}${c.rx_hz ? ` · ${mhz(c.rx_hz)} MHz` : ""}">
          <b>${esc(c.name || `Slot ${slot}`)}</b>
          <span>${c.rx_hz ? mhz(c.rx_hz, 4) : "?"} <em class="${c.mode === "DMR" ? "dmr" : "fm"}">${esc(c.mode || "")}</em></span>
          <span>${esc(c.rx_hz ? `${offsetText(c.rx_hz, c.tx_hz, c.rx_only)} · ${toneText(c, true)}` : "")}</span></button>`;
      }).join("")}</div>`;
    el.querySelectorAll("[data-zone]").forEach((b) => (b.onclick = () => {
      ui.zone = b.dataset.zone === "all" ? "all" : +b.dataset.zone;
      renderZones();
    }));
    el.querySelectorAll("[data-goto]").forEach((b) => (b.onclick = () => {
      if (b.classList.contains("cur")) return;
      apply({ goto: { zone: sel, index: +b.dataset.goto } });
    }));
  }

  // ---------------------------------------------------------------- VFO editor

  function vfoBase(i) {
    const stored = ctl().vfo?.[i] || {}, liveV = S.vfos?.[i];
    const b = { ...stored };
    if (liveV) {
      Object.assign(b, { rx_hz: liveV.rx_hz, tx_hz: liveV.tx_hz, mode: liveV.mode, rx_only: liveV.rx_only });
      if (liveV.mode === "DMR") Object.assign(b, { colour_code: liveV.colour_code, timeslot: liveV.timeslot });
      else Object.assign(b, { rx_tone: liveV.rx_tone, tx_tone: liveV.tx_tone, wide: liveV.bandwidth === "25 kHz" });
    }
    return b;
  }

  function toneOptions(sel) {
    const f = facts();
    return opt("", "None", !sel)
      + `<optgroup label="CTCSS">${(f.ctcss || []).map((t) => opt(t.toFixed(1), `${t.toFixed(1)} Hz`, sel === t.toFixed(1))).join("")}</optgroup>`
      + `<optgroup label="DCS">${(f.dcs || []).flatMap((d) => [opt(`D${d}N`, `D${d}N`, sel === `D${d}N`), opt(`D${d}I`, `D${d}I`, sel === `D${d}I`)]).join("")}</optgroup>`;
  }

  function renderVfo(force) {
    const el = q(".gd-vfo");
    if (!force && typing(".gd-vfo")) return;
    if (!S.vfos && !ctl().vfo) {
      el.innerHTML = `<div class="gd-head"><span class="label">VFO</span></div><div class="gd-empty">Not read yet</div>`;
      return;
    }
    const cur = current();
    if (ui.vfo === null) ui.vfo = cur.mode === "vfo" ? cur.vfo : ctl().settings?.vfo ?? 0;
    const i = ui.vfo, base = vfoBase(i), d = { ...base, ...ui.vdraft[i] };
    const f = facts(), dirty = Object.keys(ui.vdraft[i]).length > 0;
    const ch = (k) => (k in ui.vdraft[i] ? "changed" : "");
    const isDmr = d.mode === "DMR";
    el.innerHTML = `<div class="gd-head"><span class="label">VFO</span>
        <div class="seg">${[0, 1].map((k) => `<button class="chip${k === i ? " on" : ""}" data-v="${k}">VFO ${"AB"[k]}${Object.keys(ui.vdraft[k]).length ? " •" : ""}</button>`).join("")}</div>
        <span class="spacer"></span><span class="wl-dim">${cur.mode === "vfo" && cur.vfo === i ? "the radio is on this VFO" : ""}</span></div>
      <div class="gd-form">
        <label class="${ch("rx_hz")}">RX MHz</label><div class="row"><input class="gd-rx" inputmode="decimal" value="${d.rx_hz ? mhz(d.rx_hz) : ""}"></div>
        <label class="${ch("tx_hz")}">TX MHz</label><div class="row"><input class="gd-tx" inputmode="decimal" value="${d.tx_hz ? mhz(d.tx_hz) : ""}">
          <div class="seg">${OFFSETS.map(([o, l]) => `<button class="chip" data-off="${o}" title="TX = RX ${l === "simplex" ? "" : l + " MHz"}">${l}</button>`).join("")}</div></div>
        <label class="${ch("mode")}">Mode</label><div class="row"><div class="seg">
          ${["FM", "DMR"].map((m) => `<button class="chip${d.mode === m ? " on" : ""}" data-m="${m}">${m}</button>`).join("")}</div></div>
        ${isDmr ? `
        <label class="${ch("colour_code")}">Colour code</label><div class="row"><select class="gd-cc">${Array.from({ length: 16 }, (_, k) => opt(k, `CC ${k}`, d.colour_code === k)).join("")}</select></div>
        <label class="${ch("timeslot")}">Timeslot</label><div class="row"><div class="seg">
          ${[1, 2].map((t) => `<button class="chip${d.timeslot === t ? " on" : ""}" data-ts="${t}">TS${t}</button>`).join("")}</div></div>` : `
        <label class="${ch("rx_tone")}">RX tone</label><div class="row"><select class="gd-rxt">${toneOptions(d.rx_tone)}</select></div>
        <label class="${ch("tx_tone")}">TX tone</label><div class="row"><select class="gd-txt">${toneOptions(d.tx_tone)}</select>
          <button class="chip chip-mini" data-copytone title="TX tone = RX tone">= RX</button></div>
        <label class="${ch("wide")}">Bandwidth</label><div class="row"><div class="seg">
          ${[[false, "12.5 kHz"], [true, "25 kHz"]].map(([w, l]) => `<button class="chip${!!d.wide === w ? " on" : ""}" data-w="${w}">${l}</button>`).join("")}</div></div>`}
        <label class="${ch("power")}">Power</label><div class="row"><select class="gd-pw">
          ${Array.from({ length: 11 }, (_, k) => opt(k, k === 0 ? `Master (${powerName((ctl().settings?.power ?? -1) + 1)})` : powerName(k), d.power === k)).join("")}</select></div>
        <label class="${ch("sql")}">Squelch</label><div class="row"><select class="gd-sq">
          ${Array.from({ length: 22 }, (_, k) => opt(k, k ? sqName(k) : "Band default", d.sql === k)).join("")}</select></div>
        <label class="${ch("step")}">Step</label><div class="row"><select class="gd-st">
          ${(f.steps_hz || []).map((s, k) => opt(k, `${s / 1000} kHz`, d.step === k)).join("")}</select></div>
        <label class="${ch("rx_only")}">RX only</label><div class="row"><input type="checkbox" class="gd-ro"${d.rx_only ? " checked" : ""}>
          <span class="wl-dim">the radio won't transmit on it</span></div>
      </div>
      <div class="gd-actions">
        <button class="chip go gd-v-tune"${canChange() ? "" : " disabled"} title="Write this VFO and put the radio on it (it restarts)">Tune radio to VFO ${"AB"[i]}</button>
        <button class="chip gd-v-store"${canChange() && dirty ? "" : " disabled"} title="Write this VFO, stay where the radio is">Store only</button>
        <button class="chip chip-mini gd-v-reset"${dirty ? "" : " disabled"}>Undo edits</button>
        <span class="note gd-v-err"></span>
      </div>`;
    const set = (k, v) => {
      if (v === base[k]) delete ui.vdraft[i][k]; else ui.vdraft[i][k] = v;
    };
    const err = (msg) => { el.querySelector(".gd-v-err").textContent = msg || ""; };
    const freqIn = (cls, key) => {
      const inp = el.querySelector(cls);
      inp.onchange = () => {
        const hz = Math.round(parseFloat(inp.value) * 1e5) * 10;
        inp.classList.toggle("bad", !inBand(hz));
        if (!inBand(hz)) return err(`${inp.value} MHz is outside the radio's bands`);
        err("");
        if (key === "rx_hz" && d.tx_hz === d.rx_hz) set("tx_hz", hz);       // simplex follows RX
        set(key, hz);
        renderVfo(true);
      };
    };
    freqIn(".gd-rx", "rx_hz");
    freqIn(".gd-tx", "tx_hz");
    el.querySelectorAll("[data-v]").forEach((b) => (b.onclick = () => { ui.vfo = +b.dataset.v; renderVfo(true); }));
    el.querySelectorAll("[data-off]").forEach((b) => (b.onclick = () => {
      const hz = d.rx_hz + +b.dataset.off;
      if (!inBand(hz)) return err("that offset leaves the band");
      set("tx_hz", hz); renderVfo(true);
    }));
    el.querySelectorAll("[data-m]").forEach((b) => (b.onclick = () => { set("mode", b.dataset.m); renderVfo(true); }));
    el.querySelectorAll("[data-ts]").forEach((b) => (b.onclick = () => { set("timeslot", +b.dataset.ts); renderVfo(true); }));
    el.querySelectorAll("[data-w]").forEach((b) => (b.onclick = () => { set("wide", b.dataset.w === "true"); renderVfo(true); }));
    const sel = (cls, k, num = true) => {
      const x = el.querySelector(cls);
      if (x) x.onchange = () => { set(k, num ? +x.value : x.value); renderVfo(true); };
    };
    sel(".gd-cc", "colour_code"); sel(".gd-rxt", "rx_tone", false); sel(".gd-txt", "tx_tone", false);
    sel(".gd-pw", "power"); sel(".gd-sq", "sql"); sel(".gd-st", "step");
    const cp = el.querySelector("[data-copytone]");
    if (cp) cp.onclick = () => { set("tx_tone", d.rx_tone || ""); renderVfo(true); };
    el.querySelector(".gd-ro").onchange = (e) => { set("rx_only", e.target.checked); renderVfo(true); };
    el.querySelector(".gd-v-reset").onclick = () => { ui.vdraft[i] = {}; renderVfo(true); };
    const send = (tune) => {
      const m = { vfos: Object.keys(ui.vdraft[i]).length ? { [i]: { ...ui.vdraft[i] } } : {} };
      if (tune) m.settings = { mode: "vfo", vfo: i };
      if (apply(m)) ui.vdraft[i] = {};
    };
    el.querySelector(".gd-v-tune").onclick = () => send(true);
    el.querySelector(".gd-v-store").onclick = () => send(false);
  }

  // ---------------------------------------------------------------- the screen

  function draw(b64) {
    const [w, h] = S.screen_size || [160, 128];
    const raw = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    if (raw.length < w * h * 2) return;
    const cv = q(".gd-screen"), ctx = cv.getContext("2d");
    const img = ctx.createImageData(w, h);
    for (let i = 0; i < w * h; i++) {
      const p = (raw[2 * i] << 8) | raw[2 * i + 1];       // big-endian 16-bit colour
      let r = ((p >> 11) & 0x1f) << 3, g = ((p >> 5) & 0x3f) << 2, b = (p & 0x1f) << 3;
      if (S.screen_bgr) [r, b] = [b, r];
      img.data.set([r, g, b, 255], 4 * i);
    }
    ctx.putImageData(img, 0, 0);
  }

  function stopLive() {
    if (live) clearInterval(live);
    live = null;
    if (root) q(".gd-live")?.classList.remove("on");
  }

  function renderScreenCard() {
    const can = connected() && S.info?.screen;
    q(".gd-grab").disabled = q(".gd-live").disabled = !can;
    q(".gd-clock").disabled = !canChange();
    if (!can) stopLive();
  }

  // ---------------------------------------------------------------- settings

  function tzOptions(sel) {
    const odd = [-570, -210, 210, 270, 330, 345, 390, 525, 570, 630, 765, 825];   // the world's half / quarter hours
    const out = [];
    for (let m = -12 * 60; m <= 14 * 60; m += 15) {
      if (m % 60 && !odd.includes(m) && m !== sel) continue;
      const s = m < 0 ? "−" : "+", a = Math.abs(m);
      out.push(opt(m, `UTC${s}${String(Math.floor(a / 60)).padStart(2, "0")}:${String(a % 60).padStart(2, "0")}${m === -420 ? " (Arizona)" : ""}`, m === sel));
    }
    return out.join("");
  }

  function settingControl(s, v) {
    const vals = s.values;
    if (Array.isArray(vals[0])) return `<select data-set="${s.key}">${vals.map(([x, l]) => opt(x, l, x === v)).join("")}</select>`;
    const [lo, hi] = vals;
    const names = s.key.startsWith("squelch") ? sqName : String;
    if (s.key.startsWith("brightness")) return `<div class="rng"><input type="range" min="${lo}" max="${hi}" value="${v}" data-set="${s.key}"><b>${v}%</b></div>`;
    return `<select data-set="${s.key}">${Array.from({ length: hi - lo + 1 }, (_, k) => opt(lo + k, names(lo + k), lo + k === v)).join("")}</select>`;
  }

  function renderSettings(force) {
    const el = q(".gd-settings");
    if (!force && typing(".gd-settings")) return;
    const c = ctl(), st = c.settings, schema = facts().schema || [];
    if (!st) {
      el.innerHTML = `<div class="gd-empty">${esc(c.why_not || (connected() ? "Reading the radio's settings…" : "Not connected"))}</div>`;
      return;
    }
    const d = { ...st, ...ui.sdraft }, n = Object.keys(ui.sdraft).length, dis = canChange() ? "" : " disabled";
    const groups = [...new Set(schema.map((s) => s.group))];
    const row = (key, label, control) => `<label class="gd-set${key && key in ui.sdraft ? " changed" : ""}"><span>${esc(label)}</span>${control}</label>`;
    const backups = c.backups || [];
    el.innerHTML = `${c.why_not ? `<div class="gd-probs">Changing settings is off: ${esc(c.why_not)}.</div>` : ""}
      <div class="gd-note${c.saved ? "" : " warn"}" style="margin-bottom:12px">${c.saved
        ? `Read from the radio at ${hhmm(c.at)}, after it saved: what it is on now.`
        : "Shown as the radio last saved them (at power-off, or a change from here). A setting changed at the keypad since then shows its old value: Refresh reads it now (the radio saves first, no restart)."}
        Applying writes only the settings you changed and restarts the radio (about 8 s).</div>
      <div class="gd-sets">
        ${groups.map((g) => `<div class="gd-set-group"><span class="label">${esc(g)}</span>
          ${schema.filter((s) => s.group === g).map((s) => row(s.key, s.label, settingControl(s, d[s.key]))).join("")}
          ${g === "Radio" ? row("", "+W- (user power)", `<span class="wl-dim">${st.user_power_mw ? `${st.user_power_mw} mW` : "—"}</span>`)
            + row("", "GPS", `<span class="wl-dim">${esc(st.gps)}</span>`) : ""}</div>`).join("")}
        <div class="gd-set-group"><span class="label">Time</span>
          ${row("tz_minutes", "Timezone", `<select data-tz>${tzOptions(d.tz_minutes)}</select>`)}
          ${row("tz_local", "Show local time", `<span><input type="checkbox" data-tzl${d.tz_local ? " checked" : ""}></span>`)}
          <div class="gd-actions"><button class="chip chip-mini gd-clock2"${dis}>Set the radio's clock now</button>
            <span class="note">from this server's clock, UTC; no restart</span></div></div>
      </div>
      <div class="gd-sticky">
        <b class="${n ? "changed" : "wl-dim"}" style="font:600 12px var(--mono)">${n ? `${n} change${n === 1 ? "" : "s"}` : "No changes"}</b>
        <button class="chip go gd-s-apply"${n && canChange() ? "" : " disabled"}>Apply to radio (it restarts)</button>
        <button class="chip chip-mini gd-s-undo"${n ? "" : " disabled"}>Discard</button>
        <span class="spacer"></span>
        <button class="chip chip-mini gd-s-refresh"${dis} title="The radio saves its settings, then they are read (no restart)">Refresh from radio</button>
        ${backups.length ? `<select class="gd-s-backup" title="Memory backups, one before every change">
          ${[...backups].reverse().map((b, k) => opt(b, `${k === 0 ? "before the last change · " : ""}${b.slice(0, 17).replace("_", " ")}`, k === 0)).join("")}</select>
          <button class="chip chip-mini warn gd-s-restore"${dis}>Restore</button>` : ""}
      </div>`;
    const set = (k, v) => { if (v === st[k]) delete ui.sdraft[k]; else ui.sdraft[k] = v; renderSettings(true); };
    el.querySelectorAll("[data-set]").forEach((x) => {
      if (x.type === "range") {
        x.oninput = () => { x.nextElementSibling.textContent = `${x.value}%`; };
        x.onchange = () => set(x.dataset.set, +x.value);
      } else x.onchange = () => set(x.dataset.set, +x.value);
    });
    el.querySelector("[data-tz]").onchange = (e) => set("tz_minutes", +e.target.value);
    el.querySelector("[data-tzl]").onchange = (e) => set("tz_local", e.target.checked);
    el.querySelector(".gd-s-apply").onclick = () => {
      const changes = { ...ui.sdraft };
      const names = Object.keys(changes).map((k) => (schema.find((s) => s.key === k) || {}).label || { tz_minutes: "Timezone", tz_local: "Show local time" }[k] || k).join(", ");
      if (apply({ settings: changes }, `Apply to the radio: ${names}? It restarts on the new settings (about 8 s).`)) ui.sdraft = {};
    };
    el.querySelector(".gd-s-undo").onclick = () => { ui.sdraft = {}; renderSettings(true); };
    el.querySelector(".gd-s-refresh").onclick = () => api.send("memory_refresh");
    el.querySelector(".gd-clock2").onclick = () => api.send("clock_sync");
    const rb = el.querySelector(".gd-s-restore");
    if (rb) rb.onclick = () => {
      const file = el.querySelector(".gd-s-backup").value;
      if (!confirm(`Restore the radio's settings, VFOs and channel from ${file}? It restarts on them.`)) return;
      api.send("memory_restore", { file, confirm: true });
    };
  }

  function apply(m, ask) {
    if (!canChange()) {
      toast(busy() ? `MD-UV390 is busy: ${S.status.busy}` : ctl().why_not || "The radio isn't ready", "warn");
      return false;
    }
    if (ask && !confirm(ask)) return false;
    api.send("apply", m);
    return true;
  }

  // ---------------------------------------------------------------- codeplug model helpers

  const isTag = (v) => v && typeof v === "object" && "$tag" in v;
  const txt = (v) => (isTag(v) ? "" : v ?? "");
  const kindOf = (it) => (it.fm ? "fm" : it.dmr ? "dmr" : Object.keys(it)[0]);
  const bodyOf = (it) => it[kindOf(it)];
  const M = () => ui.cp?.model;
  const list = (key) => (M()[key] ||= []);
  const hzOf = (s) => {
    const m = /^([\d.]+)\s*(MHz|kHz|Hz)?$/.exec(String(txt(s)).trim());
    return m ? Math.round(parseFloat(m[1]) * ({ MHz: 1e6, kHz: 1e3, Hz: 1 }[m[2] || "MHz"])) : null;
  };
  const fmtHz = (hz) => `${+(hz / 1e6).toFixed(6)} MHz`;
  function toneOf(v) {
    if (!v || typeof v !== "object") return "";
    if (v.ctcss) return parseFloat(v.ctcss).toFixed(1);
    if (v.dcs) {
      const m = /^([ni])?0*(\d+)$/i.exec(String(v.dcs).trim());
      return m ? `D${m[2].padStart(3, "0")}${(m[1] || "n").toLowerCase() === "i" ? "I" : "N"}` : String(v.dcs);
    }
    return "";
  }
  const toneTo = (t) => (!t ? null : t.startsWith("D") ? { dcs: `${t.endsWith("I") ? "i" : "n"}${t.slice(1, 4)}` } : { ctcss: `${t} Hz` });
  function nextId(prefix, key) {
    const used = new Set(list(key).map((it) => (it.id ? it.id : bodyOf(it)?.id)));
    let n = 1;
    while (used.has(`${prefix}${n}`)) n++;
    return `${prefix}${n}`;
  }
  const chName = (id) => { const it = list("channels").find((c) => bodyOf(c).id === id); return it ? bodyOf(it).name : id; };
  const ctName = (id) => { const it = list("contacts").find((c) => bodyOf(c).id === id); return it ? bodyOf(it).name : id; };
  const dirty = () => { ui.cpDirty = true; renderCodeplugBar(); };

  const OGD = () => ({ scanZoneSkip: "false", scanAllSkip: "false", beep: "true", powerSave: "true", location: "",
    talkerAliasTS1: "None", talkerAliasTS2: "None" });
  function newChannel(kind, from) {
    const b = { id: from?.id || nextId("ch", "channels"), name: from?.name || "New channel",
      rxFrequency: from?.rxFrequency || "146.52 MHz", txFrequency: from?.txFrequency || "146.52 MHz",
      timeout: kind === "dmr" ? "90 s" : "infinite", rxOnly: from?.rxOnly || "false", vox: "off",
      openGD77: from?.openGD77 || OGD() };
    if (kind === "fm") {
      Object.assign(b, { squelch: { $tag: "default", $value: "" }, admit: "Always", rxTone: null, txTone: null,
        bandwidth: "Wide", extended: { talkaround: "false", reverseBurst: "false" } });
    } else {
      const g = list("groupLists")[0];
      Object.assign(b, { admit: "Always", colorCode: "1", timeSlot: "TS1", radioId: { $tag: "default", $value: "" },
        groupList: g ? g.id : null, extended: { talkaround: "false", privateCallConfirm: "false", sms: "true",
          smsConfirm: "false", dataConfirm: "true", dcdm: "false", loneWorker: "false" } });
    }
    b.power = from?.power ?? { $tag: "default", $value: "" };
    return { [kind]: b };
  }

  // ---------------------------------------------------------------- codeplug view

  function renderCodeplugBar() {
    const cp = S.codeplug, b = S.status?.busy;
    const el = root && q(".gd-cp-bar");
    if (!el) return;
    const w = el.querySelector(".gd-cp-write");
    el.querySelector(".gd-cp-read").disabled = !!b || !connected();
    w.disabled = !!b || !connected() || !ui.cp?.model || !ui.cpDirty;
    w.textContent = ui.cpDirty ? "Write changes to radio" : "Write to radio";
    el.querySelector(".gd-cp-dl").disabled = !cp;
    el.querySelector(".gd-cp-sum").textContent = cp
      ? `${cp.file} · ${cp.kind} ${hhmm(cp.at)} · ${cp.channels.length} channels${ui.cpDirty ? " · edited, not written" : ""}`
      : "Not read yet";
    el.querySelector(".gd-cp-yaml").classList.toggle("on", ui.yaml);
  }

  function renderCodeplug() {
    const el = q(".gd-cp-body");
    renderCodeplugBar();
    const cp = S.codeplug, probs = cp?.problems || [];
    const head = probs.length ? `<div class="gd-probs">qdmr couldn't decode this codeplug cleanly, so writing is blocked: ${probs.slice(0, 3).map(esc).join(" · ")}${probs.length > 3 ? ` (+${probs.length - 3} more)` : ""}</div>` : "";
    if (ui.yaml) {
      el.innerHTML = `${head}<div class="gd-note" style="margin-bottom:8px">The codeplug as qdmr YAML. ${ui.cpDirty ? "<b class='changed'>Form edits not written yet are not in it.</b>" : ""}</div>
        <textarea class="gd-yaml" spellcheck="false"></textarea>
        <div class="gd-actions"><button class="chip chip-mini gd-y-load">Load the radio's file</button>
          <button class="chip warn gd-y-write">Write this YAML to radio</button>
          <span class="note">checked, backed up and read back by the server</span></div>`;
      el.querySelector(".gd-y-load").onclick = () => { editorWanted = true; api.send("codeplug_get"); };
      el.querySelector(".gd-y-write").onclick = () => {
        const yaml = el.querySelector(".gd-yaml").value;
        if (!yaml.trim()) return;
        if (!confirm("Write this YAML codeplug to the radio? It changes what the radio transmits. The radio's current codeplug is backed up first.")) return;
        api.send("codeplug_write", { yaml, confirm: true });
      };
      if (cp) { editorWanted = true; api.send("codeplug_get"); }
      return;
    }
    if (!cp) {
      el.innerHTML = `<div class="gd-empty">Read the codeplug from the radio (about 10 s) to edit its channels, zones, contacts and group lists, or open a qdmr YAML file.</div>`;
      return;
    }
    if (!ui.cp || ui.cp.file !== cp.file) {
      if (ui.modelAsked !== cp.file) { ui.modelAsked = cp.file; api.send("codeplug_model"); }
      el.innerHTML = `${head}<div class="gd-empty">Opening ${esc(cp.file)}… (checking that it reads back exactly)</div>`;
      return;
    }
    if (ui.cp.error) {
      el.innerHTML = `${head}<div class="gd-probs">This file can't be edited as forms: ${esc(ui.cp.error)}. The YAML editor still works.</div>`;
      return;
    }
    const counts = { channels: list("channels").length, zones: list("zones").length, contacts: list("contacts").length,
      groupLists: list("groupLists").length };
    const tabs = [["channels", `Channels ${counts.channels}`], ["zones", `Zones ${counts.zones}`], ["contacts", `Talkgroups ${counts.contacts}`],
      ["groupLists", `Group lists ${counts.groupLists}`], ["general", "Radio ID & boot"]];
    el.innerHTML = `${head}${(ui.cp.problems || []).length ? `<div class="gd-probs">${ui.cp.problems.map(esc).join("<br>")}</div>` : ""}
      <div class="seg gd-cp-tabs">${tabs.map(([k, l]) => `<button class="chip${ui.cpTab === k ? " on" : ""}" data-cpt="${k}">${esc(l)}</button>`).join("")}</div>
      <div class="gd-cp-pane"></div>`;
    el.querySelectorAll("[data-cpt]").forEach((b) => (b.onclick = () => { ui.cpTab = b.dataset.cpt; renderCodeplug(); }));
    ({ channels: paneChannels, zones: paneZones, contacts: paneContacts, groupLists: paneGroups, general: paneGeneral })[ui.cpTab](el.querySelector(".gd-cp-pane"));
  }

  function paneChannels(el) {
    const chans = list("channels"), f = ui.cpFilter.toLowerCase();
    const sel = Math.min(ui.cpSel.channels, chans.length - 1);
    const zonesOf = (id) => list("zones").filter((z) => (z.A || []).includes(id) || (z.B || []).includes(id)).map((z) => z.name);
    el.innerHTML = `<div class="gd-split"><div>
        <div class="gd-actions" style="margin:0 0 10px">
          <input class="gd-filter" placeholder="Filter by name or MHz" value="${esc(ui.cpFilter)}">
          <span class="spacer"></span>
          <button class="chip chip-mini" data-add="fm">+ FM channel</button><button class="chip chip-mini" data-add="dmr">+ DMR channel</button></div>
        <div class="gd-list"><table class="gd-tbl"><thead><tr><th>#</th><th>Name</th><th>RX</th><th>TX</th><th>Mode</th><th>Tone / DMR</th><th>Power</th><th>Zones</th></tr></thead><tbody>
        ${chans.map((it, i) => {
          const k = kindOf(it), b = bodyOf(it), rx = hzOf(b.rxFrequency), tx = hzOf(b.txFrequency);
          if (f && !(String(b.name).toLowerCase().includes(f) || (rx && mhz(rx, 4).includes(f)))) return "";
          const tone = k === "dmr" ? `CC${txt(b.colorCode)} ${txt(b.timeSlot)}`
            : [toneOf(b.rxTone) && `R ${toneOf(b.rxTone)}`, toneOf(b.txTone) && `T ${toneOf(b.txTone)}`].filter(Boolean).join(" ");
          return `<tr data-i="${i}" class="${i === sel ? "sel" : ""}"><td>${i + 1}</td><td class="nm">${esc(b.name)}</td>
            <td class="f">${rx ? mhz(rx, 4) : "?"}</td><td>${rx && tx ? esc(offsetText(rx, tx, txt(b.rxOnly) === "true")) : ""}</td>
            <td class="${k}">${esc(k.toUpperCase())}</td><td>${esc(tone)}</td><td>${esc(isTag(b.power) ? "default" : b.power)}</td>
            <td>${esc(zonesOf(b.id).join(", "))}</td></tr>`;
        }).join("")}</tbody></table></div></div>
      <div class="gd-edit"></div></div>`;
    el.querySelector(".gd-filter").oninput = (e) => {
      ui.cpFilter = e.target.value;
      const pos = e.target.selectionStart;
      paneChannels(el);
      const nf = el.querySelector(".gd-filter");
      nf.focus(); nf.setSelectionRange(pos, pos);
    };
    el.querySelectorAll("tr[data-i]").forEach((r) => (r.onclick = () => { ui.cpSel.channels = +r.dataset.i; paneChannels(el); }));
    el.querySelectorAll("[data-add]").forEach((b) => (b.onclick = () => {
      if (chans.length >= 1024) return toast("The radio holds 1024 channels", "warn");
      chans.push(newChannel(b.dataset.add));
      ui.cpSel.channels = chans.length - 1; dirty(); paneChannels(el);
    }));
    if (sel >= 0) editChannel(el.querySelector(".gd-edit"), sel, () => paneChannels(el));
    else el.querySelector(".gd-edit").innerHTML = `<div class="gd-empty">No channels</div>`;
  }

  function editChannel(el, i, redraw) {
    const chans = list("channels"), it = chans[i], k = kindOf(it), b = bodyOf(it);
    const rx = hzOf(b.rxFrequency), tx = hzOf(b.txFrequency);
    const zones = list("zones"), groups = list("groupLists"), contacts = list("contacts");
    const pw = isTag(b.power) ? "" : b.power;
    const to = txt(b.timeout);
    el.innerHTML = `<span class="label">Channel ${i + 1} · ${esc(k.toUpperCase())}</span>
      <div class="gd-form">
        <label>Name</label><div class="row"><input data-f="name" maxlength="16" value="${esc(b.name)}" style="width:170px"></div>
        <label>Type</label><div class="row"><div class="seg">${["fm", "dmr"].map((x) => `<button class="chip${x === k ? " on" : ""}" data-kind="${x}">${x.toUpperCase()}</button>`).join("")}</div></div>
        <label>RX MHz</label><div class="row"><input data-hz="rxFrequency" inputmode="decimal" value="${rx ? mhz(rx) : ""}"></div>
        <label>TX MHz</label><div class="row"><input data-hz="txFrequency" inputmode="decimal" value="${tx ? mhz(tx) : ""}">
          <div class="seg">${OFFSETS.map(([o, l]) => `<button class="chip" data-off="${o}">${l}</button>`).join("")}</div></div>
        <label>Power</label><div class="row"><select data-power>${QPOWER.map(([v, l]) => opt(v, l, v === pw)).join("")}</select></div>
        <label>Timeout</label><div class="row"><select data-f="timeout">${TIMEOUTS.map((v) => opt(v, v, v === to)).join("")}
          ${TIMEOUTS.includes(to) ? "" : opt(to, to || "default", true)}</select></div>
        <label>RX only</label><div class="row"><input type="checkbox" data-bool="rxOnly"${txt(b.rxOnly) === "true" ? " checked" : ""}></div>
        <label>Scan</label><div class="row"><label class="wl-dim"><input type="checkbox" data-ogd="scanZoneSkip"${b.openGD77?.scanZoneSkip === "true" ? " checked" : ""}> skip in zone scan</label>
          <label class="wl-dim"><input type="checkbox" data-ogd="scanAllSkip"${b.openGD77?.scanAllSkip === "true" ? " checked" : ""}> skip in all scan</label></div>
        ${k === "fm" ? `
        <label>RX tone</label><div class="row"><select data-tone="rxTone">${toneOptions(toneOf(b.rxTone))}</select></div>
        <label>TX tone</label><div class="row"><select data-tone="txTone">${toneOptions(toneOf(b.txTone))}</select></div>
        <label>Bandwidth</label><div class="row"><div class="seg">${["Narrow", "Wide"].map((x) => `<button class="chip${txt(b.bandwidth) === x ? " on" : ""}" data-bw="${x}">${x === "Wide" ? "25 kHz" : "12.5 kHz"}</button>`).join("")}</div></div>` : `
        <label>Colour code</label><div class="row"><select data-f="colorCode">${Array.from({ length: 16 }, (_, x) => opt(String(x), `CC ${x}`, String(x) === txt(b.colorCode))).join("")}</select></div>
        <label>Timeslot</label><div class="row"><div class="seg">${["TS1", "TS2"].map((x) => `<button class="chip${txt(b.timeSlot) === x ? " on" : ""}" data-ts="${x}">${x}</button>`).join("")}</div></div>
        <label>Group list</label><div class="row"><select data-ref="groupList">${opt("", "None", !b.groupList)}${groups.map((g) => opt(g.id, g.name, g.id === b.groupList)).join("")}</select></div>
        <label>TX contact</label><div class="row"><select data-ref="contact">${opt("", "None (the group list's)", !b.contact)}${contacts.map((c) => opt(bodyOf(c).id, `${bodyOf(c).name} · ${bodyOf(c).number}`, bodyOf(c).id === b.contact)).join("")}</select></div>`}
        <label>Zones</label><div class="row">${zones.length ? zones.map((z, zi) => `<label class="wl-dim"><input type="checkbox" data-zm="${zi}"${(z.A || []).includes(b.id) || (z.B || []).includes(b.id) ? " checked" : ""}> ${esc(z.name)}</label>`).join("") : `<span class="wl-dim">no zones</span>`}</div>
      </div>
      <div class="gd-actions">
        <button class="chip chip-mini" data-mv="-1"${i ? "" : " disabled"}>↑ Up</button><button class="chip chip-mini" data-mv="1"${i < chans.length - 1 ? "" : " disabled"}>↓ Down</button>
        <button class="chip chip-mini" data-dup>Duplicate</button><span class="spacer"></span>
        <button class="chip chip-mini danger" data-del>Delete</button></div>`;
    const done = () => { dirty(); redraw(); };
    el.querySelector("[data-f=name]").onchange = (e) => { b.name = e.target.value.slice(0, 16); done(); };
    el.querySelectorAll("select[data-f]").forEach((s) => (s.onchange = () => { b[s.dataset.f] = s.value; done(); }));
    el.querySelectorAll("[data-hz]").forEach((inp) => (inp.onchange = () => {
      const hz = Math.round(parseFloat(inp.value) * 1e5) * 10;
      if (!inBand(hz)) { inp.classList.add("bad"); return; }
      const wasSimplex = hzOf(b.rxFrequency) === hzOf(b.txFrequency);
      b[inp.dataset.hz] = fmtHz(hz);
      if (inp.dataset.hz === "rxFrequency" && wasSimplex) b.txFrequency = fmtHz(hz);
      done();
    }));
    el.querySelectorAll("[data-off]").forEach((x) => (x.onclick = () => {
      const hz = hzOf(b.rxFrequency) + +x.dataset.off;
      if (!inBand(hz)) return toast("That offset leaves the band", "warn");
      b.txFrequency = fmtHz(hz); done();
    }));
    el.querySelector("[data-power]").onchange = (e) => { b.power = e.target.value ? e.target.value : { $tag: "default", $value: "" }; done(); };
    el.querySelectorAll("[data-bool]").forEach((x) => (x.onchange = () => { b[x.dataset.bool] = x.checked ? "true" : "false"; done(); }));
    el.querySelectorAll("[data-ogd]").forEach((x) => (x.onchange = () => { b.openGD77 = { ...(b.openGD77 || OGD()), [x.dataset.ogd]: x.checked ? "true" : "false" }; done(); }));
    el.querySelectorAll("[data-tone]").forEach((x) => (x.onchange = () => { b[x.dataset.tone] = toneTo(x.value); done(); }));
    el.querySelectorAll("[data-bw]").forEach((x) => (x.onclick = () => { b.bandwidth = x.dataset.bw; done(); }));
    el.querySelectorAll("[data-ts]").forEach((x) => (x.onclick = () => { b.timeSlot = x.dataset.ts; done(); }));
    el.querySelectorAll("[data-ref]").forEach((x) => (x.onchange = () => {
      if (x.value) b[x.dataset.ref] = x.value;
      else if (x.dataset.ref === "contact") delete b.contact;
      else b[x.dataset.ref] = null;
      done();
    }));
    el.querySelectorAll("[data-kind]").forEach((x) => (x.onclick = () => {
      if (x.dataset.kind === k) return;
      chans[i] = newChannel(x.dataset.kind, b);
      done();
    }));
    el.querySelectorAll("[data-zm]").forEach((x) => (x.onchange = () => {
      const z = zones[+x.dataset.zm];
      z.A = z.A || [];
      if (x.checked) {
        if (z.A.length >= 80) { x.checked = false; toast("A zone holds 80 channels", "warn"); return; }
        z.A.push(b.id);
      } else { z.A = z.A.filter((c) => c !== b.id); z.B = (z.B || []).filter((c) => c !== b.id); }
      done();
    }));
    el.querySelectorAll("[data-mv]").forEach((x) => (x.onclick = () => {
      const j = i + +x.dataset.mv;
      [chans[i], chans[j]] = [chans[j], chans[i]];
      ui.cpSel.channels = j; done();
    }));
    el.querySelector("[data-dup]").onclick = () => {
      if (chans.length >= 1024) return toast("The radio holds 1024 channels", "warn");
      const copy = JSON.parse(JSON.stringify(it));
      bodyOf(copy).id = nextId("ch", "channels");
      bodyOf(copy).name = `${String(b.name).slice(0, 14)} 2`;
      chans.splice(i + 1, 0, copy);
      ui.cpSel.channels = i + 1; done();
    };
    el.querySelector("[data-del]").onclick = () => {
      if (!confirm(`Delete channel ${b.name}? It also leaves every zone.`)) return;
      chans.splice(i, 1);
      for (const z of zones) { z.A = (z.A || []).filter((c) => c !== b.id); z.B = (z.B || []).filter((c) => c !== b.id); }
      ui.cpSel.channels = Math.max(0, i - 1); done();
    };
  }

  function memberList(items, nameOf) {
    return items.map((id, j) => `<div class="gd-member"><small>${j + 1}</small><span>${esc(nameOf(id))}</span>
      <button class="chip" data-up="${j}"${j ? "" : " disabled"}>↑</button><button class="chip" data-dn="${j}"${j < items.length - 1 ? "" : " disabled"}>↓</button>
      <button class="chip" data-rm="${j}">✕</button></div>`).join("");
  }

  function wireMembers(ed, arr, redraw) {
    ed.querySelectorAll("[data-up]").forEach((x) => (x.onclick = () => { const j = +x.dataset.up; [arr[j - 1], arr[j]] = [arr[j], arr[j - 1]]; redraw(); }));
    ed.querySelectorAll("[data-dn]").forEach((x) => (x.onclick = () => { const j = +x.dataset.dn; [arr[j + 1], arr[j]] = [arr[j], arr[j + 1]]; redraw(); }));
    ed.querySelectorAll("[data-rm]").forEach((x) => (x.onclick = () => { arr.splice(+x.dataset.rm, 1); redraw(); }));
  }

  function paneZones(el) {
    const zones = list("zones"), chans = list("channels");
    const sel = Math.min(ui.cpSel.zones, zones.length - 1);
    el.innerHTML = `<div class="gd-split"><div>
        <div class="gd-actions" style="margin:0 0 10px"><span class="spacer"></span><button class="chip chip-mini gd-z-add">+ Zone</button></div>
        <div class="gd-list"><table class="gd-tbl"><thead><tr><th>#</th><th>Zone</th><th>Channels</th></tr></thead><tbody>
        ${zones.map((z, i) => `<tr data-i="${i}" class="${i === sel ? "sel" : ""}"><td>${i + 1}</td><td class="nm">${esc(z.name)}</td>
          <td>${(z.A || []).length + (z.B || []).length}</td></tr>`).join("")}</tbody></table></div></div>
      <div class="gd-edit"></div></div>`;
    el.querySelectorAll("tr[data-i]").forEach((r) => (r.onclick = () => { ui.cpSel.zones = +r.dataset.i; paneZones(el); }));
    el.querySelector(".gd-z-add").onclick = () => {
      if (zones.length >= 68) return toast("The radio holds 68 zones", "warn");
      zones.push({ id: nextId("zone", "zones"), name: "New zone", A: [], B: [] });
      ui.cpSel.zones = zones.length - 1; dirty(); paneZones(el);
    };
    const ed = el.querySelector(".gd-edit");
    if (sel < 0) { ed.innerHTML = `<div class="gd-empty">No zones</div>`; return; }
    const z = zones[sel];
    z.A = z.A || [];
    const inZone = new Set([...z.A, ...(z.B || [])]);
    ed.innerHTML = `<span class="label">Zone ${sel + 1}</span>
      <div class="gd-form"><label>Name</label><div class="row"><input class="gd-z-name" maxlength="16" value="${esc(z.name)}" style="width:170px"></div></div>
      <div class="gd-members">${memberList(z.A, chName) || `<div class="gd-note">No channels in this zone.</div>`}</div>
      <div class="gd-actions"><select class="gd-z-pick">${chans.map(bodyOf).filter((b) => !inZone.has(b.id)).map((b) => opt(b.id, b.name)).join("")}</select>
        <button class="chip chip-mini gd-z-addch">Add</button><span class="wl-dim">${z.A.length}/80</span><span class="spacer"></span>
        <button class="chip chip-mini danger gd-z-del">Delete zone</button></div>`;
    const redraw = () => { dirty(); paneZones(el); };
    ed.querySelector(".gd-z-name").onchange = (e) => { z.name = e.target.value.slice(0, 16); redraw(); };
    wireMembers(ed, z.A, redraw);
    ed.querySelector(".gd-z-addch").onclick = () => {
      const v = ed.querySelector(".gd-z-pick").value;
      if (!v) return;
      if (z.A.length >= 80) return toast("A zone holds 80 channels", "warn");
      z.A.push(v); redraw();
    };
    ed.querySelector(".gd-z-del").onclick = () => {
      if (!confirm(`Delete zone ${z.name}? Its channels stay.`)) return;
      zones.splice(sel, 1); ui.cpSel.zones = Math.max(0, sel - 1); redraw();
    };
  }

  function paneContacts(el) {
    const cts = list("contacts");
    const sel = Math.min(ui.cpSel.contacts, cts.length - 1);
    el.innerHTML = `<div class="gd-split"><div>
        <div class="gd-actions" style="margin:0 0 10px"><span class="spacer"></span><button class="chip chip-mini gd-c-add">+ Talkgroup</button></div>
        <div class="gd-list"><table class="gd-tbl"><thead><tr><th>#</th><th>Name</th><th>Number</th><th>Type</th><th>TS override</th></tr></thead><tbody>
        ${cts.map((c, i) => { const b = bodyOf(c); return `<tr data-i="${i}" class="${i === sel ? "sel" : ""}"><td>${i + 1}</td><td class="nm">${esc(b.name)}</td>
          <td class="f">${esc(b.number)}</td><td>${esc(b.type)}</td><td>${esc(b.openGD77?.timeSlotOverride || "")}</td></tr>`; }).join("")}</tbody></table></div></div>
      <div class="gd-edit"></div></div>`;
    el.querySelectorAll("tr[data-i]").forEach((r) => (r.onclick = () => { ui.cpSel.contacts = +r.dataset.i; paneContacts(el); }));
    el.querySelector(".gd-c-add").onclick = () => {
      if (cts.length >= 1024) return toast("The radio holds 1024 contacts", "warn");
      cts.push({ dmr: { id: nextId("cont", "contacts"), name: "New TG", ring: "false", type: "GroupCall", number: "9", openGD77: { timeSlotOverride: "None" } } });
      ui.cpSel.contacts = cts.length - 1; dirty(); paneContacts(el);
    };
    const ed = el.querySelector(".gd-edit");
    if (sel < 0) { ed.innerHTML = `<div class="gd-empty">No talkgroups</div>`; return; }
    const b = bodyOf(cts[sel]);
    ed.innerHTML = `<span class="label">Talkgroup ${sel + 1}</span>
      <div class="gd-form">
        <label>Name</label><div class="row"><input data-f="name" maxlength="16" value="${esc(b.name)}" style="width:170px"></div>
        <label>Number</label><div class="row"><input data-f="number" inputmode="numeric" value="${esc(b.number)}"></div>
        <label>Type</label><div class="row"><select data-f="type">${["GroupCall", "PrivateCall", "AllCall"].map((t) => opt(t, t.replace("Call", " call"), t === b.type)).join("")}</select></div>
        <label>TS override</label><div class="row"><div class="seg">${["None", "TS1", "TS2"].map((t) => `<button class="chip${(b.openGD77?.timeSlotOverride || "None") === t ? " on" : ""}" data-tso="${t}">${t}</button>`).join("")}</div></div>
      </div>
      <div class="gd-actions"><span class="spacer"></span><button class="chip chip-mini danger gd-c-del">Delete</button></div>`;
    const redraw = () => { dirty(); paneContacts(el); };
    ed.querySelectorAll("[data-f]").forEach((x) => (x.onchange = () => {
      if (x.dataset.f === "number" && !/^\d{1,8}$/.test(x.value)) { x.classList.add("bad"); return; }
      b[x.dataset.f] = x.dataset.f === "name" ? x.value.slice(0, 16) : x.value; redraw();
    }));
    ed.querySelectorAll("[data-tso]").forEach((x) => (x.onclick = () => { b.openGD77 = { ...(b.openGD77 || {}), timeSlotOverride: x.dataset.tso }; redraw(); }));
    ed.querySelector(".gd-c-del").onclick = () => {
      if (!confirm(`Delete ${b.name}? It also leaves every group list and channel.`)) return;
      cts.splice(sel, 1);
      for (const g of list("groupLists")) g.contacts = (g.contacts || []).filter((c) => c !== b.id);
      for (const c of list("channels")) if (bodyOf(c).contact === b.id) delete bodyOf(c).contact;
      ui.cpSel.contacts = Math.max(0, sel - 1); redraw();
    };
  }

  function paneGroups(el) {
    const gs = list("groupLists");
    const sel = Math.min(ui.cpSel.groupLists, gs.length - 1);
    el.innerHTML = `<div class="gd-split"><div>
        <div class="gd-actions" style="margin:0 0 10px"><span class="spacer"></span><button class="chip chip-mini gd-g-add">+ Group list</button></div>
        <div class="gd-list"><table class="gd-tbl"><thead><tr><th>#</th><th>Group list</th><th>Talkgroups</th><th>Used by</th></tr></thead><tbody>
        ${gs.map((g, i) => `<tr data-i="${i}" class="${i === sel ? "sel" : ""}"><td>${i + 1}</td><td class="nm">${esc(g.name)}</td>
          <td>${(g.contacts || []).length}</td><td>${list("channels").filter((c) => bodyOf(c).groupList === g.id).length} channels</td></tr>`).join("")}</tbody></table></div></div>
      <div class="gd-edit"></div></div>`;
    el.querySelectorAll("tr[data-i]").forEach((r) => (r.onclick = () => { ui.cpSel.groupLists = +r.dataset.i; paneGroups(el); }));
    el.querySelector(".gd-g-add").onclick = () => {
      if (gs.length >= 76) return toast("The radio holds 76 group lists", "warn");
      gs.push({ id: nextId("grp", "groupLists"), name: "New list", contacts: [] });
      ui.cpSel.groupLists = gs.length - 1; dirty(); paneGroups(el);
    };
    const ed = el.querySelector(".gd-edit");
    if (sel < 0) { ed.innerHTML = `<div class="gd-empty">No group lists</div>`; return; }
    const g = gs[sel];
    g.contacts = g.contacts || [];
    ed.innerHTML = `<span class="label">Group list ${sel + 1}</span>
      <div class="gd-form"><label>Name</label><div class="row"><input class="gd-g-name" maxlength="16" value="${esc(g.name)}" style="width:170px"></div></div>
      <div class="gd-members">${memberList(g.contacts, ctName) || `<div class="gd-note">No talkgroups in this list.</div>`}</div>
      <div class="gd-actions"><select class="gd-g-pick">${list("contacts").map(bodyOf).filter((b) => !g.contacts.includes(b.id)).map((b) => opt(b.id, `${b.name} · ${b.number}`)).join("")}</select>
        <button class="chip chip-mini gd-g-addc">Add</button><span class="wl-dim">${g.contacts.length}/32</span><span class="spacer"></span>
        <button class="chip chip-mini danger gd-g-del">Delete list</button></div>`;
    const redraw = () => { dirty(); paneGroups(el); };
    ed.querySelector(".gd-g-name").onchange = (e) => { g.name = e.target.value.slice(0, 16); redraw(); };
    wireMembers(ed, g.contacts, redraw);
    ed.querySelector(".gd-g-addc").onclick = () => {
      const v = ed.querySelector(".gd-g-pick").value;
      if (!v) return;
      if (g.contacts.length >= 32) return toast("A group list holds 32 talkgroups", "warn");
      g.contacts.push(v); redraw();
    };
    ed.querySelector(".gd-g-del").onclick = () => {
      if (!confirm(`Delete group list ${g.name}? Channels using it are left without one.`)) return;
      gs.splice(sel, 1);
      for (const c of list("channels")) if (bodyOf(c).groupList === g.id) bodyOf(c).groupList = null;
      ui.cpSel.groupLists = Math.max(0, sel - 1); redraw();
    };
  }

  function paneGeneral(el) {
    const m = M(), first = (m.radioIDs || [])[0], id = first ? bodyOf(first) : {}, boot = m.settings?.boot || {};
    el.innerHTML = `<div class="gd-split"><div class="gd-edit" style="position:static">
        <span class="label">Radio ID</span>
        <div class="gd-form">
          <label>Callsign</label><div class="row"><input data-id="name" maxlength="16" value="${esc(id.name || "")}"${first ? "" : " disabled"}></div>
          <label>DMR ID</label><div class="row"><input data-id="number" inputmode="numeric" value="${esc(id.number || "")}"${first ? "" : " disabled"}></div>
        </div>
        <span class="label" style="margin-top:16px">Boot screen</span>
        <div class="gd-form">
          <label>Line 1</label><div class="row"><input data-boot="message1" maxlength="16" value="${esc(txt(boot.message1))}"></div>
          <label>Line 2</label><div class="row"><input data-boot="message2" maxlength="16" value="${esc(txt(boot.message2))}"></div>
        </div></div>
      <div class="gd-note">The radio's own options (power, squelch, backlight, sound, DMR filters) are on the Settings tab: OpenGD77 keeps them apart from the codeplug, and they apply without a codeplug write.</div></div>`;
    el.querySelectorAll("[data-id]").forEach((x) => (x.onchange = () => {
      if (!first) return;
      if (x.dataset.id === "number" && !/^\d{1,8}$/.test(x.value)) { x.classList.add("bad"); return; }
      bodyOf(first)[x.dataset.id] = x.value; dirty();
    }));
    el.querySelectorAll("[data-boot]").forEach((x) => (x.onchange = () => {
      m.settings = m.settings || {};
      m.settings.boot = m.settings.boot || {};
      m.settings.boot[x.dataset.boot] = x.value.slice(0, 16); dirty();
    }));
  }

  function download(name, text) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type: "text/yaml" }));
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  // ---------------------------------------------------------------- frame

  function renderStrip() {
    const st = S.status || {}, info = S.info;
    const pill = q(".gd-conn");
    pill.className = `gd-conn pill ${busy() ? "pill-wait" : connected() ? "pill-ok" : "pill-bad"}`;
    pill.innerHTML = `<i></i>${busy() ? "Busy" : connected() ? "Connected" : "Not connected"}`;
    q(".gd-info").textContent = info ? `${info.model} · OpenGD77 ${info.revision} · built ${info.built}${st.port ? ` · ${st.port}` : ""}` : (st.error || "");
    qa("[data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === ui.tab));
    qa(".gd-page").forEach((p) => (p.hidden = p.dataset.page !== ui.tab));
    const lc = S.last_change, last = q(".gd-last");
    const stale = connected() && !S.radio;              // a server still running the add-on from before control
    last.hidden = !lc && !stale;
    if (stale) { last.className = "gd-last warn"; last.textContent = "This radio server is still running the previous MD-UV390 add-on: restart it to change the radio from here. Watching works."; }
    else if (lc) { last.className = `gd-last ${lc.ok ? "ok" : "warn"}`; last.textContent = `${hhmm(lc.at)} · ${lc.msg}`; }
    q(".gd").classList.toggle("busy", busy());
    if (busy()) {
      if (!ui.busySince) ui.busySince = Date.now();
      q(".gd-busy span").textContent = `${st.busy[0].toUpperCase()}${st.busy.slice(1)}…`;
      if (!busyTimer) busyTimer = setInterval(tickBusy, 400);
      tickBusy();
    } else {
      ui.busySince = 0;
      if (busyTimer) { clearInterval(busyTimer); busyTimer = null; }
    }
  }

  function tickBusy() {
    if (!root || !busy()) return;
    const t = (Date.now() - ui.busySince) / 1000, restarts = /changing|restoring/.test(S.status.busy);
    q(".gd-busy .bar i").style.width = `${Math.min(95, (t / (restarts ? RESTART_S : 20)) * 100)}%`;
    q(".gd-busy small").textContent = restarts ? "the radio restarts on the change, about 8 s" : `${Math.round(t)} s`;
  }

  function render() {
    if (!root) return;
    renderStrip();
    if (ui.tab === "radio") { renderFront(); renderZones(); renderVfo(); renderScreenCard(); }
    else if (ui.tab === "settings") renderSettings();
    else renderCodeplugBar();
  }

  function mount(body, a) {
    api = a;
    root = body;
    if (!document.querySelector('link[href$="addons/opengd77.css"]')) {     // a server older than this page skips it
      const l = document.createElement("link");
      l.rel = "stylesheet";
      l.href = "static/addons/opengd77.css";
      document.head.append(l);
    }
    body.innerHTML = `<div class="gd">
      <div class="gd-strip"><span class="gd-conn pill">Not connected</span>
        <div class="seg gd-tabs"><button class="chip on" data-tab="radio">Radio</button><button class="chip" data-tab="settings">Settings</button>
          <button class="chip" data-tab="codeplug">Codeplug</button></div>
        <span class="spacer"></span><span class="gd-info"></span></div>
      <div class="gd-busy"><i></i><span></span><div class="bar"><i></i></div><small></small></div>
      <div class="gd-last" hidden></div>
      <div class="gd-page" data-page="radio">
        <div class="gd-grid">
          <section class="gd-card gd-front"></section>
          <section class="gd-card gd-scr"><div class="gd-head" style="margin:0;width:100%"><span class="label">Screen</span><span class="spacer"></span>
              <span class="wl-dim gd-scr-at"></span></div>
            <canvas class="gd-screen" width="160" height="128"></canvas>
            <div class="gd-scr-row"><button class="chip chip-mini gd-grab" title="Read the radio's screen once">Refresh</button>
              <button class="chip chip-mini gd-live" title="Read the screen every 2 seconds while this is on">Live</button>
              <button class="chip chip-mini gd-clock" title="Set the radio's clock from this server (UTC), no restart">Set clock</button></div>
            <div class="gd-note">Nothing on this page keys the radio: OpenGD77 has no CAT, so PTT, volume and the S-meter stay at the radio.</div></section>
          <section class="gd-card gd-zones"></section>
          <section class="gd-card gd-vfo"></section>
        </div></div>
      <div class="gd-page" data-page="settings" hidden><div class="gd-settings"></div></div>
      <div class="gd-page" data-page="codeplug" hidden>
        <div class="gd-cp-bar">
          <button class="chip chip-mini gd-cp-read" title="Read the codeplug from the radio with qdmr (dmrconf); a dated copy is kept">Read from radio</button>
          <button class="chip go gd-cp-write" title="Checked, backed up, written and read back by the server">Write to radio</button>
          <button class="chip chip-mini gd-cp-load" title="Open a qdmr YAML codeplug file to edit (nothing goes to the radio until Write)">Open file…</button>
          <input type="file" class="gd-cp-file" accept=".yaml,.yml" hidden>
          <button class="chip chip-mini gd-cp-dl" title="Download the codeplug (with your edits) as qdmr YAML">Download</button>
          <button class="chip chip-mini gd-cp-yaml" title="The codeplug as raw qdmr YAML">YAML</button>
          <span class="spacer"></span><span class="gd-cp-sum"></span></div>
        <div class="gd-cp-body"></div></div>
    </div>`;
    qa("[data-tab]").forEach((b) => (b.onclick = () => {
      ui.tab = b.dataset.tab;
      renderStrip();
      if (ui.tab === "settings") renderSettings(true);
      else if (ui.tab === "codeplug") renderCodeplug();
      else render();
    }));
    q(".gd-grab").onclick = () => api.send("screen");
    q(".gd-live").onclick = () => {
      if (live) return stopLive();
      api.send("screen");
      live = setInterval(() => (document.hidden || ui.tab !== "radio" ? null : api.send("screen")), 2000);
      q(".gd-live").classList.add("on");
    };
    q(".gd-clock").onclick = () => api.send("clock_sync");
    q(".gd-cp-read").onclick = () => {
      if (ui.cpDirty && !confirm("Read the codeplug from the radio? Your edits not yet written are dropped.")) return;
      api.send("codeplug_read");
    };
    q(".gd-cp-write").onclick = () => {
      if (!ui.cp?.model) return;
      const c = { ch: list("channels").length, z: list("zones").length };
      if (!confirm(`Write the edited codeplug (${c.ch} channels, ${c.z} zones) to the radio? It changes what the radio transmits. `
        + "The server checks it, backs up the radio's current codeplug, writes, and reads it back.")) return;
      api.send("codeplug_write_model", { model: ui.cp.model, confirm: true });
    };
    q(".gd-cp-load").onclick = () => q(".gd-cp-file").click();
    q(".gd-cp-file").onchange = async (ev) => {
      const f = ev.target.files[0];
      ev.target.value = "";
      if (!f) return;
      if (ui.cpDirty && !confirm(`Open ${f.name}? Your edits not yet written are dropped.`)) return;
      api.send("codeplug_load", { yaml: await f.text(), name: f.name });
    };
    q(".gd-cp-dl").onclick = () => {
      editorWanted = false;
      if (ui.cpDirty && ui.cp?.model) api.send("codeplug_render", { model: ui.cp.model });
      else api.send("codeplug_get");
    };
    q(".gd-cp-yaml").onclick = () => { ui.yaml = !ui.yaml; renderCodeplug(); };
    render();
  }

  function hello(snap) {
    S = { ...snap };
    if (connected() && S.info?.screen) api?.send("screen");
    render();
  }

  function event(m) {
    const wasConnected = connected();
    if (m.snapshot) S = { ...m.snapshot };
    for (const k of ["status", "info", "vfos", "codeplug", "now", "layout", "control", "radio", "last_change"])
      if (m[k] !== undefined) S[k] = m[k];
    if (m.codeplug_yaml !== undefined) {
      const ta = root && q(".gd-yaml");
      if (editorWanted && ta && !m.rendered) { ta.value = m.codeplug_yaml; editorWanted = false; }
      else download(m.file || "codeplug.yaml", m.codeplug_yaml);
    }
    if (m.codeplug_model !== undefined) {
      ui.cp = { model: m.codeplug_model, file: m.file, error: m.model_error || null, problems: m.model_problems || [] };
      ui.cpDirty = false;
      if (ui.tab === "codeplug" && !ui.yaml) renderCodeplug();
    }
    if (m.codeplug !== undefined && ui.tab === "codeplug" && !ui.yaml && (!ui.cp || ui.cp.file !== m.codeplug?.file)) renderCodeplug();
    if (m.screen) {
      draw(m.screen);
      if (root) q(".gd-scr-at").textContent = m.screen_at ? hhmm(m.screen_at) : "";
    }
    if (!wasConnected && connected() && S.info?.screen && !m.screen) api?.send("screen");
    if (m.control !== undefined && ui.tab === "settings") renderSettings();
    render();
  }

  function off() {
    stopLive();
    S = {};
    render();
  }

  function nowText(n) {
    if (!n) return "";
    if (n.mode === "channel") return [`${n.zone} · Ch ${n.number}: ${n.channel || `channel ${n.number}`}`, n.power].filter(Boolean).join(" · ");
    return (n.lines || []).length ? `Screen: ${n.lines.join(" · ")}` : "Screen: no text read";
  }

  function tab() {
    return { dot: busy() || connected() ? "ok" : "off", off: !connected() && !busy(),
      title: connected() ? `${S.info?.model || "OpenGD77"}: ${nowText(S.now) || "connected"}` : `OpenGD77: ${S.status?.error || "not connected"}` };
  }

  function card() {
    if (!connected()) return { state: "off", lines: [esc(S.status?.error || "Not connected"), "Open →"] };
    const n = S.now;
    const lines = n?.mode === "channel"
      ? [esc(`${n.channel || `Ch ${n.number}`}${n.power ? ` · ${n.power}` : ""}`), esc(`${n.zone} · Ch ${n.number}`)]
      : [esc(nowText(n) || "Connected")];
    return { state: "ok", lines: [...lines, "Open →"] };
  }

  addons.register("opengd77", { mount, hello, event, off, tab, card });
})();
