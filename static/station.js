"use strict";

// Station page: navigation between the connected HF radios (each its own
// Radio Remote page in a frame, /r/<id>/), Winlink, Mesh and Packet, plus an
// overview with every radio's ports. One WebSocket to station.py carries the
// radio list, Winlink and Mesh. Uses common.js; winlink.js and mesh.js use
// send() from here.

let ws = null, wsBackoff = 500;

function send(m) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(m)); }

const station = (() => {
  let radios = [], sharedPort = 0, packet = { present: false, configured: false };
  let cfg = {}, meshOn = false, meshConnected = false, wlOn = false, wlArmed = false;
  let meshUnread = 0;
  const frames = {};              // radio id -> iframe (kept while the radio is connected)
  const live = {};                // radio id -> {ws, S}: each radio's own state, read only

  const route = () => (location.hash.replace(/^#\/?/, "") || "home");
  const present = () => radios.filter((r) => r.present);
  // The packet hub's name: what the host file calls it, else the UV-PRO (the
  // only packet hub there was before network KISS TNCs).
  const packetName = () => packet.name || "UV-PRO";
  const portsOf = (r) => {
    const sp = r.shared ? (sharedPort ?? 0) : 0;
    return sp > 0 ? `${sp} · ${r.rigctl}` : `${r.rigctl}`;
  };

  // ---------- navigation

  function tab(id, label, opts = {}) {
    const a = document.createElement("a");
    a.href = `#/${id}`;
    a.className = "st-tab" + (route() === id ? " on" : "") + (opts.off ? " off" : "");
    a.innerHTML = (opts.dot ? `<i class="st-dot ${opts.dot}"></i>` : "") + `<span>${esc(label)}</span>` +
      (opts.sub ? `<small>${esc(opts.sub)}</small>` : "") +
      (opts.badge ? `<b class="st-badge">${opts.badge}</b>` : "");
    if (opts.title) a.title = opts.title;
    return a;
  }

  function renderNav() {
    const nav = $("#st-nav");
    nav.innerHTML = "";
    // A service or add-on that is off or not connected leaves the bar (it got
    // crowded): its Overview card stays, and opening it from there works. Its
    // tab stays while it is the open view, so it never vanishes from under you.
    const view = route(), tucked = [];
    const add = (id, label, opts = {}) => {
      if (opts.off && id !== view) tucked.push(label);
      else nav.append(tab(id, label, opts));
    };
    nav.append(tab("home", "Overview"));
    for (const r of present()) {
      nav.append(tab(r.id, r.name, { dot: "ok", sub: portsOf(r),
        title: `${r.name}: rigctl ${portsOf(r).replace(" · ", " and ")}${r.shared && (sharedPort ?? 0) > 0 ? ` (${sharedPort ?? 0} is shared: the radio connected longest)` : ""}` }));
    }
    if (wlOn) add("winlink", "Winlink", { dot: wlArmed ? "tx" : "", title: `Winlink mail over HF (ARDOP), the ${packetName()} (packet) or telnet` });
    if (meshOn) add("mesh", "Mesh", { dot: meshConnected ? "ok" : "off", badge: meshUnread || "", off: !meshConnected });
    if (packet.configured) add("packet", "Packet", { dot: packet.present ? "ok" : "off", off: !packet.present, title: `${packetName()} packet hub: APRS and the Winlink packet link` });
    for (const d of addons.list()) add(d.name, d.label, addons.tab(d.name) || {});
    if (tucked.length) nav.firstChild.title = `Off or not connected: ${tucked.join(", ")} (their cards are here)`;
    if (modules.enabled()) nav.append(tab("modules", "Modules", { title: "Every module: what's on, what it needs, add or remove" }));
  }

  // ---------- views

  function show() {
    let r = route();
    const radio = radios.find((x) => x.id === r);
    if (radio && !radio.present) { location.hash = "#/home"; return; }
    const addon = addons.list().find((d) => d.name === r);
    if ((r === "winlink" && !wlOn) || (r === "mesh" && !meshOn) || (r === "packet" && !packet.configured)
        || (r === "modules" && !modules.enabled())) r = "home";
    if (!radio && !addon && !["home", "winlink", "mesh", "packet", "modules"].includes(r)) r = "home";
    for (const v of document.querySelectorAll(".st-view")) v.hidden = true;
    if (radio) {
      $("#view-radio").hidden = false;
      frame(radio);
      for (const [id, f] of Object.entries(frames)) f.hidden = id !== radio.id;
      document.title = `${radio.name} · ${cfg.name || "Station"}`;
    } else if (r === "packet") {
      $("#view-packet").hidden = false;
      if (!$("#view-packet iframe") && packet.present) {
        $("#view-packet").innerHTML = "";
        const f = document.createElement("iframe");
        // A fresh address each time: a browser that cached the hub's page
        // (it sent no Cache-Control before 2026-10-19) must fetch it again.
        f.src = `packet/?t=${Date.now()}`;
        f.title = "UV-PRO packet hub";
        $("#view-packet").append(f);
      }
      if (!packet.present) $("#view-packet").innerHTML = `<div class="st-empty">The UV-PRO packet hub is not running.</div>`;
      document.title = `Packet · ${cfg.name || "Station"}`;
    } else if (addon) {
      $(`#view-addon-${r}`).hidden = false;
      document.title = `${addon.label} · ${cfg.name || "Station"}`;
    } else {
      $(`#view-${r}`).hidden = false;
      if (r === "mesh") { meshUnread = 0; }
      if (r === "modules") modules.shown();
      document.title = `${r === "home" ? "Overview" : r[0].toUpperCase() + r.slice(1)} · ${cfg.name || "Station"}`;
    }
    renderNav();
    renderStrip();
  }

  function frame(r) {
    if (frames[r.id]) return frames[r.id];
    const f = document.createElement("iframe");
    f.src = `r/${encodeURIComponent(r.id)}/${token ? `?token=${encodeURIComponent(token)}` : ""}`;
    f.title = `${r.name}: Ranch Radio`;
    f.allow = "microphone; autoplay";
    $("#view-radio").append(f);
    frames[r.id] = f;
    return f;
  }

  function dropGoneFrames() {
    for (const [id, f] of Object.entries(frames)) {
      if (!radios.some((r) => r.id === id && r.present)) { f.remove(); delete frames[id]; }
    }
  }

  // ---------- the radio strip (review 2026-10-18: Winlink, Mesh and Packet
  // hid whether an HF radio was transmitting)

  const fmtF = (f) => {
    const s = String(Math.round(f || 0)).padStart(7, "0");
    return `${s.slice(0, -6) || "0"}.${s.slice(-6, -3)}.${s.slice(-3)}`;
  };

  function syncLive() {
    for (const r of present()) {
      if (live[r.id]) continue;
      const l = live[r.id] = { S: {}, ws: null };
      const open = () => {
        if (live[r.id] !== l) return;
        const w = new WebSocket(withToken(`r/${encodeURIComponent(r.id)}/ws`));
        l.ws = w;
        w.onmessage = (e) => {
          const m = JSON.parse(e.data);
          if (m.t === "hello") Object.assign(l.S, m.state);
          else if (m.t === "state") Object.assign(l.S, m.d);
          else return;
          if ("freq" in (m.d || m.state || {}) || "tx" in (m.d || m.state || {}) ||
              "mode" in (m.d || m.state || {}) || "data" in (m.d || m.state || {}) || m.t === "hello") renderStrip();
        };
        w.onclose = () => { if (live[r.id] === l) setTimeout(open, 2000); };
      };
      open();
    }
    for (const id of Object.keys(live)) {
      if (!present().some((r) => r.id === id)) {
        const l = live[id];
        delete live[id];
        if (l.ws) l.ws.close();
      }
    }
    renderStrip();
  }

  function release(id) {
    const l = live[id];
    if (l && l.ws && l.ws.readyState === 1) l.ws.send(JSON.stringify({ t: "set", k: "tx", v: false }));
  }

  function renderStrip() {
    const el = $("#st-strip");
    const onRadio = radios.some((x) => x.id === route() && x.present);
    const rs = present().filter((r) => live[r.id] && live[r.id].S.freq);
    el.hidden = onRadio || !rs.length;
    if (el.hidden) return;
    el.innerHTML = "";
    for (const r of rs) {
      const S = live[r.id].S, tx = !!S.tx;
      const a = document.createElement("a");
      a.className = "sr" + (tx ? " tx" : "");
      a.href = `#/${r.id}`;
      a.title = `${r.name}: open its page`;
      a.innerHTML = `<b>${esc(r.name)}</b><span class="f">${fmtF(S.freq)}</span>` +
        `<span>${esc(S.mode || "")}${S.data ? "-D" : ""}</span>` + (tx ? `<span class="air">ON AIR</span>` : "");
      el.append(a);
      if (tx) {
        const b = document.createElement("button");
        b.textContent = "Release TX";
        b.title = `Unkey the ${r.name} (anyone may unkey)`;
        b.onclick = () => release(r.id);
        el.append(b);
      }
    }
  }

  // ---------- overview

  function card(title, state, lines, href) {
    const c = document.createElement(href ? "a" : "div");
    c.className = `st-card ${state}`;
    if (href) c.href = href;
    c.innerHTML = `<div class="st-card-head"><i class="st-dot ${state}"></i><b>${esc(title)}</b></div>` +
      lines.map((l) => `<div class="st-card-line">${l}</div>`).join("");
    return c;
  }

  function renderCards() {
    const el = $("#st-cards");
    el.innerHTML = "";
    for (const r of radios) {
      el.append(card(r.name, r.present ? "ok" : "off", r.present ? [
        `rigctl <code>${r.rigctl}</code>${r.shared && (sharedPort ?? 0) > 0 ? ` and the shared <code>${sharedPort ?? 0}</code>` : ""}`,
        r.winlink ? "Winlink over ARDOP" : "",
        "Open the radio →"].filter(Boolean) : ["Not connected", `rigctl <code>${r.rigctl}</code> when plugged in`],
        r.present ? `#/${r.id}` : null));
    }
    if (wlOn) el.append(card("Winlink", wlArmed ? "tx" : "ok", [wlArmed ? "Session on the air" : "Mailbox and sessions", "Open →"], "#/winlink"));
    if (meshOn) el.append(card("Mesh", meshConnected ? "ok" : "off",
      [meshConnected ? `Meshtastic node connected${meshUnread ? ` · ${meshUnread} new` : ""}` : "Node not connected", meshConnected ? "Open →" : ""].filter(Boolean),
      "#/mesh"));
    const packetTitle = packet.title || `Packet (${packetName()})`;
    if (packet.configured) el.append(card(packetTitle, packet.present ? "ok" : "off",
      [packet.present ? "APRS and Winlink packet hub" : "Hub not running", packet.present ? "Open →" : ""].filter(Boolean),
      packet.present ? "#/packet" : null));
    for (const d of addons.list()) {
      const c = addons.card(d.name);
      if (c) el.append(card(d.label, c.state, c.lines, `#/${d.name}`));
    }
    const p = present();
    const sp = sharedPort ?? 0;
    $("#st-help").innerHTML = !p.length
      ? `No HF radio is connected. Plug one in: its page and rigctl port appear here by themselves.`
      : (sp > 0
          ? `Apps that don't care which radio: rigctl <code>${sp}</code> (now the ${esc(p.find((r) => r.shared)?.name || "?")}). ` +
            `An app set up for one radio: use that radio's own port, which never changes.`
          : `An app set up for a radio: use that radio's own port, which never changes.`);
  }

  function viaOptions() {
    const list = present().filter((r) => r.winlink)
      .map((r) => ({ id: r.id, name: r.name, kind: "ardop", label: `HF · ${r.name} (ARDOP)` }));
    const packetLabel = packetName();
    if (packet.present) list.push({ id: "packet", name: packetLabel, kind: "packet", label: `VHF · ${packetLabel} (packet)` });
    list.push({ id: "telnet", name: "internet", kind: "telnet", label: "Telnet (internet, no radio)" });
    winlink.setVias(list);
  }

  // ---------- link

  function applyRadios(m) {
    radios = m.radios || [];
    sharedPort = (m.shared_port !== undefined && m.shared_port !== null) ? m.shared_port : sharedPort;
    packet = m.packet || packet;
    dropGoneFrames();
    syncLive();
    viaOptions();
    renderCards();
    show();
  }

  function setConn(ok) {
    const c = $("#conn");
    c.className = `pill ${ok ? "pill-ok" : "pill-wait"}`;
    c.querySelector("b").textContent = ok ? "Connected" : "Reconnecting";
  }

  function connect() {
    ws = new WebSocket(withToken("ws"));
    ws.onopen = () => { wsBackoff = 500; setConn(true); };
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.t === "hello") {
        cfg = m.config || {};
        $("#st-name").textContent = cfg.name || "Station";
        wlOn = !!cfg.winlink;
        meshOn = !!cfg.mesh;
        meshConnected = !!m.mesh?.status?.connected;
        wlArmed = !!m.winlink?.armed;
        winlink.hello(m.winlink, cfg);
        mesh.hello(m.mesh, cfg);
        addons.hello(cfg.addons, m.addons);
        modules.hello(cfg);
        applyRadios(m);
      } else if (m.t === "radios") {
        applyRadios(m);
      } else if (m.t === "winlink") {
        winlink.event(m);
        if ("armed" in m) { wlArmed = !!m.armed; renderNav(); renderCards(); }
      } else if (m.t === "mesh") {
        mesh.event(m);
        if (m.status) { meshConnected = !!m.status.connected; renderNav(); renderCards(); }
        if (m.msg && !m.msg.mine && route() !== "mesh") { meshUnread++; renderNav(); renderCards(); }
      } else if (m.t === "addon" && m.addon === "winlink") {
        winlink.power(m);                 // Winlink's page is its own panel
        if ("on" in m) modules.addonChanged();
      } else if (m.t === "addon" && m.addon === "mesh") {
        mesh.power(m);                    // Mesh's page is its own tab
        meshConnected = mesh.connected();
        renderNav(); renderCards();
        if ("on" in m) modules.addonChanged();
      } else if (m.t === "addon") {
        addons.event(m);
        if ("on" in m) modules.addonChanged();
      } else if (m.t === "modules" || m.t === "module_plan" || m.t === "module_applied") {
        modules.event(m);
      } else if (m.t === "notice") {
        toast(m.msg, m.level);
      }
    };
    ws.onclose = () => {
      ws = null;
      setConn(false);
      setTimeout(connect, wsBackoff);
      wsBackoff = Math.min(wsBackoff * 2, 5000);
    };
  }

  function clock() {
    $("#utc").textContent = new Date().toISOString().slice(11, 19) + " UTC";
  }

  function start() {
    // Each add-on's panel goes in a view of its own; its tab and card redraw
    // when its state changes (batched to one redraw per frame).
    let queued = false;
    addons.setHost({
      place(name, panel) {
        const v = document.createElement("section");
        v.className = "st-view";
        v.id = `view-addon-${name}`;
        v.hidden = true;
        const g = document.createElement("div");
        g.className = "grid st-grid";
        g.append(panel);
        v.append(g);
        $(".st-main").append(v);
      },
      changed() {
        if (queued) return;
        queued = true;
        requestAnimationFrame(() => { queued = false; renderNav(); renderCards(); });
      },
    });
    modules.mount($("#modules"), { changed: renderNav, visible: () => route() === "modules" });
    window.addEventListener("hashchange", show);
    clock();
    setInterval(clock, 1000);
    connect();
  }

  return { start };
})();
