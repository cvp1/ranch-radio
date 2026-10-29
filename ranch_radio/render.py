"""Render a host file into the files that host installs.

    artifacts(host) -> [Artifact]

An Artifact is one installed file: where it goes (relative to the home
directory), its format (plist, systemd, json) and its content. Comparison is
by meaning, not bytes: a plist as a dict, station.json as JSON, a systemd
unit as its keys (Description and comments ignored; Exec* lines in order).
"""

import json
import os
import plistlib
import re
from dataclasses import dataclass

HEADER = "# Rendered by ranch-radio from hosts/{host}.json. Edit that file, then `ranch-radio install`.\n"
UNIT_DIR = ".config/systemd/user"
STATION_JSON = ".config/radio-remote/station.json"
AGENTS = "Library/LaunchAgents"
WIREPLUMBER_CONF = ".config/wireplumber/wireplumber.conf.d/51-ranch-radio-audio.conf"
# Below the laptop's own sinks and sources (1000-2100), so a radio's sound
# card never wins the desktop default, even from WirePlumber's history of
# past defaults (stored priority + 20000 - position).
RADIO_AUDIO_PRIORITY = 100
# A Mac has one radio server, and it runs the host's add-ons (server.py --addons).
MAC_ADDONS = ".config/radio-remote/addons.json"
ARDOP_STATE = "~/.local/state/radio-remote/ardop"
# The daily update check (ranch-radio update --scheduled). On unless the host
# file says "updates": false; it applies updates only with "apply": true.
UPDATE_LABEL = "org.ranch-radio.update"
UPDATE_SERVICE = "ranch-radio-update.service"
UPDATE_TIMER = "ranch-radio-update.timer"
UPDATE_AT = "10:00"
UPDATE_LOG = "~/Library/Logs/ranch-radio-update.log"
# The packet hub, and Direwolf when it is the hub's modem ("tnc": "direwolf").
PACKET_LABEL = "org.ranch-radio.packet-hub"
DIREWOLF_LABEL = "org.ranch-radio.direwolf"
DIREWOLF_SERVICE = "direwolf.service"
DIREWOLF_PTY = "/tmp/kisstnc"            # Direwolf -p makes this link to its KISS pseudo-terminal
ORDERED_KEYS = re.compile(r"^(Exec|Environment)")


@dataclass
class Artifact:
    module: str          # catalog name: ic7300, station, winlink, ...
    path: str            # relative to $HOME
    fmt: str             # plist | systemd | json | text
    content: object      # dict for plist/json, str for systemd

    def text(self):
        if self.fmt == "plist":
            return plistlib.dumps(self.content).decode()
        if self.fmt == "json":
            return json.dumps(self.content, indent=2) + "\n"
        return self.content


class HostError(ValueError):
    pass


# ---------------------------------------------------------------- value rules
#
# Host-file values end up on command lines: a Linux unit's ExecStart is one
# space-joined line, the hub's --mycall/--tnc are raw words, wait_audio puts
# the sound card names inside sh -c '...', and install prints launchd labels
# into commands meant for pasting. A newline, a space or a shell character in
# any of them could add a directive or an argument (review 2026-10-21, Grok:
# ardop_logs with a newline, mycall "N0CALL --tx", a label with ";"). So every
# string is checked here, before anything renders.

CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# One command-line word: no whitespace, quotes, backslash, shell or systemd
# specials, and not an option (leading "-").
TOKEN = re.compile(r"[^\s'\"\\;&|`$<>(){}*?!#%\[\]-][^\s'\"\\;&|`$<>(){}*?!#%\[\]]*")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
CALL = re.compile(r"[A-Za-z0-9]{3,7}(-[0-9]{1,2})?")
SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
TEXT = re.compile(r"[^-].*")              # free text passed as one argv element (no shell)
CLOCK = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
USB_ID = re.compile(r"[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}")
PTT_MODE = re.compile(r"off|serial")

