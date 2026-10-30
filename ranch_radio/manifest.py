"""What each switchable module needs, in a form a program can check.

catalog.py says what a module is and where it ships on the operator's hosts (prose).
This file says what it takes to turn one on anywhere: where its block lives in
a host file, a starting block, the fields only the operator can supply, the
modules it leans on, and the things outside Python it needs on the machine.
`ranch-radio add`, `remove` and `doctor` read it; nothing else does.

Recipes ("auto" on a need): a shell command `doctor --fix` may run as you,
never as root: a venv, a pip install, the ardop build, brew. {repo}, {path}
(the need's resolved path), {dir} (its directory), {python} / {venv} (the
need's python and its venv) and {host_python} are filled in. A need with no
recipe, or one needing root, is only printed.

Per module:
    label     its short name on the page (the catalog's title is the long one)
    family    "addon": the page's process runs it (Addons.items: on/off/broken);
              "files": a service or desktop integration, known by its installed files
    block     where it lives in the host file: ("winlink",) or ("addons", "uvpro")
    os        the host OSes the renderer ships it on
    template  {os: block}; ASK marks a field the operator must give (--set)
    ask       {field: what to give}, shown when a field is missing
    with      {module: extra fields} merged in when that module is on the host
    requires  modules that must be on the host first
    radios    {field: "free_port"}: each radio gets one if it has none
    needs     machine checks, each one of:
                {"bin": NAME, "key": FIELD, "default": PATH}   an executable
                {"pymod": NAME, "key": FIELD, "default": PY}   importable by that python
                {"file": PATH} / {"files": FIELD}              present on disk
              plus "fix" (how to get it), "os" (only there), "level": "warn"
              (doesn't stop the module, only worth knowing)
"""

import re

ASK = "<required>"

