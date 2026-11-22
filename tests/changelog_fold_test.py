"""tools/changelog_fold.py: changelog.d fragments into CHANGELOG.md's Unreleased.

  - a fragment's bullets go to the top of their existing section; the rest stays
  - a section Unreleased lacks is made, in Keep a Changelog order
  - two fragments: the later name lands on top; continuation lines stay with their bullet
  - released sections below Unreleased are untouched
  - refused, nothing written: an unknown heading, a bullet before a heading,
    stray text, an empty fragment, a CHANGELOG with no Unreleased
  - --apply in a git checkout: CHANGELOG written, the fragments git rm'd,
    changelog.d/README.md kept; without --apply nothing changes
  - the real CHANGELOG.md folds a fragment cleanly (dry run in memory)

    python3 tests/changelog_fold_test.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import changelog_fold as cf  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


BASE = """# Changelog

## [Unreleased]

### Added

- old added

### Fixed

- old fixed

## [0.1.0] - 2026-01-01

### Added

- released
"""

new = cf.fold(BASE, [("a.md", "### Added\n\n- new one\n  continued\n")])
check("existing section: on top, continuation kept, old kept",
      new.split("### Fixed")[0].split("### Added")[1].strip().split("\n"), ["- new one", "  continued", "", "- old added"])
check("released sections untouched", new.split("## [0.1.0] - 2026-01-01")[1], BASE.split("## [0.1.0] - 2026-01-01")[1])

new = cf.fold(BASE, [("b.md", "### Security\n\n- sec\n"), ("c.md", "### Changed\n\n- chg\n")])
heads = [l for l in new.split("## [0.1.0]")[0].split("\n") if l.startswith("### ")]
check("missing sections made in order", heads, ["### Security", "### Added", "### Changed", "### Fixed"])
check("a made section has its bullet", "### Changed\n\n- chg\n" in new, True)

new = cf.fold(BASE, [("z-later.md", "### Added\n\n- later\n"), ("a-earlier.md", "### Added\n\n- earlier\n")])
added = new.split("### Fixed")[0].split("### Added")[1]
check("two fragments: the later name on top", [l for l in added.split("\n") if l.startswith("- ")], ["- later", "- earlier", "- old added"])
check("blank line between entries kept tidy", "\n\n\n" in new.split("## [0.1.0]")[0], False)

for name, frag in (("an unknown heading", "### Misc\n\n- x\n"), ("a bullet before a heading", "- x\n"),
                   ("stray text", "### Added\n\nsome prose\n"), ("an empty fragment", "\n")):
    try:
        cf.fold(BASE, [("bad.md", frag)])
        got = "folded"
    except ValueError as e:
        got = "refused" if "bad.md" in str(e) else str(e)
    check(f"refused: {name}", got, "refused")
try:
    cf.fold("# Changelog\n", [("a.md", "### Added\n\n- x\n")])
    got = "folded"
except ValueError:
    got = "refused"
check("refused: no Unreleased", got, "refused")

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    (d / "CHANGELOG.md").write_text(BASE)
    (d / "changelog.d").mkdir()
    (d / "changelog.d/README.md").write_text("how\n")
    (d / "changelog.d/x.md").write_text("### Fixed\n\n- a fix\n")
    (d / "changelog.d/y.md").write_text("### Misc\n\n- bad\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    env_git = ["-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", *env_git, "-C", str(d), "commit", "-qm", "x"], check=True)
    check("a bad fragment: exit 1, nothing written", (cf.main(["--apply", "--root", str(d)]), (d / "CHANGELOG.md").read_text(),
          (d / "changelog.d/x.md").exists()), (1, BASE, True))
    subprocess.run(["git", "-C", str(d), "rm", "-q", "changelog.d/y.md"], check=True)
    subprocess.run(["git", *env_git, "-C", str(d), "commit", "-qm", "drop the bad one"], check=True)
    check("dry run: exit 0, nothing written", (cf.main(["--root", str(d)]), (d / "CHANGELOG.md").read_text()), (0, BASE))
    check("--apply: exit 0", cf.main(["--apply", "--root", str(d)]), 0)
    check("--apply: the entry is in", "### Fixed\n\n- a fix\n\n- old fixed" in (d / "CHANGELOG.md").read_text(), True)
    check("--apply: the fragment git rm'd, the README kept",
          (sorted(p.name for p in (d / "changelog.d").iterdir()),
           subprocess.run(["git", "-C", str(d), "status", "--short", "changelog.d"], capture_output=True, text=True).stdout.strip()),
          (["README.md"], "D  changelog.d/x.md"))

real = (ROOT / "CHANGELOG.md").read_text()
new = cf.fold(real, [("t.md", "### Added\n\n- probe entry\n")])
check("the real CHANGELOG: one line in, under Unreleased's Added, nothing else moved",
      (new.replace("- probe entry\n\n", "", 1) == real, new.index("- probe entry") < new.index("## [", new.index("## [Unreleased]") + 5)),
      (True, True))

print(f"\n{fails} failure(s)" if fails else "\nall ok")
sys.exit(1 if fails else 0)
