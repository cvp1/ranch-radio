"use strict";

// Inside the station page (station.html): its navigation names the radio.
if (window.top !== window) document.documentElement.classList.add("embedded");

// ============================================================ constants


// What the connected radio can do (sent by the server; IC-7300 until then).
let CAPS = {
  name: "IC-7300", max_watts: 100, freq: [30_000, 74_800_000],
  modes: ["LSB", "USB", "AM", "CW", "RTTY", "FM", "CW-R", "RTTY-R"], data_modes: ["LSB", "USB", "AM", "FM"],
  filters: 3, agc: [[1, "FAST"], [2, "MID"], [3, "SLOW"]], preamp: ["OFF", "AMP 1", "AMP 2"],
  att_label: "20 dB", levels: ["af", "rf", "sql", "power"], meters: ["smeter", "po", "swr", "alc"],
  features: ["nb", "nr", "tuner", "split", "vfo_b", "power_on"], scope: true,
};
const has = (f) => CAPS.features.includes(f);
const MODE_ORDER = ["LSB", "USB", "CW", "CW-R", "AM", "FM", "RTTY", "RTTY-R"];
const STEPS = [[10, "10"], [100, "100"], [500, "500"], [1000, "1k"], [5000, "5k"], [10000, "10k"]];
const SPANS = [[2500, "±2.5k"], [5000, "±5k"], [10000, "±10k"], [25000, "±25k"], [50000, "±50k"],
  [100000, "±100k"], [250000, "±250k"], [500000, "±500k"]];
const FILTER_BW = { SSB: [3000, 2400, 1800], CW: [1200, 500, 250], RTTY: [2400, 500, 250],
  AM: [9000, 6000, 3000], FM: [15000, 10000, 7000] };
const METER_KEYS = new Set(["smeter", "po", "swr", "alc"]);
const ACCENT = [57, 225, 198], TXRED = [255, 77, 94], VIOLET = [167, 139, 250];

// ============================================================ helpers

const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

function fmtFreq(f) {
  const s = String(Math.round(f)).padStart(7, "0");
  return `${s.slice(0, -6) || "0"}.${s.slice(-6, -3)}.${s.slice(-3)}`;
}

function bandOf(f) { return BANDS.find((b) => f >= b.lo && f <= b.hi); }
function modeFamily(m) { return m === "LSB" || m === "USB" ? "SSB" : m.startsWith("CW") ? "CW" : m.startsWith("RTTY") ? "RTTY" : m; }

// Receive passband relative to the dial frequency, in Hz.
function passband() {
  const bw = (FILTER_BW[modeFamily(S.mode)] || [3000])[(S.filter || 1) - 1];
  if (S.mode === "USB") return [100, 100 + bw];
  if (S.mode === "LSB") return [-100 - bw, -100];
  return [-bw / 2, bw / 2];
}

// Radio meter units -> human units (IC-7300 CI-V reference scales).
function sUnits(raw) {
  if (raw <= 120) {
    const s = Math.round((raw / 120) * 9);
    return { text: `S${s}`, dbm: Math.round(-73 - (9 - (raw / 120) * 9) * 6) };
  }
  const over = Math.round(((raw - 120) / 121) * 60 / 5) * 5;
  return { text: `S9+${over}`, dbm: Math.round(-73 + ((raw - 120) / 121) * 60) };
}
const poPct = (r) => (r <= 143 ? (r / 143) * 50 : 50 + ((r - 143) / 70) * 50);
function swrVal(r) {
  const pts = [[0, 1], [48, 1.5], [80, 2], [120, 3], [255, 9.9]];
  for (let i = 1; i < pts.length; i++) {
    if (r <= pts[i][0]) {
      const [x0, y0] = pts[i - 1], [x1, y1] = pts[i];
      return y0 + ((r - x0) / (x1 - x0)) * (y1 - y0);
    }
  }
  return 9.9;
}

// Mouse-wheel notches -> one step each; trackpad pixel scrolling accumulates.
function wheelSteps(e, acc) {
  if (e.deltaMode !== 0 || Math.abs(e.deltaY) >= 50) return e.deltaY < 0 ? 1 : -1;
  acc.v += e.deltaY;
  const n = Math.trunc(-acc.v / 30);
  acc.v += n * 30;
  return n;
}


// ============================================================ link

const S = {};          // mirror of the radio state
let CFG = {};          // server config
const localEdit = {};  // key -> time of our last change (to ignore stale echoes)
let ws = null, wsBackoff = 500, overlayTimer = null, lastPing = 0, rtt = null;


function send(m) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(m)); }
// A line in the radio server's log from this page (diagnosis; the server caps it).
function pageLog(msg) { send({ t: "page_log", msg: String(msg).slice(0, 300) }); }

function setRadio(k, v) {
  localEdit[k] = performance.now();
  S[k] = v;
  send({ t: "set", k, v });
  render();
}

function connect() {
  ws = new WebSocket(withToken("ws"));
  ws.binaryType = "arraybuffer";
  clearTimeout(overlayTimer);
  ws.onopen = () => { wsBackoff = 500; $("#overlay").hidden = true; };
  ws.onmessage = (e) => {
    if (typeof e.data !== "string") { scope.push(e.data); return; }
    const m = JSON.parse(e.data);
    if (m.t === "hello") {
      Object.assign(S, m.state);
      applyConfig(m.config);
      wlArmed = !!(m.winlink && m.winlink.armed);
      if (typeof winlink !== "undefined") winlink.hello(m.winlink, m.config);
      if (typeof mesh !== "undefined") mesh.hello(m.mesh, m.config);
      if (typeof addons !== "undefined") addons.hello(m.config.addons, m.addons);
      if (typeof modules !== "undefined") modules.hello(m.config);
    } else if (m.t === "config") {
      applyConfig(m.config);
    } else if (m.t === "state") {
      let structural = false;
      const now = performance.now();
      for (const [k, v] of Object.entries(m.d)) {
        // confirmed: the server refused our change and says what the radio has
        if (m.confirmed) delete localEdit[k];
        else if (k !== "tx" && k !== "connected" && now - (localEdit[k] ?? -1e9) < 700) continue;
        S[k] = v;
        if (!METER_KEYS.has(k)) structural = true;
      }
      if (structural) render();
      if ("freq" in m.d) txLevel.bandMaybeChanged();
    } else if (m.t === "codec") {
      txLevel.event(m);
    } else if (m.t === "voice") {
      $("#b-usbsrc").hidden = !m.usb_src;
    } else if (m.t === "winlink") {
      if ("armed" in m) {
        wlArmed = !!m.armed;
        if (wlArmed) pttUp();
        renderTx();
      }
      if (typeof winlink !== "undefined") winlink.event(m);
    } else if (m.t === "mesh") {
      if (typeof mesh !== "undefined") mesh.event(m);
    } else if (m.t === "addon" && m.addon === "winlink") {
      if (typeof winlink !== "undefined") winlink.power(m);   // Winlink's page is its own panel
      if (typeof modules !== "undefined" && "on" in m) modules.addonChanged();
    } else if (m.t === "addon" && m.addon === "mesh") {
      if (typeof mesh !== "undefined") mesh.power(m);   // Mesh's page is its own tab
      if (typeof modules !== "undefined" && "on" in m) modules.addonChanged();
    } else if (m.t === "addon") {
      if (typeof addons !== "undefined") addons.event(m);
      if (typeof modules !== "undefined" && "on" in m) modules.addonChanged();
    } else if (m.t === "modules" || m.t === "module_plan" || m.t === "module_applied") {
      if (typeof modules !== "undefined") modules.event(m);
    } else if (m.t === "notice") {
      toast(m.msg, m.level);
    } else if (m.t === "ptt_released") {
      // The server let go of our PTT (time limit, another operator, the
      // radio) while the button was held: stop the keep-alives and let go
      // here too. Press again to transmit.
      pttUp();
    } else if (m.t === "pong") {
      rtt = performance.now() - m.ts;
      $("#rtt").textContent = `${Math.round(rtt)} ms`;
    }
  };
  ws.onclose = (e) => {
    ws = null;
    renderConn();
    if (e.code === 4401) {
      const t = prompt("This station needs an access token:", "");
      if (t !== null) { token = t.trim(); store.set("token", token); }
    }
    overlayTimer = setTimeout(() => {
      $("#overlay-msg").textContent = "Reconnecting to station…";
      $("#overlay").hidden = false;
    }, 800);
    setTimeout(connect, wsBackoff);
    wsBackoff = Math.min(wsBackoff * 1.6, 5000);
  };
}

