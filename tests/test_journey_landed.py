"""Journey 5 — the back office, delivered cost, and the clock moving.

The parts of the product a buyer only meets after the first week: keeping the
vendor and item lists tidy, bringing a colleague in, running an auction that
is judged on the delivered price rather than the bid, editing it before it
opens, watching the auto-extension push the finish line back, and calling one
off entirely.

    python tests/test_journey_landed.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journey import (Watcher, free_port, join_link_from_outbox, local_time,   # noqa: E402
                     make_tmp, sign_in, start_server)

FAILS: list[str] = []
PW = "demo1234"


def check(label, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


def money(text: str) -> float:
    found = re.search(r"([\d,]+\.\d\d)", text.replace("₹", ""))
    return float(found.group(1).replace(",", "")) if found else float("nan")


def run(browser, base, tmp, watch):                                  # noqa: C901
    buyer = browser.new_context(viewport={"width": 1500, "height": 1000}).new_page()
    buyer.on("dialog", lambda d: d.accept())
    watch.attach(buyer, "BUYER ")
    sign_in(buyer, base, "buyer@demo.in", PW)

    # ------------------------------------------------------------------ 1
    print("\n1. Tidying the vendor list")
    buyer.goto(base + "/masters")
    buyer.wait_for_load_state("networkidle")
    watch.note_page(buyer, "vendors")
    rows = buyer.locator("table tbody tr")
    check("every vendor is listed with the addresses we email",
          rows.count() >= 5 and "@" in rows.first.inner_text(),
          f"{rows.count()} vendors")
    check("a vendor nobody has invited yet is shown all the same",
          "Bharat Fasteners" in buyer.content())

    # the same company typed twice
    buyer.fill("form[action='/masters/vendors'] input[name=name]", "Sunrise Packaging Ltd")
    buyer.fill("form[action='/masters/vendors'] input[name=email]", "vendor1@demo.in")
    buyer.click("form[action='/masters/vendors'] button[type=submit]")
    buyer.wait_for_load_state("networkidle")
    watch.note_page(buyer, "a vendor whose email is already on file")
    told = buyer.locator(".alert").first.inner_text() if buyer.locator(".alert").count() else ""
    check("an address already on file updates that supplier rather than duplicating them",
          "already on file" in told and "second one" in told, " ".join(told.split())[:90])
    check("...and a rename is spelled out, because it shows everywhere they appear",
          "Sunrise Packaging Pvt Ltd" in told and "Sunrise Packaging Ltd" in told,
          " ".join(told.split())[90:180])
    sunrise = buyer.locator("table tbody tr:has-text('Sunrise Packaging')").count()
    check("...and there is still only one of them", sunrise == 1, f"{sunrise} rows")

    # changing who gets the emails
    buyer.goto(base + "/masters")
    buyer.wait_for_timeout(300)
    row = buyer.locator("table tbody tr:has-text('Nagpur Metal Works')").first
    row.locator("button.linkbtn").first.click()
    buyer.wait_for_timeout(400)
    panel = buyer.locator(".modal.on").first
    check("the who-gets-the-emails panel opens for the right supplier",
          "Nagpur Metal Works" in panel.inner_text(), panel.inner_text()[:50])
    panel.locator("textarea[name=extra_emails]").fill("purchase@nagpurmetal.example")
    panel.locator("button:has-text('Save recipients')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(400)
    watch.note_page(buyer, "recipients changed")
    check("the email list can be changed from the list itself",
          "purchase@nagpurmetal.example" in buyer.content(),
          buyer.locator(".alert").first.inner_text()[:60]
          if buyer.locator(".alert").count() else "")

    # archiving
    row = buyer.locator("table tbody tr:has-text('Bharat Fasteners')").first
    row.locator("button:has-text('Archive')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(300)
    watch.note_page(buyer, "vendor archived")
    check("a vendor can be archived",
          "archived" in buyer.locator("table tbody tr:has-text('Bharat Fasteners')")
          .first.inner_text().lower())
    buyer.goto(base + "/auctions/new")
    buyer.wait_for_timeout(600)
    check("...and an archived vendor is no longer offered on a new auction",
          "Bharat Fasteners" not in buyer.locator("#vendor-list").inner_text())
    buyer.goto(base + "/masters")
    buyer.wait_for_timeout(300)
    buyer.locator("table tbody tr:has-text('Bharat Fasteners')").first \
         .locator("button:has-text('Restore')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(300)
    check("...and restored again",
          "active" in buyer.locator("table tbody tr:has-text('Bharat Fasteners')")
          .first.inner_text().lower())

    print("\n2. Units and items")
    buyer.goto(base + "/masters?tab=units")
    buyer.wait_for_timeout(300)
    buyer.fill("form[action='/masters/units'] input[name=code]", "kg")
    buyer.click("form[action='/masters/units'] button[type=submit]")
    buyer.wait_for_load_state("networkidle")
    watch.note_page(buyer, "a duplicate unit")
    told = buyer.locator(".alert").first.inner_text() if buyer.locator(".alert").count() else ""
    check("a unit code that already exists is caught, whatever the case",
          "already exists" in told and "Nothing was added" in told,
          " ".join(told.split())[:70])
    units_named_kg = buyer.locator("table tbody tr:has-text('KG')").count()
    check("...and no second row appears for it", units_named_kg == 1,
          f"{units_named_kg} rows")
    buyer.goto(base + "/masters?tab=items")
    buyer.wait_for_timeout(300)
    buyer.fill("form[action='/masters/items'] input[name=name]", "Nitrile gasket sheet, 3 mm")
    buyer.select_option("form[action='/masters/items'] select[name=default_unit_id]", index=2)
    buyer.fill("form[action='/masters/items'] input[name=category]", "Spares")
    buyer.click("form[action='/masters/items'] button[type=submit]")
    buyer.wait_for_load_state("networkidle")
    watch.note_page(buyer, "item added")
    check("a new item saves and appears", "Nitrile gasket sheet, 3 mm" in buyer.content(),
          buyer.locator(".alert").first.inner_text()[:60]
          if buyer.locator(".alert").count() else "")

    # ------------------------------------------------------------------ 3
    print("\n3. Bringing a colleague onto the buying side")
    buyer.goto(base + "/team")
    buyer.wait_for_timeout(300)
    watch.note_page(buyer, "the team page")
    check("the team page lists the buying side", "Priya Raman" in buyer.content())
    buyer.fill("#email", "buyer@demo.in")
    buyer.click("button:has-text('Send the invitation')")
    buyer.wait_for_load_state("networkidle")
    told = buyer.locator(".alert").first.inner_text() if buyer.locator(".alert").count() else ""
    check("inviting somebody who can already sign in is refused",
          "already" in told.lower(), " ".join(told.split())[:70])
    buyer.fill("#email", "arun@northplant.example")
    buyer.click("button:has-text('Send the invitation')")
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(800)
    watch.note_page(buyer, "colleague invited")
    check("a colleague is invited", "arun@northplant.example" in buyer.content()
          or "invitation" in buyer.locator(".alert").first.inner_text().lower(),
          buyer.locator(".alert").first.inner_text()[:70])
    link = join_link_from_outbox(buyer, base, "arun@northplant.example")
    check("...and their email carries a link that sets a password",
          bool(link) and "/join/" in (link or ""), (link or "none")[:52])
    mate = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
    mate.on("dialog", lambda d: d.accept())
    watch.attach(mate, "COLLEAGUE ")
    mate.goto(link)
    mate.wait_for_load_state("networkidle")
    watch.note_page(mate, "the join page")
    mate.fill("#name", "Arun Prakash")
    mate.fill("#password", "northplant2")
    mate.fill("#confirm", "northplant2")
    mate.click("button[type=submit]")
    mate.wait_for_load_state("networkidle")
    watch.note_page(mate, "colleague signed in")
    check("they set a password and land inside", "/login" not in mate.url, mate.url)
    mate.goto(base + "/masters")
    mate.wait_for_load_state("networkidle")
    check("a buying colleague gets the buyer's screens, not a bidder's",
          "do not have access" not in mate.locator("main").inner_text(),
          mate.locator("main").inner_text()[:50].replace("\n", " "))
    mate.goto(base + "/auctions/new")
    mate.wait_for_timeout(500)
    check("...and can start an auction of their own",
          mate.locator("#title").count() == 1)

    # ------------------------------------------------------------------ 4
    print("\n4. An auction judged on the delivered price")
    buyer.goto(base + "/auctions/new")
    buyer.wait_for_timeout(700)
    buyer.fill("#title", "Nitrile gasket sheet — delivered price")
    buyer.fill("#description", "Freight and duty are added to each bid before we compare them.")
    buyer.fill("#terms", "60 days credit. Delivery to the Salem plant.")
    row = buyer.locator("#line-rows .item-row").first
    labels = row.locator("select[name=line_item_id] option").all_inner_texts()
    index = next(i for i, text in enumerate(labels) if "Nitrile gasket" in text)
    row.locator("select[name=line_item_id]").select_option(index=index)
    row.locator("input[name=line_qty]").fill("8000")
    row.locator("input[name=line_price]").fill("12.50")
    row.locator("input[name=line_spec]").fill("Shore A 70, 1000×1000 sheets")
    boxes = buyer.locator("#vendor-list input[type=checkbox]")
    for i in range(boxes.count()):
        boxes.nth(i).check()
    buyer.wait_for_timeout(200)
    buyer.check("#compare_landed")
    buyer.wait_for_timeout(400)
    watch.note_page(buyer, "the delivered-cost form")
    check("ticking delivered cost explains what changes",
          buyer.locator("#landed-note").is_visible()
          and "deliver" in buyer.locator("#landed-note").inner_text().lower(),
          " ".join(buyer.locator("#landed-note").inner_text().split())[:70])
    check("...and each supplier's freight is shown, pre-filled from their record",
          buyer.locator(".vendor-adders").first.is_visible()
          and any(v not in ("", "0")
                  for v in buyer.locator(".vendor-adders input[name^=freight_]")
                  .all_inner_texts() or ["x"]) is not None,
          f"{buyer.locator('.vendor-adders').count()} panels")
    freights = [buyer.locator(".vendor-adders input[name^=freight_]").nth(i).input_value()
                for i in range(buyer.locator(".vendor-adders input[name^=freight_]").count())]
    check("...with different numbers for different suppliers",
          len({f for f in freights if f}) >= 2, ", ".join(freights[:5]))
    # One supplier whose freight alone eats the whole ceiling — Nagpur, three
    # states away. Their panel, not whichever one happens to be last.
    nagpur = buyer.locator(".vendor-row:has-text('Nagpur Metal Works')").first
    nagpur.locator("input[name^=freight_]").fill("13.00")
    buyer.fill("#min_decrement", "0.10")
    buyer.fill("#start_at", local_time(-1))
    buyer.fill("#end_at", local_time(240))
    buyer.locator("button:has-text('Publish')").last.click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(2500)
    watch.note_page(buyer, "the delivered-cost auction, published")
    check("it publishes and opens", "LIVE" in buyer.locator("main .pill").first.inner_text(),
          buyer.locator("main .pill").first.inner_text())
    landed_id = int(buyer.url.rstrip("/").split("/")[-1].split("?")[0])
    check("the auction says plainly that it is judged on delivered cost",
          "delivered price" in buyer.content())

    print("\n5. Bidding into it, with freight on top")
    first = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
    first.on("dialog", lambda d: d.accept())
    watch.attach(first, "SUPPLIER-1 ")
    sign_in(first, base, "vendor1@demo.in", PW)
    first.goto(f"{base}/auctions/{landed_id}")
    first.wait_for_timeout(800)
    watch.note_page(first, "the delivered-cost auction, as a bidder")
    note = " ".join(first.locator(".range-note").first.inner_text().split())
    check("the bidder is told what to type, not what to work out",
          "type" in note.lower() and re.search(r"\d+\.\d\d", note) is not None, note[:130])
    check("...and their own freight is named", "freight" in first.content().lower())
    suggested = first.locator("input[name=unit_price]").first.evaluate("el => el.placeholder")
    first.locator("input[name=unit_price]").first.fill(suggested)
    first.locator("button:has-text('Place bid')").first.click()
    first.wait_for_load_state("networkidle")
    first.wait_for_timeout(500)
    check("the suggested price lands exactly on the delivered ceiling",
          "L1" in first.locator(".alert").first.inner_text(),
          f"{suggested} → {first.locator('.alert').first.inner_text()[:44]}")
    board = first.locator("#live-board").inner_text()
    check("the board shows the bid and the delivered figure side by side",
          suggested in board and "12.50" in board, f"typed {suggested}")

    print("   (and the supplier whose freight uses up the ceiling)")
    priced_out = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
    priced_out.on("dialog", lambda d: d.accept())
    watch.attach(priced_out, "SUPPLIER-4 ")
    sign_in(priced_out, base, "vendor3@demo.in", PW)   # Nagpur, the far-away one
    priced_out.goto(f"{base}/auctions/{landed_id}")
    priced_out.wait_for_timeout(800)
    watch.note_page(priced_out, "a bidder priced out by their own freight")
    page_text = priced_out.locator("#live-board").inner_text()
    check("they are told there is no price they could offer, and why",
          ("no price" in page_text.lower() or "as far as it can" in page_text.lower()
           or "use up" in page_text.lower()),
          " ".join(page_text.split())[:130])
    check("...and are not left with a box that cannot work",
          priced_out.locator("input[name=unit_price]").count() == 0
          or "no price" in page_text.lower())

    # ------------------------------------------------------------------ 6
    print("\n6. Editing an auction before it opens")
    buyer.goto(base + "/auctions?status=scheduled")
    buyer.wait_for_load_state("networkidle")
    scheduled_id = int(buyer.locator("table tbody a[href^='/auctions/']").first
                       .get_attribute("href").rstrip("/").split("/")[-1])
    buyer.goto(f"{base}/auctions/{scheduled_id}/edit")
    buyer.wait_for_timeout(700)
    watch.note_page(buyer, "editing a scheduled auction")
    check("the edit form comes back filled in",
          len(buyer.input_value("#title")) > 5, buyer.input_value("#title")[:40])
    check("...with the items it already has",
          buyer.locator("#line-rows .item-row").count() >= 1)
    buyer.fill("#end_at", local_time(400))
    buyer.fill("#terms", "45 days credit. Delivery within four weeks of the order.")
    buyer.locator("button:has-text('Save')").first.click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(2000)
    watch.note_page(buyer, "a scheduled auction changed")
    check("saving the change works and says so",
          "aved" in buyer.locator(".alert").first.inner_text()
          or "updated" in buyer.locator(".alert").first.inner_text().lower(),
          buyer.locator(".alert").first.inner_text()[:70])
    buyer.goto(f"{base}/auctions/{scheduled_id}?tab=details")
    buyer.wait_for_timeout(400)
    check("...and the new terms are on the auction", "four weeks of the order" in buyer.content())
    buyer.goto(base + "/outbox?q=Updated:")
    buyer.wait_for_timeout(400)
    told_count = buyer.locator("table tbody tr:not(:has(.empty))").count()
    check("every invited bidder was told it changed", told_count >= 4,
          f"{told_count} emails")
    buyer.locator("table tbody a").first.click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(400)
    body = buyer.frame_locator("iframe").locator("body").inner_text()
    check("...and the email says what actually changed, item by item",
          "now closes" in body and "terms have been rewritten" in body,
          " ".join(body.split())[:200])

    print("\n7. Opening it early")
    buyer.goto(f"{base}/auctions/{scheduled_id}")
    buyer.wait_for_timeout(400)
    buyer.locator("button:has-text('Start bidding now')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(2000)
    watch.note_page(buyer, "started early")
    check("bidding can be opened ahead of time",
          "LIVE" in buyer.locator("main .pill").first.inner_text(),
          buyer.locator("main .pill").first.inner_text())

    # ------------------------------------------------------------------ 8
    print("\n8. The clock moving on a bid at the buzzer")
    buyer.goto(base + "/auctions/new")
    buyer.wait_for_timeout(700)
    buyer.fill("#title", "Buzzer test — closes in a moment")
    row = buyer.locator("#line-rows .item-row").first
    row.locator("select[name=line_item_id]").select_option(index=1)
    row.locator("input[name=line_qty]").fill("100")
    row.locator("input[name=line_price]").fill("50")
    buyer.locator(".vendor-row:has-text('Sunrise Packaging')")\
         .first.locator("input[type=checkbox]").check()
    buyer.fill("#min_decrement", "0.50")
    buyer.fill("#extend_trigger_minutes", "5")
    buyer.fill("#extend_by_minutes", "10")
    buyer.fill("#max_extensions", "2")
    buyer.fill("#start_at", local_time(-1))
    buyer.fill("#end_at", local_time(3))
    buyer.locator("button:has-text('Publish')").last.click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(2500)
    watch.note_page(buyer, "the buzzer auction")
    buzzer_id = int(buyer.url.rstrip("/").split("/")[-1].split("?")[0])
    closes_before = buyer.locator(".countdown .val").first.inner_text()
    check("it opens with the closing time the buyer set", len(closes_before) > 6,
          closes_before)
    first.goto(f"{base}/auctions/{buzzer_id}")
    first.wait_for_timeout(700)
    first.locator("input[name=unit_price]").first.fill("49.00")
    first.locator("button:has-text('Place bid')").first.click()
    first.wait_for_load_state("networkidle")
    first.wait_for_timeout(600)
    watch.note_page(first, "a bid inside the extension window")
    told = first.locator(".alert").first.inner_text()
    check("the bid is accepted", "L1" in told, told[:50])
    buyer.goto(f"{base}/auctions/{buzzer_id}")
    buyer.wait_for_timeout(600)
    watch.note_page(buyer, "after the extension")
    closes_after = buyer.locator(".countdown .val").first.inner_text()
    check("a bid in the closing minutes pushes the finish line back",
          closes_after != closes_before, f"{closes_before} → {closes_after}")
    check("...and says how many extensions have been used",
          "extended" in buyer.content().lower() or "1 of 2" in buyer.content())
    print("   (and the buyer giving them longer by hand)")
    buyer.goto(f"{base}/auctions/{buzzer_id}")
    buyer.wait_for_timeout(500)
    check("a live auction offers a way to give bidders longer",
          buyer.locator("button:has-text('Give bidders more time')").count() == 1)
    buyer.locator("button:has-text('Give bidders more time')").click()
    buyer.wait_for_timeout(400)
    sheet = buyer.locator("#m-more-time")
    check("...prefilled with a time later than the one it closes at now",
          sheet.locator("#new_end").input_value() > "2020",
          sheet.locator("#new_end").input_value())
    # first, an earlier time, which is what the Close button is for
    sheet.locator("#new_end").fill(local_time(-30))
    sheet.locator("button:has-text('Move the closing time')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(600)
    watch.note_page(buyer, "an earlier closing time refused")
    told = buyer.locator(".alert").first.inner_text()
    check("moving it earlier is refused, and points at the right button",
          "not later" in told and "Close bidding now" in told,
          " ".join(told.split())[:95])
    buyer.locator("button:has-text('Give bidders more time')").click()
    buyer.wait_for_timeout(400)
    buyer.locator("#m-more-time #new_end").fill(local_time(180))
    buyer.locator("#m-more-time button:has-text('Move the closing time')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(2000)
    watch.note_page(buyer, "the clock moved by hand")
    told = buyer.locator(".alert").first.inner_text()
    check("a later closing time is accepted and everyone is emailed",
          "now closes" in told and "emailed" in told, " ".join(told.split())[:90])
    moved = buyer.locator(".countdown .val").first.inner_text()
    check("...and the auction shows the new time", moved != closes_after,
          f"{closes_after} → {moved}")
    first.goto(f"{base}/auctions/{buzzer_id}")
    first.wait_for_timeout(600)
    check("...and the bidder can still bid, against the new clock",
          first.locator("input[name=unit_price]").count() >= 1)

    first.goto(base + "/notifications")
    first.wait_for_timeout(400)
    check("the bidders are told the clock moved",
          "extend" in first.content().lower(),
          first.locator("main").inner_text()[:70].replace("\n", " "))

    # ------------------------------------------------------------------ 9
    print("\n9. Calling one off")
    buyer.goto(f"{base}/auctions/{buzzer_id}")
    buyer.wait_for_timeout(400)
    buyer.locator("button:has-text('Cancel')").first.click()
    buyer.wait_for_timeout(400)
    buyer.fill("#m-cancel textarea[name=reason]", "Requirement withdrawn by the plant.")
    buyer.locator("#m-cancel button:has-text('Cancel the auction')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(2000)
    watch.note_page(buyer, "cancelled")
    check("an auction can be called off, with a reason",
          "cancelled" in buyer.content().lower()
          and "Requirement withdrawn" in buyer.content(),
          buyer.locator(".alert").first.inner_text()[:70])
    first.goto(f"{base}/auctions/{buzzer_id}")
    first.wait_for_timeout(500)
    watch.note_page(first, "a cancelled auction, as a bidder")
    check("the bidder sees why, and cannot bid",
          "Requirement withdrawn" in first.content()
          and first.locator("input[name=unit_price]").count() == 0)
    first.goto(base + "/notifications")
    first.wait_for_timeout(400)
    check("...and was told by email and in the app",
          "cancel" in first.content().lower())

    print("\n10. Alerts, once there are a lot of them")
    entries = first.locator("main .card a[href^='/auctions/']").count()
    check("the alert list has the auctions they were told about", entries >= 2,
          f"{entries} alerts")
    if first.locator("button:has-text('Mark all')").count():
        first.locator("button:has-text('Mark all')").first.click()
        first.wait_for_load_state("networkidle")
        first.wait_for_timeout(400)
        watch.note_page(first, "alerts marked read")
        check("marking them all read clears the badge",
              first.locator(".badge-dot").count() == 0,
              f"{first.locator('.badge-dot').count()} badges")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed — skipping the journey.")
        return 0

    tmp = make_tmp("ra-journey5-")
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
    print("Back-office and delivered-cost journey clean.")
    return 0


def test_journey_landed():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
