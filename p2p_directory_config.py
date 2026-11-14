"""The host file's winlink.directory block, checked: stdlib only.

render.py checks this block, and the radio server reaches render at runtime
(levels.py, on a codec-level save), so this module must stay importable without
aiohttp or anything else the directory fetcher (p2p_directory.py) needs.
"""
import re

DEFAULT_URL = "https://cvp1.github.io/p2p-directory/v1/directory.json"
DEFAULT_REFRESH_H = 6


class DirectoryError(ValueError):
    pass


def config(block):
    """(url, refresh_hours) from the host file's winlink.directory, or None when off."""
    if block is False:
        return None
    if block is None or block is True:
        return DEFAULT_URL, DEFAULT_REFRESH_H
    if not isinstance(block, dict):
        raise DirectoryError("winlink.directory must be false or an object")
    url = block.get("url", DEFAULT_URL)
    hours = block.get("refresh_hours", DEFAULT_REFRESH_H)
    if not isinstance(url, str) or not re.fullmatch(r"https://[^\s]+|http://(127\.0\.0\.1|localhost)(:\d+)?/[^\s]*", url):
        raise DirectoryError(f"winlink.directory.url = {url!r}: https://, or http:// on this machine")
    if not isinstance(hours, (int, float)) or isinstance(hours, bool) or not 1 <= hours <= 168:
        raise DirectoryError(f"winlink.directory.refresh_hours = {hours!r}: 1 to 168")
    return url, float(hours)
