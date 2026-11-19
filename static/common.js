"use strict";

// Shared by the radio page (app.js) and the station page (station.js), and
// used by winlink.js and mesh.js: DOM and storage helpers, toasts, the band
// table, the page's base path and the access token.

const BANDS = [
  { n: "160", lo: 1.8e6, hi: 2.0e6, f: 1_900_000, m: "LSB" },
  { n: "80", lo: 3.5e6, hi: 4.0e6, f: 3_750_000, m: "LSB" },
  { n: "60", lo: 5.25e6, hi: 5.45e6, f: 5_357_000, m: "USB" },
  { n: "40", lo: 7.0e6, hi: 7.3e6, f: 7_150_000, m: "LSB" },
  { n: "30", lo: 10.1e6, hi: 10.15e6, f: 10_136_000, m: "USB" },
  { n: "20", lo: 14.0e6, hi: 14.35e6, f: 14_200_000, m: "USB" },
  { n: "17", lo: 18.068e6, hi: 18.168e6, f: 18_130_000, m: "USB" },
  { n: "15", lo: 21.0e6, hi: 21.45e6, f: 21_250_000, m: "USB" },
  { n: "12", lo: 24.89e6, hi: 24.99e6, f: 24_950_000, m: "USB" },
  { n: "10", lo: 28.0e6, hi: 29.7e6, f: 28_400_000, m: "USB" },
  { n: "6", lo: 50.0e6, hi: 54.0e6, f: 50_150_000, m: "USB" },
  { n: "2", lo: 144e6, hi: 148e6, f: 144_200_000, m: "USB" },
  { n: "70", label: "70cm", lo: 420e6, hi: 450e6, f: 432_100_000, m: "USB" },
  { n: "23", label: "23cm", lo: 1240e6, hi: 1300e6, f: 1_296_100_000, m: "USB" },
];

function bandOf(f) { return BANDS.find((b) => f >= b.lo && f <= b.hi); }

// A frequency in Hz as MHz.kHz.Hz (14.074.000). Shared since the station's rig
// bar shows it too (UX plan PR 1a, 2026-11-19; was app.js fmtFreq and a copy,
// station.js fmtF). A missing frequency reads 0.000.000, never NaN.
function fmtFreq(f) {
  const s = String(Math.round(f || 0)).padStart(7, "0");
  return `${s.slice(0, -6) || "0"}.${s.slice(-6, -3)}.${s.slice(-3)}`;
}

// Typed text -> Hz, or null. Under 100 is MHz, under 100000 kHz, else Hz.
// Shared since the station's rig bar takes a typed frequency too (UX plan
// PR 1d, 2026-11-19; was app.js): one rule on both pages.
function parseFreq(txt) {
  const v = parseFloat(String(txt ?? "").replace(",", "."));
  if (!isFinite(v) || v <= 0) return null;
  if (v < 100) return v * 1e6;          // MHz
  if (v < 100_000) return v * 1e3;      // kHz
  return v;                              // Hz
}

// The band stack: where band `target` was last left, else its default.
// Records `cur` ({freq, mode}: where the radio is now) for its own band
// first. One stored stack (store "bandstack") for the radio page and the
// station page (UX plan PR 1d, 2026-11-19; was the core of app.js
// bandStack()). null: no such band.
function bandStackTarget(target, cur) {
  const b = BANDS.find((x) => x.n === target);
  if (!b) return null;
  const stack = store.get("bandstack", {}) || {};
  const here = cur && cur.freq ? bandOf(cur.freq) : null;
  if (here) stack[here.n] = { f: cur.freq, m: cur.mode };
  try { store.set("bandstack", stack); } catch { /* storage off: the defaults */ }
  return stack[target] || { f: b.f, m: b.m };
}

// Said in the title of every control that can change band (UX plan PR 1d,
// 2026-11-19): with auto-tune on, the radio server tunes the ATU on a band
// it has not tuned this run, and a tune is a carrier on the air.
function bandChangeNote(autoTune) {
  return autoTune ? "Auto-tune is on here: a band change transmits a tune carrier."
    : "On a station with auto-tune, a band change transmits a tune carrier (off here).";
}

// skywatch's propagation regions and their names. Shared since Mission
// Control's band strip names a forecast's region too (UX plan PR 3a,
// 2026-11-19; was propagation.js's own list): one name for a region on every
// page. The order is propagation.js's colour slots.
const REGION_NAMES = [
  ["JA", "Japan"], ["NA_EAST", "NA east"], ["EU", "Europe"], ["SA", "S America"],
  ["VK_ZL", "VK/ZL"], ["NA_WEST", "NA west"], ["OCEANIA_DEEP", "Pacific"],
];
const regionName = (r) => (REGION_NAMES.find(([k]) => k === r) || [r, String(r ?? "").replace(/_/g, " ")])[1];

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

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const store = {
  get(k, d) { try { const v = localStorage.getItem("ic7300." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { localStorage.setItem("ic7300." + k, JSON.stringify(v)); },
};

// action: {label, fn, ms} adds a button (Undo) and keeps the toast up ms.
function toast(msg, level = "info", action = null) {
  const t = document.createElement("div");
  t.className = `toast ${level}`;
  t.textContent = msg;
  if (action) {
    const b = document.createElement("button");
    b.className = "chip chip-mini toast-act";
    b.textContent = action.label;
    b.onclick = () => { t.remove(); action.fn(); };
    t.append(" ", b);
  }
  $("#toasts").append(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, action?.ms || 4200);
}


function on(el, cond) { el.classList.toggle("on", !!cond); }

function chip(label, value, onClick, cls = "chip") {
  const b = document.createElement("button");
  b.className = cls; b.textContent = label; b.dataset.v = value;
  b.addEventListener("click", () => onClick(value, b));
  return b;
}

const params = new URLSearchParams(location.search);
let token = params.get("token") || store.get("token", "");
if (params.get("token")) store.set("token", token);
// Paths are relative to the page, which may be served at / or under the
// station page's /r/<radio>/.
const BASE = location.pathname.replace(/[^/]*$/, "");
const wsBase = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${BASE}`;
const withToken = (path) => wsBase + path + (token ? `?token=${encodeURIComponent(token)}` : "");
