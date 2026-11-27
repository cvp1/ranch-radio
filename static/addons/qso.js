"use strict";

// The QSO tab: log a phone or CW contact by hand. The server side is
// addons/qso.py (and qso_lookup.py); the registry (static/addons.js) gives
// this a panel with an on/off switch and routes its messages. Uses common.js
// (esc, toast).
//
// What the page fills in itself: the frequency and mode from the chosen radio
// (the snapshot's "rigs", then {"event": "rigs"} as the dial moves; untick
// "Follow the radio" to type them), and the time on, set when the call is
// first typed. A call that looks like one is looked up once typing stops:
//   page -> {do: "lookup", id, call, khz, band, mode}
//   server -> {event: "lookup", id, call, dxcc, qsos, pota, pota_ref, qrz_state, qrz, need, qrz_login}
// A field the operator has typed in is never overwritten by a lookup.
//
// Log: {do: "log", id, qso: {...}} -> {event: "logged", id, qid, what, call, band, mode, time}
// or {event: "logged", error}. The QSO goes into the logbook sync's ledger and
// waits there until Wavelog and QRZ can be reached.
//
// QRZ login: shown when the station has no QRZ password, or QRZ refused it
// (and from the "QRZ login" link). {do: "login", user, password}; the box is
// cleared as it is sent, and the page only ever hears the account's state.

