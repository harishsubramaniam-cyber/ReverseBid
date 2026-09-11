# Putting ReverseBid online for people to try

Right now the app only runs on your own PC, which means only you can open it. This
puts it on the internet at an address like `https://reversebid-demo.onrender.com`
that you can paste into an email and anyone can click.

It is **free**, it takes about ten minutes, and you never touch a command line.

**What visitors will find.** The site comes up already filled with a demo company —
a live auction with bids arriving, four suppliers, finished auctions with savings
on the dashboard — which they can sign into with a published password.

They can also press **Set up my organisation** and start their own, empty and
entirely separate: their own suppliers, items and auctions, which nobody else on
the site can see. That is the more convincing demonstration, because it is exactly
what a real buyer would do on day one.

Nothing they do can reach a real supplier: this deployment has no mail server
attached, so every email is kept inside the app's own Outbox page for them to read
instead of being sent.

> **This is a showcase, not a live tender.** Whatever visitors type is wiped
> whenever the service restarts, and the demo company is rebuilt clean. That is
> on purpose. If you later want to run a real auction with real suppliers, see
> **Running it for real** at the end — it is the same app, set up differently.

---

## Before you start

You need your code on GitHub — which you have already done — and an email
address to make one free account with.

---

## Step 1 — Make a Render account (2 minutes)

1. Go to **https://render.com**
2. Click **Get Started** and choose **GitHub** to sign up. Signing in with GitHub
   is what lets Render see your repository.
3. Approve the permission screen GitHub shows you. You can give Render access to
   only the `ReverseBid` repository if you prefer — that is enough.

No card is needed for what follows.

---

## Step 2 — Point Render at your repository (3 minutes)

1. In Render, click **New +** (top right) → **Blueprint**.
2. Pick your **ReverseBid** repository from the list. If it is not listed, click
   **Configure account** and give Render access to it.
3. Render finds the file called `render.yaml` in your repository and reads it.
   That file already says everything: which Python to use, how to start the app,
   and every setting. You should see a service named **reversebid-demo**.
4. Give the blueprint any name you like, and click **Apply** (or **Create
   Services**).

That is the whole configuration. There is nothing to fill in.

---

## Step 3 — Wait for the first build (3–5 minutes)

Render now downloads your code, installs what the app needs, and starts it. You
will see a log scrolling past. It is finished when you see:

```
Demo data created. Sign in as buyer@demo.in / demo1234.
==> Your service is live 🎉
```

The address is at the top of the page, something like
**`https://reversebid-demo.onrender.com`**. Click it.

> **The first page may take up to a minute to appear.** That is normal and is
> explained in Step 5.

---

## Step 4 — Check it, then share it

Open the address and sign in:

| Who | Email | Password |
| --- | --- | --- |
| **The buyer** — runs the auctions | `buyer@demo.in` | `demo1234` |
| A bidder | `vendor1@demo.in` | `demo1234` |
| Another bidder | `vendor2@demo.in` | `demo1234` |

Worth doing once yourself before you send the link round:

1. Sign in as the buyer. The dashboard shows savings from the finished auctions.
2. Open **Auctions** → the live one. Bids are already in, ranked cheapest first.
3. Open **Outbox**. Every email the demo produced is there — click one and read
   it exactly as a supplier would receive it.
4. Now open a **private / incognito** window (Ctrl+Shift+N), go to the same
   address, and sign in as `vendor1@demo.in`. Place a bid. Switch back to the
   buyer's window and watch it appear.

That last trick is the one to tell people about — seeing both sides at once is
what makes a reverse auction click.

**A note to send with the link:**

> Here is the reverse auction platform: `https://…onrender.com`
> Sign in as **buyer@demo.in / demo1234** to run auctions, or
> **vendor1@demo.in / demo1234** to bid. Open the two in separate browser
> windows to watch both sides at once. It is a demonstration — no email
> actually leaves the site, and everything resets periodically.

---

## Step 5 — Two things to expect on the free plan

**It falls asleep.** After about 15 minutes with nobody on it, Render stops the
service. The next visitor's first page takes 30–60 seconds while it wakes up —
after that it is quick again. If you are demonstrating live, open the site a
minute beforehand so it is already awake.

**It forgets.** When it sleeps, restarts, or you push new code, anything visitors
created is wiped and the demo company is rebuilt from scratch. For a try-out site
this is what you want: it is never left in a mess. Just do not use it for
anything you need to keep.

Both go away on Render's paid plan — see below.

---

## Making the emails really go

Out of the box the site is in **practice mode**: it writes every email into its
own **Outbox** page instead of sending it, which is why those rows say
**Saved here**. Nothing is broken — no mail server has been attached yet, on
purpose, so visitors clicking around cannot email real suppliers.

To switch real sending on, you do **not** edit any file — on Render the settings
are typed into a form. There are two ways, and **on the free plan only the first
one works.**

### The way that works on the free plan: an email service

Render's free plan blocks outbound traffic to the ports mail servers listen on
(25, 465 and 587), so a Gmail password simply cannot get out — you get
*"Network is unreachable"*, and no setting will fix it. The way round it is to
hand the message to an email service over ordinary web traffic instead, which
nothing blocks.

