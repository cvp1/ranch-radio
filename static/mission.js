"use strict";

// The Shack's instruments (station.js draws the view): a radio's mini
// spectrum and waterfall from the scope frames its server already sends every
// client, a signal trace of the last two minutes, and the cards' icons.
//
// The view redraws its cards often (an S-meter moves); each instrument is one
// canvas kept here and moved into its card's slot after a redraw, so its
// history survives. Uses common.js (clamp).

const mission = (() => {
  // The radio page's waterfall colours (app.js Scope.makeLut).
  const LUT = (() => {
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
  })();
  const ROWS = 48;                       // waterfall history kept, one row per frame
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

  // A radio's mini scope: spectrum line over a short waterfall, the tuned
  // frequency marked. push() takes a scope frame (type 1: start, end, values).
  class MiniScope {
    constructor() {
      this.el = document.createElement("canvas");
      this.el.className = "mc-scope";
      this.ctx = this.el.getContext("2d");
      this.off = document.createElement("canvas");
      this.octx = this.off.getContext("2d");
      this.n = 0; this.start = 0; this.end = 0; this.avg = null; this.floor = 30;
      this.at = 0; this.freq = 0; this.tx = false; this.dirty = false;
    }

    get live() { return Date.now() - this.at < 5000; }

    push(buf) {
      const dv = new DataView(buf);
      if (buf.byteLength < 18 || dv.getUint8(0) !== 1) return;
      const start = dv.getFloat64(1, true), end = dv.getFloat64(9, true), data = new Uint8Array(buf, 17);
      const n = data.length;
      if (n !== this.n) {
        this.n = n; this.avg = Float32Array.from(data);
        this.off.width = n; this.off.height = ROWS;
        this.octx.fillStyle = "#04060a"; this.octx.fillRect(0, 0, n, ROWS);
        this.row = this.octx.createImageData(n, 1);
      }
      this.start = start; this.end = end;
      const sorted = Array.from(data).sort((a, b) => a - b);
      this.floor += (sorted[(n * 0.25) | 0] - this.floor) * 0.1;
      for (let i = 0; i < n; i++) this.avg[i] += (data[i] - this.avg[i]) * 0.5;
      this.octx.drawImage(this.off, 0, 1);
      const px = this.row.data, lo = this.floor - 3;
      for (let i = 0; i < n; i++) {
        const v = (clamp((data[i] - lo) / 72, 0, 1) * 255) | 0;
        px[i * 4] = LUT[v * 3]; px[i * 4 + 1] = LUT[v * 3 + 1]; px[i * 4 + 2] = LUT[v * 3 + 2]; px[i * 4 + 3] = 255;
      }
      this.octx.putImageData(this.row, 0, 0);
      const first = !this.at;
      this.at = Date.now();
      this.dirty = true;
      if (first) hooks.firstFrame();
    }

    draw() {
      if (!this.dirty || !this.el.isConnected) return;
      this.dirty = false;
      const dpr = window.devicePixelRatio || 1, w = this.el.clientWidth, h = this.el.clientHeight;
      if (!w || !h) return;
      if (this.el.width !== Math.round(w * dpr)) { this.el.width = Math.round(w * dpr); this.el.height = Math.round(h * dpr); }
      const c = this.ctx, W = this.el.width, H = this.el.height, sh = Math.round(H * 0.45);
      c.clearRect(0, 0, W, H);
      c.imageSmoothingEnabled = true;
      c.drawImage(this.off, 0, 0, this.n, ROWS, 0, sh, W, H - sh);
      // The spectrum: a filled line over the waterfall.
      const tx = this.tx, col = tx ? "255,77,94" : theme.rgb("--accent-rgb", [57, 225, 198]).join(",");
      const y = (v) => sh - clamp((v - this.floor + 6) / 80, 0, 1) * (sh - 2);
      const g = c.createLinearGradient(0, 0, 0, sh);
      g.addColorStop(0, `rgba(${col},.45)`); g.addColorStop(1, `rgba(${col},0)`);
      c.beginPath(); c.moveTo(0, sh);
      for (let i = 0; i < this.n; i++) c.lineTo((i / (this.n - 1)) * W, y(this.avg[i]));
      c.lineTo(W, sh); c.closePath(); c.fillStyle = g; c.fill();
      c.beginPath();
      for (let i = 0; i < this.n; i++) c[i ? "lineTo" : "moveTo"]((i / (this.n - 1)) * W, y(this.avg[i]));
      c.strokeStyle = `rgba(${col},.95)`; c.lineWidth = 1.2 * dpr; c.stroke();
      // The tuned frequency.
      if (this.freq > this.start && this.freq < this.end) {
        const x = ((this.freq - this.start) / (this.end - this.start)) * W;
        c.fillStyle = tx ? "rgba(255,77,94,.9)" : "rgba(255,181,71,.85)";
        c.fillRect(Math.round(x), 0, Math.max(1, dpr), H);
      }
      c.fillStyle = "rgba(255,255,255,.06)";
      c.fillRect(0, sh, W, Math.max(1, dpr));
    }
  }

  // The last two minutes of a radio's S-meter (or power on the air), one
  // sample a second: a line, red where it transmitted.
  class Trace {
    constructor() {
      this.el = document.createElement("canvas");
      this.el.className = "mc-trace";
      this.ctx = this.el.getContext("2d");
      this.pts = [];                    // [{v 0..1, tx}]
      this.cur = 0; this.tx = false;
    }

    sample() {
      this.pts.push({ v: this.cur, tx: this.tx });
      if (this.pts.length > 120) this.pts.shift();
      this.draw();
    }

    draw() {
      if (!this.el.isConnected) return;
      const dpr = window.devicePixelRatio || 1, w = this.el.clientWidth, h = this.el.clientHeight;
      if (!w || !h) return;
      if (this.el.width !== Math.round(w * dpr)) { this.el.width = Math.round(w * dpr); this.el.height = Math.round(h * dpr); }
      const c = this.ctx, W = this.el.width, H = this.el.height, n = this.pts.length;
      c.clearRect(0, 0, W, H);
      if (n < 2) return;
      const accent = theme.rgb("--accent-rgb", [57, 225, 198]).join(",");
      const x = (i) => W - ((n - 1 - i) / 119) * W, y = (v) => H - 1 - v * (H - 3);
      for (let i = 1; i < n; i++) {
        c.beginPath();
        c.moveTo(x(i - 1), y(this.pts[i - 1].v)); c.lineTo(x(i), y(this.pts[i].v));
        c.strokeStyle = this.pts[i].tx ? "rgba(255,77,94,.95)" : `rgba(${accent},.8)`;
        c.lineWidth = 1.4 * dpr; c.stroke();
      }
    }
  }

  // Line icons, 24x24, stroked with currentColor.
  const P = {
    radio: '<rect x="3" y="8" width="18" height="12" rx="2"/><path d="M7 8 17 3"/><circle cx="15.5" cy="14" r="3"/><path d="M6 12h3M6 15h3"/>',
    handheld: '<rect x="7" y="7" width="10" height="15" rx="2"/><path d="M9 7V2"/><path d="M10 11h4M10 14h4"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
    packet: '<path d="M12 20V10"/><path d="M8.5 13.5a5 5 0 0 1 7 0M6 11a8.5 8.5 0 0 1 12 0"/><circle cx="12" cy="9" r="1.2"/>',
    mesh: '<circle cx="5" cy="6" r="2"/><circle cx="19" cy="7" r="2"/><circle cx="12" cy="18" r="2"/><circle cx="12" cy="10" r="1.6"/><path d="M6.7 7 10.6 9.3M17.2 7.8 13.5 9.5M12 11.6v4.4M6 8l5 8.5M18 9l-4.8 7.6"/>',
    chart: '<path d="M3 20h18"/><path d="M5 16c3-9 5-9 7-4s4 5 7-6"/>',
    book: '<path d="M4 4h11a3 3 0 0 1 3 3v13H7a3 3 0 0 1-3-3z"/><path d="M4 17a3 3 0 0 1 3-3h11"/>',
    list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
    map: '<path d="m9 4-6 2v14l6-2 6 2 6-2V4l-6 2z"/><path d="M9 4v14M15 6v14"/>',
    dot: '<circle cx="12" cy="12" r="4"/>',
  };
  const KIND = { winlink: "mail", packet: "packet", mesh: "mesh", meshcore: "mesh", propagation: "chart",
    logbook: "book", roster: "list", map: "map", uvpro: "handheld", opengd77: "handheld" };
  function icon(kind) {
    return `<svg class="mc-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" ` +
      `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[kind] || P.dot}</svg>`;
  }

  const scopes = {}, traces = {};
  const hooks = { firstFrame: () => {} };     // station.js: redraw, so the scope gets its slot
  const scopeOf = (id) => (scopes[id] ||= new MiniScope());
  const traceOf = (id) => (traces[id] ||= new Trace());
  function forget(keep) {
    for (const id of Object.keys(scopes)) if (!keep.includes(id)) delete scopes[id];
    for (const id of Object.keys(traces)) if (!keep.includes(id)) delete traces[id];
  }

  // After a redraw: each instrument back into its card's slot.
  function mount(root) {
    for (const slot of root.querySelectorAll("[data-scope]")) {
      const s = scopes[slot.dataset.scope];
      if (s?.at) { slot.append(s.el); s.dirty = true; }    // a radio with no scope: no empty box
    }
    for (const slot of root.querySelectorAll("[data-trace]")) {
      const t = traces[slot.dataset.trace];
      if (t) { slot.append(t.el); t.draw(); }
    }
  }

  // Draw scopes at the screen's pace (at most ~15 a second), only when shown.
  let last = 0;
  function frame(t) {
    requestAnimationFrame(frame);
    if (t - last < (reduced() ? 250 : 66)) return;
    last = t;
    for (const s of Object.values(scopes)) s.draw();
  }
  requestAnimationFrame(frame);
  setInterval(() => { for (const tr of Object.values(traces)) tr.sample(); }, 1000);

  return { icon, KIND, scopeOf, traceOf, forget, mount, hooks, scopes, traces };
})();
