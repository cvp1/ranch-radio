"use strict";

// MeshCore add-on (addons/meshcore.py): a MeshCore companion node on its own
// tab. Top: the node (name, model, firmware, radio, battery, location).
// Left: messages on the node's channels and direct messages, with a send box
// for the selected channel. Right: the nodes it has heard advertise, newest
// first, with how far away each says it is. MeshCore and Meshtastic are
// separate meshes; this tab never shows the Mesh tab's nodes.

(() => {
  let snap = {}, body = null, api = null, view = 0, unread = {};
  const TYPE = { chat: "Companion", repeater: "Repeater", room: "Room server", sensor: "Sensor" };

  const ago = (ts) => {
    if (!ts) return "never";
    const s = Math.max(0, Date.now() / 1000 - ts);
    return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago`
      : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
  };
  const km = (a, b) => {
    if (a?.lat == null || a?.lon == null || b?.lat == null || b?.lon == null) return null;
    const r = Math.PI / 180, dLat = (b.lat - a.lat) * r, dLon = (b.lon - a.lon) * r;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLon / 2) ** 2;
    return 12742 * Math.asin(Math.sqrt(h));
  };
  const me = () => snap.info?.me || {};
  const isDm = (v) => typeof v === "string";       // view: a channel index, or a contact key prefix

  function head() {
    const s = snap.status || {}, i = snap.info || {}, r = i.radio || {}, d = i.device || {}, m = me();
    const state = s.connected ? `<span class="mc-ok">Connected</span>`
      : `<span class="mc-off">${esc(s.error || "Not connected")}</span>`;
    const bat = snap.battery_mv ? `${(snap.battery_mv / 1000).toFixed(2)} V` : "";
    const radio = r.freq ? `${r.freq} MHz · ${r.bw} kHz · SF${r.sf} · CR${r.cr} · ${r.tx_power} dBm` : "";
    const loc = m.lat != null ? `${m.lat.toFixed(3)}, ${m.lon.toFixed(3)}${m.share_loc ? " (shared)" : " (private)"}` : "no location";
    return `<div class="mc-head">
        <span class="mc-name">${esc(m.name || "—")}</span>${state}
        <span class="stat">${esc([d.model, d.fw].filter(Boolean).join(" · "))}</span>
        <span class="spacer"></span>
        <button class="chip chip-mini" data-advert="0" title="Announce this node to the nodes in direct range (one packet on the air)">Advert</button>
        <button class="chip chip-mini" data-advert="1" title="Announce this node across the mesh: repeaters pass it on">Flood advert</button>
      </div>
      <div class="mc-sub stat">${esc([radio, bat, loc].filter(Boolean).join("  ·  "))}</div>`;
  }

  function tabs() {
    const ch = (snap.channels || []).map((c) =>
      `<button class="chip${view === c.index ? " on" : ""}" data-view="${c.index}">${esc(c.name)}${
        unread[c.index] ? ` <span class="mc-unread">${unread[c.index]}</span>` : ""}</button>`);
    const dms = new Set((snap.messages || []).filter((x) => x.dm).map((x) => x.mine ? x.to : x.from).filter(Boolean));
    const dm = [...dms].map((k) => {
      const c = (snap.contacts || []).find((c) => (c.key || "").startsWith(k));
      return `<button class="chip${view === k ? " on" : ""}" data-view="${esc(k)}">✉ ${esc(c?.name || k.slice(0, 8))}${
        unread[k] ? ` <span class="mc-unread">${unread[k]}</span>` : ""}</button>`;
    });
    return `<div class="seg mc-tabs">${[...ch, ...dm].join("")}</div>`;
  }

  function messages() {
    const list = (snap.messages || []).filter((x) => isDm(view)
      ? x.dm && (x.mine ? x.to : x.from) === view : !x.dm && x.ch === view);
    if (!list.length) return `<div class="mc-empty st-empty">No messages here yet.</div>`;
    return list.slice(-150).map((x) => {
      const meta = [new Date(x.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
        x.hops != null ? `${x.hops} hop${x.hops === 1 ? "" : "s"}` : x.mine ? "" : "direct",
        x.snr != null ? `SNR ${x.snr}` : "", x.mine ? x.ack + (x.why ? `: ${x.why}` : "") : ""].filter(Boolean).join(" · ");
      return `<div class="mc-msg${x.mine ? " mine" : ""}"><b>${esc(x.mine ? "You" : x.name || (x.from || "?").slice(0, 8))}</b>
        <span>${esc(x.text)}</span><small>${esc(meta)}</small></div>`;
    }).join("");
  }

  function contacts() {
    const list = snap.contacts || [];
    if (!list.length) return `<div class="mc-empty st-empty">No nodes heard yet. A node shows up here when it
      advertises; press Advert so nearby nodes learn this one.</div>`;
    return list.map((c) => {
      const d = km(me(), c), heard = (snap.heard || {})[c.key];
      return `<div class="mc-node" title="Message this node" data-key="${esc((c.key || "").slice(0, 12))}">
        <b>${esc(c.name || (c.key || "").slice(0, 8))}</b><span class="stat">${esc(TYPE[c.type] || c.type)}</span>
        <small>${esc([c.hops == null ? "flood" : c.hops === 0 ? "direct" : `${c.hops} hop${c.hops === 1 ? "" : "s"}`,
          d != null ? `${d < 10 ? d.toFixed(1) : Math.round(d)} km` : "", `advert ${ago(heard || c.last_advert)}`].filter(Boolean).join(" · "))}</small>
      </div>`;
    }).join("");
  }

  function render() {
    if (!body) return;
    const max = snap.text_max || 140;
    body.innerHTML = `${head()}
      <div class="mc-cols">
        <div class="mc-left">${tabs()}<div class="mc-list">${messages()}</div>
          <form class="wl-row mc-form"><input class="wl-url mc-text" maxlength="${max * 2}" autocomplete="off"
            placeholder="${isDm(view) ? "Direct message" : "Message to the channel"}"><span class="stat mc-count"></span>
            <button type="submit" class="chip on" ${snap.status?.connected ? "" : "disabled"}>Send</button></form>
        </div>
        <div class="mc-right"><div class="label">Heard (${(snap.contacts || []).length})</div>
          <div class="mc-nodes">${contacts()}</div></div>
      </div>`;
    const list = body.querySelector(".mc-list");
    list.scrollTop = list.scrollHeight;
    const input = body.querySelector(".mc-text"), count = body.querySelector(".mc-count");
    input.oninput = () => {
      const n = new TextEncoder().encode(input.value.trim()).length;
      count.textContent = n ? `${n}/${max}` : "";
      count.classList.toggle("mc-over", n > max);
    };
    body.querySelector(".mc-form").onsubmit = (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      api.send("send", isDm(view) ? { text, to: view } : { text, ch: view });
      input.value = "";
      count.textContent = "";
    };
    for (const b of body.querySelectorAll("[data-view]")) b.onclick = () => {
      const v = b.dataset.view;
      view = /^\d+$/.test(v) ? Number(v) : v;
      delete unread[view];
      render();
      api.changed();
    };
    for (const b of body.querySelectorAll("[data-advert]")) b.onclick = () => {
      const flood = b.dataset.advert === "1";
      if (flood && !confirm("Flood an advert? Every repeater in range passes it on.")) return;
      api.send("advert", { flood });
    };
    for (const n of body.querySelectorAll(".mc-node")) n.onclick = () => {
      view = n.dataset.key;
      render();
    };
  }

  addons.register("meshcore", {
    mount(el, a) { body = el; api = a; render(); },
    hello(s) {
      snap = { ...s };
      if (!(snap.channels || []).some((c) => c.index === view) && !isDm(view)) view = snap.channels?.[0]?.index ?? 0;
      render();
    },
    off() { snap = { status: { connected: false, error: "Switched off" } }; render(); },
    event(m) {
      if (m.status) snap.status = m.status;
      if (m.info) snap.info = m.info;
      if (m.channels) snap.channels = m.channels;
      if (m.contacts) snap.contacts = m.contacts;
      if ("battery_mv" in m) snap.battery_mv = m.battery_mv;
      if (m.heard) snap.heard = { ...(snap.heard || {}), [m.heard.key]: m.heard.t };
      if (m.msg) {
        (snap.messages ||= []).push(m.msg);
        const where = m.msg.dm ? m.msg.from : m.msg.ch;
        if (!m.msg.mine && where !== view) unread[where] = (unread[where] || 0) + 1;
      }
      if (m.update) {
        const i = (snap.messages || []).findIndex((x) => x.mine && x.t === m.update.t);
        if (i >= 0) snap.messages[i] = m.update;
      }
      const typing = body && document.activeElement === body.querySelector(".mc-text");
      if (typing && !m.msg && !m.status) return;     // don't wipe a half-typed message for a contact refresh
      const keep = typing ? body.querySelector(".mc-text").value : null;
      render();
      if (keep != null) { const i = body.querySelector(".mc-text"); i.value = keep; i.focus(); i.oninput(); }
    },
    tab() {
      const n = Object.values(unread).reduce((a, b) => a + b, 0);
      return { dot: snap.status?.connected ? "ok" : "off", off: !snap.status?.connected, badge: n || "" };
    },
    card() {
      const c = snap.contacts || [];
      return { state: snap.status?.connected ? "ok" : "off",
        lines: [esc(me().name || "MeshCore"), `${c.length} node${c.length === 1 ? "" : "s"} heard`, "Open →"] };
    },
  });
})();
