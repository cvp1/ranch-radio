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
    hostFrames();
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
    f.hidden = route() !== r.id;          // a prepared frame (PR 1c) waits hidden
    f.addEventListener("load", () => { delete listenWant[r.id]; hostFrames(); renderBar(); });
    $("#view-radio").append(f);
    frames[r.id] = f;
    return f;
  }

  function dropGoneFrames() {
    for (const [id, f] of Object.entries(frames)) {
      if (!radios.some((r) => r.id === id && r.present)) {
        f.remove(); delete frames[id]; delete listening[id]; delete listenWant[id];
      }
    }
  }

  // ---------- Listen in the rig bar (UX plan PR 1c, 2026-11-19)
  //
  // The focus radio's page is loaded early, hidden (other radios' on first
  // view), so the bar's speaker can start its receive audio from any view.
  // The bar calls the frame's window.rrFrame (static/app.js) directly and
  // synchronously inside the click: a parent click activates a same-origin
  // frame, and a direct call keeps the AudioContext inside that gesture.
  // Receive only: rrFrame.listen sends the radio server nothing, so it never
  // keys, retunes, or takes PTT or a Winlink session (server.py compares
  // socket identity, and the frame's socket asks for neither).
  const listening = {};           // radio id -> the frame's last report (onchange)
  const listenWant = {};          // radio id -> the state last asked for, while a call is out
  const onFrame = {};             // radio id -> the onchange this page gave its rrFrame

  function api(id) {
    try {
      const a = frames[id]?.contentWindow?.rrFrame;
      return a && a.v === 1 ? a : null;
    } catch { return null; }      // not same-origin (never, behind the station): no Listen
  }
  // Loaded, with no rrFrame: a radio page from before PR 1c. No Listen for it.
  function oldPage(id) {
    const f = frames[id];
    try { return !!f && f.contentDocument?.readyState === "complete" && !api(id); } catch { return true; }
  }

  // The focus radio's frame, made early and hidden; tell every frame whether
  // it is on screen (a hidden one drops its spectrum socket and loops).
  function prepareFocus() {
    const r = present().find((x) => x.id === focusId());
    if (r) frame(r);
  }
  function hostFrames() {
    const view = route(), shownView = !$("#view-radio").hidden;
    for (const id of Object.keys(frames)) {
      const a = api(id);
      if (!a) continue;
      if (a.onchange !== onFrame[id]) {
        onFrame[id] = (st) => { listening[id] = !!st?.listening; renderBar(); };
        a.onchange = onFrame[id];
        listening[id] = a.listening();
      }
      a.setHost({ shown: shownView && view === id });
    }
  }

  // Called synchronously from the click: no await before rrFrame.listen().
  function toggleListen(r) {
    const a = api(r.id);
    if (!a) { cannotListen(r); return; }
    const now = r.id in listenWant ? listenWant[r.id] : a.listening();
    const want = !now;
    listenWant[r.id] = want;
    renderBar();
    a.listen(want).then((ok) => {
      if (listenWant[r.id] !== want) return;    // a later click owns the outcome
      delete listenWant[r.id];
      listening[r.id] = ok;
      if (want && !ok) cannotListen(r);
      renderBar();
    }, () => { if (listenWant[r.id] === want) delete listenWant[r.id]; renderBar(); });
  }
  // The browser would not start audio from here (not ready yet, or it wants
  // a gesture on the radio's own page): say how, and go there.
  function cannotListen(r) {
    toast("Open the radio tab and press Listen once", "warn");
    location.hash = `#/${encodeURIComponent(r.id)}`;
  }

  // ---------- the rig bar (UX plan PR 1a, 2026-11-19; replaces the radio
  // strip of 2026-10-18, which Winlink, Mesh and Packet needed because they hid
  // whether an HF radio was transmitting). Every configured HF radio, on every
  // view, Mission Control and the radios' own tabs included: name and state
  // dot, frequency, mode, band, S-meter (on the air: watts, red), ON AIR with
  // Release TX. It sends the Release TX the strip always had ({t: "set", k:
  // "tx", v: false}), STOP TX (PR 1b) and, from PR 1d (2026-11-19), a typed
  // frequency for the focus radio, through tuneFocus: the one way this page
  // retunes a radio. The radio's name opens its tab.

  // Each radio's own socket: its state, config (tx_stopped, caps), Winlink arm
  // state, STOP TX latch and notices. It sends only Release TX and the bar's
  // STOP TX (PR 1b), both messages the radio's own page sends.
  function syncLive() {
    for (const r of present()) {
      if (live[r.id]) continue;
      // helloWs: the socket whose hello is in (tuneFocus sends only on that
      // one). waiters: tuneFocus's answers in flight (PR 1d, 2026-11-19).
      const l = live[r.id] = { S: {}, ws: null, cfg: {}, armed: false, helloWs: null, waiters: new Set() };
      const open = () => {
        if (live[r.id] !== l) return;
        const w = new WebSocket(withToken(`r/${encodeURIComponent(r.id)}/ws`));
        w.binaryType = "arraybuffer";
        l.ws = w;
        w.onmessage = (e) => {
          // A scope frame: Mission Control's mini spectrum (drawn only while shown).
          if (typeof e.data !== "string") { mission.scopeOf(r.id).push(e.data); return; }
          const m = JSON.parse(e.data);
          if (m.t === "state") Object.assign(l.S, m.d);
          // tuneFocus's waiters see each message on their socket after the state
          // has taken it; one that claims a notice keeps it out of the toasts
          // (its own report says it).
          let claimed = false;
          for (const fn of [...l.waiters]) claimed = fn(w, m) || claimed;
          if (m.t === "hello") {
            l.helloWs = w;
            Object.assign(l.S, m.state);
            l.cfg = m.config || {};
            l.armed = !!m.winlink?.armed;
            stopHeard(r.id, w, l.cfg.tx_stopped || null, true);
          } else if (m.t === "state") { /* taken above */ }
          else if (m.t === "config") {
            l.cfg = m.config || l.cfg;
            if (latch[r.id]?.ws === w) stopHeard(r.id, w, l.cfg.tx_stopped || null, false);
            renderBar();
            return;
          }
          else if (m.t === "winlink") { if ("armed" in m) { l.armed = !!m.armed; renderBar(); } return; }
          else if (m.t === "tx_stop") { stopHeard(r.id, w, m.stopped || null, false); renderBar(); return; }
          else if (m.t === "notice") {
            // tx_stop() reports a step it could not finish as a warn notice
            // starting "STOP TX:" (server.py); the bar keeps it beside the
            // stop state (UX plan PR 1b, 2026-11-19): it is the only signal.
            if (/^STOP TX:/.test(m.msg || "") && latch[r.id]?.ws === w) {
              latch[r.id].fail = m.level === "warn" ? m.msg : "";
              renderBar();
            }
            // A refusal or warning from the radio's server. Its own page shows
            // it when that page is the open view; elsewhere the station does.
            if (route() !== r.id && !claimed) toast(`${r.name}: ${m.msg}`, m.level);
            return;
          } else return;
          instruments(r.id, l.S);
          homeChanged();
          renderBar();
        };
        w.onclose = () => {
          // What this socket said about the latch ends with it: the radio is
          // unknown until the next socket's hello (UX plan PR 1b, 2026-11-19).
          if (latch[r.id]?.ws === w) { delete latch[r.id]; renderBar(); }
          if (l.helloWs === w) l.helloWs = null;
          for (const fn of [...l.waiters]) fn(w, { t: "closed" });
          if (live[r.id] === l) setTimeout(open, 2000);
        };
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
    entryFor = null;              // an open entry was for the old focus: the rebuild drops it
    prepareFocus();
    renderBar();
    homeChanged();                // the band strip follows the focus radio's bands (PR 3a)
  }

  // ---------- tuneFocus: the one way the station page retunes a radio
  // (UX plan PR 1d, 2026-11-19). In this PR only the bar's typed frequency
  // calls it; band cells, openings and spots will (PR 4c).
  //
  // Everything that could make a retune wrong is read again at the moment of
  // sending, never from what the page showed when the entry opened: the lock
  // (the radio pages' key, "ic7300.locked"; a storage event reaches the other
  // documents, not the writer, so it is read from storage here), which radio
  // has focus, its socket and hello, and whether it is transmitting, tuning
  // or has a Winlink session armed. Any of those, and nothing is sent. The
  // radio server keeps its own refusals (transmitting, PTT held, tuner, and
  // S1's armed lane); this only refuses earlier. Only the radio page's own
  // messages go out: set freq, then set mode if it differs.
  //
  // A band change can bring an ATU tune on a station with auto-tune: that is
  // a carrier, said in every band-changing control's title (bandChangeNote).
  const TUNE_ANSWER_MS = 5000;
  const TUNE_SLOP_HZ = 50;        // a radio may round the dial (10 Hz steps)
  const tuneLocked = () => store.get("locked", true) !== false;   // unreadable: locked

  // Why tuning the focus radio must not happen now, or "". expect: the radio
  // id the action was aimed at (the focus when the entry opened).
  function tuneBlocked(expect) {
    if (tuneLocked()) return "locked";
    const id = focusId(), r = present().find((x) => x.id === id), l = live[id];
    if (expect && id !== expect) return "The focus radio changed: nothing sent.";
    if (!r || !l) return "No radio has focus: nothing sent.";
    if (!l.ws || l.ws.readyState !== 1 || l.helloWs !== l.ws) return `${r.name}: not connected yet: nothing sent.`;
    const S = l.S;
    if (S.connected === false) return `${r.name}: the radio isn't answering: nothing sent.`;
    if (S.tx) return `${r.name} is transmitting: not retuned.`;
    if (S.tuner === 2) return `${r.name} is tuning its ATU: not retuned.`;
    if (l.armed) return `${r.name}: a Winlink session is armed and owns the dial: not retuned.`;
    return "";
  }

  // The radio's answer to one set on socket `w`: {ok, why}. Accepted when its
  // state reaches `want`; refused on a confirmed state (server.py _confirm
  // sends the real value after a refusal) or a warn notice to this socket
  // (a refusal, or "Rejected: ..."); unknown after TUNE_ANSWER_MS or if the
  // socket closes.
  function answer(l, w, key, want) {
    return new Promise((done) => {
      let note = "";
      const hit = (v) => key === "freq" ? Math.abs(Number(v) - want) <= TUNE_SLOP_HZ : v === want;
      const end = (res) => { l.waiters.delete(fn); clearTimeout(t); done(res); };
      const fn = (sock, m) => {
        if (sock !== w) return false;
        if (m.t === "closed") { end({ ok: false, why: "the connection dropped before the radio answered" }); return false; }
        if (m.t === "notice" && m.level === "warn") {
          note = m.msg || "";
          if (/^Rejected/.test(note)) end({ ok: false, why: note });
          return true;
        }
        if (m.t === "state" && m.d && key in m.d) {
          if (hit(m.d[key])) end({ ok: true });
          else if (m.confirmed) end({ ok: false, why: note || `the radio kept ${key} unchanged` });
        }
        return false;
      };
      const t = setTimeout(() => end({ ok: false, why: note || "no answer from the radio" }), TUNE_ANSWER_MS);
      l.waiters.add(fn);
    });
  }

  function setOn(w, k, v) {
    w.send(JSON.stringify({ t: "set", k, v }));
  }

  // tuneFocus({freq, mode, band}, expect): retune the focus radio. freq in
  // Hz; band ("40", ...) picks from the shared band stack when no freq is
  // given; mode only when it differs. Resolves {ok, why, freq, mode}: what
  // happened, also said in a toast when it is not plain success.
  async function tuneFocus({ freq = null, mode = null, band = null } = {}, expect = null) {
    const why = tuneBlocked(expect);
    if (why === "locked") { nudgeBarLock(); return { ok: false, why }; }
    if (why) { toast(why, "warn"); return { ok: false, why }; }
    const id = focusId(), r = present().find((x) => x.id === id), l = live[id], w = l.ws;
    if (freq == null && band) {
      const mem = bandStackTarget(String(band), { freq: l.S.freq, mode: l.S.mode });
      if (!mem) { toast(`No ${band} m band here`, "warn"); return { ok: false, why: "no such band" }; }
      freq = mem.f;
      mode = mode || mem.m;
    }
    const out = { ok: true, why: "", freq: false, mode: false };
    if (freq != null) {
      freq = Math.round(Number(freq));
      const range = l.cfg?.caps?.freq;
      if (!Number.isFinite(freq) || freq <= 0 || (Array.isArray(range) && (freq < range[0] || freq > range[1]))) {
        toast(`${r.name} can't tune ${Number.isFinite(freq) ? fmtFreq(freq) : "that"}`, "warn");
        return { ...out, ok: false, why: "out of range" };
      }
      if (Math.abs(freq - (l.S.freq || 0)) > 0) {
        setOn(w, "freq", freq);
        const a = await answer(l, w, "freq", freq);
        if (!a.ok) {
          toast(`${r.name}: not retuned (${a.why})`, "warn");
          return { ...out, ok: false, why: a.why };
        }
        out.freq = true;
      }
    }
    if (mode && mode !== l.S.mode) {
      // Read it all again: the freq step took time, and the radio may have
      // keyed, armed or lost focus meanwhile.
      const again = tuneBlocked(id);
      const a = again ? { ok: false, why: again === "locked" ? "tuning was locked" : again }
        : (setOn(w, "mode", mode), await answer(l, w, "mode", mode));
      if (!a.ok) {
        toast(`${r.name}: ${out.freq ? `on ${fmtFreq(l.S.freq)}, but ` : ""}mode stayed ${l.S.mode || "?"} (${a.why})`, "warn");
        return { ...out, ok: false, why: a.why };
      }
      out.mode = true;
    }
    return out;
  }

  // Locked: flash the bar's padlock (the radio page's nudge, here), once a
  // toast every few seconds.
  let nudgeT = -Infinity;         // not 0: performance.now() starts at 0 with each page
  function nudgeBarLock() {
    const b = document.querySelector("#rig-bar .rb-lock");
    if (b) { b.classList.remove("flash"); void b.offsetWidth; b.classList.add("flash"); }
    const now = performance.now();
    if (now - nudgeT > 3000) { nudgeT = now; toast("Tuning is locked · unlock the padlock to change it"); }
  }

  function setTuneLocked(v) {
    try { store.set("locked", !!v); } catch { /* storage off: it reads as locked */ }
    lockChanged();
  }
  // The lock changed (here, or in a radio page: a storage event): an open
  // entry closes, so nothing typed under one lock commits under another.
  function lockChanged() {
    if (entryFor && tuneLocked()) closeEntry();
    renderBar();
  }
  window.addEventListener("storage", (e) => {
    if (e.key === null || e.key === "ic7300.locked") lockChanged();
    if (e.key === null || e.key === FOCUS_KEY) { barSig = ""; entryFor = null; renderBar(); }
  });

  // The bar's frequency entry. entryFor: the radio id it was opened for.
  let entryFor = null;
  function closeEntry() {
    entryFor = null;
    const d = document.querySelector("#rig-bar .rb-rig.focus");
    if (!d) return;
    const form = d.querySelector(".rb-entry"), f = d.querySelector(".rb-f");
    if (form) form.hidden = true;
    if (f) f.hidden = false;
  }
  function bindEntry(d, r) {
    const f = d.querySelector(".rb-f"), form = d.querySelector(".rb-entry"), input = d.querySelector(".rb-in");
    d.querySelector(".rb-lock").onclick = () => setTuneLocked(!tuneLocked());
    f.onclick = () => {
      if (tuneLocked()) { nudgeBarLock(); return; }
      const S = live[r.id]?.S || {};
      if (!S.freq) return;
      entryFor = r.id;
      f.hidden = true; form.hidden = false;
      input.value = (S.freq / 1e6).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
      input.focus(); input.select();
    };
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const want = entryFor, txt = input.value;
      closeEntry();
      if (!want) return;
      const hz = parseFreq(txt);
      if (hz) tuneFocus({ freq: hz }, want);
      else if (txt.trim()) toast("Couldn't read that frequency", "warn");
    });
    input.addEventListener("blur", () => { if (entryFor === r.id) closeEntry(); });
    input.addEventListener("keydown", (e) => { if (e.key === "Escape") closeEntry(); });
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
      entryFor = null;            // a rebuild drops an open entry (PR 1d)
      el.innerHTML = "";
      for (const r of radios) el.append(rigEl(r, r.id === fid));
      el.append(stopEl());
    }
    for (const r of radios) {
      const d = [...el.children].find((c) => c.dataset.id === r.id);
      if (d) fillRig(d, r);
    }
    fillStop(el.querySelector(".rb-stop"));
  }

  function rigEl(r, focus) {
    const d = document.createElement("div");
    d.className = `rb-rig${focus ? " focus" : ""}`;
    d.dataset.id = r.id;
    const href = `#/${encodeURIComponent(r.id)}`;
    const name = `<i class="st-dot"></i><b>${esc(r.name)}</b>`;
    d.innerHTML = (r.present ? `<a class="rb-name" href="${href}" title="Open the ${esc(r.name)}'s page">${name}</a>`
        : `<span class="rb-name">${name}</span>`) +
      (focus && r.present ? `<button type="button" class="rb-listen" aria-pressed="false" hidden>` +
        `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 6h3l4-3v10l-4-3H2z"/>` +
        `<path class="rb-waves" d="M11 5.5a3.5 3.5 0 0 1 0 5M12.8 3.5a6 6 0 0 1 0 9"/></svg></button>` : "") +
      // PR 1d: the focus radio's frequency is a button that opens a typed
      // entry (unlocked), beside the lock both pages share.
      (focus && r.present ? `<button type="button" class="rb-f"></button>` +
        `<form class="rb-entry" hidden><input class="rb-in" inputmode="decimal" autocomplete="off" ` +
        `spellcheck="false" placeholder="14.074 or 7074" aria-label="Frequency for the ${esc(r.name)}"></form>` +
        `<button type="button" class="rb-lock" role="switch" aria-checked="true">` +
        `<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="7" width="10" height="7" rx="1.5"/>` +
        `<path class="rb-shackle" d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/></svg></button>`
        : `<span class="rb-f"></span>`) +
      `<span class="rb-mode"></span><span class="rb-band"></span>` +
      `<span class="rb-meter" aria-hidden="true"><i></i></span><span class="rb-lvl"></span>` +
      `<a class="rb-tag" hidden></a><span class="rb-air" hidden>ON AIR</span>` +
      `<button type="button" class="rb-release" hidden title="Unkey the ${esc(r.name)} (anyone may unkey)">Release TX</button>`;
    d.querySelector(".rb-release").onclick = () => release(r.id);
    const ls = d.querySelector(".rb-listen");
    if (ls) ls.onclick = () => toggleListen(r);
    if (focus && r.present) bindEntry(d, r);
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
    const lk = d.querySelector(".rb-lock");
    const note = bandChangeNote(!!l?.cfg?.auto_tune);
    f.title = off ? `${r.name}: not connected` : dead ? `${r.name}: its server is up, the radio isn't answering`
      : f.tagName !== "BUTTON" ? ""
      : tuneLocked() ? `Tuning is locked: unlock (the padlock) to type a frequency for the ${r.name}. ${note}`
      : `Type a frequency for the ${r.name}. ${note}`;
    if (lk) {
      const v = tuneLocked();
      lk.setAttribute("aria-checked", String(v));
      lk.classList.toggle("on", v);
      const t = v ? "Tuning locked here and on every radio page: click to unlock"
        : "Tuning unlocked: click to lock it here and on every radio page";
      if (lk.title !== t) { lk.title = t; lk.setAttribute("aria-label", t); }
      const inp = d.querySelector(".rb-in");
      inp.title = `A frequency for the ${r.name} in MHz, kHz or Hz. ${note}`;
    }
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
    // Lane state: STOP TX latched (on this socket; PR 1b), or a Winlink session
    // armed here. The tag opens the radio's tab, where Resume and its confirm are.
    const tag = d.querySelector(".rb-tag");
    const st = stopWord(r);
    d.dataset.stop = st.k;
    const t = st.k === "keyed" ? "STILL KEYED" : st.k === "stopped" ? "STOPPED" : r.present && l?.armed ? "WINLINK" : "";
    put(".rb-tag", t);
    tag.hidden = !t;
    tag.classList.toggle("stop", st.k === "stopped" || st.k === "keyed");
    if (r.present) tag.href = `#/${encodeURIComponent(r.id)}`; else tag.removeAttribute("href");
    tag.title = st.k === "keyed" ? "STOP TX is latched but the radio still reports transmitting: check the radio"
      : st.k === "stopped" ? "STOP TX is latched on this radio: nothing keys it until it is resumed on its page (open it)"
      : t ? "A Winlink session has this radio's transmitter" : "";
    d.querySelector(".rb-air").hidden = !tx;
    d.querySelector(".rb-release").hidden = !tx;
    // Listen (PR 1c): the focus radio, when its server has audio and its page
    // is not one from before rrFrame. Pressed while a start is out, too.
    const ls = d.querySelector(".rb-listen");
    if (ls) {
      ls.hidden = !(r.present && l?.cfg?.audio && !oldPage(r.id));
      const want = r.id in listenWant ? listenWant[r.id] : !!listening[r.id];
      const p = String(want);
      if (ls.getAttribute("aria-pressed") !== p) ls.setAttribute("aria-pressed", p);
      ls.classList.toggle("busy", r.id in listenWant);
      ls.classList.toggle("on", !!listening[r.id]);
      const t = listening[r.id] ? `Stop listening to the ${r.name}` : `Listen to the ${r.name} (receive audio; volume on its page)`;
      if (ls.title !== t) { ls.title = t; ls.setAttribute("aria-label", t); }
    }
  }

  // ---------- STOP TX for every HF radio (UX plan PR 1b, 2026-11-19)
  //
  // One button sends the radio page's own {t: "tx_stop"} on each radio's
  // socket (the station proxies it; server.py handles it as from that page).
  // Targets are the configured HF radios only, present or not; the packet
  // hub's Off means something else (plan D1). Sending proves nothing: the
  // station accepts the socket before the radio does, so a radio counts as
  // stopped only when its *current* socket said so (a hello, config or
  // tx_stop with a non-null tx_stopped). A closed socket makes it unknown.
  //
  // A press is also an intent per radio, kept in localStorage so a reload or
  // a radio that was down still gets it (plan D2): on each fresh hello a live
  // intent re-sends until the radio confirms. It ends on that confirmation,
  // on a Resume, on Cancel here, or after STOP_INTENT_TTL_S; an expired one
  // shows as not delivered and never re-sends, so an old press cannot undo
  // a later deliberate Resume. A radio whose latch vanishes (its server
  // restarted: the latch is process memory) is reported, never re-stopped.
  const STOP_INTENT_KEY = "rr-stop-intent";
  const STOP_INTENT_TTL_S = 600;
  const latch = {};               // radio id -> {ws, stopped, sent, fail}: what its current socket said
  const wasStopped = {};          // radio id -> true: this page has seen it latched
  const unstopped = {};           // radio id -> "resumed" | "restarted": was stopped, is not now
  let stopMem = {};               // the intents when localStorage is off
  let stopTimer = 0;

  // A test hook may shorten the TTL (never lengthen it): rr-stop-ttl-s.
  function stopTtlMs() {
    let t = NaN;
    try { t = Number(localStorage.getItem("rr-stop-ttl-s")); } catch { /* storage off */ }
    return (t > 0 && t < STOP_INTENT_TTL_S ? t : STOP_INTENT_TTL_S) * 1000;
  }
  function intents() {
    try {
      const v = JSON.parse(localStorage.getItem(STOP_INTENT_KEY) || "{}");
      return v && typeof v === "object" ? v : {};
    } catch { return { ...stopMem }; }
  }
  function saveIntents(v) {
    stopMem = v;
    try {
      if (Object.keys(v).length) localStorage.setItem(STOP_INTENT_KEY, JSON.stringify(v));
      else localStorage.removeItem(STOP_INTENT_KEY);
    } catch { /* storage off: this page only */ }
  }
  const intentLive = (it) => !!it && Date.now() - it.at < stopTtlMs();
  function endIntent(id) {
    const v = intents();
    if (id in v) { delete v[id]; saveIntents(v); }
  }

  // Send this radio a live intent's STOP TX if its current socket has said
  // it is not latched.
  function deliverStop(id) {
    const k = latch[id];
    if (intentLive(intents()[id]) && k && !k.stopped) sendStop(id);
  }
  // One radio's STOP TX on its current socket, once that socket's hello is in.
  // A throw here must not keep the message from the radios after it.
  function sendStop(id) {
    const k = latch[id];
    if (!k || live[id]?.ws !== k.ws || k.ws.readyState !== 1) return;
    try {
      k.ws.send(JSON.stringify({ t: "tx_stop" }));
      k.sent = true;
    } catch { /* this socket is going; its next hello re-sends a live intent */ }
  }

  // What socket `w` says about radio `id`'s latch. fresh: its hello (a new
  // socket, so a new chance to deliver a live intent). Nothing a socket says
  // before its hello counts.
  function stopHeard(id, w, stopped, fresh) {
    const prev = latch[id];
    if (!fresh && prev?.ws !== w) return;
    latch[id] = { ws: w, stopped, sent: !fresh && !!prev.sent, fail: (!fresh && prev.fail) || "" };
    if (stopped) {
      wasStopped[id] = true;
      delete unstopped[id];
      endIntent(id);                               // confirmed: the intent is done
    } else if (wasStopped[id]) {
      // Stopped before and not now: a Resume on this socket, or a hello from a
      // radio server that restarted. Reported; never re-stopped by the page.
      wasStopped[id] = false;
      unstopped[id] = fresh ? "restarted" : "resumed";
      if (!fresh) endIntent(id);                   // a Resume ends any intent
      else deliverStop(id);                        // only a press made since then re-sends
    } else if (fresh) deliverStop(id);
    renderBar();
  }

  function stopAll() {
    const now = Date.now(), v = intents();
    for (const r of radios) { v[r.id] = { at: now }; delete unstopped[r.id]; }
    saveIntents(v);
    // Every reachable radio gets it, latched or not: a second STOP TX unkeys
    // again (server.py tx_stop "again"). The rest get it on their next hello.
    for (const r of radios) sendStop(r.id);
    renderBar();
  }

  function cancelStop() {
    saveIntents({});
    for (const id of Object.keys(unstopped)) delete unstopped[id];
    renderBar();
  }

  // One radio's stop state: {k, text}. k: keyed | stopped | expired |
  // pending | offline | unknown | resumed | restarted | none.
  function stopWord(r) {
    const it = intents()[r.id], k = latch[r.id], S = live[r.id]?.S || {};
    const known = r.present && k && live[r.id]?.ws === k.ws;
    if (known && k.stopped) {
      return S.tx ? { k: "keyed", text: "LATCHED BUT STILL KEYED: check the radio" } : { k: "stopped", text: "stopped" };
    }
    const where = !r.present ? "offline" : !known ? "unknown" : "";
    if (it && !intentLive(it)) return { k: "expired", text: "not delivered (expired)" };
    if (it) return { k: "pending", text: `not reached${where ? ` (${where})` : k?.sent ? " yet" : ""}` };
    if (where) return { k: where, text: where };
    if (unstopped[r.id] === "restarted") return { k: "restarted", text: "no longer stopped (its server restarted)" };
    if (unstopped[r.id] === "resumed") return { k: "resumed", text: "resumed" };
    return { k: "none", text: "not stopped" };
  }

  function stopEl() {
    const d = document.createElement("div");
    d.className = "rb-stop";
    d.innerHTML = `<span class="rb-stop-state" role="status" aria-live="polite" hidden></span>` +
      `<button type="button" class="rb-stop-cancel" hidden>Cancel</button>` +
      `<button type="button" class="rb-stop-btn" title="STOP TX on every HF radio: unkey, stop and disarm Winlink, ` +
      `and refuse every new transmission until each is resumed on its own page. Not the packet hub.">` +
      `STOP TX <small>all HF</small></button>`;
    d.querySelector(".rb-stop-btn").onclick = stopAll;
    d.querySelector(".rb-stop-cancel").onclick = cancelStop;
    return d;
  }

  function fillStop(d) {
    if (!d) return;
    const v = intents(), ws = radios.map((r) => ({ r, ...stopWord(r) }));
    const latched = ws.filter((x) => x.k === "stopped" || x.k === "keyed");
    const keyed = ws.filter((x) => x.k === "keyed");
    const anyIntent = radios.some((r) => v[r.id]);
    const restarted = ws.some((x) => x.k === "restarted");
    // A radio seen latched whose socket is gone stays in view as unknown.
    const lost = ws.some((x) => wasStopped[x.r.id] && (x.k === "unknown" || x.k === "offline"));
    let state = "", text = "";
    if (keyed.length) {
      state = "keyed";
      text = keyed.map((x) => `${x.r.name}: LATCHED BUT STILL KEYED: check the radio`).join(" · ");
      const rest = ws.filter((x) => x.k !== "keyed" && x.k !== "stopped");
      if (rest.length) text += " · " + rest.map((x) => `${x.r.name} ${x.text}`).join(" · ");
    } else if (ws.length && latched.length === ws.length) {
      state = "all";
      text = "TX STOPPED";
    } else if (latched.length || anyIntent || restarted || lost) {
      state = latched.length ? "partial" : "none";
      text = (latched.length || !anyIntent ? "" : "STOP TX reached no radio: ") +
        ws.filter((x) => latched.length || x.k !== "none").map((x) => `${x.r.name} ${x.text}`).join(" · ");
    }
    const fails = ws.filter((x) => latch[x.r.id]?.fail && (x.k === "stopped" || x.k === "keyed"))
      .map((x) => `${x.r.name}: ${latch[x.r.id].fail}`);
    if (fails.length) text += (text ? " · " : "") + fails.join(" · ");
    const el = d.querySelector(".rb-stop-state");
    if (el.textContent !== text) el.textContent = text;
    el.hidden = !text;
    el.dataset.state = state;
    el.title = state === "all" ? "Every HF radio reports STOP TX latched. Resume each on its own page." : text;
    d.classList.toggle("bad", state === "keyed" || !!fails.length || ws.some((x) => x.k === "expired" || x.k === "restarted"));
    d.classList.toggle("all", state === "all");
    // Cancel ends pending intents; with only expired ones (or a lost latch
    // to acknowledge) it dismisses them.
    const c = d.querySelector(".rb-stop-cancel");
    const pending = ws.some((x) => x.k === "pending");
    c.hidden = !(anyIntent || restarted);
    c.textContent = pending ? "Cancel" : "Dismiss";
    c.title = pending ? "Stop re-sending STOP TX to the radios not reached yet (nothing is resumed)"
      : "Clear the notes on STOP TX here (nothing is sent)";
    // Re-draw when the next live intent expires.
    clearTimeout(stopTimer);
    const ttl = stopTtlMs();
    const next = Math.min(...radios.map((r) => v[r.id]).filter(intentLive).map((it) => it.at + ttl - Date.now()));
    if (Number.isFinite(next)) stopTimer = setTimeout(renderBar, Math.max(0, next) + 50);
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
    // The band strip under the clock row (UX plan PR 3a, 2026-11-19): the
    // bands the focus radio covers. Display only; it never retunes.
    bandStrip.render($("#mc-bands"), { range: live[focusId()]?.cfg?.caps?.freq || null });
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
    prepareFocus();
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

  // tuneFocus: for PR 4c's band cells, openings and spots, and the tests.
  return { start, tuneFocus };
})();
