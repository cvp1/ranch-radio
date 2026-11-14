"use strict";

// The Logbook tab: the station's web logbook (Wavelog) in a frame, with a link
// to open it in its own tab. The server says where it is (addons/logbook.py):
// {url} as given, or {port, scheme, path} on the name this page was opened at,
// so a logbook on the radio's machine is the same site as the page and its
// login cookie holds inside the frame.
//
// The frame is loaded the first time the tab is shown, not with the page.
// A secure page can't frame a plain-http logbook (mixed content), so then the
// tab offers only the link. Uses common.js (esc).
//
// When the station runs the logbook sync (logsync.py), its status comes in the
// snapshot ("logsync") and in {"t": "addon", addon: "logbook", logsync} events:
// a strip of counts and holds above the frame, and the latest QSOs with a tick
// per destination. Display only, plus the operator's Send correction for a QSO
// edited after upload; this tab never starts or stops the sync.

(() => {
  let body = null, snap = {}, loadedSrc = "", seen = null;

  function target() {
    if (snap.url) return snap.url;
    if (!snap.port) return "";
    const host = location.hostname.includes(":") ? `[${location.hostname}]` : location.hostname;
    return `${snap.scheme || "https"}://${host}:${snap.port}${snap.path || "/"}`;
  }

  // Why the frame can't show it, or "" when it can.
  function blocked(url) {
    return location.protocol === "https:" && url.startsWith("http:")
      ? "This page is secure (https) and the logbook is plain http, so the browser won't show it here. Open it in its own tab, or give the logbook an https address."
      : "";
  }

  function render() {
    if (!body) return;
    const url = target();
    if (!url) { body.innerHTML = ""; loadedSrc = ""; return; }
    const why = blocked(url);
    let frame = body.querySelector(".lb-frame");
    if (!body.querySelector(".lb-bar") || body.dataset.url !== url) {
      body.dataset.url = url;
      loadedSrc = "";
      body.innerHTML = `<div class="lb">
          <div class="lb-sync" hidden></div>
          <div class="lb-bar">
            <a class="chip chip-mini lb-open" href="${esc(url)}" target="_blank" rel="noopener">Open in its own tab</a>
            <span class="lb-url stat">${esc(url)}</span>
          </div>
          ${why ? `<div class="lb-why st-empty">${esc(why)}</div>`
                : `<iframe class="lb-frame" title="Logbook" referrerpolicy="no-referrer"></iframe>
                   <div class="lb-hint stat">Blank, or a certificate warning? Open it in its own tab once and accept the logbook's certificate, then come back.</div>`}
        </div>`;
      frame = body.querySelector(".lb-frame");
      watch();
    }
    renderSync();
    if (frame && visible() && loadedSrc !== url) { frame.src = url; loadedSrc = url; }
  }

  const NAMES = { wavelog: "Wavelog", qrz: "QRZ", lotw: "LoTW" };
  const TICK = {
    sent: ["✓", "sent"], submitted: ["↑", "uploaded, waiting for LoTW to list it"],
    accepted: ["✓", "on LoTW"], confirmed: ["✓✓", "confirmed"], pending: ["…", "waiting to send"],
    attempting: ["…", "sending"], unknown: ["?", "result unknown: being checked"],
    held_backfill: ["–", "held for the backfill review"], held: ["✕", "held"],
  };

  // The sync's strip and recent QSOs. Every value is escaped; the server sends
  // only counts, states and reason classes (never a URL or a credential).
  function renderSync() {
    const el = body && body.querySelector(".lb-sync");
    if (!el) return;
    const s = snap.logsync;
    if (!s) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false;
    if (!s.loaded || s.running === false) {
      el.innerHTML = `<div class="lb-sync-bar st-empty">Logbook sync: ${esc(s.error || "not running")}</div>`;
      return;
    }
    const counts = s.counts || {};
    const parts = (s.dests || []).map((d) => {
      const c = counts[d] || {};
      const done = (c.sent || 0) + (c.accepted || 0) + (c.confirmed || 0) + (c.submitted || 0);
      const wait = (c.pending || 0) + (c.attempting || 0) + (c.unknown || 0);
      const held = c.held || 0;
      const hold = (s.holds || {})[d];
      return `<span class="lb-dest${hold || held ? " lb-bad" : ""}" data-dest="${esc(d)}">${esc(NAMES[d] || d)} ${done}✓`
        + (wait ? ` · ${wait} waiting` : "") + (held ? ` · ${held} held` : "")
        + (c.held_backfill ? ` · ${c.held_backfill} backfill` : "")
        + (hold ? ` · stopped: ${esc(hold)}` : "") + `</span>`;
    });
    const rows = (s.recent || []).map((q) => {
      const ticks = (s.dests || []).map((d) => {
        const x = (q.d || {})[d];
        if (!x) return `<td></td>`;
        const [sym, what] = TICK[x.state] || ["?", x.state];
        const why = x.why ? `: ${x.why}` : "";
        const fix = x.edited && d !== "lotw"
          ? ` <button class="chip chip-mini lb-fix" data-id="${esc(q.id)}" data-dest="${esc(d)}" title="Send the edited QSO to ${esc(NAMES[d] || d)}">Send correction</button>` : "";
        return `<td class="lb-t lb-${esc(x.state)}" title="${esc(NAMES[d] || d)}: ${esc(what + why)}${x.edited ? " (edited after upload)" : ""}">${esc(sym)}${x.edited ? " ✎" : ""}${fix}</td>`;
      }).join("");
      return `<tr data-id="${esc(q.id)}"><td>${esc(q.time)}</td><td>${esc(q.call)}</td><td>${esc(q.band)}</td><td>${esc(q.mode)}</td>${ticks}`
        + `<td>${q.near ? `<span class="lb-near" title="Another QSO with them on this band and mode within 2 minutes: check it">near miss</span>` : ""}</td></tr>`;
    }).join("");
    el.innerHTML = `<div class="lb-sync-bar stat">Logbook sync (${esc(s.intake || "")}): ${parts.join(" ")}</div>`
      + (rows ? `<details class="lb-recent"><summary class="stat">Latest QSOs</summary><table class="lb-q">
           <tr><th>UTC</th><th>Call</th><th>Band</th><th>Mode</th>${(s.dests || []).map((d) => `<th>${esc(NAMES[d] || d)}</th>`).join("")}<th></th></tr>
           ${rows}</table></details>` : "");
    for (const b of el.querySelectorAll(".lb-fix")) {
      b.onclick = () => {
        if (!confirm(`Send the edited QSO to ${NAMES[b.dataset.dest] || b.dataset.dest}?`)) return;
        send({ t: "logsync", do: "correct", id: Number(b.dataset.id), dest: b.dataset.dest });
      };
    }
  }

  function visible() { return !!body && body.offsetParent !== null; }

  // Load the frame when the tab is first shown (the logbook's dashboard is
  // heavy; most page opens never look at it).
  function watch() {
    if (seen || !body || typeof IntersectionObserver === "undefined") return;
    seen = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) render(); });
    seen.observe(body);
  }

  addons.register("logbook", {
    mount(b) { body = b; render(); },
    hello(s) { snap = s || {}; render(); },
    event(m) { if (m && m.logsync) { snap.logsync = m.logsync; renderSync(); } },
    off() { snap = {}; render(); },
    tab() { return { dot: target() ? "ok" : "off", title: target() || "Your logbook" }; },
    card() {
      const url = target();
      return { state: url ? "linked" : "off", lines: url ? [`<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>`] : [] };
    },
  });
})();