setInterval(() => send({ t: "ping", ts: performance.now() }), 2000);

// ============================================================ rendering

const freqEl = $("#freq");
const digitEls = [], sepEls = [];

let digits = 8;

function buildFreq() {
  digits = CAPS.freq[1] >= 1e9 ? 10 : CAPS.freq[1] >= 1e8 ? 9 : 8;
  freqEl.innerHTML = "";
  digitEls.length = sepEls.length = 0;
  freqEl.classList.toggle("long", digits > 8);
  for (let pow = digits - 1; pow >= 0; pow--) {
    const d = document.createElement("span");
    d.className = "d" + (pow < 3 ? " hz" : "");
    d.dataset.pow = pow;
    digitEls[pow] = d;
    freqEl.append(d);
    if (pow === 9 || pow === 6 || pow === 3) {
      const sep = document.createElement("span");
      sep.className = "sep" + (pow === 3 ? " hz" : "");
      sep.textContent = ".";
      sepEls[pow] = sep;
      freqEl.append(sep);
    }
  }
  const u = document.createElement("span");
  u.className = "unit"; u.textContent = "MHz";
  freqEl.append(u);
}

function renderFreq() {
  const s = String(Math.round(S.freq || 0)).padStart(digits, "0").slice(-digits);
  let leading = true;
  for (let pow = digits - 1; pow >= 0; pow--) {
    const ch = s[digits - 1 - pow];
    if (ch !== "0" || pow <= 6) leading = false;
    const d = digitEls[pow];
    if (d.textContent !== ch) d.textContent = ch;
    d.classList.toggle("ghost", leading);
    if (sepEls[pow]) sepEls[pow].classList.toggle("ghost", leading);
  }
}


function renderConn() {
  const pill = $("#conn");
  let cls = "pill-ok", text = "Connected";
  if (!ws || ws.readyState !== 1) { cls = "pill-bad"; text = "Offline"; }
  else if (!S.connected) { cls = "pill-wait"; text = "Radio not responding"; }
  pill.className = `pill ${cls}`;
  pill.querySelector("b").textContent = text;
  on($("#power-btn"), S.connected && ws);
}

function render() {
  document.body.classList.toggle("tx", !!S.tx);
  renderConn();
  renderFreq();
  $("#freq-b").textContent = fmtFreq(S.freq_b || 0);
  $("#vfo-label").textContent = `VFO ${S.vfo}`;
  $("#vfo-b-label").textContent = `VFO ${S.vfo === "A" ? "B" : "A"}${S.split ? " · TX" : ""}`;
  $("#b-mode").textContent = S.mode;
  $("#b-filter").textContent = `FIL${S.filter}`;
  $("#b-data").hidden = !S.data;
  $("#b-split").hidden = !S.split;
  $("#b-tx").hidden = !S.tx;

  const band = bandOf(S.freq);
  document.querySelectorAll("#bands .chip").forEach((c) => on(c, band && c.dataset.v === band.n));
  document.querySelectorAll("#modes .chip").forEach((c) => {
    if (c.dataset.v === "DATA") {
      on(c, S.data);
      c.disabled = !CAPS.data_modes.includes(S.mode);
      c.classList.toggle("dim", c.disabled);
    } else on(c, c.dataset.v === S.mode);
  });
  document.querySelectorAll("#filters .chip").forEach((c) => on(c, +c.dataset.v === S.filter));
  document.querySelectorAll("#agcs .chip").forEach((c) => on(c, +c.dataset.v === S.agc));
  document.querySelectorAll("#spans .chip").forEach((c) => {
    // Span only applies in centre mode; fixed mode uses the radio's band edges.
    on(c, +c.dataset.v === S.scope_span && S.scope_mode === "center");
    c.disabled = S.scope_mode !== "center";
    c.classList.toggle("dim", c.disabled);
  });
  document.querySelectorAll("#steps .chip").forEach((c) => on(c, +c.dataset.v === step));
  on($("#btn-center"), S.scope_mode === "center");
  on($("#btn-fixed"), S.scope_mode === "fixed");
  on($("#btn-split"), S.split);
  on($("#btn-ab"), S.vfo === "B");

  const tog = (id, active, sub) => { const b = $(id); on(b, active); b.querySelector("small").textContent = sub; };
  tog("#t-preamp", S.preamp > 0, CAPS.preamp[S.preamp] || "OFF");
  tog("#t-att", S.att, S.att ? CAPS.att_label : "OFF");
  tog("#t-nb", S.nb, S.nb ? "ON" : "OFF");
  tog("#t-nr", S.nr, S.nr ? "ON" : "OFF");
  tog("#t-tuner", S.tuner > 0, S.tuner === 2 ? "TUNING" : S.tuner ? "ON" : "OFF");
  tog("#t-tune", S.tuner === 2, S.tuner === 2 ? "TUNING…" : "TX");
  $("#t-tune").disabled = !armed;

  for (const k of knobs) k.sync();
  renderTx();
}

// ============================================================ frequency entry & tuning

let step = store.get("step", 100);

// Lock: frequency and level controls ignore the wheel, drags and clicks, so
// scrolling the page over them can't retune the radio. Locked by default.
let locked = store.get("locked", true);

function setLocked(v) {
  locked = v;
  store.set("locked", v);
  document.body.classList.toggle("locked", v);
  const b = $("#btn-lock");
  on(b, v);
  b.setAttribute("aria-checked", String(v));
  b.querySelector("span").textContent = v ? "LOCKED" : "UNLOCKED";
  freqEl.title = v ? "Locked · press LOCK (or L) to tune"
    : "Scroll a digit to tune · click to type a frequency";
}

// A deliberate click on a locked control: point at the lock, don't act.
let nudgeT = 0;
function nudgeLock() {
  const b = $("#btn-lock");
  b.classList.remove("flash"); void b.offsetWidth; b.classList.add("flash");
  const now = performance.now();
  if (now - nudgeT > 3000) { nudgeT = now; toast("Tuning is locked · press LOCK (or L) to change it"); }
}

function tune(f) {
  if (locked) { nudgeLock(); return; }
  f = clamp(Math.round(f), CAPS.freq[0], CAPS.freq[1]);
  if (f !== S.freq) setRadio("freq", f);
}
const snap = (f, s = step) => Math.round(f / s) * s;

function bindFreq() {
  let wheelAcc = { v: 0 }, drag = null;
  freqEl.addEventListener("wheel", (e) => {
    if (locked) return;   // let the page scroll
    e.preventDefault();
    const d = e.target.closest(".d");
    const unit = d ? 10 ** +d.dataset.pow : step;
    const n = wheelSteps(e, wheelAcc);
    if (n) tune((d ? S.freq : snap(S.freq, unit)) + n * unit);
  }, { passive: false });

  freqEl.addEventListener("pointerdown", (e) => {
    if (locked) return;
    const d = e.target.closest(".d");
    drag = { y: e.clientY, moved: false, unit: d ? 10 ** +d.dataset.pow : step, acc: 0 };
    freqEl.setPointerCapture(e.pointerId);
  });
  freqEl.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dy = drag.y - e.clientY;
    if (Math.abs(dy) > 4) drag.moved = true;
    const steps = Math.trunc(dy / 14);
    if (steps) { drag.y -= steps * 14; tune(S.freq + steps * drag.unit); }
  });
  freqEl.addEventListener("pointerup", () => {
    if (locked) { nudgeLock(); return; }
    if (drag && !drag.moved) openEntry();
    drag = null;
  });

  const form = $("#freq-entry"), input = $("#freq-input");
  function openEntry() {
    freqEl.hidden = true; form.hidden = false;
    input.value = (S.freq / 1e6).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
    input.focus(); input.select();
  }
  function closeEntry() { form.hidden = true; freqEl.hidden = false; }
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const f = parseFreq(input.value);
    if (f) tune(f); else if (input.value.trim()) toast("Couldn't read that frequency", "warn");
    closeEntry();
  });
  input.addEventListener("blur", closeEntry);
  input.addEventListener("keydown", (e) => { if (e.key === "Escape") closeEntry(); });
}

