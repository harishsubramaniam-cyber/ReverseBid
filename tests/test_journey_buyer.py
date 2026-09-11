"""Journey 3 — a buyer's day, on the demo data.

Priya signs in to a running platform, watches a live auction, answers a
bidder, sends out a drawing, closes early, awards the business at a
negotiated price, and then checks that every number she is shown agrees with
every other number — on the screen, in the CSV and in the PDF.

    python tests/test_journey_buyer.py
"""
from __future__ import annotations

import csv
import io
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


def money(text: str) -> float:
    """The first money figure in a lump of text, as a number."""
    found = re.search(r"([\d,]+\.\d\d)", text.replace("₹", ""))
    return float(found.group(1).replace(",", "")) if found else float("nan")


def run(browser, base, tmp, watch):                                   # noqa: C901
    buyer = browser.new_context(viewport={"width": 1500, "height": 1000}).new_page()
    buyer.on("dialog", lambda d: d.accept())
    watch.attach(buyer, "BUYER ")

    # ------------------------------------------------------------------ 1
    print("\n1. Signing in and reading the dashboard")
    sign_in(buyer, base, "buyer@demo.in", PW)
    watch.note_page(buyer, "the dashboard")
    check("the buyer lands on their dashboard", buyer.url.rstrip("/") == base,
          buyer.url)
    tiles = {}
    for index in range(buyer.locator(".stat").count()):
        tile = buyer.locator(".stat").nth(index)
        tiles[tile.locator(".k").inner_text().strip().lower()] = \
            tile.locator(".v").inner_text().strip()
    check("the savings tile is a real number, not a placeholder",
          money(tiles.get("total savings", "")) > 0
          and "nan" not in tiles.get("total savings", "").lower(),
          tiles.get("total savings"))
    check("savings equal baseline minus awarded value",
          abs((money(tiles["baseline value"]) - money(tiles["awarded value"]))
              - money(tiles["total savings"])) < 1.0,
          f"{tiles['baseline value']} − {tiles['awarded value']} vs {tiles['total savings']}")
    check("the counts line reads sensibly",
          "live" in buyer.locator(".stat").last.locator(".h").inner_text(),
          buyer.locator(".stat").last.locator(".h").inner_text()[:60])
    check("the closing-soon list names the live auction",
          "live demo" in buyer.content())
    check("the month chart has bars, not a blank box",
          buyer.locator(".spark .b").count() >= 3,
          f"{buyer.locator('.spark .b').count()} bars")

    # ------------------------------------------------------------------ 2
    print("\n2. Finding the auction again")
    buyer.goto(base + "/auctions")
    buyer.wait_for_load_state("networkidle")
    watch.note_page(buyer, "the auction list")
    total = buyer.locator("table tbody tr").count()
    check("every auction is listed", total >= 5, f"{total} rows")
    buyer.fill("#q", "stretch film")
    buyer.click("button:has-text('Filter')")
    buyer.wait_for_load_state("networkidle")
    check("search narrows it down", buyer.locator("table tbody tr").count() == 1,
          f"{buyer.locator('table tbody tr').count()} rows")
    check("...and keeps what was typed", buyer.input_value("#q") == "stretch film")
    buyer.goto(base + "/auctions?status=live")
    buyer.wait_for_load_state("networkidle")
    check("the status filter works too",
          buyer.locator("table tbody tr").count() == 1
          and "live" in buyer.locator("table tbody .pill").first.inner_text().lower(),
          buyer.locator("table tbody .pill").first.inner_text())
    buyer.locator("table tbody a:has-text('Open')").first.click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(600)
    watch.note_page(buyer, "the live auction")
    auction_id = int(buyer.url.rstrip("/").split("/")[-1].split("?")[0])
    check("Open takes them into the auction", f"/auctions/{auction_id}" in buyer.url,
          buyer.url)

    # ------------------------------------------------------------------ 3
    print("\n3. Watching it run")
    check("the clock is counting, not blank",
          len(buyer.locator(".clock").first.inner_text().strip()) >= 4,
          buyer.locator(".clock").first.inner_text())
    check("the auto-extension rule is spelled out", "Auto-extension" in buyer.content())
    check("the tiles say how much has been bid",
          "bids from" in buyer.content())
    board = buyer.locator("#live-board").inner_text()
    check("the board ranks the bidders L1 first", "L1" in board and "L2" in board)
    check("the buyer sees real supplier names, not aliases",
          "Sunrise Packaging" in board or "Deccan Industrial" in board)
    baseline = money(buyer.locator(".stat").first.locator(".v").inner_text())
    best = money(buyer.locator(".stat").nth(1).locator(".v").inner_text())
    saving = money(buyer.locator(".stat").nth(2).locator(".v").inner_text())
    check("the auction's own tiles reconcile",
          abs((baseline - best) - saving) < 1.0, f"{baseline} − {best} vs {saving}")
    check("the savings percentage is not silly",
          0 <= float(re.search(r"([\d.]+)%", buyer.content()).group(1)) < 100)

    # a bidder undercuts while the buyer is watching the board
    print("   (a bid arrives while the page is open)")
    rival = browser.new_context(viewport={"width": 1200, "height": 900}).new_page()
    rival.on("dialog", lambda d: d.accept())
    watch.attach(rival, "BIDDER ")
    sign_in(rival, base, "vendor4@demo.in", PW)
    rival.goto(f"{base}/auctions/{auction_id}")
    rival.wait_for_timeout(700)
    box = rival.locator("input[name=unit_price]").first
    suggested = box.evaluate("el => el.placeholder")
    rival.locator("[data-fill]").first.click()
    rival.wait_for_timeout(150)
    rival.locator("button:has-text('Place bid')").first.click()
    rival.wait_for_load_state("networkidle")
    rival.wait_for_timeout(400)
    check("the bidder's own suggested price is accepted as-is",
          "L1" in rival.locator(".alert").first.inner_text(),
          f"{suggested} → {rival.locator('.alert').first.inner_text()[:44]}")
    buyer.wait_for_timeout(7000)          # the board refreshes itself
    watch.note_page(buyer, "the board after a bid")
    check("the buyer's board picks the new bid up on its own",
          "Coastal Logistics" in buyer.locator("#live-board").inner_text(),
          buyer.locator("#live-board").inner_text()[:60].replace("\n", " "))
    check("...without the buyer's tab having to be reloaded",
          f"/auctions/{auction_id}" in buyer.url, buyer.url)

    # ------------------------------------------------------------------ 4
    print("\n4. Answering a bidder")
    buyer.goto(f"{base}/auctions/{auction_id}?tab=conversation")
    buyer.wait_for_timeout(400)
    watch.note_page(buyer, "the conversation")
    check("the question that was already waiting is there",
          "5-ply specification firm" in buyer.content())
    check("each bidder is a thread of their own",
          buyer.locator("details.qa").count() >= 3,
          f"{buyer.locator('details.qa').count()} threads")
    thread = buyer.locator("details.qa:has-text('Coastal Logistics')").first
    thread.locator("summary").click()
    thread.locator("textarea[name=body]").fill(
        "Lead time is four weeks from the purchase order. Please confirm you can hold that.")
    thread.locator("button:has-text('Send')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(400)
    watch.note_page(buyer, "after replying")
    check("the reply is sent", "four weeks" in buyer.content(),
          buyer.locator(".alert").first.inner_text()[:50])
    rival.goto(f"{base}/auctions/{auction_id}?tab=conversation")
    rival.wait_for_timeout(300)
    check("...and reaches that bidder alone", "four weeks" in rival.content())
    check("the other bidders' questions are not shown to them",
          "5-ply specification firm" not in rival.content())

    # ------------------------------------------------------------------ 5
    print("\n5. Sending out a drawing mid-auction")
    drawing = Path(tmp) / "box-drawing-revC.pdf"
    drawing.write_bytes(b"%PDF-1.4\n" + b"drawing " * 300)
    buyer.goto(f"{base}/auctions/{auction_id}?tab=documents")
    buyer.wait_for_timeout(400)
    buyer.set_input_files("#files", str(drawing))
    buyer.select_option("#item_id", index=1)
    buyer.fill("#note", "Revision C, supersedes rev B")
    buyer.locator("form.doc-drop button:has-text('Attach')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(500)
    watch.note_page(buyer, "drawing attached")
    check("the drawing attaches and says who can see it",
          "bidder" in buyer.locator(".alert").first.inner_text().lower(),
          buyer.locator(".alert").first.inner_text()[:70])
    check("...and it is filed against the item, with the note",
          "Revision C" in buyer.content() and "for Corrugated box" in buyer.content())
    rival.goto(f"{base}/auctions/{auction_id}?tab=documents")
    rival.wait_for_timeout(300)
    check("every bidder can see it", "box-drawing-revC.pdf" in rival.content())
    rival.goto(f"{base}/auctions/{auction_id}")
    rival.wait_for_timeout(600)
    check("...and it is offered beside the item they are bidding on",
          "box-drawing-revC.pdf" in rival.content())

    # ------------------------------------------------------------------ 6
    print("\n6. Closing early")
    buyer.goto(f"{base}/auctions/{auction_id}")
    buyer.wait_for_timeout(500)
    buyer.locator("button:has-text('Close bidding now')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(1500)
    watch.note_page(buyer, "closed")
    check("the auction closes", "Bidding is closed" in buyer.content(),
          buyer.locator(".alert").first.inner_text()[:60])
    check("...and the way to award is offered right there",
          buyer.locator("a:has-text('Award this auction')").count() == 1)
    rival.goto(f"{base}/auctions/{auction_id}")
    rival.wait_for_timeout(500)
    check("the bidder can no longer bid",
          rival.locator("input[name=unit_price]").count() == 0)

    # ------------------------------------------------------------------ 7
    print("\n7. Awarding, at a price that was negotiated afterwards")
    buyer.locator("a:has-text('Award this auction')").click()
    buyer.wait_for_load_state("networkidle")
    watch.note_page(buyer, "the award screen")
    check("every line is pre-set to its lowest bidder",
          buyer.locator("tr.is-best").count() == buyer.locator(".line-card").count(),
          f"{buyer.locator('tr.is-best').count()} of "
          f"{buyer.locator('.line-card').count()} lines")
    first_line = buyer.locator(".line-card").first
    l1_price = money(first_line.locator("tr.is-best td").nth(3).inner_text())
    prefilled = float(first_line.locator("input[name^=price_]").input_value())
    check("...and the award price is filled in with what they actually bid",
          abs(l1_price - prefilled) < 0.005, f"{l1_price} vs {prefilled}")

    # quick fill to one supplier, then back to L1 — the prices must follow
    name = buyer.locator(".card.tight .chip").nth(1).inner_text().replace("Everything to ", "")
    buyer.locator(".card.tight .chip").nth(1).click()
    buyer.wait_for_timeout(300)
    moved = float(first_line.locator("input[name^=price_]").input_value())
    picked = first_line.locator("tr.is-best td").nth(1).inner_text().strip()
    check(f"“Everything to {name}” moves the winner and the price together",
          picked.startswith(name.split()[0])
          and abs(moved - money(first_line.locator("tr.is-best td").nth(3).inner_text())) < 0.005,
          f"{picked} at {moved}")
    buyer.locator(".card.tight .chip").first.click()
    buyer.wait_for_timeout(300)
    back = float(first_line.locator("input[name^=price_]").input_value())
    check("“Every item to its own L1” puts it back", abs(back - l1_price) < 0.005,
          f"{back} vs {l1_price}")

    # the real world: the winner shaved a little more off on the phone
    negotiated = round(l1_price - 0.25, 2)
    first_line.locator("input[name^=price_]").fill(str(negotiated))
    first_line.locator("input[name^=note_]").fill("Agreed on the phone, 9 Sep.")
    buyer.locator("button:has-text('Confirm award')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(2000)
    watch.note_page(buyer, "awarded")
    flash = buyer.locator(".alert").first.inner_text()
    check("the award goes through and quotes the savings", "Awarded to" in flash,
          " ".join(flash.split())[:90])
    check("...and everyone was emailed", "emailed" in flash)
    buyer.goto(f"{base}/auctions/{auction_id}?tab=award")
    buyer.wait_for_timeout(400)
    watch.note_page(buyer, "the award tab")
    check("the awarded price is the negotiated one, not the bid",
          f"{negotiated:,.2f}" in buyer.content(), f"{negotiated}")
    check("the buyer's own note is kept", "Agreed on the phone" in buyer.content())

    # ------------------------------------------------------------------ 8
    print("\n8. Do the numbers agree with each other?")
    buyer.goto(f"{base}/auctions/{auction_id}")
    buyer.wait_for_timeout(400)
    on_auction = money(buyer.locator(".stat").nth(2).locator(".v").inner_text())
    buyer.goto(base + "/reports?date_from=2000-01-01")
    buyer.wait_for_timeout(600)
    watch.note_page(buyer, "reports")
    row = buyer.locator(f"table tbody tr:has(a[href='/reports/auction/{auction_id}'])").first
    in_report = money(row.locator("td").nth(7).inner_text())
    check("the auction's savings match the savings report",
          abs(on_auction - in_report) < 1.0, f"{on_auction} vs {in_report}")
    check("the report names who won it", len(row.locator("td").last.inner_text()) > 3,
          row.locator("td").last.inner_text()[:40])
    totals = money(buyer.locator(".stat.hero .v").inner_text())
    each = 0.0
    rows = buyer.locator("table tbody tr")
    for index in range(rows.count()):
        cells = rows.nth(index).locator("td")
        if cells.count() >= 8:
            each += money(cells.nth(7).inner_text())
    check("the report's total is the sum of its own rows",
          abs(totals - each) < 2.0, f"{totals} vs {each}")

    with buyer.expect_download() as info:
        buyer.locator("#dl-csv").click()
    body = Path(info.value.path()).read_text(encoding="utf-8-sig")
    lines = list(csv.reader(io.StringIO(body)))
    csv_row = [r for r in lines if r and str(auction_id) in r[0] or (r and "live demo" in " ".join(r))]
    check("the CSV downloads and has a row per auction", len(lines) >= 4,
          f"{len(lines)} lines")
    check("...and the CSV agrees with the screen",
          any(abs(money(",".join(r)) - 0) >= 0 for r in csv_row) and
          any(f"{in_report:,.2f}".replace(",", "") in ",".join(r).replace(",", "")
              for r in lines),
          f"looking for {in_report}")

    with buyer.expect_download() as info:
        buyer.locator("#dl-pdf").click()
    pdf = Path(info.value.path())
    check("the PDF downloads and is a real PDF",
          pdf.read_bytes()[:5] == b"%PDF-" and pdf.stat().st_size > 1500,
          f"{pdf.stat().st_size} bytes")

    buyer.goto(f"{base}/reports/auction/{auction_id}")
    buyer.wait_for_timeout(500)
    watch.note_page(buyer, "the auction report")
    check("the single-auction report opens",
          "live demo" in buyer.content() and "Savings" in buyer.content())
    check("...and shows the bid history behind the award",
          "withdrawn" in buyer.content().lower() or "Every bid" in buyer.content()
          or buyer.locator("table").count() >= 2,
          f"{buyer.locator('table').count()} tables")
    for kind in ("pdf", "csv"):
        with buyer.expect_download() as info:
            buyer.locator(f"a[href$='/export/{kind}']").first.click()
        got = Path(info.value.path())
        check(f"the auction's own {kind.upper()} downloads", got.stat().st_size > 200,
              f"{got.stat().st_size} bytes")

    # ------------------------------------------------------------------ 9
    print("\n9. The trail it all left")
    buyer.goto(f"{base}/auctions/{auction_id}?tab=history")
    buyer.wait_for_timeout(400)
    watch.note_page(buyer, "the audit trail")
    trail = buyer.locator(".timeline").inner_text()
    for action in ("bid.place", "auction.close", "award"):
        check(f"the trail records {action}", action in trail)
    check("every entry says who did it and when",
          buyer.locator(".timeline li .muted").count()
          == buyer.locator(".timeline li").count(),
          f"{buyer.locator('.timeline li').count()} entries")
    buyer.goto(base + "/outbox")
    buyer.wait_for_timeout(500)
    check("the outcome emails are in the outbox",
          "award" in buyer.content().lower() or "Awarded" in buyer.content())
    before = buyer.locator("table tbody tr:not(:has(.empty))").count()
    who = buyer.locator("table tbody tr td").nth(1).inner_text().split("\n")[-1].strip()
    buyer.fill("input[name=q]", who)
    buyer.locator("form button:has-text('Search')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(300)
    found = buyer.locator("table tbody tr:not(:has(.empty))").count()
    check("...and can be found by who they went to",
          1 <= found <= before, f"{who}: {found} of {before} rows")
    buyer.fill("input[name=q]", "nobody@nowhere.invalid")
    buyer.locator("form button:has-text('Search')").click()
    buyer.wait_for_load_state("networkidle")
    buyer.wait_for_timeout(300)
    watch.note_page(buyer, "an outbox search that finds nothing")
    check("a search that finds nothing says so, rather than “nothing sent yet”",
          "No emails match" in buyer.content(),
          buyer.locator("table tbody .empty").inner_text()[:60]
          if buyer.locator("table tbody .empty").count() else "no empty row")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed — skipping the journey.")
        return 0

    tmp = make_tmp("ra-journey3-")
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
    print("Buyer journey clean.")
    return 0


def test_journey_buyer():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
