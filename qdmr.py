"""qdmr's dmrconf: read and write an OpenGD77 radio's codeplug as qdmr YAML.

dmrconf is qdmr's command-line tool (DM3MAT, github.com/hmatuschek/qdmr).
On mac-shack: /opt/homebrew/bin/dmrconf, qdmr 0.15.1 from Homebrew (installed
2026-10-17). MacPorts' 0.11.3 at /opt/local/bin is too old (below), and
MacPorts there needs `sudo port migrate` after the macOS upgrade. The codeplug is a
qdmr YAML file, and it stays the source of truth: the page shows its
channels and edits the file, and dmrconf checks it before anything is
written.

Which dmrconf is safe for an MD-UV380 / MD-UV390 on OpenGD77: one that lists
the radio key "openuv380" (qdmr lib/openuv380.cc; current master, 2026-09).
Older dmrconf (0.11.3, what MacPorts installed here) knows only the GD-77
flavour of OpenGD77 and writes with 'W', which the MD-UV380 firmware answers
with a fake acknowledgement and stores nothing (usb_com.c handleCPSRequest:
'W' is "Fake write", 'X' is the real one; qdmr master picks 'X' for every
non-GD-77 radio, opengd77_interface.cc). So `writable()` is the gate.
"""

import os
import re
import subprocess

import qdmr_yaml

RADIO_KEY = "openuv380"
READ_TIMEOUT_S = 180
WRITE_TIMEOUT_S = 300
OUT_CAP = 8000                    # bytes of dmrconf output kept for the page


class QdmrError(Exception):
    pass


