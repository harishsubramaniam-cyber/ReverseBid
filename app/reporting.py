"""Report 1 (Total Savings) and Report 2 (Individual Auction Summary),
rendered to HTML in the app and downloadable as CSV or PDF."""
from __future__ import annotations

import csv
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)
from sqlalchemy.orm import Session

from . import config, engine
from .models import Auction, AuctionStatus, Award
from .utils import fmt_dt, fmt_money, fmt_qty

ACCENT = colors.HexColor("#1d4ed8")
LIGHT = colors.HexColor("#f1f5f9")
GREY = colors.HexColor("#64748b")


# ------------------------------------------------------------------ data
def total_savings(db: Session, start: datetime, end: datetime,
                  statuses=(AuctionStatus.AWARDED,)) -> dict:
    query = (db.query(Auction)
               .filter(Auction.status.in_(list(statuses)))
               .filter(Auction.start_at >= start, Auction.start_at <= end)
               .order_by(Auction.start_at.asc()))
    rows = []
    for auction in query.all():
        summary = engine.auction_summary(db, auction)
        awardees = sorted({a.vendor.name for a in summary["awards"]})
        rows.append({
            "auction": auction, "reference": auction.reference, "title": auction.title,
            "date": auction.awarded_at or auction.closed_at or auction.start_at,
            "baseline": summary["baseline"], "final": summary["final_value"],
            "savings": summary["savings"], "savings_pct": summary["savings_pct"],
            "bids": summary["total_bids"], "bidders": summary["active_bidders"],
            "awardees": ", ".join(awardees) or "—",
        })
    totals = {
        "baseline": sum(r["baseline"] for r in rows),
        "final": sum(r["final"] for r in rows),
        "savings": sum(r["savings"] for r in rows),
        "count": len(rows),
    }
    totals["savings_pct"] = (totals["savings"] / totals["baseline"] * 100) if totals["baseline"] else 0.0
    return {"rows": rows, "totals": totals, "start": start, "end": end}


def auction_summary_report(db: Session, auction: Auction) -> dict:
    summary = engine.auction_summary(db, auction)
    lines = []
    for line in auction.lines:
        result = engine.line_result(db, line)
        history = engine.line_bids(db, line.id)
        awards = db.query(Award).filter(Award.line_id == line.id).all()
        lines.append({
            "line": line, "label": engine.line_label(line), "result": result,
            "history": history, "awards": awards,
            "highest": result["highest"], "lowest": result["lowest"],
            "baseline": engine.line_baseline(db, line),
        })
    return {"auction": auction, "summary": summary, "lines": lines,
            "awards": summary["awards"]}


# ------------------------------------------------------------------ CSV
def savings_csv(data: dict) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([f"{config.APP_NAME} — Total Savings Report"])
    writer.writerow([f"Period: {fmt_dt(data['start'], False)} to {fmt_dt(data['end'], False)}"])
    writer.writerow([])
    writer.writerow(["Reference", "Auction", "Awarded on", "Bidders", "Bids",
                     f"Baseline ({config.CURRENCY})", f"Final ({config.CURRENCY})",
                     f"Savings ({config.CURRENCY})", "Savings %", "Awarded to"])
    for row in data["rows"]:
        writer.writerow([row["reference"], row["title"], fmt_dt(row["date"], False),
                         row["bidders"], row["bids"], f"{row['baseline']:.2f}",
                         f"{row['final']:.2f}", f"{row['savings']:.2f}",
                         f"{row['savings_pct']:.2f}", row["awardees"]])
    totals = data["totals"]
    writer.writerow([])
    writer.writerow(["TOTAL", f"{totals['count']} auctions", "", "", "",
                     f"{totals['baseline']:.2f}", f"{totals['final']:.2f}",
                     f"{totals['savings']:.2f}", f"{totals['savings_pct']:.2f}", ""])
    return buffer.getvalue().encode("utf-8-sig")


