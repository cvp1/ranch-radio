"""The HaLow add-on (addons/halow.py, halow_ap.py) against a fake bridge
(tests/fake_halow_ssh.py, standing in for ssh):

  - a good block loads, on, in the tools group; bad blocks don't load, with the field named
  - the read is parsed: host health, radios (the HaLow one marked), networks,
    stations with signal/rates and their address from the neighbour table
  - the HaLow network's channel is the radio's S1G one, not the driver's 5 GHz stand-in;
    a bridge clock months off shows as its skew
  - Wi-Fi keys never reach the snapshot (the box filters them; so does the parser)
  - the DHCP guard: all pools ignored reads clean; a serving pool shows, notices once (edge), and plans the fix
  - stations joining and leaving are noticed, once each
  - a change is a plan first: nothing runs on the bridge until apply, with that plan's id
  - apply runs exactly the planned bytes, once; a second apply of the same id is refused
  - a plan expires: apply after PLAN_TTL is refused, nothing runs
  - bad values are refused before anything is built: injection in an SSID, an unknown
    section or option, a bad MAC, an option on the wrong section type, a bad ping address
  - an SSID with spaces and a quote is shell-quoted, not split
  - HaLow-radio changes carry the cameras-drop warning; a set offers its undo
  - every applied change is in halow-actions.log
  - an unreachable bridge: the error shows, the last reading is kept and marked stale,
    one notice, and one more when it answers again
  - it is in the module catalog and the manifest

    python3 tests/halow_addon_test.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import halow_ap  # noqa: E402
from addons import Addons  # noqa: E402
from addons import halow as halow_mod  # noqa: E402
from ranch_radio import catalog, manifest  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


READ = r"""@@uptime
93784.21 370012.55
0.08 0.12 0.10 1/97 4412
@@date
1781629508
@@mem
MemTotal:         245432 kB
MemFree:           61220 kB
MemAvailable:     118004 kB
@@df
Filesystem           1K-blocks      Used Available Use% Mounted on
overlay                   9216      4300      4916  47% /overlay
tmpfs                   122716      2088    120628   2% /tmp
@@board
{
	"kernel": "5.15.167",
	"hostname": "HT-H7608",
	"system": "ARMv8 Processor rev 4",
	"model": "Heltec HT-H7608",
	"release": {
		"distribution": "OpenWrt",
		"version": "23.05.5",
		"description": "OpenWrt 23.05.5 r24106"
	}
}
@@wireless
wireless.radio0=wifi-device
wireless.radio0.type='mac80211'
wireless.radio0.channel='6'
wireless.radio0.htmode='HT20'
wireless.radio0.country='US'
wireless.radio1=wifi-device
wireless.radio1.type='morse'
wireless.radio1.hwmode='11ah'
wireless.radio1.channel='27'
wireless.radio1.country='US'
wireless.default_radio0=wifi-iface
wireless.default_radio0.device='radio0'
wireless.default_radio0.network='ahwlan'
wireless.default_radio0.mode='ap'
wireless.default_radio0.ssid='Ranch-wifi'
wireless.default_radio0.encryption='psk2'
wireless.default_radio0.key='leaked-if-the-box-filter-failed'
wireless.default_radio1=wifi-iface
wireless.default_radio1.device='radio1'
wireless.default_radio1.network='ahwlan'
wireless.default_radio1.mode='ap'
wireless.default_radio1.ssid='Ranch'
wireless.default_radio1.encryption='owe'
@@dhcp
dhcp.lan=dhcp
dhcp.lan.interface='lan'
dhcp.lan.ignore='1'
dhcp.ahwlan=dhcp
dhcp.ahwlan.interface='ahwlan'
dhcp.ahwlan.ignore='1'
@@iwdev
phy#1
	Interface wlan0
		ifindex 9
		addr 02:00:00:00:01:01
		ssid Ranch
		type AP
		channel 44 (5220 MHz), width: 80 MHz, center1: 5210 MHz
		txpower 21.00 dBm
