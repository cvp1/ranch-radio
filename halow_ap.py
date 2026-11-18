"""The HaLow access point's side of the HaLow add-on (addons/halow.py): what
is read from an OpenWrt Wi-Fi HaLow (802.11ah) bridge over SSH, how it is
parsed, and the exact command each change runs.

Nothing is installed on the box. One fixed read script (READ_SCRIPT) is sent
over SSH on each poll; its output is sections headed `@@name`. Changes are
plans: plan_*() validate every variable part against a strict rule, build the
command on this side and say what it will do. The page shows that exact
command and the add-on runs those bytes, once, only when the operator
confirms (addons/halow.py). The operator never sends a command string, so
nothing typed on the page can reach the box's shell unvalidated; every value
is shell-quoted as well.

Secret fields (Wi-Fi keys and passwords) are dropped on the box by the read
script and again here by parse_uci(), so they never reach a page or a log.
"""
import re
import shlex

# --- what is read --------------------------------------------------------

SERVICES = ("mediamtx", "aws_kvs", "dnsmasq", "mesh11sd", "trelay", "ttyd")
SECRET_OPTS = ("key", "key1", "key2", "key3", "key4", "sae_password", "password",
               "auth_secret", "acct_secret", "priv_key", "wpa_psk", "psk")
_SECRET_RE = "|".join(SECRET_OPTS)
# Lines that repeat every few seconds and bury the events worth reading
# (on the HT-H7608, checkstatus logs a missing uci entry ~900 times a day).
LOG_NOISE = "checkstatus\\[[0-9]+\\]: uci: Entry not found"

READ_SCRIPT = "; ".join([
    "echo @@uptime", "cat /proc/uptime /proc/loadavg",
    "echo @@date", "date -u +%s",
    "echo @@mem", "cat /proc/meminfo",
    "echo @@df", "df -k /overlay /tmp 2>/dev/null",
    "echo @@board", "ubus call system board 2>/dev/null",
    "echo @@wireless", f"uci -q show wireless | grep -v -E '\\.({_SECRET_RE})='",
    "echo @@dhcp", "uci -q show dhcp | grep -E '=dhcp$|\\.(interface|ignore|start|limit)='",
    "echo @@iwdev", "iw dev",
    "for i in $(iw dev | awk '$1==\"Interface\"{print $2}'); do echo \"@@sta $i\"; iw dev $i station dump; done",
    "echo @@neigh", "ip neigh show",
    "echo @@leases", "cat /tmp/dhcp.leases 2>/dev/null",
    "echo @@svc",
    "for s in " + " ".join(SERVICES) + "; do [ -x /etc/init.d/$s ] || continue; "
    "/etc/init.d/$s running >/dev/null 2>&1 && r=1 || r=0; /etc/init.d/$s enabled && e=1 || e=0; echo \"$s $r $e\"; done",
    "echo @@conn", "netstat -tun 2>/dev/null",
    "echo @@log", f"logread 2>/dev/null | grep -v -E '{LOG_NOISE}' | tail -n 80",
])

MAX_OUTPUT = 512 * 1024        # a read bigger than this is cut, never buffered without bound


def sections(text):
    """`@@name` headed output -> {name: [lines]} (a `@@sta wlan0` header -> key 'sta wlan0')."""
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return out


def _unquote(v):
    vals = re.findall(r"'((?:[^']|'\\'')*)'", v)
    if not vals:
        return v
    vals = [x.replace("'\\''", "'") for x in vals]
    return vals[0] if len(vals) == 1 else vals


def parse_uci(lines):
    """`uci show` lines -> {section: {".type": type, option: value}}, secrets dropped."""
    out = {}
    for line in lines:
        m = re.match(r"^[a-z0-9_]+\.([A-Za-z0-9_@\[\]-]+)(?:\.([A-Za-z0-9_]+))?=(.*)$", line.strip())
        if not m:
            continue
        sec, opt, val = m.groups()
        d = out.setdefault(sec, {})
        if opt is None:
            d[".type"] = val
        elif opt not in SECRET_OPTS:
            d[opt] = _unquote(val)
    return out


def parse_iw_dev(lines):
    """`iw dev` -> [{ifname, ssid, type, channel, freq, txpower, mac}]."""
    out, cur = [], None
    for line in lines:
        s = line.strip()
        m = re.match(r"^Interface (\S+)", s)
        if m:
            cur = {"ifname": m.group(1)}
            out.append(cur)
            continue
        if cur is None:
            continue
        if s.startswith("ssid "):
            cur["ssid"] = s[5:]
        elif s.startswith("type "):
            cur["type"] = s[5:]
        elif s.startswith("addr "):
            cur["mac"] = s[5:].lower()
        elif s.startswith("channel "):
            m = re.match(r"channel (\d+) \(([\d.]+) MHz\)", s)
            if m:
                cur["channel"], cur["freq"] = int(m.group(1)), float(m.group(2))
        elif s.startswith("txpower "):
            m = re.match(r"txpower ([\d.]+)", s)
            if m:
                cur["txpower"] = float(m.group(1))
    return out


