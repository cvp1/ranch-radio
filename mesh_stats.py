"""The Mesh tab's Health view: what this node hears, added up, and what looks wrong.

Pure: no I/O, no clock of its own (every call takes `now`), so it is tested
on its own (tests/mesh_stats_test.py). mesh.py feeds it the bridge's "rx"
events and node lists; the page draws summary().

What it keeps, in memory only (a restart starts it over; the page says since
when):
  - the last WINDOW_S of packets heard from other nodes: sender, port, hops,
    hop start (the hop limit the sender set), SNR/RSSI, via MQTT, the relay
    byte (the firmware's relayNode: the last byte of the node that passed it
    to us, firmware 2.6+), size
  - the last LOG_KEEP packets as a log
  - this node's own channel utilization and air-tx, from the node list: the
    highest of each UTIL_SLOT_S slot for the last UTIL_SLOTS slots (24 h)

findings() turns that into a short list, each with what to do about it:
  busy channel, a chatty node, a sender with a high hop limit, a weak direct
  link, a node heard only over MQTT, one relay carrying most of what reaches
  us, and much traffic on channels this node has no key for. Each needs
  enough packets to say anything: below the minimums it stays quiet.

Idea from Mesh-Client's Diagnostics tab (2026-11-17); only the parts that can
be worked out honestly from one node's own receive side are here.
"""

import time
from collections import Counter, deque

WINDOW_S = 3600               # packets kept for the per-node figures
RATE_BUCKET_S = 60            # the traffic chart: packets a minute ...
RATE_BUCKETS = 60             # ... for the last hour
LOG_KEEP = 200
UTIL_SLOT_S = 300             # channel utilization: the highest of each 5 minutes ...
UTIL_SLOTS = 288              # ... for 24 hours
TALKERS_KEEP = 15

# Meshtastic firmware (AirTime.h): above 25 % channel utilization a node holds
# back its own routine broadcasts (polite_channel_util_percent), above 40 %
# most of what it would send (max_channel_util_percent).
UTIL_WARN, UTIL_BAD = 25.0, 40.0
CHATTY_PER_HOUR = 60          # packets an hour from one node, once we have heard for MIN_SPAN_S
CHATTY_SHARE = 0.25           # or this share of everything heard ...
CHATTY_MIN = 20               # ... from at least this many of its packets
MIN_SPAN_S = 600              # an hourly rate needs at least 10 minutes of listening
HOP_START_DEFAULT = 3         # the firmware's default hop limit
WEAK_SNR_DB = -10.0           # a direct link averaging below this is weak on the longer presets
WEAK_MIN = 5
RELAY_SHARE = 0.5
RELAY_MIN = 20
ENCRYPTED_SHARE = 0.3
ENCRYPTED_MIN = 40

PORT_FIX = {
    "POSITION_APP": "its position broadcast is frequent: a longer interval, or smart position, sends far less",
    "TELEMETRY_APP": "its telemetry interval is short: 30 minutes or more is plenty for most uses",
    "NODEINFO_APP": "it sends its node info often: the default is every 3 hours",
    "TEXT_MESSAGE_APP": "it is a busy chat or a bot",
    "NEIGHBORINFO_APP": "its NeighborInfo interval is short (the firmware's floor is 4 hours on the default channel)",
    "RANGE_TEST_APP": "it is running a range test: leave that for a test, then switch it off",
    "ENCRYPTED": "on a channel this node has no key for, so the port isn't known",
}
LEVELS = ("info", "warn", "bad")


def port_name(port):
    """POSITION_APP -> position: how the page names a port."""
    return str(port or "?").removesuffix("_APP").replace("_", " ").lower()


