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

  const api = (path, opts) => fetch(path + (token ? `${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}` : ""), opts)
    .then(async (r) => {
      if (!r.ok) throw new Error((await r.text()).trim() || `HTTP ${r.status}`);
      return r;
    });
  const addrs = (list) => (list || []).map((a) => a.Addr).join(", ");
  const when = (d) => (d ? new Date(d).toISOString().slice(0, 16).replace("T", " ") : "");

  // ---------- mailbox

  async function loadBox(b = box) {
    box = b;
    for (const c of $("#wl-boxes").children) on(c, c.dataset.v === box);
    $("#wl-read").hidden = true;
    const list = $("#wl-list");
    try {
      const msgs = await (await api(`${BASE}winlink/api/mailbox/${box}`)).json();
      msgs.sort((a, b) => String(b.Date).localeCompare(String(a.Date)));
      list.innerHTML = msgs.length ? "" : `<div class="wl-empty">No messages</div>`;
      for (const m of msgs) {
        const row = document.createElement("button");
        row.className = "wl-msg" + (m.Unread ? " unread" : "");
        row.dataset.mid = m.MID;
        const who = box === "in" ? m.From?.Addr : addrs(m.To);
        row.innerHTML = `<span class="wl-who">${esc(who)}</span><span class="wl-subj">${esc(m.Subject)}</span>` +
          `<span class="wl-date">${esc(when(m.Date))}</span>`;
        row.onclick = () => read(m.MID, row);
        list.append(row);
      }
    } catch (e) {
      list.innerHTML = `<div class="wl-empty">Mailbox unavailable: ${esc(e.message)}</div>`;
    }
  }

  async function read(mid, row) {
    try {
      const m = await (await api(`${BASE}winlink/api/mailbox/${box}/${encodeURIComponent(mid)}`)).json();
      $("#wl-read-head").innerHTML =
        `<b>${esc(m.Subject)}</b><span>From ${esc(m.From?.Addr)} · To ${esc(addrs(m.To))}` +
        `${m.Cc?.length ? ` · Cc ${esc(addrs(m.Cc))}` : ""} · ${esc(when(m.Date))}</span>` +
        (m.Files?.length ? `<span>Attachments: ${m.Files.map((f) =>
          `<a href="${BASE}winlink/api/mailbox/${box}/${encodeURIComponent(mid)}/${encodeURIComponent(f.Name)}${token ? `?token=${encodeURIComponent(token)}` : ""}" target="_blank">${esc(f.Name)}</a>`).join(", ")}</span>` : "");
      $("#wl-body").textContent = m.Body || "";
      $("#wl-compose").hidden = true;
      $("#wl-read").hidden = false;
      if (m.Unread && box === "in") {
        row?.classList.remove("unread");
        api(`${BASE}winlink/api/mailbox/in/${encodeURIComponent(mid)}/read`, { method: "POST",
          headers: { "Content-Type": "application/json" }, body: "true" }).catch(() => {});
      }
    } catch (e) {
      toast(`Could not open the message: ${e.message}`, "warn");
    }
  }

  function openCompose(show = true) {
    $("#wl-compose").hidden = !show;
    if (show) { $("#wl-read").hidden = true; $("#wl-to").focus(); }
  }

  async function submitCompose(e) {
    e.preventDefault();
    const form = new FormData();
    form.append("to", $("#wl-to").value.trim());
    form.append("subject", $("#wl-subject").value.trim());
    form.append("body", $("#wl-text").value.replace(/\r?\n/g, "\r\n"));
    for (const f of $("#wl-files").files) form.append("files", f, f.name);
    try {
      await api(`${BASE}winlink/api/mailbox/out`, { method: "POST", body: form });
      $("#wl-compose").reset();
      openCompose(false);
      toast("Queued in the outbox; it goes out on the next session.");
      loadBox("out");
    } catch (err) {
      toast(`Not queued: ${err.message}`, "warn");
    }
  }

  // ---------- gateways

  // Via (station page only): which radio a session goes out on. Its kind
  // picks the gateway list: ARDOP on an HF radio, packet on the UV-PRO, or
  // none for telnet. On a radio's own page there is no #wl-via: ARDOP.
  let vias = [];
  const viaSel = () => $("#wl-via");
  const viaKind = () => (vias.find((v) => v.id === viaSel()?.value) || { kind: "ardop" }).kind;

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
    $("#wl-band").hidden = $("#wl-gw-refresh").hidden = $("#wl-gateways").hidden = kind === "telnet";
    if (kind === "telnet") {
      $("#wl-url").value = `telnet://${$("#wl-mycall").textContent || "MYCALL"}:CMSTelnet@cms.winlink.org:8772/wl2k`;
      resetConfirm();
      return;
    }
    if ($("#wl-url").value.startsWith("telnet://")) { $("#wl-url").value = ""; resetConfirm(); }
    fillBands();
    if (save) loadGateways();
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

  async function loadGateways() {
    const el = $("#wl-gateways");
    const band = $("#wl-band").value;
    const kind = viaKind();
    store.set(kind === "packet" ? "wl.band.vhf" : "wl.band", band);
    el.innerHTML = `<div class="wl-empty">Loading gateways…</div>`;
    try {
      const q = new URLSearchParams({ mode: kind === "packet" ? "packet" : "ardop" });
      if (band) q.set("band", band);
      const gws = await (await api(`${BASE}winlink/api/rmslist?${q}`)).json();
      el.innerHTML = gws.length ? "" : `<div class="wl-empty">No gateways for that band</div>`;
      for (const g of gws.slice(0, 40)) {
        const row = document.createElement("button");
        row.className = "wl-gw";
        row.dataset.url = g.url;
        row.innerHTML = `<span class="wl-who">${esc(g.callsign)}</span><span>${esc(g.dial?.khz)} kHz</span>` +
          `<span>${Math.round(g.distance)} km ${Math.round(g.azimuth)}°</span><span class="wl-dim">${esc(g.modes)}</span>`;
        row.onclick = () => {
          $("#wl-url").value = g.url;
          for (const r of el.children) r.classList.toggle("on", r === row);
          resetConfirm();
        };
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
    const url = $("#wl-url").value.trim();
    if (!url) { toast("Pick a gateway or type a connect URL first.", "warn"); return; }
    if (!confirmTimer) {
      // The operator confirms every transmission: second click within a few seconds.
      const b = $("#wl-connect");
      const via = vias.find((v) => v.id === viaSel()?.value);
      b.textContent = via?.kind === "telnet" ? "Confirm: connect over the internet"
        : `Confirm: transmit${via ? ` on the ${via.name}` : ""} ${freqOf(url) || "now"}`;
      b.classList.add("wl-confirm");
      confirmTimer = setTimeout(resetConfirm, CONFIRM_MS);
      return;
    }
    resetConfirm();
    send({ t: "winlink_connect", url, via: viaSel()?.value || null });
  }

  function setState(text, cls = "") {
    const el = $("#wl-state");
    el.textContent = text;
    el.className = `wl-state ${cls}`;
  }

  function renderSession() {
    $("#wl-stop").disabled = !session;
    $("#wl-connect").disabled = !!session || off;
    $("#wl-compose-btn").disabled = off;
    $("#winlink").classList.toggle("wl-armed", armed);
    $("#winlink").classList.toggle("wl-off", off);
    const pw = $("#wl-power");
    pw.textContent = off ? "Switch on" : "Switch off";
    pw.title = off ? "Start Pat" : "Stop Pat; arming a radio for the station still works";
  }

  // The add-on registry's on/off switch (addons/winlink.py). Off stops Pat. The
  // server refuses it while a session runs; the transmit lane is not the add-on's.
  $("#wl-power").onclick = () => {
    const want = off;
    if (!want && !confirm("Switch Winlink off? Pat stops until you switch it on again.")) return;
    send({ t: "addon", addon: "winlink", do: "power", on: want });
  };

  function log(line) {
    const el = $("#wl-log");
    const lines = (el.textContent ? el.textContent.split("\n") : []).concat(line).slice(-LOG_KEEP);
    el.textContent = lines.join("\n");
    el.scrollTop = el.scrollHeight;
  }

  function codec(db) {
    $("#wl-codec").textContent = db === null || db === undefined ? "" : `Codec ${Math.round(db * 10) / 10} dB`;
  }

  // Station ID (47 CFR 97.119): what the server's ledger says about ardopcf's
  // IDs. "sent" only when an ID was seen going out through the lane.
  function idState(i) {
    const el = $("#wl-id");
    if (!i) return;
    let text = "", warn = false, title = "";
    const at = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (!i.tracked) {
      text = "ID not tracked";
      title = "Station ID is not checked: start the server with --winlink-ardop-log";
    } else if (i.waiting || i.in_progress) {
      text = "Sending station ID…";
    } else if (i.owed) {
      text = "ID owed";
      warn = true;
      title = i.last && i.last.on_air === false
        ? `ardopcf's ID (${i.last.reason}) did not go on air: no session was armed`
        : "Transmitted since the last station ID that went on air";
    } else if (i.last && i.last.on_air) {
      text = `ID sent ${at(i.last.end || i.last.start)}`;
      title = i.last.reason;
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
    $("#wl-mycall").textContent = snap.mycall || "";
    $("#wl-log").textContent = (snap.log || []).join("\n");
    codec(snap.codec_db);
    idState(snap.id);
    if (off) setState("Switched off · Pat isn't running", "warn");
    else setState(session ? `Armed · session to ${session}` : armed ? "Armed" : "Idle", armed ? "on" : "");
    renderSession();
    if (!off) loadBox(box);
  }

  // {"t": "addon", "addon": "winlink", "on", "error", "snapshot"}: switched on or off
  function power(m) {
    if (m.snapshot) hello(m.snapshot, { winlink: true });
    else { off = !m.on; renderSession(); }
    if (m.error) setState(`Winlink: ${m.error}`, "warn");
  }

  function event(m) {
    if ("armed" in m) {
      armed = m.armed;
      if ("codec_db" in m) codec(m.codec_db);
      if (armed) setState(session ? `Armed · session to ${session}` : "Armed", "on");
      renderSession();
    }
    if (m.id) idState(m.id);
    if (m.log) log(m.log);
    if (m.status && "mycall" in m.status) $("#wl-mycall").textContent = m.status.mycall;
    if (m.session === "started") {
      session = m.url;
      setState(`Armed · session to ${m.url}`, "on");
      renderSession();
    } else if (m.session === "ended") {
      session = null;
      // sent is null when Pat's mailbox could not be read: say unknown, never 0.
      const sent = m.sent == null ? "sent ?" : `sent ${m.sent}`;
      const left = m.ok && m.unsent ? ` · ${m.unsent} still in the outbox` : "";
      setState(m.ok ? `Session ended · ${sent}, received ${m.received}${left}`
                    : `Session ended · ${m.reason || "failed"}${m.sent ? ` · sent ${m.sent}` : ""}`,
        m.ok && !m.unsent ? "" : "warn");
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
  $("#wl-compose-btn").onclick = () => openCompose($("#wl-compose").hidden);
  $("#wl-compose-cancel").onclick = () => openCompose(false);
  $("#wl-compose").onsubmit = submitCompose;
  $("#wl-gw-refresh").onclick = loadGateways;
  $("#wl-band").onchange = loadGateways;
  $("#wl-url").oninput = resetConfirm;
  $("#wl-connect").onclick = connectClick;
  $("#wl-stop").onclick = () => send({ t: "winlink_stop" });
  if (viaSel()) viaSel().onchange = () => viaChanged();
  fillBands();

  return { hello, event, power, setVias };
})();