# (path, rule): "*" is every element of a list or every value of an object.
RULES = [
    (("host",), SLUG), (("python",), TOKEN), (("repo",), TOKEN), (("call",), CALL),
    (("radios", "*", "id"), SLUG), (("radios", "*", "rig"), SLUG), (("radios", "*", "device"), TOKEN),
    (("radios", "*", "rigctld"), TOKEN),
    (("radios", "*", "audio_in"), TOKEN), (("radios", "*", "audio_out"), TOKEN),
    (("radios", "*", "audio_device"), TEXT),
    (("radios", "*", "codec_db"), TOKEN), (("radios", "*", "voice_codec_db"), TOKEN),
    (("radios", "*", "codec_mixer"), TOKEN), (("radios", "*", "winlink_mode"), TOKEN),
    (("radios", "*", "rigctl_host"), TOKEN), (("radios", "*", "tls_names", "*"), TOKEN),
    (("radios", "*", "log"), TOKEN), (("radios", "*", "path"), TOKEN), (("radios", "*", "label"), LABEL),
    (("radios", "*", "jtdx_profile"), TOKEN),
    (("winlink", "pat"), TOKEN), (("winlink", "ardopcf"), TOKEN), (("winlink", "ardop_logs"), TOKEN),
    (("winlink", "ardopcf", "bin"), TOKEN), (("winlink", "ardopcf", "label"), LABEL),
    (("winlink", "ardopcf", "audio_in"), TEXT), (("winlink", "ardopcf", "audio_out"), TEXT),
    (("winlink", "ardopcf", "hostcommands"), TEXT),
    (("packet", "tnc"), TOKEN), (("packet", "mycall"), CALL), (("packet", "allow_calls", "*"), CALL),
    (("packet", "url"), TOKEN), (("packet", "agwpe"), TOKEN),
    (("packet", "name"), LABEL), (("packet", "title"), TEXT),
    (("packet", "pat_http", "pat"), TOKEN), (("packet", "pat_http", "addr"), TOKEN),
    (("packet", "label"), LABEL),
    (("packet", "direwolf", "usb"), USB_ID), (("packet", "direwolf", "audio"), TEXT),
    (("packet", "direwolf", "ptt"), PTT_MODE), (("packet", "direwolf", "bin"), TOKEN),
    (("packet", "direwolf", "label"), LABEL),
    (("packet", "direwolf", "igate", "login"), CALL), (("packet", "direwolf", "igate", "passcode"), TOKEN),
    (("packet", "direwolf", "igate", "server"), TOKEN),
    (("mesh", "port"), TOKEN), (("mesh", "keys", "*"), TOKEN), (("mesh", "python"), TOKEN),
    (("mesh", "bridge"), TOKEN), (("mesh", "log"), TOKEN),
    (("integrations", "macos-widget", "label"), LABEL), (("integrations", "macos-widget", "app"), TOKEN),
    (("updates", "at"), CLOCK), (("updates", "label"), LABEL),
    (("addons", "*"), None),     # add-on names: checked below
]
PORTS = [("radios", "*", "web"), ("radios", "*", "rigctl"), ("radios", "*", "lane"),
         ("winlink", "pat_port"), ("winlink", "lane_port"), ("winlink", "ardop_port"),
         ("winlink", "ardopcf", "port"), ("station", "rigctl_port")]
BOOLS = [("radios", "*", "tx"), ("radios", "*", "https"), ("radios", "*", "auto_tune"), ("radios", "*", "codec_save"),
         ("packet", "tx"), ("station", "https"), ("addons", "*", "enabled"), ("updates", "apply")]


def _at(d, path, where=()):
    """(where, value) for every value at path; "*" fans out over lists and objects."""
    if not path:
        yield where, d
        return
    k, rest = path[0], path[1:]
    if k == "*":
        items = enumerate(d) if isinstance(d, list) else d.items() if isinstance(d, dict) else ()
        for i, v in items:
            yield from _at(v, rest, where + (i,))
    elif isinstance(d, dict) and k in d:
        yield from _at(d[k], rest, where + (k,))


def _where(where):
    return ".".join(str(w) for w in where)


def _strings(d, where=()):
    if isinstance(d, str):
        yield where, d
    elif isinstance(d, dict):
        for k, v in d.items():
            yield where + (k,), k
            yield from _strings(v, where + (k,))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            yield from _strings(v, where + (i,))


def _validate_port(host, where, v, is_rigctl=False):
    """Validate TCP port number constraints, permitting port 0 strictly for rigctl."""
    if not isinstance(v, int) or isinstance(v, bool):
        raise HostError(f"{host}: {_where(where)} = {v!r}: must be an integer")
    if is_rigctl:
        if not (0 <= v < 65536):
            raise HostError(f"{host}: {_where(where)} = {v!r}: must be between 0 and 65535")
    else:
        if not (1 <= v < 65536):
            raise HostError(f"{host}: {_where(where)} = {v!r}: must be a valid port number (1-65535)")
    return v


def check_values(h):
    """Refuse any value that could break out of the line it is rendered into."""
    host = h.get("host", "?")
    for where, v in _strings(h):
        if CONTROL.search(v):
            raise HostError(f"{host}: {_where(where)}: control character (newline, tab, NUL) not allowed")
    for path, rule in RULES:
        if rule is None:
            continue
        for where, v in _at(h, path):
            if isinstance(v, dict) and path[-1] == "ardopcf":
                continue                  # a Mac's ardopcf is a block; its fields have their own rules
            if not isinstance(v, str) or not rule.fullmatch(v):
                raise HostError(f"{host}: {_where(where)} = {v!r}: not allowed here "
                                f"(one word: no spaces, quotes or shell characters, not starting with -)")
    for name in (h.get("addons") or {}):
        if not SLUG.fullmatch(name):
            raise HostError(f"{host}: add-on name {name!r} not allowed")
    for path in PORTS:
        for where, v in _at(h, path):
            is_rigctl = "rigctl" in path[-1] if path else False
            _validate_port(host, where, v, is_rigctl=is_rigctl)
    for path in BOOLS:
        for where, v in _at(h, path):
            if not isinstance(v, bool):
                raise HostError(f"{host}: {_where(where)} = {v!r}: must be true or false")
    for where, v in _at(h, ("radios", "*", "tx_timeout")):
        if not (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0):
            raise HostError(f"{host}: {_where(where)} = {v!r}: must be a number of seconds")