function parseFreq(txt) {
  const v = parseFloat(txt.replace(",", "."));
  if (!isFinite(v) || v <= 0) return null;
  if (v < 100) return v * 1e6;          // MHz
  if (v < 100_000) return v * 1e3;      // kHz
  return v;                              // Hz
}

// ============================================================ spectrum + waterfall

class Scope {
  constructor() {
    this.spec = $("#spectrum"); this.wf = $("#waterfall"); this.body = $("#scope-body");
    this.sctx = this.spec.getContext("2d"); this.wctx = this.wf.getContext("2d");
    this.off = document.createElement("canvas");
    this.octx = this.off.getContext("2d", { willReadFrequently: false });
    this.start = 0; this.end = 0; this.n = 0;
    this.avg = null; this.peak = null; this.floor = 30;
    this.dirty = false; this.hoverX = null;
    this.lut = this.makeLut();
    new ResizeObserver(() => this.resize()).observe(this.body);
    this.resize();
    this.bindPointer();
    const loop = () => { if (this.dirty) this.draw(); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }

  makeLut() {
    const stops = [[0, [4, 6, 10]], [0.22, [10, 30, 72]], [0.42, [14, 96, 140]], [0.58, [32, 200, 180]],
      [0.74, [245, 215, 80]], [0.88, [255, 120, 60]], [1, [255, 250, 240]]];
    const lut = new Uint8ClampedArray(256 * 3);
    for (let i = 0; i < 256; i++) {
      const t = i / 255;
      let k = 1; while (k < stops.length - 1 && stops[k][0] < t) k++;
      const [t0, c0] = stops[k - 1], [t1, c1] = stops[k];
      const u = (t - t0) / (t1 - t0);
      for (let j = 0; j < 3; j++) lut[i * 3 + j] = c0[j] + (c1[j] - c0[j]) * u;
    }
    return lut;
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    for (const c of [this.spec, this.wf]) {
      c.width = Math.round(c.clientWidth * dpr);
      c.height = Math.round(c.clientHeight * dpr);
    }
    this.dpr = dpr; this.dirty = true;
  }

  push(buf) {
    const dv = new DataView(buf);
    if (dv.getUint8(0) !== 1) return;
    this.pushLine(dv.getFloat64(1, true), dv.getFloat64(9, true), new Uint8Array(buf, 17));
  }

  // One spectrum line: values 0..160 spread evenly from start to end (Hz).
  pushLine(start, end, data) {
    const n = data.length;
    $("#scope-empty").hidden = true;

    if (n !== this.n || Math.abs((end - start) - (this.end - this.start)) > 1) {
      // New geometry: restart averages and waterfall history.
      this.n = n; this.avg = Float32Array.from(data); this.peak = Float32Array.from(data);
      this.floor = Array.from(data).sort((a, b) => a - b)[(n * 0.25) | 0];   // no warm-up glare
      this.off.width = n; this.off.height = 180;
      this.octx.fillStyle = "#04060a"; this.octx.fillRect(0, 0, n, 180);
      this.row = this.octx.createImageData(n, 1);
    } else if (start !== this.start) {
      // Tuned in centre mode: slide history sideways so signals stay aligned.
      const dx = Math.round(((this.start - start) / (end - start)) * n);
      if (dx) {
        this.octx.drawImage(this.off, dx, 0);
        this.octx.fillStyle = "#04060a";
        this.octx.fillRect(dx > 0 ? 0 : n + dx, 0, Math.abs(dx), this.off.height);
        const shift = (arr) => { const c = arr.slice(); arr.fill(this.floor); for (let i = 0; i < n; i++) { const j = i + dx; if (j >= 0 && j < n) arr[j] = c[i]; } };
        shift(this.avg); shift(this.peak);
      }
    }
    this.start = start; this.end = end;

    // Noise floor estimate (25th percentile), smoothed.
    const sorted = Array.from(data).sort((a, b) => a - b);
    this.floor += (sorted[(n * 0.25) | 0] - this.floor) * 0.08;

    for (let i = 0; i < n; i++) {
      this.avg[i] += (data[i] - this.avg[i]) * 0.55;
      this.peak[i] = Math.max(this.peak[i] - 0.35, this.avg[i]);
    }

    // Waterfall row.
    this.octx.drawImage(this.off, 0, 1);
    const px = this.row.data, lut = this.lut, lo = this.floor - 3, range = 72;
    for (let i = 0; i < n; i++) {
      const v = (clamp((data[i] - lo) / range, 0, 1) * 255) | 0;
      px[i * 4] = lut[v * 3]; px[i * 4 + 1] = lut[v * 3 + 1]; px[i * 4 + 2] = lut[v * 3 + 2]; px[i * 4 + 3] = 255;
    }
    this.octx.putImageData(this.row, 0, 0);
    $("#scope-range").textContent = `${fmtFreq(start)} – ${fmtFreq(end)}`;
    this.dirty = true;
  }

  fx(f) { return ((f - this.start) / (this.end - this.start)) * this.spec.width; }
  xf(xCss) { return this.start + (xCss / this.spec.clientWidth) * (this.end - this.start); }

  draw() {
    this.dirty = false;
    const c = this.sctx, W = this.spec.width, H = this.spec.height, dpr = this.dpr;
    const accent = S.tx ? TXRED : ACCENT;
    c.clearRect(0, 0, W, H);
    const bg = c.createLinearGradient(0, 0, 0, H);
    bg.addColorStop(0, "#081018"); bg.addColorStop(1, "#04060a");
    c.fillStyle = bg; c.fillRect(0, 0, W, H);
    if (!this.n) return;

    const top = this.floor + 105, bottom = this.floor - 18;
    const yv = (v) => H - ((v - bottom) / (top - bottom)) * (H - 22 * dpr) - 18 * dpr;

    // grid
    c.strokeStyle = "rgba(255,255,255,0.045)"; c.lineWidth = 1;
    for (let v = Math.ceil(bottom / 20) * 20; v < top; v += 20) {
      const y = Math.round(yv(v)) + 0.5; c.beginPath(); c.moveTo(0, y); c.lineTo(W, y); c.stroke();
    }
    const span = this.end - this.start;
    const gstep = [500, 1000, 2000, 2500, 5000, 10000, 20000, 25000, 50000, 100000, 200000]
      .find((s) => span / s <= Math.min(10, W / (72 * dpr))) || 250000;
    c.font = `${10.5 * dpr}px ${getComputedStyle(document.body).getPropertyValue("--mono")}`;
    c.textAlign = "center"; c.textBaseline = "bottom";
    for (let f = Math.ceil(this.start / gstep) * gstep; f <= this.end; f += gstep) {
      const x = Math.round(this.fx(f)) + 0.5;
      c.strokeStyle = "rgba(255,255,255,0.05)";
      c.beginPath(); c.moveTo(x, 0); c.lineTo(x, H - 16 * dpr); c.stroke();
      c.fillStyle = "rgba(160,172,184,0.65)";
      c.fillText((f / 1e6).toFixed(gstep < 1000 ? 4 : 3), x, H - 3 * dpr);
    }

    // passband
    const [plo, phi] = passband();
    const x0 = this.fx(S.freq + plo), x1 = this.fx(S.freq + phi);
    c.fillStyle = rgba(accent, 0.09); c.fillRect(x0, 0, x1 - x0, H - 16 * dpr);
    c.fillStyle = rgba(accent, 0.35);
    c.fillRect(Math.round(x0), 0, dpr, H - 16 * dpr); c.fillRect(Math.round(x1), 0, dpr, H - 16 * dpr);

    // peak hold
    const n = this.n, dx = W / (n - 1);
    c.beginPath();
    for (let i = 0; i < n; i++) { const y = yv(this.peak[i]); i ? c.lineTo(i * dx, y) : c.moveTo(0, y); }
    c.strokeStyle = "rgba(255,255,255,0.13)"; c.lineWidth = dpr; c.stroke();

    // trace + fill
    c.beginPath();
    for (let i = 0; i < n; i++) { const y = yv(this.avg[i]); i ? c.lineTo(i * dx, y) : c.moveTo(0, y); }
    c.save();
    c.lineTo(W, H); c.lineTo(0, H); c.closePath();
    const g = c.createLinearGradient(0, H * 0.15, 0, H);
    g.addColorStop(0, rgba(accent, 0.5)); g.addColorStop(0.6, rgba(accent, 0.1)); g.addColorStop(1, rgba(accent, 0.0));
    c.fillStyle = g; c.fill();
    c.restore();
    c.beginPath();
    for (let i = 0; i < n; i++) { const y = yv(this.avg[i]); i ? c.lineTo(i * dx, y) : c.moveTo(0, y); }
    c.strokeStyle = rgba(accent, 0.95); c.lineWidth = 1.4 * dpr;
    c.shadowColor = rgba(accent, 0.8); c.shadowBlur = 8 * dpr; c.stroke(); c.shadowBlur = 0;

    // split TX marker
    if (S.split && S.freq_b > this.start && S.freq_b < this.end) {
      const xb = Math.round(this.fx(S.freq_b)) + 0.5;
      c.setLineDash([4 * dpr, 4 * dpr]); c.strokeStyle = rgba(VIOLET, 0.9); c.lineWidth = dpr;
      c.beginPath(); c.moveTo(xb, 0); c.lineTo(xb, H - 16 * dpr); c.stroke(); c.setLineDash([]);
    }
    // dial marker
    const xv = Math.round(this.fx(S.freq)) + 0.5;
    c.strokeStyle = S.tx ? rgba(TXRED, 1) : "rgba(255,181,71,0.95)"; c.lineWidth = 1.5 * dpr;
    c.beginPath(); c.moveTo(xv, 0); c.lineTo(xv, H - 16 * dpr); c.stroke();
    c.fillStyle = c.strokeStyle;
    c.beginPath(); c.moveTo(xv - 5 * dpr, 0); c.lineTo(xv + 5 * dpr, 0); c.lineTo(xv, 6 * dpr); c.closePath(); c.fill();

    // waterfall (scaled from the offscreen history)
    const w = this.wctx;
    w.imageSmoothingEnabled = true;
    w.drawImage(this.off, 0, 0, n, this.off.height, 0, 0, this.wf.width, this.wf.height);
    w.fillStyle = S.tx ? rgba(TXRED, 0.7) : "rgba(255,181,71,0.55)";
    w.fillRect(xv - dpr / 2, 0, dpr, this.wf.height);
  }

  bindPointer() {
    const b = this.body, cur = $("#scope-cursor");
    let drag = null, wheelAcc = { v: 0 };
    const showCursor = (x) => {
      if (!this.n) return;
      const f = snap(this.xf(x), Math.max(step, 10));
      cur.hidden = false; cur.style.left = `${x}px`;
      cur.classList.toggle("flip", x > b.clientWidth - 120);
      cur.firstElementChild.textContent = fmtFreq(f);
    };
    b.addEventListener("pointerdown", (e) => {
      if (!this.n || locked) return;
      b.setPointerCapture(e.pointerId);
      drag = { x: e.offsetX, f: S.freq, moved: false };
    });
    b.addEventListener("pointermove", (e) => {
      const x = e.offsetX;
      if (drag) {
        const dxp = x - drag.x;
        if (Math.abs(dxp) > 4) drag.moved = true;
        if (drag.moved) {
          const hzPerPx = (this.end - this.start) / b.clientWidth;
          tune(snap(drag.f - dxp * hzPerPx, Math.min(step, 100)));
        }
      }
      if (e.pointerType === "mouse") showCursor(x);
    });
    b.addEventListener("pointerup", (e) => {
      if (locked && this.n) { nudgeLock(); return; }
      if (drag && !drag.moved) tune(snap(this.xf(e.offsetX), Math.max(step, 10)));
      drag = null;
    });
    b.addEventListener("pointerleave", () => { cur.hidden = true; });
    b.addEventListener("wheel", (e) => {
      if (locked) return;
      e.preventDefault();
      const n = wheelSteps(e, wheelAcc);
      if (n) tune(snap(S.freq, step) + n * step);
    }, { passive: false });
  }
}

// ============================================================ S-meter

const smeter = { el: $("#smeter"), v: 0, peak: 0, peakT: 0 };

function renderMeters() {
  const raw = S.smeter || 0;
  const su = sUnits(raw);
  $("#s-units").textContent = S.tx ? "TX" : su.text;
  const watts = (poPct(S.po || 0) / 100) * CAPS.max_watts;
  $("#s-dbm").textContent = S.tx ? (CAPS.meters.includes("po") ? `${Math.round(watts)} W out` : "") : `${su.dbm} dBm`;
  const po = poPct(S.po || 0), swr = swrVal(S.swr || 0), alc = clamp((S.alc || 0) / 120 * 100, 0, 100);
  $("#m-po").style.width = `${po}%`;
  $("#v-po").textContent = `${Math.round(watts)} W`;
  $("#m-swr").style.width = `${clamp((S.swr || 0) / 120 * 100, 0, 100)}%`;
  $("#m-swr").classList.toggle("bad", swr > 2);
  $("#v-swr").textContent = swr >= 9.9 ? "∞" : swr.toFixed(1);
  $("#m-alc").style.width = `${alc}%`;
  $("#v-alc").textContent = `${Math.round(alc)}%`;
}

function drawSmeter() {
  const c = smeter.el, dpr = window.devicePixelRatio || 1;
  const W = Math.round(c.clientWidth * dpr), H = Math.round(c.clientHeight * dpr);
  if (c.width !== W || c.height !== H) { c.width = W; c.height = H; }
  const x = c.getContext("2d");
  const target = S.tx ? 0 : (S.smeter || 0);
  // Analog-ish ballistics: quick attack, slower release.
  smeter.v += (target - smeter.v) * (target > smeter.v ? 0.45 : 0.12);
  const now = performance.now();
  if (smeter.v >= smeter.peak) { smeter.peak = smeter.v; smeter.peakT = now; }
  else if (now - smeter.peakT > 1200) smeter.peak = Math.max(smeter.v, smeter.peak - 1.5);

  x.clearRect(0, 0, W, H);
  const pad = 2 * dpr, barY = 26 * dpr, barH = 22 * dpr, segs = 46;
  const gap = 2.5 * dpr, segW = (W - pad * 2 - gap * (segs - 1)) / segs;
  const rawAt = (i) => ((i + 0.5) / segs) * 241;
  const peakSeg = Math.floor((smeter.peak / 241) * segs);
  for (let i = 0; i < segs; i++) {
    const r = rawAt(i), lit = r <= smeter.v;
    let col;
    if (r <= 120) col = ACCENT;
    else { const t = (r - 120) / 121; col = [255, Math.round(181 - t * 104), Math.round(71 + t * 23)]; }
    const sx = pad + i * (segW + gap);
    x.fillStyle = lit ? rgba(col, 0.95) : (i === peakSeg && smeter.peak > 4 ? rgba(col, 0.85) : rgba(col, r <= 120 ? 0.07 : 0.1));
    if (lit) { x.shadowColor = rgba(col, 0.7); x.shadowBlur = 8 * dpr; } else x.shadowBlur = 0;
    x.beginPath(); x.roundRect(sx, barY, segW, barH, 2 * dpr); x.fill();
  }
  x.shadowBlur = 0;
  // scale
  x.font = `600 ${11 * dpr}px ${getComputedStyle(document.body).getPropertyValue("--mono")}`;
  x.textBaseline = "alphabetic"; x.textAlign = "center";
  const marks = [[1, 13.3], [3, 40], [5, 66.7], [7, 93.3], [9, 120], ["+20", 160.3], ["+40", 200.7], ["+60", 241]];
  for (const [lab, r] of marks) {
    const px = pad + (r / 241) * (W - pad * 2);
    x.fillStyle = r > 120 ? "rgba(255,150,90,0.85)" : "rgba(160,172,184,0.8)";
    x.textAlign = r >= 241 ? "right" : "center";
    x.fillText(String(lab), r >= 241 ? W - pad : px, 16 * dpr);
    x.fillStyle = "rgba(255,255,255,0.18)";
    x.fillRect(Math.min(px, W - pad - dpr), barY + barH + 5 * dpr, dpr, 5 * dpr);
  }
  x.fillStyle = "rgba(160,172,184,0.5)"; x.textAlign = "left";
  x.font = `500 ${9.5 * dpr}px ${getComputedStyle(document.body).getPropertyValue("--mono")}`;
  x.fillText("S", pad, H - 2 * dpr);
  x.textAlign = "right"; x.fillText("dB", W - pad, H - 2 * dpr);
}

// ============================================================ knobs

let levelStyle = store.get("levelStyle", "dials");

function setLevelStyle(style) {
  levelStyle = style;
  store.set("levelStyle", style);
  $("#knobs").classList.toggle("sliders", style === "sliders");
  document.querySelectorAll("#level-style .chip").forEach((c) => on(c, c.dataset.v === style));
}

class Knob {
  constructor(host) {
    this.host = host; this.key = host.dataset.key; this.unit = host.dataset.unit || "%";
    this.v = -1; this.editT = -1e9;
    host.tabIndex = 0;
    host.innerHTML = `
      <svg viewBox="0 0 100 100">
        <path class="track" d="${Knob.arc(1)}" pathLength="100"/>
        <path class="val" d="${Knob.arc(1)}" pathLength="100" stroke-dasharray="0 100"/>
        <circle class="cap" cx="50" cy="50" r="27"/>
        <line class="tick" x1="50" y1="30" x2="50" y2="38"/>
      </svg>
      <span class="kv"></span><span class="kl">${host.dataset.label}</span>
      <div class="sl">
        <span class="sl-label">${host.dataset.label}</span>
        <div class="sl-track"><i class="sl-fill"></i><b class="sl-thumb"></b></div>
        <span class="sl-val"></span>
      </div>`;
    this.valEl = host.querySelector(".val"); this.tick = host.querySelector(".tick");
    this.kv = host.querySelector(".kv"); this.track = host.querySelector(".sl-track");
    this.fill = host.querySelector(".sl-fill"); this.thumb = host.querySelector(".sl-thumb");
    this.slv = host.querySelector(".sl-val");
    this.draw();
    this.bind();
  }
  static arc(frac) {
    const a0 = (135 * Math.PI) / 180, a1 = a0 + (270 * frac * Math.PI) / 180, r = 40;
    const p = (a) => `${50 + r * Math.cos(a)} ${50 + r * Math.sin(a)}`;
    return `M ${p(a0)} A ${r} ${r} 0 ${frac > 2 / 3 ? 1 : 0} 1 ${p(a1)}`;
  }
  draw() {
    const f = Math.max(this.v, 0) / 255;
    this.valEl.setAttribute("stroke-dasharray", `${f * 100} 100`);
    this.tick.setAttribute("transform", `rotate(${-135 + f * 270} 50 50)`);
    this.kv.textContent = this.slv.textContent = this.v < 0 ? "—"   // radio doesn't report it
      : this.unit === "W" ? `${Math.round(f * CAPS.max_watts)} W` : `${Math.round(f * 100)}%`;
    this.fill.style.width = this.thumb.style.left = `${f * 100}%`;
  }
  sync() {
    if (performance.now() - this.editT < 700) return;
    const v = S[this.key] === null ? -1 : S[this.key];
    if (v !== undefined && v !== this.v) { this.v = v; this.draw(); }
  }
  change(v) {
    v = clamp(Math.round(v), 0, 255);
    if (v === this.v) return;
    this.v = v; this.editT = performance.now(); this.draw();
    clearTimeout(this.sendT);
    const now = performance.now();
    if (now - (this.lastSend || 0) > 60) { this.lastSend = now; setRadio(this.key, v); }
    else this.sendT = setTimeout(() => { this.lastSend = performance.now(); setRadio(this.key, this.v); }, 60);
  }
  bind() {
    let drag = null;
    // Dials drag vertically (relative); sliders jump to and follow the pointer.
    const fromX = (x) => { const r = this.track.getBoundingClientRect(); return ((x - r.left) / r.width) * 255; };
    this.host.addEventListener("pointerdown", (e) => {
      if (locked) return;
      this.host.setPointerCapture(e.pointerId); this.host.focus();
      drag = { y: e.clientY, v: this.v, slider: levelStyle === "sliders" };
      if (drag.slider) this.change(fromX(e.clientX));
    });
    this.host.addEventListener("pointermove", (e) => {
      if (!drag) return;
      this.change(drag.slider ? fromX(e.clientX) : drag.v + ((drag.y - e.clientY) / 160) * 255);
    });
    this.host.addEventListener("pointerup", () => { if (locked) nudgeLock(); drag = null; });
    this.host.addEventListener("wheel", (e) => {
      if (locked) return;
      e.preventDefault(); this.change(this.v + (e.deltaY < 0 ? 5 : -5));
    }, { passive: false });
    this.host.addEventListener("keydown", (e) => {
      const d = { ArrowUp: 5, ArrowRight: 5, ArrowDown: -5, ArrowLeft: -5, PageUp: 25, PageDown: -25 }[e.key];
      if (d && locked) { e.stopPropagation(); nudgeLock(); }
      else if (d) { e.preventDefault(); e.stopPropagation(); this.change(this.v + d); }
    });
  }
}

// ============================================================ audio

const audio = {
  ctx: null, node: null, gain: null, ws: null, mic: null, micStream: null, level: 0,

  async start() {
    this.ctx = new AudioContext({ latencyHint: "interactive" });
    this.gain = this.ctx.createGain();
    this.setVolume($("#vol").value);
    if (this.ctx.audioWorklet) {
      await this.ctx.audioWorklet.addModule(BASE + "static/worklet.js");
      this.node = new AudioWorkletNode(this.ctx, "rx-player", { outputChannelCount: [1] });
      this.feed = (buf) => this.node.port.postMessage(buf, [buf]);
      this.node.port.postMessage({ agc: store.get("agc", true) });
    } else {
      // Plain-http fallback: browsers only expose AudioWorklet on secure origins.
      const ring = makeRing(this.ctx.sampleRate);
      this.node = this.ctx.createScriptProcessor(2048, 0, 1);
      this.node.onaudioprocess = (e) => ring.pull(e.outputBuffer.getChannelData(0));
      this.feed = (buf) => ring.push(new Int16Array(buf));
    }
    this.node.connect(this.gain).connect(this.ctx.destination);
    this.syncSocket();
  },

  // The audio socket is open while listening, or while the audio spectrum needs it.
  wantSpectrum: false,
  syncSocket() {
    const want = !!this.ctx || this.wantSpectrum;
    if (want && !this.ws) this.openSocket();
    else if (!want && this.ws) { const w = this.ws; this.ws = null; w.close(); this.level = 0; }
  },

  openSocket() {
    const sock = new WebSocket(withToken("audio"));
    sock.binaryType = "arraybuffer";
    sock.onmessage = (e) => {
      const s = new Int16Array(e.data);
      let sum = 0; for (let i = 0; i < s.length; i += 4) sum += s[i] * s[i];
      this.level = Math.sqrt(sum / (s.length / 4)) / 32768;
      if (this.wantSpectrum) audioSpectrum.push(s);   // before feed(): that transfers the buffer
      if (this.ctx && this.feed) this.feed(e.data);   // player may still be loading
    };
    sock.onclose = () => {
      if (this.ws === sock) { this.ws = null; setTimeout(() => this.syncSocket(), 1500); }
    };
    this.ws = sock;
  },

  async stop() {
    this.micOff();
    const ctx = this.ctx; this.ctx = null; this.feed = null;
    this.syncSocket();
    if (ctx) await ctx.close();
  },

  setAgc(on) {
    store.set("agc", on);
    if (this.node && this.node.port) this.node.port.postMessage({ agc: on });
  },

  setVolume(v) { if (this.gain) this.gain.gain.value = (v / 100) ** 2 * 2.5; store.set("vol", +v); },

  async micOn() {
    if (!window.isSecureContext || !navigator.mediaDevices) {
      toast("Browsers only allow the microphone over https. Start the server with --https.", "warn");
      return false;
    }
    if (!this.ctx) { await this.start(); setListen(true); }
    this.micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    this.resume();
    const src = this.ctx.createMediaStreamSource(this.micStream);
    this.mic = new AudioWorkletNode(this.ctx, "mic-capture");
    const sink = this.ctx.createGain(); sink.gain.value = 0;
    src.connect(this.mic).connect(sink).connect(this.ctx.destination);
    this.micFrames = 0; this.micSent = 0;
    this.mic.port.onmessage = (e) => {
      this.micFrames++;
      if (this.tone) return;                           // the test tone goes out instead
      if (pttHeld && S.tx && this.ws && this.ws.readyState === 1) { this.ws.send(e.data); this.micSent++; }
    };
    const t = this.micStream.getAudioTracks()[0];
    pageLog(`mic on: "${t ? t.label : "no track"}" ${t ? t.readyState : ""}${t && t.muted ? " MUTED" : ""}, ` +
      `audio ${this.ctx.state} ${this.ctx.sampleRate} Hz, socket ${this.ws ? this.ws.readyState : "none"}`);
    return true;
  },

  // Test tone: a steady 1 kHz at -12 dBFS sent while PTT is held, instead of
  // the mic, so power and ALC hold still while the level is set (mac-shack,
  // 2026-10-18: with speech it was "hard to tell").
  async toneOn() {
    if (!this.ctx) { await this.start(); setListen(true); }
    this.resume();
    const osc = this.ctx.createOscillator(); osc.frequency.value = 1000;
    const g = this.ctx.createGain(); g.gain.value = 0.25;
    const node = new AudioWorkletNode(this.ctx, "mic-capture");
    const sink = this.ctx.createGain(); sink.gain.value = 0;
    osc.connect(g).connect(node).connect(sink).connect(this.ctx.destination);
    osc.start();
    this.micFrames = 0; this.micSent = 0;
    node.port.onmessage = (e) => {
      this.micFrames++;
      if (pttHeld && S.tx && this.ws && this.ws.readyState === 1) { this.ws.send(e.data); this.micSent++; }
    };
    this.tone = { osc, node };
    pageLog("test tone on: 1 kHz, -12 dBFS");
  },

  toneOff() {
    if (!this.tone) return;
    this.tone.osc.stop(); this.tone.node.disconnect();
    this.tone = null;
  },

  // A suspended audio engine runs no worklet: the mic captures nothing and PTT
  // goes out silent (mac-shack, 2026-10-18: "audio suspended", 0 frames). Browsers
  // suspend a context made without a gesture, and can again later; resuming
  // needs a gesture, so the Mic click and every PTT press call this.
  resume() {
    if (this.ctx && this.ctx.state !== "running") {
      this.ctx.resume().catch((e) => pageLog(`audio resume failed: ${e.name}: ${e.message}`));
    }
  },

  // What the mic did while PTT was held (mac-shack, 2026-10-18: the server got
  // no audio and could not tell whether the browser captured or sent any).
  micReport() {
    if (!this.mic && !this.tone) return;
    const t = this.micStream && this.micStream.getAudioTracks()[0];
    pageLog(`voice${this.tone ? " (test tone)" : ""}: mic frames ${this.micFrames}, sent ${this.micSent}; track ${t ? t.readyState : "none"}` +
      `${t && t.muted ? " MUTED" : ""}; audio ${this.ctx ? this.ctx.state : "none"}; socket ` +
      `${this.ws ? this.ws.readyState : "none"}; S.tx ${!!S.tx}`);
    this.micFrames = 0; this.micSent = 0;
  },

  micOff() {
    if (this.micStream) this.micStream.getTracks().forEach((t) => t.stop());
    if (this.mic) this.mic.disconnect();
    this.mic = null; this.micStream = null;
  },
};

// Waterfall from receive audio, for radios that don't send their own spectrum.
// Shows the ~3 kHz passband mapped onto RF using the dial frequency and mode.
const audioSpectrum = {
  N: 1024, RATE: 16000, MAX_HZ: 3000, CW_PITCH: 600,
  buf: new Float32Array(1024), fill: 0,
  init() {
    this.win = Float32Array.from({ length: this.N }, (_, i) => 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (this.N - 1)));
    this.re = new Float32Array(this.N); this.im = new Float32Array(this.N);
    this.bins = Math.round((this.MAX_HZ * this.N) / this.RATE);
    this.span = (this.bins * this.RATE) / this.N;
    this.out = new Uint8Array(this.bins);
    this.db = new Float32Array(this.bins);
    this.ref = null;
  },
  push(s) {
    for (let i = 0; i < s.length; i++) {
      this.buf[this.fill++] = s[i] / 32768;
      if (this.fill === this.N) { this.frame(); this.fill = 0; }   // ~16 lines/s
    }
  },
  frame() {
    const { N, re, im } = this;
    for (let i = 0; i < N; i++) { re[i] = this.buf[i] * this.win[i]; im[i] = 0; }
    fft(re, im);
    for (let k = 0; k < this.bins; k++) this.db[k] = 10 * Math.log10(re[k] * re[k] + im[k] * im[k] + 1e-12);
    // Scale against the audio's own noise floor, so a quiet radio looks like a loud one.
    const floor = Array.from(this.db).sort((a, b) => a - b)[(this.bins * 0.25) | 0];
    this.ref = this.ref === null ? floor : this.ref + (floor - this.ref) * 0.05;
    for (let k = 0; k < this.bins; k++) this.out[k] = clamp(40 + (this.db[k] - this.ref) * 2.5, 0, 160);
    // Audio tone -> RF: upper-sideband modes read upwards, lower-sideband reversed.
    const f = S.freq, span = this.span, line = this.out.slice();
    if (S.mode === "LSB") scope.pushLine(f - span, f, line.reverse());
    else if (S.mode === "CW") scope.pushLine(f - this.CW_PITCH, f - this.CW_PITCH + span, line);
    else if (S.mode === "CW-R") scope.pushLine(f + this.CW_PITCH - span, f + this.CW_PITCH, line.reverse());
    else scope.pushLine(f, f + span, line);
  },
};

