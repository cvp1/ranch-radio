"use strict";

// Mesh panel: a Meshtastic node's channels, direct conversations (one per
// node), replies and emoji reactions, the node list (a node's card: message
// it, traceroute, ask for its position / telemetry / user info / neighbours, favorite,
// ignore, remove), the map (meshmap.js, with NeighborInfo links; with nothing
// selected, this node's own: who hears it and who it hears directly; each
// node's mark says its role, hops and reach; beside it the same by name, as
// on the Map tab; ⤢ gives the map the room), who hears
// this node ("heard by N": the server's hears_us) and settings. Everything goes over the control WebSocket ({"t": "mesh"} in,
// mesh_send / mesh_config / mesh_favorite / mesh_trace / mesh_request /
// mesh_node / mesh_reboot / mesh_auto out); the server keeps the history, so a page
// opened later sees it too.
// Uses app.js's $, send, toast and on.
//
// More than one node (mesh-radios, 2026-11-21): the snapshot's "radios" lists
// them ([{id, label, connected, error}], the first is id ""), and a switcher
// beside "Mesh" picks which one this view shows. Every command goes out with
// that node's id (tx()); picking one asks the station for its snapshot
// (mesh_select) and redraws from it; an event from another node only updates
// its dot and unread count in the switcher.

const mesh = (() => {
  const HEARD_RECENT_S = 24 * 3600;
  const ACK = { sending: ["…", "Sending"], sent: ["✓?", "Sent, waiting for an ACK"],
    relayed: ["✓", "A neighbour rebroadcast it"], delivered: ["✓✓", "Delivered"],
    failed: ["✗", "Not delivered"], unknown: ["?", "No ACK came back; it may not have gone out"],
    queued: ["⋯", "Waiting for the part before it"] };
  let info = null, nodes = [], messages = [], connected = false, linkError = null, textMax = 200;
  let names = {};           // node id -> {short, long, hw}: every name the station has seen (mesh.py remember_names)
  // Nodes the radio no longer lists, as the station last had them (mesh.py
  // keep_nodes): the radio's own list is short (80 on many boards), a phone app
  // keeps every node it has seen, and so does this list. id -> node, "kept": true.
  let kept = {};
  let partsMax = 9, partBytes = 194; // a long text goes as up to partsMax parts of partBytes (mesh.py split_text)
  let switchedOff = false;  // the Mesh add-on is off: the bridge is stopped and the node's port free
  let view = null;          // channel index, or "dm"
  let dmTo = null;          // node id for a direct message
  let recentOnly = false;    // the list shows every node the radio knows unless "Recent"
  let selected = null;      // node id whose card is open
  let replyTo = null;       // the message being replied to
  const traces = {};        // node id -> the last traceroute to it
  let requests = {};        // node id -> {kind: {state, t, error}}: position / telemetry / user info asks
  let neighbors = {};       // node id -> {t, neighbors: [{id, snr}]}: who each node hears directly
  let hears = [];           // [{id, t, snr, how}]: who hears this node (mesh.py hears_us)
  let hearsOpen = false;
  const HOW = { neighbor: ["says it hears us", "Its NeighborInfo lists this node, with the SNR it hears us at"],
    trace: ["first hop of a traceroute", "A traceroute's first hop: it heard us at this SNR"],
    dm: ["got our DM", "It ACKed a direct message of ours: it got it, maybe through relays"] };
  const REQUESTS = [["position", "Position", "Ask it for its position"],
    ["telemetry", "Telemetry", "Ask it for its battery, voltage and channel use"],
    ["env", "Environment", "Ask it for its sensor readings (temperature, humidity, pressure)"],
    ["nodeinfo", "User info", "Swap user info with it (its name and key; it gets ours)"],
    ["neighbors", "Neighbours", "Ask it which nodes it hears directly, with SNR (answers only with its NeighborInfo module on; once every 3 minutes)"]];
  const map = meshMap.create($("#mesh-map"), { onSelect: (id) => select(id, true), rings: true, onExpand: () => toggleBig(),
    labels: true, popup: (id) => popupHtml(id), onPopup: (id, act) => popupAct(id, act) });
  const LIVE_S = 15 * 60;
  // Per conversation ("c0", "d!id"): archived, muted, pinned, read_t, kept by
  // the station (mesh_convs.py), so unread survives a reload and every browser agrees.
  let convState = { since: Date.now() / 1000, convs: {} };
  let railFind = "";        // the rail's search box
  let archivedOpen = store.get("mesh.archived", false);
  let cfg = null;           // {sections, values}: the bridge's settings schema and the node's values
  let cfgOpen = false, cfgSec = null;
  let healthOpen = false;
  let health = null;        // mesh_stats.py summary(): traffic, channel load, findings
  let packets = [];         // the last packets heard ({"rx"} events), for the Health view's log
  const edits = {};         // section -> {field: value} not yet saved
  let cur = "";             // the node this view shows ("" the first)
  let radios = [];          // [{id, label, connected, error}] when the station has more than one
  const unread = {};        // node id -> messages from others since it was last shown
  const tx = (o) => send(cur ? { ...o, node: cur } : o);    // every command names the node it is for
  const radioBar = document.createElement("span");
  radioBar.className = "mesh-radios";
  radioBar.id = "mesh-radios";
  radioBar.setAttribute("role", "tablist");
  radioBar.setAttribute("aria-label", "Mesh radios");
  $("#mesh-me")?.before(radioBar);
  function renderRadios() {
    radioBar.hidden = radios.length < 2;
    if (radioBar.hidden) { radioBar.innerHTML = ""; return; }
    radioBar.innerHTML = radios.map((r) => {
      const n = unread[r.id] || 0, here = r.id === cur;
      const t = `${r.label}: ${r.connected ? "connected" : (r.error || "not connected")}${n ? `, ${n} new` : ""}`;
      return `<button type="button" class="chip chip-mini mesh-radio${here ? " on" : ""}" role="tab" aria-selected="${here}" ` +
        `data-node="${esc(r.id)}" title="${esc(t)}"><i class="mesh-radio-dot${r.connected ? " ok" : ""}"></i>${esc(r.label)}` +
        `${n && !here ? ` <b class="mesh-radio-n">${n}</b>` : ""}</button>`;
    }).join("");
  }
  radioBar.addEventListener("click", (e) => {
    const b = e.target.closest?.(".mesh-radio");
    if (!b || b.dataset.node === cur) return;
    cur = b.dataset.node;
    unread[cur] = 0;
    store.set("mesh.radio", cur);
    tx({ t: "mesh_select" });
    renderRadios();
  });
  function radioStatus(node, st) {
    const r = radios.find((x) => x.id === (node || ""));
    if (r && st) { r.connected = !!st.connected; r.error = st.error || null; renderRadios(); }
  }

  const byId = (id) => nodes.find((n) => n.id === id) || kept[id];
  // The radio's list and the kept nodes, as one: the list, its count, "heard today".
  const everyNode = () => {
    const listed = new Set(nodes.map((n) => n.id));
    return nodes.concat(Object.values(kept).filter((n) => !listed.has(n.id)));
  };
  // A node that has left the radio's list keeps the name it last had (names).
  const short = (id) => byId(id)?.shortName || names[id]?.short || id || "?";
  const long = (id) => byId(id)?.longName || names[id]?.long || id || "?";
  const hhmm = (t) => new Date(t * 1000).toTimeString().slice(0, 5);
  const myId = () => info?.me?.id;
  const bytes = (s) => new TextEncoder().encode(s).length;
  const viewOf = (m) => (m.dm ? "dm" : m.ch);
  const peerOf = (m) => (m.mine ? m.to : m.from);
  const isReaction = (m) => !!(m.emoji && m.reply_id);
  const byMsgId = (id) => messages.find((m) => m.id === id && !isReaction(m));

  // Great-circle distance (km) and initial bearing from this node.
  function where(n) {
    const me = byId(myId());
    const ok = (x) => x && x.lat != null && (x.lat || x.lon);
    if (!ok(me) || !ok(n) || me === n) return null;
    const r = Math.PI / 180, [la1, la2] = [me.lat * r, n.lat * r], dl = (n.lon - me.lon) * r;
    const a = Math.sin((la2 - la1) / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin(dl / 2) ** 2;
    const km = 12742 * Math.asin(Math.sqrt(a));
    const deg = (Math.atan2(Math.sin(dl) * Math.cos(la2),
      Math.cos(la1) * Math.sin(la2) - Math.sin(la1) * Math.cos(la2) * Math.cos(dl)) / r + 360) % 360;
    return { km, deg };
  }
  const kmText = (w) => (w ? `${w.km < 10 ? w.km.toFixed(1) : Math.round(w.km)} km` : "");
  const compass = (deg) => ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8];
  function dur(s) {
    if (s == null) return "";
    if (s < 3600) return `${Math.round(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
    return `${Math.floor(s / 86400)}d ${Math.round((s % 86400) / 3600)}h`;
  }

  function ago(t) {
    if (!t) return "";
    const s = Date.now() / 1000 - t;
    if (s < 90) return "now";
    if (s < 3600) return `${Math.round(s / 60)}m`;
    if (s < 86400) return `${Math.round(s / 3600)}h`;
    return `${Math.round(s / 86400)}d`;
  }

  // ---------- header and tabs

  function renderHead() {
    const me = info?.me;
    $("#mesh-me").textContent = me ? `${me.long || ""} (${me.short || ""})` : "";
    const st = $("#mesh-state");
    st.textContent = switchedOff ? "Switched off" : connected ? "Connected" : "Not connected";
    // Why, e.g. the meshtastic CLI holding the port ("Could not exclusively lock port").
    st.className = "mesh-state" + (connected ? " on" : " warn");
    const via = { serial: "USB", ble: "Bluetooth", tcp: "Wi-Fi" }[info?.link];
    st.title = connected ? `${me?.hw || ""} · firmware ${me?.fw || "?"} · ${info?.preset || ""}${via ? ` · over ${via}` : ""}` : linkError || "";
    const keys = (info?.channels || []).filter((c) => c.key_ok !== null && c.key_ok !== undefined);
    $("#mesh-key").innerHTML = keys.map((c) => c.key_ok
      ? `<span class="stat mesh-key-ok" title="The node's key for ${esc(c.name)} is the expected one">${esc(c.name)} key ✓</span>`
      : `<span class="stat mesh-key-bad" title="The node's key for ${esc(c.name)} is NOT the expected one">${esc(c.name)} key ✗</span>`).join("")
      + (info?.missing || []).map((n) => `<span class="stat mesh-key-bad" title="Not configured on the node">${esc(n)} missing</span>`).join("");
    const meNode = nodes.find((n) => n.me), stats = meNode?.stats || {};
    const util = stats.util ?? meNode?.util, air = stats.air_tx ?? meNode?.air_tx;
    const parts = [util != null ? [`ch ${util.toFixed(1)}%`, "Channel utilization: how busy the air is, as this node hears it"] : null,
      air != null ? [`tx ${air.toFixed(1)}%`, "Air time this node transmitted in the last hour"] : null,
      meNode?.battery != null ? [meNode.battery > 100 ? "⚡ powered" : `🔋 ${meNode.battery}%`, "This node's battery"] : null,
      stats.online != null ? [`${stats.online} online`, `Nodes heard in the last 2 hours (of ${stats.total ?? "?"} known)`] : null,
      stats.rx != null ? [`rx ${stats.rx} / tx ${stats.tx ?? "?"}`, `Packets received / sent since boot${stats.rx_bad ? `; ${stats.rx_bad} bad` : ""}${stats.dupes ? `; ${stats.dupes} duplicates` : ""}${stats.relayed ? `; ${stats.relayed} relayed` : ""}`] : null,
      stats.noise != null ? [`noise ${stats.noise} dBm`, "Noise floor"] : null,
      (stats.uptime ?? meNode?.uptime) != null ? [`up ${dur(stats.uptime ?? meNode.uptime)}`, "Since the node last booted"] : null].filter(Boolean);
    $("#mesh-stats").innerHTML = connected ? parts.map(([t, why]) => `<span class="stat" title="${esc(why)}">${esc(t)}</span>`).join("") : "";
    $("#mesh").classList.toggle("mesh-off", !connected);
    const pw = $("#mesh-power");
    pw.textContent = switchedOff ? "Switch on" : "Switch off";
    pw.title = switchedOff ? "Start the link to the node" : "Close the link to the node and free its port";
  }

  // The add-on registry's on/off switch (addons/mesh.py). Off stops the bridge,
  // so the meshtastic CLI or the phone app can have the node's port.
  $("#mesh-power").onclick = () => {
    const want = switchedOff;
    if (!want && !confirm("Switch Mesh off? The link to the node closes and its port is free until you switch it on again.")) return;
    send({ t: "addon", addon: "mesh", do: "power", on: want });
  };

  // Who hears us: direct evidence first (its NeighborInfo, a traceroute's first
  // hop), then the nodes that ACKed a DM (reached, maybe through relays).
  function renderHears() {
    const direct = new Set(hears.filter((h) => h.how !== "dm").map((h) => h.id));
    const btn = $("#mesh-hears-btn");
    btn.hidden = !connected && !hears.length;
    btn.textContent = `heard by ${direct.size}`;
    btn.title = `Nodes that hear this one directly (last 7 days)${hears.length > direct.size ? "; the list also has nodes that got our DMs" : ""}`;
    on(btn, hearsOpen);
    const el = $("#mesh-hears");
    el.hidden = !hearsOpen;
    if (!hearsOpen) return;
    // One row per node: every way we know, the SNR of its newest direct evidence.
    const group = (list) => {
      const by = new Map();
      for (const h of list) {
        const g = by.get(h.id) || { id: h.id, t: 0, snr: null, snrT: 0, how: [] };
        g.how.push(h.how);
        g.t = Math.max(g.t, h.t);
        if (h.snr != null && h.t > g.snrT) { g.snr = h.snr; g.snrT = h.t; }
        by.set(h.id, g);
      }
      return [...by.values()].sort((a, b) => b.t - a.t);
    };
    const rows = (list) => group(list).map((g) => {
      const how = g.how.map((k) => (HOW[k] || [k])[0]).join(" · ");
      const why = g.how.map((k) => (HOW[k] || [k, ""])[1]).filter(Boolean).join("; ");
      return `<button class="mesh-hear" data-id="${esc(g.id)}" title="${esc(`${long(g.id)} ${g.id}: ${why}`)}">` +
        `<b>${esc(short(g.id))}</b><span class="mesh-long">${esc(long(g.id))}</span>` +
        `<span class="wl-dim">${esc(how)}</span><span class="wl-dim">${g.snr != null ? `${g.snr} dB` : ""}</span>` +
        `<span class="wl-dim">${esc(ago(g.t))}</span></button>`;
    }).join("");
    const d = hears.filter((h) => h.how !== "dm"), r = hears.filter((h) => h.how === "dm" && !direct.has(h.id));
    el.innerHTML = `<div class="wl-dim mesh-hears-note">From NeighborInfo, traceroutes and DM ACKs over the last 7 days; ` +
      `nothing is sent to find out. A node with NeighborInfo off only shows up here after a traceroute.</div>` +
      (d.length ? `<div class="mesh-hears-h">Hear us directly</div>${rows(d)}` : `<div class="wl-empty">No node has said it hears us yet. Traceroute a node to find out.</div>`) +
      (r.length ? `<div class="mesh-hears-h">Got our DMs (maybe through relays)</div>${rows(r)}` : "");
    el.onclick = (e) => {
      const id = e.target.closest("button.mesh-hear")?.dataset.id;
      if (id && byId(id)) select(id);
    };
  }
  $("#mesh-hears-btn").onclick = () => { hearsOpen = !hearsOpen; renderHears(); };

  // ---------- conversations (the rail, chat.js)

  const convOf = (v, peer) => (v === "dm" ? `d${peer}` : `c${v}`);
  const convOfMsg = (m) => (m.dm ? convOf("dm", peerOf(m)) : convOf(m.ch));
  const cs = (conv) => convState.convs[conv] || {};
  const readT = (conv) => cs(conv).read_t ?? convState.since;
  const incoming = (conv) => messages.filter((m) => !m.mine && !isReaction(m) && convOfMsg(m) === conv);
  const unreadOf = (conv) => { const rt = readT(conv); return incoming(conv).filter((m) => m.t > rt).length; };
  const inFlight = (m) => m.mine && (["sending", "sent", "queued"].includes(m.ack) || m.retry_at != null);
  function markRead(conv) {
    const last = incoming(conv).at(-1);
    if (!last || last.t <= readT(conv)) return;
    convState.convs[conv] = { ...cs(conv), read_t: last.t };      // at once here; the station's echo follows
    tx({ t: "mesh_conv", action: "read", conv, msg_t: last.t });
  }
  const openConv = () => (view === "dm" ? (dmTo ? convOf("dm", dmTo) : null) : convOf(view));
  let newMark = null;       // {conv, n}: how many were unread when it was opened, for the "New" line
  const onlineNow = () => everyNode().filter((n) => !n.me && n.last_heard && Date.now() / 1000 - n.last_heard < LIVE_S).length;

  function dmPeers() {
    const last = new Map();
    for (const m of messages) {
      if (!m.dm || isReaction(m)) continue;
      const p = peerOf(m);
      if (p && p !== myId()) last.set(p, m);
    }
    if (dmTo && !last.has(dmTo)) last.set(dmTo, null);
    return [...last].sort((a, b) => (b[1]?.t ?? Infinity) - (a[1]?.t ?? Infinity));
  }
  const lastLine = (m) => (m ? { who: m.mine ? "You" : m.dm ? "" : short(m.from), text: m.text, t: m.t } : null);

  function convTitle(conv) {
    if (conv[0] === "d") return long(conv.slice(1));
    const c = (info?.channels || []).find((x) => convOf(x.index) === conv);
    return c?.name || `Channel ${conv.slice(1)}`;
  }

  // A conversation's ⋯ menu (the rail's rows and the chat head).
  function convMenu(conv) {
    const st = cs(conv), dm = conv[0] === "d";
    const n = messages.filter((m) => convOfMsg(m) === conv && !isReaction(m)).length;
    return [
      ...(incoming(conv).length ? [unreadOf(conv) ? ["read", "Mark read"] : ["unread", "Mark unread"]] : []),
      [st.pinned ? "unpin" : "pin", st.pinned ? "Unpin" : "Pin to top"],
      [st.muted ? "unmute" : "mute", st.muted ? "Unmute" : "Mute (no pop-ups; stays archived)"],
      [st.archived ? "unarchive" : "archive", st.archived ? "Move out of Archived" : "Archive"],
      ...(n ? [[dm ? "delete" : "clear", dm ? "Delete conversation…" : "Clear history…", "danger"]] : []),
      [null, "Delete and clear remove messages from this station only: the mesh cannot unsend, and other nodes keep theirs. Undo is offered."],
    ];
  }

  function onConvMenu(conv, act) {
    if (act === "read") return markRead(conv);
    if (act === "unread") {
      const last = incoming(conv).at(-1);
      if (last) tx({ t: "mesh_conv", action: "unread", conv, msg_t: last.t });
      return;
    }
    if (act === "clear" || act === "delete") {
      const n = messages.filter((m) => convOfMsg(m) === conv && !isReaction(m)).length;
      const what = act === "delete" ? `your conversation with ${convTitle(conv)}` : `the history of ${convTitle(conv)}`;
      if (!confirm(`Delete ${what} (${n} message${n === 1 ? "" : "s"}) from this station?\n\nThe mesh cannot unsend: everyone else keeps their copies. You can undo this for a few seconds.`)) return;
      tx({ t: "mesh_conv", action: "delete", conv });
      return;
    }
    tx({ t: "mesh_conv", action: act, conv });
  }

  function renderTabs() {
    const chans = info?.channels || [];
    // Before the node has sent its channels (a restart, switching on), keep the
    // view: falling back to Direct then would stick once the channels arrive.
    if (view === null || (info && view !== "dm" && !chans.some((c) => c.index === view))) view = chans.length ? chans[0].index : "dm";
    const lastIn = (ch) => [...messages].reverse().find((m) => !m.dm && m.ch === ch && !isReaction(m));
    const q = railFind.trim().toLowerCase();
    const hit = (conv, title) => !q || title.toLowerCase().includes(q) || conv.slice(1).toLowerCase().includes(q)
      || messages.some((m) => convOfMsg(m) === conv && String(m.text || "").toLowerCase().includes(q));
    const flags = (conv) => { const st = cs(conv); return { pinned: !!st.pinned, muted: !!st.muted, archived: !!st.archived, menu: convMenu(conv) }; };
    const pinFirst = (a, b) => (b.pinned - a.pinned);
    const chanRows = chans.map((c) => ({ id: convOf(c.index), title: c.name || `Channel ${c.index}`, glyph: "#", seed: `#${c.name}`,
      last: lastLine(lastIn(c.index)), unread: unreadOf(convOf(c.index)), on: view === c.index,
      hint: `Channel ${c.index}: everyone on ${c.name || "it"} hears what you send here`, ...flags(convOf(c.index)) }))
      .filter((r) => hit(r.id, r.title)).sort(pinFirst);
    const dmRows = dmPeers().map(([p, m]) => ({ id: convOf("dm", p), title: long(p), seed: p, glyph: null,
      last: lastLine(m), unread: unreadOf(convOf("dm", p)), on: view === "dm" && dmTo === p, empty: "New conversation",
      hint: `${short(p)} ${p}`, ...flags(convOf("dm", p)) }))
      .filter((r) => hit(r.id, r.title)).sort(pinFirst);
    const live = (rows) => rows.filter((r) => !r.archived), arch = [...chanRows, ...dmRows].filter((r) => r.archived);
    const sections = [["Channels", live(chanRows), q ? "No channel matches" : "The node has sent no channels yet"],
      ["Direct", live(dmRows), q ? "No conversation matches" : "Tap a sender's name, or a node on the right, to message it directly"]];
    if (arch.length) {
      const n = arch.reduce((a, r) => a + r.unread, 0);
      sections.push([`Archived ${arch.length}${n ? ` · ${n} unread` : ""}`, arch, "",
        { collapsed: !archivedOpen && !q, onToggle: () => { archivedOpen = !archivedOpen; store.set("mesh.archived", archivedOpen); renderTabs(); } }]);
    }
    chat.rail($("#mesh-tabs"), sections, pick, {
      find: { placeholder: "Find a conversation or message", onInput: (v) => { railFind = v; renderTabs(); } },
      onMenu: onConvMenu });
  }

  function pick(conv) {
    if (conv[0] === "c") {
      const v = Number(conv.slice(1));
      newMark = unreadOf(conv) ? { conv, n: unreadOf(conv) } : null;
      view = v; markRead(conv);
      if (replyTo && viewOf(replyTo) !== view) replyTo = null;
      renderTabs(); renderMessages(); renderCompose();
      $("#mesh-text").focus();
    } else directTo(conv.slice(1));
  }

  const headMenu = () => `<button class="chip chip-mini" data-head="menu" title="Archive, mute, pin, delete…" aria-label="Conversation actions">⋯</button>`;
  function wireHeadMenu(el, conv) {
    const b = el.querySelector("[data-head=menu]");
    b.onclick = (e) => { e.stopPropagation(); chat.menu(b, convMenu(conv), (act) => onConvMenu(conv, act)); };
  }

  function renderChatHead() {
    const el = $("#mesh-chat-head");
    if (view === "dm" && dmTo) {
      const n = byId(dmTo);
      const bits = [short(dmTo), n ? hopsText(n) : "", n?.snr != null ? `SNR ${n.snr}` : "", n?.last_heard ? `heard ${when(n.last_heard)}` : "not heard yet"].filter(Boolean);
      el.innerHTML = chat.avatar(long(dmTo), dmTo) + `<span class="chat-head-text"><b>${esc(long(dmTo))}</b><span>${esc(bits.join(" · "))}</span></span>` +
        `<button class="chip chip-mini" data-head="node" title="Its card: position, telemetry, traceroute">Node</button>` + headMenu();
      el.querySelector("[data-head=node]").onclick = () => select(dmTo);
      wireHeadMenu(el, convOf("dm", dmTo));
    } else if (view !== "dm") {
      const c = (info?.channels || []).find((x) => x.index === view);
      el.innerHTML = chat.avatar(c?.name || "#", `#${c?.name}`, "#") + `<span class="chat-head-text"><b>${esc(c?.name || `Channel ${view}`)}</b>` +
        `<span>${esc(`Channel ${view}${view === 0 ? " (primary)" : ""} · ${onlineNow()} heard in the last 15 min`)}</span></span>` + headMenu();
      wireHeadMenu(el, convOf(view));
    } else {
      el.innerHTML = `<span class="chat-head-text"><b>Direct messages</b><span>Pick a conversation, a sender, or a node</span></span>`;
    }
  }

  // ---------- messages

  // Reactions to message id, folded by emoji (who reacted, "me" for us).
  function reactionsTo(id) {
    const by = new Map();
    for (const r of messages) {
      if (!isReaction(r) || r.reply_id !== id) continue;
      if (!by.has(r.text)) by.set(r.text, []);
      by.get(r.text).push(r.mine ? "me" : short(r.from));
    }
    return [...by].map(([emoji, who]) => ({ emoji, who }));
  }

  function againIn(at) {
    const s = Math.max(0, Math.round(at - Date.now() / 1000));
    return s ? `again in ${s < 90 ? `${s} s` : `${Math.round(s / 60)} min`}` : "sending again…";
  }
  setInterval(() => {
    for (const el of document.querySelectorAll("#mesh-list .mesh-retry-in")) el.textContent = againIn(Number(el.dataset.at));
  }, 1000);

  const keyOf = (m) => (m.mine ? `t${m.t}` : `i${m.id}`);

  // One message as a chat.js item.
  function item(m) {
    const [glyph, why] = m.mine ? (ACK[m.ack] || ["", ""]) : ["", ""];
    const sub = m.mine ? "" : [m.hops == null ? "" : m.hops === 0 ? "direct" : `${m.hops} hop${m.hops === 1 ? "" : "s"}`,
      m.snr != null ? `SNR ${m.snr}` : ""].filter(Boolean).join(" · ");
    const orig = m.reply_id ? byMsgId(m.reply_id) : null;
    const canAct = connected && m.id != null && m.ack !== "failed";
    const extra = !m.mine ? ""
      : m.retry_at ? `<span class="mesh-retry-in wl-dim" data-at="${m.retry_at}" title="The mesh did not take it; the station sends it again by itself">${esc(againIn(m.retry_at))}</span>` +
        `<button class="chip chip-mini mesh-retry" data-act="cancel" title="Don't send it again">Cancel</button>`
      : m.ack === "queued" ? `<button class="chip chip-mini mesh-retry" data-act="cancel" title="Don't send this part or the ones after it">Cancel</button>`
      : (m.ack === "failed" || m.ack === "unknown") && connected ? `<button class="chip chip-mini mesh-retry" data-act="retry" title="Send it again">Retry</button>` : "";
    const peer = peerOf(m);
    return { m, key: keyOf(m), mine: !!m.mine, t: m.t, text: m.text, sub, extra,
      who: m.mine ? "me" : long(m.from), tag: m.mine ? "" : short(m.from), whoTitle: m.mine ? "" : `${short(m.from)} · ${m.from || ""} · message it directly`,
      seed: m.mine ? myId() : m.from, peer: !m.mine && peer && peer !== myId() && !(m.dm && dmTo === peer) ? peer : null,
      quote: !m.reply_id ? null : orig ? { key: keyOf(orig), who: orig.mine ? "You" : long(orig.from), text: orig.text } : { missing: true },
      reacts: m.id != null ? reactionsTo(m.id) : [],
      ack: m.mine ? { glyph, cls: m.ack, why: why + (m.why ? `: ${m.why}` : "") } : null,
      canReply: canAct, canReact: canAct, canCopy: !!m.text, canDelete: !inFlight(m),
      deleteHint: "Delete from this station (the mesh cannot unsend it)" };
  }

  const handlers = {
    onWho: (it) => directTo(it.peer),
    onReply: (it) => { replyTo = it.m; renderCompose(); $("#mesh-text").focus(); },
    onReact: (it, emoji) => {
      const m = it.m;
      tx({ t: "mesh_send", text: emoji, ch: m.dm ? 0 : m.ch, to: m.dm ? peerOf(m) : null, reply_to: m.id, emoji: true });
    },
    onAct: (it, act) => {
      const m = it.m;
      if (act === "retry") tx({ t: "mesh_send", text: m.text, ch: m.ch, to: m.dm ? m.to : null, reply_to: m.reply_id || null });
      else if (act === "cancel") tx({ t: "mesh_cancel", msg_t: m.t });
      else if (act === "delete") tx({ t: "mesh_conv", action: "delete", ts: [m.t] });
    },
  };

  function renderMessages() {
    renderChatHead();
    const list = $("#mesh-list");
    const conv = view === "dm" ? convOf("dm", dmTo) : convOf(view);
    if (view === "dm" && !dmTo) {
      chat.render(list, [], { conv, empty: `<div class="wl-empty chat-empty">Pick a conversation on the left, a sender's name, or a node on the right to message it</div>` });
      return;
    }
    const shown = messages.filter((m) => viewOf(m) === view && !isReaction(m) && (view !== "dm" || peerOf(m) === dmTo));
    const n = newMark?.conv === conv ? newMark.n : 0;
    chat.render(list, shown.map(item), { conv, unread: n, ...handlers,
      empty: `<div class="wl-empty chat-empty">No messages${view === "dm" ? ` with ${esc(long(dmTo))} yet. Say hello 👋` : " here yet"}</div>` });
  }

  function addMessage(m) {
    messages.push(m);
    if (messages.length > 300) messages.shift();
    const v = viewOf(m);
    const here = v === view && (v !== "dm" || (dmTo && peerOf(m) === dmTo));
    if (isReaction(m)) {
      if (byMsgId(m.reply_id) && viewOf(byMsgId(m.reply_id)) === view) renderMessages();
      if (!m.mine && byMsgId(m.reply_id)?.mine) toast(`Mesh · ${short(m.from)} reacted ${m.text}`, "info");
      return;
    }
    if (here) { renderMessages(); if (!m.mine) markRead(convOfMsg(m)); }
    renderTabs();
    if (!m.mine && m.dm && !here && !cs(convOfMsg(m)).muted) toast(`Mesh · ${short(m.from)}: ${m.text}`, "info");
  }

  function updateMessage(m) {
    const i = messages.findIndex((x) => x.mine && x.t === m.t);
    if (i < 0) return;
    messages[i] = m;
    if (isReaction(m)) { renderMessages(); return; }
    if (viewOf(m) === view) renderMessages();
  }

  // ---------- compose

  function directTo(id) {
    if (dmTo !== id) replyTo = null;
    const conv = convOf("dm", id);
    newMark = unreadOf(conv) ? { conv, n: unreadOf(conv) } : null;
    dmTo = id;
    view = "dm";
    markRead(conv);
    renderTabs();
    renderMessages();
    renderCompose();
    if (bigMap) toggleBig(false);       // a conversation was asked for: the map gives the room back
    $("#mesh-text").focus();
  }

  function renderCompose() {
    const to = $("#mesh-to");
    // A reply belongs to the conversation it was started in.
    if (replyTo && (viewOf(replyTo) !== view || (view === "dm" && peerOf(replyTo) !== dmTo))) replyTo = null;
    let html = "";
    if (view === "dm" && dmTo) html = `<span class="wl-dim">To</span> <b>${esc(short(dmTo))}</b> <span class="wl-dim">${esc(long(dmTo))}, direct</span>`;
    if (replyTo) {
      html = `<span class="mesh-replying">↪ Replying to <b>${esc(replyTo.mine ? "me" : short(replyTo.from))}</b> ` +
        `<span class="wl-dim">${esc(replyTo.text.length > 50 ? `${replyTo.text.slice(0, 50)}…` : replyTo.text)}</span></span>` +
        `<button class="chip chip-mini" id="mesh-reply-x" title="Not a reply (Esc)">✕</button>`;
    }
    to.hidden = !html;
    to.innerHTML = html;
    const rx = $("#mesh-reply-x");
    if (rx) rx.onclick = () => { replyTo = null; renderCompose(); $("#mesh-text").focus(); };
    const c = (info?.channels || []).find((x) => x.index === view);
    $("#mesh-text").placeholder = view === "dm" ? (dmTo ? `Message ${short(dmTo)}` : "Pick a conversation first")
      : `Message #${c?.name || view}`;
    count();
  }

  function count() {
    const n = bytes($("#mesh-text").value.trim());
    const c = $("#mesh-count");
    // Over one packet a text goes in parts (a reply can't); the server splits
    // between words, so this is the least it takes and it has the last word.
    const parts = n <= textMax ? 1 : Math.ceil(n / partBytes);
    const tooLong = replyTo ? n > textMax : parts > partsMax;
    c.textContent = n > textMax ? (replyTo ? `${n}/${textMax}` : `${n} B · ${parts} parts`)
      : n > textMax * 0.75 ? `${textMax - n} left` : "";
    c.title = n > textMax && !replyTo ? `Sent as ${parts} or more messages, "[1/${parts}] …", one after another (at most ${partsMax})` : `${n} of ${textMax} bytes in one packet`;
    c.classList.toggle("warn", tooLong);
    $("#mesh-send").disabled = !connected || !n || tooLong || (view === "dm" && !dmTo);
  }

  const compose = chat.composer({ form: $("#mesh-form"), text: $("#mesh-text"), emoji: $("#mesh-emoji"), onInput: count,
    onEscape: () => { if (replyTo) { replyTo = null; renderCompose(); } } });

  function submit(e) {
    e.preventDefault();
    const text = $("#mesh-text").value.trim();
    if (!text || $("#mesh-send").disabled) return;
    const dm = view === "dm";
    tx({ t: "mesh_send", text, ch: dm ? 0 : view, to: dm ? dmTo : null, reply_to: replyTo?.id ?? null });
    replyTo = null;
    compose.clear();
    renderCompose();
  }

  // ---------- nodes

  const when = (t) => (ago(t) === "now" ? "just now" : `${ago(t)} ago`);
  const hopsText = (n) => (n.hops == null ? "" : n.hops === 0 ? "direct" : `${n.hops} hop${n.hops > 1 ? "s" : ""}`);

  function renderNodes() {
    const q = $("#mesh-find").value.trim().toLowerCase();
    const now = Date.now() / 1000;
    const every = everyNode();
    const list = every.filter((n) => !n.me)
      .filter((n) => !recentOnly || q || (n.last_heard && now - n.last_heard < HEARD_RECENT_S) || n.fav)
      .filter((n) => !q || [n.shortName, n.longName, n.id, n.hwModel, n.role].some((s) => String(s || "").toLowerCase().includes(q)))
      .sort((a, b) => (!!b.fav - !!a.fav) || ((b.last_heard || 0) - (a.last_heard || 0)));
    const el = $("#mesh-nodes");
    el.innerHTML = list.length ? "" : `<div class="wl-empty">${every.length ? "No nodes heard in the last day" : "No nodes yet"}</div>`;
    for (const n of list) {
      const b = document.createElement("button");
      const live = n.last_heard && now - n.last_heard < LIVE_S;
      b.className = "mesh-node" + (n.id === selected ? " on" : "") + (n.ignored ? " ignored" : "") + (n.kept ? " kept" : "");
      b.dataset.id = n.id;
      b.title = `${n.longName || ""} ${n.id || ""} · ${n.hwModel || ""}${n.role ? ` · ${n.role}` : ""} — click for details and traceroute` +
        (n.kept ? ". Kept by the station: the radio no longer lists it" : "");
      b.innerHTML = `<span class="mesh-short"><span class="mesh-fav${n.fav ? " on" : ""}" title="${n.fav
        ? "Favorite: click to remove" : "Make a favorite (a CLIENT_BASE node always relays for its favorites)"}">${n.fav ? "★" : "☆"}</span>${esc(n.shortName || "?")}</span>` +
        `<span class="mesh-long">${esc(n.longName || n.id)}<span class="mesh-sub">${esc([n.ignored ? "🚫 ignored" : "",
          n.role && n.role !== "CLIENT" ? n.role : "",
          n.lat != null && (n.lat || n.lon) ? `📍${kmText(where(n)) ? ` ${kmText(where(n))}` : ""}` : "", n.mqtt ? "MQTT" : ""].filter(Boolean).join(" "))}</span></span>` +
        `<span class="wl-dim">${esc(hopsText(n))}${n.snr != null && n.hops === 0 ? ` ${n.snr} dB` : ""}</span>` +
        `<span class="wl-dim">${n.battery != null ? (n.battery > 100 ? "⚡" : `${n.battery}%`) : ""}</span>` +
        `<span class="wl-dim${live ? " mesh-live" : ""}">${esc(ago(n.last_heard))}</span>`;
      b.onclick = () => select(n.id);
      b.querySelector(".mesh-fav").onclick = (e) => {
        e.stopPropagation();
        if (connected && !n.kept) tx({ t: "mesh_favorite", to: n.id, fav: !n.fav });   // a favorite is the radio's: it must list the node
      };
      el.append(b);
    }
    const all = every.filter((n) => !n.me).length, mine = nodes.filter((n) => !n.me).length;
    const cn = $("#mesh-count-nodes");
    cn.textContent = recentOnly && !q ? `${list.length} of ${all}` : `${all} known`;
    cn.title = all > mine ? `${mine} in the radio's own list, ${all - mine} more kept by the station (the radio drops the longest unheard)` : "";
    renderCard();
  }

  // ---------- a node's card: details, message, traceroute, favorite

  // NeighborInfo as map links: every pair heard; or, with a node selected,
  // just its own, each saying which way: green = it is heard by that node
  // (that node's list has it), blue = it hears that node, purple = both.
  // This node's own links, drawn with nothing selected so the map answers
  // "who hears me, and who do I hear" at a glance, in the colours a selected
  // node's links have. Hears us: mesh.py's hears_us (its NeighborInfo lists
  // us, or it was a traceroute's first hop; a DM's ACK is not direct, so not
  // here). We hear it: a packet from it arrived with no hop used in the last
  // day, or our own node's NeighborInfo lists it.
  // -> Map: node id -> {inn: the SNR it hears us at, out: the SNR we hear it at};
  // a side nobody vouches for is absent, one with no SNR given is null.
  function reach() {
    const me = myId(), now = Date.now() / 1000, by = new Map();
    if (!me) return by;
    const rec = (id) => by.get(id) || by.set(id, {}).get(id);
    for (const n of nodes) {
      if (!n.me && n.id !== me && n.hops === 0 && n.last_heard && now - n.last_heard < HEARD_RECENT_S) rec(n.id).out = n.snr ?? null;
    }
    for (const x of neighbors[me]?.neighbors || []) if (x.id !== me && !("out" in rec(x.id))) rec(x.id).out = x.snr ?? null;
    for (const h of hears) if (h.how !== "dm" && h.id !== me && !("inn" in rec(h.id))) rec(h.id).inn = h.snr ?? null;   // newest first
    return by;
  }

  function myLinks() {
    const me = myId(), by = reach();
    const db = (v) => (v != null ? `${v} dB` : "?");
    return [...by].map(([id, r]) => {
      const inn = "inn" in r, outw = "out" in r;
      return { from: me, to: id, cls: inn && outw ? "both" : inn ? "in" : "out",
        label: inn && outw ? `${db(r.inn)} / ${db(r.out)}` : db(inn ? r.inn : r.out),
        title: [inn ? `${short(id)} hears you at ${db(r.inn)}` : "", outw ? `you hear ${short(id)} at ${db(r.out)}` : ""].filter(Boolean).join("; ") };
    });
  }

  function links() {
    const snr = (a, b) => (neighbors[a]?.neighbors || []).find((x) => x.id === b)?.snr;
    const hears = (a, b) => (neighbors[a]?.neighbors || []).some((x) => x.id === b);
    const db = (v) => (v != null ? `${v} dB` : "?");
    const out = [], seen = new Set();
    const mine = selected ? [] : myLinks();
    for (const l of mine) seen.add([l.from, l.to].sort().join("-"));      // ours are drawn once, in colour, on top
    for (const [from, rec] of Object.entries(neighbors)) {
      for (const x of rec.neighbors || []) {
        if (selected && from !== selected && x.id !== selected) continue;
        const key = [from, x.id].sort().join("-");
        if (seen.has(key)) continue;
        seen.add(key);
        if (!selected) {
          out.push({ from, to: x.id, label: x.snr != null ? `${x.snr} dB` : "" });
          continue;
        }
        const other = from === selected ? x.id : from;
        const inn = hears(other, selected), outw = hears(selected, other);
        const s = short(selected), o = short(other);
        out.push({ from: selected, to: other,
          cls: inn && outw ? "both" : inn ? "in" : "out",
          label: inn && outw ? `${db(snr(other, selected))} / ${db(snr(selected, other))}` : db(inn ? snr(other, selected) : snr(selected, other)),
          title: [inn ? `${o} hears ${s} at ${db(snr(other, selected))}` : "", outw ? `${s} hears ${o} at ${db(snr(selected, other))}` : ""]
            .filter(Boolean).join("; ") });
      }
    }
    return [...out, ...mine];
  }
  const legend = () => `${selected ? `${short(selected)}: green hears it, blue it hears, purple both`
    : reach().size ? "you: green hears you, blue you hear, purple both" : ""}${nodes.length > 1 ? " · ◆ a router · beside a name: its SNR if heard direct, else its hops" : ""}`.replace(/^ · /, "");
  const drawLinks = () => { map.setLinks(links(), legend()); renderReach(); };

  // What the map draws of each node beyond its place: a router is a diamond;
  // one more than a hop away carries its hops; one this node hears or is
  // heard by wears that link's colour as a ring and is named first; its hover
  // text says the rest (role, how it reached us, when, how far, its battery).
  const ROUTERS = new Set(["ROUTER", "ROUTER_CLIENT", "ROUTER_LATE", "REPEATER"]);
  function mapNodes() {
    const r = reach(), db = (v) => (v != null ? `${v} dB` : "an SNR it didn't give");
    return nodes.map((n) => {
      if (n.me) return { ...n, tip: `${n.longName || ""} (${n.shortName || "?"}) ${n.id}\nthis node` };
      const x = r.get(n.id) || {}, w = where(n), inn = "inn" in x, outw = "out" in x;
      const how = n.hops == null ? "" : n.hops === 0 ? `direct${n.snr != null ? `, ${n.snr} dB` : ""}` : hopsText(n);
      const heard = n.last_heard ? (ago(n.last_heard) === "now" ? "heard just now" : `heard ${ago(n.last_heard)} ago`) : "never heard";
      const tip = [`${n.longName || n.shortName || n.id} (${n.shortName || "?"}) ${n.id}`,
        [n.role, how, n.mqtt ? "by MQTT" : ""].filter(Boolean).join(" · "),
        [heard, w ? `${kmText(w)} ${compass(w.deg)}` : "", n.battery != null ? (n.battery > 100 ? "powered" : `battery ${n.battery}%`) : ""].filter(Boolean).join(" · "),
        [inn ? `hears you at ${db(x.inn)}` : "", outw ? `you hear it at ${db(x.out)}` : ""].filter(Boolean).join(" · ")].filter(Boolean).join("\n");
      return { ...n, tip, shape: ROUTERS.has(n.role) ? "diamond" : "", ring: inn && outw ? "both" : inn ? "in" : outw ? "out" : "",
        glyph: n.hops > 0 ? String(Math.min(n.hops, 9)) : "", pin: !!(n.fav || inn || outw), kind: n.mqtt ? "mqtt" : "", ...badge(n) };
    });
  }

  // What a signal report needs beside a node's name on the map (2026-11-24):
  // heard direct, the SNR we last heard it at; relayed, how many hops; by MQTT, that.
  const signed = (v) => `${v > 0 ? "+" : ""}${v}`;
  function badge(n) {
    if (n.mqtt) return { badge: "MQTT", badgeCls: "mqtt" };
    if (n.hops == null) return {};
    if (n.hops === 0) return { badge: n.snr != null ? `${signed(n.snr)} dB` : "direct", badgeCls: "h0" };
    return { badge: `${n.hops} hop${n.hops > 1 ? "s" : ""}`, badgeCls: `h${Math.min(n.hops, 3)}` };
  }

  // ---------- the card at a node on the map: who it is, how we hear it, and what to do

  function reportText(n) {
    const r = reach().get(n.id) || {}, w = where(n), tr = traces[n.id];
    const how = n.mqtt ? "heard by MQTT" : n.hops === 0 ? `heard direct${n.snr != null ? `, SNR ${signed(n.snr)} dB` : ""}`
      : n.hops != null ? `heard over ${n.hops} hop${n.hops > 1 ? "s" : ""}` : "hops unknown";
    const parts = [`${n.shortName || "?"} (${n.longName || n.id}) ${n.id}`, how,
      "inn" in r ? `hears us at ${r.inn != null ? `${signed(r.inn)} dB` : "?"}` : "",
      w ? `${kmText(w)} ${compass(w.deg)}` : "", n.last_heard ? `${ago(n.last_heard) === "now" ? "just now" : `${ago(n.last_heard)} ago`}` : ""];
    let text = parts.filter(Boolean).join(" · ");
    if (tr?.state === "done") {
      const there = [myId(), ...(tr.route || []), tr.to];
      text += `\ntrace: ${there.map((id, i) => short(id) + (i ? ` (${tr.snr_towards?.[i - 1] == null ? "?" : `${signed(tr.snr_towards[i - 1])} dB`})` : "")).join(" > ")}`;
    }
    return text;
  }

  function popupHtml(id) {
    const n = byId(id);
    if (!n) return null;
    if (n.me) return `<div class="mm-pop-h"><b>${esc(n.shortName || "?")}</b> ${esc(n.longName || "")}</div><div class="wl-dim">this node · ${esc(n.id)}</div>`;
    const r = reach().get(n.id) || {}, w = where(n), tr = traces[n.id];
    const reqs = requests[n.id] || {}, busy = Object.values(reqs).some((x) => x.state === "running");
    const row = (k, v) => (v ? `<div class="mm-pop-r"><span>${k}</span><b>${esc(v)}</b></div>` : "");
    const b = badge(n);
    const trLine = !tr ? "" : tr.state === "running" ? `<div class="mm-pop-tr wl-dim">Traceroute… waiting (up to 90 s)</div>`
      : tr.state === "failed" ? `<div class="mm-pop-tr failed">Traceroute failed: ${esc(tr.error || "?")}</div>`
      : `<div class="mm-pop-tr">${[myId(), ...(tr.route || []), tr.to].map((x, i) => esc(short(x)) +
          (i ? ` <span class="wl-dim">${tr.snr_towards?.[i - 1] == null ? "?" : `${signed(tr.snr_towards[i - 1])}`}</span>` : "")).join(" → ")}` +
        ` <span class="wl-dim">· ${(tr.route || []).length} between · ${esc(ago(tr.done_t) === "now" ? "just now" : `${ago(tr.done_t)} ago`)}</span></div>`;
    const off = !connected || busy ? " disabled" : "";
    return `<div class="mm-pop-h"><b>${esc(n.shortName || "?")}</b> ${esc(n.longName || "")}` +
      (b.badge ? ` <span class="mm-pop-badge ${esc(b.badgeCls)}">${esc(b.badge)}</span>` : "") + `</div>` +
      `<div class="wl-dim mm-pop-id">${esc(n.id)}${n.hwModel ? ` · ${esc(n.hwModel)}` : ""}${n.role ? ` · ${esc(n.role)}` : ""}</div>` +
      row("hops", hopsText(n) || "unknown") +
      row("we hear it", "out" in r ? (r.out != null ? `${signed(r.out)} dB SNR` : "direct") : n.hops === 0 && n.snr != null ? `${signed(n.snr)} dB SNR` : "") +
      row("it hears us", "inn" in r ? (r.inn != null ? `${signed(r.inn)} dB SNR` : "yes") : "") +
      row("distance", w ? `${kmText(w)} ${compass(w.deg)} (${Math.round(w.deg)}°)` : "") +
      row("heard", n.last_heard ? (ago(n.last_heard) === "now" ? "just now" : `${ago(n.last_heard)} ago`) : "never") +
      row("battery", n.battery != null ? (n.battery > 100 ? "powered" : `${n.battery}%`) : "") +
      trLine +
      `<div class="mm-pop-acts">` +
      `<button class="chip chip-mini" data-act="trace"${!connected || tr?.state === "running" ? " disabled" : ""} title="Ask the mesh for the route to this node and back, with each hop's SNR">Traceroute</button>` +
      `<button class="chip chip-mini" data-act="dm" title="A direct message to this node">Message</button>` +
      `<button class="chip chip-mini" data-act="req-position"${off} title="Ask it for its position">Position</button>` +
      `<button class="chip chip-mini" data-act="req-telemetry"${off} title="Ask it for its battery, voltage and channel use">Telemetry</button>` +
      `<button class="chip chip-mini" data-act="req-neighbors"${off} title="Ask it which nodes it hears directly, with SNR">Neighbours</button>` +
      `<button class="chip chip-mini" data-act="copy" title="Copy its name, how we hear it and the last traceroute, for a signal report">Copy report</button>` +
      `</div>`;
  }

  function copyText(text) {
    // navigator.clipboard needs a secure page; a station on plain http over the LAN isn't one.
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    const t = document.createElement("textarea");
    t.value = text;
    t.style.position = "fixed";
    t.style.opacity = "0";
    document.body.append(t);
    t.select();
    const ok = document.execCommand("copy");
    t.remove();
    return ok ? Promise.resolve() : Promise.reject(new Error("copy refused"));
  }

  let lastReport = "";                       // the last report copied (the page test reads it: _report)
  function popupAct(id, act) {
    const n = byId(id);
    if (!n) return;
    if (act === "trace") tx({ t: "mesh_trace", to: id });
    else if (act === "dm") { map.closePopup(); directTo(id); }
    else if (act.startsWith("req-")) tx({ t: "mesh_request", to: id, kind: act.slice(4) });
    else if (act === "copy") {
      const text = reportText(n);
      lastReport = text;
      copyText(text).then(() => toast(`Copied: ${text.split("\n")[0]}`), () => toast("Couldn't copy: the browser refused", "warn"));
    }
  }
  const drawMap = () => map.setNodes(mapNodes(), myId());

  // ---------- who hears whom, by name, beside the map (as on the Map tab)

  let reachTab = "in", reachOpen = store.get("mesh.reach", window.innerWidth > 700);
  function renderReach() {
    const box = map.side, me = myId();
    box.hidden = !me;
    if (!me) return;
    box.classList.toggle("shut", !reachOpen);
    if (!reachOpen) {
      box.innerHTML = `<button class="chip chip-mini" data-toggle title="Who hears this node, and whom it hears, by name">Reach</button>`;
      return;
    }
    const all = [...reach()].map(([id, x]) => ({ id, n: byId(id), inn: "inn" in x, out: "out" in x, x, w: where(byId(id)) }));
    const of = { both: all.filter((r) => r.inn && r.out), in: all.filter((r) => r.inn), out: all.filter((r) => r.out) };
    const db = (v) => (v != null ? (v > 0 ? `+${v}` : `${v}`) : "?");
    const rows = of[reachTab].sort((a, b) => (b.w?.km ?? -1) - (a.w?.km ?? -1)).map((r) =>
      `<button class="mm-row${r.id === selected ? " sel" : ""}" data-id="${esc(r.id)}" title="${esc(`${long(r.id)} ${r.id}` +
        `${r.inn ? `; hears you at ${db(r.x.inn)} dB` : ""}${r.out ? `; you hear it at ${db(r.x.out)} dB` : ""}`)}">` +
      `<b>${esc(short(r.id))}</b><span class="mm-long">${esc(long(r.id))}</span>` +
      `<span class="mm-num">${esc(r.w ? kmText(r.w) : "")}</span>` +
      `<span class="mm-num mm-out" title="You hear it at">${r.out ? `▼${db(r.x.out)}` : ""}</span>` +
      `<span class="mm-num mm-in" title="It hears you at">${r.inn ? `▲${db(r.x.inn)}` : ""}</span></button>`).join("");
    const empty = { both: "No node both hears this one and is heard by it, yet.", in: "No node has said it hears this one yet. Traceroute a node to find out.",
      out: "No packet has come straight from a node in the last day." }[reachTab];
    const top = box.querySelector(".mm-rows")?.scrollTop || 0;
    box.innerHTML = `<div class="mm-side-h"><span class="seg">` +
      [["both", "Two-way"], ["in", "Hear you"], ["out", "You hear"]].map(([k, t]) =>
        `<button class="chip chip-mini${k === reachTab ? " on" : ""}" data-tab="${k}">${t} ${of[k].length}</button>`).join("") +
      `</span><button class="chip chip-mini" data-toggle title="Hide the list">✕</button></div>` +
      `<div class="mm-rows">${rows || `<div class="mm-none">${empty}</div>`}</div>` +
      `<div class="mm-src" title="Who hears you: a node's NeighborInfo or a traceroute's first hop, last 7 days. Whom you hear: packets that used no hop, last day.">` +
      `Direct links only, from what was already heard. Nothing is sent to find out.</div>`;
    box.querySelector(".mm-rows").scrollTop = top;
  }
  map.side.onclick = (e) => {
    const tab = e.target.closest("[data-tab]"), row = e.target.closest("[data-id]");
    if (e.target.closest("[data-toggle]")) { reachOpen = !reachOpen; store.set("mesh.reach", reachOpen); renderReach(); map.render(); }
    else if (tab) { reachTab = tab.dataset.tab; renderReach(); }
    else if (row && byId(row.dataset.id)) { if (selected !== row.dataset.id) select(row.dataset.id, true); map.show(row.dataset.id); }
  };

  // The map given the room, or the conversation back (the map's own ⤢).
  let bigMap = store.get("mesh.bigmap", false);
  function showBig() { $("#mesh-main").classList.toggle("mesh-bigmap", bigMap); map.setBig?.(bigMap); }
  // Esc gives the room back, unless it is closing something else first (a picker).
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !bigMap || e.defaultPrevented || $("#mesh").closest("[hidden]")) return;
    toggleBig(false);
  });
  function toggleBig(want = !bigMap) { bigMap = want; store.set("mesh.bigmap", bigMap); showBig(); }

  function select(id, fromMap = false) {
    selected = selected === id && !fromMap ? null : id;
    map.select(selected);
    map.setTrace(traces[selected] || null);
    drawLinks();
    renderNodes();
    if (fromMap && selected) $(`#mesh-nodes [data-id="${selected}"]`)?.scrollIntoView({ block: "nearest" });
  }

  function hop(id, snr) {
    const name = id === "?" ? "?" : short(id);
    return `<span class="mesh-hop" title="${esc(id === "?" ? "a relay that didn't say who it is" : `${long(id)} ${id}`)}">${esc(name)}` +
      `${snr !== undefined ? ` <span class="wl-dim">(${snr == null ? "?" : `${snr} dB`})</span>` : ""}</span>`;
  }

  function traceHtml(tr) {
    if (!tr) return "";
    if (tr.state === "running") return `<div class="mesh-trace"><span class="wl-dim">Traceroute…</span>waiting for ${esc(short(tr.to))} (up to 90 s)</div>`;
    if (tr.state === "failed") return `<div class="mesh-trace failed">Traceroute failed: ${esc(tr.error || "?")} <span class="wl-dim">${esc(when(tr.done_t))}</span></div>`;
    const there = [myId(), ...(tr.route || []), tr.to];
    const out = there.map((id, i) => hop(id, i ? tr.snr_towards?.[i - 1] : undefined)).join(" → ");
    let back = "";
    if (tr.route_back) {
      const path = [tr.to, ...tr.route_back, myId()];
      back = `<div><span class="wl-dim">back</span>${path.map((id, i) => hop(id, i ? tr.snr_back?.[i - 1] : undefined)).join(" → ")}</div>`;
    }
    return `<div class="mesh-trace"><div><span class="wl-dim">there</span>${out}</div>${back}` +
      `<div class="wl-dim">${(tr.route || []).length} hop${(tr.route || []).length === 1 ? "" : "s"} between · ${esc(when(tr.done_t))}</div></div>`;
  }

  // Why a link can't be a line on the map: the map places a node only by its
  // position (nodes sharing one are drawn apart).
  function offMap(a, b) {
    const pa = byId(a), pb = byId(b);
    const has = (x) => x && x.lat != null && (x.lat || x.lon);
    if (!pb) return "not in this node's list";
    if (!has(pb)) return "no position";
    return "";                        // the same spot is fine: the map fans those apart
  }

  // Who the node hears (its NeighborInfo) and who hears it (others' lists),
  // every one by name: the map only shows those it can place.
  function neighboursHtml(n) {
    const nb = neighbors[n.id];
    const by = Object.entries(neighbors).filter(([id, r]) => id !== n.id && (r.neighbors || []).some((x) => x.id === n.id))
      .map(([id, r]) => ({ id, snr: r.neighbors.find((x) => x.id === n.id).snr }));
    if (!nb && !by.length) return "";
    const item = (x, from, to) => {
      const why = offMap(from, to);
      return `<span class="mesh-nb" data-go="${esc(x.id)}" title="${esc(`${long(x.id)} ${x.id}`)}">${esc(short(x.id))}` +
        `${x.snr != null ? ` <span class="wl-dim">${x.snr} dB</span>` : ""}${why ? ` <span class="wl-dim mesh-nb-off">(${esc(why)})</span>` : ""}</span>`;
    };
    const list = (xs, from, to) => xs.map((x) => item(x, from(x), to(x))).join("");
    const hears = nb ? `<div><span class="wl-dim">hears</span>${(nb.neighbors || []).length
      ? list(nb.neighbors, () => n.id, (x) => x.id) : `<span class="wl-dim">nobody yet</span>`}` +
      ` <span class="wl-dim">· ${esc(when(nb.t))}</span></div>` : "";
    const heardBy = by.length ? `<div><span class="wl-dim">heard by</span>${list(by, (x) => x.id, () => n.id)}</div>` : "";
    return `<div class="mesh-nbs">${hears}${heardBy}</div>`;
  }

  // A node's line from the Health view's counts: packets in the last hour, what, and its hop limit.
  function trafficText(id) {
    const t = health?.talkers.find((x) => x.id === id);
    if (!t) return "";
    return `${t.n} packet${t.n === 1 ? "" : "s"} last hour, mostly ${portName(t.top_port)}` + (t.hop_start != null ? ` · hop limit ${t.hop_start}` : "");
  }

  function renderCard() {
    const card = $("#mesh-card");
    const n = byId(selected);
    card.hidden = !n;
    if (!n) { map.refreshPopup(); return; }
    const tr = traces[n.id];
    const w = where(n), env = n.env || {};
    const fields = [["hops", hopsText(n)], ["SNR", n.snr != null ? `${n.snr} dB` : ""],
      ["battery", n.battery != null ? (n.battery > 100 ? "powered" : `${n.battery}%`) : ""],
      ["voltage", n.voltage != null ? `${n.voltage.toFixed(2)} V` : ""],
      ["ch. util", n.util != null ? `${n.util.toFixed(1)}%` : ""],
      ["air tx", n.air_tx != null ? `${n.air_tx.toFixed(1)}%` : ""],
      ["uptime", dur(n.uptime)], ["heard", n.last_heard ? when(n.last_heard) : "never"],
      ["position", n.lat != null && (n.lat || n.lon) ? `${n.lat.toFixed(4)}, ${n.lon.toFixed(4)}` : "none"],
      ["altitude", n.alt != null && n.lat != null && (n.lat || n.lon) ? `${n.alt} m` : ""],
      ["fixed at", n.pos_t ? when(n.pos_t) : ""],
      ["distance", w ? `${kmText(w)} ${compass(w.deg)} (${Math.round(w.deg)}°)` : ""],
      ["temp", env.temp != null ? `${env.temp.toFixed(1)} °C / ${(env.temp * 9 / 5 + 32).toFixed(0)} °F` : ""],
      ["humidity", env.humidity != null ? `${env.humidity.toFixed(0)}%` : ""],
      ["pressure", env.pressure != null ? `${env.pressure.toFixed(1)} hPa` : ""],
      ["IAQ", env.iaq != null ? `${env.iaq}` : ""], ["light", env.lux != null ? `${Math.round(env.lux)} lx` : ""],
      ["wind", env.wind != null ? `${env.wind.toFixed(1)} m/s` : ""],
      ["key", n.pkc ? "PKI ✓" : ""], ["via", n.mqtt ? "MQTT" : ""],
      ["ignored", n.ignored ? "yes: its packets are dropped" : ""],
      ["traffic", trafficText(n.id)]].filter(([, v]) => v);
    // A kept node: favorite and ignore are the radio's and need it listed there;
    // Remove forgets the station's record, with or without the radio.
    const own = connected && !n.kept;
    const reqs = requests[n.id] || {};
    const busy = Object.values(reqs).some((r) => r.state === "running");
    const reqNote = Object.entries(reqs).filter(([, r]) => r.state !== "done" || Date.now() / 1000 - (r.done_t || 0) < 120)
      .map(([k, r]) => {
        const label = REQUESTS.find(([x]) => x === k)?.[1] || k;
        return r.state === "running" ? `${label}: asking…` : r.state === "done" ? `${label}: answered ${when(r.done_t)}`
          : `${label}: ${r.error || "no answer"}`;
      }).join(" · ");
    card.innerHTML = `<div class="mesh-card-head"><b>${esc(n.shortName || "?")}</b><span>${esc(n.longName || "")}</span>` +
      `<span class="wl-dim">${esc(n.id)} · ${esc(n.hwModel || "")}${n.role ? ` · ${esc(n.role)}` : ""}</span>` +
      `<span class="spacer"></span><button class="chip chip-mini" data-act="close" title="Close">✕</button></div>` +
      `<div class="mesh-card-grid">${fields.map(([k, v]) => `<span>${k} <b>${esc(v)}</b></span>`).join("")}</div>` +
      (n.kept ? `<div class="wl-dim mesh-kept-note">Kept by the station: the radio no longer lists this node (its own list is short). It goes back in when the radio next hears its name.</div>` : "") +
      `<div class="wl-row"><button class="chip chip-mini" data-act="dm">Message</button>` +
      `<button class="chip chip-mini" data-act="trace"${!connected || tr?.state === "running" ? " disabled" : ""} ` +
      `title="Ask the mesh for the route to this node and back, with each hop's SNR">Traceroute</button>` +
      `<button class="chip chip-mini" data-act="fav"${own ? "" : " disabled"}>${n.fav ? "★ Unfavorite" : "☆ Favorite"}</button>` +
      `<button class="chip chip-mini" data-act="ignore"${own ? "" : " disabled"} title="${n.ignored
        ? "Hear this node again" : "Drop this node's packets: it disappears from messages and the map"}">${n.ignored ? "Unignore" : "Ignore"}</button>` +
      `<button class="chip chip-mini" data-act="remove"${own || n.kept ? "" : " disabled"} title="Forget this node; it comes back when it is next heard">Remove</button></div>` +
      `<div class="wl-row mesh-reqs"><span class="wl-dim">Ask for</span>` +
      REQUESTS.map(([k, label, why]) => `<button class="chip chip-mini" data-req="${k}" title="${esc(why)}"${!connected || busy ? " disabled" : ""}>${label}</button>`).join("") +
      (reqNote ? `<span class="wl-dim mesh-req-note">${esc(reqNote)}</span>` : "") + "</div>" +
      neighboursHtml(n) + traceHtml(tr);
    map.refreshPopup();
    card.onclick = (e) => {
      const go = e.target.closest("[data-go]")?.dataset.go;
      if (go) { if (byId(go)) select(go); return; }
      const act = e.target.closest("button")?.dataset.act;
      if (act === "close") select(n.id);
      else if (act === "dm") directTo(n.id);
      else if (act === "trace") tx({ t: "mesh_trace", to: n.id });
      else if (act === "fav") tx({ t: "mesh_favorite", to: n.id, fav: !n.fav });
      else if (act === "ignore") tx({ t: "mesh_node", to: n.id, action: n.ignored ? "unignore" : "ignore" });
      else if (act === "remove" && confirm(`Remove ${n.shortName || n.id} from the node's list? It comes back when it is next heard.`)) {
        tx({ t: "mesh_node", to: n.id, action: "remove" });
        select(n.id);
      }
      const req = e.target.closest("button")?.dataset.req;
      if (req) tx({ t: "mesh_request", to: n.id, kind: req });
    };
  }

  function onRequest(r) {
    const { to, kind, ...rec } = r;
    (requests[to] ||= {})[kind] = rec;
    if (to === selected) renderCard();
    if (rec.state === "failed") toast(`Mesh: ${short(to)} didn't answer (${rec.error || "no reply"})`, "warn");
  }

  function onTrace(tr) {
    traces[tr.to] = tr;
    if (tr.to === selected) { map.setTrace(tr); renderCard(); }
    if (tr.state === "failed" && tr.to === selected) toast(`Traceroute to ${short(tr.to)} failed: ${tr.error}`, "warn");
  }

  // ---------- settings

  const sec = () => cfg?.sections.find((s) => s.name === cfgSec);
  const valueOf = (s, f) => (edits[s]?.[f] !== undefined ? edits[s][f] : cfg.values[s]?.[f]);

  function invalid(f, v) {
    if (f.type === "int" || f.type === "float") {
      if (typeof v !== "number" || Number.isNaN(v)) return "a number";
      if (f.type === "int" && !Number.isInteger(v)) return "a whole number";
      if ((f.min != null && v < f.min) || (f.max != null && v > f.max)) return `${f.min}–${f.max}`;
    }
    if (f.type === "str") {
      if (cfgSec === "owner" && !v.trim()) return "not empty";
      if (f.max_bytes && bytes(v) > f.max_bytes) return `at most ${f.max_bytes} bytes`;
    }
    return null;
  }

  function field(f) {
    const v = valueOf(cfgSec, f.name);
    let el;
    if (f.type === "bool") {
      el = document.createElement("input");
      el.type = "checkbox";
      el.checked = !!v;
    } else if (f.type === "enum") {
      el = document.createElement("select");
      const opts = f.options.includes(v) ? f.options : [...f.options, v];
      el.innerHTML = opts.map((o) => `<option${o === v ? " selected" : ""}>${esc(o)}</option>`).join("");
    } else {
      el = document.createElement("input");
      el.type = f.type === "str" ? "text" : "number";
      if (f.type !== "str") {
        el.step = f.type === "int" ? "1" : "any";
        if (f.min != null) el.min = f.min;
        if (f.max != null) el.max = f.max;
      }
      el.value = v ?? "";
      el.spellcheck = false;
    }
    el.disabled = !connected;
    const lab = document.createElement("label");
    const name = `${esc(f.label)}${f.unit ? ` <span class="wl-dim" style="display:inline">(${esc(f.unit)})</span>` : ""}` +
      (f.risky ? `<span class="risky" title="${esc(f.risky)}">⚠</span>` : "");
    lab.innerHTML = `<span>${name}${f.hint ? `<span class="wl-dim">${esc(f.hint)}</span>` : ""}</span>`;
    lab.title = f.risky || f.hint || "";
    lab.append(el);
    const read = () => (f.type === "bool" ? el.checked : f.type === "int" || f.type === "float"
      ? (el.value === "" ? NaN : Number(el.value)) : el.value);
    const update = () => {
      const val = read(), e = (edits[cfgSec] ||= {});
      if (val === cfg.values[cfgSec]?.[f.name]) delete e[f.name]; else e[f.name] = val;
      lab.classList.toggle("dirty", f.name in e);
      renderCfgTabs();
      renderCfgFoot();
    };
    el.addEventListener(f.type === "bool" || f.type === "enum" ? "change" : "input", update);
    lab.classList.toggle("dirty", edits[cfgSec]?.[f.name] !== undefined);
    return lab;
  }

  function renderCfgTabs() {
    const seg = $("#mesh-cfg-tabs");
    seg.innerHTML = "";
    for (const s of cfg?.sections || []) {
      const n = Object.keys(edits[s.name] || {}).length;
      const b = chip(s.label + (n ? " •" : ""), s.name, () => { cfgSec = s.name; renderCfg(); });
      on(b, s.name === cfgSec);
      seg.append(b);
    }
  }

  function renderCfgFoot() {
    const s = sec(), e = edits[cfgSec] || {}, names = Object.keys(e);
    const bad = s ? s.fields.filter((f) => names.includes(f.name) && invalid(f, e[f.name])) : [];
    $("#mesh-cfg-save").disabled = !connected || !names.length || bad.length > 0;
    $("#mesh-cfg-revert").disabled = !names.length;
    $("#mesh-reboot").disabled = !connected;
    $("#mesh-cfg-note").textContent = !cfg ? "The node has not sent its settings yet."
      : !connected ? `The node is not connected${linkError ? `: ${linkError}` : "."}`
      : bad.length ? bad.map((f) => `${f.label}: ${invalid(f, e[f.name])}`).join(" · ")
      : names.length ? `${names.length} unsaved change${names.length > 1 ? "s" : ""}` : "";
  }

  function renderCfg() {
    if (cfg && !sec()) cfgSec = cfg.sections[0]?.name ?? null;
    renderCfgTabs();
    const form = $("#mesh-cfg-form");
    form.innerHTML = "";
    for (const f of sec()?.fields || []) form.append(field(f));
    renderCfgFoot();
  }

  function saveCfg() {
    const s = sec(), e = edits[cfgSec];
    if (!s || !e || !Object.keys(e).length) return;
    const risky = s.fields.filter((f) => f.risky && f.name in e);
    if (risky.length && !confirm("These changes can cut this node off the mesh:\n\n" +
        risky.map((f) => `${f.label}: ${cfg.values[cfgSec][f.name]} → ${e[f.name]}\n  ${f.risky}`).join("\n") +
        "\n\nThe node may restart after saving. Save anyway?")) return;
    tx({ t: "mesh_config", section: cfgSec, values: { ...e } });
    $("#mesh-cfg-note").textContent = "Saving… (the node may restart)";
  }

  function setConfig(c) {
    cfg = c;
    for (const [s, e] of Object.entries(edits)) {          // saved, or changed elsewhere to the same
      for (const f of Object.keys(e)) if (e[f] === cfg?.values[s]?.[f]) delete e[f];
    }
    renderCfg();
  }

  // ---------- find radios: what this host can reach by USB and Bluetooth
  // (mesh_scan.py through the add-on's "mesh_scan"). Asked for, never on its
  // own; it lists and listens, and adding one stays a host-file edit. Seen
  // live 2026-11-22: the roof node that heard twice what the station's did
  // was in Bluetooth reach all along, and nothing on the page said so.
  let scanOpen = false, scan = null, scanning = null;   // scan: the last answer; scanning: the timer while one runs
  const SCAN_GIVE_UP_MS = 45000;
  const scanBtn = document.createElement("button");
  scanBtn.type = "button";
  scanBtn.className = "chip chip-mini";
  scanBtn.id = "mesh-scan-btn";
  scanBtn.textContent = "Find radios";
  scanBtn.title = "The Meshtastic radios this station can reach by USB or Bluetooth, and how to add one";
  $("#mesh-cfg-btn").before(scanBtn);
  // Auto-trace: the station's only sending of its own (mesh.py auto_trace).
  // Off: this node sends only what someone asks it to. Kept by the station.
  let autoTrace = false, autoPerHour = 6;
  const autoBtn = document.createElement("button");
  autoBtn.type = "button";
  autoBtn.className = "chip chip-mini";
  autoBtn.id = "mesh-auto-btn";
  scanBtn.before(autoBtn);
  function renderAuto() {
    autoBtn.textContent = `Auto-trace ${autoTrace ? "on" : "off"}`;
    autoBtn.classList.toggle("on", autoTrace);
    autoBtn.setAttribute("aria-pressed", String(autoTrace));
    autoBtn.disabled = switchedOff;
    autoBtn.title = autoTrace
      ? `This station traceroutes nodes it hears over relays by itself (route discovery, up to ${autoPerHour} an hour). Click to stop: it will send only what you send.`
      : "This station sends nothing by itself: no automatic traceroutes. Click to let it trace nodes heard over relays.";
  }
  autoBtn.addEventListener("click", () => tx({ t: "mesh_auto", on: !autoTrace }));
  const scanBox = document.createElement("div");
  scanBox.className = "mesh-scan";
  scanBox.id = "mesh-scan";
  scanBox.hidden = true;
  $("#mesh-health").after(scanBox);
  // The station's node whose port names this radio, if any: "this station's LF".
  function scanMine(r) {
    const want = [r.port, r.device].filter(Boolean).map((x) => String(x).toLowerCase());
    return (scan?.using || []).find((u) => String(u.port || "").split(",").some((p) => want.includes(p.trim().toLowerCase())))?.label;
  }
  function renderScan() {
    if (!scanOpen) return;
    const row = (r, what) => {
      const mine = scanMine(r);
      return `<div class="mesh-scan-row"><b>${esc(r.name || r.device || "?")}</b><span class="wl-dim">${esc(what)}</span>` +
        (r.port ? `<code>${esc(r.port)}</code>` : `<span class="wl-dim">no port to give: the host file refuses a character in its name</span>`) +
        (mine ? `<span class="mesh-scan-mine">this station's ${esc(mine)}</span>` : "") + `</div>`;
    };
    const errs = Object.entries(scan?.errors || {});
    const none = (what) => `<div class="wl-dim">${what}</div>`;
    scanBox.innerHTML = `<div class="wl-row"><button type="button" class="chip chip-mini" id="mesh-scan-go"${scanning ? " disabled" : ""}>` +
      `${scanning ? "Looking…" : scan ? "Look again" : "Look"}</button>` +
      `<span class="wl-dim">Lists USB serial devices and listens about 10 s for Meshtastic radios on Bluetooth. It opens nothing, connects to nothing and sends nothing.</span></div>` +
      (scan ? `<div class="label">Bluetooth</div>` +
        (scan.ble?.length ? scan.ble.map((r) => row(r, r.rssi != null ? `${r.rssi} dBm` : "")).join("") : none(errs.some(([k]) => k === "ble") ? "Not looked at (below)." : "None advertising now.")) +
        `<div class="label">USB</div>` +
        (scan.usb?.length ? scan.usb.map((r) => row(r, [r.maker, r.device].filter(Boolean).join(" · "))).join("") : none(errs.some(([k]) => k === "usb") ? "Not looked at (below)." : "No USB serial device.")) +
        errs.map(([k, v]) => `<div class="mesh-scan-err">${esc(k)}: ${esc(v)}</div>`).join("") : "") +
      `<div class="wl-dim mesh-scan-notes">` +
      `<p>USB lists every serial device, a transceiver's CAT cable included: only opening one tells a node from the rest, and this opens none. Pick by name.</p>` +
      `<p>Not listed: a node this station or a phone is connected to over Bluetooth (it stops advertising), a node with a USB client attached (its Bluetooth is off), and a node on Wi-Fi (name it as <code>tcp:HOST</code>).</p>` +
      `<p>To add one, list it in the host file under mesh <code>"radios"</code>, for example <code>{"label": "Roof", "port": "ble:NAME"}</code>, ` +
      `and apply it with <code>ranch-radio install --apply</code>. A Bluetooth node must be bonded to this host first (its PIN), and its one Bluetooth connection is then the station's: a phone can't connect while the station holds it.</p></div>`;
    $("#mesh-scan-go").onclick = runScan;
  }
  function runScan() {
    if (scanning) return;
    scanning = setTimeout(() => {            // no answer (a read-only page, the add-on switched off): stop waiting
      scanning = null;
      scan = { usb: [], ble: [], errors: { scan: "no answer from the station" }, using: [] };
      renderScan();
    }, SCAN_GIVE_UP_MS);
    send({ t: "mesh_scan" });                // the station's own: not for one node
    renderScan();
  }
  function scanAnswer(d) {
    clearTimeout(scanning);
    scanning = null;
    scan = d;
    renderScan();
  }
  scanBtn.onclick = () => {
    scanOpen = !scanOpen;
    cfgOpen = healthOpen = false;
    showViews();
    if (scanOpen) { renderScan(); if (!scan) runScan(); }
  };

  // Settings, Health and Find radios each take the place of the conversation and node list.
  function showViews() {
    on($("#mesh-cfg-btn"), cfgOpen);
    on($("#mesh-health-btn"), healthOpen);
    on(scanBtn, scanOpen);
    $("#mesh-cfg").hidden = !cfgOpen;
    $("#mesh-health").hidden = !healthOpen;
    scanBox.hidden = !scanOpen;
    $("#mesh-main").hidden = cfgOpen || healthOpen || scanOpen;
  }
  // On a phone the header's key, stats and buttons fold behind ⋯ (UX plan PR
  // 5b, 2026-11-19; app.css shows it at 700 px and under). It opens and shuts
  // that menu; choosing one of its buttons, or a click outside, shuts it.
  const tools = $("#mesh-tools"), more = $("#mesh-more");
  if (tools && more) {
    const shutTools = () => { tools.classList.remove("open"); more.setAttribute("aria-expanded", "false"); };
    more.onclick = (e) => {
      e.stopPropagation();
      const open = tools.classList.toggle("open");
      more.setAttribute("aria-expanded", open ? "true" : "false");
    };
    tools.addEventListener("click", (e) => { if (e.target.closest("button")) shutTools(); });
    document.addEventListener("click", (e) => { if (!e.target.closest("#mesh-tools, #mesh-more")) shutTools(); });
  }
  $("#mesh-cfg-btn").onclick = () => {
    cfgOpen = !cfgOpen;
    healthOpen = scanOpen = false;
    showViews();
    if (cfgOpen) renderCfg();
  };
  $("#mesh-health-btn").onclick = () => {
    healthOpen = !healthOpen;
    cfgOpen = scanOpen = false;
    showViews();
    if (healthOpen) renderHealth();
  };

  // ---------- health: what this node hears, added up (mesh_stats.py)

  const HEALTH = { healthy: ["Healthy", "Nothing stands out in what this node hears"],
    attention: ["Attention", "Something below is worth a look"], busy: ["Busy", "The channel is busy: nodes hold back what they send"] };
  const PACKETS_KEEP = 200;

  function renderHealthBtn() {
    const b = $("#mesh-health-btn"), st = health?.status || "healthy";
    b.classList.toggle("mesh-health-warn", st === "attention");
    b.classList.toggle("mesh-health-bad", st === "busy");
    b.title = `${HEALTH[st][0]}: ${HEALTH[st][1]}. What this node hears: traffic, channel load, what looks wrong.`;
  }

  // Packets a minute for the last hour, one bar a minute.
  function rateChart(rate) {
    const W = 600, H = 72, n = rate.length || 1, max = Math.max(4, ...rate), bw = W / n;
    const bars = rate.map((v, i) => v ? `<rect class="mh-bar" x="${(i * bw + 0.5).toFixed(1)}" y="${(H - (v / max) * (H - 4)).toFixed(1)}" ` +
      `width="${Math.max(1, bw - 1).toFixed(1)}" height="${((v / max) * (H - 4)).toFixed(1)}"><title>${n - i} min ago: ${v}</title></rect>` : "").join("");
    return `<svg class="mh-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Packets a minute, last hour">` +
      `<line class="mh-axis" x1="0" y1="${H - 0.5}" x2="${W}" y2="${H - 0.5}"/>${bars}</svg>` +
      `<div class="mh-scale"><span>60 min ago</span><span>top ${max}/min</span><span>now</span></div>`;
  }

  // This node's channel utilization (and its own air-tx) over 24 h, with the firmware's 25 % and 40 % lines.
  function utilChart(h) {
    const W = 600, H = 72, end = h.now, start = end - 86400;
    const top = Math.max(h.util_bad + 10, ...h.util.map(([, u]) => u || 0));
    const x = (t) => ((t - start) / 86400 * W).toFixed(1), y = (v) => (H - (v / top) * (H - 2)).toFixed(1);
    const line = (i, cls) => {
      const pts = h.util.filter((r) => r[i] != null).map((r) => `${x(r[0] + h.util_slot_s / 2)},${y(r[i])}`);
      return pts.length ? `<polyline class="${cls}" points="${pts.join(" ")}"/>` : "";
    };
    const guide = (v, cls) => `<line class="${cls}" x1="0" x2="${W}" y1="${y(v)}" y2="${y(v)}"><title>${v} %</title></line>`;
    const dots = h.util.length < 2 ? h.util.filter((r) => r[1] != null).map((r) => `<circle class="mh-util-dot" cx="${x(r[0] + h.util_slot_s / 2)}" cy="${y(r[1])}" r="3"/>`).join("") : "";
    return `<svg class="mh-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Channel utilization, last 24 hours">` +
      `<line class="mh-axis" x1="0" y1="${H - 0.5}" x2="${W}" y2="${H - 0.5}"/>${guide(h.util_warn, "mh-warn")}${guide(h.util_bad, "mh-bad")}` +
      `${line(2, "mh-air")}${line(1, "mh-util")}${dots}</svg>` +
      `<div class="mh-scale"><span>24 h ago</span><span><i class="mh-key mh-util"></i>channel <i class="mh-key mh-air"></i>our tx · lines at ${h.util_warn} % and ${h.util_bad} %</span><span>now</span></div>`;
  }

  const portName = (p) => String(p || "?").replace(/_APP$/, "").replace(/_/g, " ").toLowerCase();

  function renderHealth() {
    const el = $("#mesh-health");
    const h = health;
    if (!h) { el.innerHTML = `<div class="wl-empty">Nothing heard yet.</div>`; return; }
    const [label, why] = HEALTH[h.status] || HEALTH.healthy;
    const u = h.util_now;
    const head = `<div class="mesh-health-band ${esc(h.status)}" title="${esc(why)}"><b>${esc(label)}</b>` +
      `<span>${h.total} packet${h.total === 1 ? "" : "s"} from ${h.nodes} node${h.nodes === 1 ? "" : "s"} in the last hour</span>` +
      (u && u[1] != null ? `<span>channel ${u[1].toFixed(1)} %${u[2] != null ? ` · our tx ${u[2].toFixed(1)} %` : ""}</span>` : "") +
      `<span class="spacer"></span><span class="wl-dim">counted since ${esc(hhmm(h.since))} (starts over when the station restarts)</span></div>`;
    const charts = `<div class="mesh-health-charts"><div><div class="label">Packets a minute</div>${rateChart(h.rate)}</div>` +
      `<div><div class="label">Channel utilization</div>${h.util.length ? utilChart(h) : `<div class="wl-empty">No reading from the node yet</div>`}</div></div>`;
    const finds = h.findings.length ? h.findings.map((f, i) => `<button class="mesh-finding ${esc(f.level)}" data-i="${i}"${f.node ? "" : " disabled"}>` +
      `<b>${esc(f.title)}</b><span>${esc(f.detail)}</span><span class="wl-dim">${esc(f.fix)}</span></button>`).join("")
      : `<div class="wl-empty">Nothing stands out.</div>`;
    const rows = h.talkers.map((t) => `<tr data-id="${esc(t.id)}"><td title="${esc(`${long(t.id)} ${t.id}`)}">${esc(short(t.id))}</td>` +
      `<td>${t.n}${t.per_hour != null ? ` <span class="wl-dim">(${t.per_hour}/h)</span>` : ""}</td>` +
      `<td title="${esc(Object.entries(t.ports).map(([p, n]) => `${portName(p)} ${n}`).join(", "))}">${esc(portName(t.top_port))}</td>` +
      `<td>${t.hop_start ?? ""}</td><td>${t.hops ?? ""}</td><td>${t.snr_avg != null ? `${t.snr_avg} dB` : ""}</td><td>${t.mqtt || ""}</td></tr>`).join("");
    const table = h.talkers.length ? `<div class="mesh-scroll"><table class="mesh-talkers mesh-click"><thead><tr><th>Node</th><th>Packets</th><th>Mostly</th>` +
      `<th title="The hop limit it sends with">Hop limit</th><th>Hops</th><th title="Average, heard directly">SNR</th><th>MQTT</th></tr></thead><tbody>${rows}</tbody></table></div>` : "";
    el.innerHTML = head + charts + `<div class="label">What looks wrong</div><div class="mesh-findings">${finds}</div>` +
      (table ? `<div class="label">Top talkers, last hour</div>${table}` : "") +
      `<div class="label">Packets heard <span class="wl-dim">(newest first, the last ${PACKETS_KEEP})</span></div><div class="mesh-packets" id="mesh-packets"></div>`;
    el.querySelector(".mesh-findings").onclick = (e) => {
      const f = h.findings[e.target.closest(".mesh-finding")?.dataset.i];
      if (f?.node) openNode(f.node);
    };
    for (const tr of el.querySelectorAll(".mesh-talkers tbody tr")) tr.onclick = () => openNode(tr.dataset.id);
    renderPackets();
  }

  // The relay byte is the last byte of the node that passed a packet on: a name
  // when exactly one node we know ends in it, the byte when none or several do.
  function relayCell(b) {
    const hex = Number(b).toString(16).padStart(2, "0");
    const ids = new Set([...nodes.map((n) => n.id), ...Object.keys(names)].filter((id) => id?.endsWith(hex)));
    const [only] = ids;
    return ids.size === 1 ? `<span title="${esc(`${long(only)} ${only} (the only node we know ending in ${hex})`)}">${esc(short(only))}</span>`
      : `<span title="${esc(ids.size ? `${ids.size} nodes we know end in ${hex}` : "The last byte of the node that passed it to us")}">…${hex}</span>`;
  }

  function renderPackets() {
    const el = $("#mesh-packets");
    if (!el) return;
    const rows = packets.slice(-PACKETS_KEEP).reverse().map((p) => `<tr><td>${p.t ? esc(new Date(p.t * 1000).toTimeString().slice(0, 8)) : ""}</td>` +
      `<td title="${esc(`${long(p.from)} ${p.from}`)}">${esc(short(p.from))}</td><td>${esc(portName(p.port))}</td>` +
      `<td>${p.mqtt ? "MQTT" : p.hops == null ? "" : p.hops === 0 ? "direct" : `${p.hops} hop${p.hops > 1 ? "s" : ""}`}</td>` +
      `<td>${p.hop_start ?? ""}</td><td>${p.snr != null ? `${p.snr} dB` : ""}</td><td>${p.rssi != null ? `${p.rssi} dBm` : ""}</td>` +
      `<td>${p.relay != null && p.hops ? relayCell(p.relay) : ""}</td>` +
      `<td>${p.size != null ? `${p.size} B` : ""}</td></tr>`).join("");
    el.innerHTML = rows ? `<table class="mesh-talkers"><thead><tr><th>Time</th><th>From</th><th>Port</th><th>Path</th><th>Hop limit</th>` +
      `<th>SNR</th><th>RSSI</th><th>Relay</th><th>Size</th></tr></thead><tbody>${rows}</tbody></table>` : `<div class="wl-empty">None yet</div>`;
  }

  let packetsDue = null;
  function onPacket(p) {
    packets.push(p);
    if (packets.length > PACKETS_KEEP) packets.splice(0, packets.length - PACKETS_KEEP);
    if (healthOpen && !packetsDue) packetsDue = setTimeout(() => { packetsDue = null; renderPackets(); }, 1000);
  }

  function openNode(id) {
    healthOpen = false;
    showViews();
    if (selected !== id) select(id, true);
  }
  $("#mesh-cfg-form").onsubmit = (e) => e.preventDefault();
  $("#mesh-cfg-save").onclick = saveCfg;
  $("#mesh-cfg-revert").onclick = () => { delete edits[cfgSec]; renderCfg(); };
  $("#mesh-reboot").onclick = () => {
    if (connected && confirm("Reboot the node? The link drops for a few seconds and comes back on its own.")) tx({ t: "mesh_reboot" });
  };

  // ---------- from app.js

  let lastCfg = null;
  function hello(snap, cfg) {
    lastCfg = cfg || lastCfg;
    $("#mesh").hidden = !cfg?.mesh;
    if (!cfg?.mesh || !snap) return;
    radios = snap.radios || [];                    // a station with one node sends none
    // A page opened (or reconnected) with another node picked: the first
    // node's snapshot arrives in the hello; ask for the picked one's.
    // Switched off: every node is off, whichever is picked; say so from this snapshot.
    const want = store.get("mesh.radio", "");
    if (!snap.__selected && !snap.off && radios.length > 1 && want && radios.some((r) => r.id === want)) {
      cur = want;
      renderRadios();
      tx({ t: "mesh_select" });
      return;                                      // the picked node's own snapshot follows
    }
    if (!snap.__selected) cur = radios.some((r) => r.id === want) && snap.off ? want : "";
    renderRadios();
    // Another node's view: nothing of the last one's carries over.
    for (const k of Object.keys(traces)) delete traces[k];
    for (const k of Object.keys(edits)) delete edits[k];
    selected = null; replyTo = null; dmTo = null;
    info = snap.info;
    nodes = snap.nodes || [];
    names = snap.names || {};
    kept = Object.fromEntries((snap.kept || []).filter((n) => n?.id).map((n) => [n.id, n]));
    convState = snap.convs || { since: Date.now() / 1000, convs: {} };
    messages = snap.messages || [];
    switchedOff = !!snap.off;
    connected = !!snap.status?.connected;
    linkError = snap.status?.error || null;
    textMax = snap.text_max || textMax;
    partsMax = snap.parts_max || partsMax;
    partBytes = snap.part_bytes || partBytes;
    setConfig(snap.config || null);
    for (const tr of snap.traces || []) traces[tr.to] = tr;
    requests = snap.requests || {};
    neighbors = snap.neighbors || {};
    hears = snap.hears || [];
    health = snap.health || null;
    packets = snap.packets || [];
    autoTrace = !!snap.auto_trace;
    autoPerHour = snap.auto_trace_per_hour || autoPerHour;
    renderAuto();
    renderHealthBtn();
    if (healthOpen) renderHealth();
    drawMap();
    drawLinks();
    renderHead();
    renderHears();
    renderTabs();
    renderMessages();
    renderCompose();
    renderNodes();
  }

  function event(m) {
    if (m.scan) { scanAnswer(m.scan); return; }    // the station's answer to "Find radios": no node's own
    if (m.select) {                                // a node's snapshot, asked for by this page
      if ((m.node || "") === cur) hello({ ...m.select, __selected: true }, lastCfg);
      return;
    }
    if (m.status) radioStatus(m.node, m.status);
    if ((m.node || "") !== cur) {                  // another node: its dot and its unread count only
      if (m.msg && !m.msg.mine && !m.msg.emoji) { unread[m.node || ""] = (unread[m.node || ""] || 0) + 1; renderRadios(); }
      return;
    }
    if (m.status) {
      connected = !!m.status.connected;
      linkError = m.status.error || null;
      renderHead();
      count();
      renderCfg();
      renderCard();
    }
    if (m.info) {
      info = m.info;
      drawMap();
      renderHead();
      renderTabs();
      renderMessages();
      renderCompose();
    }
    if (m.config) setConfig(m.config);
    if (typeof m.auto_trace === "boolean") { autoTrace = m.auto_trace; renderAuto(); }
    if (m.names) Object.assign(names, m.names);
    if (m.kept) {
      for (const n of m.kept.set || []) if (n?.id) kept[n.id] = n;
      for (const id of m.kept.drop || []) delete kept[id];
      if (!m.nodes) { renderNodes(); if (selected && !nodes.some((n) => n.id === selected)) renderCard(); }
    }
    if (m.convs) { convState = m.convs; if (!m.history) { renderTabs(); renderChatHead(); } }
    if (m.history) {                               // after a delete or an undo: the station's whole history
      const inDm = (list) => list.some((x) => x.dm && peerOf(x) === dmTo);
      const had = view === "dm" && dmTo && inDm(messages);
      messages = m.history;
      if (had && !inDm(messages)) dmTo = null;       // the open conversation was deleted
      if (replyTo && !messages.includes(replyTo)) replyTo = messages.find((x) => x.t === replyTo.t) || null;
      renderTabs(); renderMessages(); renderCompose();
    }
    if (m.deleted) {
      const d = m.deleted;
      toast(`Deleted ${d.n} message${d.n === 1 ? "" : "s"} from this station` +
        (d.kept ? ` (${d.kept} still being sent kept)` : ""), "info",
        { label: "Undo", ms: 12000, fn: () => tx({ t: "mesh_conv", action: "undo", ref: d.ref }) });
    }       // just what changed; the {nodes} right after it redraws
    if (m.nodes) {
      nodes = m.nodes;
      drawMap();
      drawLinks();
      renderNodes();
      renderHead();
      renderHears();
    }
    if (m.neighbors) {
      neighbors = m.neighbors;
      drawMap();
      drawLinks();
      renderCard();
    }
    if (m.hears) { hears = m.hears; renderHears(); drawMap(); drawLinks(); }
    if (m.status) renderHears();
    if (m.request) onRequest(m.request);
    if (m.trace) onTrace(m.trace);
    if (m.rx) { map.pulse(m.rx.from); onPacket(m.rx); }
    if (m.health) {
      health = m.health;
      renderHealthBtn();
      if (healthOpen) renderHealth();
      renderCard();
    }
    if (m.msg && m.msg.mine) map.pulse(myId(), "tx");        // ours, handed to the node to send: red, at us
    if (m.msg) addMessage(m.msg);
    if (m.update) updateMessage(m.update);
  }

  $("#mesh-form").onsubmit = submit;
  $("#mesh-find").oninput = renderNodes;
  $("#mesh-all").onclick = () => { recentOnly = !recentOnly; on($("#mesh-all"), recentOnly); renderNodes(); };
  setInterval(() => { renderNodes(); renderHears(); drawMap(); renderReach(); }, 60000);  // keep "heard 5m ago" and the map's colours current
  showBig();

  // {"t": "addon", "addon": "mesh", "on", "error", "snapshot"}: switched on or off
  function power(m) {
    if (m.snapshot) { hello(m.snapshot, { mesh: true }); }
    else {
      switchedOff = !m.on; connected = false; linkError = m.error || null; renderHead();
      for (const r of radios) { r.connected = false; r.error = m.error || null; }   // switched off: every node is
      renderRadios();
    }
    if (m.error) { linkError = m.error; renderHead(); }
    renderCompose();
    renderNodes();
  }

  // For the Shack (station.js): nodes heard in the last day and the
  // last quarter hour, and the last message someone sent.
  function summary() {
    const now = Date.now() / 1000, me = myId();
    const others = everyNode().filter((n) => n.id !== me && !n.me && n.last_heard);
    const last = [...messages].reverse().find((m) => !m.mine && !isReaction(m) && m.text);
    return { connected, day: others.filter((n) => now - n.last_heard < HEARD_RECENT_S).length,
      live: others.filter((n) => now - n.last_heard < LIVE_S).length,
      last: last ? { who: short(last.from), text: last.text, t: last.t } : null };
  }
  // For the bar and the Shack (station.js): Mesh is up while any of its
  // nodes is, and "down" names the ones that are not.
  function radioLinks() {
    if (radios.length < 2) return { up: connected, down: [] };
    return { up: radios.some((r) => r.connected), down: radios.filter((r) => !r.connected).map((r) => r.label) };
  }
  return { hello, event, power, summary, radioLinks, connected: () => connected, _nodes: () => nodes, _report: () => lastReport };
})();