SAFE_WORD = re.compile(r"[A-Za-z0-9_@%+=:,./~-]+")


def sd_word(w):
    """One ExecStart word, quoted the systemd way only when it needs it.
    check_values already refuses unsafe values; this keeps a word one word
    even so (and leaves today's units byte-for-byte as they are)."""
    w = str(w)
    if SAFE_WORD.fullmatch(w):
        return w
    return '"' + w.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%") + '"'


def exec_line(argv):
    return " ".join(sd_word(w) for w in argv)


# ---------------------------------------------------------------- loading

def load(path):
    with open(path) as f:
        h = json.load(f)
    validate(h)
    return h


def validate(h):
    for k in ("host", "os", "repo", "python", "radios"):
        if k not in h:
            raise HostError(f"{h.get('host', '?')}: missing {k!r}")
    check_values(h)
    if h["os"] not in ("macos", "linux"):
        raise HostError(f"{h['host']}: os must be macos or linux, not {h['os']!r}")
    ids, ports = set(), {}
    for r in h["radios"]:
        for k in ("id", "name", "device"):
            if k not in r:
                raise HostError(f"{h['host']}: a radio is missing {k!r}")
        if r["id"] in ids:
            raise HostError(f"{h['host']}: radio id {r['id']!r} twice")
        ids.add(r["id"])
        # Two radios on one port would key the wrong transmitter: refuse.
        for k in ("web", "rigctl", "lane"):
            if k in r:
                p = r[k]
                if k == "rigctl" and p == 0:
                    continue
                if p in ports:
                    raise HostError(f"{h['host']}: port {p} is both {ports[p]} and {r['id']}.{k}")
                ports[p] = f"{r['id']}.{k}"
    st = h.get("station") or {}
    st_rigctl = st.get("rigctl_port")
    if st_rigctl and st_rigctl > 0 and st_rigctl in ports:
        raise HostError(f"{h['host']}: station.rigctl_port {st_rigctl} conflicts with {ports[st_rigctl]}")
    for r in h["radios"]:
        # A mixer's lowest step can be mute (the Digirig: -37 dB is off), and a
        # muted codec keys the radio at 0 W: refuse levels below codec_min_db.
        if r.get("voice_codec_db") and not r.get("codec_mixer") and h["os"] == "linux":
            raise HostError(f"{h['host']}: {r['id']}.voice_codec_db needs codec_mixer (CARD:CONTROL)")
        if r.get("codec_save") and not (r.get("codec_db") or r.get("voice_codec_db")):
            raise HostError(f"{h['host']}: {r['id']}.codec_save without codec_db: nothing to save")
        lo, hi = r.get("codec_min_db"), r.get("codec_max_db")
        if lo is not None and hi is not None and hi < lo:
            raise HostError(f"{h['host']}: {r['id']}.codec_max_db {hi} is below codec_min_db {lo}")
        if lo is None and hi is None:
            continue
        for key in ("codec_db", "voice_codec_db"):
            for part in str(r.get(key) or "").split(","):
                try:
                    db = float(part.rpartition("=")[2])
                except ValueError:
                    continue                 # server.py reports a malformed table
                if lo is not None and db < lo:
                    raise HostError(f"{h['host']}: {r['id']}.{key} {part.strip()!r} is below "
                                    f"codec_min_db {lo} (the mixer's next step down is mute)")
                if hi is not None and db > hi:
                    raise HostError(f"{h['host']}: {r['id']}.{key} {part.strip()!r} is above "
                                    f"codec_max_db {hi} (the radio's ceiling)")
    if h["os"] == "linux" and len(h["radios"]) > 0 and "station" not in h:
        raise HostError(f"{h['host']}: a Linux host serves its radios through the station; add \"station\"")
    if h["os"] == "macos" and len(h["radios"]) > 1:
        raise HostError(f"{h['host']}: one radio per Mac (station.py is Linux-only)")
    for u in (h.get("manual_restart") or {}):
        if not isinstance(h["manual_restart"][u], str) or not h["manual_restart"][u].strip():
            raise HostError(f"{h['host']}: manual_restart {u!r} needs the reason")
    pk = h.get("packet") or {}
    if pk.get("tnc") == "direwolf":
        dw = pk.get("direwolf")
        if not isinstance(dw, dict) or not dw.get("usb") or not dw.get("audio"):
            raise HostError(f"{h['host']}: packet.tnc \"direwolf\" needs a packet.direwolf block with "
                            f"\"usb\" (the cable's VID:PID) and \"audio\" (its sound card)")
        td = dw.get("txdelay")
        if td is not None and (not isinstance(td, int) or isinstance(td, bool) or not 10 <= td <= 2000):
            raise HostError(f"{h['host']}: packet.direwolf.txdelay = {td!r}: milliseconds, 10 to 2000")
        ig = dw.get("igate")
        if ig is not None and (not isinstance(ig, dict) or not ig.get("login") or not ig.get("passcode")):
            raise HostError(f"{h['host']}: packet.direwolf.igate needs \"login\" (callsign-SSID) and "
                            f"\"passcode\" (a file holding the APRS-IS passcode)")
    elif pk.get("direwolf"):
        raise HostError(f"{h['host']}: packet.direwolf is set but packet.tnc isn't \"direwolf\"")
    u = h.get("updates", True)
    if u is not True and u is not False and not isinstance(u, dict):
        raise HostError(f"{h['host']}: updates must be false or an object like "
                        f"{{\"at\": \"10:00\", \"apply\": false}}")
    for name, opts in (h.get("addons") or {}).items():
        if not isinstance(opts, dict):
            raise HostError(f"{h['host']}: add-on {name!r} needs an object")