// In-place radix-2 FFT.
function fft(re, im) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) { [re[i], re[j]] = [re[j], re[i]]; [im[i], im[j]] = [im[j], im[i]]; }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len, wr = Math.cos(ang), wi = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let cr = 1, ci = 0;
      for (let k = 0; k < len / 2; k++) {
        const a = i + k, b = a + len / 2;
        const tr = re[b] * cr - im[b] * ci, ti = re[b] * ci + im[b] * cr;
        re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
        [cr, ci] = [cr * wr - ci * wi, cr * wi + ci * wr];
      }
    }
  }
}
audioSpectrum.init();

function makeRing(outRate) {
  const cap = 32000, buf = new Float32Array(cap), step = 16000 / outRate;
  let r = 0, n = 0, pos = 0, playing = false;
  return {
    push(s) {
      for (let i = 0; i < s.length; i++) {
        if (n === cap) { r = (r + 1) % cap; n--; }
        buf[(r + n) % cap] = s[i] / 32768; n++;
      }
      if (n > 8000) { const d = n - 3200; r = (r + d) % cap; n -= d; }
    },
    pull(out) {
      if (!playing && n < 3200) { out.fill(0); return; }
      playing = true;
      for (let i = 0; i < out.length; i++) {
        if (n < 2) { playing = false; out.fill(0, i); return; }
        const a = buf[r], b = buf[(r + 1) % cap];
        out[i] = a + (b - a) * pos; pos += step;
        while (pos >= 1) { pos -= 1; r = (r + 1) % cap; n--; }
      }
    },
  };
}