def auction_csv(db: Session, data: dict) -> bytes:
    auction = data["auction"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([f"{config.APP_NAME} — Auction Summary"])
    writer.writerow(["Reference", auction.reference])
    writer.writerow(["Title", auction.title])
    writer.writerow(["Status", auction.status.value])
    writer.writerow(["Ran", f"{fmt_dt(auction.start_at, False)} to {fmt_dt(auction.end_at, False)}"])
    summary = data["summary"]
    writer.writerow(["Baseline", f"{summary['baseline']:.2f}"])
    writer.writerow(["Final", f"{summary['final_value']:.2f}"])
    writer.writerow(["Savings", f"{summary['savings']:.2f}", f"{summary['savings_pct']:.2f}%"])
    writer.writerow([])
    writer.writerow(["Item", "Qty", "Unit", "Starting price", "Highest bid", "Lowest bid",
                     "Savings", "Awarded to", "Awarded qty", "Awarded price"])
    for entry in data["lines"]:
        line = entry["line"]
        awards = entry["awards"]
        writer.writerow([
            entry["label"], fmt_qty(line.qty), line.unit.code if line.unit else "",
            f"{line.starting_price:.2f}" if line.has_ceiling else "no ceiling",
            f"{entry['highest'].unit_price:.2f}" if entry["highest"] else "",
            f"{entry['lowest'].unit_price:.2f}" if entry["lowest"] else "",
            f"{entry['result']['savings']:.2f}",
            "; ".join(a.vendor.name for a in awards),
            "; ".join(fmt_qty(a.qty) for a in awards),
            "; ".join(f"{a.unit_price:.2f}" for a in awards),
        ])
    writer.writerow([])
    writer.writerow(["Every bid placed"])
    writer.writerow(["Time", "Item", "Bidder", "Unit price", "Line total", "Withdrawn"])
    for entry in data["lines"]:
        for bid in sorted(entry["history"], key=lambda b: b.created_at):
            writer.writerow([fmt_dt(bid.created_at, False), entry["label"], bid.vendor.name,
                             f"{bid.unit_price:.2f}", f"{bid.total:.2f}",
                             "yes" if bid.withdrawn else "no"])
    return buffer.getvalue().encode("utf-8-sig")


# ------------------------------------------------------------------ PDF
def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=17, spaceAfter=4,
                                textColor=colors.HexColor("#0f172a"), alignment=0),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontSize=9, textColor=GREY,
                              spaceAfter=10),
        "h2": ParagraphStyle("h", parent=base["Heading2"], fontSize=12, spaceBefore=12,
                             spaceAfter=6, textColor=ACCENT),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontSize=8, leading=10),
        "note": ParagraphStyle("n", parent=base["Normal"], fontSize=8, textColor=GREY),
    }


def _table(rows, widths, align_right=(), header=True):
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                  ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                  ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    for col in align_right:
        style.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
    table.setStyle(TableStyle(style))
    return table


def _document(buffer, landscape_mode=True):
    size = landscape(A4) if landscape_mode else A4
    return SimpleDocTemplate(buffer, pagesize=size, leftMargin=14 * mm, rightMargin=14 * mm,
                             topMargin=14 * mm, bottomMargin=14 * mm,
                             title=f"{config.APP_NAME} report")


def savings_pdf(data: dict) -> bytes:
    buffer = io.BytesIO()
    doc = _document(buffer)
    st = _styles()
    totals = data["totals"]
    story = [
        Paragraph("Total Savings Report", st["title"]),
        Paragraph(f"{config.APP_NAME} &nbsp;·&nbsp; "
                  f"{fmt_dt(data['start'], False)} to {fmt_dt(data['end'], False)} "
                  f"&nbsp;·&nbsp; generated {fmt_dt(datetime.utcnow())}", st["sub"]),
    ]
    headline = [[
        Paragraph("<b>Auctions awarded</b><br/>" + str(totals["count"]), st["cell"]),
        Paragraph("<b>Baseline value</b><br/>" + fmt_money(totals["baseline"]), st["cell"]),
        Paragraph("<b>Final value</b><br/>" + fmt_money(totals["final"]), st["cell"]),
        Paragraph("<b>Total savings</b><br/>" + fmt_money(totals["savings"]), st["cell"]),
        Paragraph("<b>Savings %</b><br/>" + f"{totals['savings_pct']:.1f}%", st["cell"]),
    ]]
    box = Table(headline, colWidths=[52 * mm] * 5)
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT),
                             ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                             ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                             ("TOPPADDING", (0, 0), (-1, -1), 8),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [box, Spacer(1, 10)]

    rows = [["Reference", "Auction", "Awarded on", "Bidders", "Bids", "Baseline",
             "Final", "Savings", "%", "Awarded to"]]
    for row in data["rows"]:
        rows.append([row["reference"], Paragraph(row["title"], _styles()["cell"]),
                     fmt_dt(row["date"], False), str(row["bidders"]), str(row["bids"]),
                     fmt_money(row["baseline"], False), fmt_money(row["final"], False),
                     fmt_money(row["savings"], False), f"{row['savings_pct']:.1f}",
                     Paragraph(row["awardees"], _styles()["cell"])])
    rows.append(["", "TOTAL", "", "", "", fmt_money(totals["baseline"], False),
                 fmt_money(totals["final"], False), fmt_money(totals["savings"], False),
                 f"{totals['savings_pct']:.1f}", ""])
    widths = [24 * mm, 55 * mm, 27 * mm, 15 * mm, 12 * mm, 24 * mm, 24 * mm, 24 * mm,
              12 * mm, 45 * mm]
    table = _table(rows, widths, align_right=(3, 4, 5, 6, 7, 8))
    table.setStyle(TableStyle([("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                               ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#dbeafe"))]))
    story += [table, Spacer(1, 8),
              Paragraph("Savings = (quantity × starting price) − final awarded value. "
                        f"All amounts in {config.CURRENCY}.", st["note"])]
    doc.build(story)
    return buffer.getvalue()