phy#0
	Interface phy0-ap0
		ifindex 8
		addr 02:00:00:00:00:01
		ssid Ranch-wifi
		type AP
		channel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz
		txpower 20.00 dBm
@@sta wlan0
Station 02:00:00:00:aa:01 (on wlan0)
	inactive time:	120 ms
	rx bytes:	88123456
	tx bytes:	1234567
	tx retries:	41
	tx failed:	2
	signal:  	-71 dBm
	signal avg:	-70 dBm
	tx bitrate:	6.5 MBit/s MCS 3
	rx bitrate:	4.3 MBit/s MCS 2
	connected time:	86012 seconds
@@sta phy0-ap0
@@neigh
192.168.1.1 dev br-ahwlan lladdr 02:00:00:00:00:fe REACHABLE
192.168.1.120 dev br-ahwlan lladdr 02:00:00:00:aa:01 STALE
@@leases
@@svc
mediamtx 1 1
aws_kvs 0 0
dnsmasq 1 1
@@log
Wed Nov 18 09:00:01 2026 daemon.info hostapd: wlan0: STA 02:00:00:00:aa:01 IEEE 802.11: associated
"""


class Host:
    def __init__(self):
        self.notices, self.sent = [], []

    def notice(self, msg, level="info", ws=None):
        self.notices.append((level, msg))

    def _broadcast(self, payload):
        self.sent.append(json.loads(payload))

    async def _send(self, ws, payload):
        self.sent.append(json.loads(payload))


tmp = Path(tempfile.mkdtemp(prefix="halow-test-"))
os.environ["FAKE_HALOW_DIR"] = str(tmp)
(tmp / "read.txt").write_text(READ)
fake = str(ROOT / "tests" / "fake_halow_ssh.py")
os.chmod(fake, 0o755)
BLOCK = {"enabled": True, "host": "192.168.1.65", "key": str(tmp / "key"), "ssh": fake, "poll": 3600}


def cmds():
    p = tmp / "cmds.log"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# --- loading
host = Host()
reg = Addons(host, {"halow": BLOCK}, state_path=str(tmp / "state" / "addons.json"))
d = {x["name"]: x for x in reg.describe()}["halow"]
check("loads, on, in its group, no error", (d["label"], d["group"], d["on"], d["error"]), ("HaLow", "tools", True, None))
for bad, field in (({"host": "a b"}, "'host'"), ({"host": "h", "poll": 1}, "'poll'"), ({"host": "h", "user": "r;oot"}, "'user'")):
    r = Addons(Host(), {"halow": bad}, state_path=None)
    err = r.items["halow"]["error"] or ""
    check(f"a bad block doesn't load, naming {field}", (err.startswith("not loaded"), field in err), (True, True))

a = reg.get("halow")
a.state_dir = str(tmp / "state")


async def main():
    # --- reading
    await a.read()
    s = a.snapshot()
    check("read ok, no error", (s["error"], s["at"] is not None, s["stale"]), (None, True, False))
    check("host health", (s["host"]["model"], s["host"]["release"], s["host"]["uptime_s"], s["host"]["disk_kb"]["/overlay"]["free"],
                          s["host"]["mem_kb"]["available"]), ("Heltec HT-H7608", "OpenWrt 23.05.5 r24106", 93784, 4916, 118004))
    check("radios, the HaLow one marked", [(r["section"], r["halow"], r["channel"]) for r in s["radios"]],
          [("radio0", False, "6"), ("radio1", True, "27")])
    check("networks matched to live interfaces", sorted((i["ssid"], i["ifname"], i["channel"]) for i in s["ifaces"]),
          [("Ranch", "wlan0", 27), ("Ranch-wifi", "phy0-ap0", 6)])
    check("the HaLow network shows its S1G channel, not the driver's 5 GHz stand-in",
          [(i["channel"], i["freq"]) for i in s["ifaces"] if i["ssid"] == "Ranch"], [(27, None)])
    check("a clock months behind shows as a negative skew", s["host"]["clock_skew_s"] < -86400 * 30, True)
    check("the log filter drops the checkstatus noise on the box", "checkstatus" in halow_ap.READ_SCRIPT and "grep -v -E" in halow_ap.READ_SCRIPT.split("@@log")[1], True)
    st = s["stations"]
    check("one station, its signal, rates, retries, joined time, address",
          [(x["mac"], x["signal"], x["signal_avg"], x["tx_rate"], x["tx_retries"], x["connected_s"], x["ip"]) for x in st],
          [("02:00:00:00:aa:01", -71, -70, 6.5, 41, 86012, "192.168.1.120")])
    check("no Wi-Fi key in the snapshot", "leaked-if-the-box-filter-failed" in json.dumps(s), False)
    check("the box-side filter drops keys too", "grep -v -E" in halow_ap.READ_SCRIPT and "sae_password" in halow_ap.READ_SCRIPT, True)
    check("DHCP guard clean", s["dhcp"]["serving"], [])
    check("services", [(v["name"], v["running"]) for v in s["services"]], [("mediamtx", True), ("aws_kvs", False), ("dnsmasq", True)])
    check("no notices on a clean first read", host.notices, [])

    # --- DHCP guard trips (edge) and stations change
    (tmp / "read.txt").write_text(READ.replace("dhcp.ahwlan.ignore='1'\n", "")
                                  .replace("Station 02:00:00:00:aa:01", "Station 02:00:00:00:aa:02")
                                  .replace("lladdr 02:00:00:00:aa:01", "lladdr 02:00:00:00:aa:02"))
    await a.read()
    await a.read()
    dhcp_n = [m for _, m in host.notices if "serving DHCP" in m]
    check("a serving pool shows", a.snapshot()["dhcp"]["serving"], ["ahwlan"])
    check("and is noticed once, not on every read", len(dhcp_n), 1)
    check("station left and joined, noticed once each",
          sorted(m for _, m in host.notices if "the bridge." in m),
          ["HaLow: 02:00:00:00:aa:01 left the bridge.", "HaLow: 02:00:00:00:aa:02 joined the bridge."])

    # --- plan, then apply
    before = len(cmds())
    host.sent.clear()
    await a.command(None, "plan", {"what": "dhcp_off"})
    p = next(m for m in host.sent if m.get("event") == "plan")
    check("a plan carries the exact command", p["cmd"], "uci set dhcp.ahwlan.ignore=1 && uci commit dhcp && /etc/init.d/dnsmasq restart")
    check("planning runs nothing on the bridge", len(cmds()), before)
    await a.command(None, "apply", {"id": p["id"]})
    ran = cmds()[before:]
    check("apply runs exactly the planned bytes", ran[0], p["cmd"])
    n = len(cmds())
    await a.command(None, "apply", {"id": p["id"]})
    check("the same plan can't run twice", (len(cmds()) - n, "expired or was already made" in host.notices[-1][1]), (0, True))
    check("no write in the refused apply", [c for c in cmds()[n:] if c.startswith("uci")], [])

    # --- expiry
    await a.command(None, "plan", {"what": "reboot"})
    p = [m for m in host.sent if m.get("event") == "plan"][-1]
    a.plans[p["id"]]["made"] -= halow_mod.PLAN_TTL + 1
    n = len(cmds())
    await a.command(None, "apply", {"id": p["id"]})
    check("an expired plan runs nothing", ("reboot" in cmds()[n:], "expired" in host.notices[-1][1]), (False, True))

    # --- validation
    cases = [
        ("ssid with a newline", {"what": "set", "section": "default_radio1", "option": "ssid", "value": "x\nreboot"}),
        ("unknown section", {"what": "set", "section": "radio9", "option": "channel", "value": "1"}),
        ("section with shell", {"what": "set", "section": "radio1;reboot", "option": "channel", "value": "1"}),
        ("option not allowed", {"what": "set", "section": "radio1", "option": "key", "value": "x"}),
        ("option on the wrong type", {"what": "set", "section": "radio1", "option": "ssid", "value": "x"}),
        ("channel not a number", {"what": "set", "section": "radio1", "option": "channel", "value": "27;reboot"}),
        ("bad MAC", {"what": "kick", "ifname": "wlan0", "mac": "02:00:00:00:aa:0$(reboot)"}),
        ("unknown interface", {"what": "kick", "ifname": "eth0", "mac": "02:00:00:00:aa:02"}),
        ("unknown service", {"what": "service", "service": "dropbear", "action": "stop"}),
        ("unknown change", {"what": "rm"}),
    ]
    for name, m in cases:
        host.sent.clear()
        host.notices.clear()
        await a.command(None, "plan", m)
        check(f"refused: {name}", ([x for x in host.sent if x.get("event") == "plan"], host.notices[-1][0] if host.notices else None),
              ([], "warn"))
    n = len(cmds())
    await a.command(None, "ping", {"ip": "192.168.1.120; reboot"})
    check("refused: bad ping address, nothing run", len(cmds()), n)

    # --- quoting, warnings, undo
    host.sent.clear()
    await a.command(None, "plan", {"what": "set", "section": "default_radio1", "option": "ssid", "value": "Gate cam's net"})
    p = [m for m in host.sent if m.get("event") == "plan"][-1]
    check("an SSID with a space and quote is quoted as one word",
          p["cmd"], "uci set 'wireless.default_radio1.ssid=Gate cam'\"'\"'s net' && uci commit wireless && wifi reload")
    check("HaLow change warns the cameras drop and won't follow a rename",
          ("every camera on it drops" in p["warn"], "renamed" in p["warn"]), (True, True))
    check("a set offers its undo", p["undo"], {"do": "set", "section": "default_radio1", "option": "ssid", "value": "Ranch"})
    host.sent.clear()
    await a.command(None, "plan", {"what": "set", "section": "radio0", "option": "channel", "value": "11"})
    p = [m for m in host.sent if m.get("event") == "plan"][-1]
    check("a 2.4 GHz change carries no HaLow warning", p["warn"], "")
    await a.command(None, "apply", {"id": p["id"]})
    ap = [m for m in host.sent if m.get("event") == "applied"][-1]
    check("applied, with its undo", (ap["ok"], ap["undo"]["value"]), (True, "6"))
    log = [json.loads(x) for x in (tmp / "state" / "halow-actions.log").read_text().splitlines()]
    check("every applied change is in the action log", [r["title"] for r in log],
          ["Stop the bridge's DHCP (ahwlan)", "Set radio0.channel = 11 (now 6)"])

    # --- unreachable
    host.notices.clear()
    (tmp / "down").write_text("")
    await a.read()
    await a.read()
    s = a.snapshot()
    check("unreachable: error shown, last reading kept, marked stale",
          ("Connection refused" in (s["error"] or ""), s["stale"], len(s["radios"])), (True, True, 2))
    check("unreachable: noticed once", len([m for _, m in host.notices if "can't read" in m]), 1)
    (tmp / "down").unlink()
    await a.read()
    check("answers again: noticed, no longer stale", (host.notices[-1][1], a.snapshot()["stale"]),
          ("HaLow: the bridge answers again.", False))

    # --- the registry lifecycle
    await reg.start()
    check("starts", reg.items["halow"]["running"], True)
    await reg.stop()
    check("stops", reg.items["halow"]["running"], False)


asyncio.run(main())

check("in the catalog (ranch_radio/catalog.py)", "halow" in catalog.by_name(), True)
check("in the manifest, an add-on in the addons block (ranch_radio/manifest.py)",
      (manifest.MANIFESTS.get("halow") or {}).get("block"), ("addons", "halow"))

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
