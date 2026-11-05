"""tools/meshcore_wifi.py against a fake rescue CLI: no node, no network.

  - waits for the rescue banner, then sends ssid, password, enabled, reboot
  - the password reaches the node but never the screen (it is echoed back
    by the CLI, as on the real firmware)
  - settings the node didn't keep stop it before the reboot
  - no rescue banner: it says how to retry and sends nothing

    python3 tests/meshcore_wifi_test.py
"""
import contextlib
import importlib.util
import io
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PWD = "s3cret-Pa55word"
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:58} got {got!r}" + ("" if ok else f" want {want!r}"))


class FakeRescue:
    """The firmware's rescue CLI: a banner after reset, echo, '  <reply>\\r\\n'."""
    def __init__(self, *a, banner=True, keep=True, **k):
        self.banner, self.keep = banner, keep
        self.out, self.lines, self.prefs = b"", [], {}
        self.dtr = self.rts = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def __setattr__(self, k, v):
        object.__setattr__(self, k, v)
        if k == "rts" and v is False and getattr(self, "banner", False):
            self.out += b"boot...\r\n========= CLI Rescue =========\r\n"

    def read(self, n):
        d, self.out = self.out[:n], self.out[n:]
        return d

    def reset_input_buffer(self):
        self.out = b""

    def write(self, data):
        line = data.decode().rstrip("\r")
        self.lines.append(line)
        reply = "OK"
        if line.startswith("set wifi.ssid "):
            if self.keep:
                self.prefs["ssid"] = line[14:]
            reply = f"> wifi.ssid is now {line[14:]}"
        elif line.startswith("set wifi.pwd "):
            self.prefs["pwd"] = line[13:]
            reply = "> wifi.pwd updated (reboot to apply)"
        elif line == "set wifi.enabled 1":
            self.prefs["on"] = "1"
            reply = "> wifi.enabled is now 1"
        elif line == "get wifi.ssid":
            reply = "> " + self.prefs.get("ssid", "(not set)")
        elif line == "get wifi.enabled":
            reply = "> " + self.prefs.get("on", "0")
        self.out += data + f"  {reply}\r\n".encode()     # echo, then the reply


def run(**fake):
    spec = importlib.util.spec_from_file_location("meshcore_wifi", os.path.join(ROOT, "tools", "meshcore_wifi.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    made = []

    def Serial(*a, **k):
        made.append(FakeRescue(*a, **fake, **k))
        return made[-1]

    sys.modules["serial"] = types.SimpleNamespace(Serial=Serial)
    mod.getpass = types.SimpleNamespace(getpass=lambda prompt="": PWD)
    mod.lan = lambda: None                       # no LAN scan in a test
    mod.RESCUE_WAIT_S = 1.0
    sys.argv = ["meshcore_wifi.py", "/dev/fake", "--ssid", "HomeNet"]
    out = io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = mod.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), made[0] if made else None


code, out, node = run()
check("sets the network, then the password, then enables Wi-Fi",
      [l.split(" ")[1] for l in node.lines[:3]], ["wifi.ssid", "wifi.pwd", "wifi.enabled"])
check("the node got the password", node.prefs.get("pwd"), PWD)
check("the password never reaches the screen", PWD in out, False)
check("reads back and reboots", node.lines[-1], "reboot")
check("exits 0", code, 0)

code, out, node = run(keep=False)
check("settings not kept: stops before the reboot", "reboot" in node.lines, False)
check("  and says so", "didn't keep" in str(code), True)
check("  without showing the password", PWD in out + str(code), False)

code, out, node = run(banner=False)
check("no rescue mode: nothing is sent", node.lines, [])
check("  and it says how to retry", "hold the button" in str(code), True)

print("PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
