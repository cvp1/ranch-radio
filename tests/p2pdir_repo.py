"""A scratch P2P directory repo for the intake tests: a bare origin and a
clone that can push to it, with stand-ins for the repo's parser (p2pdir.py)
and build (build.py) that keep the real ones' contract (the directory repo's
SPEC.md): the parser prints the listing as JSON, or `rejected: <code>: ...`
and exits 1; the build exits 1 on any bad listing. The stand-in build calls a
listing bad when it holds BUILDFAIL, and imports a sibling module as the
real one does; the parser flags auto at 14300 kHz.
Nothing reaches GitHub."""
import os
import subprocess
import tempfile

PARSER = r'''
import json, sys
text = sys.stdin.read()
call = sys.argv[sys.argv.index("--call") + 1]
lines = [l for l in text.splitlines() if l.strip()]
if not lines or lines[0] != "P2PDIR 1":
    print("rejected: no-header: the first line must be P2PDIR 1"); sys.exit(1)
ents, note = [], ""
for l in lines[1:]:
    if l.startswith("entry: "):
        f = l[7:].split(None, 4)
        flags = ["hf-auto-outside-97.221b"] if f[3] == "auto" and float(f[0]) == 14300 else []
        ents.append({"centre_khz": float(f[0]), "mode": f[1], "bw_hz": int(f[2]), "control": f[3],
                     "hours": f[4] if len(f) > 4 else "", "flags": flags})
    elif l.startswith("note: "):
        note = l[6:]
grid = next((l[6:] for l in lines if l.startswith("grid: ")), None)
print(json.dumps({"call": call, "grid": grid, "note": note, "entries": ents}))
'''
BUILD = r'''
import os, sys
import p2pdir_rules  # a sibling module, as the real build imports p2pdir.py
root = os.path.dirname(os.path.abspath(__file__))
for n in os.listdir(os.path.join(root, "listings")):
    if n.endswith(".txt") and "BUILDFAIL" in open(os.path.join(root, "listings", n)).read():
        print("rejected: " + n); sys.exit(1)
'''


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make(seed=None):
    """(origin, clone) in a new temp dir; `seed` = {CALL: (listing text, confirmed date)}."""
    tmp = tempfile.mkdtemp(prefix="p2pdir-repo-")
    origin, repo = os.path.join(tmp, "origin.git"), os.path.join(tmp, "clone")
    git(tmp, "init", "-q", "--bare", "-b", "main", origin)
    git(tmp, "clone", "-q", origin, repo)
    for k, v in (("user.name", "test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
        git(repo, "config", k, v)
    git(repo, "checkout", "-q", "-b", "main")
    os.makedirs(os.path.join(repo, "listings"))
    with open(os.path.join(repo, "p2pdir.py"), "w") as f:
        f.write(PARSER)
    with open(os.path.join(repo, "build.py"), "w") as f:
        f.write(BUILD)
    with open(os.path.join(repo, "p2pdir_rules.py"), "w") as f:
        f.write("RULES = 1\n")
    with open(os.path.join(repo, ".gitignore"), "w") as f:
        f.write("__pycache__/\n")
    for call, (text, confirmed) in (seed or {}).items():
        with open(os.path.join(repo, "listings", f"{call}.txt"), "w") as f:
            f.write(text)
        with open(os.path.join(repo, "listings", f"{call}.json"), "w") as f:
            f.write('{"confirmed": "%s", "source": "maintainer"}\n' % confirmed)
    with open(os.path.join(repo, "listings", "README.md"), "w") as f:
        f.write("listings\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "seed")
    git(repo, "push", "-q", "origin", "HEAD:main")
    git(repo, "branch", "-q", "--set-upstream-to=origin/main", "main")
    return origin, repo


def origin_file(origin, path):
    r = subprocess.run(["git", "show", f"main:{path}"], cwd=origin, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None
