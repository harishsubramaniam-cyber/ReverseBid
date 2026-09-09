"""Plain-language help.

Three things live here:

* ``FIELD_HELP``  - one short sentence per form field, shown as a hint under it.
* ``PAGE_HELP``   - what this screen is for, in the "Help" panel.
* ``ANSWERS``     - the in-product assistant's knowledge, matched on keywords.

Rule for every string in this file: no jargon, no acronym without an
explanation, and never more than two sentences.
"""
from __future__ import annotations

# --------------------------------------------------------------------- fields
FIELD_HELP = {
    "title": "A short name people will recognise, like “Corrugated boxes — Q3”.",
    "description": "What you are buying and anything bidders should know before they price it.",
    "terms": "Payment terms, delivery, warranty — anything the price depends on.",
    "start_at": "When bidding opens. Bidders get an email at this moment.",
    "end_at": "When bidding closes. You can change this any time before it starts.",
    "decrement_type": "Should the minimum drop be a fixed amount of money, or a percentage of the current price?",
    "min_decrement": "How much lower each new bid must be. This stops bidders shaving off one rupee at a time.",
    "max_decrement": "The biggest drop allowed in one bid. Leave it at 0 if you don't want a limit.",
    "show_rank": "Let bidders see their position — L1 is the lowest price, L2 is second lowest, and so on.",
    "show_lowest_bid": "Let bidders see the current lowest price. It usually pushes prices down faster.",
    "hide_bidder_names": "Bidders see each other as “Bidder A”, “Bidder B” instead of by name. You always see the real names.",
    "auto_extend": "If a bid lands in the last few seconds, add more time. It stops someone winning by bidding at the buzzer.",
    "extend_trigger_seconds": "A bid inside this window triggers extra time.",
    "extend_by_seconds": "How much extra time each extension adds.",
    "max_extensions": "How many times the clock can be pushed back before it must end.",
    "requires_approval": "Send the auction to an approver before it can go live.",
    "starting_price": "The most you are willing to pay per unit. Bids have to come in at or below this.",
    "qty": "How many units you are buying. Price × quantity gives the line total.",
    "vendors": "The bidders you are inviting. Only these companies can see and bid on the auction.",
    "vendor_name": "The company name. This is all we really need.",
    "vendor_email": "The main address for this vendor. Invitations and alerts go here.",
    "vendor_extra_emails": "Anyone else at this vendor who should get the same emails — their "
                           "sales desk, a second contact, a shared inbox. One per line, or "
                           "separated by commas.",
    "notify_emails": "Sending this particular auction to someone else at this vendor? Type their "
                     "address here. Leave it blank and we use the vendor's usual list.",
    "cc_emails": "Your own colleagues who should get a copy when this auction is published, "
                 "closed, awarded or cancelled. They do not need a login.",
    "item_name": "What you are buying, in everyday words.",
    "unit_code": "A short unit like KG, NOS, MTR, LTR.",
    "bid_price": "Your price per unit. Lower wins — you can bid again as many times as you like.",
    "award_price": "The price you are awarding at. It defaults to the bidder's winning price.",
}