# ---------------------------------------------------------------- paths

def _unit_path(p):
    """~ -> %h for a systemd user unit."""
    return "%h" + p[1:] if p.startswith("~") else p


def _abs(p, home):
    return home + p[1:] if p.startswith("~") else p


def systemd_escape_path(p):
    """systemd-escape --path, for the device names we use."""
    parts = [x for x in p.strip("/").split("/") if x]
    out = []
    for part in parts:
        s = ""
        for i, ch in enumerate(part):
            if (ch.isalnum() and ch.isascii()) or ch in ":_" or (ch == "." and i > 0):
                s += ch
            else:
                s += "\\x%02x" % ord(ch)
        out.append(s)
    return "-".join(out)


# ---------------------------------------------------------------- server.py

def updates(h):
    """The host's update check: {"at", "apply", "label"}, or None when "updates": false."""
    u = h.get("updates", True)
    if u is False:
        return None
    u = u if isinstance(u, dict) else {}
    return {"at": u.get("at", UPDATE_AT), "apply": bool(u.get("apply", False)), "label": u.get("label", UPDATE_LABEL)}


def mac_addons(h):
    """The add-ons a Mac's radio server runs: the host file's, plus the Packet
    tab when the host runs a packet hub (a Mac has no station page to show
    it). A host file that lists "packet" itself keeps its own entry."""
    out = dict(h.get("addons") or {})
    p = h.get("packet")
    if p and p.get("tnc") and h["radios"] and "packet" not in out:
        out["packet"] = {"enabled": True, "url": p.get("url", "http://127.0.0.1:7310"),
                         **{k: p[k] for k in ("name", "title") if k in p}}
    return out


def hub_argv(h, py, repo):
    """The packet hub's command line (tnc_hub.py), either OS."""
    p = h["packet"]
    tnc = DIREWOLF_PTY if p["tnc"] == "direwolf" else p["tnc"]
    argv = [py, f"{repo}/tnc_hub.py", "--tnc", tnc, "--mycall", p["mycall"]]
    if p.get("allow_calls"):
        argv += ["--allow-calls", ",".join(p["allow_calls"])]
    if p.get("tx"):
        argv += ["--tx"]                 # transmit allowed (off unless the host file says so)
    pos = p.get("position")
    if pos:                              # this station on the APRS map; distances from it
        argv += ["--lat", str(pos["lat"]), "--lon", str(pos["lon"])]
    if p["tnc"] == "direwolf":
        argv += ["--control-port", "0"]  # the radio-control channel is the UV-PRO's alone
    if p.get("title"):
        argv += ["--title", p["title"]]  # the hub page's heading (else "UV-PRO Packet")
    return argv


def direwolf_argv(h, py, repo, pth):
    """direwolf_tnc.py's command line: Direwolf as the hub's modem."""
    p = h["packet"]
    d = p["direwolf"]
    argv = [py, f"{repo}/direwolf_tnc.py", "--usb", d["usb"], "--audio", d["audio"],
            "--mycall", p["mycall"], "--ptt", d.get("ptt", "off")]
    if d.get("bin"):
        argv += ["--direwolf", pth(d["bin"])]
    if d.get("txdelay"):
        argv += ["--txdelay", str(d["txdelay"])]
    ig = d.get("igate")
    if ig:
        argv += ["--igate-login", ig["login"], "--igate-passcode-file", pth(ig["passcode"])]
        if ig.get("server"):
            argv += ["--igate-server", ig["server"]]
    return argv


