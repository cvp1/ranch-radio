"use strict";

// The wall (UX plan PR 5a, 2026-11-19): a read-only kiosk view of the
// station at #/wall, or ?wall for a kiosk browser's start page. Display only:
// the Map large with its night side on, a big UTC clock with local time and
// date, the band strip, the focus radio's spectrum and waterfall, and the
// last eight events this page has seen.
//
// What keeps it read-only, and why each piece is here:
//   - No radio frame. The radio page binds Space to PTT and the arrows to
//     tuning at window scope (app.js); station.js builds no frame while the
//     wall is the route (prepareFocus), keeps any it has hidden, and this
//     file takes the focus back from one that had it.
//   - Every view is inert, and so are the top bar and the toasts. The rig bar
//     is not: its STOP TX must work from here. station.js makes the bar's
//     radios and Cancel inert (barInert), so STOP TX and Exit are the only
//     controls that take input. The station's number keys are off here.
//   - The spectrum is mission.js's MiniScope for the focus radio, fed by the
//     scope frames the station's own socket to that radio already receives;
//     this file opens no socket of any kind.
//   - The Map's head (its power switch) and its controls are not rendered
//     (station.css, map.css); its night side is on for the wall through
//     addons.layout({wall: true}), never by writing map.layers.
//   - A screen wake lock, only in a secure context, only while the wall is
//     shown and the page visible; a refusal is said in the footer, released
//     on exit.
// station.js calls show(on) from its show(), mount() after Mission Control
// moves the scopes into its cards, and note() for the ticker. Uses common.js
// ($, esc, fmtFreq, bandOf), mission.js and bands.js.

const wall = (() => {
  const TICKER_MAX = 8;
  const events = [];              // [{t, text, level}], newest first: page memory only
  let on = false, host = { scope: () => null, radio: () => null, range: () => null, linked: () => true };
  let tickT = 0;

  // ?wall: the kiosk's start page. The route is #/wall; leaving drops the
  // parameter again (normalize), so a reload after Exit is not the wall.
  const params = () => new URLSearchParams(location.search);
  if (params().has("wall") && location.hash !== "#/wall") {
    history.replaceState(null, "", `${location.pathname}${location.search}#/wall`);
  }
  function normalize() {
    const p = params();
    if (!p.has("wall")) return;
    p.delete("wall");
    const q = p.toString();
    history.replaceState(null, "", `${location.pathname}${q ? `?${q}` : ""}${location.hash}`);
  }

  // ---------- the wake lock
  let lock = null, asking = false, lockWord = "";
  async function grab() {
    if (!on || lock || asking || document.visibilityState !== "visible") return;
    if (!window.isSecureContext || !navigator.wakeLock?.request) {
      lockWord = "The screen may sleep: keeping it awake needs https or localhost";
      return draw();
    }
    asking = true;
    try {
      const l = await navigator.wakeLock.request("screen");
      if (!on || document.visibilityState !== "visible") { l.release().catch(() => {}); return; }   // left while asking
      lock = l;
      lock.addEventListener?.("release", () => { if (lock === l) { lock = null; draw(); } });
      lockWord = "Screen kept awake";
    } catch (e) {
      lockWord = `The screen may sleep: the browser refused to keep it awake${e?.name ? ` (${e.name})` : ""}`;
    } finally { asking = false; draw(); }
  }
  function drop() {
    const l = lock;
    lock = null;
    if (l) { try { l.release().catch(() => {}); } catch { /* already released */ } }
  }
  // The browser releases a lock when the page is hidden; ask again on return.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") grab(); else drop();
  });

  // ---------- entering and leaving
  const blockers = () => [...document.querySelectorAll(".st-main > .st-view, .topbar, #toasts")];

  function show(want) {
    want = !!want;
    if (want === on) { if (on) { inertAll(); mount(); } return; }
    on = want;
    document.body.classList.toggle("wall", on);
    $("#wall-exit").hidden = !on;
    if (on) {
      inertAll();
      // A radio frame that had the keyboard keeps it while hidden: take it
      // back, so Space and the arrows reach this document and nothing else.
      const a = document.activeElement;
      if (a && a !== document.body) a.blur();
      tick();
      clearInterval(tickT);
      tickT = setInterval(tick, 1000);
      grab();
    } else {
      for (const e of blockers()) e.inert = false;
      clearInterval(tickT);
      drop();
      lockWord = "";
      normalize();
    }
  }
  // Views made later (an add-on switched on) are inert too.
  function inertAll() { for (const e of blockers()) e.inert = true; }

  function exit() { location.hash = "#/home"; }
  $("#wall-exit").onclick = exit;
  document.addEventListener("keydown", (e) => {
    if (on && e.key === "Escape" && !e.repeat) { e.preventDefault(); exit(); }
  });

  // ---------- drawing
  // The focus radio's MiniScope, back in the wall's slot (Mission Control's
  // redraw moves it into its card).
  function mount() {
    if (!on) return;
    const slot = $("#wall-scope"), s = host.scope();
    if (s?.at && s.el.parentNode !== slot) { slot.replaceChildren(s.el); s.dirty = true; }
    if (!s?.at && slot.firstChild) slot.replaceChildren();
  }

  function tick() {
    if (!on) return;
    inertAll();
    mount();
    bandStrip.render($("#wall-bands"), { range: host.range() });
    draw();
  }

  function draw() {
    if (!on) return;
    const now = new Date();
    $("#wall-utc").textContent = now.toISOString().slice(11, 19);
    $("#wall-local").textContent = `${now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} local`;
    $("#wall-date").textContent = now.toLocaleDateString([], { weekday: "long", year: "numeric", month: "long", day: "numeric" });
    const r = host.radio(), s = host.scope();
    $("#wall-radio").textContent = !r ? "No HF radio connected"
      : `${r.name} · ${r.freq ? `${fmtFreq(r.freq)} ${r.mode || ""}${r.data ? "-D" : ""}` : "…"}` +
        `${r.freq && bandOf(r.freq) ? ` · ${bandOf(r.freq).label || `${bandOf(r.freq).n} m`}` : ""}${r.tx ? " · ON AIR" : ""}`;
    $("#wall-radio").classList.toggle("tx", !!r?.tx);
    // A scope that stopped is said, never left frozen as if live.
    const sw = $("#wall-scope-note");
    sw.textContent = !r ? "" : !s?.at ? "No spectrum from this radio" : !s.live ? "Spectrum stopped: no scope frames in 5 s" : "";
    sw.hidden = !sw.textContent;
    const list = $("#wall-ticker");
    const html = events.map((e) => `<li class="${esc(e.level)}"><time>${new Date(e.t).toISOString().slice(11, 16)}</time>` +
      `<span>${esc(e.text)}</span></li>`).join("") || `<li class="dim"><span>Nothing yet since this page opened</span></li>`;
    if (list.innerHTML !== html) list.innerHTML = html;
    const foot = [host.linked() ? "" : "Station link down: reconnecting", lockWord].filter(Boolean);
    const f = $("#wall-foot");
    const ft = foot.join(" · ");
    if (f.textContent !== ft) f.textContent = ft;
    f.classList.toggle("bad", !host.linked());
  }

  // An event for the ticker (kept whatever the view, so the wall opens with them).
  function note(text, level = "info") {
    if (!text) return;
    events.unshift({ t: Date.now(), text: String(text).slice(0, 160), level });
    if (events.length > TICKER_MAX) events.length = TICKER_MAX;
    draw();
  }

  return { show, mount, note, shown: () => on, setHost(h) { host = { ...host, ...h }; } };
})();
