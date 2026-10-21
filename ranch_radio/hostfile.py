"""Edit a hand-formatted host file in place: add or drop one member.

Host files are written by hand, compact where that reads best. Re-dumping the
whole file would turn a one-block change into a whole-file diff, so these edits
touch only the text of the member they change, copying the spacing of its
neighbours. Every edit is checked: the new text must parse to exactly the
intended data, or nothing is returned.

    put(text, path, value) -> new text     path: ("winlink",) or ("addons", "uvpro")
                                                 or ("radios", 0, "lane")
    drop(text, path)       -> new text     an emptied parent object is dropped too
"""

import copy
import json


class EditError(ValueError):
    pass


# ---------------------------------------------------------------- spans

class _Node:
    """A JSON value's span in the text; objects keep their members' spans."""

    def __init__(self, start, end, kind, members=None):
        self.start, self.end, self.kind = start, end, kind   # end is exclusive
        self.members = members or []      # object: [(key, key_start, node)]; array: [(i, start, node)]


def _ws(t, i):
    while i < len(t) and t[i] in " \t\r\n":
        i += 1
    return i


def _string_end(t, i):
    i += 1
    while t[i] != '"':
        i += 2 if t[i] == "\\" else 1
    return i + 1


def _parse(t, i):
    i = _ws(t, i)
    c = t[i]
    if c == "{":
        members, j = [], _ws(t, i + 1)
        if t[j] == "}":
            return _Node(i, j + 1, "object"), j + 1
        while True:
            j = _ws(t, j)
            ks = j
            ke = _string_end(t, j)
            key = json.loads(t[ks:ke])
            j = _ws(t, ke)
            if t[j] != ":":
                raise EditError(f"expected ':' at {j}")
            node, j = _parse(t, j + 1)
            members.append((key, ks, node))
            j = _ws(t, j)
            if t[j] == ",":
                j += 1
                continue
            if t[j] == "}":
                return _Node(i, j + 1, "object", members), j + 1
            raise EditError(f"expected ',' or '}}' at {j}")
    if c == "[":
        members, j = [], _ws(t, i + 1)
        if t[j] == "]":
            return _Node(i, j + 1, "array"), j + 1
        n = 0
        while True:
            node, j = _parse(t, j)
            members.append((n, node.start, node))
            n += 1
            j = _ws(t, j)
            if t[j] == ",":
                j += 1
                continue
            if t[j] == "]":
                return _Node(i, j + 1, "array", members), j + 1
            raise EditError(f"expected ',' or ']' at {j}")
    if c == '"':
        e = _string_end(t, i)
        return _Node(i, e, "scalar"), e
    j = i
    while j < len(t) and t[j] not in ",}] \t\r\n":
        j += 1
    return _Node(i, j, "scalar"), j


def _find(root, path):
    node = root
    for k in path:
        for key, _, child in node.members:
            if key == k:
                node = child
                break
        else:
            return None
    return node


# ---------------------------------------------------------------- edits

def _dump(value):
    return json.dumps(value, ensure_ascii=False)


def _checked(text, new, want):
    try:
        got = json.loads(new)
    except ValueError as e:
        raise EditError(f"the edit made invalid JSON: {e}")
    if got != want:
        raise EditError("the edit did not produce the intended data")
    return new


def put(text, path, value):
    """Add (or replace) the member at path; parents that don't exist are made."""
    data = json.loads(text)
    want = copy.deepcopy(data)
    # The deepest existing object on the path; what's left of the path becomes
    # one nested value inserted there.
    depth = len(path) - 1
    while depth > 0:
        try:
            cur = want
            for k in path[:depth]:
                cur = cur[k]
            if isinstance(cur, dict):
                break
        except (KeyError, IndexError, TypeError):
            pass
        depth -= 1
    rest = path[depth:]
    nested = value
    for k in reversed(rest[1:]):
        nested = {k: nested}
    cur = want
    for k in path[:depth]:
        cur = cur[k]
    cur[rest[0]] = copy.deepcopy(nested)

    root, _ = _parse(text, 0)
    parent = _find(root, path[:depth])
    if parent is None or parent.kind != "object":
        raise EditError(f"{'/'.join(map(str, path[:depth])) or 'the file'} is not an object")
    key = rest[0]
    for k, ks, node in parent.members:
        if k == key:                                  # replace the value only
            return _checked(text, text[:node.start] + _dump(nested) + text[node.end:], want)
    member = f"{_dump(key)}: {_dump(nested)}"
    ms = parent.members
    if not ms:
        new = text[:parent.start + 1] + member + text[parent.end - 1:]
    else:
        # Copy the spacing between the last two members (or after the brace).
        if len(ms) >= 2:
            prev_end = ms[-2][2].end
            gap = text[prev_end:ms[-1][1]]
            sep = gap[gap.index(",") + 1:] if "," in gap else " "
        else:
            sep = text[parent.start + 1:ms[0][1]] or " "
        last_end = ms[-1][2].end
        new = text[:last_end] + "," + sep + member + text[last_end:]
    return _checked(text, new, want)


def drop(text, path):
    """Remove the member at path; a parent object it leaves empty goes too."""
    data = json.loads(text)
    want = copy.deepcopy(data)
    cur = want
    for k in path[:-1]:
        cur = cur[k]
    if path[-1] not in cur:
        raise EditError(f"{'/'.join(map(str, path))} is not in the file")
    del cur[path[-1]]
    if not cur and len(path) > 1 and isinstance(cur, dict):
        return drop(text, path[:-1])
    root, _ = _parse(text, 0)
    parent = _find(root, path[:-1])
    ms = parent.members
    i = next(n for n, (k, _, _) in enumerate(ms) if k == path[-1])
    if len(ms) == 1:
        new = text[:parent.start + 1] + text[parent.end - 1:]
    elif i > 0:                       # from the end of the one before to the end of this one
        new = text[:ms[i - 1][2].end] + text[ms[i][2].end:]
    else:                             # first: from its key to the next key
        new = text[:ms[0][1]] + text[ms[1][1]:]
    return _checked(text, new, want)
