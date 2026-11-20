"use strict";

// The call card (2026-11-19, UX plan PR 4b): what the logbook says about one
// callsign, on the Roster (an info button on each row opens it) and inside the
// Map's station card (the same block). It asks the station once per card:
//   {"t": "logsync", "do": "lookup", "id", "call", "addon", grid?, band?, mode?}
// and the station answers that page only, through the asking tab's add-on
// route: {"t": "addon", "addon": "roster"|"map", "callcard": {id, call, qsos,
// more, need, error}} (station.py call_lookup). The answer is the latest QSOs
// with that exact call (date, band, mode, confirmed), at most 20, and the need
// when the decode's grid, band and mode were sent.
//
// Read-only: a lookup never writes the ledger and never transmits; the info
// button's click stops at the button, so it never reaches the Roster row's
// Reply. A card holds one request at a time (a "slot": the Roster's popover,
// the Map's card): an answer whose id is not a slot's current one is late,
// for a card since closed or switched, and is ignored.
//
// The QRZ link is the only thing that leaves the station, and only when the
// operator follows it: the page itself fetches nothing outside the station.
// Uses common.js (esc) and the page's send().

const callcard = (() => {
  let seq = 0, pop = null;
  const slots = {};                    // slot -> {id, call, state: "wait"|"ok"|"err", data}
  const NEED = {
    dxcc: "New DXCC", dxcc_band: "New band", dxcc_mode: "New mode", grid: "New grid",
    grid_band: "Grid on band", grid_mode: "Grid in mode", dxcc_unconfirmed: "DXCC unconfirmed", call_band_mode: "New call",
  };

  function ask(slot, addon, call, ctx = {}) {
    const id = `cc${++seq}`;
    slots[slot] = { id, call, state: "wait", data: null };
    const m = { t: "logsync", do: "lookup", id, call, addon };
    for (const k of ["grid", "band", "mode"]) if (ctx[k]) m[k] = String(ctx[k]);
    send(m);
  }

  function qrz(call) {
    return `<a class="cc-qrz" href="https://www.qrz.com/db/${encodeURIComponent(call)}" target="_blank"
      rel="noopener noreferrer" title="Open ${esc(call)} on QRZ.com (sends the callsign to QRZ)">QRZ ↗</a>`;
  }

  function inner(s) {
    const head = `<div class="cc-h"><b class="cc-call">${esc(s.call)}</b><span class="cc-sp"></span>${qrz(s.call)}</div>`;
    if (s.state === "wait") return `${head}<div class="cc-dim">Looking it up in the logbook…</div>`;
    if (s.state === "err") return `${head}<div class="cc-err">${esc(s.data.error)}</div>`;
    const a = s.data, q = a.qsos || [];
    const need = a.need == null ? `<span class="cc-dim" title="No decode to judge it by, or the needs index is still building">Need: unavailable</span>`
      : a.need.level ? `<span class="cc-need">Need: ${esc(NEED[a.need.level] || a.need.level)}</span>`
      : `<span class="cc-dim">Need: nothing new</span>`;
    const conf = q.filter((r) => r.confirmed).length;
    const sum = !q.length ? "Not in the logbook."
      : `${a.more ? `Latest ${q.length}` : q.length} QSO${q.length === 1 ? "" : "s"} · ${conf} confirmed`;
    const rows = q.map((r) => `<tr><td>${esc(r.date)}</td><td>${esc(r.band)}</td><td>${esc(r.mode)}</td>
      <td class="cc-conf">${r.confirmed ? `<span title="confirmed">✓</span>` : ""}</td></tr>`).join("");
    return `${head}<div class="cc-sum"><span>${sum}</span>${need}</div>
      ${q.length ? `<div class="cc-rows"><table class="cc-t"><tr><th>Date</th><th>Band</th><th>Mode</th><th>Cfm</th></tr>${rows}</table></div>` : ""}`;
  }

  function paint(slot) {
    const s = slots[slot];
    for (const el of document.querySelectorAll(`.cc-block[data-cc-slot="${slot}"]`)) {
      if (s && el.dataset.ccCall === s.call) el.innerHTML = inner(s);
    }
  }

  // The block for a card that re-renders itself (the Map's): the same call
  // reuses the slot's answer; a new call asks once.
  function block(slot, addon, call, ctx) {
    if (!slots[slot] || slots[slot].call !== call) ask(slot, addon, call, ctx);
    return `<div class="cc-block" data-cc-slot="${esc(slot)}" data-cc-call="${esc(call)}">${inner(slots[slot])}</div>`;
  }

  // The answer: only a slot's current request is taken.
  function answer(a) {
    if (!a) return;
    const slot = Object.keys(slots).find((k) => slots[k].id === a.id);
    if (!slot) return;                                   // late: that card closed or moved on
    const s = slots[slot];
    if (a.error) { s.state = "err"; s.data = { error: a.error }; }
    else if (a.call !== s.call) { s.state = "err"; s.data = { error: "the answer was for another call" }; }
    else { s.state = "ok"; s.data = a; }
    paint(slot);
  }

  // The info button for a Roster row. Its own click opens the card and stops
  // there (the row's click is a Reply to JTDX).
  function button(r) {
    return `<button type="button" class="cc-info" data-cc-call="${esc(r.call)}" data-cc-grid="${esc(r.grid || "")}"
      data-cc-band="${esc(r.band || "")}" data-cc-mode="${esc(r.mode || "")}"
      title="What the logbook says about ${esc(r.call)}" aria-label="Call card for ${esc(r.call)}">i</button>`;
  }

  function wire(root) {
    for (const b of root.querySelectorAll(".cc-info")) {
      b.onclick = (e) => {
        e.stopPropagation();
        e.preventDefault();
        open(b.dataset.ccCall, b, { grid: b.dataset.ccGrid, band: b.dataset.ccBand, mode: b.dataset.ccMode });
      };
    }
  }

  function place(anchor) {
    if (!pop || !anchor || !anchor.isConnected) return;
    const a = anchor.getBoundingClientRect(), p = pop.getBoundingClientRect();
    // Beside the button when there is room, so the other rows' buttons stay
    // in reach; on a phone, below it (or above, near the bottom).
    if (a.right + 8 + p.width <= innerWidth - 8) {
      pop.style.left = `${a.right + 8}px`;
      pop.style.top = `${Math.max(8, Math.min(a.top - 8, innerHeight - p.height - 8))}px`;
      return;
    }
    const x = Math.max(8, Math.min(a.left, innerWidth - p.width - 8));
    const below = a.bottom + 6 + p.height <= innerHeight - 8;
    pop.style.left = `${x}px`;
    pop.style.top = `${Math.max(8, below ? a.bottom + 6 : a.top - p.height - 6)}px`;
  }

  // The Roster's card: one popover on the page, outside the table (the
  // table is rebuilt on every push and would take it with it).
  function open(call, anchor, ctx) {
    if (!pop) {
      pop = document.createElement("div");
      pop.className = "cc-pop";
      pop.setAttribute("role", "dialog");
      pop.addEventListener("click", (e) => { e.stopPropagation(); if (e.target.closest("[data-cc-x]")) close(); });
      document.body.append(pop);
      document.addEventListener("click", (e) => { if (pop && !pop.hidden && !e.target.closest(".cc-pop, .cc-info")) close(); });
      document.addEventListener("keydown", (e) => { if (e.key === "Escape" && pop && !pop.hidden) close(); });
    }
    delete slots.pop;                                    // a fresh look every open
    pop.innerHTML = `<button type="button" class="cc-x" data-cc-x title="Close" aria-label="Close">✕</button>${block("pop", "roster", call, ctx)}`;
    pop.setAttribute("aria-label", `Call card for ${call}`);
    pop.hidden = false;
    place(anchor);
  }

  function close() {
    if (pop) pop.hidden = true;
    delete slots.pop;                                    // its answer, if still coming, is late
  }

  return { block, answer, button, wire, open, close, slots: () => slots };
})();
window.callcard = callcard;
