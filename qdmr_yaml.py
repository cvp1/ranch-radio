"""The YAML qdmr writes, read into plain Python and written back. Stdlib only.

qdmr's codeplug files (dmrconf read -y) use a small part of YAML: block
maps and lists, flow maps and lists ({a: b}, [a, b]), plain and
double-quoted scalars, and two tags (!default "", !selected ""). That
part is all this reads; anything else raises YamlError, and the page then
offers only the raw YAML editor (nothing is guessed).

Scalars stay strings ("445.85 MHz", "true", "3"): nothing is converted, so
what is read is what is written back. A tagged scalar is a Tag. dump()
writes block style, scalar lists in flow style ([a, b]), and quotes only
what has to be quoted.

The addon proves each file survives: dmrconf encodes the original and
load-then-dump to the same binary before the structured editor opens it
(opengd77 addon, _model_check).
"""

import json
import re


class YamlError(ValueError):
    pass


class Tag:
    """A tagged scalar: !default "" -> Tag("default", "")."""
    __slots__ = ("tag", "value")

    def __init__(self, tag, value=""):
        self.tag, self.value = tag, value

    def __eq__(self, o):
        return isinstance(o, Tag) and (self.tag, self.value) == (o.tag, o.value)

    def __repr__(self):
        return f"Tag({self.tag!r}, {self.value!r})"


# ------------------------------------------------------------------ scalars

_ESC = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b"}


def _dq(s, i):
    """A double-quoted scalar starting at s[i] == '"': (value, index after)."""
    out, i = [], i + 1
    while i < len(s):
        c = s[i]
        if c == '"':
            return "".join(out), i + 1
        if c == "\\":
            i += 1
            if i >= len(s):
                break
            e = s[i]
            if e in _ESC:
                out.append(_ESC[e])
            elif e in "xuU":
                n = {"x": 2, "u": 4, "U": 8}[e]
                try:
                    out.append(chr(int(s[i + 1:i + 1 + n], 16)))
                except ValueError:
                    raise YamlError(f"bad escape in {s!r}")
                i += n
            else:
                raise YamlError(f"escape \\{e} in {s!r}")
            i += 1
            continue
        out.append(c)
        i += 1
    raise YamlError(f"unterminated string: {s!r}")