def _num(v):
    m = re.match(r"\s*(-?[\d.]+)", v or "")
    return float(m.group(1)) if m else None


def parse_stations(ifname, lines):
    """`iw dev X station dump` -> [{mac, ifname, signal, signal_avg, tx_rate, rx_rate,
    inactive_ms, connected_s, tx_retries, tx_failed}]."""
    out, cur = [], None
    keys = {"signal": "signal", "signal avg": "signal_avg", "tx bitrate": "tx_rate", "rx bitrate": "rx_rate",
            "inactive time": "inactive_ms", "connected time": "connected_s", "tx retries": "tx_retries",
            "tx failed": "tx_failed", "rx bytes": "rx_bytes", "tx bytes": "tx_bytes"}
    for line in lines:
        m = re.match(r"^Station ([0-9a-fA-F:]{17})", line.strip())
        if m:
            cur = {"mac": m.group(1).lower(), "ifname": ifname}
            out.append(cur)
            continue
        if cur is None or ":" not in line:
            continue
        k, _, v = line.strip().partition(":")
        k = k.strip()
        if k in keys:
            n = _num(v)
            cur[keys[k]] = int(n) if n is not None and n == int(n) else n
            if k in ("tx bitrate", "rx bitrate"):
                cur[keys[k] + "_text"] = v.strip()
    return out


def parse_neigh(lines):
    """`ip neigh show` -> {mac: ip} (IPv4 only, the address a page can open)."""
    out = {}
    for line in lines:
        m = re.match(r"^(\d+\.\d+\.\d+\.\d+) dev (\S+) lladdr ([0-9a-fA-F:]{17}) (\S+)", line.strip())
        if m and m.group(4) != "FAILED":
            out[m.group(3).lower()] = m.group(1)
    return out


def parse_leases(lines):
    out = {}
    for line in lines:
        p = line.split()
        if len(p) >= 4:
            out[p[1].lower()] = {"ip": p[2], "name": p[3] if p[3] != "*" else ""}
    return out


def parse_host(sec):
    up = sec.get("uptime", [])
    host = {}
    if up:
        host["uptime_s"] = int(_num(up[0]) or 0)
    if len(up) > 1:
        p = up[1].split()
        if len(p) >= 3:
            host["load"] = [float(x) for x in p[:3]]
    d = sec.get("date", [])
    if d and d[0].strip().isdigit():
        host["clock"] = int(d[0].strip())
    mem = {}
    for line in sec.get("mem", []):
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable", "MemFree"):
            mem[k] = int(_num(v) or 0)
    if mem.get("MemTotal"):
        host["mem_kb"] = {"total": mem["MemTotal"], "available": mem.get("MemAvailable", mem.get("MemFree", 0))}
    for line in sec.get("df", [])[1:]:
        p = line.split()
        if len(p) >= 6 and p[5] in ("/overlay", "/tmp"):
            host.setdefault("disk_kb", {})[p[5]] = {"total": int(p[1]), "free": int(p[3])}
    board = "\n".join(sec.get("board", []))
    for k in ("model", "hostname", "kernel"):
        m = re.search(rf'"{k}":\s*"([^"]*)"', board)
        if m:
            host[k] = m.group(1)
    m = re.search(r'"description":\s*"([^"]*)"', board)
    if m:
        host["release"] = m.group(1)
    return host


def _private(ip):
    """LAN, loopback, link-local or multicast: not off the ranch."""
    ip = ip.strip("[]").lower()
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    if ":" in ip:
        return ip in ("::", "::1") or ip.startswith(("fe80:", "fc", "fd", "ff"))
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
    except ValueError:
        return True
    return a in (0, 10, 127) or (a == 169 and b == 254) or (a == 172 and 16 <= b <= 31) \
        or (a == 192 and b == 168) or a >= 224


def parse_conns(lines):
    """`netstat -tun` -> connections to addresses off the LAN:
    [{proto, local, remote, remote_ip, state}]. Listening sockets and LAN peers are left out."""
    out = []
    for line in lines:
        p = line.split()
        if len(p) < 5 or p[0] not in ("tcp", "tcp6", "udp", "udp6"):
            continue
        remote = p[4]
        rip, _, _port = remote.rpartition(":")
        if not rip or rip in ("0.0.0.0", "::", "*") or _private(rip):
            continue
        state = p[5] if len(p) > 5 else ""
        if state in ("LISTEN", "TIME_WAIT"):
            continue
        out.append({"proto": p[0], "local": p[3], "remote": remote, "remote_ip": rip, "state": state})
    return out