function setListen(onState) { on($("#btn-listen"), onState); $("#btn-listen span").textContent = onState ? "Listening" : "Listen"; }

// ============================================================ transmit

let armed = false, pttHeld = false, pttKeepalive = null, txSince = 0;
let wlArmed = false;   // a Winlink session owns the radio: the page's PTT waits

// Outside the amateur bands a radio won't transmit (the IC-7300 keyed nothing
// on 14.369 MHz, which looked like a dead mic: mac-shack, 2026-10-18).
function outOfBand() {
  const f = S.freq || 0;
  return !!f && !bandOf(f) && !(f >= 222e6 && f <= 225e6);
}

// G90: the page's audio reaches the transmitter only with DATA on (USB-D).
function needsData() {
  return !!(CAPS.tx_audio_needs_data && (audio.mic || audio.tone) && (CAPS.data_modes || []).includes(S.mode) && !S.data);
}

function pttDown() {
  if (!armed || pttHeld || !CFG.tx_allowed || wlArmed || needsData() || outOfBand()) return;
  pttHeld = true;
  audio.resume();                          // a gesture: the one moment a browser lets it run
  $("#ptt").classList.add("active");
  // mic: whether this page sends audio. Only then does the server switch the
  // radio to USB audio and the voice level; otherwise the radio's own mic.
  const mic = !!audio.mic || !!audio.tone;
  if (!mic && CFG.audio) toast("Mic is off: this page sends no audio, the radio's own mic is used. Turn Mic on to talk from here.", "warn");
  send({ t: "set", k: "tx", v: true, mic });
  // Dead-man keep-alive: the server unkeys if these stop.
  pttKeepalive = setInterval(() => send({ t: "set", k: "tx", v: true, mic }), 500);
}

