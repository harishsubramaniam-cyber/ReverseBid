# ReverseBid — a reverse auction platform

A complete, self-contained reverse auction application: buyers publish a **ceiling** price,
invited suppliers compete by bidding **downwards**, and the lowest price wins. It covers the
whole cycle — onboarding, masters, auction creation, the live bidding engine, awarding,
reporting, and an email on every key event.

Built with FastAPI + SQLAlchemy + SQLite and server-rendered HTML. No build step, no
JavaScript framework, one command to run.

```bash
pip install -r requirements.txt
python seed.py                      # optional demo data
uvicorn app.main:app --reload
# open http://localhost:8000
```

On Windows, double-click `windows-setup.bat` once and `windows-start.bat` thereafter.
**New to this?** `docs\1 - Running ReverseBid on Windows.docx` walks through it from installing
Python onwards, and `docs\2 - Putting ReverseBid on GitHub.docx` covers getting the code online.

Demo sign-ins (after `python seed.py`), password `demo1234`:

| Role     | Email               | Sees                                             |
| -------- | ------------------- | ------------------------------------------------ |
| Buyer    | `buyer@demo.in`     | Dashboard, auctions, masters, reports, outbox     |
| Bidder   | `vendor1@demo.in` … `vendor4@demo.in` | Their invitations and the bidding screen |

---

## What is built

**1 — Easy to adopt**

* Guided onboarding that explains the whole model in four steps.
* A **? Help** drawer on every screen and an **Ask** assistant, both in plain language.
  Every form field carries a one-line hint.
* Vendor, item and unit masters where only the obvious fields are mandatory — a vendor needs a
  name and an email, an item needs a name, a unit needs a code.
* Odoo-style inline create: add a vendor, item or unit from inside the auction form without
  losing your place.

**2 — The auction engine**

* Creation and scheduling with editable start/end times until bidding opens.
* Starting price as a **ceiling** — no bid may sit above it — and it is **optional**: leave it
  empty and bidders open at any price, with savings measured from the highest bid received.
* Live rank (L1, L2, L3…) and lowest-bid visibility, each switchable per auction.
* **Minimum decrement** (how much lower each bid must be) and **maximum decrement**
  (the biggest drop allowed in one step), as a fixed amount or a percentage.
* Hidden bidder names — bidders see each other as “Bidder A”, “Bidder B”; the buyer always
  sees the real names.
* **Auto-extension**: a bid inside the closing window pushes the finish line back, with a
  configurable trigger, extension length and maximum number of extensions.

**3 — Award**

* **One bidder per item, for the whole quantity.** Different items can go to different bidders,
  or the whole auction to one — one click fills every line with the same supplier — but a single
  item is never carved up between two suppliers.
* Every line is pre-set to its L1; change the winner, change the price, or leave a line
  unawarded.

**4 — Reports and dashboard**

* Savings-first dashboard: total savings, baseline, awarded value, savings by month, closing soon.
* **Report 1 — Total Savings** across every auction in a date range.
* **Report 2 — Individual Auction Summary**: every bid, the highest and lowest price, line-level
  savings and the awardee.
* Both download as **PDF** and **CSV**.

**5 — Reach everyone, anywhere**

* Email plus in-app notification on every key event: invitation, opening, bid received, outbid,
  extension, closing soon, closed, awarded, not awarded, cancelled, messages, approvals.
* You choose the recipients: several contacts per vendor, a per-auction override for one bidder,
  and a copy list for your own team. See **Who gets the emails** below.
* Outbid alerts that pull vendors back into the auction.
* Fully responsive — buyers and bidders can work from a phone browser, with a bottom nav bar.

**6 — Generic platform features**

* Private conversations between each bidder and the auction creator.
* An append-only audit trail on every action, visible on each auction.
* Publish goes straight to the bidders — no approval step. A future auction publishes as
  *scheduled* and can be opened early with **Start bidding now**; if the opening time has already
  passed, publishing opens it immediately.
* Withdraw a bid, edit an auction before it opens, cancel with a reason at any time.
* **Every failure is explained on the screen it happened on**, in plain words, with what you
  typed still in the boxes — never a raw error page or a wall of JSON.

---

## Who gets the emails

You control the recipients in three places, and the app always shows you exactly who will be
written to before anything goes out.

1. **On the vendor** — a vendor has a main address plus as many extra contacts as you like
   (their sales desk, a second contact, a shared inbox). Every one of them receives the
   invitation, the outbid alerts, the closing reminder and the award decision. Edit the list
   from **Vendors & items → Who gets the emails**, or when you first add the vendor — including
   from the inline “+ New vendor” box inside the auction form.
2. **On the auction, per bidder** — when you tick a vendor on the auction form a box appears
   underneath it. Type an address there and it replaces that vendor's usual list *for this
   auction only*, which is what you want when a different person handles one particular tender.
   Leave it blank and the vendor's own list is used.
3. **Your own copy list** — the “Copy my own team on this auction” box sends your colleagues
   (procurement head, finance) a copy when the auction is published, closed, awarded or
   cancelled. They need no login and never see the bidding screen.

