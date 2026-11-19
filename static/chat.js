"use strict";

// The chat view shared by the Mesh tab (mesh.js) and the MeshCore tab
// (addons/meshcore.js), 2026-11-19. Before this each drew its own one-line
// rows in a 260 px box, in monospace, and the operator reached for the phone
// to answer. Now: bubbles, the sender's name and colour once per run of
// messages, day lines, a "new" line, a jump-to-latest button, a hover (or
// tap) bar to react or reply, an emoji picker, and a composer that grows and
// sends on Enter. Each tab still owns its data and its protocol: it hands
// this module plain items and gets callbacks back, so a rule about what may
// be sent (bytes, parts, replies) stays in the tab that knows it.
//
// An item: {key, mine, t, who, tag (a short name beside it), whoTitle, seed, peer, text, sub,
//           quote: {key, who, text} | {missing: true} | null,
//           reacts: [{emoji, who: [names]}], ack: {glyph, why, cls} | null,
//           extra: html (the tab's own buttons, data-act), canReply, canReact}

const chat = (() => {
  const GROUP_S = 5 * 60;          // one sender's run of messages, unbroken by more than this
  const QUICK = ["👍", "❤️", "😂", "‼️", "❓", "🎉"];
  const EMOJI = [
    ["Recent", []],
    ["Smileys", "😀 😃 😄 😁 😆 😅 🤣 😂 🙂 😉 😊 😇 🥰 😍 😘 😋 😛 😜 🤪 🤓 😎 🥳 🤩 🤔 🤨 😐 😑 😶 🙄 😏 😬 😮‍💨 😌 😴 🤤 😷 🤒 🥵 🥶 😵 🤯 🤠 😕 😟 🙁 😮 😲 😳 🥺 😢 😭 😱 😤 😡 🤬 💀 🤡 👻 👽 🤖 😺"],
    ["Gestures", "👍 👎 👌 🤌 ✌️ 🤞 🤟 🤘 🤙 👈 👉 👆 👇 ☝️ ✋ 🤚 🖐️ 🖖 👋 🤝 🙏 👏 🙌 👐 💪 🫡 🫶 ✍️ 👀 🧠 🫠"],
    ["Hearts", "❤️ 🧡 💛 💚 💙 💜 🖤 🤍 🤎 💔 ❣️ 💕 💞 💓 💗 💖 💘 💝 💯 💢 💥 💫 💦 💨 ✨ ⭐ 🌟 🔥"],
    ["Radio", "📻 📡 🛰️ 📶 🔋 🪫 🔌 💡 🔦 🧭 🗺️ 📍 🛜 📟 📞 ☎️ 📱 💻 🖥️ ⌚ ⏰ ⏳ 🔊 🔇 🎙️ 🎧 🔧 🔨 🛠️ ⚙️ 🧰 🆘 🚨 ⚠️"],
    ["Outdoors", "☀️ 🌤️ ⛅ 🌥️ ☁️ 🌧️ ⛈️ 🌩️ 🌨️ ❄️ 🌪️ 🌫️ 🌈 🌙 🌕 ⛰️ 🏔️ 🌵 🌲 🌳 🌾 🌻 🌼 🐴 🐎 🐕 🐶 🐈 🐓 🐔 🐣 🦅 🦉 🐍 🦂 🐄 🐝 🦋 🚙 🛻 🚜 🏕️ 🔭 🌌"],
    ["Food", "☕ 🍵 🍺 🍻 🥃 🍷 🥤 🍩 🍪 🎂 🍰 🥧 🍕 🌮 🌯 🍔 🌭 🥓 🍳 🥞 🍎 🍌 🍉 🍓 🌶️ 🥕 🥗 🍞 🧀"],
    ["Symbols", "✅ ☑️ ✔️ ❌ ❎ ➕ ➖ ‼️ ⁉️ ❓ ❔ ❗ 💤 🎉 🎊 🏁 🚩 🎯 🏆 🥇 📌 📎 🔒 🔓 🔑 ♻️ 🆗 🆕 🆒 ℹ️ 🔴 🟠 🟡 🟢 🔵 🟣 ⚫ ⚪"],
  ].map(([n, s]) => [n, Array.isArray(s) ? s : s.split(" ")]);
  const RECENT_KEY = "chat.emoji.recent";

  // ---------- helpers

  const hash = (s) => { let h = 0; for (const c of String(s || "")) h = (h * 31 + c.codePointAt(0)) >>> 0; return h; };
  const hue = (seed) => hash(seed) % 360;
  const nameColor = (seed) => `hsl(${hue(seed)} 72% 72%)`;
  const initials = (s) => {
    const w = String(s || "?").replace(/[^\p{L}\p{N}\p{Extended_Pictographic} ]/gu, "").trim();
    const parts = w.split(/\s+/).filter(Boolean);
    const g = (x) => [...x][0] || "";
    return ((parts.length > 1 ? g(parts[0]) + g(parts[1]) : [...(parts[0] || w || "?")].slice(0, 2).join("")) || "?").toUpperCase();
  };
  function avatar(label, seed, glyph) {
    const h = hue(seed ?? label);
    return `<span class="chat-av" style="--av:${h}" aria-hidden="true">${esc(glyph || initials(label))}</span>`;
  }
  const hhmm = (t) => new Date(t * 1000).toTimeString().slice(0, 5);
  const dayKey = (t) => new Date(t * 1000).toDateString();
  function dayLabel(t) {
    const d = new Date(t * 1000), today = new Date();
    const y = new Date(); y.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return "Today";
    if (d.toDateString() === y.toDateString()) return "Yesterday";
    return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  }
  // A message of one to three emoji and nothing else shows big, as phones do.
  function emojiOnly(text) {
    const s = String(text || "").trim();
    if (!s || s.length > 32 || !/^[\p{Extended_Pictographic}\p{Emoji_Component}‍️\s]+$/u.test(s) || /^[\d#*\s]+$/.test(s)) return false;
    const seg = typeof Intl.Segmenter === "function" ? [...new Intl.Segmenter().segment(s.replace(/\s/g, ""))].length : [...s].length;
    return seg <= 3;
  }
  // The latest of a list of conversations' messages, for a rail preview.
  function preview(text, n = 60) { const s = String(text || "").replace(/\s+/g, " "); return s.length > n ? `${s.slice(0, n)}…` : s; }

  // ---------- the message list

  const atBottom = (el) => el.scrollHeight - el.scrollTop - el.clientHeight < 60;

  function jumpBtn(list) {
    let b = list.parentElement.querySelector(":scope > .chat-jump");
    if (!b) {
      b = document.createElement("button");
      b.type = "button";
      b.className = "chat-jump";
      b.title = "Jump to the latest";
      b.hidden = true;
      b.onclick = () => { list.scrollTo({ top: list.scrollHeight, behavior: "smooth" }); };
      list.parentElement.append(b);
      list.addEventListener("scroll", () => {
        // Only a reader's scroll moves the pin; a list with no height (its tab
        // hidden) can't tell where the reader is.
        if (list.clientHeight) list._stuck = atBottom(list);
        if (atBottom(list)) b.dataset.n = "";
        jumpLabel(list, b);
      }, { passive: true });
      // Drawn while its tab was hidden (no height), or the pane resized: a
      // list pinned to the newest stays there (live 2026-11-19: the Mesh tab
      // opened on the oldest message).
      // Hiding resets the scroll and showing fires a scroll event, so the pin
      // is saved as the list hides and used as it shows again.
      if (typeof ResizeObserver === "function") new ResizeObserver(() => {
        if (!list.clientHeight) {
          if (!list._hidden) { list._hidden = true; list._pinHidden = list._stuck !== false; }
          return;
        }
        const reopened = list._hidden;
        list._hidden = false;
        if (reopened ? list._pinHidden : list._stuck) {
          list._stuck = true;
          list.scrollTop = list.scrollHeight;
        }
        jumpLabel(list, b);
      }).observe(list);
    }
    return b;
  }

  function jumpLabel(list, b) {
    const n = Number(b.dataset.n || 0);
    b.hidden = atBottom(list);
    b.textContent = n ? `↓ ${n} new` : "↓ Latest";
    b.classList.toggle("has-new", n > 0);
  }

  function bubble(it, o, first) {
    const div = document.createElement("div");
    div.className = "mesh-msg chat-msg" + (it.mine ? " mine" : "") + (first ? " first" : "");
    div.dataset.key = it.key;
    const big = emojiOnly(it.text) && !it.quote;
    const quote = !it.quote ? "" : it.quote.missing
      ? `<span class="mesh-quote chat-quote missing">↪ a message no longer here</span>`
      : `<button type="button" class="mesh-quote chat-quote" data-jump="${esc(it.quote.key)}" title="Show the message this answers">` +
        `<b>${esc(it.quote.who)}</b><span>${esc(preview(it.quote.text, 90))}</span></button>`;
    const reacts = (it.reacts || []).map((r) =>
      `<button type="button" class="mesh-react${r.who.includes("me") ? " me" : ""}" data-react="${esc(r.emoji)}" title="${esc(r.who.join(", "))}"${it.canReact ? "" : " disabled"}>` +
      `${esc(r.emoji)}${r.who.length > 1 ? `<span>${r.who.length}</span>` : ""}</button>`).join("");
    const bar = it.canReply || it.canReact ? `<span class="mesh-acts chat-bar" role="toolbar" aria-label="Message actions">${it.canReact
      ? QUICK.map((e) => `<button type="button" data-react="${esc(e)}" title="React ${esc(e)}">${esc(e)}</button>`).join("") +
        `<button type="button" class="chat-more" data-act="react-more" title="More reactions">＋</button>` : ""}${it.canReply
      ? `<button type="button" class="chat-reply" data-act="reply" title="Reply (quotes this message)">↩ Reply</button>` : ""}</span>` : "";
    const foot = `<span class="chat-foot">${it.sub ? `<span class="chat-hops">${esc(it.sub)}</span>` : ""}` +
      `<time datetime="${new Date(it.t * 1000).toISOString()}" title="${esc(new Date(it.t * 1000).toLocaleString())}">${hhmm(it.t)}</time>` +
      (it.ack ? `<span class="mesh-ack ${esc(it.ack.cls || "")}" title="${esc(it.ack.why || "")}">${esc(it.ack.glyph || "")}</span>` : "") + "</span>";
    div.innerHTML = `<div class="chat-bubble${big ? " big" : ""}" tabindex="0">${quote}` +
      `<span class="mesh-text"><span class="mesh-body">${esc(it.text)}</span></span>${foot}</div>` +
      `${bar}<span class="mesh-reacts">${reacts}</span>` +
      (it.extra ? `<span class="mesh-meta chat-extra">${it.extra}</span>` : "");
    // The bar sits beside the bubble; a wide bubble has no room there, so it
    // goes over the bubble's top edge instead.
    const place = () => {
      const bar = div.querySelector(":scope > .chat-bar"), list = div.closest(".chat-list");
      if (!bar || !list) return;
      const r = div.getBoundingClientRect(), l = list.getBoundingClientRect(), w = bar.offsetWidth + 12;
      div.classList.toggle("bar-top", it.mine ? r.left - w < l.left : r.right + w > l.right);
    };
    div.addEventListener("mouseenter", place);
    div.addEventListener("focusin", place);
    div.onclick = (e) => {
      place();
      const btn = e.target.closest("button");
      if (!btn) {          // a tap on the bubble: the action bar, for touch screens
        if (e.target.closest(".chat-bubble") && !window.getSelection()?.toString()) {
          for (const x of div.parentElement.closest(".chat-list")?.querySelectorAll(".chat-msg.act") || []) if (x !== div) x.classList.remove("act");
          div.classList.toggle("act");
        }
        return;
      }
      e.stopPropagation();
      if (btn.dataset.jump) return jumpTo(div.closest(".chat-list"), btn.dataset.jump);
      if (btn.dataset.react) { div.classList.remove("act"); return o.onReact?.(it, btn.dataset.react); }
      if (btn.dataset.act === "reply") { div.classList.remove("act"); return o.onReply?.(it); }
      if (btn.dataset.act === "react-more") return picker(btn, (em) => { div.classList.remove("act"); o.onReact?.(it, em); });
      if (btn.dataset.act) return o.onAct?.(it, btn.dataset.act, btn);
    };
    return div;
  }

  function jumpTo(list, key) {
    const el = list?.querySelector(`.chat-msg[data-key="${CSS.escape(key)}"]`);
    if (!el) return;
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
  }

  // Draw items into list (a scrolling element inside a position:relative
  // parent). Keeps the reader's place unless they were at the bottom.
  // o: {conv: which conversation, empty, unread: n of the newest incoming to
  //     mark new, onWho, onReply, onReact, onAct}
  function render(list, items, o = {}) {
    list.classList.add("chat-list");
    // A different conversation (o.conv) starts at its newest message.
    const conv = String(o.conv ?? ""), switched = list.dataset.conv !== conv;
    list.dataset.conv = conv;
    if (switched) list.dataset.count = "0";
    // Hidden (no height), where the reader is can't be measured: keep the
    // pin it had when it hid; a different conversation starts at the bottom.
    const hidden = !list.clientHeight;
    const stick = switched || (hidden ? list._pinHidden !== false : atBottom(list) || !list.childElementCount);
    if (hidden) { list._hidden = true; list._pinHidden = stick; }
    const prevTop = list.scrollTop;
    const before = Number(list.dataset.count || 0);
    list.innerHTML = "";
    if (!items.length) { list.innerHTML = o.empty ?? `<div class="wl-empty chat-empty">No messages yet</div>`; list.dataset.count = "0"; return; }
    // Where the "new" line goes: before the oldest of the last n incoming.
    let newAt = -1;
    if (o.unread > 0) {
      let n = o.unread;
      for (let i = items.length - 1; i >= 0 && n > 0; i--) if (!items[i].mine) { newAt = i; n--; }
    }
    let group = null, last = null;
    items.forEach((it, i) => {
      if (!last || dayKey(last.t) !== dayKey(it.t)) {
        const d = document.createElement("div");
        d.className = "chat-day";
        d.innerHTML = `<span>${esc(dayLabel(it.t))}</span>`;
        list.append(d);
        group = null;
      }
      if (i === newAt) {
        const d = document.createElement("div");
        d.className = "chat-new";
        d.innerHTML = "<span>New</span>";
        list.append(d);
        group = null;
      }
      const same = group && last && last.mine === it.mine && last.who === it.who && it.t - last.t < GROUP_S;
      if (!same) {
        group = document.createElement("div");
        group.className = "chat-group" + (it.mine ? " mine" : "");
        group.style.setProperty("--who", nameColor(it.seed ?? it.who));
        const stack = document.createElement("div");
        stack.className = "chat-stack";
        if (!it.mine) {
          group.insertAdjacentHTML("afterbegin", avatar(it.who, it.seed));
          stack.innerHTML = `<div class="chat-name"><button type="button" class="mesh-who" title="${esc(it.whoTitle || "")}"${it.peer ? "" : " disabled"}>${esc(it.who)}</button>${it.tag && it.tag !== it.who ? `<span class="chat-tag">${esc(it.tag)}</span>` : ""}</div>`;
          if (it.peer) stack.querySelector(".mesh-who").onclick = () => o.onWho?.(it);
        } else if (it.who && it.who !== "me") {
          stack.innerHTML = `<div class="chat-name">${esc(it.who)}</div>`;
        }
        group.append(stack);
        list.append(group);
      }
      group.lastElementChild.append(bubble(it, o, !same));
      last = it;
    });
    list.dataset.count = String(items.length);
    const jb = jumpBtn(list);
    list._stuck = stick;
    if (stick) {
      list.scrollTop = list.scrollHeight;
      jb.dataset.n = "";
      jb.hidden = true;
    } else {
      list.scrollTop = prevTop;                 // the reader is further up: leave them there
      const added = items.length - before;
      if (added > 0) jb.dataset.n = String(Number(jb.dataset.n || 0) + added);
      jumpLabel(list, jb);
    }
    // Opening a conversation with unread messages: start at the "New" line.
    if (switched && newAt >= 0) { list.querySelector(".chat-new")?.scrollIntoView({ block: "start" }); jumpLabel(list, jb); }
  }

  // ---------- the emoji picker

  let pickerEl = null;
  function closePicker() { pickerEl?.remove(); pickerEl = null; }
  document.addEventListener("click", (e) => { if (pickerEl && !pickerEl.contains(e.target)) closePicker(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && pickerEl) { e.stopPropagation(); closePicker(); } }, true);

  function recent() { try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; } catch { return []; } }
  function used(e) {
    const r = [e, ...recent().filter((x) => x !== e)].slice(0, 24);
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(r)); } catch { /* fine */ }
  }

  // A grid of emoji under (or over) anchor; onPick(emoji). Clicking the
  // anchor again closes it.
  function picker(anchor, onPick) {
    if (pickerEl && pickerEl._anchor === anchor) { closePicker(); return; }
    closePicker();
    const el = document.createElement("div");
    el.className = "chat-picker";
    el._anchor = anchor;
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-label", "Emoji");
    const cats = EMOJI.map(([n, list]) => [n, n === "Recent" ? recent() : list]).filter(([, l]) => l.length);
    let cat = cats[0][0];
    const draw = () => {
      const list = cats.find(([n]) => n === cat)[1];
      el.innerHTML = `<div class="chat-picker-tabs">${cats.map(([n, l]) =>
        `<button type="button" data-cat="${esc(n)}" class="${n === cat ? "on" : ""}" title="${esc(n)}">${esc(n === "Recent" ? "🕘" : l[0])}</button>`).join("")}</div>` +
        `<div class="chat-picker-name">${esc(cat)}</div>` +
        `<div class="chat-picker-grid">${list.map((e) => `<button type="button" data-emoji="${esc(e)}" title="${esc(e)}">${esc(e)}</button>`).join("")}</div>`;
    };
    draw();
    el.onclick = (e) => {
      e.stopPropagation();
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.cat) { cat = b.dataset.cat; draw(); return; }
      if (b.dataset.emoji) { used(b.dataset.emoji); closePicker(); onPick(b.dataset.emoji); }
    };
    document.body.append(el);
    const r = anchor.getBoundingClientRect(), w = el.offsetWidth, h = el.offsetHeight;
    const left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left));
    const top = r.top - h - 8 > 8 ? r.top - h - 8 : Math.min(window.innerHeight - h - 8, r.bottom + 8);
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
    pickerEl = el;
  }

  // ---------- the composer

  // Wire a form holding a <textarea> (text), a submit button and an emoji
  // button (emoji): the box grows to six lines; Enter sends, Shift+Enter is
  // a new line; Escape calls onEscape (cancel a reply). onInput after every
  // change, so the tab can count bytes.
  function composer({ form, text, emoji, onInput, onEscape }) {
    const grow = () => { text.style.height = "auto"; text.style.height = `${Math.min(text.scrollHeight, 150)}px`; };
    text.addEventListener("input", () => { grow(); onInput?.(); });
    text.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
      else if (e.key === "Escape") onEscape?.();
    });
    if (emoji) {
      emoji.onclick = (e) => {
        e.preventDefault(); e.stopPropagation();
        picker(emoji, (em) => {
          const s = text.selectionStart ?? text.value.length, end = text.selectionEnd ?? s;
          text.value = text.value.slice(0, s) + em + text.value.slice(end);
          text.focus();
          text.selectionStart = text.selectionEnd = s + em.length;
          grow(); onInput?.();
        });
      };
    }
    return { grow, clear() { text.value = ""; grow(); onInput?.(); } };
  }

  // ---------- the conversation rail

  // rows: [{id, title, glyph, seed, last: {who, text, t} | null, unread, on, hint}]
  // grouped under headings: [[heading, rows], ...]; onPick(id).
  function rail(el, sections, onPick) {
    el.classList.add("chat-rail");
    el.innerHTML = sections.map(([head, rows, emptyHint]) =>
      `<div class="chat-rail-head">${esc(head)}</div>` + (rows.length ? rows.map((r) =>
        `<button type="button" class="chat-conv${r.on ? " on" : ""}${r.unread ? " unread" : ""}" data-conv="${esc(String(r.id))}" title="${esc(r.hint || r.title)}">` +
        `${avatar(r.title, r.seed, r.glyph)}<span class="chat-conv-main"><span class="chat-conv-top"><b>${esc(r.title)}</b>` +
        `<time>${r.last ? esc(agoShort(r.last.t)) : ""}</time></span>` +
        `<span class="chat-conv-last">${r.last ? esc((r.last.who ? `${r.last.who}: ` : "") + preview(r.last.text, 48)) : `<i>${esc(r.empty || "No messages")}</i>`}</span></span>` +
        (r.unread ? `<span class="mesh-unread">${r.unread}</span>` : "") + "</button>").join("")
        : `<div class="chat-rail-hint">${esc(emptyHint || "")}</div>`)).join("");
    for (const b of el.querySelectorAll("[data-conv]")) b.onclick = () => onPick(b.dataset.conv);
  }
  function agoShort(t) {
    const s = Math.max(0, Date.now() / 1000 - t);
    if (s < 60) return "now";
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return hhmm(t);
    return new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  }

  return { render, composer, rail, picker, avatar, nameColor, emojiOnly, preview, QUICK };
})();
