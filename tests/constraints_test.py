"""constraints.txt holds every install to the tested versions: each package
requirements.txt asks for has an exact pin there that meets its floor, and
install.sh, CI and `ranch-radio update` all pass it to pip. Offline; no pip.

    python3 tests/constraints_test.py
"""
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ranch_radio import updater  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n      got  {got!r}\n      want {want!r}"))


def ver(v):
    return tuple(int(p) for p in re.findall(r"\d+", v)[:4])


def lines(path):
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            yield line


def key(name):
    return re.sub(r"[-_.]+", "-", name).lower()


pins, bad = {}, []
for line in lines(ROOT / "constraints.txt"):
    m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][0-9A-Za-z.]*)", line)
    if not m:
        bad.append(line)
        continue
    if key(m.group(1)) in pins:
        bad.append(f"{line} (pinned twice)")
    pins[key(m.group(1))] = m.group(2)
check("every constraint is one exact pin (name==version)", bad, [])

unpinned, below = [], []
for line in lines(ROOT / "requirements.txt"):
    req = line.partition(";")[0].strip()
    m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)", req)
    name, specs = m.group(1), [s.strip() for s in m.group(3).split(",") if s.strip()]
    pin = pins.get(key(name))
    if pin is None:
        unpinned.append(name)
        continue
    for s in specs:
        op, v = re.fullmatch(r"(>=|==|>|<=|<|!=|~=)\s*(\S+)", s).groups()
        ok = {">=": ver(pin) >= ver(v), ">": ver(pin) > ver(v), "==": ver(pin) == ver(v),
              "<=": ver(pin) <= ver(v), "<": ver(pin) < ver(v), "!=": ver(pin) != ver(v),
              "~=": ver(pin) >= ver(v)}[op]
        if not ok:
            below.append(f"{name}=={pin} vs {s}")
check("every requirement has a pin", unpinned, [])
check("every pin meets its requirement", below, [])
# the comparison above can fail: a pin under a raised floor is caught
check("  (a 3.14.4 pin under >=3.15 would be caught)", ver("3.14.4") >= ver("3.15"), False)

# ---------------------------------------------------------------- every pip that installs requirements.txt
install = (ROOT / "install.sh").read_text()
ci = (ROOT / ".github/workflows/ci.yml").read_text()
for where, text in (("install.sh", install), ("ci.yml", ci)):
    calls = [ln.strip() for ln in text.splitlines() if "-r requirements.txt" in ln]
    check(f"{where} installs requirements.txt", bool(calls), True)
    check(f"{where}: each such pip passes -c constraints.txt",
          [c for c in calls if "-c constraints.txt" not in c], [])

args = updater.install_args(ROOT)
check("update pips with -r requirements.txt -c constraints.txt",
      (args[args.index("-r") + 1], args[args.index("-c") + 1] if "-c" in args else None),
      (str(ROOT / "requirements.txt"), str(ROOT / "constraints.txt")))
with tempfile.TemporaryDirectory() as d:
    (Path(d) / "requirements.txt").write_text("aiohttp>=3.14.4\n")
    check("  an older checkout without constraints.txt: no -c", "-c" in updater.install_args(d), False)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
