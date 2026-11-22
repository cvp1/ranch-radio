"""Fold changelog.d/*.md fragments into CHANGELOG.md's Unreleased section.

    python3 tools/changelog_fold.py            # what it would do
    python3 tools/changelog_fold.py --apply    # fold, then `git rm` the fragments

Each fragment's bullets go to the top of their section under
`## [Unreleased]` (the section made, in SECTIONS order, if missing); fragments
are taken oldest name first, so the newest lands on top. A fragment with an
unknown heading, or text outside a section, stops everything (exit 1) and
nothing is written. See changelog.d/README.md.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECTIONS = ("Security", "Added", "Changed", "Deprecated", "Removed", "Fixed")
HEAD = re.compile(r"^### (\w+)\s*$")


def parse(text, name):
    """A fragment -> {section: [entry, ...]} (an entry: a bullet with its continuation lines)."""
    out, sec, cur = {}, None, None
    for line in text.splitlines():
        m = HEAD.match(line)
        if m:
            if m.group(1) not in SECTIONS:
                raise ValueError(f"{name}: unknown section {m.group(1)!r} (one of {', '.join(SECTIONS)})")
            sec, cur = m.group(1), None
            out.setdefault(sec, [])
        elif line.startswith("- "):
            if sec is None:
                raise ValueError(f"{name}: a bullet before any ### heading")
            cur = [line]
            out[sec].append(cur)
        elif line.strip():
            if cur is None:
                raise ValueError(f"{name}: text outside a bullet: {line.strip()[:60]!r}")
            cur.append(line)
    entries = {s: ["\n".join(e) for e in v] for s, v in out.items() if v}
    if not entries:
        raise ValueError(f"{name}: no entries")
    return entries


def fold(changelog, fragments):
    """CHANGELOG text + [(name, text)] -> the new text."""
    lines = changelog.split("\n")
    try:
        top = lines.index("## [Unreleased]")
    except ValueError:
        raise ValueError("CHANGELOG.md has no '## [Unreleased]' line") from None
    end = next((i for i in range(top + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    body = lines[top + 1:end]
    # The Unreleased section as [(section, [lines])], in file order.
    secs, pre = [], []
    for line in body:
        m = HEAD.match(line)
        if m:
            secs.append((m.group(1), []))
        elif secs:
            secs[-1][1].append(line)
        else:
            pre.append(line)
    add = {}
    for name, text in sorted(fragments):
        for s, es in parse(text, name).items():
            add.setdefault(s, [])[:0] = es          # later names on top
    for s, es in add.items():
        block = []
        for e in es:
            block += e.split("\n")
        have = next((x for x in secs if x[0] == s), None)
        if have is None:
            have = (s, [""])
            order = [x[0] for x in secs]
            at = next((i for i, n in enumerate(order) if SECTIONS.index(n) > SECTIONS.index(s)), len(secs)) \
                if all(n in SECTIONS for n in order) else len(secs)
            secs.insert(at, have)
        content = have[1]
        lead = 0
        while lead < len(content) and not content[lead].strip():
            lead += 1
        content[lead:lead] = block + ([""] if lead < len(content) and content[lead].strip() else [])
        if lead == 0:
            content.insert(0, "")
    new = []
    for s, content in secs:
        new.append(f"### {s}")
        new += content
    if new and new[-1].strip():
        new.append("")
    return "\n".join(lines[:top + 1] + pre + new + lines[end:])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="write CHANGELOG.md and git rm the fragments")
    p.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    root = Path(a.root)
    frags = sorted(f for f in (root / "changelog.d").glob("*.md") if f.name != "README.md")
    if not frags:
        print("no fragments in changelog.d/")
        return 0
    cl = root / "CHANGELOG.md"
    try:
        new = fold(cl.read_text(), [(f.name, f.read_text()) for f in frags])
    except ValueError as e:
        print(f"changelog_fold: {e}", file=sys.stderr)
        return 1
    for f in frags:
        print(f"{'folded' if a.apply else 'would fold'} changelog.d/{f.name}")
    if not a.apply:
        return 0
    cl.write_text(new)
    subprocess.run(["git", "rm", "-q", "--", *[str(f.relative_to(root)) for f in frags]], cwd=root, check=True)
    print("CHANGELOG.md updated; review and commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
