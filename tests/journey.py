"""Shared machinery for the journey tests.

A journey test drives a real browser through what one person actually does,
start to finish, and fails on anything a person would notice: a page that
errors, a button that does nothing, a number that contradicts another number,
a dead end with no way out.

Everything is watched all the time, not just where a check looks:

* any JavaScript error or console error, on any page
* any 4xx or 5xx the browser sees while navigating or fetching
* the words of a crash ("Something went wrong at our end", "Traceback")
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Watcher:
    """Collects everything that would make a person frown."""

    def __init__(self):
        self.js: list[str] = []
        self.http: list[str] = []
        self.crashes: list[str] = []

    def attach(self, page, label: str = ""):
        page.on("pageerror", lambda e: self.js.append(f"{label}{page.url}: {e}"))
        page.on("console", lambda m: self.js.append(f"{label}{page.url}: {m.text}")
                if m.type == "error" else None)

        def on_response(response):
            # Only the responses a person is waiting for: pages, fragments,
            # downloads. A 404 on a favicon is not a bug worth failing over.
            if response.status < 400:
                return
            if response.request.resource_type in ("image", "font", "stylesheet"):
                return
            self.http.append(f"{response.status} {response.request.method} "
                             f"{response.url.split('://', 1)[-1].split('/', 1)[-1]}")

        page.on("response", on_response)
        return page

    def note_page(self, page, where: str):
        """Called after a navigation: did the person land on a crash?"""
        try:
            text = page.content()
        except Exception:
            return
        for phrase in ("went wrong at our end", "Traceback (most recent call last)",
                       "Internal Server Error"):
            if phrase in text:
                self.crashes.append(f"{where}: “{phrase}”")

    @property
    def clean(self) -> bool:
        return not (self.js or self.http or self.crashes)

    def report(self) -> str:
        parts = []
        if self.crashes:
            parts.append("crash pages: " + "; ".join(self.crashes[:3]))
        if self.http:
            parts.append("bad responses: " + "; ".join(dict.fromkeys(self.http[:5])))
        if self.js:
            parts.append("script errors: " + "; ".join(dict.fromkeys(self.js[:3])))
        return " | ".join(parts)

    def forgive_http(self, *fragments: str):
        """Drop expected failures, e.g. a 403 a test deliberately provoked.

        The browser reports a refused navigation twice - once as a response
        and once on the console - so both lists are swept.
        """
        self.http = [h for h in self.http
                     if not any(f in h for f in fragments)]
        self.js = [j for j in self.js
                   if not any(f in j for f in fragments)]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(tmp: str, port: int, *, seed: bool) -> subprocess.Popen:
    """A server of its own, on its own database. ``seed`` decides whether it
    starts with the demo data or completely empty, which is the state a real
    first-time buyer sees."""
    env = dict(os.environ,
               RA_DATA_DIR=tmp,
               RA_DATABASE_URL=f"sqlite:///{tmp}/journey.db",
               RA_ENV_FILE=f"{tmp}/none.env",
               RA_SECRET_KEY="journey-key",
               RA_BASE_URL=f"http://127.0.0.1:{port}",
               RA_TIMEZONE="Asia/Kolkata",
               RA_SCHEDULER_INTERVAL="2",
               PYTHONPATH=str(ROOT))
    env.pop("RA_SMTP_HOST", None)
    if seed:
        subprocess.run([sys.executable, "seed.py"], cwd=ROOT, env=env, check=True,
                       stdout=subprocess.DEVNULL)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    import urllib.request
    for _ in range(80):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            return proc
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("the journey server did not start")


def make_tmp(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=prefix)


def sign_in(page, base: str, email: str, password: str):
    page.goto(base + "/login")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


def join_link_from_outbox(buyer_page, base: str, address: str) -> str | None:
    """Find the set-a-password link in whatever the app emailed someone.

    This is the real supplier route: the buyer can read the outbox, and the
    link inside is what the supplier would click in their inbox.
    """
    import re
    buyer_page.goto(f"{base}/outbox?q={address}")
    buyer_page.wait_for_timeout(400)
    rows = buyer_page.locator("table tbody tr")
    for index in range(rows.count()):
        rows.nth(index).locator("a").first.click()
        buyer_page.wait_for_load_state("networkidle")
        buyer_page.wait_for_timeout(250)
        html = buyer_page.frame_locator("iframe").locator("body").inner_html()
        found = re.search(r'href="[^"]*?(/join/[^"]+)"', html)
        if found:
            return base + found.group(1)
        buyer_page.go_back()
        buyer_page.wait_for_timeout(200)
    return None


def local_time(offset_minutes: int = 0) -> str:
    """A value for a datetime-local box, in the app's timezone."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(minutes=offset_minutes)
    return now.strftime("%Y-%m-%dT%H:%M")
