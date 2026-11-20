"""The P2P directory's maintainer intake: listing requests in Pat's inbox,
shown on the Winlink page, published to the directory repo with one click.

Only the station that maintains the directory has this. Host file:
winlink.directory.maintain = the path of a clone of the directory repo
(cvp1/p2p-directory) that can push to it. Nothing here runs on its own:

  - requests() reads Pat's inbox for messages whose subject names P2PDIR and
    returns one request per callsign, the newest (the older ones from the same
    station ride along and are archived with it). A station's newest minute
    holding messages that disagree is shown, never published: Pat dates by
    the minute and sorts ties arbitrarily, so nothing says which came last;
  - act(..., "publish") writes what the operator sent (a listing, `confirm` or
    `remove`), checks the whole directory with the repo's own build (--strict),
    commits, pushes, then moves that station's request messages to the archive;
  - act(..., "dismiss") only archives them;
  - edit(call, text, base) replaces a published listing's text (the
    maintainer's correction), checked and pushed the same way; the confirmed
    date stays, and an edit over a listing that changed since it was opened
    is refused.

Identity (the directory's SPEC.md): the callsign is the sender of a Winlink
message, never anything in the body; mail from an internet address (Pat's
Proto "SMTP") is never a request. The listing grammar is the repo's own parser
(p2pdir.py), so there is one parser, and the page shows the exact text that
will be published.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from urllib.parse import quote

import aiohttp

log = logging.getLogger("p2p_intake")

SUBJECT_RE = re.compile(r"\bp2pdir\b", re.I)
CALL_RE = re.compile(r"[A-Z0-9]{1,3}[0-9][A-Z]{1,4}")
MID_RE = re.compile(r"[A-Za-z0-9]{1,32}")
# 2026-11-18: one station sent 223 P2PDIR messages; with only the newest 50
# looked at, a valid listing from another station never showed, and Dismiss
# could not clear the flood. Pat's listing is the whole folder in one answer,
# so the bound is on what is read from it, not on pages.
MAX_SCAN = 2000             # P2PDIR messages looked at per read, newest first
MAX_REQUESTS = 50           # requests shown per read (one body fetch each)
MAX_TIED = 5                # same-minute messages per station read to see if they agree
MAX_BODY = 8192             # a listing is at most ~2 KB (SPEC.md); anything bigger is refused
RUN_TIMEOUT_S = 60          # each git / parser / build run
PAT_TIMEOUT_S = 10
# The repo's own tools run with the environment and user site-packages
# ignored, but NOT -I: isolated mode also drops the script's directory from
# sys.path, and build.py imports its sibling p2pdir.py (every Publish failed
# with ModuleNotFoundError, live 2026-11-15).
PY_FLAGS = ("-E", "-s", "-B")


class IntakeError(Exception):
    pass


def sender_call(msg):
    """(CALL, None) for a message from a Winlink callsign, else (None, why)."""
    frm = msg.get("From") or {}
    proto, addr = str(frm.get("Proto") or ""), str(frm.get("Addr") or "")
    if proto:
        return None, f"sent from an internet address ({proto}), not a Winlink callsign"
    call = addr.upper().split("-")[0]
    if not CALL_RE.fullmatch(call):
        return None, f"the sender {addr!r} is not a callsign"
    return call, None


def kind_of(body):
    first = next((ln.strip().lower() for ln in body.splitlines() if ln.strip()), "")
    return first if first in ("confirm", "remove") else "listing"


def utc_date(pat_date):
    """'2026-11-15T16:58:00-07:00' -> '2026-11-15' (UTC); today if unreadable."""
    try:
        return datetime.fromisoformat(str(pat_date).replace("Z", "+00:00")).astimezone(UTC).date().isoformat()
    except ValueError:
        return datetime.now(UTC).date().isoformat()


def when(msg):
    """A message's Date as an aware UTC datetime (oldest possible if unreadable):
    Pat's Date carries the sender's offset, so the strings don't sort."""
    try:
        d = datetime.fromisoformat(str(msg.get("Date")).replace("Z", "+00:00"))
        return d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


