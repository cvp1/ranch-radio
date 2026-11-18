"use strict";

// The HaLow panel: an OpenWrt Wi-Fi HaLow bridge (addons/halow.py). Shows its
// radios and networks, every station on them, the box's health, its DHCP guard,
// services and log. Every change is two steps: the page asks for a plan, the
// server answers with the exact command and what it will do, and only the
// Confirm button in that dialog runs it. A plan not confirmed expires on the
// server. Uses common.js (esc, toast).

(() => {
  let body = null, snap = {}, api = null, plan = null, last = null;

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

  function render() {
    if (!body) return;
    const s = snap;
    if (!s.target) { body.innerHTML = `<div class="st-empty">Waiting for the bridge…</div>`; return; }
    if (!s.at) {
      body.innerHTML = `<div class="halow">${renderPlan()}<div class="hl-bad">${esc(s.error || "Reading the bridge…")}</div></div>`;
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
        ${v.running ? `<button class="chip chip-mini hl-svc-btn" data-n="${esc(v.name)}" data-a="stop">stop…</button>` : ""}</span>`).join("");
    body.innerHTML = `<div class="halow">${renderPlan()}${head}${guard}
      ${last ? `<div class="${last.ok ? "hl-ok-line" : "hl-alarm"}">${esc(last.title)}: ${last.ok ? "done" : "failed"}
          ${last.undo ? `<button class="chip chip-mini hl-undo">Undo…</button>` : ""}
          ${last.out ? `<pre class="hl-cmd">${esc(last.out)}</pre>` : ""}</div>` : ""}
      <h4>Radios</h4><table class="hl-t"><tr><th>Radio</th><th>Type</th><th>Channel</th><th>Power</th><th>Country</th><th></th></tr>${radios}</table>
      <h4>Networks</h4><table class="hl-t"><tr><th>Name</th><th>Radio</th><th>Mode</th><th>Interface</th><th>Security</th><th>Bridge</th><th></th><th></th></tr>${ifaces}</table>
      <h4>Stations (${(s.stations || []).length})</h4>
      ${st ? `<table class="hl-t"><tr><th>MAC</th><th>Name</th><th>Address</th><th>On</th><th>Signal</th><th>TX</th><th>RX</th><th>Retries</th><th>Joined</th><th></th></tr>${st}</table>`
           : `<div class="st-empty">Nothing is joined to the bridge.</div>`}
      <h4>Services</h4><div class="hl-svcs">${svcs || "none found"}</div>
      <details><summary>Log (last ${(s.log || []).length} lines)</summary><pre class="hl-log">${esc((s.log || []).join("\n"))}</pre></details>
    </div>`;
    wire();
  }

  function wire() {
    const q = (sel, fn) => { for (const el of body.querySelectorAll(sel)) el.onclick = () => fn(el.dataset, el); };
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
    const t = body.querySelector(".hl-ttl");
    if (t) t.textContent = `expires in ${Math.max(0, Math.round((plan.expires - Date.now()) / 1000))}s`;
  }, 1000);

  addons.register("halow", {
    mount(b, a) { body = b; api = a; render(); },
    hello(s) { snap = s || {}; render(); },
    event(m) {
      if (m.snapshot) { snap = m.snapshot; if (!plan) render(); return; }
      if (m.event === "plan") { plan = { ...m, expires: Date.now() + m.ttl * 1000 }; render(); return; }
      if (m.event === "applied") { last = m; render(); return; }
      if (m.event === "ping") toast(`HaLow: ${m.ip} ${m.ok ? "answers" : "doesn't answer"} from the bridge`, m.ok ? "info" : "warn");
    },
    off() { snap = {}; plan = null; last = null; render(); },
    tab() {
      const n = (snap.stations || []).length;
      if (!snap.at) return { dot: "off", title: snap.error || "HaLow: not read yet" };
      if (snap.stale || ((snap.dhcp || {}).serving || []).length) return { dot: "tx", title: snap.error || "HaLow: the bridge is serving DHCP" };
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