# What the page may send for an ASK field (review 2026-10-21, Grok G1/G5):
# each rule is a type and, for strings, a full-match pattern. The page may set
# ASK fields only; everything else in a block comes from its template.
_CALL = r"[A-Z0-9]{3,7}(-[0-9]{1,2})?"
RULES = {
    "callsign": (str, re.compile(_CALL)),
    "callsign_list": (list, re.compile(_CALL)),
    "bt_addr": (str, re.compile(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}")),
    "tnc": (str, re.compile(r"bt:[0-9A-F]{2}(:[0-9A-F]{2}){5}|/dev/[A-Za-z0-9._-]+|tcp:[A-Za-z0-9.-]+:[0-9]{1,5}"
                            r"|direwolf")),
    "serial_or_ble": (str, re.compile(r"(/dev/[A-Za-z0-9._-]+|ble:[A-Za-z0-9_-]{1,40})"
                                      r"(,(/dev/[A-Za-z0-9._-]+|ble:[A-Za-z0-9_-]{1,40})){0,3}")),
    "audio_name": (str, re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{0,63}")),
}


def check_ask(rule, value):
    """None if value fits the rule, else why not. Callsigns and addresses are
    upper-cased by the caller first (normalize)."""
    typ, pat = RULES[rule]
    if typ is list:
        if not isinstance(value, list) or not 1 <= len(value) <= 8:
            return "a list of 1 to 8 entries"
        bad = [v for v in value if not (isinstance(v, str) and pat.fullmatch(v))]
        return f"not allowed: {', '.join(map(repr, bad))}" if bad else None
    if not isinstance(value, str) or not pat.fullmatch(value):
        return f"{value!r} doesn't fit ({rule.replace('_', ' ')})"
    return None


def normalize(rule, value):
    if rule in ("callsign", "bt_addr") and isinstance(value, str):
        return value.strip().upper()
    if rule == "callsign_list":
        if isinstance(value, str):
            value = [v for v in re.split(r"[,\s]+", value) if v]
        if isinstance(value, list):
            return [v.strip().upper() if isinstance(v, str) else v for v in value]
    if rule == "tnc" and isinstance(value, str) and value.lower().startswith("bt:"):
        return "bt:" + value[3:].upper()
    return value.strip() if isinstance(value, str) else value

PAT_FIX = "install Pat (https://getpat.io) and set its path with --set pat=PATH"
ARDOP_FIX = "build it from this repo: ardop/build.sh (see ardop/README.md)"

MANIFESTS = {
    "winlink": {
        "label": "Winlink (HF mail over ARDOP)",
        "family": "addon",
        "block": ("winlink",),
        "os": ["macos", "linux"],
        "template": {
            "linux": {"pat": "~/.local/bin/pat", "pat_port": 8095, "lane_port": 4539,
                      "ardopcf": "~/.local/bin/ardopcf", "ardop_port": 8515,
                      "ardop_logs": "~/.local/state/radio-remote/ardop"},
            # A Mac's server runs Pat itself; ardopcf gets its own launchd agent.
            "macos": {"pat": "pat", "ardop_logs": "~/Library/Logs/ardopcf",
                      "ardopcf": {"label": "ranch-radio.ardopcf", "bin": "~/bin/ardopcf",
                                  "port": 8515, "audio_in": ASK, "audio_out": ASK,
                                  "hostcommands": "CONSOLELOG 2"}},
        },
        "rules": {"ardopcf.audio_in": "audio_name", "ardopcf.audio_out": "audio_name"},
        "ask": {"ardopcf.audio_in": "the radio's sound card name for ardopcf to listen on, e.g. \"USB Audio CODEC\"",
                "ardopcf.audio_out": "the radio's sound card name for ardopcf to play into"},
        # The transmit lane is core (server.py): each radio needs its port.
        "radios": {"lane": "free_port"},
        "needs": [
            {"bin": "pat", "key": "pat", "default": "pat", "fix": PAT_FIX},
            {"bin": "ardopcf", "key": "ardopcf", "default": "~/.local/bin/ardopcf", "fix": ARDOP_FIX,
             "auto": {"macos": "(brew list portaudio >/dev/null 2>&1 || brew install portaudio) && "
                               "bash {repo}/ardop/build.sh && mkdir -p {dir} && install -m 755 {repo}/ardop/build/ardopcf {path}",
                      "linux": "bash {repo}/ardop/build.sh && mkdir -p {dir} && install -m 755 {repo}/ardop/build/ardopcf {path}"}},
        ],
    },
    "mesh": {
        "label": "Mesh (Meshtastic node)",
        "family": "addon",
        "block": ("mesh",),
        "os": ["macos", "linux"],
        "template": {"any": {"port": ASK, "keys": []}},
        "rules": {"port": "serial_or_ble"},
        "ask": {"port": "the node: a serial device (/dev/meshtastic) or ble:NODE_NAME; "
                        "several, comma-separated, are tried in order"},
        "needs": [
            {"pymod": "meshtastic", "key": "python", "default": "~/.local/share/venvs/meshtastic/bin/python",
             "auto": "{host_python} -m venv {venv} && {venv}/bin/pip install -q meshtastic",
             "fix": "python3 -m venv ~/.local/share/venvs/meshtastic && "
                    "~/.local/share/venvs/meshtastic/bin/pip install meshtastic"},
            {"files": "keys", "level": "warn",
             "fix": "a channel key file is missing or unreadable (a locked vault reads as missing)"},
        ],
    },
    "packet": {
        "label": "Packet hub (APRS, Winlink: UV-PRO, network TNC, or Direwolf on a USB cable)",
        "family": "files",
        "block": ("packet",),
        "os": ["linux", "macos"],
        # Transmit stays off until the operator turns it on (--set tx=true).
        "template": {os_name: {"url": "http://127.0.0.1:7310", "agwpe": "127.0.0.1:8000",
                               "tnc": ASK, "mycall": ASK, "tx": False} for os_name in ("linux", "macos")},
        "rules": {"tnc": "tnc", "mycall": "callsign"},
        "ask": {"tnc": "the radio's KISS TNC, e.g. tcp:direwolf.localdomain:8001, bt:00:11:22:33:44:55, "
                       "or direwolf (with a packet.direwolf block: a radio on a USB sound card cable)",
                "mycall": "your callsign"},
        "needs": [{"bin": "bluetoothctl", "default": "bluetoothctl", "level": "warn", "tnc": "bt:",
                   "fix": "install BlueZ (needed for a Bluetooth TNC)"},
                  {"bin": "direwolf", "key": "direwolf", "default": "direwolf", "tnc": "direwolf",
                   "fix": "install Direwolf (brew install direwolf, or your package manager)"}],
    },
    "uvpro": {
        "label": "UV-PRO control",
        "family": "addon",
        "block": ("addons", "uvpro"),
        "os": ["linux"],
        "template": {"linux": {"enabled": True, "address": ASK, "calls": ASK}},
        "rules": {"address": "bt_addr", "calls": "callsign_list"},
        "ask": {"address": "the UV-PRO's Bluetooth address",
                "calls": "the calls its APRS beacon may use, as JSON: '[\"N0CALL\"]'"},
        # With the packet hub on the host, borrow the radio from it instead of
        # fighting it for the one Bluetooth link.
        "with": {"packet": {"hub": "http://127.0.0.1:7310", "control": "tcp:127.0.0.1:8002"}},
        "needs": [{"pymod": "benlink", "key": "python", "default": "~/.local/share/venvs/benlink/bin/python",
                   "auto": "{host_python} -m venv {venv} && {venv}/bin/pip install -q "
                           "git+https://github.com/khusmann/benlink@c4b9d22112402ea193d376f412bc028d1778fdb4",
                   "fix": "python3 -m venv ~/.local/share/venvs/benlink && "
                          "~/.local/share/venvs/benlink/bin/pip install benlink"}],
    },
    "opengd77": {
        "label": "MD-UV390 (OpenGD77)",
        "family": "addon",
        "block": ("addons", "opengd77"),
        "os": ["macos", "linux"],
        "template": {"macos": {"enabled": True, "port": "auto", "label": "MD-UV390", "poll": 2,
                               "dmrconf": "/opt/homebrew/bin/dmrconf"},
                     "linux": {"enabled": True, "port": "auto", "label": "MD-UV390", "poll": 2,
                               "dmrconf": "/usr/bin/dmrconf"}},
        "needs": [
            {"bin": "dmrconf", "key": "dmrconf", "default": "dmrconf", "auto": {"macos": "brew install qdmr"},
             "fix": "install qdmr (macOS: brew install qdmr; Debian/Ubuntu: apt install qdmr)"},
            {"file": "/etc/udev/rules.d/99-opengd77.rules", "os": "linux", "level": "warn",
             "fix": "sudo cp linux/udev/99-opengd77.rules /etc/udev/rules.d/ && sudo udevadm control --reload"},
        ],
    },
    "propagation": {
        "label": "Propagation (space weather, bands heard)",
        "family": "addon",
        "block": ("addons", "propagation"),
        "os": ["macos", "linux"],
        # NOAA needs nothing; a skywatch InfluxDB is optional ("skywatch": {"url", "db"}).
        "template": {"macos": {"enabled": True}, "linux": {"enabled": True}},
        "needs": [],
    },
    "macos-widget": {
        "label": "Menu bar widget",
        "family": "files",
        "block": ("integrations", "macos-widget"),
        "os": ["macos"],
        "template": {"macos": {"label": "ranch-radio.widget", "app": "~/Applications/ic7300-widget.app"}},
        "needs": [{"file": "~/Applications/ic7300-widget.app", "key": "app",
                   "fix": "build it: macos/widget/build.sh"}],
    },
    "omarchy-bar": {
        "label": "Omarchy bar",
        "family": "files",
        "block": ("integrations", "omarchy-bar"),
        "os": ["linux"],
        "template": {"linux": {}},
        "needs": [],
    },
}

# What every host needs, whatever it runs (doctor checks these first).
CORE = {
    "python": (3, 10),
    "pymods": ["aiohttp"],
    "audio": {"linux": [{"bin": "arecord", "default": "arecord",
                         "fix": "install alsa-utils (sudo pacman -S alsa-utils, or sudo apt install alsa-utils)"},
                        {"bin": "aplay", "default": "aplay",
                         "fix": "install alsa-utils (sudo pacman -S alsa-utils, or sudo apt install alsa-utils)"}],
              "macos": [{"pymod": "sounddevice", "fix": "pip install sounddevice (into the host's python)",
                         "auto": "{host_python} -m pip install -q sounddevice"}]},
    "https": {"bin": "openssl", "default": "openssl", "fix": "install openssl"},
}


def template(name, os_name, present=()):
    """A starting block for module name on os_name, with extras for modules present."""
    m = MANIFESTS[name]
    t = m["template"].get(os_name, m["template"].get("any"))
    if t is None:
        return None
    block = _copy(t)
    for other, extra in (m.get("with") or {}).items():
        if other in present:
            block.update(_copy(extra))
    return block


def _copy(v):
    if isinstance(v, dict):
        return {k: _copy(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_copy(x) for x in v]
    return v


def fields(block, prefix=""):
    """Every settable (leaf) field of a block, dotted."""
    out = []
    for k, v in block.items():
        out += fields(v, f"{prefix}{k}.") if isinstance(v, dict) else [prefix + k]
    return out


def asked(block, prefix=""):
    """Dotted names of the fields in block still set to ASK."""
    out = []
    for k, v in block.items():
        if isinstance(v, dict):
            out += asked(v, f"{prefix}{k}.")
        elif v == ASK:
            out.append(prefix + k)
    return out
