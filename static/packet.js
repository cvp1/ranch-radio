// The packet page's script, out of packet.html so the page runs under a
// script-src 'self' policy (webguard.CSP). Its own $ (by id) and esc stay:
// common.js's $ is a selector, and this page does not load it.
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ago = t => { if (!t) return "never"; const s = Math.round(Date.now()/1000 - t);
  return s < 60 ? s + "s ago" : s < 3600 ? Math.round(s/60) + "m ago" : Math.round(s/3600) + "h ago"; };
const clock = t => new Date(t*1000).toLocaleTimeString([], {hour12:false});
function pill(el, cls, text) { el.className = "pill " + cls; el.querySelector("span").textContent = text; }
function rows(el, head, list, fn, empty) {
  el.innerHTML = list.length ? "<tr>" + head.map(h => `<th>${h}</th>`).join("") + "</tr>" + list.map(fn).join("")
                             : `<tr><td class="empty">${empty}</td></tr>`;
}
let txEnabled = false, txAllowed = false;
$("txbtn").onclick = async () => {
  const want = !txEnabled;
  if (want && !confirm("Allow the hub to transmit under your callsign?")) return;
  const r = await fetch("api/tx", {method: "POST", headers: {"content-type": "application/json", "X-Ranch-Radio": "1"},
                                   body: JSON.stringify({enabled: want})});
  if (!r.ok) alert((await r.json()).error || "refused");
  refresh();
};
async function refresh() {
  let s;
  try { s = await (await fetch("api/status")).json(); }
  catch (e) { pill($("radio"), "bad", "hub not answering"); return; }
  const r = s.radio;
  if (s.title && $("hubtitle").textContent !== s.title) { $("hubtitle").textContent = s.title; document.title = s.title; }
  if (r.lent) pill($("radio"), "warn", `radio lent to ${r.lent.to} · back by ${new Date(r.lent.until * 1000).toLocaleTimeString()}`);
  else pill($("radio"), r.up ? "ok" : "bad",
       r.up ? `radio linked · last packet ${ago(r.last_rx)}` : `radio not linked${r.error ? " · " + r.error : ""}`);
  txEnabled = s.tx.enabled; txAllowed = s.tx.allowed_by_start;
  pill($("tx"), txEnabled ? "bad" : s.tx.notice ? "warn" : "ok",
       txEnabled ? `transmit ON as ${s.tx.allow_calls.join("/")}` + ((s.tx.holds || []).length ? " · APRS held" : "")
                 : "receive only" + (s.tx.notice ? ` · ${s.tx.notice}` : ""));
  const b = $("txbtn");
  b.hidden = !txEnabled && !txAllowed;
  b.textContent = txEnabled ? "Stop transmitting" : "Allow transmit";
  b.className = txEnabled ? "on" : "";
  const agw = s.clients.agw.map(c => c.calls.length ? c.calls.join(",") : "AGW").join(" · ");
  $("clients").textContent = `AGWPE ${s.clients.agw.length}${agw ? " (" + agw + ")" : ""} · KISS ${s.clients.kiss.length}`;
  const c = s.counts;
  $("counts").textContent = `rx ${c.rx_frames||0} · tx ${c.tx_frames||0}` + (c.tx_refused ? ` · refused ${c.tx_refused}` : "");
  $("ports").textContent = `Apps on this Mac: AGWPE ${s.ports.bind}:${s.ports.agw} (Pat, YAAC) · KISS TCP ${s.ports.bind}:${s.ports.kiss}`;
  $("msgs-sec").hidden = !s.messages.length;
  rows($("msgs"), ["time", "from", "to", "message"], s.messages,
       m => `<tr><td>${clock(m.t)}</td><td>${esc(m.from)}</td><td>${esc(m.to)}</td><td>${esc(m.text)}</td></tr>`, "");
  rows($("heard"), ["station", "last", "#", "path", "last packet"], s.heard,
       h => `<tr><td>${esc(h.station)}</td><td>${ago(h.last)}</td><td>${h.count}</td>
             <td>${h.direct ? "direct" : "digi"}</td><td class="mono">${esc(h.text.split(":").slice(1).join(":").slice(0,80))}</td></tr>`,
       "nothing heard yet");
  rows($("links"), ["local", "remote", "state", "in/out bytes", "resent"], s.links,
       l => `<tr><td>${esc(l.local)}</td><td>${esc(l.remote)}</td><td>${l.state}</td>
             <td>${l.bytes_in}/${l.bytes_out}</td><td>${l.i_resent}</td></tr>`, "no links");
  rows($("recent"), ["time", "", "frame"], s.recent,
       p => `<tr class="${p.dir === "tx" ? "tx-row" : ""}"><td>${clock(p.t)}</td><td>${p.dir.toUpperCase()}</td>
             <td class="mono">${esc(p.text)}</td></tr>`, "no traffic yet");
}
refresh(); setInterval(refresh, 2000);
