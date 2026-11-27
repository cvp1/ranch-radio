"use strict";

// The HaLow panel: an OpenWrt Wi-Fi HaLow bridge (addons/halow.py). Shows its
// radios and networks, every station on them, the box's health, its DHCP guard,
// services and log. Every change is two steps: the page asks for a plan, the
// server answers with the exact command and what it will do, and only the
// Confirm button in that dialog runs it. A plan not confirmed expires on the
// server. Uses common.js (esc, toast).
//
// Above the tables, a map (meshmap.js, as on the Mesh tab; 2026-11-27: "a map
// visualization for all the nodes we can see and what their signal strength
// is"): the bridge, and every station it has had joined, at its place from the
// host file (one with no place is drawn beside the bridge, and says so). Each
// joined station's line to the bridge says which way it is proven, in the Mesh
// map's colours: green it hears the bridge (its ACKs came back), blue the
// bridge hears it (a measured signal), purple both; beside its name, the dBm
// the bridge hears it at. The bridge says how many hear it. How strongly a
// station hears the bridge is not measured from the bridge, so the map never
// shows a number for it. A station that has left keeps its place, fading.

(() => {
  let body = null, main = null, map = null, snap = {}, api = null, plan = null, last = null;
  const BRIDGE = "bridge";

  const dbm = (v) => (v == null ? "" : `${v} dBm`);
  const sigCls = (v) => (v == null ? "" : v >= -65 ? "hl-good" : v >= -78 ? "hl-fair" : "hl-poor");
  const dur = (s) => {
    if (s == null) return "";
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  };
  const kb = (n) => (n == null ? "?" : n >= 1048576 ? `${(n / 1048576).toFixed(1)} GB` : n >= 1024 ? `${(n / 1024).toFixed(1)} MB` : `${n} kB`);

  function ask(what, extra = {}) { api.send("plan", { what, ...extra }); }

  function editBtn(section, option, value, label) {
    return `<button class="chip chip-mini hl-edit" data-s="${esc(section)}" data-o="${esc(option)}" data-v="${esc(value)}" title="Change ${esc(option)}">${esc(label ?? value ?? "")} ✎</button>`;
  }
  function toggleBtn(section, disabled) {
    return `<button class="chip chip-mini hl-toggle" data-s="${esc(section)}" data-v="${disabled ? "0" : "1"}">${disabled ? "off — switch on" : "on — switch off"}</button>`;
  }

  function renderPlan() {
    if (!plan) return "";
    return `<div class="hl-plan" role="dialog" aria-label="Confirm change">
        <div class="hl-plan-title">${esc(plan.title)}</div>
        ${plan.warn ? `<div class="hl-warn">${esc(plan.warn)}</div>` : ""}
        <div class="hl-plan-cmd-label">This exact command runs on the bridge:</div>
        <pre class="hl-cmd">${esc(plan.cmd)}</pre>
        <div class="hl-plan-btns"><button class="chip hl-confirm">Confirm</button>
          <button class="chip hl-cancel">Cancel</button>
          <span class="hl-ttl">expires in ${Math.max(0, Math.round((plan.expires - Date.now()) / 1000))}s</span></div>
      </div>`;
  }

  // ---------- the map

  const DIRS = { both: "both ways", in: "it hears the bridge", out: "the bridge hears it" };
  const sigOf = (x) => x.signal_avg ?? x.signal;
  const badgeCls = (v) => (v == null ? "" : v >= -65 ? "h0" : v >= -78 ? "h2" : "h3");
  const nameOf = (x) => (snap.places || {})[x.mac]?.name || x.name || x.ip || x.mac.slice(-5);
  const ago = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? "just now" : `${dur(s)} ago`; };

  // Every station the bridge has had joined (snap.seen), the joined ones from this read on top.
  function stationsOnMap() {
    const now = new Map((snap.stations || []).map((x) => [x.mac, x]));
    const out = new Map();
    for (const r of snap.seen || []) out.set(r.mac, { ...r, live: false });
    for (const [mac, x] of now) {
      out.set(mac, { ...(out.get(mac) || {}), ...x, live: true,
        last: snap.at ? snap.at - (x.inactive_ms || 0) / 1000 : out.get(mac)?.last });
    }
    return [...out.values()];
  }

  function why(x) {
    const v = sigOf(x);
    return [x.dir === "out" || x.dir === "both" ? `the bridge hears it at ${dbm(v)}${x.signal_avg != null && x.signal != null ? ` (now ${x.signal})` : ""}` : "",
      x.dir === "in" || x.dir === "both" ? "it hears the bridge (its ACKs come back; how strongly isn't measured from the bridge)" : ""]
      .filter(Boolean).join("; ");
  }

  function mapNodes() {
    const pos = snap.position, places = snap.places || {}, list = stationsOnMap();
    const heardBy = list.filter((x) => x.live && (x.dir === "in" || x.dir === "both")).length;
    const h = snap.host || {};
    const nodes = [{ id: BRIDGE, me: true, shortName: h.hostname || "bridge", longName: h.model || "",
      lat: pos?.lat, lon: pos?.lon, last_heard: snap.at || 0,
      badge: snap.at ? `heard by ${heardBy}` : "", badgeCls: heardBy ? "h0" : "",
      tip: `${h.hostname || snap.target} ${h.model || ""}\nthe HaLow bridge · ${(snap.stations || []).length} joined · ` +
        `heard by ${heardBy} (their ACKs; how strongly isn't measured from the bridge)` + (pos ? "" : "\nno position set (the halow block's position)") }];
    for (const x of list) {
      const p = places[x.mac], v = sigOf(x), name = nameOf(x);
      const at = p || pos;
      nodes.push({ id: x.mac, shortName: name, longName: x.mac, lat: at?.lat, lon: at?.lon, last_heard: x.last || 0,
        ring: x.live ? x.dir || "" : "", kind: p ? "" : "unplaced", pin: !!x.live,
        badge: v != null ? (x.live ? dbm(v) : `${dbm(v)} last`) : "", badgeCls: x.live ? badgeCls(v) : "",
        tip: [`${name} ${x.mac}${x.ip ? ` · ${x.ip}` : ""}`,
          x.live ? `joined (${x.ifname || ""}) ${dur(x.connected_s)}` : `left; last heard ${x.last ? ago(x.last) : "?"}`,
          x.live ? why(x) : v != null ? `the bridge last heard it at ${dbm(v)}` : "",
          p ? "" : "no place set: drawn beside the bridge (the halow block's places)"].filter(Boolean).join("\n") });
    }
    return nodes;
  }

  function mapLinks() {
    return stationsOnMap().filter((x) => x.live && x.dir).map((x) => {
      const v = sigOf(x);
      return { from: BRIDGE, to: x.mac, cls: x.dir, label: x.dir === "in" ? "ACKs" : dbm(v),
        title: `${nameOf(x)}: ${why(x)}` };
    });
  }

  function popupHtml(id) {
    if (id === BRIDGE) {
      const h = snap.host || {};
      return `<div class="mm-pop-h"><b>${esc(h.hostname || snap.target || "bridge")}</b> ${esc(h.model || "")}</div>` +
        `<div class="wl-dim">the HaLow bridge · ${esc(snap.target || "")}</div>`;
    }
    const x = stationsOnMap().find((s) => s.mac === id);
    if (!x) return null;
    const p = (snap.places || {})[x.mac], v = sigOf(x);
    const row = (k, val) => (val ? `<div class="mm-pop-r"><span>${k}</span><b>${esc(val)}</b></div>` : "");
    return `<div class="mm-pop-h"><b>${esc(nameOf(x))}</b></div>` +
      `<div class="wl-dim mm-pop-id">${esc(x.mac)}${x.ip ? ` · ${esc(x.ip)}` : ""}</div>` +
      row("bridge hears it", x.dir === "out" || x.dir === "both" ? dbm(v) : !x.live && v != null ? `${dbm(v)} (last)` : "") +
      row("it hears the bridge", x.live && (x.dir === "in" || x.dir === "both") ? "yes, its ACKs (not measured)" : "") +
      row("link", x.live ? DIRS[x.dir] || "not proven either way" : "") +
      row("rates", x.live ? [x.tx_rate_text && `TX ${x.tx_rate_text}`, x.rx_rate_text && `RX ${x.rx_rate_text}`].filter(Boolean).join(" · ") : "") +
      row("retries", x.live && x.tx_retries != null ? `${x.tx_retries}${x.tx_failed ? `, ${x.tx_failed} failed` : ""}` : "") +
      row(x.live ? "joined" : "left", x.live ? dur(x.connected_s) : x.last ? `last heard ${ago(x.last)}` : "") +
      row("place", p ? "" : "not set (drawn beside the bridge)") +
      (x.live && x.ip ? `<div class="mm-pop-acts"><button class="chip chip-mini" data-act="ping">Ping from the bridge</button></div>` : "");
  }

  function drawMap() {
    if (!map) return;
    map.setNodes(mapNodes(), BRIDGE);
    const n = (snap.stations || []).length;
    map.setLinks(mapLinks(), `green hears the bridge, blue the bridge hears it, purple both · beside a name: how the bridge hears it` +
      (n ? "" : " · nothing joined right now"));
    map.refreshPopup();
  }

  function mountMap() {
    const el = body.querySelector(".hl-map");
    if (!el || typeof meshMap === "undefined") { el?.remove(); return; }
    map = meshMap.create(el, { onSelect: (id) => map.select(id), rings: true, labels: true, popup: popupHtml,
      fanNote: (names) => `${names.join(", ")}: no place set for some of these, so they are drawn around the bridge`,
      onPopup: (id, act) => {
        const x = stationsOnMap().find((s) => s.mac === id);
        if (act === "ping" && x?.ip) { api.send("ping", { ip: x.ip }); toast(`HaLow: pinging ${x.ip} from the bridge…`); }
      } });
  }

  function render() {
    if (!body) return;
    if (!main) {
      body.innerHTML = `<div class="halow"><div class="mesh-map hl-map"></div><div class="hl-main"></div></div>`;
      main = body.querySelector(".hl-main");
      mountMap();
    }
    drawMap();
    const s = snap;
    if (!s.target) { main.innerHTML = `<div class="st-empty">Waiting for the bridge…</div>`; return; }
    if (!s.at) {
      main.innerHTML = `${renderPlan()}<div class="hl-bad">${esc(s.error || "Reading the bridge…")}</div>`;
      wire();
      return;
    }
    const h = s.host || {}, mem = h.mem_kb, ov = (h.disk_kb || {})["/overlay"];
    const age = Math.round(Date.now() / 1000 - s.at);
    const serving = (s.dhcp || {}).serving || [];
    const head = `<div class="hl-bar stat">
        <span><b>${esc(h.hostname || s.target)}</b> ${esc(h.model || "")}</span>
        <span>${esc(h.release || "")}</span>
        <span>up ${dur(h.uptime_s)}</span>
        ${h.load ? `<span>load ${h.load.map((x) => x.toFixed(2)).join(" ")}</span>` : ""}
        ${mem ? `<span>mem ${kb(mem.available)} free of ${kb(mem.total)}</span>` : ""}
        ${ov ? `<span class="${ov.free < 1024 ? "hl-bad" : ""}">flash ${kb(ov.free)} free</span>` : ""}
        ${h.clock_skew_s != null && Math.abs(h.clock_skew_s) > 300 ? `<span class="hl-bad" title="The bridge's clock is off, so its log times are wrong. It needs a reachable NTP server.">clock ${Math.abs(h.clock_skew_s) > 86400 ? Math.round(Math.abs(h.clock_skew_s) / 86400) + " days" : Math.round(Math.abs(h.clock_skew_s) / 60) + " min"} ${h.clock_skew_s < 0 ? "behind" : "ahead"}</span>` : ""}
        <span class="${s.stale ? "hl-bad" : ""}">${s.stale ? "stale: " + esc(s.error) : `read ${age}s ago`}</span>
        <button class="chip chip-mini hl-refresh">Refresh</button>
        <button class="chip chip-mini hl-reboot">Reboot…</button>
      </div>`;
    const guard = serving.length
      ? `<div class="hl-alarm">The bridge is handing out addresses on the LAN (${esc(serving.join(", "))}), racing the router's DHCP.
           <button class="chip chip-mini hl-dhcp">Stop its DHCP…</button></div>`
      : `<div class="hl-ok-line">DHCP guard: every pool on the bridge is off; the router is the only DHCP server.</div>`;
    const radios = (s.radios || []).map((r) => `<tr>
        <td><b>${esc(r.section)}</b>${r.halow ? ` <span class="hl-tag">HaLow</span>` : ""}</td>
        <td>${esc(r.type)} ${esc(r.hwmode || r.htmode)}</td>
        <td>${editBtn(r.section, "channel", r.channel)}</td>
        <td>${editBtn(r.section, "txpower", r.txpower, r.txpower ? `${r.txpower} dBm` : "default")}</td>
        <td>${esc(r.country)}</td><td>${toggleBtn(r.section, r.disabled)}</td></tr>`).join("");
    const ifaces = (s.ifaces || []).map((i) => `<tr>
        <td>${editBtn(i.section, "ssid", i.ssid)}</td><td>${esc(i.device)}</td><td>${esc(i.mode)}</td>
        <td>${esc(i.ifname)}${i.channel ? ` ch ${i.channel}` : ""}${i.freq ? ` (${i.freq} MHz)` : ""}</td>
        <td>${esc(i.encryption)}</td><td>${esc(i.network)}</td>
        <td><button class="chip chip-mini hl-hidden" data-s="${esc(i.section)}" data-v="${i.hidden ? "0" : "1"}">${i.hidden ? "hidden" : "visible"}</button></td>
        <td>${toggleBtn(i.section, i.disabled)}</td></tr>`).join("");
    const st = (s.stations || []).map((x) => `<tr>
        <td class="hl-mac">${esc(x.mac)}</td><td>${esc(x.name)}</td><td>${esc(x.ip)}</td><td>${esc(x.ifname)}</td>
        <td class="${sigCls(x.signal)}">${dbm(x.signal)}${x.signal_avg != null ? ` (avg ${x.signal_avg})` : ""}</td>
        <td>${esc(x.tx_rate_text || "")}</td><td>${esc(x.rx_rate_text || "")}</td>
        <td class="hl-num">${x.tx_retries ?? ""}${x.tx_failed ? ` / ${x.tx_failed} failed` : ""}</td>
        <td>${dur(x.connected_s)}</td>
        <td>${x.ip ? `<button class="chip chip-mini hl-ping" data-ip="${esc(x.ip)}">ping</button>` : ""}
            <button class="chip chip-mini hl-kick" data-if="${esc(x.ifname)}" data-mac="${esc(x.mac)}">disconnect…</button></td></tr>`).join("");
    const svcs = (s.services || []).map((v) => `<span class="hl-svc ${v.running ? "hl-good" : "hl-poor"}">${esc(v.name)} ${v.running ? "running" : "stopped"}${v.enabled ? "" : " (not at boot)"}
        <button class="chip chip-mini hl-svc-btn" data-n="${esc(v.name)}" data-a="${v.running ? "restart" : "start"}">${v.running ? "restart" : "start"}…</button>
        ${v.running ? `<button class="chip chip-mini hl-svc-btn" data-n="${esc(v.name)}" data-a="stop">stop…</button>` : ""}
        <button class="chip chip-mini hl-svc-btn" data-n="${esc(v.name)}" data-a="${v.enabled ? "disable" : "enable"}">${v.enabled ? "off at boot…" : "on at boot…"}</button></span>`).join("");
    const outb = (s.outbound || []).map((c) => `<tr><td>${esc(c.proto)}</td><td>${esc(c.remote)}</td><td>${esc(c.state)}</td><td>${esc(c.local)}</td></tr>`).join("");
    main.innerHTML = `${renderPlan()}${head}${guard}
      ${last ? `<div class="${last.ok ? "hl-ok-line" : "hl-alarm"}">${esc(last.title)}: ${last.ok ? "done" : "failed"}
          ${last.undo ? `<button class="chip chip-mini hl-undo">Undo…</button>` : ""}
          ${last.out ? `<pre class="hl-cmd">${esc(last.out)}</pre>` : ""}</div>` : ""}
      <h4>Radios</h4><table class="hl-t"><tr><th>Radio</th><th>Type</th><th>Channel</th><th>Power</th><th>Country</th><th></th></tr>${radios}</table>
      <h4>Networks</h4><table class="hl-t"><tr><th>Name</th><th>Radio</th><th>Mode</th><th>Interface</th><th>Security</th><th>Bridge</th><th></th><th></th></tr>${ifaces}</table>
      <h4>Stations (${(s.stations || []).length})</h4>
      ${st ? `<table class="hl-t"><tr><th>MAC</th><th>Name</th><th>Address</th><th>On</th><th>Signal</th><th>TX</th><th>RX</th><th>Retries</th><th>Joined</th><th></th></tr>${st}</table>`
           : `<div class="st-empty">Nothing is joined to the bridge.</div>`}
      <h4>Services</h4><div class="hl-svcs">${svcs || "none found"}</div>
      <h4>Outbound (off the LAN)</h4>
      ${outb ? `<table class="hl-t"><tr><th>Proto</th><th>Remote</th><th>State</th><th>Local</th></tr>${outb}</table>`
             : `<div class="hl-ok-line">No connections from the bridge to anywhere off the LAN right now.</div>`}
      <details><summary>Log (last ${(s.log || []).length} lines)</summary><pre class="hl-log">${esc((s.log || []).join("\n"))}</pre></details>`;
    wire();
  }

  function wire() {
    const q = (sel, fn) => { for (const el of main.querySelectorAll(sel)) el.onclick = () => fn(el.dataset, el); };
    q(".hl-refresh", () => api.send("refresh"));
    q(".hl-reboot", () => ask("reboot"));
    q(".hl-dhcp", () => ask("dhcp_off"));
    q(".hl-edit", (d) => {
      const v = prompt(`New ${d.o} for ${d.s}:`, d.v);
      if (v != null && v !== d.v) ask("set", { section: d.s, option: d.o, value: v });
    });
    q(".hl-toggle", (d) => ask("set", { section: d.s, option: "disabled", value: d.v }));
    q(".hl-hidden", (d) => ask("set", { section: d.s, option: "hidden", value: d.v }));
    q(".hl-kick", (d) => ask("kick", { ifname: d.if, mac: d.mac }));
    q(".hl-svc-btn", (d) => ask("service", { service: d.n, action: d.a }));
    q(".hl-ping", (d) => { api.send("ping", { ip: d.ip }); toast(`HaLow: pinging ${d.ip} from the bridge…`); });
    q(".hl-undo", () => { const u = last.undo; last = null; ask("set", { section: u.section, option: u.option, value: u.value }); });
    q(".hl-confirm", () => { const id = plan.id; plan = null; api.send("apply", { id }); render(); });
    q(".hl-cancel", () => { api.send("cancel", { id: plan.id }); plan = null; render(); });
  }

  // Only the countdown ticks; the rest of the panel redraws on the server's pushes.
  setInterval(() => {
    if (!plan || !body) return;
    if (Date.now() > plan.expires) { plan = null; render(); return; }
    const t = main?.querySelector(".hl-ttl");
    if (t) t.textContent = `expires in ${Math.max(0, Math.round((plan.expires - Date.now()) / 1000))}s`;
  }, 1000);

  addons.register("halow", {
    mount(b, a) { body = b; api = a; main = null; map = null; render(); },
    hello(s) { snap = s || {}; render(); },
    event(m) {
      if (m.snapshot) { snap = m.snapshot; if (!plan) render(); else drawMap(); return; }
      if (m.event === "plan") { plan = { ...m, expires: Date.now() + m.ttl * 1000 }; render(); return; }
      if (m.event === "applied") { last = m; render(); return; }
      if (m.event === "ping") toast(`HaLow: ${m.ip} ${m.ok ? "answers" : "doesn't answer"} from the bridge`, m.ok ? "info" : "warn");
    },
    off() { snap = {}; plan = null; last = null; render(); },
    tab() {
      const n = (snap.stations || []).length;
      if (!snap.at) return { dot: "off", title: snap.error || "HaLow: not read yet" };
      if (snap.stale || ((snap.dhcp || {}).serving || []).length) return { dot: "tx", title: snap.error || "HaLow: the bridge is serving DHCP" };
      if ((snap.outbound || []).length) return { dot: "tx", badge: n, title: `HaLow: ${snap.outbound.length} connection(s) off the LAN` };
      return { dot: "ok", badge: n, title: `${n} on the HaLow bridge` };
    },
    card() {
      if (!snap.at) return { state: "off", lines: [esc(snap.error || "not read yet")] };
      const n = (snap.stations || []).length;
      return { state: snap.stale ? "stale" : `${n} joined`,
               lines: (snap.stations || []).slice(0, 3).map((x) => `${esc(x.name || x.ip || x.mac)} ${dbm(x.signal)}`) };
    },
  });
})();