# --------------------------------------------------------------------- pages
PAGE_HELP = {
    "dashboard": {
        "title": "Your dashboard",
        "intro": "Everything you have run, with savings first. Savings = what you expected to "
                 "pay (the starting price) minus what you actually paid.",
        "tips": [
            ("What does “baseline” mean?",
             "It is quantity × starting price, added up. Think of it as your budget before bidding."),
            ("Why is savings sometimes an estimate?",
             "Until you award an auction we use the lowest bid. Once you award, we use the real awarded prices."),
        ],
    },
    "auction_new": {
        "title": "Creating an auction",
        "intro": "Fill in the four blocks top to bottom. If a vendor, item or unit is missing, "
                 "add it right here — you don't have to leave this page.",
        "tips": [
            ("What is a reverse auction?",
             "Normal auctions go up; this one goes down. You set the highest price you will pay "
             "and your suppliers compete by lowering theirs. The lowest price wins."),
            ("What should I put as the starting price?",
             "The most you would pay per unit today — often last year's price. Set it too low and "
             "nobody can bid."),
            ("An item I buy is not in the list.",
             "Press **Create a new item**. It is saved to your item list and added to this "
             "auction as a new row, so you can create several one after another. Units work "
             "the same way."),
            ("What is the difference between Save as draft and Publish?",
             "Save as draft keeps the auction private — nobody is told about it, and you can "
             "come back and change anything. Publish emails every invited bidder, and opens "
             "bidding immediately if the opening time has already passed."),
            ("Do I have to invite vendors?",
             "Yes. Only invited vendors can see the auction, and each gets an email when you publish."),
            ("Can I choose exactly who gets the emails?",
             "Yes, in three places. Each vendor can have several addresses on their record; you "
             "can type a different address against a vendor for this auction only; and you can "
             "copy your own colleagues on the box at the bottom of the form."),
        ],
    },
    "auction_detail_buyer": {
        "title": "Running the auction",
        "intro": "Watch bids arrive line by line. The green row is the current lowest price.",
        "tips": [
            ("Can I publish it straight away?",
             "Yes. Press Publish and the invitations go out immediately. If the opening time has "
             "already passed, bidding starts there and then; otherwise it opens at the time you "
             "set, and you can still press “Start bidding now” to bring it forward."),
            ("Can I still change things?",
             "Before it starts, yes — dates, prices, invited vendors. Once bidding is live, only "
             "cancelling is possible, and everyone is told why."),
            ("What is auto-extension?",
             "If a bid arrives in the final moments, the clock is pushed back so others can respond. "
             "It stops last-second sniping."),
            ("How do I award?",
             "When bidding closes, open the Award screen. Each item is pre-set to its lowest "
             "bidder; change any of them, or send the whole auction to one supplier. An item "
             "always goes to a single bidder, for the full quantity."),
        ],
    },
    "auction_detail_vendor": {
        "title": "How to bid",
        "intro": "Enter your price per unit and press Place bid. You can bid as many times as you "
                 "like, as long as each bid is lower than the last.",
        "tips": [
            ("Why was my bid rejected?",
             "Either it was above the starting price, or it wasn't far enough below the current "
             "lowest bid. The box under each item tells you the exact price range you can use."),
            ("What does L1 mean?",
             "L1 is the lowest price right now — the winning position. L2 is second lowest."),
            ("I was L1 and now I'm not.",
             "Someone has gone lower. We email you the moment that happens, so you can bid again."),
        ],
    },
    "reports": {
        "title": "Reports",
        "intro": "Two reports. Total Savings covers a date range across every auction. "
                 "Individual Auction Summary drills into one auction, bid by bid.",
        "tips": [
            ("Which format should I download?",
             "PDF to send to someone. CSV to open in Excel and cut the numbers yourself."),
        ],
    },
    "masters": {
        "title": "Vendors, items and units",
        "intro": "The lists you pick from when creating an auction. Only the obvious fields are "
                 "mandatory, and you can always add more later.",
        "tips": [
            ("Do I need to fill everything in?",
             "No. A vendor needs a name and an email. An item needs a name. A unit needs a code. "
             "That's it."),
            ("Can one vendor have several email addresses?",
             "Yes. Add as many as you like under the vendor — every one of them receives the "
             "invitation, the outbid alerts and the award decision. Use the “Who gets the "
             "emails” link on any vendor to change the list later."),
        ],
    },
    "outbox": {
        "title": "Email outbox",
        "intro": "Every email the platform has produced, newest first. If no mail server is "
                 "configured, messages are written here instead of being sent — so you can see "
                 "exactly what your vendors would receive.",
        "tips": [
            ("How do I switch on real emails?",
             "Put your mail server details in the environment file and restart. Nothing else changes."),
        ],
    },
}

