"""mesh.remember_names: the station's memory of node names.

Checks, without a node or a server:
  - a node with a name goes in; one with no name, or a bad id, does not
  - the same name again changes nothing (nothing to save or send)
  - a new name replaces the old and comes back as changed; t never goes back
  - past keep, the oldest go first, and are not reported as changed
  - a node dropped from the list keeps its entry

    python3 tests/mesh_names_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mesh  # noqa: E402

FAILS = 0


def check(name, got, want):
    global FAILS
    ok = got == want
    FAILS += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


def node(nid, short, long_, t=None, hw="HELTEC_V4"):
    return {"id": nid, "shortName": short, "longName": long_, "hwModel": hw, "last_heard": t}


book = {}
ch = mesh.remember_names(book, [node("!0000cafe", "RDG", "Ridge Relay", 100),
                                {"id": "!0000beef", "shortName": "", "longName": ""},
                                node("not-an-id", "X", "Bad"), node(None, "Y", "None")], now=500)
check("a named node goes in", book.get("!0000cafe"), {"short": "RDG", "long": "Ridge Relay", "hw": "HELTEC_V4", "t": 100})
check("unnamed and bad ids stay out", sorted(book), ["!0000cafe"])
check("  and only the new one is changed", sorted(ch), ["!0000cafe"])
check("the same name again: nothing changed",
      mesh.remember_names(book, [node("!0000cafe", "RDG", "Ridge Relay", 200)], now=600), {})
ch = mesh.remember_names(book, [node("!0000cafe", "RDG2", "Ridge Relay II", 50)], now=700)
check("a new name replaces the old", (book["!0000cafe"]["short"], book["!0000cafe"]["long"]), ("RDG2", "Ridge Relay II"))
check("  reported as changed", sorted(ch), ["!0000cafe"])
check("  and t does not go back", book["!0000cafe"]["t"], 100)
check("no last_heard: t is now", mesh.remember_names(book, [node("!0000dead", "TRK", "Old Truck")], now=800)["!0000dead"]["t"], 800)
check("a node gone from the list keeps its name", mesh.remember_names(book, [], now=900), {})
check("  still in the book", book["!0000dead"]["short"], "TRK")
ch = mesh.remember_names(book, [node("!00000001", "N1", "New one", 1000)], now=1000, keep=2)
check("past keep, the oldest go first", sorted(book), ["!00000001", "!0000dead"])
check("  the dropped one is not reported", sorted(ch), ["!00000001"])

sys.exit(1 if FAILS else 0)
