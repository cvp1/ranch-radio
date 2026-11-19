"use strict";

// Winlink panel: mailbox, compose, gateway list and sessions, with Pat as the
// engine behind the server. Mail goes through /winlink/api (an allow-list of
// Pat's API); sessions start and stop over the control WebSocket, so the
// server can tie the transmit lane to this open page. Uses app.js's $, send,
// toast, chip, token and BANDS.

const winlink = (() => {
  const LOG_KEEP = 300;
  const CONFIRM_MS = 6000;
  let box = "in", armed = false, session = null, confirmTimer = null;
  let off = false;          // the Winlink add-on is switched off: Pat isn't running
  let blocked = null;       // Pat not launched: its config would listen/beacon/schedule (the server says why)
  let inbound = null;       // {call, since}: Pat is in a session another station started
  // A listen window (plan winlink-listen): {plan, window, busy}; null when the
  // host file sets none up (the row stays hidden).
  let listen = null, listenTimer = null;
  let modems = ["ardop"];   // HF modem schemes a peer session may use (the server's `modems`)
  const MODEM_LABEL = { ardop: "ARDOP", varahf: "VARA HF" };
  const isHF = (kind) => kind in MODEM_LABEL;
  const PLACEHOLDER = { ardop: "ardop:///CALL?bw=2000MAX&freq=kHz", varahf: "varahf:///CALL?bw=500&freq=kHz" };
  // The bandwidths Pat accepts in a peer URL's bw= (Hz); none: the modem's default.
  const PEER_BW = { varahf: [500, 2300, 2750], ardop: [200, 500, 1000, 2000] };
  let mode = store.get("wl.mode", "gateway");   // "gateway" | "peer" (P2P)
  // The message in the read pane: {box, mid, m}. Every action reads it from
  // here, never from the DOM. readGen drops a read() answer that comes back
  // after the operator moved on; fwdGen does the same for Forward's fetches.
  let current = null, readGen = 0, fwdGen = 0, boxGen = 0, busy = false;
  let pending = null, delTimer = null;   // Delete's own two-click confirm, bound to {box, mid}

  // The thrown message is what the page shows: the server's sentence, or the
  // "error" of a JSON body (the 503 while switched off).
  const api = (path, opts) => fetch(path + (token ? `${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}` : ""), opts)
    .then(async (r) => {
      if (!r.ok) {
        const text = (await r.text()).trim();
        let msg = text;
        try { const j = JSON.parse(text); if (typeof j?.error === "string") msg = j.error; } catch { /* plain text */ }
        throw new Error(msg || `HTTP ${r.status}`);
      }
      return r;
    });
  const addrList = (list) => (list || []).map((a) => a?.Addr).filter(Boolean);   // null-safe: Pat sends Cc: null
  const addrs = (list) => addrList(list).join(", ");
  const when = (d) => (d ? new Date(d).toISOString().slice(0, 16).replace("T", " ") : "");
  const msgUrl = (b, mid) => `${BASE}winlink/api/mailbox/${b}/${encodeURIComponent(mid)}`;
  const isCurrent = (b, mid) => current?.mid === mid && current.box === b;
  const ACTIONS = ["#wl-reply", "#wl-reply-all", "#wl-forward", "#wl-archive", "#wl-delete"];

  // ---------- mailbox

  async function loadBox(b = box) {
    box = b;
    const g = ++boxGen;   // a late answer for a box no longer shown is dropped
    readGen++;
    current = null;
    resetDelete();
    renderActions();
    for (const c of $("#wl-boxes").children) on(c, c.dataset.v === box);
    $("#wl-read").hidden = true;
    const list = $("#wl-list");
    try {
      const msgs = await (await api(`${BASE}winlink/api/mailbox/${box}`)).json();
      if (g !== boxGen) return;
      msgs.sort((a, b) => String(b.Date).localeCompare(String(a.Date)));
      list.innerHTML = msgs.length ? "" : `<div class="wl-empty">No messages</div>`;
      for (const m of msgs) {
        const row = document.createElement("button");
        row.className = "wl-msg" + (m.Unread ? " unread" : "");
        row.dataset.mid = m.MID;
        const who = box === "in" ? m.From?.Addr : addrs(m.To);
        row.innerHTML = `<span class="wl-who">${esc(who)}</span><span class="wl-subj">${p2pBadge(m)}${esc(m.Subject)}</span>` +
          `<span class="wl-date">${esc(when(m.Date))}</span>`;
        row.onclick = () => read(m.MID, row);
        list.append(row);
      }
      if (box === "in") setUnread(msgs);
      if (box === "out") setQueue(msgs);
    } catch (e) {
      if (g !== boxGen) return;
      list.innerHTML = `<div class="wl-empty">Mailbox unavailable: ${esc(e.message)}</div>`;
    }
    if (g === boxGen) refreshCounts(box);
  }

  // ---------- the strip's counts and the queue line (the console layout, 2026-11-18)
  // What is waiting to go and what is unread, whichever box is open: the
  // other box is read in the background. A failed read shows "?", never 0.
  let countsGen = 0;
  async function refreshCounts(skip) {
    if (off) { setUnread(null); setQueue(null); return; }
    const g = ++countsGen;
    for (const b of ["in", "out"]) {
      if (b === skip) continue;
      try {
        const msgs = await (await api(`${BASE}winlink/api/mailbox/${b}`)).json();
        if (g !== countsGen) return;
        (b === "in" ? setUnread : setQueue)(msgs);
      } catch {
        if (g === countsGen) (b === "in" ? setUnread : setQueue)(undefined);
      }
    }
  }

  function boxCount(b, n) {
    const c = $(`#wl-boxes [data-v="${b}"]`);
    if (!c) return;
    let el = c.querySelector(".wl-count");
    if (!n) { el?.remove(); return; }
    if (!el) { el = document.createElement("span"); el.className = "wl-count"; c.append(el); }
    el.textContent = String(n);
  }

  function setUnread(msgs) {
    const n = Array.isArray(msgs) ? msgs.filter((m) => m.Unread).length : null;
    $("#wl-strip-unread").textContent = msgs === null ? "—" : n == null ? "?" : String(n);
    $("#wl-pill-unread").classList.toggle("wl-pill-hot", !!n);
    boxCount("in", n);
  }

  function setQueue(msgs) {
    const q = $("#wl-queue"), t = $("#wl-queue-text");
    if (!Array.isArray(msgs)) {
      $("#wl-strip-queue").textContent = msgs === null ? "—" : "?";
      t.textContent = msgs === null ? "—" : "outbox unreadable";
      q.classList.remove("wl-queue-some");
      boxCount("out", 0);
      return;
    }
    const n = msgs.length, p2p = msgs.filter((m) => m.P2POnly).length;
    const to = [...new Set(msgs.flatMap((m) => addrList(m.To)))];
    $("#wl-strip-queue").textContent = n ? `${n} waiting` : "empty";
    $("#wl-pill-queue").classList.toggle("wl-pill-hot", n > 0);
    t.textContent = n ? `${n} message${n > 1 ? "s" : ""} · to ${to.slice(0, 4).join(", ")}${to.length > 4 ? ` +${to.length - 4}` : ""}` +
      (p2p ? ` · ${p2p} P2P only (a peer session with its addressee)` : "")
      : "Nothing waiting · Compose queues a message for the next session";
    q.classList.toggle("wl-queue-some", n > 0);
    boxCount("out", n);
  }

  async function read(mid, row) {
    const b = box, g = ++readGen;   // captured before the await: a late answer is dropped
    fwdGen++;
    resetDelete();
    try {
      const m = await (await api(msgUrl(b, mid))).json();
      if (g !== readGen || b !== box) return;
      current = { box: b, mid, m };
      resetDelete();
      $("#wl-read-head").innerHTML =
        `<b>${p2pBadge(m)}${esc(m.Subject)}</b><span>From ${esc(m.From?.Addr)} · To ${esc(addrs(m.To))}` +
        `${m.Cc?.length ? ` · Cc ${esc(addrs(m.Cc))}` : ""} · ${esc(when(m.Date))}</span>` +
        (m.Files?.length ? `<span>Attachments: ${m.Files.map((f) =>
          `<a href="${msgUrl(b, mid)}/${encodeURIComponent(f.Name)}${token ? `?token=${encodeURIComponent(token)}` : ""}" target="_blank">${esc(f.Name)}</a>`).join(", ")}</span>` : "");
      $("#wl-body").textContent = m.Body || "";
      $("#wl-compose").hidden = true;
      $("#wl-read").hidden = false;
      renderActions();
      if (m.Unread && b === "in") markRead(b, mid);
    } catch (e) {
      if (g !== readGen || b !== box) return;
      toast(`Could not open the message: ${e.message}`, "warn");
    }
  }

  // Marked read as soon as it opens (Pat's own viewer waits 2 s, then marks it
  // anyway); the row loses its bold only once Pat said yes, so a failure shows.
  async function markRead(b, mid) {
    try {
      await api(`${msgUrl(b, mid)}/read`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ read: true }) });
      if (b === box) $("#wl-list").querySelector(`.wl-msg[data-mid="${CSS.escape(mid)}"]`)?.classList.remove("unread");
      if (isCurrent(b, mid)) current.m.Unread = false;
      if (b === "in") refreshCounts();      // the strip's Unread and the Inbox count
    } catch (e) {
      toast(`Could not mark it read: ${e.message}`, "warn");
    }
  }

  function openCompose(show = true) {
    resetDelete();
    $("#wl-compose").hidden = !show;
    if (show) { $("#wl-read").hidden = true; $("#wl-to").focus(); }
  }

  function setNote(text) {
    const n = $("#wl-compose-note");
    n.textContent = text || "";
    n.hidden = !text;
  }

  // Pat's listing says P2POnly (its X-P2POnly header): such a message goes only
  // in a peer session with the station it is addressed to, never to a gateway.
  const p2pBadge = (m) => (m?.P2POnly ? `<span class="wl-badge" title="P2P only: never via the Winlink servers">P2P</span>` : "");

  // ---------- reply, reply all, forward (Pat's conventions: its composer and viewer)

  // Pat's _quoteMsg: the raw Date, '>' before every line. CRLF normalised
  // first, so the posted body (CRLF again in submitCompose) never has \r\r\n.
  function quote(m) {
    const lines = (m.Body || "").replace(/\r\n?/g, "\n").split("\n");
    return `--- ${m.Date || ""} ${m.From?.Addr || ""} wrote: ---\n` + lines.map((l) => `>${l}`).join("\n") + "\n";
  }

  function prefill({ to = "", cc = "", subject = "", body = "", files = [], note = "", p2ponly = false }) {
    $("#wl-compose").reset();
    $("#wl-p2ponly").checked = !!p2ponly;
    $("#wl-to").value = to;
    $("#wl-cc").value = cc;
    $("#wl-subject").value = subject;
    $("#wl-text").value = body;
    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    $("#wl-files").files = dt.files;
    setNote(note);
    openCompose(true);
    if (to) {
      $("#wl-text").focus();
      $("#wl-text").setSelectionRange(0, 0);
    } else {
      $("#wl-to").focus();
    }
  }

  // Re: unless it already starts with re: in any case (Winlink Express sends
  // "RE:"; Pat's check is case-sensitive and would make "Re: RE: ...").
  const reSubject = (s) => (/^re:/i.test(s) ? s : `Re: ${s}`);

  function reply(all) {
    resetDelete();
    if (!current) return;
    const { m } = current;
    const from = m.From?.Addr || "";
    let cc = "";
    if (all) {
      const skip = new Set([($("#wl-mycall").textContent || "").trim().toUpperCase(), from.toUpperCase()]);
      const seen = new Set();
      cc = addrList(m.To).concat(addrList(m.Cc)).filter((a) => {
        const k = a.toUpperCase();
        if (skip.has(k) || seen.has(k)) return false;
        seen.add(k);
        return true;
      }).join(" ");
    }
    prefill({ to: from, cc, subject: reSubject(m.Subject || ""), body: "\n\n" + quote(m), p2ponly: m.P2POnly });
  }

  // Forward re-attaches the original's files, fetched through the API (token
  // kept). A file that can't be read is left off with a note that stays in
  // the form; Pat attaches nothing if any one fetch fails, which is harsher.
  async function forward() {
    resetDelete();
    if (!current) return;
    const { box: b, mid, m } = current;
    const g = ++fwdGen;
    const files = [], failed = [];
    if (m.Files?.length) { busy = true; renderActions(); }
    try {
      for (const f of m.Files || []) {
        try {
          const blob = await (await api(`${msgUrl(b, mid)}/${encodeURIComponent(f.Name)}`)).blob();
          files.push(new File([blob], f.Name, { type: blob.type }));
        } catch {
          failed.push(f.Name);
        }
      }
    } finally {
      busy = false;
      renderActions();
    }
    // Obsolete: another message opened, the box changed, Compose pressed, or switched off.
    if (g !== fwdGen || off || !isCurrent(b, mid)) return;
    const note = failed.length ? `Forward: ${failed.join(", ")} could not be read; not attached` : "";
    if (note) toast(note, "warn");
    prefill({ subject: `Fw: ${m.Subject || ""}`, body: quote(m), files, note, p2ponly: m.P2POnly });
  }

  // ---------- archive, delete

  async function change(b, mid, done, failed, call) {
    busy = true;
    renderActions();
    try {
      await call();
      toast(done);
      // Only if the view is still that message; otherwise the toast says it.
      // A listing that then fails shows "Mailbox unavailable": both are true.
      if (isCurrent(b, mid)) {
        $("#wl-read").hidden = true;
        current = null;
        loadBox(b);
      }
    } catch (e) {
      toast(`${failed}: ${e.message}`, "warn");
    } finally {
      busy = false;
      renderActions();
    }
  }

  function archive() {
    resetDelete();
    if (!current || busy) return;
    const { box: b, mid } = current;
    change(b, mid, "Archived", "Not archived", () => api(`${BASE}winlink/api/mailbox/archive`,
      { method: "POST", headers: { "X-Pat-SourcePath": `/api/mailbox/${b}/${encodeURIComponent(mid)}` } }));
  }

  function resetDelete() {
    clearTimeout(delTimer);
    delTimer = pending = null;
    const d = $("#wl-delete");
    d.textContent = "Delete";
    d.classList.remove("wl-confirm");
  }

  // Two clicks, like Connect, with its own timer. The first binds {box, mid};
  // the second deletes only if the pane still shows that message.
  function deleteClick() {
    if (!current || busy) { resetDelete(); return; }
    const { box: b, mid } = current;
    if (!pending) {
      pending = { box: b, mid };
      const d = $("#wl-delete");
      d.textContent = b === "out" ? "Confirm: delete from the outbox — it will not be sent" : "Confirm: delete";
      d.classList.add("wl-confirm");
      delTimer = setTimeout(resetDelete, CONFIRM_MS);
      return;
    }
    const p = pending;
    resetDelete();
    if (p.box !== b || p.mid !== mid) return;
    change(b, mid, "Deleted", "Not deleted", () => api(msgUrl(b, mid), { method: "DELETE" }));
  }

  // Which actions the open message offers: Archive is hidden in Archive and
  // in the Outbox (archiving an outbox message would silently unsend it; Delete
  // says so). While a session runs (or a local radio arms for one) the outbox
  // is Pat's: Delete there is disabled (the server refuses it too).
  function renderActions() {
    const b = current?.box;
    const listening = !!(listen && (listen.window || listen.busy));
    const locked = b === "out" && (armed || !!session || !!inbound || listening);
    for (const s of ACTIONS) $(s).disabled = off || busy || !current;
    $("#wl-archive").hidden = b === "archive" || b === "out";
    for (const s of ["#wl-delete", "#wl-archive"]) {
      if (locked) $(s).disabled = true;
      $(s).title = locked ? (inbound && !session ? `The outbox is Pat's while it is in a session with ${inbound.call}`
        : listening && !session ? "The outbox is Pat's while it listens: a station that calls may take it"
          : "The outbox is Pat's while a session runs") : "";
    }
    if (locked && pending) resetDelete();
  }

  async function submitCompose(e) {
    e.preventDefault();
    const form = new FormData();
    form.append("to", $("#wl-to").value.trim());
    const cc = $("#wl-cc").value.trim();
    if (cc) form.append("cc", cc);
    form.append("subject", $("#wl-subject").value.trim());
    form.append("body", $("#wl-text").value.replace(/\r?\n/g, "\r\n"));
    if ($("#wl-p2ponly").checked) form.append("p2ponly", "true");   // Pat: X-P2POnly, never to a gateway
    for (const f of $("#wl-files").files) form.append("files", f, f.name);
    try {
      await api(`${BASE}winlink/api/mailbox/out`, { method: "POST", body: form });
      $("#wl-compose").reset();
      setNote("");
      openCompose(false);
      toast("Queued in the outbox; it goes out on the next session.");
      loadBox("out");
    } catch (err) {
      toast(`Not queued: ${err.message}`, "warn");
    }
  }

  // ---------- one-click Connect on a station row (the console layout, 2026-11-18)
  // The row's Connect picks the station and presses Connect once: the main
  // button then asks for its confirm, naming the radio and frequency. It never
  // transmits by itself; the second click is still the operator's.
  const QUICK = `<span class="wl-quick" role="button" tabindex="0" title="Pick this station and ask to transmit (Connect confirms)">Connect</span>`;
  function quick(row, pick) {
    const q = row.querySelector(".wl-quick");
    if (!q) return;
    const go = (ev) => {
      ev.stopPropagation();
      ev.preventDefault();
      if (session || armed || off || blocked) return;
      pick();
      resetConfirm();
      connectClick();
      $("#wl-connect").focus({ preventScroll: true });
    };
    q.onclick = go;
    q.onkeydown = (ev) => { if (ev.key === "Enter" || ev.key === " ") go(ev); };
  }

  // ---------- the status strip: where the next session goes, and to whom
  function targetOf(url) {
    if (!url) return "";
    const t = /^telnet:\/\/[^@]*@([^/]+)\/?(.*)$/.exec(url);
    if (t) return t[2] ? `${decodeURIComponent(t[2])} (telnet ${t[1]})` : `CMS (${t[1]})`;
    const m = /^[a-z0-9+]+:\/\/\/([^?/]+)/i.exec(url);
    if (!m) return url;
    const f = freqOf(url);
    return decodeURIComponent(m[1]) + (f ? ` · ${f}` : "");
  }
  function renderStrip() {
    const via = vias.find((v) => v.id === viaSel()?.value);
    $("#wl-strip-via").textContent = via ? `${via.label}${mode === "peer" ? " · peer" : ""}` : "—";
    const url = session || $("#wl-url").value.trim();
    $("#wl-strip-target").textContent = targetOf(url) || (mode === "peer" ? "pick a station" : "pick a gateway");
    $("#wl-pill-target").classList.toggle("wl-pill-hot", !!session);
    renderModem();
  }

  // ---------- the modem's own state (modem_status.py on the radio's server,
  // read from ardopcf's and xara's logs; relayed by the station). Which modem:
  // the running session's scheme, else the via's. Every reading shows its
  // age once it is old: a number from an hour ago is not the link now.
  let modemSnap = null, modemTimer = null, lastId = null;   // lastId: the ID ledger's snapshot (which logs are followed)
  const MODEM_FRESH_S = 90;          // ago() says "just now" below this
  const STATE_LABEL = { ISS: "sending (ISS)", IRS: "receiving (IRS)", IRStoISS: "turning over",
    IDLE: "linked, idle", DISC: "not linked", CONNECTED: "linked", DISCONNECTED: "not linked" };
  function modemKind() {
    const sch = session ? String(session).split(":")[0] : viaKind();
    return isHF(sch) ? sch : null;
  }
  const ageText = (t) => {
    if (t == null) return "";
    const s = Math.max(0, Date.now() / 1000 - t);
    return s < MODEM_FRESH_S ? "" : ` · ${ago(s)}`;
  };
  // The Modem pill's text is #wl-m-state; the rest share their pill's name.
  // A missing element skips that pill: the strip must never take the page
  // down with it (2026-11-18: a wrong id threw in hello and the station page
  // lost its tabs).
  function setPill(id, text, cls = {}) {
    const span = $(`#wl-m-${id === "modem" ? "state" : id}`), pill = $(`#wl-pill-${id}`);
    if (!span || !pill) return;
    span.textContent = text;
    for (const c of ["wl-pill-stale", "wl-pill-warm", "wl-pill-tx", "wl-pill-off", "wl-pill-hot"]) pill.classList.remove(c);
    for (const [c, on] of Object.entries(cls)) if (on) pill.classList.add(`wl-pill-${c}`);
  }
  function renderModem() {
    const group = $("#wl-modem");
    const kind = modemKind();
    group.hidden = !kind || off;
    clearInterval(modemTimer);
    modemTimer = null;
    if (group.hidden) return;
    const r = modemSnap?.[kind];
    const name = MODEM_LABEL[kind];
    if (!r) {
      // No record. Three reasons, each said as itself, never blank-as-fine
      // (2026-11-19: a radio server still on old code read "not tracked",
      // blaming a flag that was set):
      //   modemSnap null: the radio's server sends no modem status (older code);
      //   the ID ledger follows this modem's log: nothing read from it yet;
      //   otherwise: its log is not followed.
      const tracked = !!lastId?.tracked && (!Array.isArray(lastId.tracked_modems) || lastId.tracked_modems.includes(kind));
      const [word, why] = modemSnap === null
        ? ["no status", `The radio's server sends no modem status: it runs older code. ranch-radio update restarts it once it is idle.`]
        : tracked ? ["waiting", `Following ${name}'s log; nothing read from it yet (it writes on its next frame, busy change or key-up).`]
          : ["not tracked", `No status from ${name}: start the radio's server with ${ID_FLAG[kind]}`];
      setPill("modem", `${name} · ${word}`, { off: true });
      for (const id of ["link", "snr", "speed", "busy", "ptt"]) setPill(id, "—", { off: true });
      $("#wl-pill-modem").title = why;
      return;
    }
    $("#wl-pill-modem").title = "The modem the next session uses, and its link state (from its own log)";
    setPill("modem", `${name} · ${STATE_LABEL[r.state] || (r.state ? r.state : "no state yet")}`, { hot: r.linked });
    setPill("link", r.linked ? `${r.partner || "?"}${r.bw_hz ? ` · ${r.bw_hz} Hz` : ""}`
      : r.partner ? `last ${r.partner}` : "—", { hot: r.linked, off: !r.linked });
    const snrOld = r.snr_at == null || Date.now() / 1000 - r.snr_at >= MODEM_FRESH_S;
    setPill("snr", r.snr_db == null ? "—" : `${r.snr_db > 0 ? "+" : ""}${r.snr_db} dB${ageText(r.snr_at)}`, { stale: snrOld });
    let speed = "—";
    if (kind === "ardop" && r.frame) {
      const [mod, bw] = r.frame.split(".");
      speed = `${mod} ${bw} Hz${r.quality != null ? ` · Q ${r.quality}` : ""}`;
    } else if (kind === "varahf" && r.level != null) {
      speed = `L${r.level} · ${r.bps} bps ${r.rate_dir || ""}`.trim();
    }
    const rateOld = r.rate_at == null || Date.now() / 1000 - r.rate_at >= MODEM_FRESH_S;
    setPill("speed", speed === "—" ? speed : speed + ageText(r.rate_at), { stale: rateOld });
    setPill("busy", r.busy == null ? "—" : r.busy ? "YES" : "no", { warm: r.busy, off: r.busy === false });
    setPill("ptt", r.ptt == null ? "—" : r.ptt ? "TX" : "RX", { tx: r.ptt });
    // Ages move with the clock: redraw while anything shown has an age.
    if (r.snr_at != null || r.rate_at != null) modemTimer = setInterval(renderModem, 5000);
  }

  // ---------- gateways

  // Via: which radio a session goes out on. Its kind picks the gateway list:
  // ARDOP (or VARA HF, one via per modem) on an HF radio, packet on the packet hub's radio, or none for
  // telnet. The station page builds the list (station.js: every HF radio, its
  // packet hub, telnet). A radio's own page builds it here: that radio for
  // ARDOP, the packet hub while the Packet add-on reports it up (packet.js
  // calls setPacket), and telnet. The server routes by the URL's transport
  // either way; the list is what the page offers.
  let vias = [];
  let own = null, packet = null;   // this page's radio name; the Packet add-on's snapshot while up
  const viaSel = () => $("#wl-via");
  const viaKind = () => (vias.find((v) => v.id === viaSel()?.value) || { kind: "ardop" }).kind;

  // A server with more than one HF modem (`modems`): one via per modem, and
  // the via decides the scheme, in peer mode too (no separate modem picker).
  const perModem = () => vias.some((v) => v.kind === "varahf");

  function ownVias() {
    if (own == null) return;         // the station page keeps its own list
    const list = [{ id: "radio", name: own, kind: "ardop", label: `HF · ${own} (ARDOP)` }];
    if (modems.includes("varahf")) list.push({ id: "radio-vara", name: own, kind: "varahf", label: `HF · ${own} (VARA HF)` });
    const hub = packet?.hub_name || "UV-PRO";
    if (packet) list.push({ id: "packet", name: hub, kind: "packet", label: `VHF · ${hub} (packet)` });
    list.push({ id: "telnet", name: "internet", kind: "telnet", label: "Telnet (internet, no radio)" });
    setVias(list);
  }

  function ownRadio(name) {          // app.js, on hello and config: this is a radio's own page
    const was = own;
    own = name || "this radio";
    if (own !== was) ownVias();
  }

  function setPacket(snap) {         // addons/packet.js: the hub's state, null when down or off
    const was = packet;
    packet = snap || null;
    if (!!was !== !!packet || (packet && was?.hub_name !== packet.hub_name)) ownVias();
  }

  function setVias(list) {
    const sel = viaSel();
    if (!sel) return;
    const was = sel.value || store.get("wl.via", "");
    vias = list;
    sel.innerHTML = list.map((v) => `<option value="${esc(v.id)}">${esc(v.label)}</option>`).join("")
      || `<option value="">No radio for Winlink</option>`;
    sel.value = list.some((v) => v.id === was) ? was : (list[0]?.id || "");
    viaChanged(false);
  }

  function viaChanged(save = true) {
    if (save && viaSel()?.value) store.set("wl.via", viaSel().value);
    const kind = viaKind();
    const peer = mode === "peer";
    for (const c of $("#wl-mode").children) on(c, c.dataset.v === mode);
    $("#wl-peer").hidden = !peer;
    $("#wl-url").readOnly = peer;
    $("#wl-band").hidden = $("#wl-gw-refresh").hidden = $("#wl-gateways").hidden = kind === "telnet" || peer;
    $("#wl-stations-title").textContent = peer ? "P2P directory" : kind === "packet" ? "Packet gateways" : "Gateways";
    const hint = $("#wl-stations-hint");
    hint.hidden = !(kind === "telnet" && !peer);
    hint.textContent = "Telnet goes straight to the Winlink servers over the internet: no gateway to pick.";
    queueMicrotask(renderStrip);
    if (peer) {
      $("#wl-peer-freq").hidden = kind === "telnet";
      $("#wl-peer-host").hidden = $("#wl-peer-pass").hidden = kind !== "telnet";
      const ms = $("#wl-peer-modem");
      ms.hidden = !isHF(kind) || modems.length < 2 || perModem();
      if (ms.dataset.list !== modems.join()) {
        ms.innerHTML = modems.map((m) => `<option value="${esc(m)}">${esc(MODEM_LABEL[m] || m.toUpperCase())}</option>`).join("");
        ms.dataset.list = modems.join();
      }
      peerChanged();
      renderDirectory();
      return;
    }
    if ($("#wl-url").readOnly === false && $("#wl-url").dataset.peer) { $("#wl-url").value = ""; delete $("#wl-url").dataset.peer; }
    if (kind === "telnet") {
      $("#wl-url").value = `telnet://${$("#wl-mycall").textContent || "MYCALL"}:CMSTelnet@cms.winlink.org:8772/wl2k`;
      resetConfirm();
      return;
    }
    if ($("#wl-url").value.startsWith("telnet://")) { $("#wl-url").value = ""; resetConfirm(); }
    if (isHF(kind)) {
      $("#wl-url").placeholder = PLACEHOLDER[kind];
      // A gateway URL of the other modem is no longer what this via sends.
      const sch = $("#wl-url").value.split(":")[0];
      if (save && isHF(sch) && sch !== kind) { $("#wl-url").value = ""; resetConfirm(); }
    }
    fillBands();
    // The list loads by itself the first time a via's kind is shown (it used
    // to wait for a click on Gateways, an empty pane nobody understood).
    if (save || gwKind !== kind) loadGateways();
  }

  // ---------- peer (P2P): a station, not a gateway

  const peerCall = () => $("#wl-peer-call").value.trim().toUpperCase();

  // The connect URL for a peer, from the via's kind (Pat's URL forms):
  //   packet  ax25+agwpe:///CALL?freq=kHz   (the hub's radio is tuned to it)
  //   HF      <modem>:///CALL?freq=kHz       (a modem the server lists in `modems`)
  //   telnet  telnet://MYCALL:PASS@host:port/CALL   (no freq; Pat dials host:port)
  // show: the password masked, for the page; the real one only in what is sent.
  function peerUrl(show = false) {
    const call = encodeURIComponent(peerCall());
    const kind = viaKind();
    if (kind === "telnet") {
      const me = encodeURIComponent($("#wl-mycall").textContent.trim() || "MYCALL");
      const pass = $("#wl-peer-pass").value;
      const host = $("#wl-peer-host").value.trim();
      return `telnet://${me}${pass ? `:${show ? "***" : encodeURIComponent(pass)}` : ""}@${host}/${call}`;
    }
    const scheme = peerScheme();
    const khz = $("#wl-peer-freq").value.trim();
    const bw = PEER_BW[scheme] ? $("#wl-peer-bw").value : "";
    const q = [bw && `bw=${encodeURIComponent(bw)}`, khz && `freq=${encodeURIComponent(khz)}`].filter(Boolean);
    return `${scheme}:///${call}${q.length ? `?${q.join("&")}` : ""}`;
  }

  function peerScheme() {
    const kind = viaKind();
    return kind === "packet" ? "ax25+agwpe"
      : perModem() ? kind : ($("#wl-peer-modem").value || modems[0] || "ardop");
  }

  // The bandwidth list for the peer's modem; a pick that still fits is kept.
  function fillPeerBw() {
    const sel = $("#wl-peer-bw");
    const list = viaKind() === "telnet" ? null : PEER_BW[peerScheme()];
    sel.hidden = !list;
    const key = (list || []).join();
    if (sel.dataset.list !== key) {
      const keep = sel.value;
      sel.innerHTML = `<option value="">Default bandwidth</option>` +
        (list || []).map((b) => `<option value="${b}">${b} Hz</option>`).join("");
      sel.dataset.list = key;
      sel.value = (list || []).map(String).includes(keep) ? keep : "";
    }
  }

  // What is missing before a peer can be called (null = ready).
  function peerMissing() {
    if (!peerCall()) return "Type the peer's callsign.";
    if (viaKind() === "telnet") return $("#wl-peer-host").value.trim() ? null : "Type the peer's host:port.";
    return /^\d+(\.\d+)?$/.test($("#wl-peer-freq").value.trim()) ? null : "Type the frequency in kHz (e.g. 145010).";
  }

  function peerChanged() {
    fillPeerBw();
    $("#wl-url").value = peerUrl(true);
    $("#wl-url").dataset.peer = "1";
    resetConfirm();
    renderStrip();
  }

  // ---------- the P2P directory (plan winlink-directory, phase 1)
  // Stations that asked to be listed for peer-to-peer Winlink, read through
  // /winlink/api/p2pdir (the server fetches, checks and caches the public
  // file). A click on a row fills the Peer fields: call, the DIAL frequency
  // and the via/modem. It never connects: Connect and its confirm stay the
  // operator's, and the server checks the URL exactly as if it were typed.
  let dir = null, dirAt = 0, dirLoading = false, picked = null, followTimer = null;
  const FOLLOW_POLL_MS = 15000;     // while the server follows a publish, look again this often
  const DIR_KIND = { VARA_HF: "varahf", ARDOP: "ardop", PACKET: "packet" };
  const DIR_MODE = { VARA_HF: "VARA HF", VARA_FM: "VARA FM", ARDOP: "ARDOP", PACTOR: "PACTOR",
                     PACKET: "Packet", ROBUST_PACKET: "Robust Packet" };
  const DIR_FLAG = { "hf-auto-outside-97.221b": "auto on HF outside 97.221(b) segments" };
  const DIR_REFRESH_MS = 10 * 60 * 1000;
  const keyOf = (s, e) => `${s.call}|${e.mode}|${e.centre_khz}`;
  const ago = (s) => s == null ? "" : s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago`
    : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
  const daysSince = (iso) => Math.max(0, Math.floor((Date.now() - Date.parse(`${iso}T00:00:00Z`)) / 86400000));

  // Why this station can't call an entry (null = it can).
  function cannotCall(s, e) {
    if (s.self) return "this station";
    const kind = DIR_KIND[e.mode];
    if (!kind) return `${DIR_MODE[e.mode] || e.mode} isn't a mode this station runs`;
    if (e.dial_khz == null) return "no dial frequency for it";
    if (kind === "packet") {
      if (!vias.some((v) => v.kind === "packet")) return "no packet radio here";
      if (e.centre_khz < 144000) return "packet here is VHF/UHF only";
      return null;
    }
    if (!vias.some((v) => isHF(v.kind))) return "no HF radio here";
    if (!modems.includes(kind)) return `no ${MODEM_LABEL[kind]} here`;
    return null;
  }

  // force: the server refetches; now: just ask the server again (its copy).
  async function loadDirectory(force = false, now = false) {
    if (dirLoading || (!force && !now && dir && Date.now() - dirAt < DIR_REFRESH_MS)) return;
    dirLoading = true;
    if (!dir) $("#wl-p2pdir").innerHTML = `<div class="wl-empty">Loading the P2P directory…</div>`;
    try {
      dir = await (await api(`${BASE}winlink/api/p2pdir${force ? "?refresh=1" : ""}`)).json();
    } catch (e) {
      dir = { enabled: true, rows: [], reason: `P2P directory unavailable: ${e.message}` };
    } finally {
      dirLoading = false;
      dirAt = Date.now();
    }
    renderDirectory();
    // After a publish the server refetches until the change is in (bounded
    // there); the page looks again meanwhile so the new row appears by itself.
    if (dir.following && !followTimer && mode === "peer")
      followTimer = setTimeout(() => { followTimer = null; loadDirectory(false, true); }, FOLLOW_POLL_MS);
  }

  function renderDirectory() {
    const el = $("#wl-p2pdir");
    if (!el) return;
    if (mode !== "peer") { el.hidden = true; return; }
    if (!dir || Date.now() - dirAt >= DIR_REFRESH_MS) loadDirectory();
    if (!dir) return;
    el.hidden = dir.enabled === false;
    if (dir.enabled === false) return;
    const items = [];
    for (const s of dir.rows || []) for (const e of s.entries) items.push({ s, e, why: cannotCall(s, e) });
    items.sort((a, b) => (!!a.why - !!b.why) || ((a.s.distance_km ?? 1e9) - (b.s.distance_km ?? 1e9))
      || a.s.call.localeCompare(b.s.call));
    const spec = String(dir.url || "").replace(/v1\/directory\.json$/, "");
    const head = [`P2P directory · ${(dir.rows || []).length} stations`];
    if (dir.age_s != null) head.push(`fetched ${ago(dir.age_s)}`);
    if (dir.error && dir.age_s != null) head.push(`couldn't refresh (${dir.error}), showing the last copy`);
    if (dir.following) head.push("updating after a publish…");
    // A re-render (the page looking again) must not take the cursor out of the editor.
    const ta = editorEl?.querySelector("textarea");
    const caret = ta && document.activeElement === ta ? [ta.selectionStart, ta.selectionEnd] : null;
    el.innerHTML = `<div class="wl-dir-head"><span class="wl-dim">${esc(head.join(" · "))}</span>` +
      (/^https?:\/\//.test(spec) ? `<a href="${esc(spec)}" target="_blank" rel="noopener">Get listed</a>` : "") +
      `</div>` + (dir.reason && !items.length ? `<div class="wl-empty">${esc(dir.reason)}</div>` : "")
      + (!dir.reason && !items.length ? `<div class="wl-empty">No stations listed yet</div>` : "");
    renderIntake(el);
    for (const { s, e, why } of items) {
      const row = document.createElement(why ? "div" : "button");
      row.className = "wl-gw wl-dir" + (why ? " wl-dir-off" : "") + (picked === keyOf(s, e) ? " on" : "");
      const width = "bw_hz" in e ? `${e.bw_hz} Hz` : `${e.baud} bd`;
      const age = daysSince(s.confirmed);
      const notes = [e.control, e.hours, `confirmed ${age} d ago${s.stale ? " (stale)" : ""}`]
        .concat((e.flags || []).map((f) => `⚑ ${DIR_FLAG[f] || f}`));
      if (why) notes.unshift(`can't call: ${why}`);
      row.title = [`centre ${e.centre_khz} kHz`, s.grid, s.note].filter(Boolean).join(" · ");
      row.innerHTML = `<span class="wl-who">${esc(s.call)}</span>` +
        `<span>${esc(e.dial_khz ?? e.centre_khz)} kHz</span>` +
        `<span>${esc(DIR_MODE[e.mode] || e.mode)} ${esc(width)}</span>` +
        `<span class="wl-dim">${s.distance_km != null ? `${esc(s.distance_km)} km · ` : ""}${esc(notes.join(" · "))}</span>`;
      row.dataset.call = s.call;
      if (!why) {
        row.insertAdjacentHTML("beforeend", QUICK);
        row.onclick = () => pickDirectory(s, e);
        quick(row, () => pickDirectory(s, e));
      }
      el.append(row);
    }
    if (caret && ta.isConnected) { ta.focus(); ta.setSelectionRange(...caret); }
  }

  // The maintainer's station only (dir.intake present): listing requests in
  // the inbox. The card shows the exact text Publish writes to the directory
  // repo; Publish and Dismiss both move the station's request messages to
  // the archive. Nothing here sends mail.
  const INTAKE_KIND = { listing: "wants to be listed", confirm: "confirms its listing",
                        remove: "asks to be removed", refused: "can't be listed" };
  let intakeBusy = null;
  function renderIntake(el) {
    if (!dir.maintainer) return;
    const box = document.createElement("div");
    box.className = "wl-intake";
    const reqs = dir.intake || [];
    box.innerHTML = `<div class="wl-dir-head"><span class="wl-dim">Listing requests in the inbox · ${reqs.length}` +
      `</span></div>` + (dir.intake_error ? `<div class="wl-empty">${esc(dir.intake_error)}</div>` : "");
    for (const r of reqs) {
      const card = document.createElement("div");
      card.className = "wl-intake-req";
      card.dataset.mid = r.mid;
      const who = r.call || r.from || "?";
      const older = r.mids.length > 1 ? ` · ${r.mids.length - 1} older from ${who} archived with it` : "";
      const lines = [];
      for (const e of r.parsed?.entries || []) {
        const width = "bw_hz" in e ? `${e.bw_hz} Hz` : `${e.baud} bd`;
        lines.push(`<div class="wl-dim">${esc(e.centre_khz)} kHz centre · ${esc(DIR_MODE[e.mode] || e.mode)} ${esc(width)}` +
          ` · ${esc(e.control)} · ${esc(e.hours)}</div>` +
          (e.flags || []).map((f) => `<div class="wl-intake-flag">⚑ ${esc(DIR_FLAG[f] || f)}</div>`).join(""));
      }
      const cur = r.current ? `<div class="wl-dim">Listed now, confirmed ${esc(r.current.confirmed)}` +
        (r.kind === "listing" ? "; this replaces it" : "") + `</div>` : "";
      const can = !r.error && r.kind !== "refused";
      card.innerHTML = `<div><span class="wl-who">${esc(who)}</span> ${esc(INTAKE_KIND[r.kind] || r.kind)}` +
        ` <span class="wl-dim">· Winlink message ${esc(r.date || "")}${esc(older)}</span></div>` +
        (r.text ? `<pre class="wl-intake-text">${esc(r.text)}</pre>` : "") + lines.join("") + cur +
        (r.error ? `<div class="wl-intake-err">${esc(r.error)}</div>` : "") +
        `<div class="wl-intake-do">` +
        (can ? `<button class="chip chip-mini" data-do="publish">${r.kind === "remove" ? "Remove from directory"
          : r.kind === "confirm" ? "Confirm in directory" : "Publish to directory"}</button>` : "") +
        `<button class="chip chip-mini" data-do="dismiss">Dismiss</button></div>`;
      for (const b of card.querySelectorAll("[data-do]")) {
        b.disabled = !!intakeBusy;
        b.onclick = () => intakeAct(r, b.dataset.do);
      }
      box.append(card);
    }
    renderListed(box);
    el.prepend(box);
  }

  // The maintainer's station: the published listings, each editable. The
  // editor's node is kept across re-renders so typing in it isn't lost.
  let editing = null, editorEl = null, editBusy = false;   // editing = {call, base, confirmed}
  function renderListed(box) {
    const calls = dir.listed || [];
    if (!calls.length) return;
    const line = document.createElement("div");
    line.className = "wl-intake-listed";
    line.innerHTML = `<span class="wl-dim">Published listings · click to edit:</span>`;
    for (const c of calls) {
      const b = document.createElement("button");
      b.className = "chip chip-mini" + (editing?.call === c ? " on" : "");
      b.textContent = c;
      b.dataset.edit = c;
      b.disabled = editBusy;
      b.onclick = () => openEdit(c);
      line.append(b);
    }
    box.append(line);
    if (editing) box.append(editorEl && editorEl.dataset.call === editing.call ? editorEl : makeEditor());
  }

  function makeEditor() {
    const ed = document.createElement("div");
    ed.className = "wl-intake-req wl-intake-edit";
    ed.dataset.call = editing.call;
    ed.innerHTML = `<div><span class="wl-who">${esc(editing.call)}</span> <span class="wl-dim">· editing the ` +
      `published listing · confirmed ${esc(editing.confirmed || "?")}; an edit keeps that date</span></div>` +
      `<textarea class="wl-intake-text" rows="6" spellcheck="false" autocomplete="off"></textarea>` +
      `<div class="wl-intake-do"><button class="chip chip-mini" data-do="save">Save to directory</button>` +
      `<button class="chip chip-mini" data-do="cancel">Cancel</button></div>`;
    ed.querySelector("textarea").value = editing.base;
    ed.querySelector("[data-do=save]").onclick = saveEdit;
    ed.querySelector("[data-do=cancel]").onclick = () => { editing = editorEl = null; renderDirectory(); };
    editorEl = ed;
    return ed;
  }

  async function openEdit(call) {
    if (editBusy) return;
    try {
      const res = await api(`${BASE}winlink/api/p2pdir/listing?call=${encodeURIComponent(call)}`);
      const out = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
      if (!res.ok || out.ok === false) throw new Error(out.error || `HTTP ${res.status}`);
      editing = { call: out.call, base: out.text, confirmed: out.confirmed };
      editorEl = null;
      renderDirectory();
      editorEl?.querySelector("textarea")?.focus();
    } catch (e) {
      toast(`Can't open ${call}: ${e.message}`, "warn");
    }
  }

  async function saveEdit() {
    if (editBusy || !editing || !editorEl) return;
    editBusy = true;
    const { call, base } = editing;
    const text = editorEl.querySelector("textarea").value;
    for (const b of editorEl.querySelectorAll("button")) b.disabled = true;
    try {
      const res = await api(`${BASE}winlink/api/p2pdir/listing`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ call, text, base }) });
      const out = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      toast(`${call} saved (${(out.commit || "").slice(0, 7)}); the directory updates in a few minutes`);
      editing = editorEl = null;
    } catch (e) {
      toast(`Save failed: ${e.message}`, "warn");
    } finally {
      editBusy = false;
      if (editorEl) for (const b of editorEl.querySelectorAll("button")) b.disabled = false;
      loadDirectory(false, true);
    }
  }

  async function intakeAct(r, what) {
    if (intakeBusy) return;
    intakeBusy = r.mid;
    renderDirectory();
    try {
      const res = await api(`${BASE}winlink/api/p2pdir/intake`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mid: r.mid, do: what }) });
      const out = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      toast(what === "dismiss" ? `Dismissed ${r.call || r.from}'s request`
        : `${r.call} ${r.kind === "remove" ? "removed" : r.kind === "confirm" ? "confirmed" : "published"}` +
          ` (${(out.commit || "").slice(0, 7)}); the directory updates in a few minutes`);
    } catch (e) {
      toast(`${what === "dismiss" ? "Dismiss" : "Publish"} failed: ${e.message}`, "warn");
    } finally {
      intakeBusy = null;
      loadDirectory(false, true);
    }
  }

  // Fill the Peer fields from a row. Never connects.
  function pickDirectory(s, e) {
    const kind = DIR_KIND[e.mode];
    const cur = viaKind();
    let target = null;
    if (kind === "packet") target = cur === "packet" ? null : vias.find((v) => v.kind === "packet");
    else if (perModem()) target = cur === kind ? null : vias.find((v) => v.kind === kind);
    else target = isHF(cur) ? null : vias.find((v) => isHF(v.kind));
    picked = keyOf(s, e);
    if (target) {
      viaSel().value = target.id;
      viaChanged(true);
    }
    if (kind !== "packet" && !perModem()) $("#wl-peer-modem").value = kind;
    $("#wl-peer-call").value = s.call;
    $("#wl-peer-freq").value = String(e.dial_khz);
    fillPeerBw();
    $("#wl-peer-bw").value = PEER_BW[peerScheme()]?.includes(e.bw_hz) ? String(e.bw_hz) : "";
    peerChanged();
    renderDirectory();
  }

  function setMode(v) {
    mode = v === "peer" ? "peer" : "gateway";
    store.set("wl.mode", mode);
    if (mode === "gateway" && $("#wl-url").dataset.peer) { $("#wl-url").value = ""; delete $("#wl-url").dataset.peer; }
    viaChanged(false);
    if (mode === "gateway" && viaKind() !== "telnet") loadGateways();
  }

  function fillBands() {
    const sel = $("#wl-band");
    const vhf = viaKind() === "packet";
    sel.innerHTML = vhf
      ? `<option value="">All bands</option><option value="2m">2 m</option><option value="70cm">70 cm</option>`
      : `<option value="">All HF bands</option>` + BANDS.filter((b) => b.hi <= 30e6)
        .map((b) => `<option value="${b.n}m">${b.n} m</option>`).join("");
    const key = vhf ? "wl.band.vhf" : "wl.band";
    sel.value = store.get(key, vhf ? "2m" : "80m");
  }

  let gwKind = null;             // the via kind the gateway list was last asked for
  async function loadGateways() {
    const el = $("#wl-gateways");
    const band = $("#wl-band").value;
    const kind = viaKind();
    gwKind = kind;
    store.set(kind === "packet" ? "wl.band.vhf" : "wl.band", band);
    el.innerHTML = `<div class="wl-empty">Loading gateways…</div>`;
    try {
      const q = new URLSearchParams({ mode: kind === "packet" ? "packet" : isHF(kind) ? kind : "ardop" });
      if (band) q.set("band", band);
      const gws = await (await api(`${BASE}winlink/api/rmslist?${q}`)).json();
      el.innerHTML = gws.length ? "" : `<div class="wl-empty">No gateways for that band</div>`;
      for (const g of gws.slice(0, 40)) {
        const row = document.createElement("button");
        row.className = "wl-gw";
        row.dataset.url = g.url;
        row.innerHTML = `<span class="wl-who">${esc(g.callsign)}</span><span>${esc(g.dial?.khz)} kHz</span>` +
          `<span>${Math.round(g.distance)} km ${Math.round(g.azimuth)}°</span>` + QUICK;
        row.title = [g.modes, g.url].filter(Boolean).join(" · ");
        const pick = () => {
          $("#wl-url").value = g.url;
          for (const r of el.children) r.classList.toggle("on", r === row);
          resetConfirm();
          renderStrip();
        };
        row.onclick = pick;
        quick(row, pick);
        el.append(row);
      }
    } catch (e) {
      el.innerHTML = `<div class="wl-empty">Gateway list unavailable: ${esc(e.message)}</div>`;
    }
  }

  // ---------- sessions

  function freqOf(url) {
    const m = /[?&]freq=([\d.]+)/.exec(url);
    return m ? `${m[1]} kHz` : "";
  }

  function resetConfirm() {
    clearTimeout(confirmTimer);
    confirmTimer = null;
    $("#wl-connect").textContent = "Connect";
    $("#wl-connect").classList.remove("wl-confirm");
  }

  function connectClick() {
    const peer = mode === "peer";
    const missing = peer ? peerMissing() : null;
    if (missing) { toast(missing, "warn"); return; }
    const url = peer ? peerUrl() : $("#wl-url").value.trim();
    if (!url) { toast("Pick a gateway or type a connect URL first.", "warn"); return; }
    if (!confirmTimer) {
      // The operator confirms every transmission: second click within a few seconds.
      const b = $("#wl-connect");
      const via = vias.find((v) => v.id === viaSel()?.value);
      const to = peer ? ` · peer ${peerCall()}` : "";
      // With more than one HF modem, the confirm names the one this URL keys.
      const sch = url.split(":")[0];
      const modem = perModem() && isHF(sch) ? ` (${MODEM_LABEL[sch]})` : "";
      b.textContent = (via?.kind === "telnet" ? "Confirm: connect over the internet"
        : `Confirm: transmit${via ? ` on the ${via.name}${modem}` : ""} ${freqOf(url) || "now"}`) + to;
      b.classList.add("wl-confirm");
      confirmTimer = setTimeout(resetConfirm, CONFIRM_MS);
      return;
    }
    resetConfirm();
    send({ t: "winlink_connect", url, via: viaSel()?.value || null, ...(peer ? { peer: true } : {}) });
  }

  function setState(text, cls = "") {
    const el = $("#wl-state");
    el.textContent = text;
    el.className = `wl-state ${cls}`;
  }

  // The idle line: switched off, Pat refused (its config), answering a
  // station that called us, or armed/idle.
  function idleState() {
    const w = listen?.window;
    if (off) setState("Switched off · Pat isn't running", "warn");
    else if (blocked) setState(`Off · ${blocked}`, "warn");
    else if (inbound && !session) setState(`Answering ${inbound.call} · a station called this one`, "on");
    else if (listen?.busy === "starting") setState(`Starting to listen on ${listenWhere(w)}…`, "on");
    else if (listen?.busy === "ending") setState("Ending the listen window…", "on");
    else if (w?.kind === "hf" && w.ask_until) setState(`Still there? Press I'm here before ${clock(w.ask_until)} or HF listening ends`, "warn");
    else if (w) setState(`Listening${w.kind === "hf" ? " (attended)" : ""} on ${listenWhere(w)} until ${clock(w.until)} · answering calls to ${w.call}`, "on");
    else setState(session ? `Armed · session to ${targetOf(session) || session}` : armed ? "Armed" : "Idle", armed ? "on" : "");
  }

  function renderSession() {
    const listening = !!(listen && (listen.window || listen.busy));
    $("#wl-stop").disabled = !session;
    $("#wl-connect").disabled = !!session || off || !!blocked || !!inbound || listening;
    renderListen();
    $("#wl-sec-listen").hidden = $("#wl-listen").hidden && $("#wl-listen-hf").hidden;
    renderStrip();
    $("#wl-compose-btn").disabled = off;
    $("#winlink").classList.toggle("wl-armed", armed);
    $("#winlink").classList.toggle("wl-off", off);
    const pw = $("#wl-power");
    pw.textContent = off ? "Switch on" : "Switch off";
    pw.title = off ? "Start Pat" : "Stop Pat; arming a radio for the station still works";
    renderActions();
  }

  // ---------- listen window: answer stations that call this one, for a bounded time

  const mhzText = (plan) => (plan.hz / 1e6).toFixed(3);
  const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const hoursText = (h) => (h < 1 ? `${Math.round(h * 60)} min` : `${h} h`);
  // Where a window answers: an HF window by its centre and modem, a packet one by the plan's frequency.
  function listenWhere(w) {
    if (w?.kind === "hf") return `${(w.centre_hz / 1e6).toFixed(4)} MHz (${MODEM_LABEL[w.method] || w.method})`;
    if (w?.hz) return `${(w.hz / 1e6).toFixed(3)} MHz`;
    return listen?.plan ? `${mhzText(listen.plan)} MHz` : "HF";
  }

  function renderListen() {
    renderListenHf();
    const row = $("#wl-listen");
    row.hidden = !listen?.plan || off;
    if (!listen?.plan) return;
    const w = listen.window, busy = listen.busy, sel = $("#wl-listen-hours");
    const max = listen.plan.max_hours;
    if (sel.dataset.max !== String(max)) {          // the choices, once per plan
      const hs = [0.25, 0.5, 1, 2, 4, 8, 12].filter((h) => h < max).concat([max]);
      sel.replaceChildren(...hs.map((h) => Object.assign(document.createElement("option"),
        { value: String(h), textContent: hoursText(h) })));
      sel.value = String(max);
      sel.dataset.max = String(max);
    }
    const mine = !w || w.kind !== "hf";        // the HF row has its own Stop
    $("#wl-listen-btn").hidden = !!w || busy === "ending";
    $("#wl-listen-stop").hidden = !mine || (!w && busy !== "starting");
    $("#wl-listen-btn").disabled = !!session || !!inbound || !!blocked || !!busy || armed;
    sel.disabled = !!w || !!busy;
    if ((w || busy) && listenTimer) resetListenConfirm();
  }

  function resetListenConfirm() {
    clearTimeout(listenTimer);
    listenTimer = null;
    $("#wl-listen-btn").textContent = "Listen";
    $("#wl-listen-btn").classList.remove("wl-confirm");
  }

  // Two clicks, like Connect: the confirm names the frequency, the hours, and
  // that APRS and the iGate are off-frequency for the window.
  function listenClick() {
    if (!listen?.plan) return;
    const h = Number($("#wl-listen-hours").value);
    if (!listenTimer) {
      const b = $("#wl-listen-btn");
      b.textContent = `Confirm: answer calls on ${mhzText(listen.plan)} MHz for ${hoursText(h)}`
        + " · APRS and iGate off-frequency meanwhile";
      b.classList.add("wl-confirm");
      listenTimer = setTimeout(resetListenConfirm, CONFIRM_MS);
      return;
    }
    resetListenConfirm();
    send({ t: "winlink_listen", hours: h });
  }

  // ---------- attended HF listening (winlink.listen_hf): answer HF peer calls
  // while you are here. The server holds it to that: the window ends when this
  // page closes, when nobody presses I'm here, or at its time limit.

  let hfTimer = null;
  const hfLabel = (c) => `${c.band} · ${c.khz} kHz ${MODEM_LABEL[c.modem] || c.modem}` +
    (c.segment ? " · 97.221(b) segment" : "");

  function renderListenHf() {
    const row = $("#wl-listen-hf");
    const hf = listen?.hf;
    row.hidden = !hf || off;
    if (!hf) return;
    const w = listen.window, busy = listen.busy, sel = $("#wl-listen-hf-ch");
    const key = JSON.stringify(hf.channels);
    if (sel.dataset.key !== key) {
      sel.replaceChildren(...hf.channels.map((c, i) => Object.assign(document.createElement("option"),
        { value: String(i), textContent: hfLabel(c) })));
      sel.value = String(Math.min(Number(store.get("wl.listen.hf", 0)) || 0, hf.channels.length - 1));
      sel.dataset.key = key;
    }
    const ours = w?.kind === "hf";
    $("#wl-listen-hf-btn").hidden = !!w || busy === "ending";
    $("#wl-listen-hf-btn").disabled = !!session || !!inbound || !!blocked || !!busy || armed;
    $("#wl-listen-hf-stop").hidden = !ours && !(busy === "starting" && !w);
    $("#wl-listen-here").hidden = !(ours && w.ask_until);
    sel.disabled = !!w || !!busy;
    if ((w || busy) && hfTimer) resetHfConfirm();
  }

  function resetHfConfirm() {
    clearTimeout(hfTimer);
    hfTimer = null;
    $("#wl-listen-hf-btn").textContent = "Listen on HF (attended)";
    $("#wl-listen-hf-btn").classList.remove("wl-confirm");
  }

  // Two clicks: the confirm says what it is (attended, this page must stay open).
  function listenHfClick() {
    const hf = listen?.hf;
    if (!hf) return;
    const i = Number($("#wl-listen-hf-ch").value);
    const c = hf.channels[i];
    if (!c) return;
    if (!hfTimer) {
      const b = $("#wl-listen-hf-btn");
      b.textContent = `Confirm: answer calls on ${c.khz} kHz ${MODEM_LABEL[c.modem] || c.modem} for up to ` +
        `${hf.max_minutes} min · keep this page open, I'm here every ${hf.presence_minutes} min`;
      b.classList.add("wl-confirm");
      hfTimer = setTimeout(resetHfConfirm, CONFIRM_MS);
      return;
    }
    resetHfConfirm();
    store.set("wl.listen.hf", i);
    send({ t: "winlink_listen", hf: i });
  }

  // The add-on registry's on/off switch (addons/winlink.py). Off stops Pat. The
  // server refuses it while a session runs; the transmit lane is not the add-on's.
  $("#wl-power").onclick = () => {
    const want = off;
    if (!want && !confirm("Switch Winlink off? Pat stops until you switch it on again.")) return;
    send({ t: "addon", addon: "winlink", do: "power", on: want });
  };

  // The session log: Pat's lines, the time in its own column and the lines
  // that matter coloured. textContent stays the plain lines (tests read it).
  let logLines = [];
  const LOG_ERR = /\b(error|failed|failure|refused|abort(ed)?|timed? ?out|unable|denied)\b/i;
  const LOG_OK = /\b(connected to|connection established|sent \d|received \d|exchange complete|ID sent)\b/i;
  function logHtml(line) {
    const m = /^(\d{4}\/\d\d\/\d\d )?(\d\d:\d\d:\d\d) (.*)$/s.exec(line);
    const body = m ? m[3] : line;
    const cls = LOG_ERR.test(body) ? " wl-log-err" : LOG_OK.test(body) ? " wl-log-ok" : "";
    return `<span class="wl-log-line${cls}">` + (m ? `<span class="wl-log-t">${esc(m[1] || "")}${esc(m[2])}</span> ` : "") +
      `${esc(body)}</span>`;
  }
  function setLog(lines) {
    logLines = lines.slice(-LOG_KEEP);
    const el = $("#wl-log");
    el.innerHTML = logLines.map(logHtml).join("\n");
    el.scrollTop = el.scrollHeight;
  }
  function log(line) {
    const el = $("#wl-log");
    const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 24;   // don't yank a reader back down
    logLines.push(line);
    if (logLines.length > LOG_KEEP) { logLines = logLines.slice(-LOG_KEEP); setLog(logLines); }
    else el.insertAdjacentHTML("beforeend", (logLines.length > 1 ? "\n" : "") + logHtml(line));
    if (stick) el.scrollTop = el.scrollHeight;
  }

  function codec(db) {
    $("#wl-codec").textContent = db === null || db === undefined ? "" : `Codec ${Math.round(db * 10) / 10} dB`;
  }

  // Station ID (47 CFR 97.119): what the server's ledger says about the
  // modems' IDs (ardopcf's, or xara's CW ID for VARA HF: id.last.source).
  // "sent" only when an ID was seen going out through the lane.
  const ID_FLAG = { ardop: "--winlink-ardop-log", varahf: "--winlink-vara-log" };
  function idState(i) {
    const el = $("#wl-id");
    if (!i) return;
    lastId = i;
    let text = "", warn = false, title = "";
    const at = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const kind = viaKind();
    const who = i.last?.source || "ardopcf";
    if (!i.tracked || (isHF(kind) && Array.isArray(i.tracked_modems) && !i.tracked_modems.includes(kind))) {
      text = "ID not tracked";
      title = `Station ID is not checked: start the server with ${ID_FLAG[isHF(kind) ? kind : "ardop"]}`;
    } else if (i.waiting || i.in_progress) {
      text = "Sending station ID…";
    } else if (i.owed) {
      text = "ID owed";
      warn = true;
      title = i.last && i.last.on_air === false
        ? `${who}'s ID (${i.last.reason}) did not go on air${who === "ardopcf" ? ": no session was armed" : ""}`
        : "Transmitted since the last station ID that went on air";
    } else if (i.last && i.last.on_air) {
      text = `ID sent ${at(i.last.end || i.last.start)}`;
      title = who === "ardopcf" ? i.last.reason : `${who}: ${i.last.reason}`;
    }
    el.textContent = text;
    el.title = title;
    el.classList.toggle("warn", warn);
  }

  function showPrompt(p) {
    const el = $("#wl-prompt");
    el.dataset.id = p.id;
    $("#wl-prompt-msg").textContent = p.message || p.kind;
    for (const b of el.querySelectorAll("button")) b.remove();
    const opts = p.options?.length ? p.options.map((o) => [o.value, o.desc || o.value])
      : [["continue", "Send anyway"], ["abort", "Abort"]];
    for (const [v, label] of opts) {
      el.append(chip(label, v, (val) => {
        send({ t: "winlink_prompt", id: p.id, value: val });
        el.hidden = true;
      }));
    }
    el.hidden = false;
  }

  // ---------- from app.js

  function hello(snap, cfg) {
    $("#winlink").hidden = !cfg?.winlink;
    if (!cfg?.winlink || !snap) return;
    armed = !!snap.armed;
    session = snap.session || null;
    off = !!snap.off;
    blocked = snap.blocked || null;
    inbound = snap.inbound || null;
    listen = snap.listen || null;
    if (Array.isArray(snap.modems) && snap.modems.length && snap.modems.join() !== modems.join()) {
      modems = snap.modems;
      ownVias();                       // a VARA HF via when the server has it
    }
    $("#wl-mycall").textContent = snap.mycall || "";
    setLog(snap.log || []);
    codec(snap.codec_db);
    modemSnap = snap.modem || null;
    idState(snap.id);
    idleState();
    renderSession();
    if (mode === "peer") viaChanged(false);   // the modems may have changed
    if (!off) loadBox(box);
  }

  // {"t": "addon", "addon": "winlink", "on", "error", "snapshot"}: switched on or off
  function power(m) {
    resetDelete();
    fwdGen++;
    if (m.snapshot) hello(m.snapshot, { winlink: true });
    else { off = !m.on; renderSession(); }
    if (m.error) setState(`Winlink: ${m.error}`, "warn");
  }

  function event(m) {
    // The codec level comes with an arm/disarm, or on its own: the station
    // relays the radio's codec moves between sessions (its RadioWatch).
    if ("codec_db" in m) codec(m.codec_db);
    if ("armed" in m) {
      armed = m.armed;
      if (armed) setState(session ? `Armed · session to ${targetOf(session) || session}` : "Armed", "on");
      renderSession();
    }
    if (m.id) { idState(m.id); renderModem(); }
    if ("modem" in m) { modemSnap = m.modem || null; renderModem(); }
    if ("blocked" in m && (m.blocked || null) !== blocked) {
      blocked = m.blocked || null;
      idleState();
      renderSession();
    }
    if ("inbound" in m) {
      const was = inbound;
      inbound = m.inbound || null;
      if (!session) idleState();
      renderSession();
      // What the peer left is in the inbox now (and its share of the outbox
      // gone): refresh the list, unless a message is open (keep the reader).
      if (was && !inbound && !current) loadBox(box);
    }
    if ("listen" in m) {
      listen = m.listen || null;
      if (!session) idleState();
      renderSession();
    }
    if (m.log) log(m.log);
    if (m.status && "mycall" in m.status) $("#wl-mycall").textContent = m.status.mycall;
    if (m.session === "started") {
      session = m.url;
      setState(`Armed · session to ${targetOf(m.url) || m.url}`, "on");
      renderSession();
    } else if (m.session === "ended") {
      session = null;
      // sent is null when Pat's mailbox could not be read: say unknown, never 0.
      const sent = m.sent == null ? "sent ?" : `sent ${m.sent}`;
      // A gateway takes all but P2P-only messages: one it left is a warning.
      // A peer takes only what is addressed to it: what stays is just counted.
      const warnLeft = !m.peer && m.ok && m.unsent;
      const left = warnLeft ? ` · ${m.unsent} still in the outbox`
        : m.left ? ` · ${m.left} left in the outbox${m.peer ? "" : " (P2P only)"}` : "";
      setState(m.ok ? `Session ended · ${sent}, received ${m.received}${left}`
                    : `Session ended · ${m.reason || "failed"}${m.sent ? ` · sent ${m.sent}` : ""}`,
        m.ok && !warnLeft ? "" : "warn");
      $("#wl-prompt").hidden = true;
      renderSession();
      loadBox(box);
    }
    if (m.prompt) showPrompt(m.prompt);
    if (m.prompt_abort) $("#wl-prompt").hidden = true;
    if (m.notify) toast(m.notify.title || "Winlink", "info");
    if (m.mailbox) loadBox(box);
  }

  for (const c of $("#wl-boxes").children) c.onclick = () => loadBox(c.dataset.v);
  $("#wl-compose-btn").onclick = () => { fwdGen++; openCompose($("#wl-compose").hidden); };
  $("#wl-compose-cancel").onclick = () => openCompose(false);
  $("#wl-compose").onsubmit = submitCompose;
  $("#wl-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("#wl-compose").requestSubmit(); }
  });
  $("#wl-compose").addEventListener("reset", () => setNote(""));
  $("#wl-reply").onclick = () => reply(false);
  $("#wl-reply-all").onclick = () => reply(true);
  $("#wl-forward").onclick = forward;
  $("#wl-archive").onclick = archive;
  $("#wl-delete").onclick = deleteClick;
  $("#wl-gw-refresh").onclick = loadGateways;
  $("#wl-band").onchange = loadGateways;
  $("#wl-url").oninput = () => { resetConfirm(); renderStrip(); };
  for (const c of $("#wl-mode").children) c.onclick = () => setMode(c.dataset.v);
  for (const id of ["#wl-peer-call", "#wl-peer-freq", "#wl-peer-host", "#wl-peer-pass"]) $(id).oninput = peerChanged;
  $("#wl-peer-modem").onchange = peerChanged;
  $("#wl-peer-bw").onchange = peerChanged;
  $("#wl-connect").onclick = connectClick;
  $("#wl-stop").onclick = () => send({ t: "winlink_stop" });
  $("#wl-listen-btn").onclick = listenClick;
  $("#wl-listen-stop").onclick = () => send({ t: "winlink_listen_stop" });
  $("#wl-listen-hf-btn").onclick = listenHfClick;
  $("#wl-listen-hf-stop").onclick = () => send({ t: "winlink_listen_stop" });
  $("#wl-listen-here").onclick = () => send({ t: "winlink_listen_here" });
  if (viaSel()) viaSel().onchange = () => viaChanged();
  fillBands();
  renderActions();

  // _current and _api are test hooks (tests/winlink_page_test.py): the open
  // message, and what an API call would show the operator.
  const _current = () => (current ? { box: current.box, mid: current.mid } : null);
  const _api = (path, opts) => api(path, opts).then((r) => `HTTP ${r.status}`, (e) => e.message);
  // For Mission Control (station.js): the state line as this panel shows it,
  // and the mailbox counts (Pat's lists; null when Pat can't be read).
  function summary() {
    const el = $("#wl-state");
    return { text: el?.textContent || "", warn: !!el?.classList.contains("warn"), armed, session, off,
      blocked: !!blocked, inbound: inbound?.call || null, listening: !!listen?.window,
      ask: !!(listen?.window?.kind === "hf" && listen.window.ask_until) };
  }
  async function counts() {
    if (off) return null;
    try {
      const [inb, out] = await Promise.all(["in", "out"].map((b) =>
        api(`${BASE}winlink/api/mailbox/${b}`).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))));
      return { unread: (inb || []).filter((m) => m.Unread).length, inbox: (inb || []).length, outbox: (out || []).length };
    } catch {
      return null;
    }
  }
  return { hello, event, power, setVias, ownRadio, setPacket, summary, counts, _current, _api };
})();