1. Make a free account at **brevo.com** (300 emails a day, no card). Under
   **Senders**, add your own email address and click the link they send you to
   confirm it. Under **SMTP & API** → **API keys**, create a key and copy it.
2. In Render, open your service → **Environment** → **Add environment
   variable**, twice:

   | Name | Value |
   | --- | --- |
   | `RA_MAIL_API_KEY` | the key you copied from Brevo |
   | `RA_MAIL_FROM` | the address you confirmed at Brevo |

3. **Save changes**, wait for the restart, then press **Send test email** on the
   Outbox page.

That is the whole thing. The app works out from the key itself which service it
is talking to. (A key from **Resend** works the same way — paste it in and
nothing else changes.)

### The other way: your own mail server

This needs a **paid** Render instance, or your own PC, where the mail ports are
open.

1. Open your service in Render → **Environment** (left-hand menu) → **Add
   environment variable**, five times:

   | Name | Value |
   | --- | --- |
   | `RA_SMTP_HOST` | `smtp.gmail.com` |
   | `RA_SMTP_PORT` | `587` |
   | `RA_SMTP_USER` | your full Gmail address |
   | `RA_SMTP_PASSWORD` | the 16-character **app password** (see below) |
   | `RA_MAIL_FROM` | your full Gmail address again |

2. Click **Save changes**. Render restarts the service on its own — give it a
   minute.
3. Open the **Outbox** page. The blue "practice mode" box should be gone,
   replaced by a green **Sending is switched on**. Type your own address into
   **Send test email** and press it. It tells you in one line what the mail
   server said — either "Sent to…" or the exact reason it refused.

**The app password.** Gmail will not accept your normal password from a program.
Go to **myaccount.google.com** → Security, turn on **2-Step Verification** if it
is not already on, then search that page for **App passwords**. Create one
called `ReverseBid`. Google shows you 16 letters — that is the value for
`RA_SMTP_PASSWORD`. Type it without the spaces. You only see it once; if you
lose it, delete it and make another.

If your office uses Microsoft 365 instead, the host is `smtp.office365.com`,
port `587`, and it is your normal work address and password — though many
organisations block this, and the test email will say so plainly if yours does.

> For a site you are only showing people, leaving practice mode on is the
> better choice: everything still works, every email is readable in the Outbox,
> and no stranger clicking around can send mail from your Gmail account.

---

## Updating it later

Push to GitHub and Render redeploys by itself, within a couple of minutes. From
your PC that is the usual three commands:

```
git add .
git commit -m "what changed"
git push
```

Watch progress under **Logs** in Render if you want to see it happen.

---

## If something goes wrong

| What you see | What to do |
| --- | --- |
| **Render cannot find the repository** | Click **Configure account** on the repository picker and give Render access to it. |
| **"No render.yaml found"** | The file must be in the top folder of the repository, next to `README.md`. Check on GitHub that it is not inside another folder. |
| **The build fails** | Open **Logs** and read the last few red lines. Nine times out of ten it is a file that did not get pushed — check the repository on GitHub has `requirements.txt`, `app/` and `seed.py` in it. |
| **The page says "Application failed to respond"** | Give it a minute; it is probably waking up. If it persists, open **Logs** — the error is printed there in plain text. |
| **Signing in does nothing** | You are on an old, cached page. Reload with Ctrl+Shift+R. |
| **Emails say "Network is unreachable"** | Render's free plan blocks the mail ports. Nothing about your password is wrong. Use the email-service route above — `RA_MAIL_API_KEY` — or move to a paid instance. |
| **Emails say the sender is not valid** | The address in `RA_MAIL_FROM` has not been confirmed at Brevo. Add it under **Senders** and click the link they email you. |

---

## Running it for real

The same code runs a real tender; three settings change, because for real
auctions you want exactly the opposite of a demo. In Render, open the service →
**Settings** and **Environment**:

1. **Keep the data.** Change the plan from Free to **Starter**, then add a
   **Disk**: mount path `/var/data`, size 1 GB. Then set two environment
   variables so the app writes there instead of onto temporary space:
   `RA_DATA_DIR=/var/data` and
   `RA_DATABASE_URL=sqlite:////var/data/reverse_auction.db`.
   Without a disk a live auction would vanish at the next restart.
2. **Turn the demo off.** Set `RA_DEMO_SEED=0`, and wipe the demo company before
   anyone real uses the site — its accounts have a published password. Add the
   disk first, delete the database file from Render's shell, restart, then press
   **Set up my organisation** to create your real one. Anyone else who finds the
   address can set up an organisation of their own, and they will see nothing of
   yours; if you would rather nobody could, put the site behind a password at the
   Render level or run it on an internal address.
3. **Switch email on.** Add `RA_SMTP_HOST`, `RA_SMTP_PORT`, `RA_SMTP_USER`,
   `RA_SMTP_PASSWORD` and `RA_MAIL_FROM` — the same five values as in the
   Windows guide. Then open the **Outbox** page and press **Send test email**,
   which tells you in one line whether the mail server accepted it.

Leave `RA_SECRET_KEY` and `RA_TRUSTED_PROXIES` exactly as the blueprint set
them. And take a copy of the disk now and then: Render's **Backups** tab does it
on the paid plans.
