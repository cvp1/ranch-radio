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

  function renderNav() {
    // Grouped (nav.js): Mission Control, then Radios, Messaging, Mesh, Tools, then
    // Modules. A service or add-on that is off or not connected leaves the bar
    // (it got crowded): its Mission Control card stays, and opening it from there
    // works. Its tab stays while it is the open view.
    const view = route();
    const items = present().map((r) => ({ id: r.id, label: r.name, group: "radios", dot: "ok", sub: portsOf(r),
      title: `${r.name}: rigctl ${portsOf(r).replace(" · ", " and ")}${r.shared && (sharedPort ?? 0) > 0 ? ` (${sharedPort ?? 0} is shared: the radio connected longest)` : ""}` }));
    if (wlOn) items.push({ id: "winlink", label: "Winlink", group: "messaging", dot: wlArmed ? "tx" : "",
      title: `Winlink mail over HF (ARDOP), the ${packetName()} (packet) or telnet` });
    if (packet.configured) items.push({ id: "packet", label: "Packet", group: "messaging", dot: packet.present ? "ok" : "off",
      off: !packet.present, title: `${packetName()} packet hub: APRS and the Winlink packet link` });
    if (meshOn) items.push({ id: "mesh", label: "Mesh", short: "Meshtastic", group: "mesh", title: "Meshtastic node",
      dot: meshConnected ? "ok" : "off", badge: meshUnread || "", off: !meshConnected });
    for (const d of addons.list()) items.push({ id: d.name, label: d.label, group: d.group, ...(addons.tab(d.name) || {}) });
    const end = modules.enabled() ? [{ id: "modules", label: "Modules", title: "Every module: what's on, what it needs, add or remove" }] : [];
    const nav = $("#st-nav");
    // Off or disconnected members leave the bar (nav.js tucks them); Mission Control
    // and, when there is one, the Modules tab name them.
    const tucked = navGroups.render(nav, { view, first: [{ id: "home", label: "Mission Control", title: "Mission Control: what is on the air, what needs you, every radio and service (key 0)" }], items, end });
    if (tucked.length) {
      nav.firstChild.title += `\nOff or not connected: ${tucked.join(", ")} (their cards are here)`;
      const mods = nav.querySelector('a[href="#/modules"]');
      if (mods) mods.title += `\nOff or not connected: ${tucked.join(", ")}`;
    }
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
      document.title = `${r === "home" ? "Mission Control" : r[0].toUpperCase() + r.slice(1)} · ${cfg.name || "Station"}`;
    }
    renderNav();
    renderBar();
    if (r === "home") { renderCards(); refreshWlCounts(); }
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

  // ---------- the rig bar (UX plan PR 1a, 2026-11-19; replaces the radio
  // strip of 2026-10-18, which Winlink, Mesh and Packet needed because they hid
  // whether an HF radio was transmitting). Every configured HF radio, on every
  // view, Mission Control and the radios' own tabs included: name and state
  // dot, frequency, mode, band, S-meter (on the air: watts, red), ON AIR with
  // Release TX. Status only: nothing here retunes a radio (typing a frequency
  // is PR 1d); the frequency opens the radio's tab. The one thing it sends is
  // the Release TX the strip always had: {t: "set", k: "tx", v: false}.

  // Each radio's own socket, read only: its state, config (tx_stopped, caps),
  // Winlink arm state and STOP TX latch (kept for PR 1b), and its notices.
  function syncLive() {
    for (const r of present()) {
      if (live[r.id]) continue;
      const l = live[r.id] = { S: {}, ws: null, cfg: {}, armed: false, txStopped: null };
      const open = () => {
        if (live[r.id] !== l) return;
        const w = new WebSocket(withToken(`r/${encodeURIComponent(r.id)}/ws`));
        w.binaryType = "arraybuffer";
        l.ws = w;
        w.onmessage = (e) => {
          // A scope frame: Mission Control's mini spectrum (drawn only while shown).
          if (typeof e.data !== "string") { mission.scopeOf(r.id).push(e.data); return; }
          const m = JSON.parse(e.data);
          if (m.t === "hello") {
            Object.assign(l.S, m.state);
            l.cfg = m.config || {};
            l.txStopped = l.cfg.tx_stopped || null;
            l.armed = !!m.winlink?.armed;
          } else if (m.t === "state") Object.assign(l.S, m.d);
          else if (m.t === "config") { l.cfg = m.config || l.cfg; l.txStopped = l.cfg.tx_stopped || null; renderBar(); return; }
          else if (m.t === "winlink") { if ("armed" in m) { l.armed = !!m.armed; renderBar(); } return; }
          else if (m.t === "tx_stop") { l.txStopped = m.stopped || null; renderBar(); return; }
          else if (m.t === "notice") {
            // A refusal or warning from the radio's server. Its own page shows
            // it when that page is the open view; elsewhere the station does.
            if (route() !== r.id) toast(`${r.name}: ${m.msg}`, m.level);
            return;
          } else return;
          instruments(r.id, l.S);
          homeChanged();
          renderBar();
        };
        w.onclose = () => { if (live[r.id] === l) setTimeout(open, 2000); };
      };
      open();
    }
    mission.forget(present().map((r) => r.id));
    for (const id of Object.keys(live)) {
      if (!present().some((r) => r.id === id)) {
        const l = live[id];
        delete live[id];
        if (l.ws) l.ws.close();
      }
    }
    renderBar();
  }

  function release(id) {
    const l = live[id];
    if (l && l.ws && l.ws.readyState === 1) l.ws.send(JSON.stringify({ t: "set", k: "tx", v: false }));
  }

  // The focus radio (UX plan 5.1): localStorage["rr-focus"] holds its id; by
  // default the radio on the shared port, else the first present one. A focus
  // whose radio is gone falls back without forgetting it, so it comes back.
  const FOCUS_KEY = "rr-focus";
  function focusId() {
    let want = null;
    try { want = localStorage.getItem(FOCUS_KEY); } catch { /* storage off: the default */ }
    const p = present();
    if (want && p.some((r) => r.id === want)) return want;
    return (p.find((r) => r.shared) || p[0] || radios[0])?.id || null;
  }
  function setFocus(id) {
    try { localStorage.setItem(FOCUS_KEY, id); } catch { /* storage off: this page only */ }
    barSig = "";
    renderBar();
  }

  // Redraws are batched to one per frame (an S-meter moves several times a
  // second) and fill the existing elements in place: a rebuild under the
  // pointer would eat a Release TX click.
  let barSig = "", barQueued = false;
  function renderBar() {
    if (barQueued) return;
    barQueued = true;
    requestAnimationFrame(() => { barQueued = false; drawBar(); });
  }

  function drawBar() {
    const el = $("#rig-bar");
    el.hidden = !radios.length;
    if (el.hidden) return;
    const fid = focusId();
    const sig = radios.map((r) => `${r.id}:${r.present ? 1 : 0}`).join(",") + `|${fid}`;
    if (sig !== barSig) {
      barSig = sig;
      el.innerHTML = "";
      for (const r of radios) el.append(rigEl(r, r.id === fid));
    }
    for (const r of radios) {
      const d = [...el.children].find((c) => c.dataset.id === r.id);
      if (d) fillRig(d, r);
    }
  }

  function rigEl(r, focus) {
    const d = document.createElement("div");
    d.className = `rb-rig${focus ? " focus" : ""}`;
    d.dataset.id = r.id;
    const href = `#/${encodeURIComponent(r.id)}`;
    const name = `<i class="st-dot"></i><b>${esc(r.name)}</b>`;
    d.innerHTML = (r.present ? `<a class="rb-name" href="${href}" title="Open the ${esc(r.name)}'s page">${name}</a>`
        : `<span class="rb-name">${name}</span>`) +
      (focus && r.present ? `<a class="rb-f" href="${href}"></a>` : `<span class="rb-f"></span>`) +
      `<span class="rb-mode"></span><span class="rb-band"></span>` +
      `<span class="rb-meter" aria-hidden="true"><i></i></span><span class="rb-lvl"></span>` +
      `<span class="rb-tag" hidden></span><span class="rb-air" hidden>ON AIR</span>` +
      `<button type="button" class="rb-release" hidden title="Unkey the ${esc(r.name)} (anyone may unkey)">Release TX</button>`;
    d.querySelector(".rb-release").onclick = () => release(r.id);
    if (!focus) {
      // A compact radio: a click (not on its name, which opens its tab) focuses it.
      d.tabIndex = 0;
      d.setAttribute("role", "button");
      d.title = `Show the ${r.name} in full here`;
      const pick = (e) => {
        if (e.target.closest("a, button")) return;
        if (e.type === "keydown" && e.key !== "Enter" && e.key !== " ") return;
        e.preventDefault();
        setFocus(r.id);
      };
      d.addEventListener("click", pick);
      d.addEventListener("keydown", pick);
    }
    return d;
  }

  function fillRig(d, r) {
    const l = live[r.id], S = (r.present && l?.S) || {};
    const off = !r.present, dead = r.present && S.connected === false;
    const tx = r.present && !dead && !!S.tx;
    const put = (sel, text) => { const e = d.querySelector(sel); if (e.textContent !== text) e.textContent = text; };
    d.classList.toggle("tx", tx);
    d.classList.toggle("off", off || dead);
    d.querySelector(".st-dot").className = `st-dot ${tx ? "tx" : off || dead ? "off" : "ok"}`;
    put(".rb-f", off ? "offline" : dead ? "no answer" : S.freq ? fmtFreq(S.freq) : "…");
    const f = d.querySelector(".rb-f");
    f.title = off ? `${r.name}: not connected` : dead ? `${r.name}: its server is up, the radio isn't answering`
      : f.tagName === "A" ? `Open the ${r.name}'s page (tune it there)` : "";
    const up = r.present && !dead && S.freq;
    const band = up ? bandOf(S.freq) : null;
    put(".rb-mode", up && S.mode ? `${S.mode}${S.data ? "-D" : ""}` : "");
    put(".rb-band", band ? (band.label || `${band.n} m`) : "");
    let pct = 0, lvl = "";
    if (up && tx) {
      pct = poPct(S.po || 0);
      lvl = `${Math.round((pct / 100) * (l?.cfg?.caps?.max_watts || 100))} W`;
    } else if (up) {
      pct = ((S.smeter || 0) / 241) * 100;
      lvl = sUnits(S.smeter || 0).text;
    }
    d.querySelector(".rb-meter i").style.width = `${clamp(pct, 0, 100).toFixed(0)}%`;
    put(".rb-lvl", lvl);
    // Read-only lane state: STOP TX latched, or a Winlink session armed here.
    const tag = d.querySelector(".rb-tag");
    const t = r.present && l?.txStopped ? "TX STOPPED" : r.present && l?.armed ? "WINLINK" : "";
    put(".rb-tag", t);
    tag.hidden = !t;
    tag.title = t === "TX STOPPED" ? "STOP TX is latched on this radio: nothing keys it until it is resumed on its page"
      : t ? "A Winlink session has this radio's transmitter" : "";
    d.querySelector(".rb-air").hidden = !tx;
    d.querySelector(".rb-release").hidden = !tx;
  }

  // ---------- Mission Control (the home view)
  //
  // One look: what is on the air (with Release TX), what needs you (unread
  // mail and messages, an outbox waiting, a listen window asking if you're
  // there, something offline), then every radio and service live, in the tab
  // bar's groups. A card opens its tab; keys 1-9 open the cards in order, 0
  // comes back here.

  const home = () => route() === "home";
  const keyed = [];               // hrefs of the cards in order: keys 1-9
  let wlCounts = null, wlCountsAt = 0, wlCountsBusy = false;
  let homeQueued = false, homeLast = 0;

  // Redraw Mission Control at most ~3 times a second (an S-meter moves often).
  function homeChanged() {
    if (!home() || homeQueued) return;
    homeQueued = true;
    setTimeout(() => { homeQueued = false; homeLast = Date.now(); renderCards(); },
      Math.max(0, 330 - (Date.now() - homeLast)));
  }

  async function refreshWlCounts(force) {
    if (!wlOn || wlCountsBusy || (!force && Date.now() - wlCountsAt < 60_000)) return;
    wlCountsBusy = true;
    try { wlCounts = await winlink.counts(); } finally { wlCountsBusy = false; wlCountsAt = Date.now(); }
    homeChanged();
  }

  const ago = (t) => {
    const s = Math.max(0, Date.now() / 1000 - t);
    return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
  };
  const OPEN_RE = /^\s*Open( the radio)?\s*→\s*$/;

  // A radio's state into its instruments: the trace's level (S-meter, or
  // power on the air) and the scope's tuned-frequency marker.
  function instruments(id, S) {
    const tr = mission.traceOf(id), sc = mission.scopeOf(id);
    tr.tx = sc.tx = !!S.tx;
    tr.cur = S.tx ? clamp(poPct(S.po || 0) / 100, 0, 1) : clamp((S.smeter || 0) / 241, 0, 1);
    if (sc.freq !== S.freq) { sc.freq = S.freq || 0; sc.dirty = true; }
  }

  // {title, full (the long title, on hover), icon, wide, state ok|off|tx|warn, tag, big, sub, meter: {pct, text, tx, scale},
  //  scope: radio id, lines: [html], href}
  function card(c) {
    const el = document.createElement("div");
    el.className = `st-card mc-card ${c.state}${c.wide ? " mc-wide" : ""}`;
    if (c.href) {
      el.dataset.href = c.href;
      el.tabIndex = 0;
      el.setAttribute("role", "link");
      el.title = `Open ${c.full || c.title}`;
      keyed.push(c.href);
      if (keyed.length <= 9) el.dataset.key = keyed.length;
    }
    const dot = { ok: "ok", tx: "tx", warn: "warn" }[c.state] || "off";
    el.innerHTML = `<div class="st-card-head">${mission.icon(c.icon)}<i class="st-dot ${dot}"></i><b${c.full ? ` title="${esc(c.full)}"` : ""}>${esc(c.title)}</b>` +
      (c.tag ? `<span class="mc-tag ${c.state}">${esc(c.tag)}</span>` : "") +
      (el.dataset.key ? `<kbd class="mc-key">${el.dataset.key}</kbd>` : "") + `</div>` +
      (c.big ? `<div class="mc-big">${c.big}${c.sub ? ` <small>${c.sub}</small>` : ""}</div>` : "") +
      (c.scope ? `<div class="mc-scope-slot" data-scope="${esc(c.scope)}"></div>` : "") +
      (c.meter ? `<div class="mc-meter${c.meter.tx ? " tx" : ""}"><i style="width:${clamp(c.meter.pct, 0, 100).toFixed(0)}%"></i>` +
        `<span>${esc(c.meter.text)}</span>${c.meter.scale ? `<div class="mc-scale">${c.meter.scale.map(([p, t]) =>
          `<em style="left:${p}%">${t}</em>`).join("")}</div>` : ""}</div>` : "") +
      (c.scope ? `<div class="mc-trace-slot" data-trace="${esc(c.scope)}" title="The last two minutes: signal, red where it transmitted"></div>` : "") +
      (c.lines || []).filter((l) => l && !OPEN_RE.test(l)).map((l) => `<div class="st-card-line">${l}</div>`).join("");
    return el;
  }

  function radioCard(r) {
    const S = live[r.id]?.S || {};
    const ports = `rigctl <code>${r.rigctl}</code>${r.shared && (sharedPort ?? 0) > 0 ? ` and the shared <code>${sharedPort ?? 0}</code>` : ""}`;
    if (!r.present) return { title: r.name, icon: "radio", state: "off", tag: "not connected", lines: [`${ports} when plugged in`] };
    const lane = r.winlink ? `Winlink over ${(r.modems || ["ardop"]).map((m) => (m === "varahf" ? "VARA HF" : "ARDOP")).join(" and ")}` : "";
    if (!S.freq) return { title: r.name, icon: "radio", wide: true, state: "ok", big: "…", lines: [ports, lane], href: `#/${r.id}` };
    const band = bandOf(S.freq);
    const tx = !!S.tx;
    const watts = Math.round((poPct(S.po || 0) / 100) * (S.max_watts || 100));
    const su = sUnits(S.smeter || 0);
    return { title: r.name, icon: "radio", wide: true, scope: r.id, state: tx ? "tx" : "ok", tag: tx ? "ON AIR" : "",
      big: `<span class="mc-f">${fmtFreq(S.freq)}</span>`,
      sub: esc(`${S.mode || ""}${S.data ? "-D" : ""}${band ? ` · ${band.label || `${band.n} m`}` : ""}`),
      meter: tx ? { pct: poPct(S.po || 0), text: `${watts} W`, tx: true }
        : { pct: ((S.smeter || 0) / 241) * 100, text: su.text, scale: S_SCALE },
      lines: [ports, lane], href: `#/${r.id}` };
  }

  // S-meter ticks (raw 0..241: S9 = 120, +60 dB = 241).
  const S_SCALE = [[(40 / 241) * 100, "3"], [(80 / 241) * 100, "6"], [(120 / 241) * 100, "9"],
    [(160 / 241) * 100, "+20"], [(201 / 241) * 100, "+40"]];

  function winlinkCard() {
    const w = winlink.summary();
    const n = wlCounts;
    const mail = n ? `Inbox ${n.inbox}${n.unread ? ` · <span class="mc-hot">${n.unread} unread</span>` : ""} · Outbox ${n.outbox}` : "";
    const state = wlArmed ? "tx" : w.off || w.blocked || w.ask ? "warn" : "ok";
    return { title: "Winlink", icon: "mail", state, tag: wlArmed ? "SESSION" : w.listening ? "LISTENING" : w.off ? "off" : "",
      big: "", lines: [esc(w.text || "Idle"), mail], href: "#/winlink" };
  }

  function meshCard() {
    const m = mesh.summary();
    if (!meshConnected) return { title: "Mesh", icon: "mesh", state: "off", tag: "not connected", lines: ["Meshtastic node not connected"], href: "#/mesh" };
    return { title: "Mesh", icon: "mesh", state: "ok", tag: meshUnread ? `${meshUnread} new` : "",
      big: `${m.day} <small>heard today · ${m.live} in 15 min</small>`,
      lines: [m.last ? `<span class="mc-who">${esc(m.last.who)}</span> ${esc(m.last.text.slice(0, 80))}` +
        ` <span class="mc-dim">${esc(ago(m.last.t))}</span>` : "No messages yet"], href: "#/mesh" };
  }

  // What is transmitting now: HF radios (with Release TX), a Winlink
  // session, an add-on on the air (the UV-PRO).
  function renderAir() {
    const el = $("#mc-air");
    el.innerHTML = "";
    const on = [];
    for (const r of present()) {
      const S = live[r.id]?.S;
      if (S?.tx) on.push({ r, text: `${r.name} · ${fmtFreq(S.freq)} ${S.mode || ""}${S.data ? "-D" : ""}`, href: `#/${r.id}` });
    }
    if (wlOn && wlArmed) on.push({ text: `Winlink · ${winlink.summary().text}`, href: "#/winlink" });
    for (const d of addons.list()) {
      const c = addons.card(d.name);
      if (c?.state === "tx") on.push({ text: `${d.label} transmitting`, href: `#/${d.name}` });
    }
    el.classList.toggle("tx", on.length > 0);
    if (!on.length) {
      const w = wlOn ? winlink.summary() : null;
      el.innerHTML = `<div class="mc-air-row"><i class="st-dot ok"></i><span><b>Nothing on the air</b>` +
        (w && (w.listening || w.inbound) ? `<span class="mc-dim"> · Winlink: ${esc(w.text)}</span>` : "") + `</span></div>`;
      return;
    }
    for (const o of on) {
      const row = document.createElement("div");
      row.className = "mc-air-row";
      row.innerHTML = `<i class="st-dot tx"></i><a href="${o.href}"><b>ON AIR</b> ${esc(o.text)}</a>`;
      if (o.r) {
        const b = document.createElement("button");
        b.textContent = "Release TX";
        b.title = `Unkey the ${o.r.name} (anyone may unkey)`;
        b.onclick = () => release(o.r.id);
        row.append(b);
      }
      el.append(row);
    }
  }

  // What needs you: each chip opens the tab that deals with it.
  function renderNeeds(offline) {
    const el = $("#mc-needs");
    const needs = [];
    if (wlOn) {
      const w = winlink.summary();
      if (w.ask) needs.push(["warn", "Winlink: still there? Confirm to keep listening", "#/winlink"]);
      if (w.off) needs.push(["warn", "Winlink is switched off", "#/winlink"]);
      else if (w.blocked) needs.push(["warn", "Winlink can't start Pat", "#/winlink"]);
      if (wlCounts?.unread) needs.push(["hot", `${wlCounts.unread} unread Winlink message${wlCounts.unread === 1 ? "" : "s"}`, "#/winlink"]);
      if (wlCounts?.outbox) needs.push(["", `${wlCounts.outbox} in the Winlink outbox`, "#/winlink"]);
    }
    if (meshUnread) needs.push(["hot", `${meshUnread} new on Mesh`, "#/mesh"]);
    for (const d of addons.list()) {
      const t = addons.tab(d.name) || {};
      const n = parseInt(t.badge, 10);
      if (n) needs.push(["hot", `${n} new on ${d.label}`, `#/${d.name}`]);
    }
    if (offline.length) needs.push(["off", `Offline: ${offline.map((o) => o[0]).join(", ")}`, offline[0][1]]);
    el.innerHTML = needs.length ? "" : `<span class="mc-clear">Nothing needs you</span>`;
    for (const [cls, text, href] of needs) {
      const a = document.createElement("a");
      a.className = `mc-need ${cls}`;
      a.href = href;
      a.textContent = text;
      el.append(a);
    }
  }

  function renderCards() {
    const el = $("#st-cards");
    keyed.length = 0;
    // Cards in the bar's groups (nav.js): radios, messaging, mesh, tools.
    const lanes = navGroups.GROUPS.map((g) => ({ ...g, cards: [] }));
    const put = (group, c) => lanes[navGroups.rank(group)].cards.push(c);
    const offline = [];
    for (const r of radios) {
      put("radios", radioCard(r));
      if (!r.present) offline.push([r.name, "#/home"]);
    }
    if (wlOn) put("messaging", winlinkCard());
    // A short name on the card; the host file's full title on hover (2026-11-19).
    const packetTitle = packet.title || `Packet (${packetName()})`;
    if (packet.configured) {
      put("messaging", { title: `Packet · ${packetName()}`, full: packetTitle, icon: "packet", state: packet.present ? "ok" : "off", tag: packet.present ? "" : "not running",
        lines: [packet.present ? "APRS and the Winlink packet link" : "The packet hub is not running"],
        href: packet.present ? "#/packet" : null });
      if (!packet.present) offline.push([packetName(), "#/home"]);
    }
    if (meshOn) {
      put("mesh", meshCard());
      if (!meshConnected) offline.push(["Mesh", "#/mesh"]);
    }
    for (const d of addons.list()) {
      const c = addons.card(d.name);
      if (!c) continue;
      const known = ["ok", "off", "tx"].includes(c.state);
      put(d.group, { title: d.label, icon: mission.KIND[d.name] || (d.group === "radios" ? "handheld" : "dot"), state: known ? c.state : "ok", tag: known ? (c.state === "off" ? "off" : "") : c.state,
        lines: c.lines, href: `#/${d.name}` });
      const t = addons.tab(d.name);
      if (c.state === "off" && t?.off) offline.push([d.label, `#/${d.name}`]);
    }
    el.innerHTML = "";
    for (const g of lanes) {
      if (!g.cards.length) continue;
      const lane = document.createElement("section");
      lane.className = `mc-lane mc-lane-${g.id}`;
      lane.innerHTML = `<h2 class="mc-lane-name">${esc(g.label)}</h2>`;
      const grid = document.createElement("div");
      grid.className = "st-cards";
      grid.append(...g.cards.map(card));
      lane.append(grid);
      el.append(lane);
    }
    mission.mount(el);
    // The view is redrawn often: every pulse starts at the same point of one
    // 3.2 s clock (its periods divide it), so a redraw never restarts one.
    $("#view-home").style.setProperty("--sync", `-${Date.now() % 3200}ms`);
    renderAir();
    renderNeeds(offline);
    const p = present();
    const sp = sharedPort ?? 0;
    $("#st-help").innerHTML = !p.length
      ? `No HF radio is connected. Plug one in: its page and rigctl port appear here by themselves.`
      : (sp > 0
          ? `Apps that don't care which radio: rigctl <code>${sp}</code> (now the ${esc(p.find((r) => r.shared)?.name || "?")}). ` +
            `An app set up for one radio: use that radio's own port, which never changes.`
          : `An app set up for a radio: use that radio's own port, which never changes.`);
  }

  // A card opens its tab: a click (not on a link inside it), Enter, or its key.
  function openCard(e) {
    const c = e.target.closest?.(".mc-card[data-href]");
    if (!c || e.target.closest("a, button")) return;
    if (e.type === "keydown" && e.key !== "Enter") return;
    location.hash = c.dataset.href;
  }

  function hotkey(e) {
    if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (e.key === "0") { location.hash = "#/home"; e.preventDefault(); return; }
    if (!/^[1-9]$/.test(e.key) || !home()) return;
    const href = keyed[Number(e.key) - 1];
    if (href) { location.hash = href; e.preventDefault(); }
  }

  function viaOptions() {
    // One via per HF modem a radio's lane has (station.json "modems"): the id
    // carries the modem after a colon, so a radio with both is two options.
    const list = [];
    for (const r of present().filter((r) => r.winlink)) {
      list.push({ id: r.id, name: r.name, kind: "ardop", label: `HF · ${r.name} (ARDOP)` });
      if ((r.modems || []).includes("varahf")) list.push({ id: `${r.id}:varahf`, name: r.name, kind: "varahf", label: `HF · ${r.name} (VARA HF)` });
    }
    const packetLabel = packetName();
    if (packet.present) list.push({ id: "packet", name: packetLabel, kind: "packet", label: `VHF · ${packetLabel} (packet)` });
    list.push({ id: "telnet", name: "internet", kind: "telnet", label: "Telnet (internet, no radio)" });
    winlink.setVias(list);
  }

  // ---------- link

  let landed = false;             // the first radios message has picked the opening view

  function applyRadios(m) {
    radios = m.radios || [];
    sharedPort = (m.shared_port !== undefined && m.shared_port !== null) ? m.shared_port : sharedPort;
    packet = m.packet || packet;
    dropGoneFrames();
    syncLive();
    viaOptions();
    renderCards();
    // A station of one radio opens on that radio (a Mac with one rig: the page
    // is the radio's, as it always was); every other station opens on Mission Control.
    if (!landed && !location.hash && present().length === 1) location.hash = `#/${present()[0].id}`;
    landed = true;
    show();
  }

  // The station's callsign and grid beside its name (station.py identity():
  // derived from the logbook sync, Winlink or PSKReporter; 2026-11-19).
  function showIdentity(c) {
    $("#st-call").textContent = [c.call, c.grid].filter(Boolean).join(" · ");
  }

  function setConn(ok) {
    const c = $("#conn");
    c.className = `pill ${ok ? "pill-ok" : "pill-wait"}`;
    c.querySelector("b").textContent = c.title = ok ? "Connected" : "Reconnecting";   // title: a phone shows only the dot
  }

  function connect() {
    ws = new WebSocket(withToken("ws"));
    ws.onopen = () => { wsBackoff = 500; setConn(true); };
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.t === "hello") {
        cfg = m.config || {};
        $("#st-name").textContent = cfg.name || "Station";
        showIdentity(cfg);
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
        if (m.session === "ended" || "inbound" in m) refreshWlCounts(true);
        homeChanged();
        if ("armed" in m) { wlArmed = !!m.armed; renderNav(); renderCards(); }
      } else if (m.t === "mesh") {
        mesh.event(m);
        homeChanged();
        if (m.status) { meshConnected = !!m.status.connected; renderNav(); renderCards(); }
        if (m.msg && !m.msg.mine && !m.msg.emoji && route() !== "mesh") { meshUnread++; renderNav(); renderCards(); }
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
      } else if (m.t === "logsync_settings") {
        addons.event({ ...m, t: "addon", addon: "logbook", settings: true });   // the Logbook tab's Settings
      } else if (m.t === "modules" || m.t === "module_plan" || m.t === "module_applied") {
        modules.event(m);
      } else if (m.t === "notice") {
        toast(m.msg, m.level);
      } else if (m.t === "identity") {      // the Logbook settings changed the callsign
        cfg.call = m.call; cfg.grid = m.grid;
        showIdentity(cfg);
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
    const now = new Date();
    $("#utc").textContent = now.toISOString().slice(11, 19) + " UTC";
    const c = $("#mc-clock");
    if (c && home()) {
      c.querySelector(".mc-utc").textContent = now.toISOString().slice(11, 19);
      c.querySelector(".mc-local").textContent =
        `${now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} local · ${now.toISOString().slice(0, 10)}`;
    }
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
    mission.hooks.firstFrame = homeChanged;
    $("#st-cards").addEventListener("click", openCard);
    $("#st-cards").addEventListener("keydown", openCard);
    document.addEventListener("keydown", hotkey);
    setInterval(() => { if (home()) { refreshWlCounts(); homeChanged(); } }, 30_000);
    clock();
    setInterval(clock, 1000);
    connect();
  }

  return { start };
})();