# --------------------------------------------------------------------- assistant
ANSWERS = [
    (["reverse auction", "what is", "how does it work", "explain"],
     "A reverse auction turns a normal auction around. You publish the most you are willing to "
     "pay, invited suppliers bid against each other, and each new bid has to be **lower** than "
     "the last. The lowest price at the close is the winner."),
    (["create", "new auction", "start an auction", "set up"],
     "Go to **Auctions → New auction**. You need four things: a title, the items with quantities "
     "and a starting price, the vendors you want to invite, and a start and end time. Anything "
     "missing from your lists — a vendor, an item, a unit — can be added right there on the form."),
    (["starting price", "ceiling", "highest"],
     "The starting price is your ceiling: the most you will pay per unit. No bid can be above it, "
     "and the first bid can sit exactly on it."),
    (["decrement", "minimum", "step", "how much lower"],
     "The minimum decrement is how much lower each bid has to be than the current best. "
     "The maximum decrement caps how far a bidder can drop in one go, so nobody crashes the "
     "price by accident. Set it as a fixed amount or a percentage."),
    (["rank", "l1", "l2", "position"],
     "L1 means the lowest price right now — the winning spot. L2 is second lowest, and so on. "
     "You choose whether bidders can see their rank when you create the auction."),
    (["outbid", "alert", "beaten"],
     "The moment someone goes below you, we send an email and an in-app alert with the new "
     "lowest price, so you can decide whether to bid again."),
    (["auto extend", "extension", "last second", "sniping"],
     "If a bid arrives in the final moments, the clock is automatically pushed back by a few "
     "minutes so the other bidders can respond. You set the trigger window, the extra time, and "
     "how many times it can happen."),
    (["hidden", "anonymous", "names"],
     "With hidden names switched on, bidders see each other as “Bidder A”, “Bidder B” and so on. "
     "As the buyer you always see the real company names."),
    (["award", "winner", "give the business", "split"],
     "Once bidding closes, open the **Award** screen. Every item is pre-set to its lowest bidder. "
     "You can change who wins any item, change the price, or leave an item unawarded — and one "
     "click gives the whole auction to a single supplier. Each item goes to one bidder only, for "
     "the full quantity. Everyone is emailed the outcome, winners and non-winners alike."),
    (["savings", "report", "download", "pdf", "csv", "excel"],
     "**Reports → Total Savings** covers every auction in a date range. **Individual Auction "
     "Summary** shows one auction with every bid, the highest and lowest price, the savings and "
     "the awardee. Both download as PDF or CSV."),
    (["email", "notification", "smtp", "outbox", "not receiving"],
     "Every key event sends an email: invitation, auction opened, outbid, extended, closing soon, "
     "closed, awarded, and messages. If no mail server is set up, they land in the **Outbox** page "
     "so you can still see them. Add your SMTP details to the environment file to send for real."),
    (["approval", "approve", "reject", "rework"],
     "Tick “needs approval” when creating an auction and it goes to an approver first. They can "
     "approve it, reject it, or send it back for rework with a comment. You are emailed the decision."),
    (["message", "chat", "conversation", "ask the buyer"],
     "Each bidder has a private thread with the buyer on the auction page. Bidders never see each "
     "other's messages, and both sides get an email for every new one."),
    (["withdraw", "cancel", "edit", "mistake", "wrong price"],
     "A bidder can withdraw a bid while the auction is live — ranks recalculate immediately and "
     "the buyer is told. A buyer can edit an auction before it starts and cancel it at any time "
     "with a reason, which is emailed to everyone."),
    (["audit", "history", "who did what", "trail"],
     "Every action is written to an audit trail you cannot edit: who did it, what changed and "
     "exactly when. Open the **History** tab on any auction."),
    (["email address", "recipient", "who gets", "send emails to", "cc", "copy", "second contact",
      "another email", "multiple email"],
     "You control the recipients in three places. **On the vendor** — add as many addresses as "
     "you like and all of them receive every email. **On the auction** — type an address against "
     "a bidder when you invite them, and that overrides their usual list for that auction only. "
     "**Copy list** — at the bottom of the auction form, add your own colleagues to be copied "
     "when the auction is published, closed, awarded or cancelled. They need no login."),
    (["vendor", "supplier", "add", "master", "item", "unit"],
     "Vendors need only a name and an email; items need a name; units need a short code like KG. "
     "You can add all three without leaving the auction form — look for the **+ New** link next "
     "to each picker."),
    (["mobile", "phone", "app"],
     "The whole platform works in a phone browser — bidding, alerts, messages and reports. There "
     "is nothing to install."),
]

FALLBACK = (
    "I can help with creating auctions, bidding rules, decrements, ranks, awarding, reports, "
    "emails and approvals. Try asking something like *“how do decrements work?”* or "
    "*“how do I split an award?”*"
)


def answer(question: str) -> str:
    """Very small keyword matcher - deliberately dependency-free and offline.

    Swap this function for a call to an LLM and the rest of the app is unchanged.
    """
    text = (question or "").lower().strip()
    if not text:
        return FALLBACK
    best, best_score = None, 0
    for keywords, response in ANSWERS:
        score = sum(len(k) for k in keywords if k in text)
        if score > best_score:
            best, best_score = response, score
    return best or FALLBACK
