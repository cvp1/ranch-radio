"""The P2P directory's maintainer intake: listing requests in Pat's inbox,
shown on the Winlink page, published to the directory repo with one click.

Only the station that maintains the directory has this. Host file:
winlink.directory.maintain = the path of a clone of the directory repo
(cvp1/p2p-directory) that can push to it. Nothing here runs on its own:

  - requests() reads Pat's inbox for messages whose subject names P2PDIR and
    returns one request per callsign, the newest (the older ones from the same
    station ride along and are archived with it);
  - act(..., "publish") writes what the operator sent (a listing, `confirm` or
    `remove`), checks the whole directory with the repo's own build (--strict),
    commits, pushes, then moves that station's request messages to the archive;
  - act(..., "dismiss") only archives them.

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
MAX_MESSAGES = 50           # inbox messages looked at per read
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
        listing = await _pat_json(http, f"{pat}/api/mailbox/in")
        cand = [m for m in listing if isinstance(m, dict) and SUBJECT_RE.search(str(m.get("Subject") or ""))
                and MID_RE.fullmatch(str(m.get("MID") or ""))]
        cand.sort(key=lambda m: str(m.get("Date") or ""), reverse=True)
        cand = cand[:MAX_MESSAGES]
        by_call, refused = {}, []
        for m in cand:
            call, why = sender_call(m)
            if not call:
                refused.append({"mid": m["MID"], "date": m.get("Date"), "from": (m.get("From") or {}).get("Addr"),
                                "kind": "refused", "error": why, "mids": [m["MID"]]})
                continue
            by_call.setdefault(call, []).append(m)
        out = []
        for call, msgs in by_call.items():
            newest = msgs[0]
            full = await _pat_json(http, f"{pat}/api/mailbox/in/{quote(newest['MID'], safe='')}")
            body = str(full.get("Body") or "") if isinstance(full, dict) else ""
            req = {"call": call, "mid": newest["MID"], "date": newest.get("Date"),
                   "confirmed": utc_date(newest.get("Date")), "mids": [m["MID"] for m in msgs],
                   "current": self.current(call)}
            if len(body.encode()) > MAX_BODY:
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
        out.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
        return out + refused

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

    async def _publish(self, req):
        why = self.why_not()
        if why:
            raise IntakeError(why)
        if await self._git("status", "--porcelain"):
            raise IntakeError(f"{self.repo} has uncommitted changes; publish refused")
        if await self._git("rev-parse", "--abbrev-ref", "HEAD") != "main":
            raise IntakeError(f"{self.repo} is not on main")
        await self._git("pull", "-q", "--ff-only")
        start = await self._git("rev-parse", "HEAD")
        call, kind = req["call"], req["kind"]
        txt, meta = self._path(call, "txt"), self._path(call, "json")
        rel = [os.path.relpath(txt, self.repo), os.path.relpath(meta, self.repo)]
        try:
            if kind == "remove":
                await self._git("rm", "-q", "--", *rel)
            else:
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
            out = tempfile.mkdtemp(prefix="p2pdir-build-")
            try:
                rc, o, e = await self._run(self.python, *PY_FLAGS, "build.py", "--strict", "--out", out)
            finally:
                shutil.rmtree(out, ignore_errors=True)
            if rc:
                raise IntakeError(f"the directory build refused it: {(e or o).strip()[-300:]}")
            what = {"listing": "list", "confirm": "confirm", "remove": "remove"}[kind]
            await self._git("commit", "-q", "-m", f"listings: {what} {call}",
                            "-m", f"Winlink message {req['mid']} from {call}, {req.get('date')}.")
            await self._git("push", "-q", "origin", "HEAD:main")
        except BaseException:
            # Back to where this started: the tree was clean and on `start`.
            await self._run("git", "reset", "-q", "--hard", start)
            raise
        return await self._git("rev-parse", "HEAD")


async def _pat_json(http, url):
    try:
        async with http.get(url, timeout=aiohttp.ClientTimeout(total=PAT_TIMEOUT_S)) as r:
            if r.status >= 400:
                raise IntakeError(f"Pat answered {r.status} for {url.rsplit('/api/', 1)[-1]}")
            return await r.json(content_type=None) or []
    except (TimeoutError, aiohttp.ClientError, ValueError) as e:
        raise IntakeError(f"Pat not available: {e}") from None


async def _pat_move(http, pat, mid, box):
    try:
        async with http.post(f"{pat}/api/mailbox/{box}", timeout=aiohttp.ClientTimeout(total=PAT_TIMEOUT_S),
                             headers={"X-Pat-SourcePath": f"/api/mailbox/in/{quote(mid, safe='')}"}) as r:
            return r.status < 400
    except (TimeoutError, aiohttp.ClientError):
        return False
