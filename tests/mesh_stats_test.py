"""mesh_stats.py: the Health view's counts and findings, with a made-up clock.

  - packets roll out of the hour; the per-minute chart puts each in its minute
  - per node: count, ports, hop start, direct SNR only (not relayed, not
    MQTT), relay bytes only from relayed packets; an hourly rate only after
    10 minutes of listening
  - channel utilization: the highest of each 5-minute slot, 24 h kept; from
    LocalStats when there, else the node's device metrics
  - each finding fires, and stays quiet just below its line: busy channel
    (warn at 25 %, bad at 40 %, and not on a stale sample), chatty node (by
    rate and by share), hop limit over 3, weak direct link, MQTT only, the
    main relay (named from the node list by its last byte, preferring a
    direct neighbour), traffic on channels without a key
  - the status follows the worst finding

    python3 tests/mesh_stats_test.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import mesh_stats as ms  # noqa: E402

fails = 0
T0 = 1_800_000_000.0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:60} got {got!r}" + ("" if ok else f" want {want!r}"))


def pkt(frm, t, port="POSITION_APP", hops=0, hop_start=3, snr=5.0, mqtt=False, relay=None):
    return {"from": frm, "t": t, "port": port, "hops": hops, "hop_start": hop_start, "snr": snr,
            "rssi": -100, "mqtt": mqtt, "relay": relay, "size": 50}


def kinds(fs, kind=None):
    return [f for f in fs if kind is None or f["kind"] == kind]


NODES = [{"id": "!b2a6fd78", "num": 0xb2a6fd78, "me": True, "shortName": "TEST"},
         {"id": "!0000cafe", "num": 0xcafe, "hops": 0, "shortName": "RDG"},
         {"id": "!1111cafe", "num": 0x1111cafe, "hops": 2, "shortName": "FAR"},
         {"id": "!0000beef", "num": 0xbeef, "hops": 0, "shortName": "BASE"},
         {"id": "!0000dead", "num": 0xdead, "hops": 2, "shortName": "TRK"}]

# --- windows and the chart
s = ms.MeshStats(now=T0)
s.packet(pkt("!0000beef", T0), now=T0)
s.packet(pkt("!0000beef", T0 + 30), now=T0 + 30)
s.packet(pkt("!0000beef", T0 + 600), now=T0 + 600)
r = s.rate(now=T0 + 600)
check("rate: 60 minutes", len(r), 60)
check("  the newest minute holds the newest packet", r[-1], 1)
check("  600 s and 570 s back: the 11th and 10th minutes back", (r[-11], r[-10]), (1, 1))
check("  three in all", sum(r), 3)
s.packet(pkt("!0000beef", T0 + 3700), now=T0 + 3700)
check("packets older than the hour roll out", s.talkers(now=T0 + 3700)["!0000beef"]["n"], 2)

# --- per node
s = ms.MeshStats(now=T0)
s.packet(pkt("!0000beef", T0, hops=0, snr=6.0), now=T0)
s.packet(pkt("!0000beef", T0 + 1, hops=0, snr=4.0, port="TEXT_MESSAGE_APP"), now=T0 + 1)
s.packet(pkt("!0000beef", T0 + 2, hops=1, snr=-15.0, relay=0xfe, hop_start=7), now=T0 + 2)
s.packet(pkt("!0000beef", T0 + 3, hops=0, snr=-20.0, mqtt=True), now=T0 + 3)
t = s.talkers(now=T0 + 5)["!0000beef"]
check("per node: count", t["n"], 4)
check("  ports", t["ports"], {"POSITION_APP": 3, "TEXT_MESSAGE_APP": 1})
check("  the highest hop start seen", t["hop_start"], 7)
check("  SNR from direct packets only (not relayed, not MQTT)", (t["snr_avg"], t["snr_n"]), (5.0, 2))
check("  relay bytes from relayed packets only", t["relays"], {0xfe: 1})
check("  MQTT counted", t["mqtt"], 1)
check("  no hourly rate after 5 s of listening", t["per_hour"], None)
s.packet(pkt("!0000beef", T0 + 900), now=T0 + 900)
check("  an hourly rate after 15 minutes", s.talkers(now=T0 + 900)["!0000beef"]["per_hour"], 20)

# --- channel utilization
s = ms.MeshStats(now=T0)
s.sample([{"me": True, "util": 10.0, "air_tx": 1.0}], now=T0)
s.sample([{"me": True, "util": 30.0, "air_tx": 2.0}], now=T0 + 10)
s.sample([{"me": True, "util": 20.0, "air_tx": 1.5}], now=T0 + 20)
h = s.util_history()
check("util: one 5-minute slot, its highest", (len(h), h[-1][1], h[-1][2]), (1, 30.0, 2.0))
s.sample([{"me": True, "util": 5.0, "stats": {"util": 7.5, "air_tx": 0.5}}], now=T0 + 400)
check("  LocalStats win over device metrics", s.util_history()[-1][1], 7.5)
s.sample([{"me": True, "util": 5.0}], now=T0 + 25 * 3600)
check("  slots older than 24 h dropped", len(s.util_history()), 1)
s.sample([{"me": False, "util": 99.0}], now=T0 + 25 * 3600 + 1)
check("  another node's figures are not ours", s.util_now[1], 5.0)

# --- findings: busy channel
for util, want in ((24.9, []), (25.0, ["warn"]), (39.9, ["warn"]), (40.0, ["bad"])):
    s = ms.MeshStats(now=T0)
    s.sample([{"me": True, "util": util}], now=T0)
    check(f"busy channel at {util} %", [f["level"] for f in kinds(s.findings(now=T0 + 1), "busy")], want)
s = ms.MeshStats(now=T0)
s.sample([{"me": True, "util": 50.0}], now=T0)
check("  a sample older than 10 minutes says nothing", kinds(s.findings(now=T0 + 700), "busy"), [])

# --- chatty: by share, and by rate
s = ms.MeshStats(now=T0)
for i in range(20):
    s.packet(pkt("!0000dead", T0 + i), now=T0 + i)
for i in range(20):
    s.packet(pkt(f"!0000{i:04x}", T0 + i), now=T0 + i)
f = kinds(s.findings(NODES, now=T0 + 30), "chatty")
check("chatty by share: 20 packets, half of all", [x["node"] for x in f], ["!0000dead"])
check("  named, with its port and what to do", ("TRK" in f[0]["title"], "Mostly position" in f[0]["detail"],
                                                 "position" in f[0]["fix"]), (True, True, True))
s = ms.MeshStats(now=T0)
for i in range(19):
    s.packet(pkt("!0000dead", T0 + i), now=T0 + i)
check("  19 packets: not yet", kinds(s.findings(NODES, now=T0 + 30), "chatty"), [])
s = ms.MeshStats(now=T0)
for i in range(61):
    s.packet(pkt("!0000dead", T0 + i * 50), now=T0 + i * 50)
for i in range(400):
    s.packet(pkt(f"!0001{i:04x}", T0 + i * 7.5), now=T0 + i * 7.5)
f = kinds(s.findings(NODES, now=T0 + 3600), "chatty")
check("chatty by rate: 61 an hour though only 13 % of traffic", [x["node"] for x in f], ["!0000dead"])

# --- hop limit, weak link, MQTT only
s = ms.MeshStats(now=T0)
for i in range(2):
    s.packet(pkt("!0000dead", T0 + i, hops=2, hop_start=7), now=T0 + i)
    s.packet(pkt("!0000beef", T0 + i, hop_start=3), now=T0 + i)
    s.packet(pkt("!0000cafe", T0 + i, hop_start=4), now=T0 + i)
f = {x["node"]: x["level"] for x in kinds(s.findings(NODES, now=T0 + 5), "hop_limit")}
check("hop limit: 7 warns, 4 is info, 3 says nothing", f, {"!0000dead": "warn", "!0000cafe": "info"})
s = ms.MeshStats(now=T0)
for i in range(5):
    s.packet(pkt("!0000cafe", T0 + i, snr=-12.0), now=T0 + i)
    s.packet(pkt("!0000beef", T0 + i, snr=-9.0), now=T0 + i)
for i in range(4):
    s.packet(pkt("!0000dead", T0 + i, snr=-18.0), now=T0 + i)
check("weak link: -12 dB over 5 packets; not -9 dB, not 4 packets",
      [x["node"] for x in kinds(s.findings(NODES, now=T0 + 5), "weak_link")], ["!0000cafe"])
s = ms.MeshStats(now=T0)
for i in range(3):
    s.packet(pkt("!0000dead", T0 + i, mqtt=True), now=T0 + i)
    s.packet(pkt("!0000beef", T0 + i, mqtt=i > 0), now=T0 + i)
check("MQTT only: all 3 of its packets; not a node also heard on air",
      [x["node"] for x in kinds(s.findings(NODES, now=T0 + 5), "mqtt_only")], ["!0000dead"])

# --- the main relay
s = ms.MeshStats(now=T0)
for i in range(11):
    s.packet(pkt("!0000dead", T0 + i, hops=2, relay=0xfe), now=T0 + i)
for i in range(9):
    s.packet(pkt("!0000dead", T0 + i, hops=2, relay=0xef), now=T0 + i)
f = kinds(s.findings(NODES, now=T0 + 30), "main_relay")
check("main relay: 11 of 20 through fe", len(f), 1)
check("  named as the direct neighbour ending in fe (not the far one)", (f[0]["node"], "RDG" in f[0]["title"],
                                                                         "FAR" in f[0]["title"]),
      ("!0000cafe", True, False))
s = ms.MeshStats(now=T0)
for i in range(10):
    s.packet(pkt("!0000dead", T0 + i, hops=2, relay=0xfe), now=T0 + i)
    s.packet(pkt("!0000dead", T0 + i, hops=2, relay=0xef), now=T0 + i)
check("  half and half: nothing", kinds(s.findings(NODES, now=T0 + 30), "main_relay"), [])
s = ms.MeshStats(now=T0)
for i in range(19):
    s.packet(pkt("!0000dead", T0 + i, hops=2, relay=0xfe), now=T0 + i)
check("  19 relayed packets: too few to say", kinds(s.findings(NODES, now=T0 + 30), "main_relay"), [])
s = ms.MeshStats(now=T0)
for i in range(20):
    s.packet(pkt("!0000dead", T0 + i, hops=2, relay=0x42), now=T0 + i)
f = kinds(s.findings(NODES, now=T0 + 30), "main_relay")
check("  a byte no known node ends in: said as such", f and "ending in 42" in f[0]["title"], True)

# --- channels without a key
s = ms.MeshStats(now=T0)
for i in range(40):
    s.packet(pkt(f"!0002{i:04x}", T0 + i, port="ENCRYPTED" if i < 13 else "POSITION_APP"), now=T0 + i)
check("encrypted: 13 of 40 (32 %)", len(kinds(s.findings(NODES, now=T0 + 50), "encrypted")), 1)
s = ms.MeshStats(now=T0)
for i in range(40):
    s.packet(pkt(f"!0002{i:04x}", T0 + i, port="ENCRYPTED" if i < 12 else "POSITION_APP"), now=T0 + i)
check("  12 of 40 (30 %): nothing", kinds(s.findings(NODES, now=T0 + 50), "encrypted"), [])

# --- the summary and its status
s = ms.MeshStats(now=T0)
check("nothing heard: healthy, no findings", (s.summary(now=T0)["status"], s.summary(now=T0)["findings"]),
      ("healthy", []))
for i in range(2):
    s.packet(pkt("!0000dead", T0 + i, hop_start=7), now=T0 + i)
check("a warning: attention", s.summary(NODES, now=T0 + 5)["status"], "attention")
s.sample([{"me": True, "util": 45.0}], now=T0 + 5)
summ = s.summary(NODES, now=T0 + 6)
check("a busy channel: busy, and it comes first", (summ["status"], summ["findings"][0]["kind"]), ("busy", "busy"))
check("summary carries the chart, talkers and limits",
      (len(summ["rate"]), summ["talkers"][0]["id"], summ["util_warn"], summ["util_bad"]),
      (60, "!0000dead", 25.0, 40.0))

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
