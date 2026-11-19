"use strict";

// The page's theme (2026-11-19). Loaded in <head>, before the body paints, so
// the page never flashes the wrong colours. The choice lives in this
// browser's localStorage ("rr.theme"); the station's radio and packet tabs are
// same-origin iframes, so one pick reaches them all (the storage event).
// The palettes themselves are themes.css; this file only names them.

const theme = (() => {
  const KEY = "rr.theme";
  const DEFAULT = "midnight";
  // [id, label, background, accent]: the swatch in the menu.
  const THEMES = [
    ["midnight", "Midnight blue", "#060a18", "#6ea8ff"],
    ["classic", "Mint (classic)", "#07090d", "#39e1c6"],
    ["amber", "Amber", "#0c0905", "#ffb547"],
    ["sandstone", "Sandstone", "#120b09", "#e8875b"],
    ["aurora", "Aurora", "#0b0816", "#b18cff"],
    ["contrast", "High contrast", "#000000", "#ffd60a"],
  ];
  const known = (id) => THEMES.some((t) => t[0] === id);

  function get() {
    let id = null;
    try { id = localStorage.getItem(KEY); } catch { /* storage blocked: the default */ }
    return known(id) ? id : DEFAULT;
  }

  function apply(id) {
    const t = THEMES.find((x) => x[0] === id) || THEMES[0];
    document.documentElement.dataset.theme = t[0];
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = t[2];
    // Canvases (waterfall, scopes) read their colours from CSS; tell them.
    window.dispatchEvent(new CustomEvent("rr-theme", { detail: t[0] }));
    for (const el of document.querySelectorAll(".theme-pick")) render(el);
  }

  function set(id) {
    if (!known(id)) return;
    try { localStorage.setItem(KEY, id); } catch { /* still applies to this page */ }
    apply(id);
  }

  // "r, g, b" of a CSS variable that holds one (e.g. --accent-rgb), as numbers.
  function rgb(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    const p = v.split(",").map(Number);
    return p.length === 3 && p.every((n) => Number.isFinite(n)) ? p : fallback;
  }

  const ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3a9 9 0 1 0 0 18c1.1 0 1.7-.8 1.7-1.7 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 0-.9.8-1.7 1.7-1.7H16a5 5 0 0 0 5-5c0-3.9-4-7.2-9-7.2z"/><circle cx="7.5" cy="11" r="1.2"/><circle cx="10.5" cy="7.2" r="1.2"/><circle cx="15" cy="7.5" r="1.2"/></svg>';

  function render(el) {
    const cur = get();
    const open = el.classList.contains("open");
    el.innerHTML = `<button type="button" title="Theme" aria-label="Theme" aria-haspopup="true" aria-expanded="${open}">${ICON}</button>` +
      (open ? `<div class="theme-menu" role="menu">${THEMES.map(([id, label, bg, ac]) =>
        `<button type="button" role="menuitemradio" aria-checked="${id === cur}" data-theme-id="${id}" class="${id === cur ? "on" : ""}">` +
        `<span class="theme-sw" style="background:linear-gradient(90deg,${bg} 50%,${ac} 50%)"></span>${label}</button>`).join("")}</div>` : "");
    el.firstElementChild.onclick = (e) => { e.stopPropagation(); el.classList.toggle("open"); render(el); };
    for (const b of el.querySelectorAll("[data-theme-id]")) {
      b.onclick = (e) => { e.stopPropagation(); el.classList.remove("open"); set(b.dataset.themeId); };
    }
  }

  // A picker goes wherever a page puts <span class="theme-pick"></span>.
  function mount() {
    for (const el of document.querySelectorAll(".theme-pick")) render(el);
    document.addEventListener("click", (e) => {
      for (const el of document.querySelectorAll(".theme-pick.open")) {
        if (!el.contains(e.target)) { el.classList.remove("open"); render(el); }
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      for (const el of document.querySelectorAll(".theme-pick.open")) { el.classList.remove("open"); render(el); }
    });
  }

  window.addEventListener("storage", (e) => { if (e.key === KEY) apply(get()); });
  document.documentElement.dataset.theme = get();   // before first paint
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => { apply(get()); mount(); });
  else { apply(get()); mount(); }

  return { THEMES, DEFAULT, get, set, rgb };
})();
