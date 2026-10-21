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

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const store = {
  get(k, d) { try { const v = localStorage.getItem("ic7300." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { localStorage.setItem("ic7300." + k, JSON.stringify(v)); },
};

function toast(msg, level = "info") {
  const t = document.createElement("div");
  t.className = `toast ${level}`;
  t.textContent = msg;
  $("#toasts").append(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, 4200);
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
