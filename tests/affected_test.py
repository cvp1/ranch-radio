"""tests/affected.py: which runs a change touches.

  - a module's own tests, by import and by name (civ.py -> civ_safety_test and
    the ic7300 rerun; addons/logbook.py -> its addon and page tests)
  - a fake a test spawns by path (tests/fake_civ.py) reaches that test
  - a page asset reaches every page test, and an add-on's script its own tests
  - a file nothing tests (README.md) picks nothing; the harness picks everything
  - prose is not an edge: the catalog names every add-on file, and a test's
    docstring names its sibling test; neither may pull in the suite
  - the run list comes from tests/run_all.sh itself, reruns included

    python3 tests/affected_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import affected  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


names = [n for n, _ in affected.runs()]
check("the run list has every tests/*_test.py and the reruns",
      ("civ_safety_test" in names, "civ_safety_ic7300" in names, "rigctl_contract_g90" in names, "winlink_lane_g90" in names),
      (True, True, True, True))

civ = affected.select(["civ.py"])
check("civ.py: its safety test and the ic7300 rerun", ("civ_safety_test" in civ, "civ_safety_ic7300" in civ), (True, True))
check("  not a test that spawns no radio server", [n for n in civ if n in ("logbook_addon_test", "hostfile_safety_test", "ci_shard_test")], [])

lb = affected.select(["addons/logbook.py"])
check("addons/logbook.py: its own tests", ("logbook_addon_test" in lb, "logbook_page_test" in lb), (True, True))
check("  a handful, not the suite (the catalog's prose is not an edge)", len(lb) < 8, True)
check("  never civ_safety_test", "civ_safety_test" in lb, False)

check("a fake reaches the test that spawns it", "civ_safety_test" in affected.select(["tests/fake_civ.py"]), True)

app = affected.select(["static/app.js"])
page = [n for n, _ in affected.runs() if n.endswith("_page_test")]
check("a page asset: every page test", [n for n in page if n not in app], [])
check("  and not a test that opens no page", [n for n in app if n in ("aprs_test", "ax25_link_test", "qmx_test", "igate_test")], [])
uv = affected.select(["static/addons/uvpro.js"])
check("an add-on's script: its own tests too", ("uvpro_test" in uv, "uvpro_page_test" in uv), (True, True))

check("README.md picks nothing", affected.select(["README.md"]), {})
check("the harness picks everything", len(affected.select(["tests/run_all.sh"])), len(names))
check("so does requirements.txt", len(affected.select(["requirements.txt"])), len(names))
check("a module that is gone still picks by name (no hold.py, but hold_test)", "hold_test" in affected.select(["hold.py"]), True)
check("a test naming another test is prose, not an edge", "aprs_msg_test" in affected.select(["addons/logbook.py"]), False)
check("the reason is the file that picked it", lb.get("logbook_addon_test"), "addons/logbook.py")

print(f"\n{'all passed' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
