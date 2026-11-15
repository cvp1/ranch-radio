"use strict";

// Mesh panel: a Meshtastic node's channels, direct conversations (one per
// node), replies and emoji reactions, the node list (a node's card: message
// it, traceroute, ask for its position / telemetry / user info / neighbours, favorite,
// ignore, remove), the map (meshmap.js, with NeighborInfo links), who hears
// this node ("heard by N": the server's hears_us) and settings. Everything goes over the control WebSocket ({"t": "mesh"} in,
// mesh_send / mesh_config / mesh_favorite / mesh_trace / mesh_request /
// mesh_node / mesh_reboot out); the server keeps the history, so a page
// opened later sees it too.
// Uses app.js's $, send, toast and on.

const mesh = (() => {
  const HEARD_RECENT_S = 24 * 3600;
  const ACK = { sending: ["…", "Sending"], sent: ["✓?", "Sent, waiting for an ACK"],
    relayed: ["✓", "A neighbour rebroadcast it"], delivered: ["✓✓", "Delivered"],
    failed: ["✗", "Not delivered"], unknown: ["?", "No ACK came back; it may not have gone out"] };
  let info = null, nodes = [], messages = [], connected = false, linkError = null, textMax = 200;
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
  const REACTIONS = ["👍", "❤️", "😂", "‼️", "❓", "🎉"];
  const REQUESTS = [["position", "Position", "Ask it for its position"],
    ["telemetry", "Telemetry", "Ask it for its battery, voltage and channel use"],
    ["env", "Environment", "Ask it for its sensor readings (temperature, humidity, pressure)"],
    ["nodeinfo", "User info", "Swap user info with it (its name and key; it gets ours)"],
    ["neighbors", "Neighbours", "Ask it which nodes it hears directly, with SNR (answers only with its NeighborInfo module on; once every 3 minutes)"]];
  const map = meshMap.create($("#mesh-map"), { onSelect: (id) => select(id, true) });
  const LIVE_S = 15 * 60;
  const unread = {};        // channel index or "dm" -> count
  const unreadDm = {};      // node id -> count
  let cfg = null;           // {sections, values}: the bridge's settings schema and the node's values
  let cfgOpen = false, cfgSec = null;
  const edits = {};         // section -> {field: value} not yet saved

  const byId = (id) => nodes.find((n) => n.id === id);
  const short = (id) => byId(id)?.shortName || id || "?";
  const long = (id) => byId(id)?.longName || id || "?";
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
    st.title = connected ? `${me?.hw || ""} · firmware ${me?.fw || "?"} · ${info?.preset || ""}` : linkError || "";
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
      const n = byId(g.id);
      const how = g.how.map((k) => (HOW[k] || [k])[0]).join(" · ");
      const why = g.how.map((k) => (HOW[k] || [k, ""])[1]).filter(Boolean).join("; ");
      return `<button class="mesh-hear" data-id="${esc(g.id)}" title="${esc(`${long(g.id)} ${g.id}: ${why}`)}">` +
        `<b>${esc(short(g.id))}</b><span class="mesh-long">${esc(n?.longName || g.id)}</span>` +
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

  function renderTabs() {
    const seg = $("#mesh-tabs");
    seg.innerHTML = "";
    const tabs = (info?.channels || []).map((c) => [c.index, c.name]);
    tabs.push(["dm", "Direct"]);
    // Before the node has sent its channels (a restart, switching on), keep the
    // view: falling back to Direct then would stick once the channels arrive.
    if (view === null || (info && !tabs.some(([v]) => v === view))) view = tabs.length > 1 ? tabs[0][0] : "dm";
    for (const [v, label] of tabs) {
      const b = document.createElement("button");
      b.className = "chip" + (v === view ? " on" : "");
      b.innerHTML = esc(label) + (unread[v] ? ` <span class="mesh-unread">${unread[v]}</span>` : "");
      b.onclick = () => {
        if (v === "dm" && view === "dm") dmTo = null;        // Direct again: back to the conversations
        view = v;
        if (v !== "dm") unread[v] = 0;
        renderTabs(); renderMessages(); renderCompose();
      };
      seg.append(b);
    }
  }

  // ---------- messages

  // Reactions to message id, folded into "👍 2" chips (who in the tooltip).
  function reactionsTo(id) {
    const by = new Map();
    for (const r of messages) {
      if (!isReaction(r) || r.reply_id !== id) continue;
      if (!by.has(r.text)) by.set(r.text, []);
      by.get(r.text).push(r.mine ? "me" : short(r.from));
    }
    return [...by].map(([e, who]) => `<span class="mesh-react" title="${esc(who.join(", "))}">${esc(e)}${who.length > 1 ? ` ${who.length}` : ""}</span>`).join("");
  }

  function row(m) {
    const div = document.createElement("div");
    div.className = "mesh-msg" + (m.mine ? " mine" : "");
    div.dataset.key = m.mine ? `t${m.t}` : `i${m.id}`;
    const who = m.mine ? (m.dm ? `→ ${short(m.to)}` : "me")
      : (m.dm ? `${short(m.from)} → me` : short(m.from));
    const [glyph, why] = m.mine ? (ACK[m.ack] || ["", ""]) : ["", ""];
    const meta = m.mine ? "" : [m.hops != null ? `${m.hops} hop${m.hops === 1 ? "" : "s"}` : "",
      m.snr != null ? `SNR ${m.snr}` : ""].filter(Boolean).join(" · ");
    const orig = m.reply_id ? byMsgId(m.reply_id) : null;
    const quote = m.reply_id ? `<span class="mesh-quote" title="In reply to">↪ ${orig
      ? `<b>${esc(orig.mine ? "me" : short(orig.from))}</b> ${esc(orig.text.length > 60 ? `${orig.text.slice(0, 60)}…` : orig.text)}`
      : "a message no longer here"}</span>` : "";
    const canAct = connected && m.id != null && m.ack !== "failed";
    const acts = canAct ? `<span class="mesh-acts"><button data-act="reply" title="Reply">↩</button>` +
      REACTIONS.map((e) => `<button data-react="${esc(e)}" title="React ${esc(e)}">${esc(e)}</button>`).join("") + "</span>" : "";
    const retry = m.mine && (m.ack === "failed" || m.ack === "unknown") && connected ? `<button class="chip chip-mini mesh-retry" data-act="retry" title="Send it again">Retry</button>` : "";
    div.innerHTML = `<span class="mesh-time">${hhmm(m.t)}</span>` +
      `<button class="mesh-who" title="${esc(m.mine ? "" : `${long(m.from)} ${m.from || ""}`)}">${esc(who)}</button>` +
      `<span class="mesh-text">${quote}<span class="mesh-body">${esc(m.text)}</span>` +
      `<span class="mesh-reacts">${m.id != null ? reactionsTo(m.id) : ""}</span></span>` +
      `<span class="mesh-meta">${acts}${retry}${esc(meta)}${m.mine ? `<span class="mesh-ack ${esc(m.ack)}" title="${esc(why + (m.why ? `: ${m.why}` : ""))}">${glyph}</span>` : ""}</span>`;
    const peer = peerOf(m);
    if (peer && peer !== myId()) div.querySelector(".mesh-who").onclick = () => directTo(peer);
    div.querySelector(".mesh-meta").onclick = (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      const dest = m.dm ? peer : null;
      if (btn.dataset.react) {
        send({ t: "mesh_send", text: btn.dataset.react, ch: m.dm ? 0 : m.ch, to: dest, reply_to: m.id, emoji: true });
      } else if (btn.dataset.act === "reply") {
        replyTo = m;
        renderCompose();
        $("#mesh-text").focus();
      } else if (btn.dataset.act === "retry") {
        send({ t: "mesh_send", text: m.text, ch: m.ch, to: m.dm ? m.to : null, reply_to: m.reply_id || null });
      }
    };
    return div;
  }

  // Direct with no node picked: one line per conversation, newest first.
  function renderConversations(list) {
    const last = new Map();
    for (const m of messages) {
      if (!m.dm) continue;
      const p = peerOf(m);
      if (p && p !== myId()) last.set(p, m);
    }
    const convs = [...last].sort((a, b) => b[1].t - a[1].t);
    list.innerHTML = convs.length ? "" : `<div class="wl-empty">No direct messages (pick a node to message it)</div>`;
    for (const [p, m] of convs) {
      const b = document.createElement("button");
      b.className = "mesh-conv";
      b.dataset.peer = p;
      const n = unreadDm[p] || 0;
      b.innerHTML = `<b>${esc(short(p))}</b><span class="mesh-conv-last">${esc(m.mine ? "me: " : "")}${esc(isReaction(m) ? `reacted ${m.text}` : m.text)}</span>` +
        `<span class="wl-dim">${esc(ago(m.t))}</span>${n ? `<span class="mesh-unread">${n}</span>` : "<span></span>"}`;
      b.title = `${long(p)} ${p}`;
      b.onclick = () => directTo(p);
      list.append(b);
    }
  }

  function renderMessages() {
    const list = $("#mesh-list");
    if (view === "dm" && !dmTo) { renderConversations(list); return; }
    const shown = messages.filter((m) => viewOf(m) === view && !isReaction(m) && (view !== "dm" || peerOf(m) === dmTo));
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    list.innerHTML = shown.length ? "" : `<div class="wl-empty">No messages${view === "dm" ? ` with ${esc(short(dmTo))} yet` : ""}</div>`;
    for (const m of shown) list.append(row(m));
    if (atBottom || shown.length < 2) list.scrollTop = list.scrollHeight;
  }

  const unreadTotal = () => Object.values(unreadDm).reduce((a, b) => a + b, 0);

  function addMessage(m) {
    messages.push(m);
    if (messages.length > 300) messages.shift();
    const v = viewOf(m);
    const here = v === view && (v !== "dm" || (dmTo && peerOf(m) === dmTo));
    if (isReaction(m)) {
      const el = $(`#mesh-list [data-key="${byMsgId(m.reply_id)?.mine ? `t${byMsgId(m.reply_id).t}` : `i${m.reply_id}`}"]`);
      if (el) el.replaceWith(row(byMsgId(m.reply_id)));
      if (!m.mine && byMsgId(m.reply_id)?.mine) toast(`Mesh · ${short(m.from)} reacted ${m.text}`, "info");
      if (view === "dm" && !dmTo) renderMessages();
      return;
    }
    if (here) {
      renderMessages();
      $("#mesh-list").scrollTop = $("#mesh-list").scrollHeight;
    } else if (!m.mine) {
      if (v === "dm") { unreadDm[m.from] = (unreadDm[m.from] || 0) + 1; unread.dm = unreadTotal(); }
      else unread[v] = (unread[v] || 0) + 1;
      renderTabs();
      if (view === "dm" && !dmTo) renderMessages();
    }
    if (!m.mine && m.dm) toast(`Mesh · ${short(m.from)}: ${m.text}`, "info");
  }

  function updateMessage(m) {
    const i = messages.findIndex((x) => x.mine && x.t === m.t);
    if (i < 0) return;
    messages[i] = m;
    if (isReaction(m)) return;
    const el = $(`#mesh-list [data-key="t${m.t}"]`);
    if (el) el.replaceWith(row(m));
  }

  // ---------- compose

  function directTo(id) {
    if (dmTo !== id) replyTo = null;
    dmTo = id;
    view = "dm";
    unreadDm[id] = 0;
    unread.dm = unreadTotal();
    renderTabs();
    renderMessages();
    renderCompose();
    $("#mesh-text").focus();
  }

  function renderCompose() {
    const to = $("#mesh-to");
    // A reply belongs to the conversation it was started in.
    if (replyTo && (viewOf(replyTo) !== view || (view === "dm" && peerOf(replyTo) !== dmTo))) replyTo = null;
    let html = "";
    if (view === "dm") {
      html = dmTo
        ? `To <b>${esc(short(dmTo))}</b> <span class="wl-dim">${esc(long(dmTo))}</span> <button class="chip chip-mini" id="mesh-to-x" title="Back to the conversations">✕</button>`
        : `<span class="wl-dim">Pick a conversation, a node on the right, or a sender to message it</span>`;
    }
    if (replyTo) {
      html += `<span class="mesh-replying">↪ Replying to <b>${esc(replyTo.mine ? "me" : short(replyTo.from))}</b> ` +
        `<span class="wl-dim">${esc(replyTo.text.length > 50 ? `${replyTo.text.slice(0, 50)}…` : replyTo.text)}</span> ` +
        `<button class="chip chip-mini" id="mesh-reply-x" title="Not a reply">✕</button></span>`;
    }
    to.hidden = !html;
    to.innerHTML = html;
    const x = $("#mesh-to-x");
    if (x) x.onclick = () => { dmTo = null; replyTo = null; renderMessages(); renderCompose(); };
    const rx = $("#mesh-reply-x");
    if (rx) rx.onclick = () => { replyTo = null; renderCompose(); };
    count();
  }

  function count() {
    const n = bytes($("#mesh-text").value.trim());
    const c = $("#mesh-count");
    c.textContent = n > textMax * 0.75 ? `${n}/${textMax}` : "";
    c.classList.toggle("warn", n > textMax);
    $("#mesh-send").disabled = !connected || !n || n > textMax || (view === "dm" && !dmTo);
  }

  function submit(e) {
    e.preventDefault();
    const text = $("#mesh-text").value.trim();
    if (!text || $("#mesh-send").disabled) return;
    const dm = view === "dm";
    send({ t: "mesh_send", text, ch: dm ? 0 : view, to: dm ? dmTo : null, reply_to: replyTo?.id ?? null });
    $("#mesh-text").value = "";
    replyTo = null;
    renderCompose();
    count();
  }

  // ---------- nodes

  const when = (t) => (ago(t) === "now" ? "just now" : `${ago(t)} ago`);
  const hopsText = (n) => (n.hops == null ? "" : n.hops === 0 ? "direct" : `${n.hops} hop${n.hops > 1 ? "s" : ""}`);

  function renderNodes() {
    const q = $("#mesh-find").value.trim().toLowerCase();
    const now = Date.now() / 1000;
    const list = nodes.filter((n) => !n.me)
      .filter((n) => !recentOnly || q || (n.last_heard && now - n.last_heard < HEARD_RECENT_S) || n.fav)
      .filter((n) => !q || [n.shortName, n.longName, n.id, n.hwModel, n.role].some((s) => String(s || "").toLowerCase().includes(q)))
      .sort((a, b) => (b.fav - a.fav) || ((b.last_heard || 0) - (a.last_heard || 0)));
    const el = $("#mesh-nodes");
    el.innerHTML = list.length ? "" : `<div class="wl-empty">${nodes.length ? "No nodes heard in the last day" : "No nodes yet"}</div>`;
    for (const n of list) {
      const b = document.createElement("button");
      const live = n.last_heard && now - n.last_heard < LIVE_S;
      b.className = "mesh-node" + (n.id === selected ? " on" : "") + (n.ignored ? " ignored" : "");
      b.dataset.id = n.id;
      b.title = `${n.longName || ""} ${n.id || ""} · ${n.hwModel || ""}${n.role ? ` · ${n.role}` : ""} — click for details and traceroute`;
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
        if (connected) send({ t: "mesh_favorite", to: n.id, fav: !n.fav });
      };
      el.append(b);
    }
    const all = nodes.filter((n) => !n.me).length;
    $("#mesh-count-nodes").textContent = recentOnly && !q ? `${list.length} of ${all}` : `${all} known`;
    renderCard();
  }

  // ---------- a node's card: details, message, traceroute, favorite

  // NeighborInfo as map links: every pair heard; or, with a node selected,
  // just its own, each saying which way: green = it is heard by that node
  // (that node's list has it), blue = it hears that node, purple = both.
  function links() {
    const snr = (a, b) => (neighbors[a]?.neighbors || []).find((x) => x.id === b)?.snr;
    const hears = (a, b) => (neighbors[a]?.neighbors || []).some((x) => x.id === b);
    const db = (v) => (v != null ? `${v} dB` : "?");
    const out = [], seen = new Set();
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
    return out;
  }
  const legend = () => (selected ? `${short(selected)}: green hears it, blue it hears, purple both` : "");
  const drawLinks = () => map.setLinks(links(), legend());

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
  // position, and the ranch's nodes share one.
  function offMap(a, b) {
    const pa = byId(a), pb = byId(b);
    const has = (x) => x && x.lat != null && (x.lat || x.lon);
    if (!pb) return "not in this node's list";
    if (!has(pb)) return "no position";
    if (has(pa) && pa.lat === pb.lat && pa.lon === pb.lon) return "same spot";
    return "";
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

  function renderCard() {
    const card = $("#mesh-card");
    const n = byId(selected);
    card.hidden = !n;
    if (!n) return;
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
      ["ignored", n.ignored ? "yes: its packets are dropped" : ""]].filter(([, v]) => v);
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
      `<div class="wl-row"><button class="chip chip-mini" data-act="dm">Message</button>` +
      `<button class="chip chip-mini" data-act="trace"${!connected || tr?.state === "running" ? " disabled" : ""} ` +
      `title="Ask the mesh for the route to this node and back, with each hop's SNR">Traceroute</button>` +
      `<button class="chip chip-mini" data-act="fav"${connected ? "" : " disabled"}>${n.fav ? "★ Unfavorite" : "☆ Favorite"}</button>` +
      `<button class="chip chip-mini" data-act="ignore"${connected ? "" : " disabled"} title="${n.ignored
        ? "Hear this node again" : "Drop this node's packets: it disappears from messages and the map"}">${n.ignored ? "Unignore" : "Ignore"}</button>` +
      `<button class="chip chip-mini" data-act="remove"${connected ? "" : " disabled"} title="Forget this node; it comes back when it is next heard">Remove</button></div>` +
      `<div class="wl-row mesh-reqs"><span class="wl-dim">Ask for</span>` +
      REQUESTS.map(([k, label, why]) => `<button class="chip chip-mini" data-req="${k}" title="${esc(why)}"${!connected || busy ? " disabled" : ""}>${label}</button>`).join("") +
      (reqNote ? `<span class="wl-dim mesh-req-note">${esc(reqNote)}</span>` : "") + "</div>" +
      neighboursHtml(n) + traceHtml(tr);
    card.onclick = (e) => {
      const go = e.target.closest("[data-go]")?.dataset.go;
      if (go) { if (byId(go)) select(go); return; }
      const act = e.target.closest("button")?.dataset.act;
      if (act === "close") select(n.id);
      else if (act === "dm") directTo(n.id);
      else if (act === "trace") send({ t: "mesh_trace", to: n.id });
      else if (act === "fav") send({ t: "mesh_favorite", to: n.id, fav: !n.fav });
      else if (act === "ignore") send({ t: "mesh_node", to: n.id, action: n.ignored ? "unignore" : "ignore" });
      else if (act === "remove" && confirm(`Remove ${n.shortName || n.id} from the node's list? It comes back when it is next heard.`)) {
        send({ t: "mesh_node", to: n.id, action: "remove" });
        select(n.id);
      }
      const req = e.target.closest("button")?.dataset.req;
      if (req) send({ t: "mesh_request", to: n.id, kind: req });
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
    send({ t: "mesh_config", section: cfgSec, values: { ...e } });
    $("#mesh-cfg-note").textContent = "Saving… (the node may restart)";
  }

  function setConfig(c) {
    cfg = c;
    for (const [s, e] of Object.entries(edits)) {          // saved, or changed elsewhere to the same
      for (const f of Object.keys(e)) if (e[f] === cfg?.values[s]?.[f]) delete e[f];
    }
    renderCfg();
  }

  $("#mesh-cfg-btn").onclick = () => {
    cfgOpen = !cfgOpen;
    on($("#mesh-cfg-btn"), cfgOpen);
    $("#mesh-cfg").hidden = !cfgOpen;
    $("#mesh-main").hidden = cfgOpen;
    if (cfgOpen) renderCfg();
  };
  $("#mesh-cfg-form").onsubmit = (e) => e.preventDefault();
  $("#mesh-cfg-save").onclick = saveCfg;
  $("#mesh-cfg-revert").onclick = () => { delete edits[cfgSec]; renderCfg(); };
  $("#mesh-reboot").onclick = () => {
    if (connected && confirm("Reboot the node? The link drops for a few seconds and comes back on its own.")) send({ t: "mesh_reboot" });
  };

  // ---------- from app.js

  function hello(snap, cfg) {
    $("#mesh").hidden = !cfg?.mesh;
    if (!cfg?.mesh || !snap) return;
    info = snap.info;
    nodes = snap.nodes || [];
    messages = snap.messages || [];
    switchedOff = !!snap.off;
    connected = !!snap.status?.connected;
    linkError = snap.status?.error || null;
    textMax = snap.text_max || textMax;
    setConfig(snap.config || null);
    for (const tr of snap.traces || []) traces[tr.to] = tr;
    requests = snap.requests || {};
    neighbors = snap.neighbors || {};
    hears = snap.hears || [];
    map.setNodes(nodes, myId());
    drawLinks();
    renderHead();
    renderHears();
    renderTabs();
    renderMessages();
    renderCompose();
    renderNodes();
  }

  function event(m) {
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
      map.setNodes(nodes, myId());
      renderHead();
      renderTabs();
      renderMessages();
      renderCompose();
    }
    if (m.config) setConfig(m.config);
    if (m.nodes) {
      nodes = m.nodes;
      map.setNodes(nodes, myId());
      drawLinks();
      renderNodes();
      renderHead();
      renderHears();
    }
    if (m.neighbors) {
      neighbors = m.neighbors;
      drawLinks();
      renderCard();
    }
    if (m.hears) { hears = m.hears; renderHears(); }
    if (m.status) renderHears();
    if (m.request) onRequest(m.request);
    if (m.trace) onTrace(m.trace);
    if (m.rx) map.pulse(m.rx.from);
    if (m.msg) addMessage(m.msg);
    if (m.update) updateMessage(m.update);
  }

  $("#mesh-form").onsubmit = submit;
  $("#mesh-text").oninput = count;
  $("#mesh-find").oninput = renderNodes;
  $("#mesh-all").onclick = () => { recentOnly = !recentOnly; on($("#mesh-all"), recentOnly); renderNodes(); };
  setInterval(() => { renderNodes(); renderHears(); map.render(); }, 60000);  // keep "heard 5m ago" and the map's colours current

  // {"t": "addon", "addon": "mesh", "on", "error", "snapshot"}: switched on or off
  function power(m) {
    if (m.snapshot) { hello(m.snapshot, { mesh: true }); }
    else { switchedOff = !m.on; connected = false; linkError = m.error || null; renderHead(); }
    if (m.error) { linkError = m.error; renderHead(); }
    renderCompose();
    renderNodes();
  }

  return { hello, event, power, connected: () => connected, _nodes: () => nodes };
})();
