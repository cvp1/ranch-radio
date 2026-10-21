"""Headless Chromium on one page, through the DevTools protocol (no extra
packages), for the page tests."""
import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request

import aiohttp


class Browser:
    """Headless Chromium on one page, through the DevTools protocol."""

    def __init__(self, chrome, cdp_port, size="1500,1400", extra=()):
        self.dir = tempfile.mkdtemp(prefix="page-chrome-")
        self.proc = subprocess.Popen(
            [chrome, "--headless=new", f"--remote-debugging-port={cdp_port}", f"--user-data-dir={self.dir}",
             "--no-first-run", "--no-default-browser-check", f"--window-size={size}", *extra, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json"))
                page = next(t for t in targets if t["type"] == "page")
                break
            except (OSError, StopIteration):
                time.sleep(0.1)
        else:
            raise SystemExit("chromium did not start")
        self.loop = asyncio.new_event_loop()
        self.n = 0
        self.ws = self.loop.run_until_complete(self._open(page["webSocketDebuggerUrl"]))
        self.call("Runtime.enable")
        self.call("Page.enable")

    async def _open(self, url):
        self.sess = aiohttp.ClientSession()
        return await self.sess.ws_connect(url, max_msg_size=0)

    async def _call(self, method, params):
        self.n += 1
        n = self.n
        await self.ws.send_str(json.dumps({"id": n, "method": method, "params": params or {}}))
        while True:
            m = json.loads((await self.ws.receive(timeout=15)).data)
            if m.get("id") == n:
                return m

    def call(self, method, **params):
        return self.loop.run_until_complete(self._call(method, params))

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return f"JS error: {res['exceptionDetails'].get('exception', {}).get('description')}"
        return res.get("result", {}).get("value")

    def goto(self, url):
        self.call("Page.navigate", url=url)
        time.sleep(1.5)

    def wait(self, expr, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            v = self.js(expr)
            if v and not (isinstance(v, str) and v.startswith("JS error")):
                return v
            time.sleep(0.15)
        return self.js(expr)

    def screenshot(self, path):
        import base64
        r = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["result"]["data"]))

    def close(self):
        try:
            self.loop.run_until_complete(self.sess.close())
        finally:
            self.proc.terminate()
            self.proc.wait(5)
            shutil.rmtree(self.dir, ignore_errors=True)
