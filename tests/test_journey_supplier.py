"""Journey 2 — everything one supplier does, invitation to outcome.

A buyer sets up an auction and invites two suppliers who have never used the
platform. One of them does the whole thing: reads the email, sets a password,
downloads the drawing, bids, gets outbid, bids again, withdraws a mistake,
asks the buyer a question, sends a certificate, and hears the result.

    python tests/test_journey_supplier.py
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


def check(label, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


def build_auction(buyer, base, tmp):
    """The buyer sets up an auction with two brand-new suppliers and a drawing."""
    buyer.goto(base + "/masters")
    buyer.wait_for_timeout(300)
    for name, email in (("Kumar Rubber Works", "quotes@kumarrubber.example"),
                        ("Salem Polymers", "tenders@salempoly.example")):
        buyer.fill("form[action='/masters/vendors'] input[name=name]", name)
        buyer.fill("form[action='/masters/vendors'] input[name=email]", email)
        buyer.click("form[action='/masters/vendors'] button[type=submit]")
        buyer.wait_for_load_state("networkidle")
    buyer.goto(base + "/masters?tab=units")
    buyer.wait_for_timeout(200)
    buyer.fill("form[action='/masters/units'] input[name=code]", "NOS")
    buyer.click("form[action='/masters/units'] button[type=submit]")
    buyer.wait_for_load_state("networkidle")
    buyer.goto(base + "/masters?tab=items")
    buyer.wait_for_timeout(200)
    buyer.fill("form[action='/masters/items'] input[name=name]", "Rubber gasket, 80 mm")
    buyer.select_option("form[action='/masters/items'] select[name=default_unit_id]", index=1)
    buyer.click("form[action='/masters/items'] button[type=submit]")
    buyer.wait_for_load_state("networkidle")

    buyer.goto(base + "/auctions/new")
    buyer.wait_for_timeout(400)
    buyer.fill("#title", "Rubber gaskets — Q4 volumes")
    buyer.fill("#description", "80 mm nitrile gasket, 3 mm thick. Drawing attached.")
    buyer.fill("#terms", "45 days credit. Delivery to Hosur plant, in four monthly lots.")
    rows = buyer.locator("#line-rows .item-row")
    rows.first.locator("select[name=line_item_id]").select_option(index=1)
    rows.first.locator("input[name=line_qty]").fill("40000")
    rows.first.locator("input[name=line_price]").fill("12.50")
    rows.first.locator("input[name=line_spec]").fill("Nitrile, shore A 70")
    boxes = buyer.locator("#vendor-list input[type=checkbox]")
    for index in range(boxes.count()):
        boxes.nth(index).check()
    buyer.fill("#min_decrement", "0.10")
    buyer.fill("#start_at", local_time(-1))
    buyer.fill("#end_at", local_time(240))
    buyer.click("#publish-btn")
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(2500)
    url = buyer.url.split("?")[0]

    # attach the drawing the description promises
    drawing = Path(tmp) / "gasket-drawing-revB.pdf"
    drawing.write_bytes(b"%PDF-1.4\n" + b"drawing " * 300)
    buyer.goto(url + "?tab=documents")
    buyer.wait_for_timeout(400)
    buyer.set_input_files("#files", str(drawing))
    buyer.select_option("#item_id", index=1)
    buyer.fill("#note", "Revision B — 3 mm, not 2.5 mm")
    buyer.click("form[enctype='multipart/form-data'] button[type=submit]")
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(500)
    return url


def run(browser, base, tmp, watch):                                # noqa: C901
    buyer = browser.new_context(viewport={"width": 1440, "height": 1000}).new_page()
    buyer.on("dialog", lambda d: d.accept())
    watch.attach(buyer, "BUYER ")
    sign_in(buyer, base, "buyer@demo.in", "demo1234")
    url = build_auction(buyer, base, tmp)
    auction_id = url.rstrip("/").split("/")[-1]

    # ------------------------------------------------------------------ 1
    print("\n1. The email that arrives")
    link = join_link_from_outbox(buyer, base, "quotes@kumarrubber.example")
    check("the invitation carries a way in", link is not None, str(link)[:60])
    body = buyer.frame_locator("iframe").locator("body").inner_text()
    check("it says the lowest price wins", "lowest" in body.lower())
    check("it says what the starting price means",
          "maximum" in body.lower() or "ceiling" in body.lower())
    check("it quotes the per-unit ceiling, not a total",
          "12.50" in body and "500,000" not in body, body[body.find("Starting"):][:60])
    check("it gives the closing time", "Ends" in body)

    # ------------------------------------------------------------------ 2
    print("\n2. Setting a password")
    sup = browser.new_context(viewport={"width": 1380, "height": 1000}).new_page()
    sup.on("dialog", lambda d: d.accept())
    watch.attach(sup, "SUPPLIER ")
    sup.goto(link)
    sup.wait_for_timeout(300)
    watch.note_page(sup, "the join page")
    check("the page knows which supplier they are",
          "Kumar Rubber Works" in sup.content())
    check("their address is fixed, not editable",
          sup.locator("input[disabled]").first.input_value() == "quotes@kumarrubber.example")

    sup.fill("#name", "Meena Kumar")
    sup.fill("#password", "gasket1")
    sup.fill("#confirm", "gasket2")
    sup.click("button[type=submit]")
    sup.wait_for_load_state("networkidle")
    watch.note_page(sup, "mismatched passwords")
    check("two different passwords are caught, kindly",
          "not the same" in sup.content(), sup.locator(".alert").first.inner_text()[:50]
          if sup.locator(".alert").count() else "no message")
    check("...and their name is still filled in",
          sup.input_value("#name") == "Meena Kumar", sup.input_value("#name"))
    sup.fill("#password", "gasket1")
    sup.fill("#confirm", "gasket1")
    sup.click("button[type=submit]")
    sup.wait_for_load_state("networkidle")
    watch.note_page(sup, "the supplier dashboard")
    check("they are signed in and welcomed by name", "Meena" in sup.content(), sup.url)

    # ------------------------------------------------------------------ 3
    print("\n3. Finding their way around")
    check("their dashboard shows the auction they are invited to",
          "Rubber gaskets" in sup.content())
    for path, what in (("/auctions", "the auction list"),
                       ("/notifications", "their alerts"),
                       ("/team", "their team page")):
        sup.goto(base + path)
        sup.wait_for_load_state("networkidle")
        watch.note_page(sup, what)
        check(f"{what} works for a supplier",
              "went wrong" not in sup.content(), sup.url)
    for path in ("/masters", "/reports", "/outbox", "/auctions/new"):
        sup.goto(base + path)
        sup.wait_for_timeout(150)
        check(f"a supplier is kept out of {path}",
              "do not have access" in sup.content(), sup.locator("h1").first.inner_text()[:40])
    watch.forgive_http("403")

    # ------------------------------------------------------------------ 4
    print("\n4. Reading the auction before bidding")
    sup.goto(f"{base}/auctions/{auction_id}")
    sup.wait_for_timeout(900)
    watch.note_page(sup, "the bidding screen")
    check("they see the item, quantity and ceiling",
          "Rubber gasket" in sup.content() and "40,000" in sup.content()
          and "12.50" in sup.content())
    check("they see the specification", "shore A 70" in sup.content())
    check("the drawing is offered beside the item",
          "gasket-drawing-revB.pdf" in sup.content())
    sup.goto(f"{base}/auctions/{auction_id}?tab=details")
    sup.wait_for_timeout(300)
    check("the details tab shows the buyer's terms", "45 days credit" in sup.content())
    check("...and the bidding rules in words",
          "Minimum decrement" in sup.content() and "0.10" in sup.content())
    check("a supplier cannot see who else was invited",
          "Salem Polymers" not in sup.content())
    sup.goto(f"{base}/auctions/{auction_id}?tab=documents")
    sup.wait_for_timeout(300)
    with sup.expect_download() as info:
        sup.locator("a[href*='/documents/']").first.click()
    download = info.value
    check("they can actually download the drawing",
          download.suggested_filename.endswith(".pdf"), download.suggested_filename)

    # ------------------------------------------------------------------ 5
    print("\n5. Bidding")
    sup.goto(f"{base}/auctions/{auction_id}")
    sup.wait_for_timeout(700)
    box = sup.locator("input[name=unit_price]").first
    sup.locator("[data-fill]").first.click()
    sup.wait_for_timeout(200)
    check("the suggested price fills the box", box.input_value() == "12.5",
          box.input_value())
    hint = sup.locator("[id^=total]").first.inner_text()
    check("...and the line total is worked out for them",
          "500,000" in hint, hint[:60])
    box.fill("13")
    sup.locator("button:has-text('Place bid')").first.click()
    sup.wait_for_timeout(400)
    watch.note_page(sup, "a bid above the ceiling")
    # The page stops the mistake before it costs a round trip, and says so in
    # the app's own words. It used to rely on the browser's native max=, whose
    # tooltip is terse, appears in the browser's language, and vanishes - so a
    # bidder who missed it saw Place bid do nothing at all.
    problem = sup.locator(".field-error:visible")
    check("the box itself refuses a bid above the ceiling",
          problem.count() > 0 and "12.50" in problem.first.inner_text(),
          problem.first.inner_text()[:70] if problem.count() else "nothing was said")
    check("...and the page is still there, with the price still typed",
          box.input_value() == "13", box.input_value())
    # ...and the server refuses it too, for anyone who gets past the box.
    box.evaluate("el => el.removeAttribute('data-max')")
    sup.locator("button:has-text('Place bid')").first.click()
    sup.wait_for_load_state("networkidle")
    watch.note_page(sup, "a bid above the ceiling, server side")
    check("a bid above the ceiling is refused, in words they can act on",
          "above the starting price" in sup.content(),
          sup.locator(".alert").first.inner_text()[:70]
          if sup.locator(".alert").count() else "no message")
    sup.locator("input[name=unit_price]").first.fill("12.40")
    sup.locator("button:has-text('Place bid')").first.click()
    sup.wait_for_load_state("networkidle")
    sup.wait_for_timeout(400)
    watch.note_page(sup, "first bid placed")
    check("a good bid lands and tells them where they stand",
          "L1" in sup.locator(".alert").first.inner_text(),
          sup.locator(".alert").first.inner_text()[:70])
    check("their own bid is shown back to them", "12.40" in sup.content())

    # ------------------------------------------------------------------ 6
    print("\n6. Being outbid, and coming back")
    rival_link = join_link_from_outbox(buyer, base, "tenders@salempoly.example")
    rival = browser.new_context(viewport={"width": 1200, "height": 900}).new_page()
    rival.on("dialog", lambda d: d.accept())
    watch.attach(rival, "RIVAL ")
    rival.goto(rival_link)
    rival.fill("#name", "Salem Desk")
    rival.fill("#password", "salem12")
    rival.fill("#confirm", "salem12")
    rival.click("button[type=submit]")
    rival.wait_for_load_state("networkidle")
    rival.goto(f"{base}/auctions/{auction_id}")
    rival.wait_for_timeout(700)
    rival.locator("input[name=unit_price]").first.fill("12.20")
    rival.locator("button:has-text('Place bid')").first.click()
    rival.wait_for_load_state("networkidle")
    rival.wait_for_timeout(400)
    check("the rival takes the lead", "L1" in rival.locator(".alert").first.inner_text(),
          rival.locator(".alert").first.inner_text()[:60])
    check("the rival cannot see the first supplier's name",
          "Kumar Rubber" not in rival.content())

    sup.goto(base + "/notifications")
    sup.wait_for_timeout(300)
    watch.note_page(sup, "outbid alert")
    check("the first supplier is told they have been outbid",
          "outbid" in sup.content().lower(), sup.locator("main").inner_text()[:70])
    link_row = sup.locator("a[href*='/auctions/']").first
    link_row.click()
    sup.wait_for_load_state("networkidle")
    sup.wait_for_timeout(800)
    check("the alert takes them straight to the auction",
          f"/auctions/{auction_id}" in sup.url, sup.url)
    note = sup.locator(".range-note").first.inner_text()
    check("the screen tells them exactly what to bid to win",
          "12.10" in note, " ".join(note.split())[:120])
    check("...and that they are behind",
          "L2" in sup.content() or "Go lower" in sup.content())
    sup.locator("input[name=unit_price]").first.fill("12.10")
    sup.locator("button:has-text('Place bid')").first.click()
    sup.wait_for_load_state("networkidle")
    sup.wait_for_timeout(400)
    check("they take the lead back", "L1" in sup.locator(".alert").first.inner_text(),
          sup.locator(".alert").first.inner_text()[:60])

    # ------------------------------------------------------------------ 7
    print("\n7. A mistake, withdrawn")
    sup.locator("input[name=unit_price]").first.fill("1.10")
    sup.locator("button:has-text('Place bid')").first.click()
    sup.wait_for_load_state("networkidle")
    sup.wait_for_timeout(400)
    check("a fat-fingered price is accepted (it is legal, just painful)",
          "1.10" in sup.content(), sup.locator(".alert").first.inner_text()[:60])
    sup.locator("summary:has-text('Your bids on this item')").first.click()
    sup.wait_for_timeout(300)
    rows = sup.locator("details:has-text('Your bids on this item') .mono.strong")
    check("their own bids are listed newest first, so the last one is on top",
          rows.first.inner_text().strip().endswith("1.10"),
          rows.first.inner_text().strip())
    sup.locator("button:has-text('Withdraw')").first.click()
    sup.wait_for_load_state("networkidle")
    sup.wait_for_timeout(500)
    watch.note_page(sup, "after withdrawing")
    check("they can withdraw it themselves",
          "withdrawn" in sup.locator(".alert").first.inner_text().lower(),
          sup.locator(".alert").first.inner_text()[:70])
    check("...and the board goes back to the price that is really lowest",
          "12.10" in sup.locator(".range-note").first.inner_text()
          or "12.00" in sup.locator(".range-note").first.inner_text(),
          " ".join(sup.locator(".range-note").first.inner_text().split())[:90])
    sup.wait_for_timeout(300)
    sup.locator("input[name=unit_price]").first.fill("5.00")
    sup.locator("button:has-text('Place bid')").first.click()
    sup.wait_for_load_state("networkidle")
    sup.wait_for_timeout(400)
    check("...but cannot then bid above what they withdrew",
          "withdrew" in sup.content(), sup.locator(".alert").first.inner_text()[:90])

    # ------------------------------------------------------------------ 8
    print("\n8. Asking the buyer a question")
    sup.goto(f"{base}/auctions/{auction_id}?tab=conversation")
    sup.wait_for_timeout(400)
    sup.fill("textarea[name=body]", "Is shore A 70 firm, or would A 65 be acceptable?")
    sup.locator("button:has-text('Send')").first.click()
    sup.wait_for_load_state("networkidle")
    sup.wait_for_timeout(400)
    watch.note_page(sup, "message sent")
    check("the question is sent and shown back",
          "shore A 70 firm" in sup.content(),
          sup.locator(".alert").first.inner_text()[:60])
    buyer.goto(f"{base}/auctions/{auction_id}?tab=conversation")
    buyer.wait_for_timeout(400)
    check("the buyer sees it under that supplier's name",
          "shore A 70 firm" in buyer.content() and "Kumar Rubber Works" in buyer.content())
    thread = buyer.locator("details:has-text('Kumar Rubber Works')").first
    check("the thread that is waiting on an answer is the one open, and first",
          thread.evaluate("el => el.open")
          and buyer.locator("details.qa summary").first.inner_text().startswith("Kumar"),
          buyer.locator("details.qa summary").first.inner_text()[:40])
    thread.locator("textarea[name=body]").fill("A 70 only, please — it has to hold at 6 bar.")
    thread.locator("button:has-text('Send')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(400)
    sup.goto(f"{base}/auctions/{auction_id}?tab=conversation")
    sup.wait_for_timeout(300)
    check("the supplier gets the answer", "hold at 6 bar" in sup.content())
    rival.goto(f"{base}/auctions/{auction_id}?tab=conversation")
    rival.wait_for_timeout(300)
    check("the rival cannot read that conversation",
          "shore A 70 firm" not in rival.content() and "6 bar" not in rival.content())

    # ------------------------------------------------------------------ 9
    print("\n9. Sending a certificate")
    cert = Path(tmp) / "nitrile-test-certificate.pdf"
    cert.write_bytes(b"%PDF-1.4\n" + b"cert " * 200)
    sup.goto(f"{base}/auctions/{auction_id}?tab=documents")
    sup.wait_for_timeout(400)
    sup.set_input_files("#myfiles", str(cert))
    sup.fill("#mynote", "Batch 4471, shore A 70 verified")
    sup.locator("form[enctype='multipart/form-data'] button:has-text('Attach')").click()
    sup.wait_for_load_state("networkidle")
    sup.wait_for_timeout(500)
    watch.note_page(sup, "certificate attached")
    check("their certificate attaches, and says who can see it",
          "Only the buyer can see" in sup.locator(".alert").first.inner_text(),
          sup.locator(".alert").first.inner_text()[:80])
    rival.goto(f"{base}/auctions/{auction_id}?tab=documents")
    rival.wait_for_timeout(300)
    check("the rival cannot see it exists",
          "nitrile-test-certificate" not in rival.content()
          and "Batch 4471" not in rival.content())
    buyer.goto(f"{base}/auctions/{auction_id}?tab=documents")
    buyer.wait_for_timeout(300)
    check("the buyer can see it, and whose it is",
          "nitrile-test-certificate.pdf" in buyer.content()
          and "Kumar Rubber Works" in buyer.content())

    # ------------------------------------------------------------------ 10
    print("\n10. Hearing the outcome")
    buyer.goto(f"{base}/auctions/{auction_id}")
    buyer.wait_for_timeout(400)
    buyer.locator("button:has-text('Close bidding now')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(1500)
    watch.note_page(buyer, "closed")
    check("the buyer can close it early", "closed" in buyer.content().lower())
    sup.goto(f"{base}/auctions/{auction_id}")
    sup.wait_for_timeout(500)
    watch.note_page(sup, "the supplier sees it closed")
    check("the supplier sees it is closed, and what happens next",
          "reviewing the bids" in sup.content(), sup.locator(".alert").first.inner_text()[:70])
    check("...and the bid box is gone",
          sup.locator("input[name=unit_price]").count() == 0)

    buyer.goto(f"{base}/auctions/{auction_id}/award")
    buyer.wait_for_timeout(400)
    watch.note_page(buyer, "the award screen")
    radios = buyer.locator("input[type=radio][data-line]")
    check("the award screen offers the bidders", radios.count() >= 2,
          f"{radios.count()} options")
    buyer.locator("button:has-text('Confirm award')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(2500)
    watch.note_page(buyer, "awarded")
    check("the award goes through and quotes the savings",
          "Awarded to" in buyer.content() and "Savings" in buyer.content(),
          buyer.locator(".alert").first.inner_text()[:90])

    sup.goto(base + "/notifications")
    sup.wait_for_timeout(400)
    text = sup.locator("main").inner_text()
    check("the winner is told they won", "awarded" in text.lower(), text[:80])
    sup.goto(f"{base}/auctions/{auction_id}?tab=award")
    sup.wait_for_timeout(400)
    watch.note_page(sup, "the award, as the winner")
    check("the winner can see what they won",
          "12.10" in sup.content() or "Awarded" in sup.content())
    rival.goto(f"{base}/auctions/{auction_id}?tab=award")
    rival.wait_for_timeout(300)
    check("the loser is not shown the winner's price",
          "Kumar Rubber" not in rival.content())
    sup.goto(base + "/")
    sup.wait_for_timeout(400)
    watch.note_page(sup, "the supplier dashboard, after winning")
    check("their dashboard reflects the win",
          "won" in sup.locator("main").inner_text().lower()
          or "1" in sup.locator(".stat").first.inner_text())


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed — skipping the journey.")
        return 0

    tmp = make_tmp("ra-journey2-")
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
    print("Supplier journey clean.")
    return 0


def test_journey_supplier():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