def server_args(h, r, pth):
    """The server.py arguments for radio r; pth maps a ~ path for this OS."""
    a = []
    linux = h["os"] == "linux"
    if r.get("rig"):
        a += ["--rig", r["rig"]]
    if r.get("rigctld"):
        a += ["--rigctld", r["rigctld"]]
    a += ["--port", r["device"]]
    if r.get("audio_in") and r.get("audio_out"):
        a += ["--audio-device", f"pipewire:{r['audio_in']},{r['audio_out']}"]
    elif r.get("audio_device"):
        a += ["--audio-device", r["audio_device"]]
    if r.get("https"):
        a += ["--https"]
    if r.get("tx_timeout"):
        a += ["--tx-timeout", str(r["tx_timeout"])]
    if r.get("tx") is False:                    # receive only (init, until there's a callsign)
        a += ["--no-tx"]
    if r.get("auto_tune"):
        a += ["--auto-tune"]
    if linux:                                   # behind the station: page on loopback
        a += ["--host", "127.0.0.1", "--listen", str(r["web"])]
        a += ["--rigctl-host", r.get("rigctl_host", "0.0.0.0")]
    elif r.get("rigctl_host"):
        a += ["--rigctl-host", r["rigctl_host"]]
    if r.get("rigctl") is not None:
        a += ["--rigctl-port", str(r["rigctl"])]
    for n in r.get("tls_names", []):
        a += ["--tls-name", n]
    w = h.get("winlink")
    if r.get("lane"):
        if not linux and w:                     # a Mac's server runs Pat itself
            a += ["--winlink-pat", w["pat"]]
        a += ["--winlink-rigctl-port", str(r["lane"])]
        if r.get("codec_db"):
            a += ["--winlink-codec-db", r["codec_db"]]
        if r.get("codec_mixer"):
            a += ["--winlink-codec-mixer", r["codec_mixer"]]
        if r.get("voice_codec_db"):
            a += ["--voice-codec-db", r["voice_codec_db"]]
        if r.get("codec_min_db") is not None:
            a += ["--codec-min-db", f"{r['codec_min_db']:g}"]
        if r.get("codec_max_db") is not None:
            a += ["--codec-max-db", f"{r['codec_max_db']:g}"]
        if r.get("codec_save"):
            a += ["--codec-save", f"{h['host']}:{r['id']}"]
        logs = (w or {}).get("ardop_logs", ARDOP_STATE)
        a += ["--winlink-ardop-log", pth(logs + f"/{r['id']}" if linux else logs)]
        if r.get("winlink_mode"):
            a += ["--winlink-mode", r["winlink_mode"]]
    m = h.get("mesh")
    if m and not linux:                         # on Linux the station owns the node
        a += ["--mesh-port", m["port"]]
        for k in m.get("keys", []):
            ch, _, f = k.partition("=")
            a += ["--mesh-key", f"{ch}={pth(f)}"]
    if mac_addons(h) and not linux:             # on Linux the station runs them
        a += ["--addons", pth("~/" + MAC_ADDONS)]
    if not linux:                               # the Modules tab (on Linux: the station's)
        a += ["--host-file", h["host"]]
    return a


# ---------------------------------------------------------------- systemd

def unit(host, sections):
    out = [HEADER.format(host=host)]
    for name, items in sections:
        out.append(f"\n[{name}]\n")
        out += [f"{k}={v}\n" for k, v in items]
    return "".join(out)


def parse_unit(text):
    """{section: {key: [values]}} without comments, blank lines or Description.
    Values keep their order for Exec*/Environment keys; others are sorted."""
    d, sec = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            sec = d.setdefault(line[1:-1], {})
            continue
        k, _, v = line.partition("=")
        if sec is None or k == "Description":
            continue
        sec.setdefault(k.strip(), []).append(v.strip())
    for s in d.values():
        for k, v in s.items():
            if not ORDERED_KEYS.match(k):
                v.sort()
    return d


