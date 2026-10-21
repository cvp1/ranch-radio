"use strict";

// Mesh panel: a Meshtastic node's channels, direct messages, node list (a
// node's card: message it, traceroute, favorite), the map (meshmap.js) and
// settings. Everything goes over the control WebSocket ({"t": "mesh"} in,
// mesh_send / mesh_config / mesh_favorite / mesh_trace out); the server keeps the history,
// so a page opened later sees it too.
// Uses app.js's $, send, toast and on.

const mesh = (() => {
  const HEARD_RECENT_S = 24 * 3600;
  const ACK = { sending: ["…", "Sending"], sent: ["✓?", "Sent, waiting for an ACK"],
    relayed: ["✓", "A neighbour rebroadcast it"], delivered: ["✓✓", "Delivered"],
    failed: ["✗", "Not delivered"], unknown: ["?", "ACK unknown (the server or node restarted)"] };
  let info = null, nodes = [], messages = [], connected = false, linkError = null, textMax = 200;
  let switchedOff = false;  // the Mesh add-on is off: the bridge is stopped and the node's port free
  let view = null;          // channel index, or "dm"
  let dmTo = null;          // node id for a direct message
  let recentOnly = false;    // the list shows every node the radio knows unless "Recent"
  let selected = null;      // node id whose card is open
  const traces = {};        // node id -> the last traceroute to it
  const map = meshMap.create($("#mesh-map"), { onSelect: (id) => select(id, true) });
  const LIVE_S = 15 * 60;
  const unread = {};
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

  function renderTabs() {
    const seg = $("#mesh-tabs");
    seg.innerHTML = "";
    const tabs = (info?.channels || []).map((c) => [c.index, c.name]);
    tabs.push(["dm", "Direct"]);
    if (view === null || !tabs.some(([v]) => v === view)) view = tabs.length > 1 ? tabs[0][0] : "dm";
    for (const [v, label] of tabs) {
      const b = document.createElement("button");
      b.className = "chip" + (v === view ? " on" : "");
      b.innerHTML = esc(label) + (unread[v] ? ` <span class="mesh-unread">${unread[v]}</span>` : "");
      b.onclick = () => { view = v; unread[v] = 0; renderTabs(); renderMessages(); renderCompose(); };
      seg.append(b);
    }
  }

  // ---------- messages

  function row(m) {
    const div = document.createElement("div");
    div.className = "mesh-msg" + (m.mine ? " mine" : "");
    div.dataset.key = m.mine ? `t${m.t}` : `i${m.id}`;
    const who = m.mine ? (m.dm ? `→ ${short(m.to)}` : "me")
      : (m.dm ? `${short(m.from)} → me` : short(m.from));
    const [glyph, why] = m.mine ? (ACK[m.ack] || ["", ""]) : ["", ""];
    const meta = m.mine ? "" : [m.hops != null ? `${m.hops} hop${m.hops === 1 ? "" : "s"}` : "",
      m.snr != null ? `SNR ${m.snr}` : ""].filter(Boolean).join(" · ");
    div.innerHTML = `<span class="mesh-time">${hhmm(m.t)}</span>` +
      `<button class="mesh-who" title="${esc(m.mine ? "" : `${long(m.from)} ${m.from || ""}`)}">${esc(who)}</button>` +
      `<span class="mesh-text">${esc(m.text)}</span>` +
      `<span class="mesh-meta">${esc(meta)}${m.mine ? `<span class="mesh-ack ${esc(m.ack)}" title="${esc(why + (m.why ? `: ${m.why}` : ""))}">${glyph}</span>` : ""}</span>`;
    const peer = m.mine ? m.to : m.from;
    if (peer && peer !== myId()) div.querySelector(".mesh-who").onclick = () => directTo(peer);
    return div;
  }

  function renderMessages() {
    const list = $("#mesh-list");
    const shown = messages.filter((m) => viewOf(m) === view);
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    list.innerHTML = shown.length ? "" : `<div class="wl-empty">No messages${view === "dm" ? " (pick a node to message it)" : ""}</div>`;
    for (const m of shown) list.append(row(m));
    if (atBottom || shown.length < 2) list.scrollTop = list.scrollHeight;
  }

  function addMessage(m) {
    messages.push(m);
    if (messages.length > 300) messages.shift();
    const v = viewOf(m);
    if (v === view) {
      renderMessages();
      $("#mesh-list").scrollTop = $("#mesh-list").scrollHeight;
    } else if (!m.mine) {
      unread[v] = (unread[v] || 0) + 1;
      renderTabs();
    }
    if (!m.mine && m.dm) toast(`Mesh · ${short(m.from)}: ${m.text}`, "info");
  }

  function updateMessage(m) {
    const i = messages.findIndex((x) => x.mine && x.t === m.t);
    if (i < 0) return;
    messages[i] = m;
    const el = $(`#mesh-list [data-key="t${m.t}"]`);
    if (el) el.replaceWith(row(m));
  }

  // ---------- compose

  function directTo(id) {
    dmTo = id;
    view = "dm";
    unread.dm = 0;
    renderTabs();
    renderMessages();
    renderCompose();
    $("#mesh-text").focus();
  }

  function renderCompose() {
    const to = $("#mesh-to");
    if (view === "dm") {
      to.hidden = false;
      to.innerHTML = dmTo
        ? `To <b>${esc(short(dmTo))}</b> <span class="wl-dim">${esc(long(dmTo))}</span> <button class="chip chip-mini" id="mesh-to-x">✕</button>`
        : `<span class="wl-dim">Pick a node on the right (or a sender) to message it</span>`;
      const x = $("#mesh-to-x");
      if (x) x.onclick = () => { dmTo = null; renderCompose(); };
    } else {
      to.hidden = true;
    }
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
    send({ t: "mesh_send", text, ch: dm ? 0 : view, to: dm ? dmTo : null });
    $("#mesh-text").value = "";
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
      b.className = "mesh-node" + (n.id === selected ? " on" : "");
      b.dataset.id = n.id;
      b.title = `${n.longName || ""} ${n.id || ""} · ${n.hwModel || ""}${n.role ? ` · ${n.role}` : ""} — click for details and traceroute`;
      b.innerHTML = `<span class="mesh-short"><span class="mesh-fav${n.fav ? " on" : ""}" title="${n.fav
        ? "Favorite: click to remove" : "Make a favorite (a CLIENT_BASE node always relays for its favorites)"}">${n.fav ? "★" : "☆"}</span>${esc(n.shortName || "?")}</span>` +
        `<span class="mesh-long">${esc(n.longName || n.id)}<span class="mesh-sub">${esc([n.role && n.role !== "CLIENT" ? n.role : "",
          n.lat != null && (n.lat || n.lon) ? "📍" : "", n.mqtt ? "MQTT" : ""].filter(Boolean).join(" "))}</span></span>` +
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

  function select(id, fromMap = false) {
    selected = selected === id && !fromMap ? null : id;
    map.select(selected);
    map.setTrace(traces[selected] || null);
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

  function renderCard() {
    const card = $("#mesh-card");
    const n = byId(selected);
    card.hidden = !n;
    if (!n) return;
    const tr = traces[n.id];
    const fields = [["hops", hopsText(n)], ["SNR", n.snr != null ? `${n.snr} dB` : ""],
      ["battery", n.battery != null ? (n.battery > 100 ? "powered" : `${n.battery}%`) : ""],
      ["voltage", n.voltage != null ? `${n.voltage.toFixed(2)} V` : ""],
      ["ch. util", n.util != null ? `${n.util.toFixed(1)}%` : ""], ["heard", n.last_heard ? when(n.last_heard) : "never"],
      ["position", n.lat != null && (n.lat || n.lon) ? `${n.lat.toFixed(4)}, ${n.lon.toFixed(4)}` : "none"],
      ["via", n.mqtt ? "MQTT" : ""]].filter(([, v]) => v);
    card.innerHTML = `<div class="mesh-card-head"><b>${esc(n.shortName || "?")}</b><span>${esc(n.longName || "")}</span>` +
      `<span class="wl-dim">${esc(n.id)} · ${esc(n.hwModel || "")}${n.role ? ` · ${esc(n.role)}` : ""}</span>` +
      `<span class="spacer"></span><button class="chip chip-mini" data-act="close" title="Close">✕</button></div>` +
      `<div class="mesh-card-grid">${fields.map(([k, v]) => `<span>${k} <b>${esc(v)}</b></span>`).join("")}</div>` +
      `<div class="wl-row"><button class="chip chip-mini" data-act="dm">Message</button>` +
      `<button class="chip chip-mini" data-act="trace"${!connected || tr?.state === "running" ? " disabled" : ""} ` +
      `title="Ask the mesh for the route to this node and back, with each hop's SNR">Traceroute</button>` +
      `<button class="chip chip-mini" data-act="fav"${connected ? "" : " disabled"}>${n.fav ? "★ Unfavorite" : "☆ Favorite"}</button></div>` +
      traceHtml(tr);
    card.onclick = (e) => {
      const act = e.target.closest("button")?.dataset.act;
      if (act === "close") select(n.id);
      else if (act === "dm") directTo(n.id);
      else if (act === "trace") send({ t: "mesh_trace", to: n.id });
      else if (act === "fav") send({ t: "mesh_favorite", to: n.id, fav: !n.fav });
    };
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
    map.setNodes(nodes, myId());
    renderHead();
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
      renderNodes();
    }
    if (m.trace) onTrace(m.trace);
    if (m.rx) map.pulse(m.rx.from);
    if (m.msg) addMessage(m.msg);
    if (m.update) updateMessage(m.update);
  }

  $("#mesh-form").onsubmit = submit;
  $("#mesh-text").oninput = count;
  $("#mesh-find").oninput = renderNodes;
  $("#mesh-all").onclick = () => { recentOnly = !recentOnly; on($("#mesh-all"), recentOnly); renderNodes(); };
  setInterval(() => { renderNodes(); map.render(); }, 60000);  // keep "heard 5m ago" and the map's colours current

  // {"t": "addon", "addon": "mesh", "on", "error", "snapshot"}: switched on or off
  function power(m) {
    if (m.snapshot) { hello(m.snapshot, { mesh: true }); }
    else { switchedOff = !m.on; connected = false; linkError = m.error || null; renderHead(); }
    if (m.error) { linkError = m.error; renderHead(); }
    renderCompose();
    renderNodes();
  }

  return { hello, event, power, connected: () => connected };
})();
