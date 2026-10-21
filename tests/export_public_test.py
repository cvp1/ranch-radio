"""tools/export-public.sh, against a local stand-in for the public repo.

  - snapshot: one commit, no history from dev
  - sync with nothing new: "current", nothing committed
  - sync after a dev change (an edit and a deletion): one new commit with
    both, dry run leaves the public repo alone, --push publishes, and no dev
    history comes along
  - fails closed: a personal string in the tree, or in the commit message, or
    no private leak scan at all, and nothing is exported
  - uncommitted dev changes are listed, not exported
A throwaway leak list (one planted word) stands in for the private one.

    python3 tests/export_public_test.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORD = "PLANTED" + "SECRET"     # built here, so this file never carries it
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def sh(*argv, cwd=None, env=None):
    r = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


T = Path(tempfile.mkdtemp(prefix="export-public-"))
dev, public = T / "dev", T / "public.git"
sh("git", "clone", "-q", str(ROOT), str(dev))
for k, v in (("user.name", "dev"), ("user.email", "dev@example.invalid")):
    sh("git", "-C", str(dev), "config", k, v)
conf = T / "conf"
(conf / "tests").mkdir(parents=True)
(conf / "leaks.txt").write_text(WORD + "\n")
(conf / "tests/leak_scan.py").write_text('''import re, sys
from pathlib import Path
pats = [re.compile(l.strip(), re.I) for l in (Path(__file__).parent.parent / "leaks.txt").read_text().splitlines() if l.strip()]
hits = [str(p) for p in Path(sys.argv[1]).rglob("*") if p.is_file() and ".git" not in p.parts
        and any(x.search(p.read_text(errors="replace")) for x in pats)]
print("\\n".join(hits) or "clean"); sys.exit(1 if hits else 0)
''')
env = dict(os.environ, RANCH_RADIO_CONFIG=str(conf), RANCH_RADIO_PUBLIC=str(public))
script = str(dev / "tools/export-public.sh")


def public_log():
    return sh("git", "--git-dir", str(public), "log", "--format=%s")[1].split("\n")[:-1] \
        if public.exists() else []


def public_file(path):
    rc, out = sh("git", "--git-dir", str(public), "show", f"HEAD:{path}")
    return out if rc == 0 else None


# snapshot: the first publish
rc, out = sh("bash", script, "snapshot", str(T / "snap"), env=env)
check("snapshot: made and checked", (rc, "one commit" in out), (0, True))
check("  one commit, no dev history", sh("git", "-C", str(T / "snap"), "rev-list", "--count", "HEAD")[1].strip(), "1")
rc, _ = sh("bash", script, "snapshot", str(T / "snap"), env=env)
check("snapshot into an existing directory: refused", rc, 1)
sh("git", "init", "-q", "--bare", "-b", "main", str(public))
sh("git", "-C", str(T / "snap"), "push", "-q", str(public), "HEAD:main")

rc, out = sh("bash", script, "sync", env=env)
check("sync with nothing new: current, nothing committed", (rc, "current" in out, len(public_log())), (0, True, 1))

# a dev change: an edit and a deletion
(dev / "README.md").write_text((dev / "README.md").read_text() + "\nA new line.\n")
sh("git", "-C", str(dev), "rm", "-q", "tools/README.md")
sh("git", "-C", str(dev), "commit", "-qam", "dev: edit README, drop tools/README.md")
rc, out = sh("bash", script, "sync", "-m", "Docs update", env=env)
check("sync dry run: shows the change", (rc, "Ready to publish" in out, "README.md" in out), (0, True, True))
check("  and leaves the public repo alone", len(public_log()), 1)
rc, out = sh("bash", script, "sync", "-m", "Docs update", "--push", env=env)
check("sync --push: published as one new commit", (rc, public_log()), (0, ["Docs update", "Ranch Radio"]))
check("  with the edit", "A new line." in (public_file("README.md") or ""), True)
check("  and the deletion", public_file("tools/README.md"), None)
check("  and no dev history (dev's message isn't there)", any("dev:" in m for m in public_log()), False)

# fails closed
(dev / "aprs.py").write_text((dev / "aprs.py").read_text() + f"\n# {WORD}\n")
sh("git", "-C", str(dev), "commit", "-qam", "dev: a leak")
rc, out = sh("bash", script, "sync", "--push", env=env)
check("a personal string in the tree: refused, nothing published", (rc, len(public_log())), (1, 2))
sh("git", "-C", str(dev), "revert", "--no-edit", "-q", "HEAD")
rc, out = sh("bash", script, "sync", "-m", f"fix for {WORD}", "--push", env=env)
check("a personal string in the message: refused", (rc, len(public_log())), (1, 2))
rc, out = sh("bash", script, "sync", "--push", env=dict(env, RANCH_RADIO_CONFIG=str(T / "nowhere")))
check("no private leak scan: refused, nothing exported", (rc, "nothing exported" in out, len(public_log())), (1, True, 2))

# uncommitted changes are listed, not exported
(dev / "README.md").write_text((dev / "README.md").read_text() + "\nNot committed.\n")
rc, out = sh("bash", script, "sync", env=env)
check("uncommitted changes: listed", "Not exported (uncommitted" in out and "README.md" in out, True)
check("  and not exported", "Not committed." in (public_file("README.md") or ""), False)

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
