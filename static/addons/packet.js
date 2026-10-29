"use strict";

// Packet add-on panel (addons/packet.py): the packet hub's own page (APRS map
// and heard list, the transmit switch, the Winlink packet link) in a frame,
// proxied by this radio server under /packet/, as the Linux station shows it.

(() => {
  let snap = {}, body = null;

  function render() {
    if (!body) return;
    if (!snap.up) {
      body.innerHTML = `<div class="st-empty">The packet hub isn't connected to its radio${snap.hub_name ? ` (${esc(snap.hub_name)})` : ""}.
        It may still be starting, or the cable is unplugged.</div>`;
      return;
    }
    if (!body.querySelector("iframe")) {
      body.innerHTML = "";
      const f = document.createElement("iframe");
      f.src = `packet/?t=${Date.now()}`;
      f.title = snap.hub_title || "Packet hub";
      f.className = "packet-frame";
      f.style.cssText = "width:100%;height:78vh;border:0;";
      body.append(f);
    }
  }

  addons.register("packet", {
    mount(b) { body = b; render(); },
    hello(s) { snap = s || {}; render(); },
    event(m) { if (m.state) { snap = m.state; render(); } },
    off() { snap = {}; if (body) body.innerHTML = ""; },
    tab() {
      const title = `${snap.hub_title || "Packet"}: APRS and the Winlink packet link`;
      if (!snap.up) return { dot: "off", off: true, title };
      return { dot: snap.tx ? "tx" : "ok", title };
    },
    card() {
      return { state: snap.up ? (snap.tx ? "transmit on" : "receive only") : "not connected",
               lines: snap.last_rx_age != null ? [`last packet ${snap.last_rx_age} s ago`] : [] };
    },
  });
})();
