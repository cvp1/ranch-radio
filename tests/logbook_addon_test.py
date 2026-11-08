"""The Logbook add-on: where the logbook is, from the host file.

  - a url (http or https, with a host) is passed through as given
  - a port (1-65535, optional scheme and path) means "on the page's machine"
  - anything else is refused, naming the field: no url or port, both, a bad
    scheme, a port out of range or not a number, a path without a leading /
  - in the registry: a good block loads in the Tools group with its css, its
    snapshot carries the target; a bad one shows "not loaded" with the reason
    and the rest of the server runs on
  - it's in the module catalog and manifest, and the manifest's url rule
    takes an address and refuses anything that isn't http(s)

    python3 tests/logbook_addon_test.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from addons import Addons  # noqa: E402
from addons import logbook as L  # noqa: E402
from ranch_radio import catalog, manifest  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def refused(opts):
    try:
        L.resolve(opts)
    except ValueError as e:
        return str(e)
    return None


# ---------------------------------------------------------------- resolve
check("a url passes through", L.resolve({"url": "https://192.168.1.20:8087/"}), {"url": "https://192.168.1.20:8087/"})
check("  plain http too", L.resolve({"url": "http://logbook.example/index.php"}), {"url": "http://logbook.example/index.php"})
check("a port: https on the page's machine, at /", L.resolve({"port": 8087}),
      {"port": 8087, "scheme": "https", "path": "/"})
check("  with its scheme and path", L.resolve({"port": 8086, "scheme": "http", "path": "/index.php/dashboard"}),
      {"port": 8086, "scheme": "http", "path": "/index.php/dashboard"})
check("nothing given: refused, saying what it needs", "url" in (refused({}) or "") and "port" in (refused({}) or ""), True)
check("both given: refused", refused({"url": "https://a.example/", "port": 1}), "give the logbook's url or its port, not both")
check("a url that isn't http(s): refused",
      [bool(refused({"url": u})) for u in ("javascript:alert(1)", "ftp://a.example/", "https://", "a.example")],
      [True, True, True, True])
check("a port out of range or not a number: refused",
      [bool(refused({"port": p})) for p in (0, 65536, "8087", True, 80.5)], [True, True, True, True, True])
check("a bad scheme or path: refused",
      [bool(refused({"port": 80, "scheme": "ftp"})), bool(refused({"port": 80, "path": "dashboard"}))], [True, True])


# ---------------------------------------------------------------- registry
class Host:
    def notice(self, *a):
        pass

    def _broadcast(self, payload):
        pass

    async def _send(self, ws, payload):
        pass


reg = Addons(Host(), {"logbook": {"enabled": True, "port": 8087}}, state_path=None)
d = {x["name"]: x for x in reg.describe()}["logbook"]
check("loads, in the Tools group, with its css", (d["label"], d["group"], d["css"], d["on"], d["error"]),
      ("Logbook", "tools", True, True, None))
check("its snapshot is the target", reg.snapshot()["logbook"],
      {"on": True, "error": None, "port": 8087, "scheme": "https", "path": "/"})
bad = Addons(Host(), {"logbook": {"enabled": True, "port": 99999}}, state_path=None)
e = {x["name"]: x for x in bad.describe()}["logbook"]
check("a bad block: not loaded, with the reason, and off", (e["on"], e["error"].startswith("not loaded: ValueError: port")),
      (False, True))

# ---------------------------------------------------------------- catalog, manifest, page script
check("in the catalog as an add-on, both OSes", (catalog.by_name()["logbook"]["kind"], catalog.by_name()["logbook"]["platforms"]),
      ("addon", ["macos", "linux"]))
check("the manifest asks for its url", (manifest.MANIFESTS["logbook"]["block"], manifest.template("logbook", "macos")),
      (("addons", "logbook"), {"enabled": True, "url": manifest.ASK}))
check("the url rule takes an address", manifest.check_ask("http_url", "https://192.168.1.20:8087/"), None)
check("  and refuses what isn't http(s)",
      [manifest.check_ask("http_url", v) is None for v in ("javascript:alert(1)", "https://a b/", "file:///etc/passwd")],
      [False, False, False])
js = (ROOT / "static/addons/logbook.js").read_text()
check("the page script registers as logbook", 'addons.register("logbook"' in js, True)
check("  and its css exists", (ROOT / "static/addons/logbook.css").exists(), True)

print(f"\n{'all ok' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
