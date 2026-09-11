# Running ReverseBid on your Windows PC

Written for someone who has never run a program from a folder before. Nothing here can break
your computer, and everything happens on your own machine — no website, no account, no
internet needed once it is set up.

You do this **once**: Steps 1–3. After that, starting the app is a double-click.

---

## Step 1 — Install Python (once, about 3 minutes)

Python is the language the app is written in. Your PC almost certainly does not have it yet.

1. Go to **https://www.python.org/downloads/windows/**
2. Click the big yellow **Download Python 3.x** button.
3. Open the file that downloads.
4. **This is the important bit.** On the very first screen, tick the box at the bottom that says
   **“Add python.exe to PATH”**. It is easy to miss and everything else depends on it.
5. Click **Install Now**, then **Close** when it finishes.

> **How do I know it worked?** Press the **Windows key**, type `cmd`, press Enter. A black window
> opens. Type `python --version` and press Enter. You should see something like
> `Python 3.12.4`. If you instead see “not recognized”, or the Microsoft Store opens, see
> [If something goes wrong](#if-something-goes-wrong) at the bottom.

---

## Step 2 — Unzip the app

1. Find `reversebid.zip` (probably in your **Downloads** folder).
2. **Right-click** it → **Extract All…**
3. A box appears asking where to put it. Delete what is in it and type exactly `C:\` — just those
   three characters.
4. Click **Extract**.
5. Open **This PC → Local Disk (C:) → ReverseBid**.

That folder is the app. You should see, directly inside it:

```
C:\ReverseBid\
    START HERE.txt
    windows-setup.bat      <- you double-click this once, in Step 3
    windows-start.bat      <- you double-click this every time after
    README.md
    app\   docs\   tests\
```

> **Cannot see those files?** You are almost certainly looking at the wrong folder. The right one
> has `README.md` sitting in it. If you see a single folder and nothing else, open that folder.

---

## Step 3 — Run the setup (once)

Double-click **`windows-setup.bat`**.

A black window opens and text scrolls past for a minute or two. It is downloading the pieces the
app needs and creating some demo data to play with. When it finishes it says:

```
Setup finished. Now double-click windows-start.bat to run the app.
```

Press any key to close it.

> **Windows may show a blue “Windows protected your PC” box.** That is because the file came
> from the internet. Click **More info** → **Run anyway**. You can also avoid it entirely:
> right-click the `.bat` file → **Properties** → tick **Unblock** at the bottom → **OK**.

---

## Step 4 — Start the app

Double-click **`windows-start.bat`**.

Two things happen:

* A black window opens and stays open. **Leave it alone** — that window *is* the app running.
  Closing it stops the app.
* Your browser opens at **http://localhost:8000**.

If the browser does not open by itself, open it and type `localhost:8000` in the address bar.

> Windows may ask about the **firewall** the first time. You can safely click **Cancel** — the app
> only needs to talk to your own browser, not the network.

---

## Step 5 — Sign in and have a look around

The setup created a small demo company with real-looking auctions. Use any of these:

| Who you are | Email | Password |
| --- | --- | --- |
| **The buyer** (runs auctions) | `buyer@demo.in` | `demo1234` |
| A bidder | `vendor1@demo.in` | `demo1234` |
| Another bidder | `vendor2@demo.in` | `demo1234` |

**A five-minute tour, as the buyer:**

1. The **Dashboard** shows total savings across the demo auctions.
2. **Auctions** → open *“Corrugated boxes and stretch film — live demo”*. It is running right now,
   with a live countdown. Watch the bids come in, ranked cheapest first.
3. Click the **Details** tab to see exactly which email addresses each bidder will be written to.
4. Go to **Outbox**. Every email the app has produced is there — click one to read it exactly as
   a supplier would receive it.
5. Press **? Help** in the top bar on any screen, and **Ask** next to it if you have a question.

**Then try it from the other side.** Open a second browser window in *private/incognito* mode
(Ctrl+Shift+N in Chrome or Edge), go to `localhost:8000`, and sign in as `vendor1@demo.in`. You
can place a bid there and watch it appear in the buyer's window a few seconds later.

---

## Stopping and starting again

* **To stop:** click on the black window and press **Ctrl+C**, or just close the window.
* **To start again tomorrow:** double-click `windows-start.bat`. That is all — you never need to
  run the setup again.

Everything you do is saved in `C:\ReverseBid\data\`. It is still there next time.

---

## Turning on real emails

Out of the box the app does **not** send anything. Every message is written to the **Outbox**
page instead, so you can see exactly what suppliers would receive without risking a real email
going out. That is deliberate — leave it that way while you are experimenting.

When you are ready to send for real:

**1. Get an app password from Gmail** (your normal Gmail password will not work)

* Go to **https://myaccount.google.com/apppasswords**
* You need 2-Step Verification switched on first; Google will prompt you if it is not.
* Give it a name like `ReverseBid` and click **Create**.
* Google shows a **16-character password**. Copy it — you cannot see it again.

**2. Tell the app about it**

* In `C:\ReverseBid`, find the file **`.env.example`**.
* Copy it (Ctrl+C, Ctrl+V) and rename the copy to exactly **`.env`** — no name in front of the dot.
* Right-click `.env` → **Open with** → **Notepad**.
* Fill in these five lines and save:

```
RA_SMTP_HOST=smtp.gmail.com
RA_SMTP_PORT=587
RA_SMTP_USER=your.name@gmail.com
RA_SMTP_PASSWORD=the16characterpassword
RA_MAIL_FROM=your.name@gmail.com
```

> **If Windows hides file extensions** your copy may really be called `.env.txt`, which will not
> work. In File Explorer, click **View** → tick **File name extensions**, then rename it properly.

**3. Restart the app** — close the black window, double-click `windows-start.bat` again. The
settings are only read when the app starts, so nothing changes until you do this.

**4. Check it, on the Outbox page.** Go to **Outbox**. The panel at the top now says *Sending is
switched on* and lists the server, port and sign-in name it is using — if it still says
*Practice mode*, the app did not find your `.env` file. Then press **Send test email**. It sends
one message immediately and shows you exactly what the mail server said:

| What you see | What it means |
| --- | --- |
| *Sent to … through smtp.gmail.com* | Working. Check the inbox, and the spam folder. |
| *would not accept the username and password* | The app password is wrong, or `RA_SMTP_USER` is not the full address. Make a fresh app password. |
| *Nothing answered … within 30 seconds* | Your network is blocking port 587 — common on office and campus Wi-Fi. Try `RA_SMTP_PORT=465` with `RA_SMTP_SSL=1`, or a different network. |
| *The address … could not be found* | A typo in `RA_SMTP_HOST`. Gmail is `smtp.gmail.com`. |

### “The status says queued”

`going out…` (stored as *queued*) means the message is waiting its turn. Messages go out one at
a time, and a mail server that never answers takes up to 30 seconds to give up, so a handful can
sit there for a minute. The page refreshes itself; leave it and the status settles to **sent** or
**failed**, and a failure prints the reason underneath. If they were stuck because the app was
restarted mid-send, press **Try them again**.

> ⚠️ **Before you send anything for real, clear the demo data.** The demo suppliers have made-up
> addresses. See the next section.

While you are at it, change one more line in `.env` to any long random text — it keeps your
sign-ins secure:

```
RA_SECRET_KEY=some-long-random-line-of-text-that-only-you-know
```

---

## Starting fresh with your own data

The demo auctions and suppliers are only there so the app is not empty. To wipe them and start
clean:

1. Stop the app (close the black window).
2. Press the **Windows key**, type `cmd`, press Enter.
3. Type these two lines, pressing Enter after each:

```
cd /d C:\ReverseBid
python seed.py --reset
```

That deletes everything and rebuilds the demo. To have **nothing at all** instead, delete the
`data` folder inside `C:\ReverseBid`, start the app, and click **Create an account** on the
sign-in page to make your own buyer login.

---

## If something goes wrong

| What you see | What to do |
| --- | --- |
| `'python' is not recognized…` | Python was installed without the PATH box ticked. Reinstall it from python.org and tick **“Add python.exe to PATH”**, or try typing `py` instead of `python`. |
| Typing `python` opens the **Microsoft Store** | Press Windows key → type *“app execution aliases”* → open it → switch **off** the two entries called `python.exe` and `python3.exe`. Then install from python.org. |
| `Address already in use` or the page will not load | Something else is using port 8000. Open Command Prompt, then: `cd /d C:\ReverseBid` and `python -m uvicorn app.main:app --port 8001`, then use `localhost:8001`. |
| The black window flashes and vanishes | Open Command Prompt, `cd /d C:\ReverseBid`, then type `windows-start.bat` and press Enter. The error stays on screen so you can read it. |
| “Windows protected your PC” | Click **More info** → **Run anyway**. It appears because the file came from a download. |
| The page says **502** or will not load at all | Check the black window is still open. If it closed, start it again. |
| Emails are not arriving | Open the **Outbox** page and press **Send test email** — it tells you in one line what the mail server said. *saved here* means no mail server is set up yet (see *Turning on real emails*); *going out…* means give it a minute; *failed* prints the reason underneath. |
| Everything looks broken after an update | Stop the app, run `windows-setup.bat` again, then start it. |

---

## The command-line way (for reference)

The two `.bat` files just save you typing. If you would rather do it by hand, open Command
Prompt and run:

```
cd /d C:\ReverseBid
python -m pip install -r requirements.txt
python seed.py
python -m uvicorn app.main:app --port 8000
```

Then open `localhost:8000`. Press **Ctrl+C** in that window to stop.

To check everything is working correctly, you can also run the built-in test:

```
python tests\test_end_to_end.py
```

It walks through a complete auction — bidding, rules, emails, reports — and prints a tick beside
each of about seventy checks. It uses a temporary database, so it never touches your real data.