function pttUp() {
  if (!pttHeld) return;
  pttHeld = false;
  setTimeout(() => audio.micReport(), 300);   // after the last frames went out
  clearInterval(pttKeepalive);
  $("#ptt").classList.remove("active");
  send({ t: "set", k: "tx", v: false });
}

function setArmed(v) {
  armed = v && CFG.tx_allowed;
  if (!armed) pttUp();
  const b = $("#btn-arm");
  on(b, armed); b.setAttribute("aria-checked", armed);
  b.querySelector("b").textContent = armed ? "ARMED" : "SAFE";
  render();
}

function renderTx() {
  const ptt = $("#ptt");
  ptt.disabled = !armed || !CFG.tx_allowed || !S.connected || wlArmed || needsData() || outOfBand();
  $("#btn-arm").disabled = !CFG.tx_allowed;
  $("#ptt-sub").textContent = !CFG.tx_allowed ? "receive-only station"
    : wlArmed ? "Winlink session armed · waits until it ends"
    : outOfBand() ? "outside the amateur bands · tune into a band"
    : needsData() ? `turn DATA on · plain ${S.mode} sends no audio from here`
    : !armed ? "arm TX to transmit" : S.tx ? "on air" : "hold · or hold space";
  if (S.tx && !txSince) txSince = performance.now();
  if (!S.tx) txSince = 0;
}