def auction_pdf(data: dict) -> bytes:
    buffer = io.BytesIO()
    doc = _document(buffer)
    st = _styles()
    auction, summary = data["auction"], data["summary"]
    story = [
        Paragraph(f"Auction Summary — {auction.reference}", st["title"]),
        Paragraph(f"{auction.title} &nbsp;·&nbsp; status {auction.status.value} "
                  f"&nbsp;·&nbsp; generated {fmt_dt(datetime.utcnow())}", st["sub"]),
    ]
    facts = [[
        Paragraph("<b>Ran</b><br/>" + f"{fmt_dt(auction.start_at, False)}<br/>to "
                  f"{fmt_dt(auction.end_at, False)}", st["cell"]),
        Paragraph("<b>Bidders</b><br/>" + f"{summary['active_bidders']} of "
                  f"{summary['participants']} invited", st["cell"]),
        Paragraph("<b>Bids</b><br/>" + str(summary["total_bids"]), st["cell"]),
        Paragraph("<b>Baseline</b><br/>" + fmt_money(summary["baseline"]), st["cell"]),
        Paragraph("<b>Final</b><br/>" + fmt_money(summary["final_value"]), st["cell"]),
        Paragraph("<b>Savings</b><br/>" + f"{fmt_money(summary['savings'])} "
                  f"({summary['savings_pct']:.1f}%)", st["cell"]),
    ]]
    box = Table(facts, colWidths=[43 * mm] * 6)
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT),
                             ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                             ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                             ("TOPPADDING", (0, 0), (-1, -1), 7),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    story += [box, Paragraph("Line by line", st["h2"])]

    rows = [["Item", "Qty", "Start price", "Highest bid", "Lowest bid", "Savings",
             "Awarded to", "Qty", "Price"]]
    for entry in data["lines"]:
        line, awards = entry["line"], entry["awards"]
        rows.append([
            Paragraph(entry["label"], st["cell"]), fmt_qty(line.qty),
            fmt_money(line.starting_price, False) if line.has_ceiling else "—",
            fmt_money(entry["highest"].unit_price, False) if entry["highest"] else "—",
            fmt_money(entry["lowest"].unit_price, False) if entry["lowest"] else "—",
            fmt_money(entry["result"]["savings"], False),
            Paragraph("<br/>".join(a.vendor.name for a in awards) or "—", st["cell"]),
            "<br/>".join(fmt_qty(a.qty) for a in awards) or "—",
            "<br/>".join(fmt_money(a.unit_price, False) for a in awards) or "—",
        ])
    story += [_table(rows, [52 * mm, 16 * mm, 24 * mm, 24 * mm, 24 * mm, 24 * mm,
                            45 * mm, 16 * mm, 24 * mm], align_right=(1, 2, 3, 4, 5)),
              Paragraph("Every bid placed", st["h2"])]

    bid_rows = [["Time", "Item", "Bidder", "Unit price", "Line total", "Status"]]
    all_bids = [(b, entry["label"]) for entry in data["lines"] for b in entry["history"]]
    for bid, label in sorted(all_bids, key=lambda pair: pair[0].created_at):
        bid_rows.append([fmt_dt(bid.created_at, False), Paragraph(label, st["cell"]),
                         Paragraph(bid.vendor.name, st["cell"]),
                         fmt_money(bid.unit_price, False), fmt_money(bid.total, False),
                         "Withdrawn" if bid.withdrawn else "Live"])
    if len(bid_rows) == 1:
        bid_rows.append(["—", "No bids were placed", "", "", "", ""])
    story += [_table(bid_rows, [34 * mm, 60 * mm, 55 * mm, 28 * mm, 30 * mm, 22 * mm],
                     align_right=(3, 4)), Spacer(1, 8),
              Paragraph(f"Reverse auction: the lowest bid wins. All amounts in "
                        f"{config.CURRENCY}.", st["note"])]
    doc.build(story)
    return buffer.getvalue()
