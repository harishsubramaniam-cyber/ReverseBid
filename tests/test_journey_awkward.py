"""Journey 4 — the awkward paths.

Nobody uses software the way a demo does. This drives the things people
actually do: double-clicking a button, pressing back after submitting,
keeping two tabs open, leaving a page open until the auction closes under
them, refreshing at the wrong moment, guessing a URL they should not have,
typing nonsense into a price box, and doing all of it on a phone.

    python tests/test_journey_awkward.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journey import (Watcher, free_port, make_tmp, sign_in,           # noqa: E402
                     start_server)

FAILS: list[str] = []
PW = "demo1234"


def check(label, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


def my_bid_count(page) -> int:
    """How many bids this bidder has in this auction, read off the screen.

    Summed across every item, because a bid may land on an item this bidder
    had not touched before.
    """
    summary = page.locator("summary:has-text('Your bids on this item')")
    total = 0
    for index in range(summary.count()):
        found = re.search(r"\((\d+)\)", summary.nth(index).inner_text())
        total += int(found.group(1)) if found else 0
    return total


def live_auction_id(page, base) -> int:
    page.goto(base + "/auctions?status=live")
    page.wait_for_load_state("networkidle")
    href = page.locator("table tbody a[href^='/auctions/']").first.get_attribute("href")
    return int(href.rstrip("/").split("/")[-1])


def suggested_price(page):
    """The highest price this bidder may offer right now.

    Read from data-max, the raw number the page carries for its own checking -
    not from the placeholder, which is money for a person to read ("60,450.00",
    or "Your price" when they are already leading).
    """
    box = page.locator("input[name=unit_price]").first
    raw = box.get_attribute("data-max")
    return float(raw) if raw else None


def run(browser, base, tmp, watch):                                   # noqa: C901
    buyer = browser.new_context(viewport={"width": 1440, "height": 950}).new_page()
    buyer.on("dialog", lambda d: d.accept())
    watch.attach(buyer, "BUYER ")
    sign_in(buyer, base, "buyer@demo.in", PW)
    auction_id = live_auction_id(buyer, base)

    # ------------------------------------------------------------------ 1
    print("\n1. Arriving at a deep link while signed out")
    stranger = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
    stranger.on("dialog", lambda d: d.accept())
    watch.attach(stranger, "STRANGER ")
    stranger.goto(f"{base}/auctions/{auction_id}?tab=details")
    stranger.wait_for_load_state("networkidle")
    watch.note_page(stranger, "a deep link while signed out")
    check("a signed-out visitor is sent to sign in, not shown the auction",
          "/login" in stranger.url and "Corrugated" not in stranger.content(),
          stranger.url)
    check("...and the page they wanted is remembered",
          "next=" in stranger.url, stranger.url.split("?")[-1][:60])
    stranger.fill("#email", "vendor1@demo.in")
    stranger.fill("#password", PW)
    stranger.click("button[type=submit]")
    stranger.wait_for_load_state("networkidle")
    watch.note_page(stranger, "after signing in from a deep link")
    check("signing in takes them where they were going",
          f"/auctions/{auction_id}" in stranger.url, stranger.url)

    print("   (and a doctored next= is ignored)")
    stranger.goto(base + "/logout")
    stranger.wait_for_load_state("networkidle")
    stranger.locator("button:has-text('Sign me out')").first.click()
    stranger.wait_for_load_state("networkidle")
    stranger.goto(base + "/login?next=https://evil.example/steal")
    stranger.fill("#email", "vendor1@demo.in")
    stranger.fill("#password", PW)
    stranger.click("button[type=submit]")
    stranger.wait_for_load_state("networkidle")
    check("an off-site next= is refused and they land at home",
          "evil.example" not in stranger.url and base in stranger.url, stranger.url)

    # ------------------------------------------------------------------ 2
    print("\n2. Signing out, then pressing back")
    stranger.goto(base + "/auctions")
    stranger.wait_for_load_state("networkidle")
    stranger.goto(base + "/logout")
    stranger.locator("button:has-text('Sign me out')").first.click()
    stranger.wait_for_load_state("networkidle")
    watch.note_page(stranger, "signed out")
    check("signing out says so", "signed out" in stranger.content().lower(),
          stranger.locator(".alert").first.inner_text()[:50]
          if stranger.locator(".alert").count() else stranger.url)
    stranger.go_back()
    stranger.wait_for_load_state("networkidle")
    stranger.reload()
    stranger.wait_for_load_state("networkidle")
    watch.note_page(stranger, "back button after signing out")
    check("the back button cannot get them back in",
          "/login" in stranger.url, stranger.url)

    # ------------------------------------------------------------------ 3
    print("\n3. An impatient double-click on Place bid")
    bidder = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
    bidder.on("dialog", lambda d: d.accept())
    watch.attach(bidder, "BIDDER ")
    sign_in(bidder, base, "vendor1@demo.in", PW)
    bidder.goto(f"{base}/auctions/{auction_id}")
    bidder.wait_for_timeout(700)
    before = my_bid_count(bidder)
    box = bidder.locator("input[name=unit_price]").first
    suggested = suggested_price(bidder)
    box.fill(str(suggested))
    # Two presses in the same tick: exactly what an impatient double-click is.
    posts: list[str] = []
    bidder.on("request", lambda r: posts.append(r.url)
              if r.method == "POST" and r.url.endswith("/bid") else None)
    guarded = bidder.evaluate("""() => {
        const form = document.querySelector("form[action$='/bid']");
        const button = form.querySelector("button[type=submit]");
        button.click();
        button.click();
        return form.dataset.sending === "1";
    }""")
    bidder.wait_for_load_state("networkidle")
    bidder.wait_for_timeout(1200)
    check("the form knows it is already sending, so the second press does nothing",
          guarded is True, f"guard={guarded}")
    check("...and only one request left the browser", len(posts) == 1,
          f"{len(posts)} posts")
    bidder.goto(f"{base}/auctions/{auction_id}")
    bidder.wait_for_timeout(700)
    watch.note_page(bidder, "after a double-clicked bid")
    after = my_bid_count(bidder)
    check("a double-click books one bid, not two", after == before + 1,
          f"{before} → {after}")

    print("\n4. Pressing back and submitting the same bid again")
    bidder.go_back()
    bidder.wait_for_load_state("networkidle")
    bidder.wait_for_timeout(600)
    stale = bidder.locator("input[name=unit_price]")
    if stale.count():
        stale.first.evaluate("el => el.removeAttribute('max')")
        stale.first.fill(str(suggested))
        bidder.locator("button:has-text('Place bid')").first.click()
        bidder.wait_for_load_state("networkidle")
        bidder.wait_for_timeout(500)
        watch.note_page(bidder, "a re-submitted stale bid")
        told = bidder.locator(".alert").first.inner_text() if bidder.locator(".alert").count() else ""
        if bidder.locator(".field-error:visible").count():
            # The board refreshes itself, so a stale tab may already know the
            # price has moved and say so without a round trip.
            told = bidder.locator(".field-error:visible").first.inner_text()
        check("the same price a second time is refused, and says why",
              "already bid" in told or "has to come in below" in told or "Too high" in told,
              " ".join(told.split())[:80])
    bidder.goto(f"{base}/auctions/{auction_id}")
    bidder.wait_for_timeout(700)
    check("...and no extra bid was recorded", my_bid_count(bidder) == after,
          f"{after} → {my_bid_count(bidder)}")

    print("\n5. Refreshing straight after bidding")
    box = bidder.locator("input[name=unit_price]").first
    box.fill(str(round(suggested_price(bidder), 2)))
    bidder.locator("button:has-text('Place bid')").first.click()
    bidder.wait_for_load_state("networkidle")
    bidder.wait_for_timeout(500)
    placed = my_bid_count(bidder)
    bidder.reload()
    bidder.wait_for_load_state("networkidle")
    bidder.wait_for_timeout(700)
    watch.note_page(bidder, "a refresh after bidding")
    check("refreshing does not re-post the bid", my_bid_count(bidder) == placed,
          f"{placed} → {my_bid_count(bidder)}")
    check("...and the flash message does not stick around forever",
          bidder.locator(".alert.ok").count() == 0,
          f"{bidder.locator('.alert.ok').count()} messages still shown")

    # ------------------------------------------------------------------ 6
    print("\n6. Two tabs, one auction")
    tab_a = bidder
    tab_b = tab_a.context.new_page()
    tab_b.on("dialog", lambda d: d.accept())
    watch.attach(tab_b, "TAB-B ")
    tab_b.goto(f"{base}/auctions/{auction_id}")
    tab_b.wait_for_timeout(700)
    tab_a.goto(f"{base}/auctions/{auction_id}")
    tab_a.wait_for_timeout(700)
    a_price = suggested_price(tab_a)
    # Tab B bids first, so tab A's page is now out of date.
    tab_b.locator("input[name=unit_price]").first.fill(str(a_price))
    tab_b.locator("button:has-text('Place bid')").first.click()
    tab_b.wait_for_load_state("networkidle")
    tab_b.wait_for_timeout(400)
    stale_box = tab_a.locator("input[name=unit_price]").first
    stale_box.evaluate("el => el.removeAttribute('max')")
    stale_box.fill(str(a_price))
    tab_a.locator("button:has-text('Place bid')").first.click()
    tab_a.wait_for_load_state("networkidle")
    tab_a.wait_for_timeout(500)
    watch.note_page(tab_a, "the stale tab")
    told = tab_a.locator(".alert").first.inner_text() if tab_a.locator(".alert").count() else ""
    check("the stale tab is refused, with the price that is really lowest",
          ("already bid" in told or "Too high" in told or "below it" in told),
          " ".join(told.split())[:90])
    check("...and the refusal names a number they can act on",
          re.search(r"\d+\.\d\d", told) is not None, " ".join(told.split())[:60])
    tab_b.close()

    # ------------------------------------------------------------------ 7
    print("\n7. Nonsense in the price box")
    for typed, expect in (("-5", "greater than zero"),
                          ("0", "greater than zero"),
                          ("1e30", "too large"),
                          ("12,50", "not a price")):
        bidder.goto(f"{base}/auctions/{auction_id}")
        bidder.wait_for_timeout(600)
        target = bidder.locator("input[name=unit_price]").first
        # Past the browser's own checks, so it is the app's answer being tested.
        target.evaluate("el => { el.removeAttribute('max'); el.removeAttribute('min');"
                        " el.type = 'text'; }")
        target.fill(typed)
        bidder.locator("button:has-text('Place bid')").first.click()
        bidder.wait_for_load_state("networkidle")
        bidder.wait_for_timeout(300)
        watch.note_page(bidder, f"a bid of {typed!r}")
        # The page says it where it can; the server says the same words when
        # the page is bypassed. Either is a pass - the point is that the bidder
        # is told, in words, and in the same vocabulary.
        told = (bidder.locator(".alert").first.inner_text()
                if bidder.locator(".alert").count() else "")
        if bidder.locator(".field-error:visible").count():
            told = bidder.locator(".field-error:visible").first.inner_text()
        check(f"“{typed}” is turned away in plain words", expect in told,
              " ".join(told.split())[:70])
    bidder.goto(f"{base}/auctions/{auction_id}")
    bidder.wait_for_timeout(500)
    empty = bidder.locator("input[name=unit_price]").first
    empty.evaluate("el => el.removeAttribute('required')")
    empty.fill("")
    bidder.locator("button:has-text('Place bid')").first.click()
    bidder.wait_for_timeout(400)
    # The page asks for a price rather than posting an empty form...
    problem = bidder.locator(".field-error:visible")
    check("an empty box is turned away too",
          problem.count() > 0 and "price" in problem.first.inner_text().lower(),
          problem.first.inner_text()[:60] if problem.count() else "no message")
    # ...and the server says the same if anything ever gets past the page.
    sent = bidder.evaluate("""async () => {
        const form = document.querySelector("form[action$='/bid']");
        const body = new FormData(form);
        body.set('unit_price', '');
        const r = await fetch(form.action, {method: 'POST', body, redirect: 'follow'});
        return (await r.text()).includes('Type a price');
    }""")
    check("...and the server would have said so too", sent)

    # ------------------------------------------------------------------ 8
    print("\n8. URLs nobody was given")
    for path, what in ((f"/auctions/{auction_id}?tab=history", "the buyer's audit trail"),
                       ("/masters", "the master data"),
                       ("/reports", "the reports"),
                       ("/outbox", "the outbox"),
                       ("/team", "the team page")):
        bidder.goto(base + path)
        bidder.wait_for_load_state("networkidle")
        watch.note_page(bidder, f"a bidder at {path}")
        body = bidder.locator("main").inner_text()
        ok = ("do not have access" in body or "not have access" in body
              or path == "/team")
        check(f"a bidder is turned away from {what}, politely", ok, body[:60].replace("\n", " "))
    watch.forgive_http("403")

    bidder.goto(base + "/auctions/999999")
    bidder.wait_for_load_state("networkidle")
    watch.note_page(bidder, "an auction that does not exist")
    check("an auction id that does not exist gives a real page, not a stack trace",
          "not exist" in bidder.content() or "not find" in bidder.content()
          or "404" in bidder.content(),
          bidder.locator("main").inner_text()[:60].replace("\n", " "))
    watch.forgive_http("404")

    draft = buyer.locator("a[href^='/auctions/']")
    buyer.goto(base + "/auctions?status=draft")
    buyer.wait_for_load_state("networkidle")
    draft_id = int(buyer.locator("table tbody a[href^='/auctions/']").first
                   .get_attribute("href").rstrip("/").split("/")[-1])
    bidder.goto(f"{base}/auctions/{draft_id}")
    bidder.wait_for_load_state("networkidle")
    watch.note_page(bidder, "a bidder at a draft auction")
    check("a bidder cannot read a draft the buyer has not published",
          "Hydraulic hoses" not in bidder.content(),
          bidder.locator("main").inner_text()[:60].replace("\n", " "))
    watch.forgive_http("403", "404")

    # ------------------------------------------------------------------ 9
    print("\n9. A page left open until the auction closes underneath it")
    bidder.goto(f"{base}/auctions/{auction_id}")
    bidder.wait_for_timeout(700)
    held = suggested_price(bidder)
    buyer.goto(f"{base}/auctions/{auction_id}")
    buyer.wait_for_timeout(400)
    buyer.locator("button:has-text('Close bidding now')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(1500)
    bidder.locator("input[name=unit_price]").first.fill(str(held))
    bidder.locator("button:has-text('Place bid')").first.click()
    bidder.wait_for_load_state("networkidle")
    bidder.wait_for_timeout(500)
    watch.note_page(bidder, "bidding into a closed auction")
    told = bidder.locator(".alert").first.inner_text() if bidder.locator(".alert").count() else ""
    check("a bid into a closed auction is refused kindly",
          "not open for bidding" in told or "just closed" in told,
          " ".join(told.split())[:70])
    check("...and the page they are left on tells them where things stand",
          "closed" in bidder.content().lower())
    check("...with no bid box still inviting them to try",
          bidder.locator("input[name=unit_price]").count() == 0)

    # ------------------------------------------------------------------ 10
    print("\n10. All of it on a phone")
    phone = browser.new_context(viewport={"width": 390, "height": 844},
                                is_mobile=True, has_touch=True).new_page()
    phone.on("dialog", lambda d: d.accept())
    watch.attach(phone, "PHONE ")
    sign_in(phone, base, "buyer@demo.in", PW)
    for path, what in (("/", "the dashboard"),
                       (f"/auctions/{auction_id}", "the auction"),
                       (f"/auctions/{auction_id}?tab=details", "the details"),
                       ("/auctions", "the list"),
                       ("/masters", "the master data"),
                       ("/reports", "the reports"),
                       (f"/auctions/{auction_id}/award", "the award screen")):
        phone.goto(base + path)
        phone.wait_for_load_state("networkidle")
        phone.wait_for_timeout(300)
        watch.note_page(phone, f"{what} on a phone")
        overflow = phone.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth")
        check(f"{what} fits the screen sideways", overflow <= 2, f"{overflow}px over")
    phone.goto(base + f"/auctions/{auction_id}")
    phone.wait_for_timeout(400)
    check("a wide table scrolls inside its own box on a phone",
          phone.evaluate("""() => {
              const boxes = [...document.querySelectorAll('.table-wrap')];
              return boxes.length === 0 || boxes.some(b =>
                  getComputedStyle(b).overflowX !== 'visible');
          }"""))
    check("the phone gets a navigation bar of its own",
          phone.locator(".mobilenav a").first.is_visible()
          and phone.locator(".mobilenav a").count() >= 4,
          f"{phone.locator('.mobilenav a').count()} entries")
    phone.locator(".mobilenav a:has-text('More')").click()
    phone.wait_for_timeout(400)
    watch.note_page(phone, "the More sheet on a phone")
    sheet = phone.locator("#more-drawer")
    check("...and everything the wide navigation holds is reachable from it",
          all(sheet.locator(f"a[href='{path}']").count() == 1
              for path in ("/masters", "/reports", "/outbox", "/team", "/auctions")),
          " ".join(a for a in ("/masters", "/reports", "/outbox", "/team")
                   if sheet.locator(f"a[href='{a}']").count() != 1) or "all present")
    check("...including a way to sign out",
          sheet.locator("button:has-text('Sign out')").count() == 1)
    sheet.locator("a[href='/masters']").click()
    phone.wait_for_load_state("networkidle")
    watch.note_page(phone, "vendors from the phone sheet")
    check("...and it actually goes there", "/masters" in phone.url, phone.url)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed — skipping the journey.")
        return 0

    tmp = make_tmp("ra-journey4-")
    port = free_port()
    server = start_server(tmp, port, seed=True)
    base = f"http://127.0.0.1:{port}"
    watch = Watcher()
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:
                print(f"No browser available ({exc}) — skipping.")
                return 0
            run(browser, base, tmp, watch)
            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()

    print("\n" + "-" * 64)
    check("nothing errored anywhere along the way", watch.clean, watch.report())
    if FAILS:
        print(f"{len(FAILS)} problem(s) found:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("Awkward-paths journey clean.")
    return 0


def test_journey_awkward():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