def parse_services(lines):
    out = []
    for line in lines:
        p = line.split()
        if len(p) == 3 and p[0] in SERVICES:
            out.append({"name": p[0], "running": p[1] == "1", "enabled": p[2] == "1"})
    return out


def dhcp_guard(dhcp):
    """Which DHCP pools on the box would hand out addresses. The bridge sits on
    the main LAN, so any pool that is not ignored is a second DHCP server
    racing the router (what made the LAN's addresses wander in 2026)."""
    pools = []
    for sec, d in dhcp.items():
        if d.get(".type") != "dhcp":
            continue
        pools.append({"section": sec, "interface": d.get("interface", ""), "serving": d.get("ignore") != "1"})
    return {"pools": pools, "serving": [p["section"] for p in pools if p["serving"]]}


def parse_read(text):
    """The read script's output -> the add-on's view of the box."""
    sec = sections(text)
    wireless = parse_uci(sec.get("wireless", []))
    ifs = parse_iw_dev(sec.get("iwdev", []))
    neigh, leases = parse_neigh(sec.get("neigh", [])), parse_leases(sec.get("leases", []))
    stations = []
    for name, lines in sec.items():
        if name.startswith("sta "):
            stations += parse_stations(name[4:], lines)
    for s in stations:
        lease = leases.get(s["mac"], {})
        s["ip"] = neigh.get(s["mac"]) or lease.get("ip", "")
        s["name"] = lease.get("name", "")
    radios = []
    for sec_name, d in wireless.items():
        if d.get(".type") == "wifi-device":
            radios.append({"section": sec_name, "type": d.get("type", ""), "channel": d.get("channel", ""),
                           "hwmode": d.get("hwmode", ""), "htmode": d.get("htmode", ""),
                           "txpower": d.get("txpower", ""), "country": d.get("country", ""),
                           "disabled": d.get("disabled") == "1",
                           "halow": d.get("type") == "morse" or d.get("hwmode") == "11ah"})
    ifaces = []
    halow_radios = {r["section"]: r for r in radios if r["halow"]}
    for sec_name, d in wireless.items():
        if d.get(".type") == "wifi-iface":
            live = dict(next((i for i in ifs if i.get("ssid") == d.get("ssid")), {}))
            if d.get("device") in halow_radios:
                # The Morse driver reports S1G as a stand-in 5 GHz channel to
                # mac80211 (channel 8 shows as "44 (5220 MHz)"): the radio's own
                # S1G channel is the true one.
                ch = halow_radios[d["device"]]["channel"]
                live["channel"], live["freq"] = int(ch) if str(ch).isdigit() else ch, None
            ifaces.append({"section": sec_name, "device": d.get("device", ""), "ssid": d.get("ssid", ""),
                           "mode": d.get("mode", ""), "network": d.get("network", ""),
                           "encryption": d.get("encryption", ""), "hidden": d.get("hidden") == "1",
                           "disabled": d.get("disabled") == "1", "ifname": live.get("ifname", ""),
                           "channel": live.get("channel"), "freq": live.get("freq")})
    return {"host": parse_host(sec), "radios": radios, "ifaces": ifaces, "iw": ifs,
            "stations": sorted(stations, key=lambda s: (s["ifname"], -(s.get("signal") or -999))),
            "dhcp": dhcp_guard(parse_uci(sec.get("dhcp", []))),
            "services": parse_services(sec.get("svc", [])),
            "outbound": parse_conns(sec.get("conn", [])),
            "log": sec.get("log", [])[-80:]}


# --- what can be changed -------------------------------------------------

MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
IPV4 = re.compile(r"^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$")
SECTION = re.compile(r"^[A-Za-z0-9_]{1,32}$")
SSID = re.compile(r"^[\x20-\x7e]{1,32}$")

# Each option a page may set: which section type it lives on, and its rule.
OPTIONS = {
    "channel": ("wifi-device", lambda v: v == "auto" or (v.isdigit() and 1 <= int(v) <= 233)),
    "txpower": ("wifi-device", lambda v: v.isdigit() and 0 <= int(v) <= 30),
    "disabled": ("both", lambda v: v in ("0", "1")),
    "ssid": ("wifi-iface", lambda v: bool(SSID.match(v)) and v == v.strip()),
    "hidden": ("wifi-iface", lambda v: v in ("0", "1")),
}


class Refused(ValueError):
    """A change that fails its rule: the reason is shown on the page."""


def q(v):
    return shlex.quote(str(v))


def plan_reboot(view):
    return {"title": "Reboot the HaLow bridge", "cmd": "reboot",
            "warn": "Every camera on it drops until it is back (about two minutes)."}


