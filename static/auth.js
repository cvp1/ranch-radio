// Operator / read-only (webguard.AuthPolicy). Under auth readonly-lan a page
// without the operator's session can watch and press STOP TX; this chip says
// so and unlocks it with the token (a signed cookie, 12 h). In "open" mode
// (the default) it shows nothing. A radio's page inside the station's frame
// shows no chip of its own: the station's covers it.
(() => {
  if (window.top !== window) return;
  const chip = (text, title, onclick) => {
    const b = document.createElement("button");
    b.type = "button";
    b.id = "rr-auth";
    b.className = "chip";
    b.textContent = text;
    b.title = title;
    b.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:9999";
    b.onclick = onclick;
    document.body.append(b);
    return b;
  };
  const post = (path, body) => fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: { "content-type": "application/json", "X-Ranch-Radio": "1" }, body: JSON.stringify(body || {}),
  });
  fetch("auth/whoami", { credentials: "same-origin", cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((w) => {
      if (!w || w.mode === "open") return;
      if (w.role === "readonly") {
        chip("Read-only · Unlock", "This page can watch and press STOP TX. Unlock to operate.", async () => {
          const token = window.prompt("Operator token:");
          if (!token) return;
          const r = await post("auth/login", { token });
          if (r.ok) location.reload();
          else window.alert("Wrong token: still read-only.");
        });
      } else {
        chip("Operator · Lock", "Lock this browser back to read-only.", async () => {
          await post("auth/logout");
          location.reload();
        });
      }
    })
    .catch(() => {});
})();
