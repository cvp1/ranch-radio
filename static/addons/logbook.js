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
    if (frame && visible() && loadedSrc !== url) { frame.src = url; loadedSrc = url; }
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
    event() {},
    off() { snap = {}; render(); },
    tab() { return { dot: target() ? "ok" : "off", title: target() || "Your logbook" }; },
    card() {
      const url = target();
      return { state: url ? "linked" : "off", lines: url ? [`<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>`] : [] };
    },
  });
})();
