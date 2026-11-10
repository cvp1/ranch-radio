"""Winlink: Pat as the mail engine, as an add-on (winlink.py does the work).

Like Mesh it has its own panel (static/winlink.js) and keeps that panel's wire
protocol: the page sends {"t": "winlink_connect" | "winlink_stop" |
"winlink_prompt" | "winlink_listen" | "winlink_listen_stop"} (ROUTES) and gets {"t": "winlink", ...} back; the mailbox
is Pat's API passed through under /winlink/api/ (registered once, answering
503 while switched off).

What stays OUT of this add-on, on purpose: the transmit lane. Arming,
"winlink_arm" / "winlink_end", the codec level, the station-ID wait and
unkeying are the radio server's (server.py); a session reaches them over the
station's RemoteLane (station.py). The add-on only runs Pat, on the station.

Off = the shutdown path: the session (if any) is aborted, which disarms and
unkeys through the core, then Pat is stopped. Switching off is refused while
a session runs; stop the session first.

Config: built by the station from its station.json "winlink" block, as
{"engine_args": namespace, "lane_for": fn, "hf_modems": [...]}. Delete this
file and the Winlink panel goes away; the radios run on, their lanes idle.
"""

from addons import Addon


class WinlinkAddon(Addon):
    name = "winlink"
    group = "messaging"
    label = "Winlink"
    title = "Winlink mail (Pat)"
    page = False                 # its page is the Winlink panel (winlink.js)
    routes = ("winlink_connect", "winlink_stop", "winlink_prompt", "winlink_listen", "winlink_listen_stop",
              "winlink_listen_here")

    def __init__(self, host, opts):
        super().__init__(host, opts)
        from winlink import PatEngine
        if "engine_args" not in opts:
            raise ValueError("winlink needs engine_args (built by the host from its config)")
        # One engine for the add-on's life: its mailbox routes are registered
        # once, before the web server starts.
        self.engine = PatEngine(host, opts["engine_args"], lane_for=opts.get("lane_for"),
                                hf_modems=opts.get("hf_modems"))

    async def start(self):
        await self.engine.start()

    async def stop(self):
        await self.engine.stop()

    def can_stop(self):
        e = self.engine
        if e.session and not e.session.done():
            return "A Winlink session is running. Stop it first, then switch Winlink off."
        return None

    def http(self, app, authorised):
        self.engine.routes(app, authorised)

    def snapshot(self):
        if self.engine.running:
            return self.engine.snapshot()
        return {**self.engine.lane_state(), "off": True, "session": None}

    async def command(self, ws, verb, m):
        e = self.engine
        if verb == "winlink_connect":
            if m.get("peer"):               # P2P: a station, not a gateway (winlink.check_peer)
                await e.connect(ws, str(m["url"]), m.get("via") or None, peer=True)
            elif m.get("via"):
                await e.connect(ws, str(m["url"]), m.get("via") or None)
            else:
                await e.connect(ws, str(m["url"]))
        elif verb == "winlink_stop":
            await e.stop_session(ws)
        elif verb == "winlink_prompt":
            await e.answer_prompt(m["id"], m["value"])
        elif verb == "winlink_listen":          # a listen window (winlink.PatEngine.listen_start)
            if "hf" in m:                       # attended HF (listen_hf_start): a channel of the host's plan
                await e.listen_hf_start(ws, m.get("hf"))
            else:
                await e.listen_start(ws, m.get("hours"))
        elif verb == "winlink_listen_here":     # attended HF: the operator answers "still there?"
            e.listen_here(ws)
        elif verb == "winlink_listen_stop":
            await e.listen_stop(ws)
        else:
            await super().command(ws, verb, m)


ADDON = WinlinkAddon