class MeshStats:
    def __init__(self, now=None):
        self.started = time.time() if now is None else now
        self.packets = deque()        # within WINDOW_S, oldest first
        self.log = deque(maxlen=LOG_KEEP)
        self.util = {}                # slot number -> [util %, air-tx %], the highest in it
        self.util_now = None          # the latest sample: (t, util, air)

    # --- in

    def packet(self, p, now=None):
        """One packet heard from another node: a dict with from, port, hops,
        hop_start, snr, rssi, mqtt, relay, size (any may be None)."""
        now = time.time() if now is None else now
        rec = {"t": p.get("t") or now, "from": p.get("from"), "port": p.get("port") or "ENCRYPTED",
               "hops": p.get("hops"), "hop_start": p.get("hop_start"), "snr": p.get("snr"),
               "rssi": p.get("rssi"), "mqtt": bool(p.get("mqtt")), "relay": p.get("relay"),
               "size": p.get("size")}
        self.packets.append(rec)
        self.log.append(rec)
        self._prune(now)

    def sample(self, nodes, now=None):
        """This node's channel utilization and air-tx, from a node list (its
        LocalStats when it sends them, else its device metrics)."""
        now = time.time() if now is None else now
        me = next((n for n in nodes or [] if n.get("me")), None)
        if me is None:
            return
        stats = me.get("stats") or {}
        util = stats.get("util", me.get("util"))
        air = stats.get("air_tx", me.get("air_tx"))
        if util is None and air is None:
            return
        self.util_now = (now, util, air)
        slot = int(now // UTIL_SLOT_S)
        cur = self.util.setdefault(slot, [None, None])
        for i, v in enumerate((util, air)):
            if v is not None and (cur[i] is None or v > cur[i]):
                cur[i] = v
        for s in [s for s in self.util if s <= slot - UTIL_SLOTS]:
            del self.util[s]

    def _prune(self, now):
        while self.packets and now - self.packets[0]["t"] > WINDOW_S:
            self.packets.popleft()

    # --- out

    def talkers(self, now=None):
        """Per sender, over the window: {id: {...}}."""
        now = time.time() if now is None else now
        self._prune(now)
        span = max(1.0, min(WINDOW_S, now - self.started))
        out = {}
        for p in self.packets:
            nid = p["from"]
            if not nid:
                continue
            t = out.setdefault(nid, {"id": nid, "n": 0, "ports": Counter(), "hop_start": None, "hops": None,
                                     "snr": [], "rssi": None, "mqtt": 0, "last": 0, "relays": Counter()})
            t["n"] += 1
            t["ports"][p["port"]] += 1
            if p["hop_start"] is not None:
                t["hop_start"] = max(t["hop_start"] or 0, p["hop_start"])
            if p["hops"] is not None:
                t["hops"] = p["hops"]
            if p["mqtt"]:
                t["mqtt"] += 1
            elif p["hops"] == 0 and p["snr"] is not None:
                t["snr"].append(p["snr"])
                t["rssi"] = p["rssi"]
            if p["relay"] and p["hops"]:
                t["relays"][p["relay"]] += 1
            t["last"] = max(t["last"], p["t"])
        for t in out.values():
            snr = t.pop("snr")
            t["snr_avg"] = round(sum(snr) / len(snr), 2) if snr else None
            t["snr_n"] = len(snr)
            t["per_hour"] = round(t["n"] * 3600 / span) if span >= MIN_SPAN_S else None
            t["top_port"] = t["ports"].most_common(1)[0][0]
            t["ports"] = dict(t["ports"].most_common(4))
            t["relays"] = dict(t["relays"])
        return out

    def rate(self, now=None):
        """Packets heard in each of the last RATE_BUCKETS minutes, oldest first."""
        now = time.time() if now is None else now
        counts = [0] * RATE_BUCKETS
        for p in self.packets:
            i = int((now - p["t"]) // RATE_BUCKET_S)
            if 0 <= i < RATE_BUCKETS:
                counts[RATE_BUCKETS - 1 - i] += 1
        return counts

    def util_history(self):
        """[[slot start t, util %, air-tx %], ...], oldest first."""
        return [[s * UTIL_SLOT_S, u, a] for s, (u, a) in sorted(self.util.items())]

    def findings(self, nodes=None, now=None, talkers=None):
        now = time.time() if now is None else now
        talkers = self.talkers(now) if talkers is None else talkers
        out = []
        names = {n.get("id"): n.get("shortName") or n.get("longName") or n.get("id") for n in nodes or []}
        name = lambda nid: names.get(nid) or nid          # noqa: E731

        def add(level, kind, title, detail, fix, node=None):
            out.append({"level": level, "kind": kind, "node": node, "title": title, "detail": detail, "fix": fix})

        if self.util_now and self.util_now[1] is not None and now - self.util_now[0] < 2 * UTIL_SLOT_S:
            u = self.util_now[1]
            if u >= UTIL_WARN:
                add("bad" if u >= UTIL_BAD else "warn", "busy",
                    f"The channel is busy: {u:.0f} % utilization",
                    f"Above {UTIL_WARN:.0f} % nodes hold back their own routine broadcasts, above "
                    f"{UTIL_BAD:.0f} % most of what they would send.",
                    "Look at the top talkers below; fewer position and telemetry broadcasts, or a "
                    "faster preset across the mesh, frees the air.")

        total = sum(t["n"] for t in talkers.values())
        for t in sorted(talkers.values(), key=lambda t: -t["n"]):
            share = t["n"] / total if total else 0
            hourly = t["per_hour"] is not None and t["per_hour"] > CHATTY_PER_HOUR
            if hourly or (t["n"] >= CHATTY_MIN and share > CHATTY_SHARE):
                rate = f"{t['per_hour']} packets an hour" if t["per_hour"] is not None else f"{t['n']} packets"
                add("warn", "chatty", f"{name(t['id'])} is chatty: {rate}, {share:.0%} of what we hear",
                    f"Mostly {port_name(t['top_port'])} ({t['ports'][t['top_port']]} of {t['n']}).",
                    PORT_FIX.get(t["top_port"], "ask its owner to space out what it sends"), t["id"])
        for t in talkers.values():
            hs = t["hop_start"]
            if hs is not None and hs > HOP_START_DEFAULT and t["n"] >= 2:
                add("warn" if hs >= 6 else "info", "hop_limit", f"{name(t['id'])} sends with hop limit {hs}",
                    f"Each of its packets may be relayed up to {hs} times; the default is {HOP_START_DEFAULT}.",
                    f"A hop limit of {HOP_START_DEFAULT} reaches most meshes; more spends everyone's airtime.",
                    t["id"])
            if t["snr_n"] >= WEAK_MIN and t["snr_avg"] is not None and t["snr_avg"] < WEAK_SNR_DB:
                add("info", "weak_link", f"Weak direct link to {name(t['id'])}: {t['snr_avg']:g} dB on average",
                    f"Over {t['snr_n']} packets heard directly; packets this weak are often lost.",
                    "A better antenna or height at either end, or a relay between, makes it dependable.",
                    t["id"])
            if t["n"] >= 3 and t["mqtt"] == t["n"]:
                add("info", "mqtt_only", f"{name(t['id'])} is heard only over MQTT",
                    "None of its packets came over the air here.",
                    "It is not in radio range; messages to it depend on the internet gateway.", t["id"])

        relays = Counter()
        for t in talkers.values():
            relays.update(t["relays"])
        relayed = sum(relays.values())
        if relayed >= RELAY_MIN:
            byte, n = relays.most_common(1)[0]
            if n / relayed > RELAY_SHARE:
                cands = [x for x in nodes or [] if isinstance(x.get("num"), int) and x["num"] & 0xFF == byte
                         and not x.get("me")]
                direct = [x for x in cands if x.get("hops") == 0] or cands
                who = " or ".join(name(x.get("id")) for x in direct[:3]) or f"a node ending in {byte:02x}"
                add("info", "main_relay", f"Most relayed traffic reaches us through {who}",
                    f"{n} of the {relayed} packets that came over relays ({n / relayed:.0%}) were passed on by it.",
                    "This node leans on that one relay; a second router in range would keep it on the mesh "
                    "if that one goes quiet.", direct[0].get("id") if len(direct) == 1 else None)

        enc = sum(t["ports"].get("ENCRYPTED", 0) for t in talkers.values())
        if total >= ENCRYPTED_MIN and enc / total > ENCRYPTED_SHARE:
            add("info", "encrypted", f"{enc / total:.0%} of what we hear is on channels we have no key for",
                "Normal on a shared mesh: other groups' private channels. It still takes airtime.",
                "Nothing to fix here unless it is a channel of yours missing from this node.")
        out.sort(key=lambda f: -LEVELS.index(f["level"]))
        return out

    def summary(self, nodes=None, now=None):
        now = time.time() if now is None else now
        talkers = self.talkers(now)
        findings = self.findings(nodes, now, talkers)
        worst = max((LEVELS.index(f["level"]) for f in findings), default=0)
        top = sorted(talkers.values(), key=lambda t: -t["n"])[:TALKERS_KEEP]
        ports = Counter()
        for p in self.packets:
            ports[p["port"]] += 1
        return {"since": self.started, "now": now, "window_s": WINDOW_S, "total": sum(t["n"] for t in talkers.values()),
                "nodes": len(talkers), "rate": self.rate(now), "rate_bucket_s": RATE_BUCKET_S,
                "ports": dict(ports.most_common(8)), "talkers": top,
                "util": self.util_history(), "util_slot_s": UTIL_SLOT_S,
                "util_now": list(self.util_now) if self.util_now else None,
                "util_warn": UTIL_WARN, "util_bad": UTIL_BAD,
                "findings": findings, "status": ("healthy", "attention", "busy")[worst]}