def _linux(h):
    # systemd doesn't expand ~ (init writes ~/ranch-radio/.venv/bin/python): %h it.
    host, py, repo = h["host"], _unit_path(h["python"]), _unit_path(h["repo"])
    arts = []
    for r in h["radios"]:
        rid, dev = r["id"], systemd_escape_path(r["device"]) + ".device"
        svc, sock = f"{rid}-remote.service", f"{rid}-remote.socket"
        pre = []
        if r.get("lane"):
            logs = (h.get("winlink") or {}).get("ardop_logs", ARDOP_STATE)
            pre = [("ExecStartPre", f"/usr/bin/mkdir -p {_unit_path(logs)}/{rid}")]
        if r.get("audio_in") and r.get("audio_out"):
            pre.append(("ExecStartPre", wait_audio(r)))
        argv = exec_line([py, f"{repo}/server.py"] + server_args(h, r, _unit_path))
        unit_deps = [("Description", r.get("description", f"Ranch Radio: {r['name']}")),
                     ("After", "network-online.target sound.target pipewire.service wireplumber.service"),
                     ("Wants", "pipewire.service wireplumber.service"),
                     ("BindsTo", dev), ("After", dev)]
        if r.get("rigctl") and r["rigctl"] > 0:
            unit_deps += [("Wants", sock), ("After", sock)]
        arts.append(Artifact(rid, f"{UNIT_DIR}/{svc}", "systemd", unit(host, [
            ("Unit", unit_deps),
            ("Service", pre + [("ExecStart", argv),
                               ("Restart", "on-failure"), ("RestartSec", "5"),
                               ("NotifyAccess", "main"),
                               ("FileDescriptorStoreMax", "16"), ("FileDescriptorStorePreserve", "restart"),
                               ("RuntimeDirectory", f"{rid}-remote"), ("RuntimeDirectoryPreserve", "restart"),
                               ("TimeoutStopSec", "10")]),
            ("Install", [("WantedBy", dev)])])))
        if r.get("rigctl") and r["rigctl"] > 0:
            arts.append(Artifact(rid, f"{UNIT_DIR}/{sock}", "systemd", unit(host, [
                ("Unit", [("Description", f"rigctl port ({r['rigctl']}) for Ranch Radio ({r['name']}), "
                                          "held across server restarts"),
                          ("BindsTo", dev), ("After", dev)]),
                ("Socket", [("ListenStream", f"0.0.0.0:{r['rigctl']}")]),
                ("Install", [("WantedBy", dev)])])))
        for name, lines in (r.get("dropins") or {}).items():
            body = HEADER.format(host=host) + "[Service]\n" + "".join(l + "\n" for l in lines)
            arts.append(Artifact(rid, f"{UNIT_DIR}/{svc}.d/{name}", "systemd", body))

    rules = wireplumber_rules(h)
    if rules:
        arts.append(Artifact("audio", WIREPLUMBER_CONF, "text", rules))

    st = h["station"]
    argv = [py, f"{repo}/station.py", "--config", f"%h/{STATION_JSON}"]
    if st.get("https"):
        argv += ["--https", "--tls-name", "%l"]
    if st.get("rigctl_port") is not None:
        argv += ["--rigctl-port", str(st["rigctl_port"])]
    arts.append(Artifact("station", f"{UNIT_DIR}/station.service", "systemd", unit(host, [
        ("Unit", [("Description", "Ranch Radio station: one page (7300), the shared rigctl port (4532), "
                                  "Winlink and Mesh"),
                  ("After", "network-online.target sound.target pipewire.service"),
                  ("Wants", "pipewire.service")]),
        ("Service", [("ExecStart", " ".join(argv)), ("Restart", "on-failure"), ("RestartSec", "5"),
                     ("TimeoutStopSec", "15")]),
        ("Install", [("WantedBy", "default.target")])])))
    arts.append(Artifact("station", STATION_JSON, "json", station_json(h)))

    p = h.get("packet")
    if p and p.get("tnc"):
        argv = hub_argv(h, py, repo)
        tnc_val = p.get("tnc", "")
        is_dw = tnc_val == "direwolf"
        is_tcp = is_dw or (isinstance(tnc_val, str) and tnc_val.startswith("tcp:"))
        desc = ("Packet hub (APRS + Winlink, Direwolf on a USB sound card cable)" if is_dw else
                "Packet hub (APRS + Winlink over network KISS TNC)" if is_tcp else
                "UV-PRO packet hub (APRS + Winlink over the radio's Bluetooth KISS TNC)")
        after = (DIREWOLF_SERVICE if is_dw else "network-online.target") if is_tcp \
            else "bluetooth.target network-online.target"
        wants = (DIREWOLF_SERVICE if is_dw else "network-online.target") if is_tcp else "bluetooth.target"
        if is_dw:
            arts.append(Artifact("packet", f"{UNIT_DIR}/{DIREWOLF_SERVICE}", "systemd", unit(host, [
                ("Unit", [("Description", "Direwolf: the packet hub's modem on the USB sound card cable "
                                          "(no network ports; waits for the cable)")]),
                ("Service", [("ExecStart", exec_line(direwolf_argv(h, py, repo, _unit_path))),
                             ("Restart", "always"), ("RestartSec", "10"), ("TimeoutStopSec", "15")]),
                ("Install", [("WantedBy", "default.target")])])))
        svc = [("ExecStart", exec_line(argv)), ("Restart", "on-failure"), ("RestartSec", "5"),
               ("TimeoutStopSec", "10")]
        if not is_tcp:
            # Keep the Bluetooth link to the radio across restarts and
            # stops: the UV-PRO refuses a new KISS link until it is
            # power-cycled (2026-10-19). The hub parks the socket here.
            svc += [("FileDescriptorStoreMax", "1"), ("FileDescriptorStorePreserve", "yes")]
        svc.append(("NotifyAccess", "main"))
        unit_name = "uvpro-hub.service" if (isinstance(tnc_val, str) and tnc_val.startswith("bt:")) else "packet-hub.service"
        arts.append(Artifact("packet", f"{UNIT_DIR}/{unit_name}", "systemd", unit(host, [
            ("Unit", [("Description", desc), ("After", after), ("Wants", wants)]),
            ("Service", svc),
            ("Install", [("WantedBy", "default.target")])])))
        ph = p.get("pat_http")
        if ph:
            arts.append(Artifact("packet", f"{UNIT_DIR}/pat-http.service", "systemd", unit(host, [
                ("Unit", [("Description", f"Pat Winlink web UI (http://{ph['addr']}), via the packet hub"),
                          ("After", unit_name), ("Wants", unit_name)]),
                ("Service", [("ExecStart", f"{_unit_path(ph['pat'])} http --addr {ph['addr']}"),
                             ("Restart", "on-failure"), ("RestartSec", "5")]),
                ("Install", [("WantedBy", "default.target")])])))

    u = updates(h)
    if u:
        what = "check for updates and apply them" if u["apply"] else "check for updates and say so"
        arts.append(Artifact("updates", f"{UNIT_DIR}/{UPDATE_SERVICE}", "systemd", unit(host, [
            ("Unit", [("Description", f"Ranch Radio: {what} (ranch-radio update --scheduled)")]),
            # oneshot, no [Install]: only the timer starts it. RANCH_RADIO_UPDATE_JOB:
            # what it runs (a rollback's older code too) never restarts it.
            ("Service", [("Type", "oneshot"), ("Environment", "RANCH_RADIO_UPDATE_JOB=1"),
                         ("ExecStart", exec_line([py, f"{repo}/ranch-radio", "update", host, "--scheduled"]))])])))
        arts.append(Artifact("updates", f"{UNIT_DIR}/{UPDATE_TIMER}", "systemd", unit(host, [
            ("Unit", [("Description", f"Ranch Radio: {what}, daily at {u['at']}")]),
            ("Timer", [("OnCalendar", f"*-*-* {u['at']}:00"), ("Persistent", "true")]),
            ("Install", [("WantedBy", "timers.target")])])))
    return arts