Addresses can be separated by commas, semicolons or new lines, and `Name <a@b.com>` is
understood. Anything that is not a plausible address is refused with a plain-language message
rather than silently dropped. The **Details** tab of every auction lists the exact addresses each
bidder will be written to, and the **Outbox** shows what was actually produced.

Anyone with a login also gets the in-app alert, even when their address has been overridden for
that auction.

## Email

Email is real SMTP, with a safety net.

* Set `RA_SMTP_HOST` (and friends) and messages are genuinely sent.
* Leave it unset and every message is written to `data/outbox/*.eml` **and** to the in-app
  **Outbox** page, so you can see exactly what a vendor would receive without sending anything.

Either way every message is logged in the `email_messages` table with its status. Sending happens
on a background thread, so a slow mail server never blocks a bid.

Gmail example (`.env`):

```
RA_SMTP_HOST=smtp.gmail.com
RA_SMTP_PORT=587
RA_SMTP_USER=you@gmail.com
RA_SMTP_PASSWORD=your-16-character-app-password
RA_MAIL_FROM=you@gmail.com
RA_BASE_URL=https://auctions.example.com
```

Copy `.env.example` to `.env` in the project root and restart — the app reads it on startup.
Real environment variables always win over the file, so a server configured through its own
environment (systemd, Docker's `--env-file`, a platform's settings panel) is unaffected.

---

## Configuration

All settings are environment variables — see `app/config.py`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `RA_DATABASE_URL` | `sqlite:///data/reverse_auction.db` | Any SQLAlchemy URL; PostgreSQL works unchanged |
| `RA_SECRET_KEY` | `dev-secret-change-me` | **Change this in production** — it signs session cookies |
| `RA_BASE_URL` | `http://localhost:8000` | Used for the links inside emails |
| `RA_TIMEZONE` | `Asia/Kolkata` | All times are stored in UTC and displayed here |
| `RA_CURRENCY` / `RA_CURRENCY_SYMBOL` | `INR` / `₹` | Display only |
| `RA_SMTP_*`, `RA_MAIL_FROM` | empty | See above |
| `RA_SCHEDULER_INTERVAL` | `5` | Seconds between clock ticks |
| `RA_MAIL_FROM_NAME` | `ReverseBid` | The name your emails appear to come from |
| `RA_ENDING_SOON_MINUTES` | `5` | When the “closing soon” alert goes out |

---

## How it is put together

```
app/
  main.py          FastAPI app, routing, error pages
  models.py        the whole domain model
  engine.py        bid validation, ranking, auto-extension, savings maths
  scheduler.py     background clock: opens and closes auctions, time-based alerts
  mailer.py        SMTP with a dev-outbox fallback, background delivery
  notify.py        one function per event: in-app notification + email
  reporting.py     both reports, as HTML data, CSV and PDF
  help_content.py  every help string and the assistant's answers
  emails_util.py   parsing and validating the address lists people type
  migrate.py       adds any new columns to an existing database on startup
  security.py      password hashing, sessions, role guards
  audit.py         the append-only trail
  errors.py        the two user-facing failure types, so routers can explain themselves
  routers/         one module per area of the app
  templates/       Jinja2 pages + the email template
  static/          one stylesheet, one small script
seed.py            demo data
docs/              the two step-by-step guides, as Word documents and markdown
tests/             end-to-end walk through a full auction
windows-*.bat      double-clickable setup and start, for Windows
```

Two design notes worth knowing:

* **The engine is pure.** `engine.py` never touches HTTP; the routers translate its
  `BidError` messages straight to the screen, which is why bidders get sentences like
  *“Too high. The current lowest bid is ₹35.23 and you must go at least ₹0.50 below it.”*
* **New columns migrate themselves.** `migrate.py` runs on startup and adds any column the
  model has and the database does not, so upgrading an existing installation is just a restart.
* **The assistant is offline.** `help_content.answer()` is a keyword matcher with no
  dependencies. Replace that one function with an LLM call and nothing else changes.

---

## Tests

```bash
python tests/test_end_to_end.py      # or: python -m pytest -q
```

It builds a throwaway database and walks a whole auction: masters, inline create, publishing,
the scheduler opening the auction, every bidding rule (ceiling, minimum and maximum decrement,
not raising your own bid), visibility rules, withdrawal, messages, auto-extension, closing,
a split award, over-award refusal, savings maths, all four report downloads, the audit trail,
the email outbox, and the three ways of choosing recipients — around seventy assertions.

---

## Deploying

```bash
docker build -t reversebid .
docker run -p 8000:8000 --env-file .env -v $(pwd)/data:/app/data reversebid
```

Or anywhere that runs a Python web process:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For more than one worker process, move the database to PostgreSQL
(`RA_DATABASE_URL=postgresql+psycopg://…`) and run the scheduler in a single process, since each
worker would otherwise run its own clock.

Before going live: set `RA_SECRET_KEY`, set `RA_BASE_URL`, configure SMTP, and serve over HTTPS.

## Licence

MIT — see `LICENSE`.