def listing_text(body):
    """The body as the listing file holds it: LF line ends, one final newline."""
    lines = [ln.rstrip() for ln in body.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


class Intake:
    def __init__(self, repo, python=None):
        self.repo = os.path.abspath(os.path.expanduser(repo))
        self.python = python or sys.executable
        self.lock = asyncio.Lock()

    def why_not(self):
        """None when the clone looks usable, else why not."""
        for p in ("p2pdir.py", "build.py", "listings", ".git"):
            if not os.path.exists(os.path.join(self.repo, p)):
                return f"{self.repo} is not a clone of the directory repo (no {p})"
        return None

    def _path(self, call, ext):
        return os.path.join(self.repo, "listings", f"{call}.{ext}")

    def current(self, call):
        try:
            with open(self._path(call, "txt"), encoding="utf-8") as f:
                txt = f.read()
            with open(self._path(call, "json"), encoding="utf-8") as f:
                meta = json.load(f)
            return {"text": txt, "confirmed": meta.get("confirmed")}
        except (OSError, ValueError):
            return None

    async def _run(self, *argv, stdin=None, cwd=None):
        p = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd or self.repo, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(p.communicate(stdin.encode() if stdin else None), RUN_TIMEOUT_S)
        except TimeoutError:
            p.kill()
            await p.wait()
            raise IntakeError(f"{os.path.basename(argv[1] if argv[0] == self.python else argv[0])} "
                              f"took over {RUN_TIMEOUT_S} s") from None
        return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def _git(self, *args):
        rc, out, err = await self._run("git", *args)
        if rc:
            raise IntakeError(f"git {args[0]}: {(err or out).strip().splitlines()[-1] if (err or out).strip() else rc}")
        return out.strip()

    async def parse(self, call, body):
        """(parsed listing, None) by the repo's parser, or (None, its reason)."""
        rc, out, err = await self._run(self.python, *PY_FLAGS, "p2pdir.py", "-", "--call", call, stdin=body)
        if rc == 0:
            try:
                return json.loads(out), None
            except ValueError:
                return None, "the parser's answer was not JSON"
        return None, (err or out).strip().removeprefix("rejected: ") or f"rejected (rc {rc})"

    # --- what the page shows

    async def requests(self, http, pat):
        """One request per callsign, newest message first. `pat` is Pat's base URL."""
        return (await self.scan(http, pat))[0]

    async def scan(self, http, pat):
        """(requests, note): note says what a bound left out, else None."""
        listing = await _pat_json(http, f"{pat}/api/mailbox/in")
        cand = [m for m in listing if isinstance(m, dict) and SUBJECT_RE.search(str(m.get("Subject") or ""))
                and MID_RE.fullmatch(str(m.get("MID") or ""))]
        # Newest first; MID breaks a tie only so every read agrees, it is not an order.
        cand.sort(key=lambda m: (when(m), str(m["MID"])), reverse=True)
        notes = []
        if len(cand) > MAX_SCAN:
            notes.append(f"{len(cand)} listing messages in the inbox; only the newest {MAX_SCAN} were read")
            cand = cand[:MAX_SCAN]
        by_call, by_sender = {}, {}
        for m in cand:
            call, why = sender_call(m)
            if call:
                by_call.setdefault(call, []).append(m)
                continue
            addr = str((m.get("From") or {}).get("Addr") or "")
            r = by_sender.setdefault(addr, {"mid": m["MID"], "date": m.get("Date"), "from": addr,
                                            "kind": "refused", "error": why, "mids": []})
            r["mids"].append(m["MID"])
        out = []
        for call, msgs in by_call.items():
            newest = msgs[0]
            body = await _pat_body(http, pat, newest["MID"])
            req = {"call": call, "mid": newest["MID"], "date": newest.get("Date"),
                   "confirmed": utc_date(newest.get("Date")), "mids": [m["MID"] for m in msgs],
                   "current": self.current(call)}
            tied = [m for m in msgs if when(m) == when(newest)]
            clash = await self._clash(http, pat, call, newest, body, tied)
            if clash:
                req.update(kind=kind_of(body), error=clash)
            elif len(body.encode()) > MAX_BODY:
                req.update(kind="refused", error=f"the message is over {MAX_BODY} bytes")
            else:
                kind = kind_of(body)
                req["kind"] = kind
                if kind == "listing":
                    req["text"] = listing_text(body)
                    parsed, why = await self.parse(call, req["text"])
                    req.update(parsed=parsed) if parsed else req.update(error=why)
                elif not req["current"]:
                    req["error"] = f"{call} isn't listed, so there is nothing to {kind}"
            out.append(req)
        out.sort(key=lambda r: (when(r), r["mid"]), reverse=True)
        out += sorted(by_sender.values(), key=lambda r: (when(r), r["mid"]), reverse=True)
        if len(out) > MAX_REQUESTS:
            notes.append(f"{len(out)} stations have requests; the newest {MAX_REQUESTS} are shown")
            out = out[:MAX_REQUESTS]
        return out, "; ".join(notes) or None

    async def _clash(self, http, pat, call, newest, body, tied):
        """Why a station's newest minute can't be published, or None. Pat dates
        to the minute and its listing sorts a tie arbitrarily, so when two
        messages in that minute say different things there is no telling which
        the operator sent last: show it, publish nothing, let Dismiss clear it."""
        if len(tied) < 2:
            return None
        n = len(tied)
        if n > MAX_TIED:
            return (f"{n} messages from {call} in the same minute; there is no telling which came last. "
                    f"Dismiss them and ask for one more")
        said = {(kind_of(body), listing_text(body))}
        for m in tied:
            if m is not newest:
                b = await _pat_body(http, pat, m["MID"])
                said.add((kind_of(b), listing_text(b)))
        if len(said) == 1:
            return None
        kinds = sorted({k for k, _ in said})
        return (f"{n} messages from {call} in the same minute disagree ({', '.join(kinds)}); there is no telling "
                f"which came last. Dismiss them and ask for one more")

    # --- what a click does

    async def act(self, http, pat, mid, do):
        """Publish or dismiss the request whose newest message is `mid`."""
        if do not in ("publish", "dismiss"):
            raise IntakeError("do must be publish or dismiss")
        async with self.lock:
            reqs = await self.requests(http, pat)
            req = next((r for r in reqs if r["mid"] == mid), None)
            if req is None:
                raise IntakeError("that request is gone or a newer message from the station arrived; reload")
            commit = None
            if do == "publish":
                if req.get("error") or req["kind"] == "refused":
                    raise IntakeError(req.get("error") or "this request can't be published")
                commit = await self._publish(req)
            archived = []
            for m in req["mids"]:
                if await _pat_move(http, pat, m, "archive"):
                    archived.append(m)
            log.info("p2p intake: %s %s (%s)%s, archived %d", do, req.get("call") or req.get("from"),
                     req["kind"], f" as {commit[:7]}" if commit else "", len(archived))
            return {"ok": True, "call": req.get("call"), "kind": req["kind"], "commit": commit,
                    "archived": archived}

    async def _ready(self):
        """Check the clone, bring it up to date; the commit a change starts from."""
        why = self.why_not()
        if why:
            raise IntakeError(why)
        if await self._git("status", "--porcelain"):
            raise IntakeError(f"{self.repo} has uncommitted changes; publish refused")
        if await self._git("rev-parse", "--abbrev-ref", "HEAD") != "main":
            raise IntakeError(f"{self.repo} is not on main")
        await self._git("pull", "-q", "--ff-only")
        return await self._git("rev-parse", "HEAD")

    async def _commit_push(self, start, subject, body, change):
        """Apply `change` (writes and stages files), check the whole directory
        with the repo's build, commit, push. Any failure: back to `start`."""
        try:
            await change()
            out = tempfile.mkdtemp(prefix="p2pdir-build-")
            try:
                rc, o, e = await self._run(self.python, *PY_FLAGS, "build.py", "--strict", "--out", out)
            finally:
                shutil.rmtree(out, ignore_errors=True)
            if rc:
                raise IntakeError(f"the directory build refused it: {(e or o).strip()[-300:]}")
            await self._git("commit", "-q", "-m", subject, "-m", body)
            await self._git("push", "-q", "origin", "HEAD:main")
        except BaseException:
            # Back to where this started: the tree was clean and on `start`.
            await self._run("git", "reset", "-q", "--hard", start)
            raise
        return await self._git("rev-parse", "HEAD")

    async def _publish(self, req):
        start = await self._ready()
        call, kind = req["call"], req["kind"]
        txt, meta = self._path(call, "txt"), self._path(call, "json")
        rel = [os.path.relpath(txt, self.repo), os.path.relpath(meta, self.repo)]

        async def change():
            if kind == "remove":
                await self._git("rm", "-q", "--", *rel)
                return
            if kind == "listing":
                parsed, why = await self.parse(call, req["text"])
                if not parsed:
                    raise IntakeError(why)
                with open(txt, "w", encoding="utf-8") as f:
                    f.write(req["text"])
            elif not os.path.exists(txt):
                raise IntakeError(f"{call} isn't listed, so there is nothing to confirm")
            with open(meta, "w", encoding="utf-8") as f:
                json.dump({"confirmed": req["confirmed"], "source": "maintainer"}, f)
                f.write("\n")
            await self._git("add", "--", *rel)

        what = {"listing": "list", "confirm": "confirm", "remove": "remove"}[kind]
        return await self._commit_push(start, f"listings: {what} {call}",
                                       f"Winlink message {req['mid']} from {call}, {req.get('date')}.", change)

    # --- the maintainer editing a published listing

    def listed(self):
        """The callsigns listed in the clone, sorted."""
        try:
            names = os.listdir(os.path.join(self.repo, "listings"))
        except OSError:
            return []
        return sorted(n[:-4] for n in names if n.endswith(".txt") and CALL_RE.fullmatch(n[:-4]))

    def listing(self, call):
        """{call, text, confirmed} of a published listing, or IntakeError."""
        call = str(call or "").upper()
        if not CALL_RE.fullmatch(call):
            raise IntakeError("not a callsign")
        cur = self.current(call)
        if not cur:
            raise IntakeError(f"{call} isn't listed")
        return {"call": call, **cur}

    async def edit(self, call, text, base):
        """Replace a published listing's text. `base` is the text the editor
        opened; if the listing changed since, the edit is refused. The
        confirmed date stays: an edit is the maintainer's, not the operator
        confirming."""
        call = str(call or "").upper()
        if not CALL_RE.fullmatch(call):
            raise IntakeError("not a callsign")
        if not isinstance(text, str) or not isinstance(base, str):
            raise IntakeError("expected the listing's text")
        if len(text.encode()) > MAX_BODY:
            raise IntakeError(f"the listing is over {MAX_BODY} bytes")
        text = listing_text(text)
        async with self.lock:
            start = await self._ready()
            cur = self.current(call)
            if not cur:
                raise IntakeError(f"{call} isn't listed, so there is nothing to edit")
            if cur["text"] != base:
                raise IntakeError(f"{call}'s listing changed since you opened it; open it again")
            if text == cur["text"]:
                raise IntakeError("nothing changed")
            parsed, why = await self.parse(call, text)
            if not parsed:
                raise IntakeError(why)
            txt = self._path(call, "txt")

            async def change():
                with open(txt, "w", encoding="utf-8") as f:
                    f.write(text)
                await self._git("add", "--", os.path.relpath(txt, self.repo))

            commit = await self._commit_push(
                start, f"listings: edit {call}",
                "Edited by the maintainer on the station's Winlink page; the confirmed date is unchanged.",
                change)
            log.info("p2p intake: edit %s as %s", call, commit[:7])
            return {"ok": True, "call": call, "commit": commit}


async def _pat_json(http, url):
    try:
        async with http.get(url, timeout=aiohttp.ClientTimeout(total=PAT_TIMEOUT_S)) as r:
            if r.status >= 400:
                raise IntakeError(f"Pat answered {r.status} for {url.rsplit('/api/', 1)[-1]}")
            return await r.json(content_type=None) or []
    except (TimeoutError, aiohttp.ClientError, ValueError) as e:
        raise IntakeError(f"Pat not available: {e}") from None


async def _pat_body(http, pat, mid):
    full = await _pat_json(http, f"{pat}/api/mailbox/in/{quote(mid, safe='')}")
    return str(full.get("Body") or "") if isinstance(full, dict) else ""


async def _pat_move(http, pat, mid, box):
    try:
        async with http.post(f"{pat}/api/mailbox/{box}", timeout=aiohttp.ClientTimeout(total=PAT_TIMEOUT_S),
                             headers={"X-Pat-SourcePath": f"/api/mailbox/in/{quote(mid, safe='')}"}) as r:
            return r.status < 400
    except (TimeoutError, aiohttp.ClientError):
        return False
