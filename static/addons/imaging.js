"use strict";

// The Imaging panel: SSTV pictures and weather-fax charts decoded from one
// radio's receive audio (addons/imaging.py; the decoders are sstv.py and
// wefax.py). Shows what each decoder is doing, the picture coming in as it
// arrives, and the saved pictures, newest first. Buttons: Start fax (a chart
// joined mid-way) and Stop (save what came in so far). Receive only: nothing
// here tunes or keys the radio.
//
// Pictures come from /imaging/img/<name> behind the station's token. Text
// from the server goes through esc(). Uses common.js (esc, toast, token).

(() => {
  let body = null, snap = {}, api = null;

  const img = (name, v) => `imaging/img/${encodeURIComponent(name)}?` +
    (v != null ? `v=${encodeURIComponent(v)}&` : "") + (token ? `token=${encodeURIComponent(token)}` : "");
  const when = (iso) => {
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };
  const KIND = { sstv: "SSTV", fax: "Fax" };

  function decoderLine(kind, d) {
    if (!d) return "";
    const live = (snap.live || {})[kind];
    let state = esc(d.state || "");
    if (d.state === "receiving") {
      state = kind === "sstv" ? `receiving ${esc(d.label || "")}, ${Number(d.lines) || 0} of ${Number(d.total) || "?"} lines`
        : `receiving, ${Number(d.lines) || 0} lines${d.aligned === false ? " (not lined up)" : ""}`;
    } else if (d.state === "phasing") state = "start tone heard, lining up";
    else if (d.state === "hunting") state = "waiting for a picture (VIS header)";
    else if (d.state === "listening") state = "waiting for a start tone";
    const btn = d.state === "receiving" ? `<button class="chip chip-mini im-stop" data-kind="${kind}">Stop</button>`
      : kind === "fax" && d.state === "listening" ? `<button class="chip chip-mini im-faxstart">Start fax</button>` : "";
    return `<div class="im-dec"><b>${KIND[kind]}</b> <span class="im-state">${state}</span> ${btn}</div>` +
      (live && live.name ? `<img class="im-live im-${kind}" alt="${KIND[kind]} coming in" src="${img(live.name, live.v)}">` : "");
  }

  function render() {
    if (!body) return;
    const s = snap || {};
    const head = s.error ? `<div class="im-warn">${esc(s.error)}</div>`
      : s.listening ? `<div class="stat">Listening to ${esc(s.radio || "the radio")}. Tune it yourself: SSTV 14.230 USB, fax 1.9 kHz below the carrier on USB.</div>`
        : `<div class="stat">Not listening yet.</div>`;
    const decs = Object.entries(s.decoders || {}).filter(([k]) => KIND[k]).map(([k, d]) => decoderLine(k, d)).join("");
    const pics = (s.images || []).map((p) =>
      `<a class="im-pic" href="${img(p.name)}" target="_blank" rel="noopener">
         <img loading="lazy" alt="${esc(p.mode)}" src="${img(p.name)}">
         <span>${esc(when(p.at))} · ${esc(p.mode)}${p.khz ? ` · ${Number(p.khz)} kHz` : ""}${p.status !== "complete" ? ` · ${esc(p.status)}` : ""}</span>
       </a>`).join("");
    body.innerHTML = `<div class="imaging">${head}${decs}
        <div class="im-gallery">${pics || `<div class="stat">No pictures yet.</div>`}</div></div>`;
    body.querySelectorAll(".im-stop").forEach((b) => { b.onclick = () => api.send("stop", { kind: b.dataset.kind }); });
    const fs = body.querySelector(".im-faxstart");
    if (fs) fs.onclick = () => api.send("fax_start");
  }

  // Every event carries the decoders' states, so the lines above stay true.
  function event(m) {
    if (m.decoders) snap.decoders = m.decoders;
    if (m.event === "status") { snap.error = m.error; snap.listening = m.listening; }
    else if (m.event === "start") toast(`Imaging: ${m.label} starting`);
    else if (m.event === "progress") snap.live = Object.assign({}, snap.live, { [m.kind]: m.live });
    else if (m.event === "saved") {
      toast(`Imaging: ${m.label} saved`);
      snap.images = [m.image].concat((snap.images || []).filter((p) => p.name !== m.image.name)).slice(0, 60);
      if (snap.live) delete snap.live[m.kind];
    } else if (m.event === "dropped" && snap.live) delete snap.live[m.kind];
    render();
  }

  addons.register("imaging", {
    mount(b, a) { body = b; api = a; render(); },
    hello(s) { snap = s || {}; render(); },
    event,
    off() { snap = {}; render(); },
    tab() {
      const recv = Object.values(snap.decoders || {}).some((d) => d.state === "receiving");
      return { dot: snap.error ? "off" : "ok", badge: recv ? "rx" : "", title: "Pictures off the air: SSTV and weather fax" };
    },
    card() {
      const n = (snap.images || []).length;
      return { state: snap.error ? "off" : "on", lines: [snap.error ? snap.error : `${n} picture${n === 1 ? "" : "s"}`] };
    },
  });
})();