def wait_audio(r):
    """ExecStartPre: wait up to 20 s for the radio's sound card in PipeWire.

    The unit starts when the serial port appears; the sound card on the same
    adapter can come a second later, and the mixer steps and ardopcf then
    miss it (linux-station, 2026-10-17). "-": a missing card never blocks the radio.
    """
    return ("-/bin/sh -c 'for i in $$(seq 40); do "
            f"pactl get-sink-volume {r['audio_out']} >/dev/null 2>&1 && "
            f"pactl get-source-volume {r['audio_in']} >/dev/null 2>&1 && exit 0; "
            "sleep 0.5; done; echo sound card not in PipeWire after 20 s >&2'")


def wireplumber_rules(h):
    """WirePlumber rules that pin each radio sound card's node names.

    Replugged quickly, a card can come back as "<name>.2" while WirePlumber
    still holds the old name; the radio server, ardopcf and remote-audio
    tunnels name the card, so they then miss it (G90, linux-station, 2026-10-17).
    Rules apply after WirePlumber's renaming, so matching the name with any
    suffix and setting it back undoes it. The low priority keeps the card
    from becoming the desktop default, where desktop sounds would go on air.
    """
    rules = []
    for r in h["radios"]:
        if not (r.get("audio_in") and r.get("audio_out")):
            continue
        for what, name in (("transmit", r["audio_out"]), ("receive", r["audio_in"])):
            rules.append(
                f"  # {r['name']} {what} audio\n"
                "  {\n"
                f'    matches = [ {{ node.name = "~{name}*" }} ]\n'
                "    actions = {\n"
                "      update-props = {\n"
                f'        node.name = "{name}"\n'
                f"        priority.session = {RADIO_AUDIO_PRIORITY}\n"
                "      }\n"
                "    }\n"
                "  }\n")
    if not rules:
        return None
    return (HEADER.format(host=h["host"])
            + "# WirePlumber reads this at start: systemctl --user restart wireplumber.\n"
            + "monitor.alsa.rules = [\n" + "".join(rules) + "]\n")


def station_json(h):
    radios = []
    for r in h["radios"]:
        e = {"id": r["id"], "name": r["name"], "device": r["device"], "unit": f"{r['id']}-remote.service"}
        for k in ("web", "rigctl", "lane", "audio_in", "audio_out", "jtdx_profile"):
            if k in r:
                e[k] = r[k]
        radios.append(e)
    d = {"name": h["host"], "radios": radios}
    if h.get("packet"):
        d["packet"] = {k: h["packet"][k] for k in ("url", "agwpe", "name", "title") if k in h["packet"]}
        tnc = h["packet"].get("tnc")
        if isinstance(tnc, str) and (tnc.startswith("tcp:") or tnc == "direwolf") and "name" not in d["packet"]:
            d["packet"]["name"] = "KISS TNC"      # not the UV-PRO, which the page assumes when unnamed
    if h.get("winlink"):
        d["winlink"] = dict(h["winlink"])
    if h.get("mesh"):
        d["mesh"] = dict(h["mesh"])
    if h.get("addons"):
        d["addons"] = h["addons"]
    d["host_file"] = h["host"]                  # the station page's Modules tab
    return d


# ---------------------------------------------------------------- launchd