def _sq(s, i):
    out, i = [], i + 1
    while i < len(s):
        if s[i] == "'":
            if s[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(s[i])
        i += 1
    raise YamlError(f"unterminated string: {s!r}")


def _scalar(text):
    """A whole scalar (the rest of a line): plain, quoted or tagged."""
    t = text.strip()
    if not t or t in ("~", "null"):
        return None
    if t.startswith("!"):
        m = re.match(r"^!([A-Za-z][A-Za-z0-9_]*)(?:\s+(.*))?$", t)
        if not m:
            raise YamlError(f"tag not understood: {t!r}")
        inner = _scalar(m.group(2) or "")
        if isinstance(inner, (list, dict, Tag)):
            raise YamlError(f"tagged collection: {t!r}")
        return Tag(m.group(1), "" if inner is None else inner)
    if t[0] in "\"'":
        v, end = (_dq if t[0] == '"' else _sq)(t, 0)
        if t[end:].strip():
            raise YamlError(f"text after a string: {t!r}")
        return v
    if t[0] in "[{":
        v, end = _flow(t, 0)
        if t[end:].strip():
            raise YamlError(f"text after a flow collection: {t!r}")
        return v
    if t[0] in "&*|>%@`" or " #" in t:
        raise YamlError(f"YAML feature not supported here: {t!r}")
    return t


def _flow(s, i):
    """A flow collection starting at s[i] in "[{": (value, index after)."""
    close = "]" if s[i] == "[" else "}"
    is_map = s[i] == "{"
    out = {} if is_map else []
    i += 1

    def ws(j):
        while j < len(s) and s[j] == " ":
            j += 1
        return j

    def item(j):
        j = ws(j)
        if j < len(s) and s[j] in "[{":
            return _flow(s, j)
        if j < len(s) and s[j] in "\"'":
            return (_dq if s[j] == '"' else _sq)(s, j)
        k = j
        while k < len(s) and s[k] not in ",]}" and not (s[k] == ":" and k + 1 < len(s) and s[k + 1] == " "):
            k += 1
        raw = s[j:k].strip()
        return (_scalar(raw) if raw else None), k

    i = ws(i)
    if s[i:i + 1] == close:
        return out, i + 1
    while i < len(s):
        if is_map:
            key, i = item(i)
            i = ws(i)
            if s[i:i + 1] != ":":
                raise YamlError(f"flow map without ':' in {s!r}")
            val, i = item(i + 1)
            if not isinstance(key, str):
                raise YamlError(f"flow map key {key!r}")
            out[key] = "" if val is None else val
        else:
            val, i = item(i)
            out.append(val)
        i = ws(i)
        if s[i:i + 1] == ",":
            i += 1
            continue
        if s[i:i + 1] == close:
            return out, i + 1
        raise YamlError(f"flow collection not understood: {s!r}")
    raise YamlError(f"unterminated flow collection: {s!r}")


# ------------------------------------------------------------------ blocks

_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:(?:\s+(.*))?$")


def _lines(text):
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise YamlError(f"line {n}: tab indentation")
        s = raw.rstrip()
        if not s.strip() or s.lstrip().startswith("#") or s in ("---", "..."):
            continue
        out.append((len(s) - len(s.lstrip(" ")), s.strip(), n))
    return out


def load(text):
    """qdmr YAML text -> dict / list / str / Tag / None. Raises YamlError."""
    ls = _lines(text)
    if not ls:
        return {}
    val, i = _block(ls, 0, ls[0][0])
    if i != len(ls):
        raise YamlError(f"line {ls[i][2]}: not understood: {ls[i][1]!r}")
    return val


def _block(ls, i, indent):
    if ls[i][1].startswith("- ") or ls[i][1] == "-":
        return _seq(ls, i, indent)
    if _KEY.match(ls[i][1]):
        return _map(ls, i, indent)
    if len(ls) > i and ls[i][1][:1] in "[{":           # a bare flow collection: "encryptionKeys:\n  []"
        return _scalar(ls[i][1]), i + 1
    raise YamlError(f"line {ls[i][2]}: not understood: {ls[i][1]!r}")


def _value_after_key(ls, i, indent, rest):
    """The value of "key: rest" at line i: inline, or the block below it."""
    if rest is not None and rest.strip():
        return _scalar(rest), i + 1
    j = i + 1
    if j < len(ls) and (ls[j][0] > indent or (ls[j][0] == indent and ls[j][1].startswith("- "))):
        return _block(ls, j, ls[j][0])
    return None, j


def _map(ls, i, indent, first=None):
    out = {}
    while i < len(ls):
        ind, s, n = ls[i]
        if first is not None:
            s, first = first, None
        elif ind != indent:
            break
        m = _KEY.match(s)
        if not m:
            if ind == indent and s.startswith("- "):
                break
            raise YamlError(f"line {n}: not understood: {s!r}")
        key = m.group(1)
        if key in out:
            raise YamlError(f"line {n}: {key!r} twice")
        out[key], i = _value_after_key(ls, i, indent, m.group(2))
    return out, i


def _seq(ls, i, indent):
    out = []
    while i < len(ls) and ls[i][0] == indent and (ls[i][1].startswith("- ") or ls[i][1] == "-"):
        ind, s, n = ls[i]
        rest = s[1:].strip()
        if not rest:
            j = i + 1
            if j < len(ls) and ls[j][0] > indent:
                v, i = _block(ls, j, ls[j][0])
            else:
                v, i = None, j
            out.append(v)
            continue
        if rest[0] not in "\"'[{!" and _KEY.match(rest):
            col = indent + (len(s) - len(rest))          # the map's keys line up under its first key
            v, i = _map(ls, i, col, first=rest)
            out.append(v)
            continue
        out.append(_scalar(rest))
        i += 1
    return out, i


# ------------------------------------------------------------------ dump

_PLAIN = re.compile(r"^[A-Za-z0-9_.(/][^:#{}\[\],&*!|>'\"%@`]*$")


def _out(v):
    if v is None:
        return "~"
    if isinstance(v, Tag):
        return f"!{v.tag} {_out(v.value)}"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if not isinstance(v, str):
        raise YamlError(f"can't write {type(v).__name__}")
    if v and _PLAIN.match(v) and v == v.strip() and ": " not in v and " #" not in v:
        return v
    if v and re.match(r"^[A-Za-z0-9_][^:#{}\[\],\"]*$", v) and v == v.strip():   # e.g. qdmr's melodies: ais'4 r16
        return v
    return json.dumps(v, ensure_ascii=False)


def _scalar_list(v):
    return isinstance(v, list) and all(not isinstance(x, (list, dict)) for x in v)


def _flow_out(v):
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_flow_out(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_flow_out(x) for x in v) + "]"
    return _out(v)


def _dump_map(m, indent):
    sp = " " * indent
    out = []
    for k, v in m.items():
        if not isinstance(k, str) or not _KEY.match(f"{k}:"):
            raise YamlError(f"key {k!r}")
        if isinstance(v, dict) and v:
            out.append(f"{sp}{k}:")
            out += _dump_map(v, indent + 2)
        elif isinstance(v, list) and v and not _scalar_list(v):
            out.append(f"{sp}{k}:")
            out += _dump_seq(v, indent + 2)
        elif isinstance(v, (list, dict)):
            out.append(f"{sp}{k}: {_flow_out(v)}")
        else:
            out.append(f"{sp}{k}: {_out(v)}")
    return out


def _dump_seq(s, indent):
    sp = " " * indent
    out = []
    for v in s:
        if isinstance(v, dict) and v:
            lines = _dump_map(v, indent + 2)
            out.append(sp + "- " + lines[0][indent + 2:])
            out += lines[1:]
        elif isinstance(v, list) and v and not _scalar_list(v):
            raise YamlError("a list directly in a list")
        elif isinstance(v, (list, dict)):
            out.append(f"{sp}- {_flow_out(v)}")
        else:
            out.append(f"{sp}- {_out(v)}")
    return out


def dump(v):
    """dict -> qdmr-readable YAML text."""
    if not isinstance(v, dict):
        raise YamlError("a codeplug is a map")
    return "---\n" + "\n".join(_dump_map(v, 0)) + "\n...\n"


# ------------------------------------------------------------------ JSON for the page

def to_json(v):
    """Tags become {"$tag": name, "$value": value}; everything else is already JSON."""
    if isinstance(v, Tag):
        return {"$tag": v.tag, "$value": to_json(v.value)}
    if isinstance(v, dict):
        return {k: to_json(x) for k, x in v.items()}
    if isinstance(v, list):
        return [to_json(x) for x in v]
    return v


def from_json(v, depth=0):
    """The page's model back; bounded, and only JSON types (numbers and
    booleans become the strings qdmr would have written)."""
    if depth > 32:
        raise YamlError("model nested too deep")
    if isinstance(v, dict):
        if set(v) == {"$tag", "$value"}:
            if not isinstance(v["$tag"], str) or not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", v["$tag"]):
                raise YamlError(f"tag {v['$tag']!r}")
            return Tag(v["$tag"], from_json(v["$value"], depth + 1))
        return {str(k): from_json(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [from_json(x, depth + 1) for x in v]
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None or isinstance(v, str):
        return v
    raise YamlError(f"model value {v!r}")
