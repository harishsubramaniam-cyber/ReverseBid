# Putting ReverseBid on GitHub

GitHub is where the code lives online: a backup, a place to share it with a developer, and a
record of every change. This walks you through getting it up there.

**Good news — the hard part is already done.** The folder you unzipped is already a proper Git
repository with the full project committed to it. You are not starting from scratch; you are
just pointing it at GitHub and pressing send.

Two routes below. **Route A uses an app with buttons and is the one to take.** Route B is the
same thing typed out, in case you prefer it or the app misbehaves.

---

## First: the one thing that matters

Two things in that folder must **never** go to GitHub:

* **`.env`** — if you have set up email, this holds your mail password.
* **`data\`** — the database, which will hold real supplier names and prices.

This is already handled. A file called `.gitignore` lists both, and Git obeys it automatically.
You do not have to do anything — but **after you upload, do check** (there is a step for it
below). It matters because anything pushed to GitHub stays in its history even if you delete it
afterwards.

---

## Route A — GitHub Desktop (recommended)

### 1. Install it

Download from **https://desktop.github.com** and install. Open it and sign in with your GitHub
account when it asks.

### 2. Point it at the folder

* Menu **File** → **Add local repository…**
* Click **Choose…** and select `C:\ReverseBid` (the folder with `README.md` in it).
* Click **Add repository**.

GitHub Desktop recognises it as an existing repository. In the middle of the window you will see
*“No local changes”* — that is correct and means everything is already committed and ready.

> If it says *“This directory does not appear to be a Git repository”*, you have picked the wrong
> folder. The right one has `README.md` sitting directly inside it.

### 3. Publish it

* Click the **Publish repository** button at the top.
* **Name:** `reversebid` (or anything you like — no spaces).
* **Description:** optional, e.g. *Reverse auction platform*.
* **Keep this code private** — leave this **ticked** unless you deliberately want the world to
  read it. You can flip it later.
* Click **Publish repository**.

That is it. It uploads in a few seconds.

### 4. Look at it

Click **Repository** → **View on GitHub**. Your browser opens on the uploaded project.

---

## Route B — the command line

### 1. Install Git

Download **Git for Windows** from **https://git-scm.com/download/win** and install it. Every
default option is fine — just keep clicking Next.

### 2. Tell Git who you are (once, ever)

Press the **Windows key**, type `cmd`, press Enter, then run these two lines with your own
details:

```
git config --global user.name "Harish Subramaniam"
git config --global user.email "harish.subramaniam@iiml.org"
```

### 3. Make an empty repository on GitHub

* Go to **https://github.com/new**
* **Repository name:** `reversebid`
* Choose **Private** (or Public, if you want it visible to everyone).
* ⚠️ **Do not tick** “Add a README file”, and leave the .gitignore and licence dropdowns on
  **None**. The repository must be completely empty, or the upload will be rejected.
* Click **Create repository**.

The next page shows a web address like `https://github.com/yourname/reversebid.git`. Copy it.

### 4. Send the code up

Back in Command Prompt, run these four lines. Replace the address in the second line with the
one you just copied:

```
cd /d C:\ReverseBid
git remote add origin https://github.com/yourname/reversebid.git
git branch -M main
git push -u origin main
```

A browser window will pop up asking you to sign in to GitHub. Do that, and the upload finishes
by itself. You only have to sign in the first time.

---

## Check it worked

Open your repository page on github.com and confirm:

* ✅ You can see `README.md`, and folders called `app`, `docs` and `tests`.
* ✅ Near the top, the README is displayed as a formatted page.
* ❌ There is **no** `.env` file listed.
* ❌ There is **no** `data` folder listed.

If either of those last two *is* there, tell me and I will help you remove it properly — and if a
mail password was in it, change that password.

**You may also see a small orange dot or green tick** beside the commit at the top. That is
GitHub automatically running the project's test suite. Click it to watch. A green tick means all
seventy-odd checks passed on GitHub's own machines. This is set up already.

---

## Making changes later

Once it is up there, keeping it current is quick.

**With GitHub Desktop:**

1. Change whatever you want in the files.
2. Open GitHub Desktop — your changes are listed on the left.
3. Type a short note in the **Summary** box at the bottom left, e.g. *“Changed the minimum
   decrement wording”*.
4. Click **Commit to main**, then **Push origin** at the top.

**On the command line:**

```
cd /d C:\ReverseBid
git add -A
git commit -m "Changed the minimum decrement wording"
git push
```

Each commit is a save point you can always go back to, with your note attached.

---

## Private or public?

* **Private** — only you and people you invite can see it. Right for anything with your
  company's data or plans in it. Free on GitHub, with no limit.
* **Public** — anyone can read it. Fine for this code, since it holds no passwords, but the
  `README` does describe your process.

You can switch at any time: repository → **Settings** → scroll to the bottom → **Change
repository visibility**.

To let a colleague or a developer in on a private repository: **Settings** → **Collaborators** →
**Add people**.

---

## If something goes wrong

| What you see | What it means and what to do |
| --- | --- |
| `remote origin already exists` | You have run the `git remote add` line before. Use `git remote set-url origin <the address>` instead. |
| `Updates were rejected because the remote contains work that you do not have` | The GitHub repository was not empty — usually a README got added when it was created. Easiest fix: delete that repository on GitHub (**Settings** → bottom of the page → **Delete this repository**) and create a new one with nothing ticked. |
| `src refspec main does not match any` | The branch is named something else. Run `git branch -M main` first, then push again. |
| `'git' is not recognized` | Git is not installed, or Command Prompt was open before you installed it. Close the window, open a new one, and try again. |
| GitHub Desktop says *“does not appear to be a Git repository”* | Wrong folder — choose `C:\ReverseBid`, the one containing `README.md`. |
| Asked for a password on the command line and it fails | GitHub stopped accepting account passwords. Sign in through the browser window it opens instead; if none appears, use Route A. |
| You accidentally uploaded `.env` | Change the mail password immediately, then ask for help removing it — deleting the file is not enough, because it stays in the history. |

---

## What actually got uploaded

For your own peace of mind, here is what is in there:

* `app\` — the application itself: the auction engine, the emails, the screens.
* `docs\` — these two guides.
* `tests\` — the automated check that walks a whole auction end to end.
* `seed.py` — makes the demo data.
* `README.md` — the technical description, which GitHub shows on the front page.
* `Dockerfile`, `Procfile` — for putting it on a server later.
* `.env.example` — a **blank** template showing which settings exist. Your filled-in `.env` stays
  on your PC.

Two commits are already in the history, with a description of what each one did. Anyone you hand
this to can read that history and understand how it was built.