function tickTx() {
  if (txSince) {
    const s = (performance.now() - txSince) / 1000;
    $("#tx-timer").textContent = `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
    $("#ptt-ring").style.width = `${clamp(s / (CFG.tx_timeout || 180), 0, 1) * 100}%`;
  } else {
    $("#tx-timer").textContent = "";
    $("#ptt-ring").style.width = "0";
  }
}

// ============================================================ transmit level

// The codec's transmit level per band, for Winlink (while armed) and Voice
// (while this page's PTT is held). Steps apply live when that use holds the
// codec on this band; Save writes the band's level into the host file.
const txLevel = {
  cfg: null, use: null, snap: {}, band: null,
  LABEL: { winlink: "Winlink", voice: "Voice" },

  config(c) {
    this.cfg = c && c.uses && c.uses.length ? c : null;
    $("#txl").hidden = !this.cfg || !CFG.tx_allowed;
    if (!this.cfg) return;
    const seg = $("#txl-use");
    seg.replaceChildren();
    for (const u of this.cfg.uses) {
      const b = document.createElement("button");
      b.className = "chip"; b.dataset.use = u; b.textContent = this.LABEL[u] || u;
      b.onclick = () => { this.use = u; store.set("txlUse", u); this.render(); };
      seg.append(b);
    }
    const saved = store.get("txlUse", null);
    this.use = this.cfg.uses.includes(saved) ? saved : this.cfg.uses[0];
    $("#txl-save").hidden = !this.cfg.save;
    this.refresh();
  },

  refresh() {
    if (!this.cfg) return;
    this.band = (bandOf(S.freq) || {}).n || null;
    for (const u of this.cfg.uses) send({ t: "codec_get", use: u });
  },

  bandMaybeChanged() {
    if (this.cfg && ((bandOf(S.freq) || {}).n || null) !== this.band) this.refresh();
  },

  event(m) { this.snap[m.use] = m; this.render(); },

  step(db) {
    if (locked) { nudgeLock(); return; }
    send({ t: "codec_step", use: this.use, db });
  },

  render() {
    if (!this.cfg) return;
    document.querySelectorAll("#txl-use .chip").forEach((c) => on(c, c.dataset.use === this.use));
    const s = this.snap[this.use], val = $("#txl-val");
    if (!s) { val.textContent = "…"; return; }
    val.textContent = `${s.band || "default"} ${s.db} dB`;
    val.classList.toggle("live", !!s.live);
    val.classList.toggle("unsaved", !!s.unsaved);
    // In words, not only colour and a dot: phones show no tooltips (review 2026-10-18).
    const notes = [];
    if (s.live) notes.push("on the air now");
    if (!s.own) notes.push(`${s.band || "this band"} uses the default`);
    if (s.unsaved) notes.push(s.save ? "not saved: a restart loses it" : "lost on restart (saving is off)");
    if (s.saved_db != null && s.db > s.saved_db) notes.push(`above the saved ${s.saved_db} dB: watch power and ALC`);
    if (s.min_db != null && s.db <= s.min_db) notes.push(`floor ${s.min_db} dB`);
    if (s.db >= s.max_db) notes.push(`ceiling ${s.max_db} dB`);
    const note = $("#txl-note");
    note.textContent = notes.join(" · ");
    note.classList.toggle("warn", s.saved_db != null && s.db > s.saved_db);
    val.title = (s.live ? "In use now. " : "") + (s.unsaved ? "Not saved: lost on restart. " : "")
      + (s.own ? "" : "This band uses the default level. ")
      + (s.min_db != null ? `Floor ${s.min_db} dB.` : "");
    $("#txl-down").disabled = s.min_db != null && s.db <= s.min_db;
    $("#txl-up").disabled = s.db >= s.max_db;
    $("#txl-save").disabled = !s.save || !s.unsaved;
  },
};

// ============================================================ build controls


function bandStack(target) {
  if (locked) { nudgeLock(); return; }
  const stack = store.get("bandstack", {});
  const cur = bandOf(S.freq);
  if (cur) stack[cur.n] = { f: S.freq, m: S.mode };
  store.set("bandstack", stack);
  const b = BANDS.find((x) => x.n === target);
  const mem = stack[target] || { f: b.f, m: b.m };
  tune(mem.f);
  if (mem.m !== S.mode) setRadio("mode", mem.m);
}

// Rebuild everything that depends on which radio is connected.
function applyCaps() {
  document.title = `${CAPS.name} Remote`;
  $(".brand-name").textContent = CAPS.name;
  buildFreq();

  const bands = $("#bands");
  bands.replaceChildren(...BANDS.filter((b) => b.hi > CAPS.freq[0] && b.lo < CAPS.freq[1])
    .map((b) => chip(b.label || b.n + "m", b.n, bandStack)));
  const modes = $("#modes");
  modes.replaceChildren(...MODE_ORDER.filter((m) => CAPS.modes.includes(m))
    .map((m) => chip(m, m, (v) => setRadio("mode", v))));
  if (CAPS.data_modes.length) modes.append(chip("DATA", "DATA", () => setRadio("data", !S.data)));
  $("#filters").replaceChildren(...Array.from({ length: CAPS.filters }, (_, i) =>
    chip(`FIL${i + 1}`, i + 1, (v) => setRadio("filter", v))));
  $("#agcs").replaceChildren(...CAPS.agc.map(([v, l]) => chip(l, v, (x) => setRadio("agc", x))));
  $("#filters").parentElement.hidden = !CAPS.filters;
  $("#agcs").parentElement.hidden = !CAPS.agc.length;

  // Hide what this radio doesn't have. No radio scope but audio: draw from audio.
  const audioScope = !CAPS.scope && !!CFG.audio;
  $(".scope").hidden = !CAPS.scope && !audioScope;
  $(".scope").classList.toggle("audio-scope", audioScope);
  $(".scope-head .label").textContent = audioScope ? "Audio spectrum · passband" : "Spectrum";
  $("#scope-empty").textContent = audioScope ? "Listening for receive audio…" : "Waiting for scope data…";
  audio.wantSpectrum = audioScope;
  audio.syncSocket();
  $(".sub-vfo").hidden = $("#btn-ab").hidden = $("#btn-swap").hidden = !has("vfo_b");
  // Split needs VFO B over CAT; the G90's own split would TX on a stale B.
  $("#btn-split").hidden = !(has("split") && has("vfo_b"));
  $("#btn-split").parentElement.hidden = !has("vfo_b") && !has("split");
  $("#power-btn").hidden = !has("power_on");
  let shown = 0;
  document.querySelectorAll(".knob").forEach((k) => { k.hidden = !CAPS.levels.includes(k.dataset.key); shown += !k.hidden; });
  $("#knobs").style.setProperty("--knob-cols", shown || 1);
  for (const m of ["po", "swr", "alc"]) $(`#m-${m}`).closest(".tm").hidden = !CAPS.meters.includes(m);
  $(".tx-meters").hidden = !["po", "swr", "alc"].some((m) => CAPS.meters.includes(m));
  $("#t-preamp").hidden = CAPS.preamp.length < 2;
  $("#t-nb").hidden = !has("nb");
  $("#t-nr").hidden = !has("nr");
  $("#t-tuner").hidden = $("#t-tune").hidden = !has("tuner");
}

function applyConfig(cfg) {
  CFG = cfg;
  if (cfg.caps) CAPS = cfg.caps;
  applyCaps();
  $("#sim-badge").hidden = !CFG.sim;
  $("#rx-only-badge").hidden = CFG.tx_allowed;
  $("#btn-listen").disabled = !CFG.audio;
  $("#btn-mic").disabled = !CFG.audio || !CFG.tx_allowed;
  txLevel.config(CFG.codec);
  render(); renderMeters();
}

function buildControls() {
  const spans = $("#spans");
  for (const [v, l] of SPANS) spans.append(chip(l, v, (x) => setRadio("scope_span", x)));
  const steps = $("#steps");
  for (const [v, l] of STEPS) steps.append(chip(l, v, (x) => { step = x; store.set("step", x); render(); }));

  $("#btn-center").onclick = () => setRadio("scope_mode", "center");
  $("#btn-fixed").onclick = () => setRadio("scope_mode", "fixed");
  $("#btn-split").onclick = () => setRadio("split", !S.split);
  $("#btn-ab").onclick = () => setRadio("vfo", S.vfo === "A" ? "B" : "A");
  $("#btn-swap").onclick = () => send({ t: "swap" });
  $("#txl-down").onclick = () => txLevel.step(-1);
  $("#txl-up").onclick = () => txLevel.step(+1);
  $("#txl-save").onclick = () => send({ t: "codec_save", use: txLevel.use });
  $("#txl-tone").onclick = async () => {
    if (audio.tone) { audio.toneOff(); on($("#txl-tone"), false); renderTx(); return; }
    try {
      await audio.toneOn();
      on($("#txl-tone"), true);
      toast("Test tone on: PTT now sends a steady 1 kHz tone instead of the mic. Set the level by power and ALC, then turn it off.", "info");
    } catch (e) { toast(`Test tone: ${e.message}`, "warn"); }
    renderTx();
  };

  $("#t-preamp").onclick = () => setRadio("preamp", (S.preamp + 1) % CAPS.preamp.length);
  $("#t-att").onclick = () => setRadio("att", !S.att);
  $("#t-nb").onclick = () => setRadio("nb", !S.nb);
  $("#t-nr").onclick = () => setRadio("nr", !S.nr);
  $("#t-tuner").onclick = () => setRadio("tuner", S.tuner ? 0 : 1);
  $("#t-tune").onclick = () => { if (armed) setRadio("tuner", 2); };

  $("#power-btn").onclick = () => {
    if (S.connected) { if (confirm("Turn the radio off?")) send({ t: "power", v: false }); }
    else { send({ t: "power", v: true }); toast("Sent power-on to the radio"); }
  };

  $("#btn-listen").onclick = async () => {
    if (audio.ctx) { await audio.stop(); setListen(false); on($("#btn-mic"), false); }
    else {
      try { await audio.start(); setListen(true); }
      catch (e) { toast(`Audio failed: ${e.message}`, "warn"); audio.stop(); }
    }
  };
  $("#btn-mic").onclick = async () => {
    if (audio.mic) { audio.micOff(); on($("#btn-mic"), false); renderTx(); return; }
    try { if (await audio.micOn()) on($("#btn-mic"), true); }
    catch (e) {
      toast(`Microphone: ${e.message}`, "warn");
      pageLog(`mic failed: ${e.name}: ${e.message}`);
    }
    renderTx();
  };
  const agcBtn = $("#btn-agc");
  on(agcBtn, store.get("agc", true));
  agcBtn.onclick = () => { const v = !store.get("agc", true); audio.setAgc(v); on(agcBtn, v); };
  const vol = $("#vol");
  vol.value = store.get("vol", 70);
  vol.oninput = () => audio.setVolume(vol.value);

  $("#btn-lock").onclick = () => setLocked(!locked);
  $("#btn-arm").onclick = () => setArmed(!armed);
  const ptt = $("#ptt");
  ptt.addEventListener("pointerdown", (e) => { ptt.setPointerCapture(e.pointerId); pttDown(); });
  ptt.addEventListener("pointerup", pttUp);
  ptt.addEventListener("pointercancel", pttUp);
  ptt.addEventListener("lostpointercapture", pttUp);
  ptt.addEventListener("contextmenu", (e) => e.preventDefault());

  // Keyboard: ←/→ tune (shift ×10), [ ] step, space = PTT.
  const typing = (e) => e.target.closest("input, textarea");
  window.addEventListener("keydown", (e) => {
    if (typing(e)) return;
    if (e.code === "Space") { e.preventDefault(); if (!e.repeat) pttDown(); return; }
    if (e.key === "l" || e.key === "L") { setLocked(!locked); return; }
    const mult = e.shiftKey ? 10 : 1;
    if (locked && e.key.startsWith("Arrow")) { nudgeLock(); return; }
    if (e.key === "ArrowRight" || e.key === "ArrowUp") { e.preventDefault(); tune(snap(S.freq) + step * mult); }
    else if (e.key === "ArrowLeft" || e.key === "ArrowDown") { e.preventDefault(); tune(snap(S.freq) - step * mult); }
    else if (e.key === "[" || e.key === "]") {
      const i = STEPS.findIndex(([v]) => v === step);
      step = STEPS[clamp(i + (e.key === "]" ? 1 : -1), 0, STEPS.length - 1)][0];
      store.set("step", step); render();
    }
  });
  window.addEventListener("keyup", (e) => { if (e.code === "Space") pttUp(); });
  window.addEventListener("blur", pttUp);
  document.addEventListener("visibilitychange", () => { if (document.hidden) pttUp(); });
}

// ============================================================ boot

// Shared gradient for knob caps.
document.body.insertAdjacentHTML("beforeend", `<svg width="0" height="0" style="position:absolute"><defs>
  <radialGradient id="knobcap" cx="40%" cy="30%" r="80%"><stop offset="0" stop-color="#2a323c"/><stop offset="1" stop-color="#0e1318"/></radialGradient>
</defs></svg>`);

Object.assign(S, { freq: 14_074_000, freq_b: 7_074_000, mode: "USB", filter: 1, vfo: "A", agc: 2, preamp: 0, scope_span: 25000, scope_mode: "center" });
applyCaps();
bindFreq();
buildControls();
setLocked(locked);
const knobs = [...document.querySelectorAll(".knob")].map((h) => new Knob(h));
document.querySelectorAll("#level-style .chip").forEach((c) => (c.onclick = () => setLevelStyle(c.dataset.v)));
setLevelStyle(levelStyle);
const scope = new Scope();
render();
renderMeters();
connect();

setInterval(() => {
  const d = new Date();
  $("#utc").textContent = `${d.toISOString().slice(11, 19)} UTC`;
}, 1000);

(function frame() {
  drawSmeter();
  renderMeters();
  tickTx();
  $("#audio-level").style.width = `${clamp(audio.level * 400, 0, 100)}%`;
  requestAnimationFrame(frame);
})();