def plan_kick(view, ifname, mac):
    mac = str(mac).lower()
    if not MAC.match(mac):
        raise Refused(f"not a MAC address: {mac!r}")
    if ifname not in {i["ifname"] for i in view.get("iw", [])}:
        raise Refused(f"no interface {ifname!r} on the bridge")
    return {"title": f"Disconnect {mac} from {ifname}", "cmd": f"iw dev {q(ifname)} station del {q(mac)}",
            "warn": "It may reconnect at once; this is a nudge, not a block."}


def _section_type(view, section):
    for r in view.get("radios", []):
        if r["section"] == section:
            return "wifi-device", r
    for i in view.get("ifaces", []):
        if i["section"] == section:
            return "wifi-iface", i
    return None, None


def plan_set(view, section, option, value):
    value = str(value).strip() if option != "ssid" else str(value)
    if not SECTION.match(str(section)):
        raise Refused(f"not a section name: {section!r}")
    if option not in OPTIONS:
        raise Refused(f"{option!r} can't be set here (only {', '.join(OPTIONS)})")
    kind, rule = OPTIONS[option]
    stype, cur = _section_type(view, section)
    if stype is None:
        raise Refused(f"no wireless section {section!r} on the bridge")
    if kind != "both" and kind != stype:
        raise Refused(f"{option} belongs on a {kind}, and {section} is a {stype}")
    if not rule(value):
        raise Refused(f"{value!r} is not a valid {option}")
    old = cur.get(option, "")
    if isinstance(old, bool):
        old = "1" if old else "0"
    warn = []
    halow = (stype == "wifi-device" and cur.get("halow")) or (
        stype == "wifi-iface" and any(r["section"] == cur.get("device") and r["halow"] for r in view.get("radios", [])))
    if halow:
        warn.append("This is the HaLow radio: every camera on it drops while it reloads.")
        if option == "ssid":
            warn.append("Cameras join by name; they won't rejoin a renamed network until each is changed too.")
        if option == "channel":
            warn.append("HaLow channel numbers follow the radio's country; a channel outside it won't come up.")
        if option == "disabled" and value == "1":
            warn.append("Switching it off cuts the cameras off until it is switched on again.")
    return {"title": f"Set {section}.{option} = {value}" + (f" (now {old})" if old != "" else ""),
            "cmd": f"uci set {q(f'wireless.{section}.{option}={value}')} && uci commit wireless && wifi reload",
            "warn": " ".join(warn), "undo": {"do": "set", "section": section, "option": option, "value": old}
            if old != "" else None}


def plan_service(view, name, action):
    if action not in ("restart", "start", "stop", "disable", "enable"):
        raise Refused(f"not a service action: {action!r}")
    if name not in {s["name"] for s in view.get("services", [])}:
        raise Refused(f"no service {name!r} on the bridge")
    warn = "dnsmasq is the bridge's DNS; its DHCP pools stay off." if name == "dnsmasq" else ""
    if name == "mediamtx" and action in ("restart", "stop", "disable"):
        warn = "Camera restreams (RTSP/HLS/WebRTC) through the bridge drop."
    if name == "aws_kvs" and action in ("start", "enable"):
        warn = "This uploads camera video to Amazon Kinesis Video Streams if it is configured with an AWS account."
    if name == "ttyd" and action in ("start", "enable"):
        warn = "ttyd is a root login shell in the browser, open to the whole LAN."
    cmd = f"/etc/init.d/{q(name)} {action}"
    if action == "disable":          # off now and at boot
        cmd = f"/etc/init.d/{q(name)} stop; /etc/init.d/{q(name)} disable"
    title = {"disable": f"Switch {name} off (now and at boot)", "enable": f"Start {name} at boot"}.get(action,
                                                                                                    f"{action.capitalize()} {name}")
    return {"title": title, "cmd": cmd, "warn": warn}


def plan_dhcp_off(view):
    serving = view.get("dhcp", {}).get("serving", [])
    if not serving:
        raise Refused("no DHCP pool on the bridge is serving")
    for s in serving:
        if not SECTION.match(s):
            raise Refused(f"not a section name: {s!r}")
    sets = " && ".join(f"uci set {q(f'dhcp.{s}.ignore=1')}" for s in serving)
    return {"title": f"Stop the bridge's DHCP ({', '.join(serving)})",
            "cmd": f"{sets} && uci commit dhcp && /etc/init.d/dnsmasq restart",
            "warn": "The router stays the LAN's one DHCP server, as it should be."}


def ping_cmd(ip):
    """A read, from the bridge: can it reach a camera behind it."""
    if not IPV4.match(str(ip)):
        raise Refused(f"not an IPv4 address: {ip!r}")
    return f"ping -c 3 -W 1 {q(ip)}"