def _macos(h, home):
    pth = lambda p: _abs(p, home)  # noqa: E731
    repo, py = pth(h["repo"]), pth(h["python"])
    arts = []
    for r in h["radios"]:
        log = pth(r.get("log", h["repo"] + "/server.log"))
        d = {"KeepAlive": True, "Label": r["label"], "ProcessType": "Interactive",
             "ProgramArguments": [py, f"{repo}/server.py"] + server_args(h, r, pth),
             "RunAtLoad": True, "StandardErrorPath": log, "StandardOutPath": log,
             "WorkingDirectory": repo}
        if r.get("path"):
            d["EnvironmentVariables"] = {"PATH": r["path"]}
        arts.append(Artifact(r["id"], f"{AGENTS}/{r['label']}.plist", "plist", d))

    adds = mac_addons(h)
    if adds:
        arts.append(Artifact(",".join(adds), MAC_ADDONS, "json", adds))

    w = h.get("winlink") or {}
    a = w.get("ardopcf")
    if isinstance(a, dict):
        logs = pth(w["ardop_logs"])
        arts.append(Artifact("winlink", f"{AGENTS}/{a['label']}.plist", "plist", {
            "KeepAlive": True, "Label": a["label"], "ProcessType": "Interactive",
            "ProgramArguments": [pth(a["bin"]), "--logdir", logs, "--hostcommands", a["hostcommands"],
                                 str(a["port"]), a["audio_in"], a["audio_out"]],
            "RunAtLoad": True, "StandardErrorPath": f"{logs}/console.log",
            "StandardOutPath": f"{logs}/console.log"}))

    wd = (h.get("integrations") or {}).get("macos-widget")
    if wd:
        app = pth(wd["app"])
        name = os.path.splitext(os.path.basename(app))[0]
        arts.append(Artifact("macos-widget", f"{AGENTS}/{wd['label']}.plist", "plist", {
            "Label": wd["label"], "LimitLoadToSessionType": "Aqua", "ProcessType": "Interactive",
            # The radio agent it switches: its label, from this host file.
            "ProgramArguments": [f"{app}/Contents/MacOS/{name}"] + [r["label"] for r in h["radios"][:1]],
            "RunAtLoad": True}))

    p = h.get("packet")
    if p and p.get("tnc"):
        env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
        hub_log = pth("~/Library/Logs/ranch-radio-packet-hub.log")
        arts.append(Artifact("packet", f"{AGENTS}/{p.get('label', PACKET_LABEL)}.plist", "plist", {
            "Label": p.get("label", PACKET_LABEL), "KeepAlive": True, "RunAtLoad": True,
            "ProcessType": "Interactive", "ProgramArguments": hub_argv(h, py, repo),
            "WorkingDirectory": repo, "EnvironmentVariables": env, "ThrottleInterval": 10,
            "StandardOutPath": hub_log, "StandardErrorPath": hub_log}))
        if p["tnc"] == "direwolf":
            d = p["direwolf"]
            dw_log = pth("~/Library/Logs/ranch-radio-direwolf.log")
            arts.append(Artifact("packet", f"{AGENTS}/{d.get('label', DIREWOLF_LABEL)}.plist", "plist", {
                "Label": d.get("label", DIREWOLF_LABEL), "KeepAlive": True, "RunAtLoad": True,
                "ProcessType": "Interactive", "ProgramArguments": direwolf_argv(h, py, repo, pth),
                "WorkingDirectory": repo, "EnvironmentVariables": env, "ThrottleInterval": 10,
                "StandardOutPath": dw_log, "StandardErrorPath": dw_log}))

    u = updates(h)
    if u:
        hh, mm = (int(x) for x in u["at"].split(":"))
        log = pth(UPDATE_LOG)
        # No RunAtLoad: loading it (install, update) never starts an update.
        arts.append(Artifact("updates", f"{AGENTS}/{u['label']}.plist", "plist", {
            "Label": u["label"], "ProcessType": "Background",
            "ProgramArguments": [py, f"{repo}/ranch-radio", "update", h["host"], "--scheduled"],
            "StartCalendarInterval": {"Hour": hh, "Minute": mm},
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                                     "RANCH_RADIO_UPDATE_JOB": "1"},
            "StandardErrorPath": log, "StandardOutPath": log}))
    return arts


def artifacts(h, home=None):
    validate(h)
    if h["os"] == "linux":
        return _linux(h)
    return _macos(h, home or os.path.expanduser("~"))


# ---------------------------------------------------------------- comparing

def same(art, installed_bytes):
    """Does the installed file mean what the artifact means?"""
    try:
        if art.fmt == "plist":
            return plistlib.loads(installed_bytes) == art.content
        if art.fmt == "json":
            return json.loads(installed_bytes) == art.content
        if art.fmt == "text":
            return installed_bytes.decode() == art.content
        return parse_unit(installed_bytes.decode()) == parse_unit(art.content)
    except Exception:
        return False


def installed_states(h, home):
    """(artifact, installed path, state) for each file h renders, against home:
    same | differs | missing | linked."""
    from pathlib import Path
    for art in artifacts(h, home=str(home)):
        p = Path(home) / art.path
        if p.is_symlink():
            # A unit linked into a checkout (systemctl --user link) reads the
            # same as the render whenever that checkout is on this branch, so
            # "same" would compare the repo with itself (linux-station, 2026-10-16).
            # It is not installed: a pull or a branch switch rewrites it live.
            yield art, p, "linked"
        elif not p.exists():
            yield art, p, "missing"
        else:
            try:
                yield art, p, "same" if same(art, p.read_bytes()) else "differs"
            except OSError:
                yield art, p, "differs"
