"use strict";

// UV-PRO add-on panel (addons/uvpro.py): the radio's live state, its channels
// as CHIRP memories (CHIRP's columns and values: addons/chirp.py) with an
// editor, CHIRP CSV export and a previewed import, radio settings, and the
// APRS beacon, plus the Radio pane: VFO A/B, volume, region, the FM broadcast
// receiver, the APRS path and power. Every change goes to the server as {"t":
// "addon", addon: "uvpro", do: "channel"|"settings"|"beacon"|"import_apply"|
// "volume"|"region"|"fm"|"aprs_path"|"radio_power", ...}; the server checks it
// again and refuses with a notice. Changes that alter what goes on the air
// (frequency, tone, power, region, path, power off...) are confirmed here
// first. Nothing here can key the radio.
// With the packet hub (borrow mode) the radio is with packet between reads:
// the page edits the last read, and a save borrows the radio for a moment.

(() => {
  let S = {};                 // the server's snapshot, kept current by events
  let api = null, root = null;
  let pane = "channels", editing = null;
  let importText = null, importPlan = null;
  const edits = { channel: {}, settings: {}, beacon: {} };

  const q = (s) => root.querySelector(s);
  const connected = () => !!S.status?.connected;
  const borrow = () => !!S.status?.borrow;
  // Editing works on the last read in borrow mode; a save borrows the radio.
  const usable = () => connected() || (borrow() && (S.memories || []).length > 0);
  const exactCh = (id) => !S.exact || S.exact.channels.includes(id);
  const exactKind = (kind) => kind === "channel" ? exactCh(editing) : (!S.exact || S.exact[kind]);
  const NOT_EXACT = "benlink does not read this back byte for byte, so it is not written from here";
  const hhmm = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const chName = (c) => c ? (c.vfo ? c.Location : c.Name || `CH${c.id + 1}`) : "?";
  const allMem = () => [...(S.memories || []), ...(S.vfos || [])];
  const chById = (id) => allMem().find((c) => c.id === id);
  const flat = (m) => (m ? { ...m, ...(m.extra || {}) } : m);   // CHIRP columns + extra settings, one level
  const bytes = (s) => new TextEncoder().encode(s).length;
  const TONE_FIELDS = ["Tone", "rToneFreq", "cToneFreq", "DtcsCode", "CrossMode"];   // may change the TX tone

  function toneText(m) {
    const side = (mode, tone, code) => (mode === "Tone" ? tone : mode === "DTCS" ? `D${code}` : "none");
    if (m.Tone === "Tone") return m.rToneFreq;
    if (m.Tone === "TSQL") return m.cToneFreq;
    if (m.Tone === "DTCS") return `D${m.DtcsCode}`;
    if (m.Tone === "Cross") {
      const [tx, rx] = m.CrossMode.split("->");
      return `${side(tx, m.rToneFreq, m.DtcsCode)} / ${side(rx, m.cToneFreq, m.RxDtcsCode)}`;
    }
    return "";
  }

  // What the channel sends as sub-audio, by CHIRP's split_tone_encode (as the
  // server works it out): a change here is an on-air change.
  function txTone(m) {
    if (m.Tone === "Tone") return `T${m.rToneFreq}`;
    if (m.Tone === "TSQL") return `T${m.cToneFreq}`;
    if (m.Tone === "DTCS") return `D${m.DtcsCode}`;
    if (m.Tone === "Cross") {
      const tx = m.CrossMode.split("->")[0];
      return tx === "Tone" ? `T${m.rToneFreq}` : tx === "DTCS" ? `D${m.DtcsCode}` : "";
    }
    return "";
  }

  // Which tone fields CHIRP uses for this Tone Mode (and Cross Mode).
  function visible(f, m) {
    if (!f.when) return true;
    if (!f.when.includes(m.Tone)) return false;
    if (m.Tone !== "Cross") return true;
    const [tx, rx] = (m.CrossMode || "->").split("->");
    return { rToneFreq: tx === "Tone", DtcsCode: tx === "DTCS", cToneFreq: rx === "Tone", RxDtcsCode: rx === "DTCS",
      CrossMode: true }[f.name] ?? true;
  }

  // ---------- status strip

  function renderStrip() {
    const st = S.state || {}, b = S.battery || {}, dev = S.device || {};
    const set = S.settings || {};
    const conn = q(".uv-conn"), err = S.status?.error;
    if (borrow()) {
      conn.textContent = connected() ? "Borrowed from packet" : S.status?.borrowed ? "Borrowing…" : "With packet";
      conn.className = "uv-conn stat" + (connected() ? " on" : err ? " warn" : "");
      conn.title = connected() ? "The add-on has the radio; it goes back to packet when nothing has happened for a few seconds"
        : err || "The packet hub has the radio. Saving a change, or Read radio, borrows it for a moment.";
    } else {
      conn.textContent = connected() ? (S.status?.via === "hub" ? "Connected via packet hub" : "Connected") : "Not connected";
      conn.className = "uv-conn stat" + (connected() ? " on" : " warn");
      conn.title = connected() ? `firmware ${dev.firmware_version ?? "?"} · ${dev.channel_count ?? "?"} channels`
        : (err || "The bridge has not reached the radio yet");
    }
    q(".uv-refresh").textContent = borrow() && !connected() ? "Read radio" : "Refresh";
    q(".uv-refresh").disabled = !!S.status?.borrowed && !connected();
    q(".uv-giveback").hidden = !(borrow() && connected());
    const a = chById(set.channel_a), bb = chById(set.channel_b);
    const items = [];
    if (borrow() && err && !connected()) items.push(`<span class="stat warn" title="${esc(err)}">${esc(err)}</span>`);
    if (borrow() && S.status?.read_at) items.push(`<span class="stat" title="Everything below is as the radio held it then">read ${hhmm(S.status.read_at)}</span>`);
    if (!connected() && usable()) {
      items.push(`<span class="uv-ch" title="Channel A"><b>A</b> ${esc(chName(a))} <span class="uv-f">${esc(a?.Frequency || "")}</span></span>`);
      if (b.percent != null) items.push(`<span class="stat uv-bat">🔋 ${b.percent}%</span>`);
    }
    const selfKey = selfKeying(set);
    if (selfKey.length) {
      items.push(`<span class="stat uv-selfkey" title="${esc(selfKey.join(" and "))} on: the radio transmits by itself, outside the packet hub's transmit gate, and the page refuses changes while it does. Turn it off on the radio or under Settings.">⚠ ${esc(selfKey.join(" + "))} on: transmits by itself</span>`);
    }
    if (connected()) {
      items.push(`<span class="uv-act ${st.is_in_tx ? "tx" : st.is_in_rx ? "rx" : st.is_sq ? "sq" : ""}">${
        st.is_in_tx ? "TX" : st.is_in_rx ? "RX" : st.is_sq ? "SQ" : "idle"}</span>`);
      items.push(`<span class="uv-ch${st.curr_ch_id === set.channel_a ? " cur" : ""}" title="Channel A"><b>A</b> ${esc(chName(a))} <span class="uv-f">${esc(a?.Frequency || "")}</span></span>`);
      if (st.double_channel && st.double_channel !== "OFF") {
        items.push(`<span class="uv-ch${st.curr_ch_id === set.channel_b ? " cur" : ""}" title="Channel B (dual watch)"><b>B</b> ${esc(chName(bb))} <span class="uv-f">${esc(bb?.Frequency || "")}</span></span>`);
      }
      if (typeof st.rssi === "number") items.push(`<span class="stat" title="Signal strength the radio reports">RSSI ${st.rssi}</span>`);
      if (st.is_power_on === false) items.push(`<span class="stat warn">powered off</span>`);
      const x = S.extras || {};
      if (x.volume != null) items.push(`<span class="stat" title="Volume (0–15)">🔊 ${x.volume}</span>`);
      if ((x.regions || []).length > 1 && x.region != null) {
        items.push(`<span class="stat" title="Region">${esc(x.regions[x.region] || `Region ${x.region + 1}`)}</span>`);
      }
      if (x.fm?.on) items.push(`<span class="stat on" title="FM broadcast receiver">FM ${x.fm.freq.toFixed(1)}</span>`);
      if (st.is_scan) items.push(`<span class="stat on">scanning</span>`);
      items.push(`<span class="stat${st.is_gps_locked ? " on" : ""}">${st.is_gps_locked ? "GPS lock" : "no GPS lock"}</span>`);
      if (b.percent != null || b.volts != null) {
        items.push(`<span class="stat uv-bat${b.percent != null && b.percent <= 20 ? " warn" : ""}">🔋 ${
          b.percent != null ? b.percent + "%" : ""}${b.volts != null ? ` · ${b.volts.toFixed(2)} V` : ""}</span>`);
      }
    }
    q(".uv-items").innerHTML = items.join("");
  }

  // ---------- channels

  function renderChannels() {
    const set = S.settings || {}, st = S.state || {};
    const rows = allMem().map((c) => {
      const tags = [c.id === set.channel_a ? "A" : "", c.id === set.channel_b ? "B" : ""].filter(Boolean).join("");
      const lock = exactCh(c.id) ? "" : ` <span class="uv-lock" title="${esc(NOT_EXACT)}">🔒</span>`;
      return `<tr data-id="${c.id}" class="${editing === c.id ? "sel" : ""}${c.id === st.curr_ch_id ? " cur" : ""}${c.vfo ? " uv-vfo" : ""}">
        <td class="n">${esc(c.Location)}</td><td class="nm">${esc(c.Name)}${tags ? ` <b class="uv-tag">${tags}</b>` : ""}${lock}</td>
        <td class="f">${esc(c.Frequency)}</td><td>${esc(c.Duplex)}</td><td class="o">${esc(c.Duplex === "+" || c.Duplex === "-" || c.Duplex === "split" ? c.Offset : "")}</td>
        <td>${esc(c.Tone)}</td><td>${esc(toneText(c))}</td><td>${esc(c.Mode)}</td><td>${esc(c.Power)}</td><td>${esc(c.Skip)}</td>
        <td class="ab"><button class="chip chip-mini" data-ab="channel_a" title="Tune A to this channel">A</button><button class="chip chip-mini" data-ab="channel_b" title="Tune B to this channel">B</button></td></tr>`;
    }).join("");
    q(".uv-mem tbody").innerHTML = rows || `<tr><td colspan="11" class="wl-dim">${connected() ? "Reading channels…"
      : borrow() ? "Not read yet: Read radio borrows it from packet for a moment" : "Not connected"}</td></tr>`;
    q(".uv-export").disabled = !usable() || !(S.memories || []).length;
    q(".uv-import-btn").disabled = !usable() || !(S.memories || []).length;
    for (const b of root.querySelectorAll(".uv-mem button[data-ab]")) b.disabled = !usable() || (S.exact && !S.exact.settings);
    renderEditor();
  }

  function onTableClick(e) {
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    const id = Number(tr.dataset.id);
    const ab = e.target.closest("button[data-ab]");
    if (ab) {
      api.send("settings", { values: { [ab.dataset.ab]: id } });
      return;
    }
    if (editing !== id) {
      editing = id;
      edits.channel = {};
    }
    renderChannels();
  }

  function renderEditor() {
    const box = q(".uv-editor"), c = chById(editing);
    box.hidden = !c;
    if (!c) return;
    q(".uv-ed-title").textContent = `Location ${c.Location}: ${chName(c)}`;
    const form = q(".uv-ed-form");
    form.innerHTML = "";
    const cur = flat(c), now = { ...cur, ...edits.channel };
    for (const f of S.schema?.channel || []) {
      if (!visible(f, now)) continue;
      if (f.readonly || (c.vfo && f.name === "Name")) {
        // Location, TStep and polarity: the radio's fixed values; a VFO has no name.
        const lab = document.createElement("label");
        lab.className = "uv-ro";
        lab.title = "Fixed on this radio";
        lab.innerHTML = `<span>${esc(f.label)}${f.unit ? ` <span class="wl-dim" style="display:inline">(${esc(f.unit)})</span>` : ""}</span>` +
          `<span class="uv-ro-v">${esc(cur[f.name] || (f.name === "Duplex" ? "(simplex)" : ""))}</span>`;
        form.append(lab);
        continue;
      }
      form.append(field(f, cur, edits.channel, (name) => {
        if (name === "Tone" || name === "CrossMode") renderEditor(); else renderFoot("channel");
      }));
    }
    form.insertAdjacentHTML("beforeend", `<p class="wl-dim uv-tx-fixed">Frequency, Duplex and Offset set RX and TX as in CHIRP
      (Duplex off = RX only; split = Offset is the TX frequency). The radio reads each write back and a TX it can't send is reported.</p>`);
    if (!exactCh(c.id)) form.insertAdjacentHTML("beforeend", `<p class="wl-dim uv-tx-fixed">🔒 Not editable: ${esc(NOT_EXACT)}.</p>`);
    renderFoot("channel");
  }

  // ---------- generic forms (settings, beacon, channel editor)

  const valueOf = (e, cur, name) => (name in e ? e[name] : cur?.[name]);

  function setEdit(e, cur, name, v) {
    if (v === cur?.[name] || (typeof v === "number" && typeof cur?.[name] === "number" && Math.abs(v - cur[name]) < 1e-7)) delete e[name];
    else e[name] = v;
  }

  function invalid(f, v) {
    if (f.type === "int" || f.type === "channel") {
      if (typeof v !== "number" || !Number.isInteger(v)) return "a whole number";
      if (f.type === "int" && (v < f.min || v > f.max)) return `${f.min}–${f.max}`;
      if (f.step && v % f.step) return `a multiple of ${f.step}`;
    }
    if (f.type === "str" && f.max_bytes && bytes(v) > f.max_bytes) return `at most ${f.max_bytes} bytes`;
    if (f.type === "freq" && !/^\s*\d+(\.\d{0,6})?\s*$/.test(String(v ?? ""))) return "a frequency in MHz";
    return null;
  }

  function field(f, cur, e, after) {
    const v = valueOf(e, cur, f.name);
    let el;
    if (f.type === "bool") {
      el = document.createElement("input");
      el.type = "checkbox";
      el.checked = !!v;
    } else if (f.type === "enum" || f.type === "channel" || (f.name === "aprs_callsign" && S.calls?.length)) {
      el = document.createElement("select");
      let opts;
      if (f.type === "channel") opts = allMem().map((c) => [c.id, c.vfo ? c.Location : `${c.Location} ${chName(c)}`]);
      else if (f.name === "aprs_callsign") opts = S.calls.map((x) => [x, x]);
      else opts = f.options.map((o, i) => [o, f.labels?.[i] ?? (o === "" ? ({ Tone: "(none)", Skip: "(scan)", Duplex: "(simplex)" }[f.name] || "none") : String(o))]);
      if (!opts.some(([o]) => o === v)) opts.push([v, String(v ?? "")]);
      el.innerHTML = opts.map(([o, l]) => `<option value="${esc(String(o ?? ""))}"${o === v ? " selected" : ""}>${esc(l)}</option>`).join("");
      el._opts = opts;
    } else {
      el = document.createElement("input");
      el.type = f.type === "str" || f.type === "freq" ? "text" : "number";
      if (f.type === "freq") el.inputMode = "decimal";
      if (f.type === "int") { el.step = f.step || 1; el.min = f.min; el.max = f.max; }
      el.value = v ?? "";
      el.spellcheck = false;
    }
    const kind = e === edits.channel ? "channel" : e === edits.settings ? "settings" : "beacon";
    el.disabled = !usable() || !exactKind(kind);
    const lab = document.createElement("label");
    lab.innerHTML = `<span>${esc(f.label)}${f.unit ? ` <span class="wl-dim" style="display:inline">(${esc(f.unit)})</span>` : ""}` +
      (f.risky ? `<span class="risky" title="${esc(f.risky)}">⚠</span>` : "") +
      (f.hint ? `<span class="wl-dim">${esc(f.hint)}</span>` : "") + `</span>`;
    lab.title = f.risky || f.hint || "";
    lab.append(el);
    const read = () => {
      if (f.type === "bool") return el.checked;
      if (f.type === "int") return el.value === "" ? NaN : Number(el.value);
      if (el._opts) return el._opts[el.selectedIndex][0];
      return el.value;
    };
    el.addEventListener(el.tagName === "SELECT" || f.type === "bool" ? "change" : "input", () => {
      setEdit(e, cur, f.name, read());
      lab.classList.toggle("dirty", f.name in e);
      lab.classList.toggle("bad", f.name in e && !!invalid(f, e[f.name]));
      after?.(f.name, e[f.name]);
    });
    lab.classList.toggle("dirty", f.name in e);
    return lab;
  }

  const kindOf = { channel: () => flat(chById(editing)), settings: () => S.settings, beacon: () => S.beacon };

  function renderFoot(kind) {
    const foot = kind === "channel" ? q(".uv-ed-foot") : q(".uv-form-foot");
    const e = edits[kind], names = Object.keys(e);
    const fields = S.schema?.[kind] || [];
    const bad = names.map((n) => [fields.find((f) => f.name === n), e[n]]).filter(([f, v]) => f && invalid(f, v));
    foot.querySelector(".uv-save").disabled = !usable() || !exactKind(kind) || !names.length || bad.length > 0;
    foot.querySelector(".uv-revert").disabled = !names.length;
    foot.querySelector(".wl-dim").textContent = bad.length ? `${bad[0][0].label}: ${invalid(...bad[0])}`
      : names.length ? `${names.length} change${names.length > 1 ? "s" : ""} not saved` : "";
  }

  function save(kind) {
    const e = edits[kind], cur = kindOf[kind]();
    if (!Object.keys(e).length || !cur) return;
    const fields = S.schema?.[kind] || [];
    const risky = fields.filter((f) => f.risky && f.name in e);
    const lines = risky.map((f) => `${f.label}: ${String(cur[f.name] ?? "none")} → ${String(e[f.name] ?? "none")}\n  ${f.risky}`);
    if (kind === "channel" && TONE_FIELDS.some((n) => n in e)) {
      const next = { ...cur, ...e };
      if (txTone(cur) !== txTone(next)) {
        lines.unshift(`Tone: ${cur.Tone || "(none)"} ${toneText(cur)} → ${next.Tone || "(none)"} ${toneText(next)}\n  Changes the TX tone.`);
      }
    }
    let confirmed = false;
    if (lines.length) {
      if (!confirm("These may change what the UV-PRO puts on the air:\n\n" + lines.join("\n") + "\n\nSave to the radio?")) return;
      confirmed = true;
    }
    api.send(kind, { ...(kind === "channel" ? { id: editing } : {}), values: { ...e }, confirm: confirmed });
  }

  function renderForm() {
    if (pane === "channels" || pane === "radio") return;
    const kind = pane, cur = kindOf[kind]();
    const form = q(".uv-form-form");
    form.innerHTML = "";
    if (!cur) {
      form.innerHTML = `<p class="wl-dim">${connected() ? "Reading the radio…" : borrow() ? "Not read yet: Read radio borrows it from packet for a moment" : "Not connected"}</p>`;
    } else {
      for (const f of S.schema?.[kind] || []) form.append(field(f, cur, edits[kind], () => renderFoot(kind)));
    }
    q(".uv-form-note").textContent = kind === "beacon"
      ? "Position beacons make the radio transmit by itself under the callsign shown. Each one asks first."
      : "Only the settings with a clear meaning are here; the rest stay as set on the radio.";
    if (!exactKind(kind)) q(".uv-form-note").textContent += ` 🔒 Not editable: ${NOT_EXACT}.`;
    renderFoot(kind);
  }

  // drop edits the radio now matches
  function settle(kind) {
    const cur = kindOf[kind]();
    for (const [k, v] of Object.entries(edits[kind])) setEdit(edits[kind], cur, k, v);
  }

  function renderPane() {
    for (const b of root.querySelectorAll(".uv-panes .chip")) on(b, b.dataset.v === pane);
    q(".uv-channels").hidden = pane !== "channels";
    q(".uv-radio").hidden = pane !== "radio";
    q(".uv-form").hidden = pane === "channels" || pane === "radio";
    if (pane === "channels") renderChannels(); else if (pane === "radio") renderRadio(); else renderForm();
  }

  function renderAll() {
    if (!root) return;
    renderStrip();
    renderPane();
  }

  // ---------- radio pane: VFOs, volume, region, FM, APRS path, power

  let lastPower = null;
  function radioStateChanged(st) {
    const changed = st.is_power_on !== lastPower;
    lastPower = st.is_power_on;
    return changed;
  }

  function ask(text) { return confirm(text); }

  function renderRadio() {
    const box = q(".uv-radio");
    if (!box || pane !== "radio") return;
    if (box.contains(document.activeElement) && document.activeElement.tagName === "INPUT"
        && document.activeElement.type !== "range") return;          // don't wipe a half-typed field
    const x = S.extras || {}, dev = S.device || {}, set = S.settings || {}, st = S.state || {};
    const dis = !usable() ? " disabled" : "";
    const vfo = (S.vfos || []).map((v) => {
      const on = set.channel_a === v.id ? "A" : set.channel_b === v.id ? "B" : "";
      return `<div class="wl-row uv-vfo-row" data-id="${v.id}"><b>${esc(v.Location)}</b>
        <input class="uv-vfo-f" inputmode="decimal" value="${esc(v.Frequency)}" size="12"${dis} title="RX frequency (MHz); TX follows Duplex/Offset"> MHz
        <span class="wl-dim">${esc(v.Duplex || "simplex")}${v.Duplex === "+" || v.Duplex === "-" ? " " + esc(v.Offset) : ""} · ${esc(v.Mode)} · ${esc(v.Power)}${toneText(v) ? " · " + esc(toneText(v)) : ""}</span>
        <button class="chip chip-mini uv-vfo-tune"${dis}>Tune</button>
        <button class="chip chip-mini uv-vfo-edit"${dis} title="Duplex, offset, tone, mode, power: the channel editor">Edit…</button>
        <button class="chip chip-mini uv-vfo-ab" data-ab="channel_a"${dis}${on === "A" ? " disabled" : ""} title="Put side A on this VFO">A</button>
        <button class="chip chip-mini uv-vfo-ab" data-ab="channel_b"${dis}${on === "B" ? " disabled" : ""} title="Put side B on this VFO">B</button>
        ${on ? `<span class="stat on">side ${on}</span>` : ""}</div>`;
    }).join("") || `<p class="wl-dim">${usable() ? "The radio has no VFO records (251/252)." : "Not read yet."}</p>`;
    const regions = (x.regions || []).map((n, i) => `<option value="${i}"${i === x.region ? " selected" : ""}>${i + 1}. ${esc(n || "(no name)")}</option>`).join("");
    const fm = x.fm;
    box.innerHTML = `
      <div class="wl-head"><span class="label">VFO</span><span class="wl-dim">records 251 / 252: tuning changes RX and TX</span></div>
      ${vfo}
      <div class="wl-head"><span class="label">Audio</span></div>
      <div class="wl-row"><label class="uv-inline">Volume <input type="range" class="uv-vol" min="0" max="15" step="1" value="${x.volume ?? 0}"${dis}${x.volume == null ? " disabled" : ""}>
        <span class="uv-vol-v">${x.volume ?? "?"}</span></label></div>
      <div class="wl-head"><span class="label">Region</span><span class="wl-dim">a region is a set of channels; switching reads the new set</span></div>
      <div class="wl-row">${regions ? `<select class="uv-region"${dis}>${regions}</select>
        <button class="chip chip-mini uv-region-go"${dis}>Switch</button>` : `<span class="wl-dim">${usable() ? "Regions not read" : "Not read yet."}</span>`}</div>
      ${dev.supports_radio ? `<div class="wl-head"><span class="label">FM broadcast</span>
        <span class="wl-dim">${fm ? (fm.on ? `on · ${fm.freq.toFixed(1)} MHz${fm.seeking ? " · seeking…" : ""}` : "off") : "not read"}</span></div>
      <div class="wl-row"><button class="chip chip-mini uv-fm-on${fm?.on ? " on" : ""}"${dis}>${fm?.on ? "Turn off" : "Turn on"}</button>
        <input class="uv-fm-f" inputmode="decimal" size="6" value="${fm ? fm.freq.toFixed(1) : ""}"${dis}> MHz
        <button class="chip chip-mini uv-fm-tune"${dis}>Tune</button>
        <button class="chip chip-mini uv-fm-seek" data-dir="down"${dis} title="Seek down">◀ Seek</button>
        <button class="chip chip-mini uv-fm-seek" data-dir="up"${dis} title="Seek up">Seek ▶</button></div>` : ""}
      <div class="wl-head"><span class="label">APRS path</span><span class="wl-dim">the digipeater path the radio's own packets ask for</span></div>
      <div class="wl-row"><input class="uv-path" size="28" spellcheck="false" value="${esc(x.aprs_path ?? "")}" placeholder="WIDE1-1,WIDE2-1"${dis}${x.aprs_path == null ? " disabled" : ""}>
        <button class="chip chip-mini uv-path-save"${dis}>Save</button></div>
      ${x.power_control ? `<div class="wl-head"><span class="label">Power</span><span class="wl-dim">${st.is_power_on === false ? "the radio is off" : "the radio is on"}</span></div>
      <div class="wl-row"><button class="chip chip-mini uv-pwr" data-on="${st.is_power_on === false}"${dis}>${st.is_power_on === false ? "Power on" : "Power off…"}</button>
        <span class="wl-dim">Off is the radio's soft off; it keeps Bluetooth, so it can be turned on again from here (not yet tried on linux-station's radio).</span></div>` : ""}`;
    for (const row of box.querySelectorAll(".uv-vfo-row")) {
      const id = Number(row.dataset.id), v = chById(id);
      const tune = () => {
        const f = row.querySelector(".uv-vfo-f").value.trim();
        if (!/^\d+(\.\d{0,6})?$/.test(f)) { row.querySelector(".uv-vfo-f").classList.add("bad"); return; }
        if (!ask(`Tune ${v.Location} to ${f} MHz?\n\nThe TX frequency follows (${v.Duplex || "simplex"}): this changes what the UV-PRO transmits on.`)) return;
        api.send("channel", { id, values: { Frequency: f }, confirm: true });
      };
      row.querySelector(".uv-vfo-tune").onclick = tune;
      row.querySelector(".uv-vfo-f").onkeydown = (e) => { if (e.key === "Enter") tune(); };
      row.querySelector(".uv-vfo-edit").onclick = () => { pane = "channels"; editing = id; edits.channel = {}; renderPane(); };
      for (const b of row.querySelectorAll(".uv-vfo-ab")) b.onclick = () => api.send("settings", { values: { [b.dataset.ab]: id } });
    }
    const vol = box.querySelector(".uv-vol");
    if (vol) {
      vol.oninput = () => { box.querySelector(".uv-vol-v").textContent = vol.value; };
      vol.onchange = () => api.send("volume", { values: { level: Number(vol.value) } });
    }
    const go = box.querySelector(".uv-region-go");
    if (go) go.onclick = () => {
      const r = Number(box.querySelector(".uv-region").value);
      if (r === x.region) return;
      if (!ask(`Switch the UV-PRO to region ${r + 1} (${x.regions[r] || "no name"})?\n\nIts channels, and what A and B are on, become that region's.`)) return;
      api.send("region", { values: { region: r }, confirm: true });
    };
    const fmOn = box.querySelector(".uv-fm-on");
    if (fmOn) {
      fmOn.onclick = () => api.send("fm", { values: { on: !fm?.on } });
      const tuneFm = () => {
        const f = Number(box.querySelector(".uv-fm-f").value);
        if (!(f >= 64 && f <= 108)) { box.querySelector(".uv-fm-f").classList.add("bad"); return; }
        api.send("fm", { values: { freq: f } });
      };
      box.querySelector(".uv-fm-tune").onclick = tuneFm;
      box.querySelector(".uv-fm-f").onkeydown = (e) => { if (e.key === "Enter") tuneFm(); };
      for (const b of box.querySelectorAll(".uv-fm-seek")) b.onclick = () => api.send("fm", { values: { seek: b.dataset.dir } });
    }
    const ps = box.querySelector(".uv-path-save");
    if (ps) ps.onclick = () => {
      const path = box.querySelector(".uv-path").value.trim().toUpperCase().replace(/\s+/g, "");
      if (path === (x.aprs_path || "")) return;
      if (!ask(`Set the UV-PRO's APRS path to ${path || "(none: direct only)"}?\n\nThis changes how far its own packets are digipeated.`)) return;
      api.send("aprs_path", { values: { path }, confirm: true });
    };
    const pw = box.querySelector(".uv-pwr");
    if (pw) pw.onclick = () => {
      const on = pw.dataset.on === "true";
      if (!on && !ask("Power the UV-PRO off?\n\nIt stops receiving (packet included) until it is turned on again.")) return;
      api.send("radio_power", { values: { on }, confirm: !on });
    };
  }

  // ---------- add-on interface

  function mount(body, a) {
    api = a;
    root = body;
    body.innerHTML = `
      <div class="uv-strip"><span class="uv-conn stat">Not connected</span><span class="uv-items"></span>
        <span class="spacer"></span><button class="chip chip-mini uv-giveback" hidden title="Give the radio back to packet now">Give back now</button>
        <button class="chip chip-mini uv-refresh" title="Read everything from the radio again">Refresh</button></div>
      <div class="wl-bar"><div class="seg uv-panes"></div></div>
      <div class="uv-channels">
        <div class="wl-row uv-chirp-bar">
          <button class="chip chip-mini uv-export" title="Download the channels as a CHIRP CSV file">Export CHIRP CSV</button>
          <button class="chip chip-mini uv-import-btn" title="Load a CHIRP CSV; you see every change before anything is written">Import CHIRP CSV…</button>
          <input type="file" class="uv-import-file" accept=".csv,text/csv" hidden>
          <span class="wl-dim">CHIRP columns. VFO A and B are below the channels.</span></div>
        <div class="uv-import mesh-cfg" hidden>
          <div class="wl-head"><span class="label">Import preview</span><span class="wl-dim uv-import-sum"></span><span class="spacer"></span>
            <button class="chip chip-mini uv-import-close">Cancel</button></div>
          <div class="uv-table-wrap"><table class="uv-table uv-import-table"><thead><tr><th>Loc</th><th>Name</th><th>Result</th><th>Details</th></tr></thead><tbody></tbody></table></div>
          <div class="wl-row"><button class="chip on uv-import-apply" disabled>Write changes</button></div>
        </div>
        <div class="uv-table-wrap"><table class="uv-table uv-mem"><thead><tr><th>Loc</th><th>Name</th><th>Frequency</th><th>Duplex</th>
          <th>Offset</th><th>Tone Mode</th><th>Tone / DTCS</th><th>Mode</th><th>Power</th><th>Skip</th><th>Tune</th></tr></thead><tbody></tbody></table></div>
        <div class="uv-editor mesh-cfg" hidden>
          <div class="wl-head"><span class="label uv-ed-title"></span><span class="spacer"></span>
            <button class="chip chip-mini uv-ed-close">Close</button></div>
          <form class="mesh-cfg-form uv-ed-form" autocomplete="off"></form>
          <div class="wl-row uv-ed-foot"><button class="chip on uv-save" disabled>Save to radio</button>
            <button class="chip uv-revert" disabled>Revert</button><span class="wl-dim"></span></div>
        </div>
      </div>
      <div class="uv-radio mesh-cfg" hidden></div>
      <div class="uv-form mesh-cfg" hidden>
        <form class="mesh-cfg-form uv-form-form" autocomplete="off"></form>
        <div class="wl-row uv-form-foot"><button class="chip on uv-save" disabled>Save to radio</button>
          <button class="chip uv-revert" disabled>Revert</button><span class="wl-dim"></span></div>
        <p class="wl-dim uv-form-note"></p>
      </div>`;
    const seg = q(".uv-panes");
    for (const [v, l] of [["channels", "Channels"], ["radio", "Radio"], ["settings", "Settings"], ["beacon", "APRS beacon"]]) {
      seg.append(chip(l, v, () => { pane = v; renderPane(); }));
    }
    for (const f of root.querySelectorAll("form")) f.onsubmit = (e) => e.preventDefault();
    q(".uv-mem").addEventListener("click", onTableClick);
    q(".uv-export").onclick = () => api.send("export");
    q(".uv-import-btn").onclick = () => q(".uv-import-file").click();
    q(".uv-import-file").onchange = async (e) => {
      const file = e.target.files[0];
      e.target.value = "";
      if (!file) return;
      importText = await file.text();
      importPlan = null;
      api.send("import_preview", { csv: importText });
    };
    q(".uv-import-close").onclick = () => { importText = importPlan = null; renderImport(); };
    q(".uv-import-apply").onclick = () => {
      if (!importPlan || !importText) return;
      let confirmed = false;
      if (importPlan.risky) {
        if (!confirm("Some of these changes may alter what the UV-PRO puts on the air (frequency, TX tone, power):\n\n" +
          importPlan.rows.filter((r) => r.risky?.length).map((r) => `Location ${r.loc} ${r.name}: ${r.risky.join(", ")}`).join("\n") +
          "\n\nWrite them to the radio?")) return;
        confirmed = true;
      }
      api.send("import_apply", { csv: importText, confirm: confirmed });
      importText = importPlan = null;
      renderImport();
    };
    q(".uv-ed-close").onclick = () => { editing = null; edits.channel = {}; renderChannels(); };
    q(".uv-ed-foot .uv-save").onclick = () => save("channel");
    q(".uv-ed-foot .uv-revert").onclick = () => { edits.channel = {}; renderEditor(); };
    q(".uv-form-foot .uv-save").onclick = () => save(pane);
    q(".uv-form-foot .uv-revert").onclick = () => { edits[pane] = {}; renderForm(); };
    q(".uv-refresh").onclick = () => api.send("refresh");
    q(".uv-giveback").onclick = () => api.send("give_back");
    renderAll();
  }

  function renderImport() {
    const box = q(".uv-import");
    box.hidden = !importPlan;
    if (!importPlan) return;
    const p = importPlan;
    if (p.error) {
      q(".uv-import-sum").textContent = p.error;
      q(".uv-import-table tbody").innerHTML = "";
      q(".uv-import-apply").disabled = true;
      return;
    }
    const same = p.rows.filter((r) => r.status === "same").length;
    q(".uv-import-sum").textContent = `${p.changes} to change · ${same} the same · ${p.refused} refused`;
    q(".uv-import-table tbody").innerHTML = p.rows.map((r) => {
      const det = r.status === "change" ? r.diff.map(([f, a, b]) => `${f}: ${a || "(none)"} → ${b || "(none)"}`).join(" · ")
        + (r.tone && r.tone[0] !== r.tone[1] ? ` · tone ${r.tone[0] || "none"} → ${r.tone[1] || "none"}` : "")
        : r.status === "refused" ? r.why : "";
      const res = { change: r.risky?.length ? "change ⚠" : "change", same: "same", refused: "refused" }[r.status];
      return `<tr class="uv-imp-${r.status}"><td>${esc(r.loc)}</td><td>${esc(r.name)}</td><td>${res}</td><td>${esc(det)}</td></tr>`;
    }).join("");
    q(".uv-import-apply").disabled = !usable() || !p.changes;
    q(".uv-import-apply").textContent = `Write ${p.changes} channel${p.changes === 1 ? "" : "s"}`;
  }

  function download(text) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
    a.download = `uv-pro-channels-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  function hello(snap) {
    S = { ...snap };
    renderAll();
  }

  function event(m) {
    if (m.status) S.status = m.status;
    if ("exact" in m) S.exact = m.exact;
    if (m.device) S.device = m.device;
    if (m.memories) S.memories = m.memories;
    if (m.vfos) { S.vfos = m.vfos; if (S.vfos.some((c) => c.id === editing)) settle("channel"); }
    if (m.extras) S.extras = m.extras;
    if (m.memory) {
      const list = S.memories || [];
      const i = list.findIndex((c) => c.id === m.memory.id);
      if (i >= 0) list[i] = m.memory; else list.push(m.memory);
      S.memories = list;
      if (m.memory.id === editing) settle("channel");
    }
    if (m.export) { window.lastExport = m.export.csv; download(m.export.csv); }
    if (m.import_plan) { importPlan = m.import_plan; if (root) renderImport(); }
    if (m.settings) { S.settings = m.settings; settle("settings"); }
    if (m.beacon) { S.beacon = m.beacon; settle("beacon"); }
    if (m.state) S.state = m.state;
    if (m.battery) S.battery = m.battery;
    if (!root) return;
    renderStrip();
    // Live state (RX/TX/RSSI) arrives often: re-render the forms only when
    // their data changed, so a half-typed field is not wiped.
    if (m.status || m.memories || m.memory || m.vfos || m.settings || "exact" in m) {
      if (pane === "channels") renderChannels(); else if (pane !== "radio" && (m.status || m.settings)) renderForm();
    }
    if (pane === "radio" && (m.status || m.extras || m.vfos || m.settings || (m.state && radioStateChanged(m.state)))) renderRadio();
    if ((m.beacon || m.status) && pane === "beacon") renderForm();
    if (m.state && pane === "channels") {
      for (const tr of root.querySelectorAll(".uv-mem tr[data-id]")) tr.classList.toggle("cur", Number(tr.dataset.id) === m.state.curr_ch_id);
    }
  }

  function off() {
    S = {};
    editing = null;
    importText = importPlan = null;
    for (const k of Object.keys(edits)) edits[k] = {};
    renderAll();
    renderImport();
  }

  // Settings that key the radio by itself (addons/uvpro.py SELF_KEYING).
  function selfKeying(set) {
    return [["auto_relay_en", "Auto relay"], ["vox_en", "VOX"]].filter(([k]) => set && set[k]).map(([, l]) => l);
  }

  function tab() {
    const st = S.state || {};
    if (!connected() && usable()) return { dot: "ok", off: false, title: "UV-PRO: with packet; editing the last read" };
    const sk = selfKeying(S.settings);
    return { dot: !connected() ? "off" : st.is_in_tx ? "tx" : "ok", off: !connected(),
      title: (connected() ? `UV-PRO: ${chName(chById(st.curr_ch_id))}` : `UV-PRO: ${S.status?.error || "not connected"}`)
        + (sk.length ? ` · ${sk.join(" + ")} on: transmits by itself` : "") };
  }

  function card() {
    if (!connected() && usable()) {
      return { state: "ok", lines: [`With packet · read ${S.status?.read_at ? hhmm(S.status.read_at) : "?"}`,
        `${(S.memories || []).length} channels`, "Open →"] };
    }
    if (!connected()) return { state: "off", lines: [esc(S.status?.error || "Not connected"), "Open →"] };
    const st = S.state || {}, c = chById(st.curr_ch_id), b = S.battery || {};
    return { state: st.is_in_tx ? "tx" : "ok", lines: [
      c ? `${esc(chName(c))} <code>${esc(c.Frequency)}</code>` : "Connected",
      b.percent != null ? `Battery ${b.percent}%` : "",
      selfKeying(S.settings).length ? `⚠ ${esc(selfKeying(S.settings).join(" + "))} on` : "",
      "Open →"].filter(Boolean) };
  }

  addons.register("uvpro", { mount, hello, event, off, tab, card });
})();
