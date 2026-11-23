"use strict";

// The Logbook tab: the station's web logbook (Wavelog) in a frame, with a link
// to open it in its own tab. The server says where it is (addons/logbook.py):
// {url} as given, or {port, scheme, path} on the name this page was opened at,
// so a logbook on the radio's machine is the same site as the page and its
// login cookie holds inside the frame.
//
// The frame is loaded the first time the tab is shown, not with the page.
// A secure page can't frame a plain-http logbook (mixed content), so then the
// tab offers only the link. Uses common.js (esc).
//
// When the station runs the logbook sync (logsync.py), its status comes in the
// snapshot ("logsync") and in {"t": "addon", addon: "logbook", logsync} events:
// a strip of counts and holds above the frame, and the latest QSOs with a tick
// per destination. Display only, plus the operator's Send correction for a QSO
// edited after upload; this tab never starts or stops the sync.
//
// Settings (when the sync runs): the logsync block's paths and URLs, and its
// three credentials. {"t": "logsync_settings", do: get|plan|apply|secret|test}
// to the station (logsync_settings.py). Paths and URLs are planned first (the
// host-file diff is shown) and applied on the operator's word; a credential is
// write-only: its box is cleared as it is sent, and the page only ever hears
// whether it is set, how long and when it changed.

(() => {
  let body = null, snap = {}, loadedSrc = "", seen = null;

  function target() {
    if (snap.url) return snap.url;
    if (!snap.port) return "";
    const host = location.hostname.includes(":") ? `[${location.hostname}]` : location.hostname;
    return `${snap.scheme || "https"}://${host}:${snap.port}${snap.path || "/"}`;
  }

  // Why the frame can't show it, or "" when it can.
  function blocked(url) {
    return location.protocol === "https:" && url.startsWith("http:")
      ? "This page is secure (https) and the logbook is plain http, so the browser won't show it here. Open it in its own tab, or give the logbook an https address."
      : "";
  }

  function render() {
    if (!body) return;
    const url = target();
    if (!url) { body.innerHTML = ""; loadedSrc = ""; return; }
    const why = blocked(url);
    let frame = body.querySelector(".lb-frame");
    if (!body.querySelector(".lb-bar") || body.dataset.url !== url) {
      body.dataset.url = url;
      loadedSrc = "";
      body.innerHTML = `<div class="lb">
          <div class="lb-sync" hidden></div>
          <details class="lb-set" hidden><summary class="stat">Settings</summary><div class="lb-set-body stat">Loading…</div></details>
          <div class="lb-bar">
            <a class="chip chip-mini lb-open" href="${esc(url)}" target="_blank" rel="noopener">Open in its own tab</a>
            <span class="lb-url stat">${esc(url)}</span>
          </div>
          ${why ? `<div class="lb-why st-empty">${esc(why)}</div>`
                : `<iframe class="lb-frame" title="Logbook" referrerpolicy="no-referrer"></iframe>
                   <div class="lb-hint stat">Blank, or a certificate warning? Open it in its own tab once and accept the logbook's certificate, then come back.</div>`}
        </div>`;
      frame = body.querySelector(".lb-frame");
      watch();
    }
    renderSync();
    wireSettings();
    if (frame && visible() && loadedSrc !== url) { frame.src = url; loadedSrc = url; }
  }

  const NAMES = { wavelog: "Wavelog", qrz: "QRZ", lotw: "LoTW" };
  // One destination's counts from the sync's status: done, waiting, held, and
  // the reason it is stopped (the strip and the Shack card).
  function tally(s, d) {
    const c = (s.counts || {})[d] || {};
    return { done: (c.sent || 0) + (c.accepted || 0) + (c.confirmed || 0) + (c.submitted || 0),
             wait: (c.pending || 0) + (c.attempting || 0) + (c.unknown || 0), held: c.held || 0, hold: (s.holds || {})[d] };
  }
  const TICK = {
    sent: ["✓", "sent"], submitted: ["↑", "uploaded, waiting for LoTW to list it"],
    accepted: ["✓", "on LoTW"], confirmed: ["✓✓", "confirmed"], pending: ["…", "waiting to send"],
    attempting: ["…", "sending"], unknown: ["?", "result unknown: being checked"],
    held_backfill: ["–", "held for the backfill review"], held: ["✕", "held"],
  };

  // The sync's strip and recent QSOs. Every value is escaped; the server sends
  // only counts, states and reason classes (never a URL or a credential).
  function renderSync() {
    const el = body && body.querySelector(".lb-sync");
    if (!el) return;
    const s = snap.logsync;
    if (!s) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false;
    if (!s.loaded || s.running === false) {
      el.innerHTML = `<div class="lb-sync-bar st-empty">Logbook sync: ${esc(s.error || "not running")}</div>`;
      return;
    }
    const parts = (s.dests || []).map((d) => {
      const c = (s.counts || {})[d] || {};
      const { done, wait, held, hold } = tally(s, d);
      return `<span class="lb-dest${hold || held ? " lb-bad" : ""}" data-dest="${esc(d)}">${esc(NAMES[d] || d)} ${done}✓`
        + (wait ? ` · ${wait} waiting` : "") + (held ? ` · ${held} held` : "")
        + (c.held_backfill ? ` · ${c.held_backfill} backfill` : "")
        + (hold ? ` · stopped: ${esc(hold)}` : "") + `</span>`;
    });
    const rows = (s.recent || []).map((q) => {
      const ticks = (s.dests || []).map((d) => {
        const x = (q.d || {})[d];
        if (!x) return `<td></td>`;
        const [sym, what] = TICK[x.state] || ["?", x.state];
        const why = x.why ? `: ${x.why}` : "";
        const fix = x.edited && d !== "lotw"
          ? ` <button class="chip chip-mini lb-fix" data-id="${esc(q.id)}" data-dest="${esc(d)}" title="Send the edited QSO to ${esc(NAMES[d] || d)}">Send correction</button>` : "";
        return `<td class="lb-t lb-${esc(x.state)}" title="${esc(NAMES[d] || d)}: ${esc(what + why)}${x.edited ? " (edited after upload)" : ""}">${esc(sym)}${x.edited ? " ✎" : ""}${fix}</td>`;
      }).join("");
      return `<tr data-id="${esc(q.id)}"><td>${esc(q.time)}</td><td>${esc(q.call)}</td><td>${esc(q.band)}</td><td>${esc(q.mode)}</td>${ticks}`
        + `<td>${q.near ? `<span class="lb-near" title="Another QSO with them on this band and mode within 2 minutes: check it">near miss</span>` : ""}</td></tr>`;
    }).join("");
    el.innerHTML = `<div class="lb-sync-bar stat">Logbook sync (${esc(s.intake || "")}): ${parts.join(" ")}</div>`
      + (rows ? `<details class="lb-recent"><summary class="stat">Latest QSOs</summary><table class="lb-q">
           <tr><th>UTC</th><th>Call</th><th>Band</th><th>Mode</th>${(s.dests || []).map((d) => `<th>${esc(NAMES[d] || d)}</th>`).join("")}<th></th></tr>
           ${rows}</table></details>` : "");
    for (const b of el.querySelectorAll(".lb-fix")) {
      b.onclick = () => {
        if (!confirm(`Send the edited QSO to ${NAMES[b.dataset.dest] || b.dataset.dest}?`)) return;
        send({ t: "logsync", do: "correct", id: Number(b.dataset.id), dest: b.dataset.dest });
      };
    }
  }

  // ---------------------------------------------------------------- settings

  let setView = null, setPlan = null;
  const results = {};
  const GROUPS = [
    ["wavelog", "Wavelog", ["wavelog.url", "wavelog.station_profile_id", "wavelog.key_file"]],
    ["qrz", "QRZ", ["qrz.key_file"]],
    ["lotw", "LoTW", ["lotw.tqsl", "lotw.location", "lotw.login", "lotw.password_file"]],
  ];

  function setEl() { return body && body.querySelector(".lb-set"); }

  function wireSettings() {
    const d = setEl();
    if (!d) return;
    d.hidden = !snap.logsync;              // running or not: settings are how a broken one gets fixed
    if (d.dataset.wired) { renderSettings(); return; }
    d.dataset.wired = "1";
    d.addEventListener("toggle", () => { if (d.open) send({ t: "logsync_settings", do: "get" }); });
  }

  function secretState(s) {
    if (!s || !s.set) return `<span class="lb-bad">not set</span>`;
    const when = s.updated ? new Date(s.updated).toLocaleString() : "";
    return `set · ${esc(s.chars)} characters · updated ${esc(when)}` + (s.mode_ok === false ? ` · <span class="lb-bad">file readable by others</span>` : "");
  }

  function fieldInput(f, locations) {
    const id = `lbs-${f.key.replace(".", "-")}`;
    if (f.key === "lotw.location" && locations && locations.length) {
      const opts = locations.concat(locations.includes(f.value) ? [] : [f.value])
        .map((n) => `<option${n === f.value ? " selected" : ""}>${esc(n)}</option>`).join("");
      return `<label for="${id}">${esc(f.label)}</label><select id="${id}" data-key="${esc(f.key)}">${opts}</select>`;
    }
    const type = f.kind === "int" ? "number" : "text";
    return `<label for="${id}">${esc(f.label)}</label><input id="${id}" type="${type}" data-key="${esc(f.key)}" value="${esc(f.value ?? "")}" spellcheck="false" autocomplete="off">`;
  }

  function renderSettings() {
    const d = setEl();
    const el = d && d.querySelector(".lb-set-body");
    if (!el || !setView) return;
    const byKey = Object.fromEntries((setView.fields || []).map((f) => [f.key, f]));
    const groups = GROUPS.filter(([dest]) => (setView.secrets || {})[dest] || byKey[`${dest}.key_file`] || byKey[`${dest}.password_file`] || byKey[`${dest}.url`]);
    el.innerHTML = groups.map(([dest, title, keys]) => {
      const sec = (setView.secrets || {})[dest];
      const r = results[dest];
      return `<fieldset class="lb-grp" data-dest="${esc(dest)}"><legend>${esc(title)}</legend>
        <div class="lb-grid">${keys.filter((k) => byKey[k]).map((k) => fieldInput(byKey[k], setView.locations)).join("")}
        ${sec ? `<label for="lbs-sec-${esc(dest)}">${esc(sec.label)}</label>
          <span class="lb-sec"><input id="lbs-sec-${esc(dest)}" type="password" autocomplete="new-password" placeholder="type a new one to replace it" data-secret="${esc(dest)}">
          <button class="chip chip-mini lb-sec-save" data-dest="${esc(dest)}">Save</button>
          <span class="lb-sec-state">${secretState(sec)}</span></span>` : ""}</div>
        <div class="lb-test-row"><button class="chip chip-mini lb-test" data-dest="${esc(dest)}">Test ${esc(title)}</button>
          ${r ? `<span class="lb-test-msg ${r.ok ? "lb-ok" : "lb-bad"}">${r.ok ? "✓" : "✕"} ${esc(r.msg)}</span>` : ""}</div>
      </fieldset>`;
    }).join("") + `<div class="lb-set-actions"><button class="chip lb-plan">Review changes…</button>
        <span class="lb-set-note">Paths and addresses go in the host file (you'll see the change first); keys go straight to their files and are never shown.</span></div>
      ${setPlan ? `<div class="lb-plan-box"><div>Change ${setPlan.changes.map((c) => `<b>${esc(c.label)}</b>`).join(", ")}:</div>
          <pre class="lb-diff">${esc(setPlan.diff)}</pre>
          <button class="chip lb-apply">Apply and restart the sync</button> <button class="chip chip-mini lb-cancel">Cancel</button></div>` : ""}`;
    for (const b of el.querySelectorAll(".lb-sec-save")) {
      b.onclick = () => {
        const inp = el.querySelector(`input[data-secret="${b.dataset.dest}"]`);
        const v = inp.value;
        inp.value = "";                               // never left in the page
        if (!v.trim()) { toast("Type the new value first.", "warn"); return; }
        send({ t: "logsync_settings", do: "secret", dest: b.dataset.dest, value: v });
      };
    }
    for (const b of el.querySelectorAll(".lb-test")) {
      b.onclick = () => { results[b.dataset.dest] = { ok: true, msg: "testing…" }; renderSettings(); send({ t: "logsync_settings", do: "test", dest: b.dataset.dest }); };
    }
    el.querySelector(".lb-plan").onclick = () => {
      const changes = {};
      for (const i of el.querySelectorAll("[data-key]")) {
        const f = byKey[i.dataset.key];
        if (f && String(f.value ?? "") !== i.value) changes[i.dataset.key] = i.value;
      }
      if (!Object.keys(changes).length) { toast("Nothing changed.", "info"); return; }
      send({ t: "logsync_settings", do: "plan", changes });
    };
    const ap = el.querySelector(".lb-apply");
    if (ap) ap.onclick = () => { send({ t: "logsync_settings", do: "apply", id: setPlan.id }); setPlan = null; };
    const cn = el.querySelector(".lb-cancel");
    if (cn) cn.onclick = () => { setPlan = null; renderSettings(); };
  }

  function settingsEvent(m) {
    if (m.error) {
      if (m.do === "test" && m.dest) results[m.dest] = { ok: false, msg: m.error };
      toast(`Logbook settings: ${m.error}`, "warn");
    }
    if (m.view) setView = m.view;
    if (m.do === "plan" && m.plan) setPlan = m.plan;
    if (m.do === "apply" && m.applied) toast("Settings saved; the logbook sync restarted on them.", "info");
    if (m.do === "secret" && m.state) toast(`${(setView && setView.secrets[m.dest] || {}).label || m.dest} saved.`, "info");
    if (m.do === "test" && m.result) results[m.result.dest] = m.result;
    renderSettings();
  }

  function visible() { return !!body && body.offsetParent !== null; }

  // Load the frame when the tab is first shown (the logbook's dashboard is
  // heavy; most page opens never look at it).
  function watch() {
    if (seen || !body || typeof IntersectionObserver === "undefined") return;
    seen = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) render(); });
    seen.observe(body);
  }

  // A QSO's time ("YYYY-MM-DD HH:MM:SS", UTC, logsync._when) for the card:
  // "22:24Z" today, the date otherwise; anything else as given.
  function shortWhen(t) {
    const m = /^(\d{4}-\d\d-\d\d) (\d\d:\d\d)/.exec(t || "");
    if (!m) return t || "";
    return m[1] === new Date().toISOString().slice(0, 10) ? `${m[2]}Z` : m[1];
  }

  addons.register("logbook", {
    mount(b) { body = b; render(); },
    hello(s) { snap = s || {}; render(); },
    event(m) {
      if (m && m.settings) { settingsEvent(m); return; }
      if (m && m.logsync) { snap.logsync = m.logsync; renderSync(); wireSettings(); }
    },
    off() { snap = {}; render(); },
    tab() { return { dot: target() ? "ok" : "off", title: target() || "Your logbook" }; },
    // The Shack's card (2026-11-19, UX plan PR 0.4): the sync's state and counts and
    // the latest QSO, from the status it already sends. The logbook's address
    // only while there is nothing else to say (2026-11-23): under the counts it
    // was a raw URL wrapping the card, and the tab has it.
    card() {
      const url = target(), s = snap.logsync;
      const link = url ? `<a class="mc-dim" href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>` : "";
      if (!s) return { state: url ? "linked" : "off", lines: url ? [link] : [] };
      if (!s.loaded || s.running === false) return { state: "not running", lines: [`Sync: ${esc(s.error || "not running")}`, link] };
      let stopped = false;
      const dests = (s.dests || []).map((d) => {
        const { done, wait, held, hold } = tally(s, d);
        stopped = stopped || !!hold || held > 0;
        return `${esc(NAMES[d] || d)} ${done}✓` + (wait ? ` · ${wait} waiting` : "") + (held ? ` · ${held} held` : "")
          + (hold ? ` · stopped` : "");
      });
      const q = (s.recent || [])[0];
      return { state: stopped ? "held" : "syncing",              // a tag on the card (station.js)
               lines: [dests.join(" · ") || "No destinations",
                       q ? `Last: <b>${esc(q.call)}</b> ${esc(q.band)} ${esc(q.mode)} <span class="mc-dim">${esc(shortWhen(q.time))}</span>` : "No QSOs yet"] };
    },
  });
})();
