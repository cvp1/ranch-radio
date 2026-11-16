"""The P2P directory's maintainer intake (p2p_intake). Nothing goes on air,
nothing reaches GitHub: the directory repo is a scratch clone of a scratch
bare repo, its parser and build are stand-ins that keep the real ones'
contract, and Pat is a small in-process stand-in serving the inbox.

  sender: a callsign from Pat's From (SSID dropped); an internet sender and
          a non-callsign refused, whatever the body says
  requests: only P2PDIR subjects; newest per callsign, older ones ride along;
            a parser rejection shown with its reason; confirm/remove of a
            station not listed refused; the current listing shown
  publish: the listing file holds exactly the text the page showed, the
           .json the message's UTC date; one commit pushed; the request
           messages archived; a second station's messages untouched
  confirm / remove: the date moved / both files gone, pushed
  refusals leave everything as it was: a dirty clone, a build that fails
  (the commit is undone, nothing pushed, nothing archived), a stale MID
  dismiss: archives only, nothing committed

    .venv/bin/python tests/p2p_intake_test.py
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

import p2p_intake as I  # noqa: E402
import p2pdir_repo as R  # noqa: E402
import testkit  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {name:<66} got {got!r}" + ("" if ok else f" want {want!r}"))


# --- unit: who sent it

def frm(addr, proto=""):
    return {"From": {"Proto": proto, "Addr": addr}}


check("sender: a callsign", I.sender_call(frm("K7NGS")), ("K7NGS", None))
check("sender: SSID dropped, upper-cased", I.sender_call(frm("k7ngs-10")), ("K7NGS", None))
check("sender: internet address refused", I.sender_call(frm("k7ngs@winlink.org", "SMTP"))[0], None)
check("sender: not a callsign refused", I.sender_call(frm("SERVICE"))[0], None)
check("sender: internet sender spelled like a callsign refused", I.sender_call(frm("K7NGS", "SMTP"))[0], None)
check("kind: listing / confirm / remove", [I.kind_of(b) for b in ("P2PDIR 1\n", "\n Confirm \n", "remove")],
      ["listing", "confirm", "remove"])
check("date: Pat's local time -> UTC date", I.utc_date("2026-11-15T20:58:00-07:00"), "2026-11-16")
check("listing text: CRLF -> LF, one final newline", I.listing_text("P2PDIR 1\r\ngrid: CN86 \r\n\r\n"),
      "P2PDIR 1\ngrid: CN86\n")

# --- the directory repo: a bare origin and a clone with stand-in tools

git = R.git
origin, repo = R.make({"N0OLD": ("P2PDIR 1\ngrid: DM79\nentry: 145090 PACKET 1200 sked weekends\n", "2026-01-01")})


def origin_head():
    return git(origin, "rev-parse", "main")


def origin_file(path):
    return R.origin_file(origin, path)


# --- a stand-in Pat: the inbox, message bodies, and moves

def msg(mid, date, addr, subject, body, proto=""):
    return {"MID": mid, "Date": date, "From": {"Proto": proto, "Addr": addr}, "To": [{"Proto": "", "Addr": "N9ABC"}],
            "Cc": None, "Subject": subject, "Body": body, "BodyHTML": "", "Files": [], "P2POnly": False, "Unread": True}


INBOX = [
    msg("AAA1", "2026-11-15T16:54:00-07:00", "K7NGS", "P2PDIR listing",
        "P2PDIR 1\r\ngrid: CN86\r\nentry: 14110 VARA_HF 2300 auto listen window 2h\r\n"),
    msg("AAA2", "2026-11-15T16:55:00-07:00", "K7NGS", "P2PDIR listing", "confirm\r\n"),
    msg("AAA3", "2026-11-15T16:58:00-07:00", "K7NGS", "P2PDIR listing",
        "P2PDIR 1\r\ngrid: CN86\r\nentry: 14300 VARA_HF 2300 auto listen window 2h\r\n"),
    msg("BBB1", "2026-11-14T10:00:00-07:00", "W1BAD", "p2pdir listing", "hello there\r\n"),
    msg("CCC1", "2026-11-14T11:00:00-07:00", "N0OLD", "Re: P2PDIR listing", "confirm\r\n"),
    msg("DDD1", "2026-11-14T12:00:00-07:00", "N0GONE", "P2PDIR listing", "remove\r\n"),
    msg("EEE1", "2026-11-14T13:00:00-07:00", "k7ngs@winlink.org", "P2PDIR listing",
        "P2PDIR 1\r\ngrid: AA00\r\nentry: 7000 VARA_HF 500 auto evil\r\n", proto="SMTP"),
    msg("FFF1", "2026-11-14T14:00:00-07:00", "N0DAJ", "Lunch?", "P2PDIR 1\r\n"),
]
boxes = {"in": [dict(m) for m in INBOX], "archive": []}


async def pat_list(request):
    return web.json_response([{**m, "Body": ""} for m in boxes[request.match_info["box"]]])


async def pat_msg(request):
    m = next((m for m in boxes[request.match_info["box"]] if m["MID"] == request.match_info["mid"]), None)
    return web.json_response(m) if m else web.Response(status=404)


async def pat_move(request):
    src = request.headers["X-Pat-SourcePath"].strip("/").split("/")
    m = next((m for m in boxes[src[2]] if m["MID"] == src[3]), None)
    if not m:
        return web.Response(status=404)
    boxes[src[2]].remove(m)
    boxes[request.match_info["box"]].append(m)
    return web.Response(status=200)


async def main():
    app = web.Application()
    app.router.add_get("/api/mailbox/{box}", pat_list)
    app.router.add_get("/api/mailbox/{box}/{mid}", pat_msg)
    app.router.add_post("/api/mailbox/{box}", pat_move)
    runner = web.AppRunner(app)
    await runner.setup()
    port = testkit.free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    pat = f"http://127.0.0.1:{port}"
    it = I.Intake(repo)
    async with aiohttp.ClientSession() as http:
        check("clone usable", it.why_not(), None)
        reqs = await it.requests(http, pat)
        by = {r.get("call") or r.get("from"): r for r in reqs}
        check("requests: P2PDIR subjects only, one per station, the refused one too",
              sorted(by), sorted(["K7NGS", "W1BAD", "N0OLD", "N0GONE", "k7ngs@winlink.org"]))
        k = by["K7NGS"]
        check("K7NGS: the newest message is the request", (k["mid"], k["kind"]), ("AAA3", "listing"))
        check("K7NGS: older messages ride along", sorted(k["mids"]), ["AAA1", "AAA2", "AAA3"])
        check("K7NGS: the text shown is what will be written",
              k["text"], "P2PDIR 1\ngrid: CN86\nentry: 14300 VARA_HF 2300 auto listen window 2h\n")
        check("K7NGS: parsed, with the parser's flag", k["parsed"]["entries"][0]["flags"], ["hf-auto-outside-97.221b"])
        check("K7NGS: not listed yet; confirmed = the message's UTC date", (k["current"], k["confirmed"]),
              (None, "2026-11-15"))
        check("W1BAD: the parser's reason shown", by["W1BAD"].get("error", "").startswith("no-header"), True)
        check("N0OLD confirm: the current listing shown", by["N0OLD"]["current"]["confirmed"], "2026-01-01")
        check("N0GONE remove: not listed, refused", "isn't listed" in by["N0GONE"].get("error", ""), True)
        check("internet sender: refused as such", (by["k7ngs@winlink.org"]["kind"],
              "internet address" in by["k7ngs@winlink.org"]["error"]), ("refused", True))

        async def refused(mid, do, why_part, name, status=""):
            before, inbox = origin_head(), len(boxes["in"])
            try:
                await it.act(http, pat, mid, do)
                check(name, "published", "refused")
            except I.IntakeError as e:
                check(name, (why_part in str(e), origin_head() == before, len(boxes["in"]) == inbox,
                             git(repo, "status", "--porcelain")), (True, True, True, status))

        await refused("BBB1", "publish", "no-header", "publish a rejected listing: refused, nothing changed")
        await refused("EEE1", "publish", "internet address", "publish an internet sender: refused")
        await refused("AAA1", "publish", "gone", "publish an older MID (a newer one exists): refused")
        await refused("AAA3", "launch", "publish or dismiss", "an unknown action: refused")

        # a dirty clone is never published over
        with open(os.path.join(repo, "stray.txt"), "w") as f:
            f.write("x")
        await refused("AAA3", "publish", "uncommitted", "dirty clone: refused, the stray file left alone",
                      status="?? stray.txt")
        os.remove(os.path.join(repo, "stray.txt"))

        # a build that fails undoes the commit: nothing pushed, nothing archived
        boxes["in"].append(msg("GGG1", "2026-11-13T09:00:00-07:00", "N0FAIL", "P2PDIR listing",
                               "P2PDIR 1\r\ngrid: DM79\r\nentry: 7103 VARA_HF 500 attended BUILDFAIL\r\n"))
        head = git(repo, "rev-parse", "HEAD")
        await refused("GGG1", "publish", "build refused", "the build fails: refused")
        check("  ...the clone is back where it was, no file left", (git(repo, "rev-parse", "HEAD") == head,
              os.path.exists(os.path.join(repo, "listings", "N0FAIL.txt"))), (True, False))

        # publish K7NGS
        before = origin_head()
        res = await it.act(http, pat, "AAA3", "publish")
        check("publish: ok, a commit", (res["ok"], res["call"], len(res["commit"] or "")), (True, "K7NGS", 40))
        check("publish: exactly one commit pushed", git(origin, "rev-list", "--count", f"{before}..main"), "1")
        check("publish: the listing file is the text shown", origin_file("listings/K7NGS.txt"), k["text"])
        check("publish: the .json", json.loads(origin_file("listings/K7NGS.json")),
              {"confirmed": "2026-11-15", "source": "maintainer"})
        check("publish: the commit names the message", "AAA3" in git(origin, "log", "-1", "--format=%B", "main"), True)
        check("publish: all three K7NGS messages archived", sorted(m["MID"] for m in boxes["archive"]),
              ["AAA1", "AAA2", "AAA3"])
        check("publish: the other stations' messages still in the inbox",
              sorted(m["MID"] for m in boxes["in"]), ["BBB1", "CCC1", "DDD1", "EEE1", "FFF1", "GGG1"])

        # confirm moves the date, keeps the text
        old_txt = origin_file("listings/N0OLD.txt")
        await it.act(http, pat, "CCC1", "publish")
        check("confirm: date moved, text kept", (json.loads(origin_file("listings/N0OLD.json"))["confirmed"],
              origin_file("listings/N0OLD.txt") == old_txt), ("2026-11-14", True))

        # remove deletes both files
        boxes["in"].append(msg("HHH1", "2026-11-16T09:00:00-07:00", "K7NGS", "P2PDIR listing", "remove\r\n"))
        r = next(r for r in await it.requests(http, pat) if r.get("call") == "K7NGS")
        check("remove: the current listing shown", r["current"]["confirmed"], "2026-11-15")
        await it.act(http, pat, "HHH1", "publish")
        check("remove: both files gone", (origin_file("listings/K7NGS.txt"), origin_file("listings/K7NGS.json")),
              (None, None))

        # dismiss archives only
        before = origin_head()
        res = await it.act(http, pat, "BBB1", "dismiss")
        check("dismiss: no commit, archived", (res["commit"], origin_head() == before,
              "BBB1" in [m["MID"] for m in boxes["archive"]]), (None, True, True))
        check("the clone is clean after all of it", git(repo, "status", "--porcelain"), "")
    await runner.cleanup()


asyncio.run(main())
print("PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