(() => {
  let body = null, snap = {}, api = null, seq = 0, want = null, typed = new Set(), showLogin = false;
  const logged = [];                   // this page's QSOs, newest first
  const BANDS = [["160m", 1.8, 2], ["80m", 3.5, 4], ["60m", 5.06, 5.45], ["40m", 7, 7.3], ["30m", 10.1, 10.15],
    ["20m", 14, 14.35], ["17m", 18.068, 18.168], ["15m", 21, 21.45], ["12m", 24.89, 24.99], ["10m", 28, 29.7],
    ["6m", 50, 54], ["2m", 144, 148], ["70cm", 420, 450]];
  const MODES = ["SSB", "CW", "AM", "FM", "FT8", "FT4", "RTTY", "PSK31", "JS8", "OLIVIA", "SSTV", "DIGITALVOICE"];
  const QRZ_SAYS = {
    ok: "", not_found: "Not on QRZ.", no_login: "No QRZ login on this station.",
    bad_login: "QRZ refused the login.", no_subscription: "QRZ: no XML subscription on this login (name and QTH need one).",
    offline: "QRZ can't be reached (offline?): the logbook and DXCC only.", error: "QRZ didn't answer properly.",
    unknown: "",
  };
  const NEED = {
    dxcc: "New DXCC", dxcc_band: "New band", dxcc_mode: "New mode", grid: "New grid",
    grid_band: "Grid on band", grid_mode: "Grid in mode", dxcc_unconfirmed: "DXCC unconfirmed", call_band_mode: "New call",
  };

  const $ = (sel) => body && body.querySelector(sel);
  const val = (sel) => (($(sel) || {}).value || "").trim();
  const bandOf = (mhz) => (BANDS.find(([, lo, hi]) => mhz >= lo && mhz <= hi) || [""])[0];
  const utcNow = () => new Date().toISOString().slice(11, 19);

  function rig() {
    const rigs = snap.rigs || [];
    const id = val(".qso-radio") || snap.radio;
    return rigs.find((r) => r.id === id) || rigs.find((r) => r.present) || rigs[0] || null;
  }

  function rigMode(r) {
    const m = String(r.mode || "").toUpperCase();
    if (r.data) return ["FT8", ""];
    if (m === "USB" || m === "LSB") return ["SSB", m];
    if (m.startsWith("CW")) return ["CW", ""];
    if (m === "AM" || m === "FM") return [m, ""];
    return ["", ""];
  }

  // The dial, when following the radio: into the frequency and mode boxes.
  function follow() {
    if (!body || !$(".qso-follow") || !$(".qso-follow").checked) return;
    const r = rig();
    if (!r || !r.freq) { $(".qso-dial").textContent = r ? `${r.name}: no dial yet` : "No radio"; return; }
    const [mode, side] = rigMode(r);
    $(".qso-mhz").value = (r.freq / 1e6).toFixed(5);
    if (mode) $(".qso-mode").value = mode;
    $(".qso-mode").dataset.side = side;
    paintDial();
  }

  function paintDial() {
    const mhz = parseFloat(val(".qso-mhz")), side = $(".qso-mode").dataset.side || "";
    $(".qso-dial").textContent = Number.isFinite(mhz)
      ? `${mhz.toFixed(5)} MHz · ${bandOf(mhz) || "out of band"} · ${val(".qso-mode")}${side ? ` (${side})` : ""}` : "";
    const cw = val(".qso-mode") === "CW";
    for (const sel of [".qso-rsts", ".qso-rstr"]) {
      const el = $(sel);
      if (!typed.has(sel) && (el.value === "" || el.value === "59" || el.value === "599")) el.value = cw ? "599" : "59";
    }
  }

  function render() {
    if (!body) return;
    if (!snap.rigs) { body.innerHTML = `<div class="qso st-empty">Switched off.</div>`; return; }
    const my = snap.my || {};
    const cur = (rig() || {}).id;
    const radios = (snap.rigs || []).map((r) =>
      `<option value="${esc(r.id)}"${r.id === cur ? " selected" : ""}>${esc(r.name || r.id)}${r.present ? "" : " (absent)"}</option>`).join("");
    const mine = [my.my_pota, my.my_sota].filter(Boolean).join(" · ");
    body.innerHTML = `<div class="qso">
      ${snap.logsync ? "" : `<div class="qso-warn">The logbook sync isn't running on this station: a QSO can't be logged here.</div>`}
      <div class="qso-row">
        <label>Radio <select class="qso-radio">${radios}</select></label>
        <label class="qso-chk"><input type="checkbox" class="qso-follow" checked> Follow the radio</label>
        <span class="qso-dial stat"></span>
      </div>
      <div class="qso-row">
        <label>Call <input class="qso-call" autocomplete="off" spellcheck="false" maxlength="16" placeholder="N9ABC"></label>
        <label>MHz <input class="qso-mhz" inputmode="decimal" size="10" disabled></label>
        <label>Mode <select class="qso-mode" disabled>${MODES.map((m) => `<option>${m}</option>`).join("")}</select></label>
        <label>Sent <input class="qso-rsts" size="4" maxlength="6" value="59"></label>
        <label>Rcvd <input class="qso-rstr" size="4" maxlength="6" value="59"></label>
        <label>On (UTC) <input class="qso-ton" size="8" maxlength="8" placeholder="HH:MM"></label>
      </div>
      <div class="qso-card stat"></div>
      <div class="qso-row">
        <label>Name <input class="qso-f" data-k="name" maxlength="60"></label>
        <label>QTH <input class="qso-f" data-k="qth" maxlength="60"></label>
        <label>State <input class="qso-f" data-k="state" size="4" maxlength="20"></label>
        <label>Grid <input class="qso-f" data-k="grid" size="7" maxlength="8"></label>
      </div>
      <div class="qso-row">
        <label>Their POTA <input class="qso-f" data-k="pota" size="16" placeholder="US-1234" maxlength="60"></label>
        <span class="qso-spots"></span>
        <label>Their SOTA <input class="qso-f" data-k="sota" size="12" placeholder="W1/HA-001" maxlength="14"
          title="Typed in: SOTA spots aren't fetched (the SOTA API's terms)"></label>
      </div>
      <div class="qso-row">
        <label class="qso-wide">Comment <input class="qso-f" data-k="comment" maxlength="200"></label>
      </div>
      <details class="qso-my"${mine ? " open" : ""}><summary class="stat">My station${mine ? `: ${esc(mine)}` : ""}</summary>
        <div class="qso-row">
          <label>My POTA <input class="qso-my-f" data-k="my_pota" size="16" value="${esc(my.my_pota || "")}" placeholder="US-1234"></label>
          <label>My SOTA <input class="qso-my-f" data-k="my_sota" size="12" value="${esc(my.my_sota || "")}" placeholder="W1/HA-001"></label>
          <label>My grid <input class="qso-my-f" data-k="my_grid" size="7" value="${esc(my.my_grid || "")}" placeholder="FN31"></label>
          <span class="stat">Kept for every QSO until you clear them.</span>
        </div>
      </details>
      <div class="qso-row">
        <button class="chip qso-log" ${snap.logsync ? "" : "disabled"} title="Log it (Ctrl+Enter)">Log</button>
        <button class="chip chip-mini qso-clear">Clear</button>
        <span class="qso-status stat"></span>
        <span class="qso-sp"></span>
        <a href="#" class="qso-login-link stat">QRZ login</a>
      </div>
      <div class="qso-login" hidden></div>
      <div class="qso-done"></div>
    </div>`;
    wire();
    follow();
    paintDial();
    paintLogin();
    paintDone();
  }

  function wire() {
    $(".qso-radio").onchange = follow;
    $(".qso-follow").onchange = () => {
      const on = $(".qso-follow").checked;
      $(".qso-mhz").disabled = on; $(".qso-mode").disabled = on;
      if (on) follow();
    };
    $(".qso-mhz").oninput = paintDial;
    $(".qso-mode").onchange = () => { $(".qso-mode").dataset.side = ""; paintDial(); };
    let t = null;
    $(".qso-call").oninput = () => {
      const c = $(".qso-call"), ton = $(".qso-ton");
      c.value = c.value.toUpperCase().replace(/\s/g, "");
      if (c.value && !ton.dataset.auto) { ton.dataset.auto = utcNow(); ton.placeholder = ton.dataset.auto.slice(0, 5); }
      if (!c.value) { delete ton.dataset.auto; ton.placeholder = "HH:MM"; }
      clearTimeout(t);
      t = setTimeout(lookup, 600);
    };
    $(".qso-call").onkeydown = (e) => { if (e.key === "Enter" && !e.ctrlKey && !e.metaKey) { clearTimeout(t); lookup(); } };
    for (const el of body.querySelectorAll(".qso-f, .qso-rsts, .qso-rstr")) {
      el.addEventListener("input", () => typed.add(el.dataset.k ? `f:${el.dataset.k}` : `.${el.className.split(" ")[0]}`));
    }
    for (const el of body.querySelectorAll(".qso-my-f")) {
      el.onchange = () => api.send("my", { [el.dataset.k]: el.value });
    }
    body.querySelector(".qso").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) logIt(); });
    $(".qso-log").onclick = logIt;
    $(".qso-clear").onclick = clear;
    $(".qso-login-link").onclick = toggleLogin;
  }

  function lookup() {
    const call = val(".qso-call");
    if (call.length < 3 || !/^[A-Z0-9/]+$/.test(call) || !/[0-9]/.test(call) || !/[A-Z]/.test(call)) {
      want = null; $(".qso-card").innerHTML = ""; return;
    }
    const mhz = parseFloat(val(".qso-mhz")), id = `q${++seq}`;
    want = id;
    $(".qso-card").innerHTML = `<span class="cc-dim">Looking up ${esc(call)}…</span>`;
    api.send("lookup", { id, call, khz: Number.isFinite(mhz) ? mhz * 1000 : "", band: bandOf(mhz), mode: val(".qso-mode") });
  }

  function setField(k, v) {
    const el = body.querySelector(`.qso-f[data-k="${k}"]`);
    if (el && !typed.has(`f:${k}`)) el.value = v || "";
  }

  function answer(a) {
    if (!body || a.id !== want) return;               // late: the call has changed since
    const card = $(".qso-card");
    if (a.error) { card.innerHTML = `<span class="cc-err">${esc(a.error)}</span>`; return; }
    const q = a.qrz || {};
    for (const k of ["name", "qth", "state", "grid"]) setField(k, q[k]);
    setField("pota", a.pota_ref);
    body.dataset.country = q.country || (a.dxcc && a.dxcc.name) || "";
    body.dataset.county = q.county || "";
    body.dataset.dxcc = String(q.dxcc || (a.dxcc && a.dxcc.dxcc) || "");
    const qs = a.qsos || [], conf = qs.filter((r) => r.confirmed).length;
    const parts = [
      a.dxcc ? `${esc(a.dxcc.name)} (${esc(a.dxcc.cont)}, CQ ${esc(a.dxcc.cq)})` : "",
      qs.length ? `worked ${qs.length}${qs.length >= 20 ? "+" : ""}× · ${conf} confirmed · last ${esc(qs[0].date)} ${esc(qs[0].band)} ${esc(qs[0].mode)}`
        : "not in the logbook",
      a.need && a.need.level ? `<span class="cc-need">${esc(NEED[a.need.level] || a.need.level)}</span>` : "",
    ].filter(Boolean);
    const says = QRZ_SAYS[a.qrz_state] ?? "";
    card.innerHTML = `<b>${esc(a.call)}</b>${q.name ? ` · ${esc(q.name)}` : ""} · ${parts.join(" · ")}`
      + (says ? `<div class="cc-dim">${esc(says)}</div>` : "");
    const age = a.spots_age_s, old = age != null && age > 600;
    $(".qso-spots").innerHTML = (a.pota || []).map((s) =>
      `<button class="chip chip-mini qso-park${s.on_freq ? " qso-on" : ""}" data-ref="${esc(s.ref)}"
        title="${esc(s.name)} (${esc(s.loc)}), spotted ${Math.round(s.age_s / 60)} min ago on ${esc(s.khz)} kHz ${esc(s.mode)}">${esc(s.ref)}${s.on_freq ? " ●" : ""}</button>`).join("")
      + (old ? `<span class="cc-dim"> spots ${Math.round(age / 60)} min old</span>` : "")
      + (age == null && snap.spots && snap.spots.on ? `<span class="cc-dim"> no POTA spots yet</span>` : "");
    for (const b of body.querySelectorAll(".qso-park")) {
      b.onclick = () => { body.querySelector(`.qso-f[data-k="pota"]`).value = b.dataset.ref; typed.add("f:pota"); };
    }
    if (a.qrz_login) { snap.qrz = a.qrz_login; paintLogin(); }
  }

  function form() {
    const ton = $(".qso-ton");
    const f = {
      call: val(".qso-call"), mhz: val(".qso-mhz"), mode: val(".qso-mode"), sideband: $(".qso-mode").dataset.side || "",
      time_on: ton.value.trim() || ton.dataset.auto || "", rst_sent: val(".qso-rsts"), rst_rcvd: val(".qso-rstr"),
      country: body.dataset.country || "", county: body.dataset.county || "", dxcc: body.dataset.dxcc || "",
    };
    for (const el of body.querySelectorAll(".qso-f, .qso-my-f")) f[el.dataset.k] = el.value.trim();
    return f;
  }

  function logIt() {
    if (!snap.logsync || !val(".qso-call")) return;
    $(".qso-status").textContent = "Logging…";
    api.send("log", { id: `l${++seq}`, qso: form() });
  }

  function clear() {
    $(".qso-call").value = "";
    for (const el of body.querySelectorAll(".qso-f")) el.value = "";
    const ton = $(".qso-ton");
    ton.value = ""; delete ton.dataset.auto; ton.placeholder = "HH:MM";
    typed = new Set(); want = null;
    $(".qso-card").innerHTML = ""; $(".qso-spots").innerHTML = "";
    delete body.dataset.country; delete body.dataset.county; delete body.dataset.dxcc;
    paintDial();
    $(".qso-call").focus();
  }

  function onLogged(a) {
    if (!body) return;
    if (a.error) { $(".qso-status").innerHTML = `<span class="cc-err">${esc(a.error)}</span>`; return; }
    const same = a.what === "same";
    if (!same) { logged.unshift(a); logged.length = Math.min(logged.length, 30); }
    toast(same ? `${a.call}: already in the log` : `Logged ${a.call}`);
    clear();
    $(".qso-status").textContent = same ? `${a.call} was already in the log.` : `Logged ${a.call} ${a.band} ${a.mode}: waiting to send.`;
    paintDone();
  }

  function paintDone() {
    const el = $(".qso-done");
    if (!el) return;
    el.innerHTML = logged.length ? `<table class="qso-q"><tr><th>UTC</th><th>Call</th><th>Band</th><th>Mode</th></tr>${logged.map((q) =>
      `<tr><td>${esc(q.time)}</td><td>${esc(q.call)}</td><td>${esc(q.band)}</td><td>${esc(q.mode)}</td></tr>`).join("")}</table>
      <div class="stat">The logbook sync sends them on; each one's state is on the Logbook tab.</div>` : "";
  }

  // The login form: always open while there is no working login (no password,
  // or QRZ refused it), and then the link is hidden, since it would toggle
  // nothing (2026-11-27: the link did nothing while the form was forced open).
  // With a working login the form is closed and the link opens it to change
  // the login, with the cursor in the password box.
  function paintLogin(err) {
    const el = $(".qso-login"), link = $(".qso-login-link");
    if (!el) return;
    const q = snap.qrz || {};
    const need = !q.set || q.state === "no_login" || q.state === "bad_login";
    if (link) {
      link.hidden = need;
      link.textContent = showLogin ? "Close QRZ login" : "Change QRZ login";
    }
    el.hidden = !(need || showLogin || err);
    if (el.hidden) return;
    const state = q.state === "ok" ? `<span class="qso-ok">Logged in as ${esc(q.user)}.</span>`
      : `<span>${esc(QRZ_SAYS[q.state] || "Not checked yet.")}</span>`;
    el.innerHTML = `<div class="qso-grp"><div class="stat">QRZ lookup (name, QTH and grid; needs a QRZ XML subscription). ${state}</div>
      <div class="qso-row">
        <label>Username <input class="qso-qu" autocomplete="username" value="${esc(q.user || "")}" size="10"></label>
        <label>Password <input class="qso-qp" type="password" autocomplete="current-password" size="16"></label>
        <button class="chip chip-mini qso-qgo">Save and test</button>
        ${err ? `<span class="cc-err">${esc(err)}</span>` : ""}
      </div>
      <div class="stat">The password is kept on this station only (mode 600) and never shown again.</div></div>`;
    const go = () => {
      const p = el.querySelector(".qso-qp"), password = p.value;
      p.value = "";
      api.send("login", { user: el.querySelector(".qso-qu").value.trim(), password });
    };
    el.querySelector(".qso-qgo").onclick = go;
    el.querySelector(".qso-qp").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } };
  }

  function toggleLogin(e) {
    e.preventDefault();
    showLogin = !showLogin;
    paintLogin();
    const p = showLogin && $(".qso-qp");
    if (p) { p.scrollIntoView({ block: "nearest" }); p.focus(); }
  }

  addons.register("qso", {
    mount(b, a) { body = b; api = a; render(); },
    hello(s) { snap = s || {}; render(); },
    event(m) {
      if (m.event === "rigs") { snap.rigs = m.rigs; follow(); }
      else if (m.event === "lookup") answer(m);
      else if (m.event === "logged") onLogged(m);
      else if (m.event === "login") {
        snap.qrz = m.qrz_login || snap.qrz;
        if (!m.error && snap.qrz.state === "ok") showLogin = false;
        paintLogin(m.error);
        if (!m.error) toast(snap.qrz.state === "ok" ? "QRZ: logged in" : `QRZ: ${QRZ_SAYS[snap.qrz.state] || snap.qrz.state}`);
      } else if (m.event === "my") snap.my = m.my;
    },
    off() { snap = {}; render(); },
    tab() {
      const q = snap.qrz || {};
      const title = !snap.logsync ? "Log a contact: no logbook sync on this station"
        : q.state === "bad_login" ? "Log a contact (QRZ refused the login)" : "Log a contact";
      return { dot: snap.logsync ? "ok" : "off", title, ...(logged.length ? { badge: logged.length } : {}) };
    },
    card() {
      return { state: snap.logsync ? "on" : "off", lines: logged.length ? [`${logged.length} logged this session`] : [] };
    },
  });
})();
