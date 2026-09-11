"""Journey 1 — the very first hour with an empty install.

Somebody has just unzipped this and double-clicked. There is no data at all.
They create their account, find their way around empty screens, add their
first supplier and item, and get their first auction out of the door.

    python tests/test_journey_first_run.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journey import (Watcher, free_port, local_time, make_tmp,   # noqa: E402
                     sign_in, start_server)

FAILS: list[str] = []


def check(label, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


def run(page, base, watch):                                       # noqa: C901
    # ------------------------------------------------------------------ 1
    print("\n1. Arriving with nothing")
    page.goto(base + "/")
    page.wait_for_load_state("networkidle")
    watch.note_page(page, "the front door")
    check("an empty install sends you to sign in", "/login" in page.url, page.url)
    check("the sign-in page offers a way to create the first account",
          page.locator("a[href='/signup']").count() >= 1)

    page.goto(base + "/signup")
    page.wait_for_timeout(200)
    watch.note_page(page, "signup")
    check("the signup page offers to set up an organisation",
          "organisation" in page.locator("h1").first.inner_text().lower(),
          page.locator("h1").first.inner_text())
    check("it explains that everyone else arrives by invitation",
          "invitation" in page.content())

    # a mistake first, because everybody makes one
    page.fill("#name", "Priya Raman")
    page.fill("#email", "priya@northplant.example")
    page.fill("#password", "abc")
    page.fill("#company", "North Plant Industries")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    watch.note_page(page, "signup with a short password")
    check("a short password is explained, not swallowed",
          "6 characters" in page.content(), page.locator(".alert").first.inner_text()[:60]
          if page.locator(".alert").count() else "no message")
    check("...and everything typed is still on the form",
          page.input_value("#name") == "Priya Raman"
          and page.input_value("#company") == "North Plant Industries",
          f"{page.input_value('#name')!r} / {page.input_value('#company')!r}")

    page.fill("#password", "northplant1")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    watch.note_page(page, "onboarding")
    check("a good password gets them in", "/onboarding" in page.url, page.url)
    check("they are greeted by name", "Priya" in page.content())
    check("the tour counts what they have so far",
          "0 so far" in page.content() or "(0 so far)" in page.content())

    # ------------------------------------------------------------------ 2
    print("\n2. Every empty screen, before there is any data")
    empties = {
        "/": "the dashboard",
        "/auctions": "the auction list",
        "/masters": "vendors",
        "/masters?tab=items": "items",
        "/masters?tab=units": "units",
        "/reports": "reports",
        "/outbox": "the outbox",
        "/notifications": "alerts",
        "/team": "the team page",
        "/auctions/new": "the new auction form",
    }
    for path, what in empties.items():
        page.goto(base + path)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(120)
        watch.note_page(page, what)
        body = page.locator("main").inner_text()
        check(f"{what} works with no data", len(body.strip()) > 40 and
              "went wrong" not in body, f"{len(body.strip())} chars")

    page.goto(base + "/reports")
    page.wait_for_timeout(200)
    check("an empty report says so, and says what to try",
          "No awarded auctions" in page.content() and "widening" in page.content())
    page.goto(base + "/auctions/new")
    page.wait_for_timeout(300)
    check("the auction form admits there are no vendors yet",
          "No vendors yet" in page.content())
    check("...and points at the button that fixes it",
          "New vendor" in page.content() or "new vendor" in page.content())

    # ------------------------------------------------------------------ 3
    print("\n3. Adding the first supplier and item")
    page.goto(base + "/masters")
    page.wait_for_timeout(250)
    page.fill("form[action='/masters/vendors'] input[name=name]", "Sundaram Castings")
    # "sales@localhost" gets past the browser's own check but is not a real
    # address, so this exercises the app's own message rather than Chrome's.
    page.fill("form[action='/masters/vendors'] input[name=email]", "sales@localhost")
    page.fill("form[action='/masters/vendors'] input[name=phone]", "9876543210")
    page.fill("form[action='/masters/vendors'] input[name=gstin]", "33AAAAA0000A1Z5")
    page.click("form[action='/masters/vendors'] button[type=submit]")
    page.wait_for_load_state("networkidle")
    watch.note_page(page, "a bad vendor email")
    check("a bad address is refused in plain words",
          "does not look like" in page.content())
    for field, value in (("name", "Sundaram Castings"), ("phone", "9876543210"),
                         ("gstin", "33AAAAA0000A1Z5")):
        actual = page.input_value(f"form[action='/masters/vendors'] input[name={field}]")
        check(f"  ...and “{value}” is still in its box", actual == value, actual)

    page.fill("form[action='/masters/vendors'] input[name=email]",
              "quotes@sundaramcast.example")
    page.click("form[action='/masters/vendors'] button[type=submit]")
    page.wait_for_load_state("networkidle")
    watch.note_page(page, "vendor saved")
    check("a good one saves, and says who will be emailed",
          "saved" in page.content() and "1 address" in page.content(),
          page.locator(".alert").first.inner_text()[:70])

    page.goto(base + "/masters?tab=units")
    page.wait_for_timeout(200)
    page.fill("form[action='/masters/units'] input[name=code]", "kg")
    page.fill("form[action='/masters/units'] input[name=name]", "Kilogram")
    page.click("form[action='/masters/units'] button[type=submit]")
    page.wait_for_load_state("networkidle")
    check("a unit saves, upper-cased", "KG" in page.content())

    page.goto(base + "/masters?tab=items")
    page.wait_for_timeout(200)
    page.fill("form[action='/masters/items'] input[name=name]", "SG iron casting, 12 kg")
    page.select_option("form[action='/masters/items'] select[name=default_unit_id]", index=1)
    page.click("form[action='/masters/items'] button[type=submit]")
    page.wait_for_load_state("networkidle")
    watch.note_page(page, "item saved")
    check("an item saves with its unit",
          "SG iron casting, 12 kg" in page.content() and "KG" in page.content())

    # ------------------------------------------------------------------ 4
    print("\n4. The first auction, using the inline create")
    page.goto(base + "/auctions/new")
    page.wait_for_timeout(400)
    page.fill("#title", "SG iron castings — October")
    page.fill("#description", "Monthly rate contract. Drawings attached once published.")

    # create a second supplier without leaving the form
    page.locator("button:has-text('Create a new vendor')").first.click()
    page.wait_for_timeout(300)
    page.fill("#m-vendor input[name=name]", "Kovai Foundry")
    page.fill("#m-vendor input[name=email]", "sales@kovaifoundry.example")
    page.fill("#m-vendor textarea[name=extra_emails]", "works@kovaifoundry.example")
    page.click("#m-vendor button[type=submit]")
    page.wait_for_timeout(700)
    watch.note_page(page, "inline vendor create")
    check("the new supplier appears, already ticked",
          page.locator("#vendor-list .vendor-row input:checked").count() >= 1,
          f"{page.locator('#vendor-list .vendor-row input:checked').count()} ticked")
    check("...and the form says where their emails will go",
          "works@kovaifoundry.example" in page.content())

    # create a second item inline too
    page.locator("button:has-text('Create a new item')").first.click()
    page.wait_for_timeout(300)
    page.fill("#m-item input[name=name]", "Machining fixture, cast")
    page.click("#m-item button[type=submit]")
    page.wait_for_timeout(700)
    rows = page.locator("#line-rows .item-row")
    check("the new item becomes a row of its own", rows.count() >= 1,
          f"{rows.count()} rows")

    # fill the rows in
    for index in range(rows.count()):
        item_select = rows.nth(index).locator("select[name=line_item_id]")
        if not item_select.input_value():
            item_select.select_option(index=1)
        rows.nth(index).locator("input[name=line_qty]").fill(str(1200 * (index + 1)))
        rows.nth(index).locator("input[name=line_price]").fill(str(180 + index * 40))

    # tick every supplier
    boxes = page.locator("#vendor-list input[type=checkbox]")
    for index in range(boxes.count()):
        boxes.nth(index).check()
    page.fill("#cc_emails", "finance@northplant.example")
    page.fill("#start_at", local_time(-2))
    page.fill("#end_at", local_time(180))

    page.locator("button:has-text('Save as draft')").click()
    page.wait_for_load_state("networkidle")
    watch.note_page(page, "saved as draft")
    check("saving as a draft lands on the auction",
          "/auctions/" in page.url and "draft" in page.content().lower(), page.url)
    check("the draft says plainly that nobody has been told",
          "Nobody has been told about it" in page.content())
    auction_url = page.url.split("?")[0]

    page.goto(base + "/outbox")
    page.wait_for_timeout(300)
    sent = page.locator("table tbody tr:not(:has(.empty)) a")
    check("...and truly nothing has been emailed", sent.count() == 0,
          f"{sent.count()} emails")

    # ------------------------------------------------------------------ 5
    print("\n5. Publishing it")
    page.goto(auction_url)
    page.wait_for_timeout(300)
    page.locator("button:has-text('Publish')").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2500)
    watch.note_page(page, "published")
    check("publishing opens bidding at once and says how many were invited",
          "LIVE" in page.locator("main .pill").first.inner_text()
          and "invited by email" in page.content(),
          page.locator(".alert").first.inner_text()[:80])
    page.goto(base + "/outbox")
    page.wait_for_timeout(500)
    count = page.locator("table tbody tr:not(:has(.empty)) a").count()
    check("the invitations really went out", count >= 3, f"{count} emails")
    check("the copy list was written to as well", "finance@northplant" in page.content())

    page.goto(base + "/")
    page.wait_for_timeout(300)
    watch.note_page(page, "dashboard with one live auction")
    check("the dashboard now shows the live auction",
          "1 live" in page.content() or "Closing soon" in page.content())
    check("...with no nonsense in the savings tile",
          "inf" not in page.locator(".stat").first.inner_text().lower()
          and "nan" not in page.locator(".stat").first.inner_text().lower())
    return auction_url


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed — skipping the journey.")
        return 0

    tmp = make_tmp("ra-journey1-")
    port = free_port()
    server = start_server(tmp, port, seed=False)
    base = f"http://127.0.0.1:{port}"
    watch = Watcher()
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:
                print(f"No browser available ({exc}) — skipping.")
                return 0
            page = browser.new_context(viewport={"width": 1440, "height": 1000}).new_page()
            page.on("dialog", lambda d: d.accept())
            watch.attach(page)
            run(page, base, watch)
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
    print("First-run journey clean.")
    return 0


def test_journey_first_run():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
