"""Roster add-on: who JTDX is hearing now, marked by what you still need
(new DXCC, new on this band or mode, new grid, not yet confirmed, new call),
with a click that asks JTDX to answer that station, as a double-click in
JTDX (or GridTracker's call roster) would.

The roster comes from the logbook sync (logsync.py, a station module): its
intake already hears JTDX, and its ledger is what "worked" and "confirmed"
mean. This tab shows it and passes the click on; switching the tab off stops
the pushes, never the sync. The click sends JTDX a Reply only: Ranch Radio
never enables or halts transmit, sends free text or touches the radio. Whether
JTDX then transmits is JTDX's own setting for a double-click.

Options (the host file's addons block):

    "roster": {"enabled": true}
"""
from . import Addon


class Roster(Addon):
    name = "roster"
    label = "Roster"
    title = "Who JTDX hears now, by what you still need; click to answer"
    group = "tools"
    css = True

    def _sync(self):
        return getattr(self.host, "logsync", None)

    def snapshot(self):
        ls = self._sync()
        if ls is None:
            return {"error": "the logbook sync isn't running on this station, so there's no roster"}
        return {"roster": ls.roster_view()}

    def push(self):
        """The sync has new decodes (logsync's roster callback, via the station)."""
        ls = self._sync()
        if ls is not None:
            self.emit({"roster": ls.roster_view()})

    async def command(self, ws, verb, m):
        ls = self._sync()
        if verb != "reply":
            return await super().command(ws, verb, m)
        if ls is None:
            self.notice("The logbook sync isn't running, so the roster can't answer.", "warn", ws)
            return
        why = ls.reply(str(m.get("key", "")), done=lambda ok, msg: self.notice(f"Roster: {msg}", "info" if ok else "warn", ws))
        if why:
            self.notice(f"Roster: {why}", "warn", ws)


ADDON = Roster