class Dmrconf:
    def __init__(self, path="/opt/homebrew/bin/dmrconf"):
        self.path = os.path.expanduser(path)

    def _run(self, args, timeout):
        try:
            p = subprocess.run([self.path] + args, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise QdmrError(f"dmrconf not found at {self.path}")
        except subprocess.TimeoutExpired:
            raise QdmrError(f"dmrconf {args[0]} took longer than {timeout} s")
        out = ((p.stdout or "") + (p.stderr or ""))[-OUT_CAP:]
        return p.returncode, out

    def version(self):
        rc, out = self._run(["--version"], 15)
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None

    def radios(self):
        rc, out = self._run(["--list-radios"], 15)
        return [m.group(1) for m in re.finditer(r"^\s*([a-z0-9-]+)\s*\|", out, re.MULTILINE) if m.group(1) != "key"]

    def writable(self):
        """None if this dmrconf can safely write an MD-UV380 on OpenGD77, else why not."""
        try:
            keys = self.radios()
        except QdmrError as e:
            return str(e)
        if RADIO_KEY not in keys:
            return (f"dmrconf {self.version() or '?'} has no '{RADIO_KEY}' driver: it would write with the GD-77 "
                    "command, which this radio acknowledges and ignores. Upgrade qdmr first.")
        return None

    def read(self, out_path, radio=RADIO_KEY, device=None):
        dev = ["--device", device] if device else []
        rc, out = self._run(["read", "--yaml", "--radio", radio] + dev + [out_path], READ_TIMEOUT_S)
        if rc != 0 or not os.path.exists(out_path):
            raise QdmrError(f"dmrconf read failed ({rc}): {out.strip()[-600:]}")
        return out

    def verify(self, yaml_path, radio=RADIO_KEY):
        """Offline check. Raises on a rejection. Returns "crashed" when
        dmrconf dies on a signal: dmrconf 0.15.1 `verify --radio openuv380`
        segfaults on nearly every codeplug (mac-shack, 2026-10-17: any channel
        but qdmr's one test frequency; the same files verify for uv390 and
        opengd77). The crash comes after the YAML is read, so a file it
        can't parse is still refused here, and `write` runs the same limits
        check itself against the connected radio before uploading
        (cli/writecodeplug.cc)."""
        rc, out = self._run(["verify", "--yaml", "--radio", radio, yaml_path], 60)
        if rc < 0:
            return "crashed"
        if rc != 0:
            raise QdmrError(f"dmrconf verify refused the codeplug ({rc}): {out.strip()[-1500:]}")
        return out

    def encode(self, yaml_path, out_path, radio=RADIO_KEY):
        """Offline: the binary image dmrconf would build from yaml_path (bytes).
        Unlike verify on 0.15.1 it doesn't crash, and it applies the radio's
        limits. Raises on a file it refuses."""
        rc, out = self._run(["encode", "--yaml", "--radio", radio, yaml_path, out_path], 60)
        if rc != 0 or not os.path.exists(out_path):
            raise QdmrError(f"dmrconf encode refused the codeplug ({rc}): {out.strip()[-1500:]}")
        with open(out_path, "rb") as f:
            return f.read()

    def write(self, yaml_path, radio=RADIO_KEY, device=None):
        dev = ["--device", device] if device else []
        rc, out = self._run(["write", "--yaml", "--radio", radio] + dev + [yaml_path], WRITE_TIMEOUT_S)
        if rc != 0:
            raise QdmrError(f"dmrconf write failed ({rc}): {out.strip()[-1500:]}")
        return out


def read_problems(out):
    """What dmrconf said it couldn't decode while reading, e.g. "While linking
    zone 'GMRS': 5-th channel index 32 out of bounds." A read with any of
    these is not faithful, and a write built on it would push the damage
    back (mac-shack, 2026-10-17: qdmr 0.15.1 renumbers a radio's sparse channel
    slots 1..N but links zones by the old slot numbers, so zones point at the
    wrong channels or past the end)."""
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    return [m.group(1).strip() for m in re.finditer(r"(?:Warning|ERROR)[^:]*:\s*(While linking[^\n]*|[^\n]*out of bounds[^\n]*)", out)]


# ---------------------------------------------------------------- channels

_KEY = re.compile(r"^(\s*)(-\s+)?([A-Za-z][A-Za-z0-9]*):\s*(.*?)\s*$")


def _scalar(v):
    v = v.strip()
    if v in ("~", "null", ""):
        return ""
    if v.startswith("!<!default>"):
        return ""
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _mhz(v):
    m = re.match(r"^([0-9.]+)\s*(MHz|kHz|Hz)?$", _scalar(v))
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or "MHz"
    return round(n * {"MHz": 1e6, "kHz": 1e3, "Hz": 1}[unit])


def _tone(v):
    v = _scalar(v)
    m = re.match(r"^\{\s*(ctcss|dcs)\s*:\s*([^}]+?)\s*\}$", v)
    if not m:
        return ""
    if m.group(1) == "ctcss":
        return "%.1f" % float(m.group(2).replace("Hz", "").strip())
    return "D" + m.group(2).strip().upper()


def _model_tone(v):
    if isinstance(v, dict) and len(v) == 1:
        (k, x), = v.items()
        if k == "ctcss" and isinstance(x, str):
            try:
                return "%.1f" % float(x.replace("Hz", "").strip())
            except ValueError:
                return ""
        if k == "dcs" and isinstance(x, str):
            m = re.match(r"^([niNI])?0*([0-7]+)$", x.strip())
            if m:                                   # qdmr "n315" / "i023" -> the radio's own D315N / D023I
                return "D%03d%s" % (int(m.group(2)), "I" if (m.group(1) or "n").lower() == "i" else "N")
            return "D" + x.strip().upper()
    return ""


def _model_text(v):
    if isinstance(v, qdmr_yaml.Tag):
        return v.tag
    return v if isinstance(v, str) else ""


def channels(text):
    """The channels of a qdmr YAML codeplug, for showing: [{kind, id, name,
    rx_hz, tx_hz, power, rx_tone, tx_tone, bandwidth, colour_code, timeslot}].
    Read with qdmr_yaml (block and flow tones alike); a file it can't read
    falls back to the line reader below. The file itself is never rebuilt
    from this."""
    try:
        model = qdmr_yaml.load(text)
    except qdmr_yaml.YamlError:
        model = None
    if isinstance(model, dict) and isinstance(model.get("channels"), list):
        out = []
        for item in model["channels"]:
            if not isinstance(item, dict) or len(item) != 1:
                continue
            (kind, c), = item.items()
            if not isinstance(c, dict):
                continue
            ch = {"kind": kind, "id": _model_text(c.get("id")), "name": _model_text(c.get("name")),
                  "rx_hz": _mhz(_model_text(c.get("rxFrequency"))), "tx_hz": _mhz(_model_text(c.get("txFrequency"))),
                  "power": _model_text(c.get("power")), "bandwidth": _model_text(c.get("bandwidth"))}
            if kind == "dmr":
                ch.update(colour_code=_model_text(c.get("colorCode")),
                          timeslot=_model_text(c.get("timeSlot")).replace("TS", ""))
            else:
                ch.update(rx_tone=_model_tone(c.get("rxTone")), tx_tone=_model_tone(c.get("txTone")))
            out.append({k: v for k, v in ch.items() if v not in (None,)})
        return out
    return _channels_by_line(text)


def _channels_by_line(text):
    out, cur, in_ch, item_indent = [], None, False, None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and not line.startswith("-"):
            in_ch = line.startswith("channels:")
            cur = None
            continue
        if not in_ch:
            continue
        m = _KEY.match(line)
        if not m:
            continue
        indent, dash, key, val = len(m.group(1)), m.group(2), m.group(3), m.group(4)
        if dash and val == "":                                  # "- fm:" / "- dmr:" starts a channel
            item_indent = indent
            cur = {"kind": key}
            out.append(cur)
            continue
        if cur is None or indent <= item_indent:
            continue
        if key in ("rxFrequency", "txFrequency"):
            cur["rx_hz" if key[0] == "r" else "tx_hz"] = _mhz(val)
        elif key in ("rxTone", "txTone"):
            cur["rx_tone" if key[0] == "r" else "tx_tone"] = _tone(val)
        elif key == "colorCode":
            cur["colour_code"] = _scalar(val)
        elif key == "timeSlot":
            cur["timeslot"] = _scalar(val).replace("TS", "")
        elif key in ("id", "name", "power", "bandwidth"):
            cur[key] = _scalar(val)
    return out


def same_channels(a, b):
    """Do two channel lists agree on what goes on the air (name, RX, TX, mode)?
    Returns (True, None) or (False, first difference)."""
    key = lambda c: (c.get("name"), c.get("rx_hz"), c.get("tx_hz"), c.get("kind"))  # noqa: E731
    if len(a) != len(b):
        return False, f"{len(a)} channels written, {len(b)} read back"
    for x, y in zip(a, b):
        if key(x) != key(y):
            return False, f"{x.get('name')!r}: wrote {key(x)}, read back {key(y)}"
    return True, None


# ---------------------------------------------------------------- zones
#
# qdmr 0.15.1 numbers a radio's channels 1..N in slot order but reads zone
# members as slot numbers (mac-shack, 2026-10-17: the MD-UV390's slots
# 1-8,12,22-43 came out as ch1..ch31, zone 'AZ SV DMR' kept slot 12 as
# "ch12", which is another channel, and every member above slot 31 was
# dropped). Writing is not affected: dmrconf write numbers channels 1..N,
# switches every other slot off, and rebuilds the zone table from the file
# (openuv380_codeplug.cc encodeChannels, ZoneBankElement::encode; the raw
# image it reads first is never decoded). So the fix is the file: zones are
# rebuilt from the radio's own zone table (opengd77.OpenGD77.layout), and a
# write onto sparse slots is let through only when the file's zones point
# where the radio's do. One such write compacts the radio for good.

_ZONE_KEY = re.compile(r"^(\s*)(-\s+)?(id|name|A|B):\s*(.*?)\s*$")


def _ids(v):
    m = re.match(r"^\[(.*)\]$", v.strip())
    if not m:
        raise QdmrError(f"zone members not in [a, b] form: {v!r}")
    return [_scalar(x) for x in m.group(1).split(",") if x.strip()]


def zones(text):
    """The zones of a qdmr YAML codeplug: [{"id", "name", "A", "B", "lines":
    {key: line index}}]. Only the block form dmrconf writes; anything else
    there raises, so nothing is rebuilt from a zone it can't read."""
    out, cur, in_z = [], None, False
    for i, line in enumerate(text.splitlines()):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and not line.startswith("-"):
            in_z = line.startswith("zones:")
            continue
        if not in_z:
            continue
        m = _ZONE_KEY.match(line)
        if not m:
            raise QdmrError(f"zone line not understood: {line.strip()!r}")
        key, val = m.group(3), m.group(4)
        if m.group(2):
            cur = {"A": [], "B": [], "lines": {}}
            out.append(cur)
        if cur is None:
            raise QdmrError(f"zone line outside a zone: {line.strip()!r}")
        cur[key] = _ids(val) if key in ("A", "B") else _scalar(val)
        cur["lines"][key] = i
    return out


def _positions(text):
    return {c.get("id"): i for i, c in enumerate(channels(text))}


def zones_match(text, layout):
    """None if the file's zones point at the channels the radio's zones do,
    counting channels by position (the radio's k-th slot in use is the
    file's k-th channel); otherwise why not."""
    rank = {s: i for i, s in enumerate(layout["slots"])}
    pos = _positions(text)
    mine, theirs = zones(text), layout["zones"]
    if [z.get("name") for z in mine] != [z["name"] for z in theirs]:
        return (f"the file's zones are {[z.get('name') for z in mine]}, "
                f"the radio's are {[z['name'] for z in theirs]}")
    for z, r in zip(mine, theirs):
        got = [pos.get(c) for c in z["A"] + z["B"]]
        want = [rank.get(s) for s in r["slots"]]
        if got != want:
            return f"zone {z.get('name')!r} points at other channels than the radio's"
    return None


def rebuild_zones(text, layout):
    """The file with every zone's members set from the radio's own zone
    table. Refuses unless the file's channels are the radio's slots in order
    (same count, same names)."""
    chans, slots = channels(text), layout["slots"]
    if len(chans) != len(slots):
        raise QdmrError(f"the file has {len(chans)} channels, the radio {len(slots)} slots in use")
    by_slot = {}
    for s, c in zip(slots, chans):
        if (c.get("name") or "").strip() != layout["names"].get(s, "").strip():
            raise QdmrError(f"channel {c.get('id')} is {c.get('name')!r} in the file, "
                            f"slot {s} holds {layout['names'].get(s)!r}")
        by_slot[s] = c["id"]
    mine, theirs = zones(text), layout["zones"]
    if [z.get("name") for z in mine] != [z["name"] for z in theirs]:
        raise QdmrError(f"the file's zones are {[z.get('name') for z in mine]}, "
                        f"the radio's are {[z['name'] for z in theirs]}")
    lines = text.splitlines(keepends=True)
    for z, r in zip(mine, theirs):
        missing = [s for s in r["slots"] if s not in by_slot]
        if missing:
            raise QdmrError(f"zone {z['name']!r} lists slots the radio has no channel in: {missing}")
        if "A" not in z["lines"]:
            raise QdmrError(f"zone {z['name']!r} has no A list to set")
        i = z["lines"]["A"]
        indent = re.match(r"^(\s*(?:-\s+)?)", lines[i]).group(1)
        lines[i] = f"{indent}A: [{', '.join(by_slot[s] for s in r['slots'])}]\n"
        if "B" in z["lines"]:
            j = z["lines"]["B"]
            lines[j] = re.match(r"^(\s*(?:-\s+)?)", lines[j]).group(1) + "B: []\n"
    out = "".join(lines)
    if zones_match(out, layout):
        raise QdmrError("rebuilt zones still don't match the radio")
    return out


def compacted(text, layout):
    """None if the radio's own memory now holds the file as written: channels
    in slots 1..N with the file's names, and the file's zones by those
    numbers; otherwise the first difference."""
    chans = channels(text)
    if layout["slots"] != list(range(1, len(chans) + 1)):
        return f"the radio's channels sit in slots {layout['slots'][:8]}..., not 1-{len(chans)}"
    for i, c in enumerate(chans, 1):
        if (c.get("name") or "").strip() != layout["names"].get(i, "").strip():
            return f"slot {i} holds {layout['names'].get(i)!r}, the file has {c.get('name')!r}"
    zero = [layout["names"].get(n) for n, i in sorted((layout.get("dmr_id") or {}).items()) if i == 0]
    if zero:
        return f"{len(zero)} channels override the DMR ID with 0 (e.g. {zero[0]!r})"
    pos = _positions(text)
    mine, theirs = zones(text), layout["zones"]
    if [z.get("name") for z in mine] != [z["name"] for z in theirs]:
        return f"the radio's zones are {[z['name'] for z in theirs]}"
    for z, r in zip(mine, theirs):
        if [pos.get(c, -1) + 1 for c in z["A"] + z["B"]] != r["slots"]:
            return f"zone {z.get('name')!r} holds slots {r['slots']}"
    return None


APRS_PHANTOM = "APRS Channel"


def drop_aprs_phantom(text, n_slots):
    """The file without channels qdmr made up. When an OpenGD77 radio's APRS
    settings name a frequency no channel has, qdmr adds an FM channel 'APRS
    Channel' after the real ones and points the APRS revert at it
    (opengd77base_codeplug.cc APRSSettingsElement::link). Written back, it
    becomes a real channel (mac-shack, 2026-10-17: 127 MHz, an unset APRS
    frequency, outside the radio's bands). Channels past the radio's slot
    count are dropped only if each is that phantom, and with them the revert
    lines naming them (the radio's APRS frequency is then written as none);
    anything else there raises. Returns (text, dropped names)."""
    chans = channels(text)
    extra = chans[n_slots:]
    if not extra:
        return text, []
    odd = [c.get("name") for c in extra if c.get("name") != APRS_PHANTOM or c.get("kind") != "fm"]
    if odd:
        raise QdmrError(f"the file has channels the radio doesn't: {odd}")
    ids = {c["id"] for c in extra}
    lines = text.splitlines(keepends=True)
    keep, in_ch, skip = [], False, False
    for line in lines:
        top = line.strip() and not line.startswith(" ") and not line.startswith("-")
        if top:
            in_ch, skip = line.startswith("channels:"), False
        elif in_ch and re.match(r"^\s*-\s+\w+:\s*$", line):
            skip = False                                  # a channel starts; is it one to drop?
            start = len(keep)
        elif in_ch and skip is False and re.match(r"^\s*id:\s*(\S+)\s*$", line) \
                and _scalar(line.split(":", 1)[1]) in ids:
            del keep[start:]                              # drop its "- fm:" line too
            skip = True
            continue
        if skip:
            continue
        if re.match(r"^\s*revert:\s*(\S+)\s*$", line) and _scalar(line.split(":", 1)[1]) in ids:
            continue
        keep.append(line)
    out = "".join(keep)
    if len(channels(out)) != n_slots or any(re.search(rf"\b{re.escape(i)}\b", out) for i in ids):
        raise QdmrError("could not take qdmr's APRS channel out of the file cleanly")
    return out, [c.get("name") for c in extra]



# ---------------------------------------------------------------- what qdmr loses
#
# Two more round-trip losses, found in the raw diff after the gap-closing write
# (mac-shack, 2026-10-17):
#  - power: OpenGD77 stores a level 0-10 per channel (0 = the radio's master
#    power). qdmr reads 7, 8 and 9 all as "High" and writes High as 9, so the
#    MD-UV390's channels at 7 (3 W on its screen) came back at 9 (10 W). It can
#    write only 0, 1, 3, 6, 9 and 10 (ChannelElement::power / setPower).
#  - DMR ID: a channel with radioId "!default" is written with the per-channel
#    override switched on and ID 0 (ChannelElement::encode compares the channel's
#    default-ID stand-in with the settings' defaultID; they differ). Naming the
#    default ID itself writes the override off, as the radio had it.

QDMR_POWER_LEVELS = (0, 1, 3, 6, 9, 10)


def power_losses(layout):
    """Channels whose power level qdmr can't write back as it is:
    [(name, level)]."""
    return [(layout["names"].get(n), p) for n, p in sorted((layout.get("power") or {}).items())
            if p not in QDMR_POWER_LEVELS]


def pin_default_radio_id(text):
    """The file with every channel's radioId "!default" set to the settings'
    defaultID, so qdmr writes no DMR ID override. Returns (text, how many)."""
    m = re.search(r"^\s+defaultID:\s*(\S+)\s*$", text, re.MULTILINE)
    if not m:
        return text, 0
    rid = _scalar(m.group(1))
    out, n, in_ch = [], 0, False
    for line in text.splitlines(keepends=True):
        if line.strip() and not line.startswith(" ") and not line.startswith("-"):
            in_ch = line.startswith("channels:")
        mm = re.match(r"^(\s+)radioId:\s*!default\s*(\"\")?\s*$", line) if in_ch else None
        if mm:
            line, n = f"{mm.group(1)}radioId: {rid}\n", n + 1
        out.append(line)
    return "".join(out), n


# ---------------------------------------------------------------- the page's codeplug model

MAX = {"channels": 1024, "zones": 68, "contacts": 1024, "groupLists": 76, "zone_members": 80, "group_members": 32,
       "name": 16}


def model_problems(model):
    """What is wrong with a codeplug model (qdmr_yaml.load form) the page
    edited, before it becomes YAML: references to things that don't exist,
    duplicate ids, names longer than the radio stores, frequencies outside
    its bands, more of anything than it holds. [] when it is fine; dmrconf's
    own encode and write checks run after this."""
    out = []
    if not isinstance(model, dict):
        return ["the codeplug is not a map"]

    def items(key):
        v = model.get(key) or []
        if not isinstance(v, list):
            out.append(f"{key} is not a list")
            return []
        res = []
        for it in v:
            if isinstance(it, dict) and len(it) == 1 and isinstance(next(iter(it.values())), dict):
                res.append(next(iter(it.items())))
            elif isinstance(it, dict) and "id" in it:          # groupLists / zones: plain maps
                res.append((None, it))
            else:
                out.append(f"{key}: an entry not understood")
        return res

    def ids(key, entries):
        seen = set()
        for _, e in entries:
            i = e.get("id")
            if not isinstance(i, str) or not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", i):
                out.append(f"{key}: id {i!r}")
            elif i in seen:
                out.append(f"{key}: id {i!r} twice")
            seen.add(i)
        return seen

    def name(key, e):
        n = e.get("name")
        if not isinstance(n, str) or not n.strip():
            out.append(f"{key}: an entry has no name")
        elif len(n) > MAX["name"]:
            out.append(f"{key}: {n!r} is longer than the {MAX['name']} characters the radio stores")
        elif not all(32 <= ord(ch) < 127 for ch in n):
            out.append(f"{key}: {n!r} has characters the radio can't show")

    chans, zones, contacts, groups = items("channels"), items("zones"), items("contacts"), items("groupLists")
    for key, entries in (("channels", chans), ("zones", zones), ("contacts", contacts), ("groupLists", groups)):
        if len(entries) > MAX[key]:
            out.append(f"{len(entries)} {key}: the radio holds {MAX[key]}")
    ch_ids, z_ids = ids("channels", chans), ids("zones", zones)
    ct_ids, g_ids = ids("contacts", contacts), ids("groupLists", groups)
    for kind, c in chans:
        name("channels", c)
        label = c.get("name") or c.get("id")
        if kind not in ("fm", "dmr"):
            out.append(f"channel {label!r}: kind {kind!r} (FM or DMR only on this radio)")
        for f in ("rxFrequency", "txFrequency"):
            hz = _mhz(_model_text(c.get(f)))
            if hz is None:
                out.append(f"channel {label!r}: {f} {c.get(f)!r} is not a frequency")
            elif not any(lo <= hz <= hi for lo, hi in ((127_000_000, 178_000_000), (190_000_000, 282_000_000),
                                                       (380_000_000, 564_000_000))):
                out.append(f"channel {label!r}: {hz / 1e6:.4f} MHz is outside the radio's bands")
        if kind == "dmr":
            g = c.get("groupList")
            if isinstance(g, str) and g and g not in g_ids:
                out.append(f"channel {label!r}: group list {g!r} doesn't exist")
            t = c.get("contact")
            if isinstance(t, str) and t and t not in ct_ids:
                out.append(f"channel {label!r}: contact {t!r} doesn't exist")
            cc = _model_text(c.get("colorCode"))
            if not (cc.isdigit() and 0 <= int(cc) <= 15):
                out.append(f"channel {label!r}: colour code {cc!r}")
            if _model_text(c.get("timeSlot")) not in ("TS1", "TS2"):
                out.append(f"channel {label!r}: timeslot {c.get('timeSlot')!r}")
    for _, z in zones:
        name("zones", z)
        for side in ("A", "B"):
            members = z.get(side) or []
            if not isinstance(members, list):
                out.append(f"zone {z.get('name')!r}: {side} is not a list")
                continue
            if len(members) > MAX["zone_members"]:
                out.append(f"zone {z.get('name')!r}: {len(members)} channels, the radio holds {MAX['zone_members']}")
            for mbr in members:
                if mbr not in ch_ids:
                    out.append(f"zone {z.get('name')!r}: channel {mbr!r} doesn't exist")
    for _, gl in groups:
        name("groupLists", gl)
        members = gl.get("contacts") or []
        if not isinstance(members, list):
            out.append(f"group list {gl.get('name')!r}: contacts is not a list")
            continue
        if len(members) > MAX["group_members"]:
            out.append(f"group list {gl.get('name')!r}: {len(members)} contacts, the radio holds {MAX['group_members']}")
        for mbr in members:
            if mbr not in ct_ids:
                out.append(f"group list {gl.get('name')!r}: contact {mbr!r} doesn't exist")
    for _, ct in contacts:
        name("contacts", ct)
        num = _model_text(ct.get("number"))
        if not (num.isdigit() and 1 <= int(num) <= 16_776_415):
            out.append(f"contact {ct.get('name')!r}: number {num!r}")
    return out[:30]
