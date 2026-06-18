<p align="center">
  <img src="https://raw.githubusercontent.com/mrpickles007/imap-cleanup-tool/main/src/imap_cleanup_tool/assets/logo.png" alt="IMAP Cleanup Tool logo" width="360">
</p>

<h1 align="center">IMAP Cleanup Tool</h1>

<p align="center">
  <a href="https://pypi.org/project/imap-cleanup-tool/"><img src="https://img.shields.io/pypi/v/imap-cleanup-tool" alt="PyPI version"></a>
  <a href="https://pypi.org/project/imap-cleanup-tool/"><img src="https://img.shields.io/pypi/pyversions/imap-cleanup-tool" alt="Supported Python versions"></a>
  <img src="https://img.shields.io/badge/license-AGPL--3.0--or--later-blue" alt="License: AGPL-3.0-or-later">
</p>

Clean your inbox **with AI, by hand, or both.** Let an **LLM** decide what is junk
and delete it, with your choice of model:

- **Free & private** - run a local model via **Ollama**, so nothing ever leaves
  your machine; or
- **BYOA (bring your own API key)** - point it at any cloud model (OpenAI,
  OpenRouter, ...) using your own key.

Prefer to stay in control? Write precise **sender / domain / nested-rule** filters
yourself. Or **combine the two** - aim the AI at a single noisy domain, or let it
sweep a whole folder.

Bulk-delete or move IMAP emails from the **command line** and a local **web
interface**. The CLI uses only the Python standard library; the web UI and the AI
features are optional extras (see [Install](#install)).

> 💡 **Friendly heads-up:** this tool is a *little* tricky to get the hang of -
> because it has **tons of features**. The good news: **everything is documented
> in tooltips.** Hover the little **ⓘ** icons in the web UI and you'll find a
> plain-English explanation for every single option. So take a minute with this
> README, and when in doubt, **read the tooltips** - they've got your back. 🙂

- 🤖 **AI Cleanup (the headline):** a **local** heuristic scores every sender,
  then an **LLM** decides what is junk and deletes it - with a configurable
  threshold, a report-only mode, and per-model cost tracking. Pick your model:
  a **free local one** via Ollama (nothing leaves your machine), **or** your own
  cloud key (**BYOA** - OpenAI / OpenRouter / ...). Either way only sender
  **subjects + stats** are sent, never message bodies, and it works on a filter or
  a whole folder - just like Move. Even on a **cloud** model the cost is tiny: in
  testing it cleaned **~13,000 emails** from a ~40k-message mailbox for about
  **€0.03** (a local model is free). See [AI Cleanup](#ai-cleanup).
- Prefer manual control? Match by a target file (one sender/domain per line)
  **or** by a rule expression like `sender contains amazon.com OR (subject is
  Invoice AND date starts 2025-01-01)`.
- Fast **server-side search** for huge folders, or strict **local matching**.
- **Count** how many emails a filter matches before deleting anything.
- **Export / import messages**: download the matching messages (full content) -
  or a whole folder - as a single `.mbox` file (read-only, never marks mail read),
  and import a `.mbox`/`.eml` back into a folder. Handy for backups or moving mail
  between mailboxes.
- **Move** matched emails to another folder instead of deleting them, and
  **create** new folders (or **labels** on Gmail) right from the app.
- **Spam addresses**: the senders AI flags are saved per account, and you can
  **report them as spam** to the server (train it so their *future* mail
  auto-routes to spam).
- **Bulk unsubscribe from newsletters**: from that same list, unsubscribe in one
  go via each sender's `List-Unsubscribe` - **automatic** for `mailto:` (sent from
  your SMTP profile) and one-click links, open-the-page for the rest. See
  [Bulk unsubscribe from newsletters](#bulk-unsubscribe-from-newsletters).
- **Email notifications**: get a mail when a cleanup/AI run finishes, with the AI
  report attached as CSV.
- **Gmail mode**: moves matches to Trash (the only way to truly delete on Gmail).
- **Empty a whole folder** (e.g. Trash) without scanning.
- **List senders** with counts and export them to CSV (with timestamp).
- **Stop** button / cooperative cancellation for long runs.
- **Scheduler**: save jobs (including AI jobs) and **install** them into the
  system scheduler (Windows Task Scheduler / cron) - once, hourly, daily, weekly,
  monthly, or every N minutes - so they run even when the app is closed, with
  per-job logs.

> ⚠️ Deleting email is destructive. Always do a `--dry-run` first. Without
> `--expunge`, messages are only flagged deleted (often hidden by the client
> but recoverable until expunged).

---

## Table of contents

- [Quick start - web interface (with AI)](#quick-start---web-interface-with-ai)
- [Quick start - command line](#quick-start---command-line)
- [AI Cleanup](#ai-cleanup)
- [Install](#install)
- [Command-line usage](#command-line-usage)
- [Rule expressions](#rule-expressions)
- [Target file format](#target-file-format)
- [Web interface](#web-interface)
- [Folders vs labels, and moving](#folders-vs-labels-and-moving)
- [Remote / headless server (SSH port forwarding)](#remote--headless-server-ssh-port-forwarding)
- [Scheduling](#scheduling)
- [Email notifications](#email-notifications)
- [Spam addresses](#spam-addresses)
- [Bulk unsubscribe from newsletters](#bulk-unsubscribe-from-newsletters)
- [Gmail notes](#gmail-notes)

---

## Quick start - web interface (with AI)

The web interface is the easiest way to use the tool - including the AI cleanup -
and the recommended path for most users.

**Prerequisite:** Python **3.10 or newer**. Check with `python --version` (or
`python3 --version`). If it is missing, download it from
[python.org/downloads](https://www.python.org/downloads/) - on Windows, tick
*"Add python.exe to PATH"* in the installer. Linux/macOS usually ship Python, or
install it with the system package manager (`sudo apt install python3 python3-pip`,
`brew install python`, etc.).

> **Where do I type these?** In a **terminal**: **Command Prompt** or
> **PowerShell** on Windows (press <kbd>Win</kbd>, type *cmd* or *PowerShell*),
> **Terminal** on macOS (Spotlight ▸ *Terminal*) or Linux. Run the commands below
> one line at a time; lines starting with `#` are comments, don't type them.

**Windows** (PowerShell):

```powershell
# 1. Create and activate a virtual environment (keeps the install isolated)
python -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Install. [web,ai] installs the core CLI, the web UI, AND AI Cleanup
#    (it pulls in the base package automatically - no separate step needed).
pip install "imap-cleanup-tool[web,ai]"

# 3. Launch. Serves http://127.0.0.1:8765 and opens the default browser.
imap-cleanup-tool-web
```

**macOS / Linux** (bash/zsh):

```bash
# 1. Create and activate a virtual environment (keeps the install isolated)
python3 -m venv .venv
source .venv/bin/activate

# 2. Install (core CLI + web UI + AI Cleanup in one go)
pip install "imap-cleanup-tool[web,ai]"

# 3. Launch. Serves http://127.0.0.1:8765 and opens the default browser.
imap-cleanup-tool-web
```

> The virtual environment is recommended but optional - you can skip step 1 and
> just `pip install "imap-cleanup-tool[web,ai]"` globally. Either way, activate the
> same environment (`.venv\Scripts\Activate.ps1` / `source .venv/bin/activate`)
> in every new terminal before running `imap-cleanup-tool-web`. Don't want the AI
> features? Use `[web]` instead of `[web,ai]`.
>
> If `pip` is not found, use `python -m pip ...` (Windows) or `python3 -m pip ...`
> (macOS/Linux); on some systems the command is `pip3`. On Windows, if
> `Activate.ps1` is blocked, run
> `Set-ExecutionPolicy -Scope Process RemoteSigned` first, or use
> `.venv\Scripts\activate.bat` from cmd.exe.

Then, in the browser:

1. **Connect** - pick a provider preset (or type host/port), enter your username
   and password (for Gmail, an **App Password** - see [Gmail notes](#gmail-notes)),
   and click *Connect*. Optionally save it as a **connection profile** so you do
   not retype it next time.
2. **Pick folders** - select one or more folders to scan (each shows its message
   count); use *Select all* / *Deselect all* as needed.
3. 🤖 **Let AI clean it (the easy path)** - tick **AI Cleanup**, pick a model: a
   free local **Ollama** model keeps everything on your machine, or paste your own
   cloud API key in the **LLM** tab. Click **Generate report** to see exactly what
   it *would* delete, then **Run**. Only subjects + stats are ever sent to the
   model, never message bodies. See [AI Cleanup](#ai-cleanup).
4. **Or match it yourself** - either paste a **target list** (one sender or domain
   per line) or build a **rule** visually (field ▸ operator ▸ value, with AND/OR
   groups). Click **Count matching emails** to see how many would be hit.
5. **Review, then run** - *dry-run is on by default*, so the first run only
   reports. Watch the live log; use **Stop** to cancel. When the preview looks
   right, turn off dry-run (or pick an action - *Move to another folder*,
   *Gmail: move to Trash*, *Expunge*) and run for real.
6. *(Optional)* **Schedule it** - in the *Scheduling* tab, turn the same settings
   (manual or AI) into a job and install it into the system scheduler. See
   [Scheduling](#scheduling).

> On a server with no desktop, the GUI is still usable from a local browser via
> SSH port forwarding - see
> [Remote / headless server](#remote--headless-server-ssh-port-forwarding).

> ⚠️ Deleting email is destructive. Keep dry-run on until the count and log look
> right. Without *Expunge*, messages are only flagged deleted (often hidden by
> the client but recoverable until expunged).

---

## Quick start - command line

```bash
# 0. Check the installed version (update with: pip install -U imap-cleanup-tool)
imap-cleanup-tool --version

# 1. Let AI build a report of what's junk (nothing is deleted), saved to CSV.
#    Heuristic-only here (no model) - works offline; needs the [ai] extra.
imap-cleanup-tool --host imap.gmail.com --user you@gmail.com \
    --ai-cleanup --ai-report-only --ai-report-csv report.csv

# 2. Run AI Cleanup for real with a configured model (omit --dry-run to delete).
#    Use a local Ollama model to keep everything on your machine.
imap-cleanup-tool --host imap.gmail.com --user you@gmail.com \
    --ai-cleanup --ai-model my-model --dry-run

# 3. Or do it by hand: see folders, preview a target/rule cleanup, then run.
imap-cleanup-tool --host imap.gmail.com --user you@gmail.com --list-folders
imap-cleanup-tool --host imap.gmail.com --user you@gmail.com \
    --targets targets.txt --dry-run
imap-cleanup-tool --host imap.gmail.com --user you@gmail.com \
    --targets targets.txt --gmail-trash
```

> AI Cleanup needs the **`[ai]`** extra: `pip install "imap-cleanup-tool[ai]"`.
> Configure a model in the web **LLM** tab (or point at a local Ollama model).
> See [AI Cleanup](#ai-cleanup) for the full set of `--ai-*` flags.

Credentials are read from flags, then environment variables
(`IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_PORT`), then an interactive
prompt. Prefer the prompt or env vars over `--password` so the secret does not
land in your shell history.

---

## AI Cleanup

*Optional - install the AI extra:* `pip install "imap-cleanup-tool[ai]"`.

> **Local-first, and BYOA (Bring Your Own API key).** AI Cleanup runs great on a
> **free local model** (Ollama) so nothing ever leaves your machine - or you can
> **bring your own API key** for any cloud model (OpenAI, OpenRouter, ...). Your
> key, your model, your choice. Either way, only sender **subjects + stats** are
> sent to the model - **never the message body**.

AI Cleanup hands "which of these do I actually want?" to a model, safely - and
**efficiently**. The key design choice: it works on **aggregated per-sender
statistics**, never on your individual emails. It never feeds a whole mailbox to
an LLM (that would be slow and make the token count explode); a **local heuristic
does the bulk of the work**, and only a small shortlist of borderline **senders**
(with a few sample subjects each) ever reaches the model.

1. **Heuristic pre-filter (local), per sender.** It groups your mail **by sender**
   (not per individual email) and gives each sender a 0-10 **spam score** from
   signals read on your machine: `List-Unsubscribe`, the share of **unread**
   messages, send **frequency**, `Precedence: bulk`, and sender patterns
   (`noreply@`, `newsletter@`...). Weights are calibrated and **tunable**. This
   local engine does most of the filtering, fast and for free.
2. **LLM verdict.** Only senders at or above your **threshold** (default 6) are
   sent to the model, with a few sample **subjects** each (never the body); it
   replies in strict JSON which to delete. The prompt has an explicit
   **safeguard**: it must KEEP anything that looks like online orders/receipts,
   appointments/bookings, medical/health, travel, banking/tax, security/2FA, or
   personal mail - only obvious bulk (newsletters, promotions, notifications) is
   marked deletable. The reply is **validated with pydantic**, and the model is
   **retried up to 3 times** before giving up.
3. **Verdict to action** - see the two buttons below.

> 💸 **Real-world cost & speed.** In our testing, running AI Cleanup with
> **`gpt-4o-mini`** over a **~40,000-message** Gmail mailbox cost about **€0.03**
> (a few cents) and cleaned roughly **13,000 emails in ~5 minutes**. Cost scales
> with how many senders cross the threshold (only those go to the LLM, a few
> subjects each), so your mileage varies - and a **local Ollama model costs
> nothing at all**.
>
> 📉 **The more you run, the less it costs.** Every run saves the flagged senders
> to your [Spam addresses](#spam-addresses) list, and **Check spam addresses**
> (on by default) skips those known senders from the LLM on later runs - so each
> cleanup sends **fewer addresses to the model** than the last. It gets cheaper
> the more you use it.

### Generate report vs Run

- **Generate report** - builds the report and **changes nothing**. By default it
  also asks the LLM for a verdict on each flagged sender (so the report shows what
  *would* be deleted); download it as **CSV** (Excel-friendly), and the log shows
  how many emails are potentially deletable. CLI: `--ai-cleanup --ai-report-only`.
- **Skip LLM (heuristic only)** - a small checkbox next to the buttons. When
  ticked, **Generate report uses only the local heuristic score** - **no LLM call**,
  so it is free, much faster, and nothing leaves your machine; the report simply
  has no per-sender AI verdicts. CLI: `--ai-report-only` *without* `--ai-model`.
- **Run** - builds the report **and deletes** the senders the LLM confirms
  (dry-run simulates). **Run always calls the chosen LLM model, even if "Skip LLM"
  is ticked** - deleting is driven by the LLM verdict, so a model is required for
  Run. "Skip LLM" only affects *Generate report*, never *Run*. CLI: `--ai-cleanup
  --ai-model NAME` (omit `--dry-run` to actually delete).

Every report (from Generate report or Run) is **auto-saved to disk as a
timestamped CSV** in your config directory (`ai_reports/`), so reports stay
available after other runs or a restart. Reports are saved **per account** (the
account is in the file name), and the **dropdown** next to the buttons lists only
the **connected mailbox's** reports, newest-first - it refreshes automatically
when you connect or switch account. Pick one and click **Download CSV**, or
**Delete** to remove that saved report (the CSV file only - it does not touch any
email). If **email notifications for interactive runs** are enabled
(see [Email notifications](#email-notifications)), *Generate report* also **emails
you the CSV** as an attachment (named like the saved file).

**Flag senders as spam (on Run).** Optionally, when **Run** deletes a confirmed
sender, first move **one** of their messages to the **Junk/Spam** folder - the
standard "report spam" signal that trains the server to route that sender's
**future** mail to spam - then delete the rest. This also works in scheduled AI
jobs (a checkbox) and on the CLI (`--ai-flag-spam`). It needs a Junk/Spam folder
on the server.

**Check spam addresses (saves LLM tokens, on by default).** Flagged senders that
are **already in this account's [Spam addresses](#spam-addresses) list** (from
earlier reports/runs) are accepted as spam **without asking the model again**, so
fewer addresses are sent to the LLM - real token savings on repeat runs. They are
treated as confirmed for deletion with a synthetic verdict ("already in saved spam
list"). It is a checkbox in the AI panel and in scheduled AI jobs (CLI:
`--ai-no-check-spam` to turn it off). **Edge case:** an important email from a
sender already on the list would be treated as spam - this is rare, and avoided by
running with the option **off** (then every flagged sender is re-evaluated by the
LLM).

AI Cleanup deletes the **same way as a normal run**: on a regular server the
messages are flagged `\Deleted` and, if you tick **Expunge**, immediately removed
for good (otherwise they linger until an expunge). On **Gmail** they are moved to
the **Trash** and are not permanently gone until the Trash is emptied (the UI
reminds you and offers to set that up - see [Gmail notes](#gmail-notes)). On the
CLI add `--expunge` for permanent removal.

Your own mailbox address is **pre-filled in the Exclude box when you connect**, so
self-sent mail is skipped by default. **Remove that line** if you want your own
address included too, and add any other senders to skip (one per line). On the CLI
the same default applies - pass `--ai-include-self` to include your own address,
or `--ai-exclude ADDR` to skip more.

Like **Move**, AI Cleanup honors the active **filter** (target list or rule) when
one is set, or scans the **whole folder** when none is - so you can point it at a
single noisy domain or let it sweep everything.

**Models** are configured in the **LLM** tab (powered by litellm). On first run
the tool seeds two ready-to-use defaults you can edit or delete: **`gpt-4o-mini`**
(cloud, no key stored - set `OPENAI_API_KEY` or paste a key) and
**`ollama-llama3`** (free, local via Ollama). More options:

- **Local & private (recommended):** an Ollama model (e.g. `ollama/llama3`) keeps
  everything on your machine. ⚠️ A **remote** model (OpenAI, OpenRouter, ...)
  sends the sample subjects to that provider - the app warns you, and only ever
  sends subjects + stats, never message bodies.
- **Edit** a saved model from the list (the **edit** button loads it into the
  form). The key is never shown - leave the key field blank to keep the current
  one, or type a new one to replace it.
- API keys live in a local SQLite DB, optionally **encrypted** (encrypted = not
  usable in scheduling, like connection profiles). Keys are never committed.
- **Prefer an environment variable?** Leave the model's API-key field **blank**
  and export the provider's standard variable instead - e.g.
  `OPENAI_API_KEY` (OpenAI), `OPENROUTER_API_KEY` (OpenRouter). litellm picks it
  up automatically, so the key never touches disk. (PowerShell:
  `$env:OPENAI_API_KEY = "sk-..."`; bash: `export OPENAI_API_KEY=sk-...`.)
- Optional **cost tracking**: set the price per million tokens and get a
  per-model cost log.

AI Cleanup can also be **scheduled** (Scheduling tab -> "AI Cleanup job") with a
non-encrypted model; the scheduled CLI runs `--ai-cleanup --ai-model NAME`.

Everything the web panel offers is available on the CLI too - threshold, sample
size, exclusions, heuristic weights, report-only, and CSV export:

```bash
pip install "imap-cleanup-tool[ai]"
# Configure a model + API key in the LLM tab (or a local Ollama model), then:
imap-cleanup-tool --host HOST --user USER \
    --ai-cleanup --ai-model my-model --dry-run

# Heuristic report only (no LLM, nothing deleted), saved to CSV:
imap-cleanup-tool --host HOST --user USER \
    --ai-cleanup --ai-report-only --ai-report-csv report.csv

# Tune threshold/weights and add exclusions, with an LLM, report only:
imap-cleanup-tool --host HOST --user USER \
    --ai-cleanup --ai-model my-model --ai-report-only \
    --ai-threshold 7 --ai-weight unread_ratio=4 --ai-weight bulk=2 \
    --ai-exclude boss@work.com --ai-report-csv report.csv
```

> If you run an `--ai-cleanup` command without the `[ai]` extra installed, the CLI
> stops with a clear message telling you to `pip install "imap-cleanup-tool[ai]"`.

### Local header cache (faster repeat reports)

Fetching message headers is the slow part of building a report - on a slow IMAP
server it can take **several seconds per 50 messages** (a Gmail-class server does
the same in 1-2s). Without a cache, every report's speed depends **entirely on
your IMAP server**, so on a slow provider each report is slow again.

That is why **Enable local cache** (in the connection card) is **on by default**.
You can untick it; the setting is **saved with the connection profile**, and on
the CLI it is opt-in via `--local-cache` (or carried by a saved profile). With it
on, the tool caches the **immutable** header fields on your machine, keyed by
message **UID**: `From`, `Date`, `Subject`, the `List-Unsubscribe` /
`List-Unsubscribe-Post` info (so one-click unsubscribe data is cached too) and the
`Precedence: bulk` marker. **No message bodies** are ever stored, and the volatile
`\Seen` (read/unread) flag is **not** cached - it is always re-read fresh so unread
counts stay accurate. The next time, only the **new** messages are fetched, so
repeat reports are near-instant. The cache is **per account**.

The cache applies to **every operation that downloads headers**: AI reports/runs,
**List senders**, and matching with **`--scan-mode full`** (interactive *and*
scheduled jobs - they share the same cache). It does **not** apply to the default
server-side **`search`** mode (move / delete / count / list-senders in `search`
mode), because there the **server** does the filtering and no headers are
downloaded at all - so there is nothing to cache there.

> **First run on a new mailbox is the slow one.** The *first* report on a mailbox
> still fetches every header (it can take a few minutes on a big, slow mailbox) -
> that's it filling the cache. Every report after that is fast.

**The flag just controls whether this run reads/writes the cache.** With it
**off**, headers are **fetched fresh from the server every time and not stored**
(the slow path), and an existing cache is simply **left untouched and ignored** -
nothing is deleted. Re-tick **Enable local cache** any time and it **self-heals**: it reuses
what's already cached and fetches only the messages that arrived in the meantime
(headers never change, so old entries stay valid; entries for deleted messages are
just never looked up). The CLI behaves the same (an existing cache is left intact
and ignored, with a note suggesting `--local-cache`).

To wipe the cache on purpose, use the **Clear cache** button - it appears under
the checkbox when the connected account has cached headers and shows **how many
are stored**.

It stays correct: headers never change, and the volatile `\Seen` flag is always
re-read fresh (a cheap fetch) so unread counts stay accurate. The cache is pinned
to each folder's **UIDVALIDITY** (the IMAP value that changes only if the server
renumbers messages - folder recreated, mailbox migrated, ...); if it changes, the
stale rows are dropped and headers are re-fetched. Stored locally in a small
SQLite file (`header_cache.sqlite`) in your config directory.

---

## Install

Requires **Python 3.10 or newer** (`python --version`). See
[python.org/downloads](https://www.python.org/downloads/) if you need it.

### From PyPI (recommended)

A virtual environment keeps the install isolated (optional but recommended):

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
```

Then install. The base package is the CLI only; the `[web]` extra adds the web UI
and `[ai]` adds AI Cleanup (each pulls in the base package automatically):

```bash
pip install imap-cleanup-tool             # core CLI only (no AI, no web UI)
pip install "imap-cleanup-tool[web]"      # core CLI + web UI (imap-cleanup-tool-web)
pip install "imap-cleanup-tool[ai]"       # core CLI + AI Cleanup (litellm)
pip install "imap-cleanup-tool[web,ai]"   # everything (recommended)
```

You do not need to install the base separately before an extra - it is included.
The CLI stays dependency-free; the `[web]` extra pulls in FastAPI/uvicorn (and
cryptography for encrypted profiles), and the **`[ai]` extra** pulls in
**`litellm`** for [AI Cleanup](#ai-cleanup) (cloud models or a local Ollama one).
Want the AI features but not the web UI? `pip install "imap-cleanup-tool[ai]"`.

### From source

```bash
git clone https://github.com/mrpickles007/imap-cleanup-tool.git
cd imap-cleanup-tool

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -e ".[dev,web,ai]"   # editable install + dev tools + web UI + AI
```

### Running the tests

The test suite uses only the standard library (`unittest`) - nothing extra to
install:

```bash
python -m unittest discover -s tests -v
```

---

## Command-line usage

| Option | Meaning |
| --- | --- |
| `-V`, `--version` | Print the installed version and exit. |
| `--host`, `--port`, `--user`, `--password` | Connection (port default 993). |
| `--timeout N` | Socket timeout in seconds (default 120). |
| `--folder NAME` | Folder to scan; repeat for several. Default `INBOX`. |
| `--targets FILE` | Match by a target list file. |
| `--rule "EXPR"` | Match by a rule expression (see below). |
| `--scan-mode search\|full` | Server-side search (fast) or local match (strict). |
| `--include-subdomains` | In `full` mode, also match subdomains. |
| `--batch-size N` | Messages per IMAP request (default 500). |
| `--local-cache` | Cache message headers locally so repeat AI reports are faster (also enabled by a profile's setting). |
| `--list-folders` | Print folders and exit. |
| `--list-senders` | Print unique senders with counts and exit. |
| `--save-senders CSV` | With `--list-senders`, append to a CSV. |
| `--empty-folder` | Delete ALL messages in the folder(s); no filtering. |
| `--gmail-trash` | Move matches to Gmail Trash via labels. |
| `--move` | Move matches to `--dest-folder` (or **all** messages if no `--targets`/`--rule`). |
| `--dest-folder NAME` | Destination folder/label for `--move`. |
| `--create-folder NAME` | Create a folder (a label on Gmail) on the server, then exit. |
| `--delete-folder NAME` | Delete a non-system folder/label on the server, then exit. |
| `--ai-cleanup` | AI cleanup: heuristic score -> LLM verdict -> delete confirmed senders (needs `[ai]`). |
| `--ai-model NAME` | Saved (non-encrypted) LLM model config to use for `--ai-cleanup`. |
| `--ai-threshold N` | Heuristic spam-score threshold 0-10 (default 6). |
| `--ai-sample N` | Sample emails per flagged sender (default 5). |
| `--ai-exclude ADDR` | Extra sender to exclude from the report (repeatable). Your own address is excluded by default. |
| `--ai-include-self` | Include your own mailbox address in the report (by default it is excluded). |
| `--ai-weight KEY=VALUE` | Override a heuristic weight (repeatable): `list_unsubscribe`, `unread_ratio`, `bulk`, `sender_pattern`, `frequency`. |
| `--ai-report-only` | Build the report (and LLM verdicts if `--ai-model` is given) but delete nothing; a model is optional. |
| `--ai-report-csv PATH` | Write the report as CSV (Excel-friendly) to `PATH`. |
| `--ai-flag-spam` | On delete, first move one message per confirmed sender to Junk/Spam (trains the server), then delete the rest. Needs a Junk/Spam folder. |
| `--ai-no-check-spam` | Re-evaluate every flagged sender with the LLM. By default, senders already in the saved Spam list are accepted as spam without asking the model (saves tokens). |
| `--dry-run` | Report only; make no changes. |
| `--expunge` | Permanently remove after flagging. |
| `--yes` | Skip the confirmation prompt (for scripts/cron). |
| `--verbose`, `-v` | Debug logging with per-batch progress. |
| `--run-job NAME` | Run a saved scheduled job by name (used by the OS scheduler). |
| `--profile NAME` | Load host/user/password from a saved, non-encrypted profile. |
| `--notify-profile NAME` | Send the completion email from this saved (non-encrypted) SMTP profile instead of the active one. Used by scheduled jobs. |

Examples:

```bash
# Save a sender report (timestamp, account, folder, sender, count)
imap-cleanup-tool --host HOST --user USER --list-senders --save-senders senders.csv

# Empty the Trash, fast, no scan
imap-cleanup-tool --host HOST --user USER --folder Trash --empty-folder --dry-run
imap-cleanup-tool --host HOST --user USER --folder Trash --empty-folder

# Strict local matching including subdomains
imap-cleanup-tool --host HOST --user USER --targets targets.txt \
    --scan-mode full --include-subdomains --dry-run

# Create a folder/label, then MOVE matched mail into it (instead of deleting)
imap-cleanup-tool --host HOST --user USER --create-folder "Archive/2025"
imap-cleanup-tool --host HOST --user USER --targets targets.txt \
    --move --dest-folder "Archive/2025" --dry-run
```

---

## Rule expressions

In the **web UI** you build rules visually with the query builder (no typing).
The text grammar below is what the **CLI** `--rule` flag accepts and what
scheduled jobs store - the visual builder produces exactly these expressions
under the hood.

Rules are an alternative to target files, evaluated server-side via IMAP
`SEARCH`. Fields and operators:

| Field | Operators | Maps to |
| --- | --- | --- |
| `sender` | `is`, `contains` | `FROM` |
| `subject` | `is`, `contains` | `SUBJECT` |
| `date` | `is`, `starts`, `ends` | `ON`, `SINCE`, `BEFORE` |

Combine conditions with `AND` / `OR`, and group with parentheses for nesting.
Dates accept `YYYY-MM-DD`. Quote values with spaces.

```bash
imap-cleanup-tool --host HOST --user USER --dry-run \
    --rule 'sender contains amazon.com OR (subject is "Black Friday" AND date starts 2025-11-01)'
```

> `is` and `contains` both map to IMAP substring matching on the header; IMAP
> has no exact-header match, so treat `contains` as the reliable operator and
> use the target-file `--scan-mode full` path when you need strict exactness.

---

## Target file format

One entry per line; `#` starts a comment.

```text
spam@example.com        # exact sender address
*@newsletter.com        # that domain EXACTLY - never subdomains
annoying.com            # that domain, plus subdomains if --include-subdomains
mail.annoying.com       # that specific (sub)domain
```

The `*@domain` form always matches the domain exactly; the bare `domain` form
also matches subdomains when `--include-subdomains` is given. This distinction
applies to local `--scan-mode full`; server-side `search` is a substring match
either way.

So in `full` mode `*@paypal.com` is the same as `paypal.com` *without*
`--include-subdomains`. The useful part is mixing them: with
`--include-subdomains` **on**, `*@paypal.com` stays exact while bare domains
expand to their subdomains - per-entry control in a single list. Example:

```text
*@paypal.com      # exact, even with --include-subdomains
newsletter.com    # this one DOES include its subdomains
```

---

## Web interface

A local web UI (FastAPI) is the tool's graphical interface. Install the extra
and run:

```bash
pip install "imap-cleanup-tool[web,ai]"
imap-cleanup-tool-web        # serves http://127.0.0.1:8765 and opens your browser
```

Options: `--host`, `--port`, `--no-browser`. It runs only on your machine
(`127.0.0.1`) by default. The IMAP connection lives on the local server and is
reused across actions, surviving a page refresh; it is dropped automatically
after a period of inactivity. Your password is never stored.

Highlights:

- 🤖 **AI Cleanup** with a model dropdown (local Ollama or your own cloud key),
  a threshold slider, Generate report / Run, and per-model cost tracking - see
  [AI Cleanup](#ai-cleanup). The **LLM** tab has a **model picker** (presets per
  provider, an **✎ edit** toggle to type any custom litellm id, and the option to
  save or remove your own presets); the **API key is optional** - you can set the
  provider's env var (`OPENAI_API_KEY`, …) instead. When the `[ai]` extra is
  missing, the AI option is disabled with a banner explaining how to install it.
- Many provider presets, connect-and-load-folders (with per-folder message
  counts), multi-folder selection, Select all / Deselect all.
- **Connection profiles**: save host / user / password to a local SQLite DB -
  optionally **encrypted** with a password, with a per-profile **Enable local
  cache** toggle (see [AI Cleanup](#ai-cleanup)) - and pick one from a dropdown.
- Match by a **target list** (paste or load from a file, with inline format
  help) or a **visual nested query builder** (field ▸ operator ▸ value, AND/OR
  groups).
- **Count matching emails** before deleting; **dry-run** is on by default.
- **Move** matches to another folder instead of deleting (pick the destination
  from a dropdown of your folders, or create a new one inline) - or **every**
  message if you leave the filter empty; plus
  **create** and **delete** folders/labels on the server (system folders are
  protected). The folder box distinguishes *Add to scan* (just lists a folder to
  scan, creates nothing) from *Create on server* (really creates a folder, a
  **label** on Gmail) - see [Folders vs labels](#folders-vs-labels-and-moving).
- Context-aware options with tooltips (e.g. *Include subdomains* only in
  `"full"` scan mode; *Gmail: move to Trash* only for Gmail).
- Background runs with a **Stop** button and a persistent, live log panel.
- **List senders** with counts (export to CSV), a **Spam addresses** tab, an
  **Email notifications** tab, and a **Scheduling** tab to create jobs and install
  them into the OS scheduler.
- A **light / dark theme** toggle (top bar on desktop, in the menu on mobile); your
  choice is remembered. The brand colors stay the same in both.

---

## Folders vs labels, and moving

Over IMAP a "folder" and a Gmail "label" are the same thing, so creating one
works everywhere: on a normal mailbox you get a folder, on Gmail you get a label.

Two different actions in the app are easy to confuse, so they are kept distinct:

- **Add to scan** (the folder box) only adds a name to the list of folders you
  will scan. It does **not** create anything on the server - use it to scan a
  folder that was not auto-listed.
- **Create on server** actually creates a new folder/label on your mailbox (via
  IMAP `CREATE`). Use it to make a destination before moving.
- **Delete on server** (the 🗑 on a folder row, or `--delete-folder`) removes a
  folder/label from your mailbox. Only folders **you** created can be deleted;
  **system folders are protected** (see below).

**How "system folders" are detected.** The app does **not** use a hardcoded list
of names (that would break on Gmail and on non-English mailboxes). Instead it
reads each folder's IMAP attributes from the `LIST` response and protects any
folder the server marks as **special-use** (RFC 6154): `\All`, `\Archive`,
`\Drafts`, `\Flagged`, `\Junk`, `\Sent`, `\Trash`, `\Important`, or `\Noselect`
(a non-selectable container like `[Gmail]`) - plus **INBOX**, always. So
`[Gmail]/Trash`, the localized `[Gmail]/Cestino`, *Sent Mail*, Drafts, etc. are
recognized automatically by their flags, not their names. (Backstop: even if a
server failed to flag a special folder, its own `DELETE` would still refuse it.)
In the web UI those protected folders simply don't show the 🗑 button.

When you choose **Move**, the destination is a **dropdown of your existing
folders** - pick one, or choose *➕ Create new…* to make a new folder/label on the
spot (no typing the name unless you are creating one).

**Moving** copies the matched messages into the destination and removes them from
the source. The tool uses the server's `MOVE` command when available, otherwise
`COPY` + delete + expunge. On **Gmail** a move *relabels* the messages (removes
the source label, adds the destination one); the message itself still lives in
*All Mail*. Move is mutually exclusive with delete / Gmail-trash / expunge; only
*Empty folder* overrides everything.

You **cannot move a folder into itself**: the web destination dropdown lists only
folders **not** selected as the source, and the core skips any move where source
== destination (with a warning) - so the CLI and scheduled jobs are protected
too. (IMAP does not guard this reliably: the `COPY` + delete fallback could even
duplicate the messages.)

**Move everything.** If you enable Move **without** a target list or rule, it
moves **every** message in the selected folders into the destination (handy to
clear out or reorganize a whole folder). From the CLI, just omit `--targets` and
`--rule`:

```bash
imap-cleanup-tool --host HOST --user USER --create-folder "Receipts"

# Move only matches
imap-cleanup-tool --host HOST --user USER --targets bills.txt \
    --move --dest-folder "Receipts" --dry-run

# Move EVERYTHING from INBOX into Archive (no filter)
imap-cleanup-tool --host HOST --user USER --folder INBOX \
    --move --dest-folder "Archive" --dry-run
```

Move jobs can be **scheduled** like any other job (the Scheduling tab carries the
same Move setting and destination into the saved job).

---

## Remote / headless server (SSH port forwarding)

The tool can be installed on a **remote, desktop-less server** (e.g. a VPS or a
home server reached over SSH) and still be driven through the **web GUI in a
local browser**. The web server binds to the server's loopback
(`127.0.0.1:8765`) and is **not** exposed to the network; the browser reaches it
through an encrypted **SSH tunnel** that maps a local port to that loopback port.
This is the same "local port forwarding" mechanism used by the *VS Code Remote*
extension and SSH clients such as *Bitvise* or *PuTTY*.

**On the server** (in the SSH session), start the web server without trying to
open a browser it does not have:

```bash
pip install "imap-cleanup-tool[web]"
imap-cleanup-tool-web --no-browser          # listens on 127.0.0.1:8765
```

**On the local machine**, open an SSH tunnel that forwards a local port to the
server's `127.0.0.1:8765`:

```bash
# Forward local 8765  ->  server's localhost:8765
ssh -N -L 8765:localhost:8765 user@your-server
```

Then open **http://localhost:8765** in your local browser. Traffic travels inside
the SSH connection; nothing is published on the server's public interface.

- **VS Code Remote-SSH**: open the folder on the server, run `imap-cleanup-tool-web
  --no-browser` in its terminal - VS Code auto-forwards the port and offers to
  open it locally. (Add it manually in the *Ports* panel if needed.)
- **Bitvise / PuTTY**: add a *Local* (C2S) forwarding rule - listen interface
  `127.0.0.1`, listen port `8765`, destination host `localhost`, destination
  port `8765` - then browse to `http://localhost:8765`.
- Pick a different **local** port if 8765 is busy, e.g. `-L 9000:localhost:8765`
  → open `http://localhost:9000`. To run the server on another port, use
  `imap-cleanup-tool-web --no-browser --port 8800` and forward to that.

> **Keep it on loopback.** Prefer the SSH tunnel over `--host 0.0.0.0` (which
> would expose the unauthenticated UI to the whole network). The tunnel gives you
> SSH's authentication and encryption for free.

> **Keep it running after logout.** A plain SSH session stops the server when you
> disconnect. To leave it running, start it under `tmux`/`screen`, with
> `nohup imap-cleanup-tool-web --no-browser &`, or as a `systemd` service. For
> *unattended* recurring cleanups you usually want a **scheduled job** instead of
> a long-lived server - see [Scheduling](#scheduling).

---

## Scheduling

Jobs are stored as JSON in your user config directory
(`%APPDATA%\imap-cleanup-tool` on Windows, `~/.config/imap-cleanup-tool` elsewhere).
Scheduling is handled entirely by the **operating system scheduler** - there is
no background process to keep running.

Click *Install to system scheduler* to register a job directly (a `schtasks`
task on Windows, a `crontab` line on Linux/macOS) so it runs even when the app
is closed. *Export command* shows the equivalent line.

**Frequency** - pick one in the *Scheduling* tab; the form shows only the inputs
that apply:

| Frequency | Inputs | Windows | Linux/macOS |
| --- | --- | --- | --- |
| Run once | date + time | `schtasks /SC ONCE` | `at` (must be installed) |
| Every N minutes | minutes | `/SC MINUTE /MO N` | `*/N * * * *` |
| Hourly | minute of hour | `/SC HOURLY` | `M * * * *` |
| Daily | time | `/SC DAILY` | `MM HH * * *` |
| Weekly | weekday + time | `/SC WEEKLY /D` | `MM HH * * <dow>` |
| Monthly | day 1-28 + time | `/SC MONTHLY /D` | `MM HH <dom> * *` |

The time/date pickers use your system locale; the one-time date is rendered in
the system's short-date format for `schtasks`. One-time jobs on Linux/macOS use
`at`: the tool records the `at` job number on install, so they show as
*installed* (via `atq`) and can be uninstalled from the panel (via `atrm`),
just like recurring cron jobs. A one-time job that has already fired drops back
to *saved* (it is no longer queued).

> **Linux/macOS one-time jobs need `at`.** The `at`/`atq`/`atrm` commands must
> be installed **and** the `atd` daemon must be running, otherwise the job will
> not fire. On many distributions `at` is not installed by default:
> `sudo apt install at` (Debian/Ubuntu) or `sudo dnf install at` (Fedora), then
> enable the daemon with `sudo systemctl enable --now atd`. (macOS ships `at`,
> but `atrun` is disabled by default - enable it with
> `sudo launchctl load -w /System/Library/LaunchDaemons/com.apple.atrun.plist`.)
> Recurring jobs use cron instead and have no such requirement.

Each job connects with a saved **connection profile** (chosen in the Scheduling
tab), so different jobs can target different accounts. The scheduled task runs
the job by name (`imap-cleanup-tool --run-job NAME`) via the current interpreter
(so it works inside your virtualenv without relying on `PATH`); at run time the
CLI loads host / user / password from the profile's local SQLite DB. Only
**non-encrypted** profiles can be scheduled - a cron has no way to type the
password to decrypt an encrypted one.

**AI Cleanup jobs** - tick *AI Cleanup* in the Scheduling tab to schedule the AI
flow. Two extra options:

- **Report only** - the job builds the report and **deletes nothing**. The report
  is saved on disk (and listed in the *Download* dropdown), and if **email
  notifications for jobs** are enabled it is sent as a **CSV attachment**.
- **Skip LLM (heuristic only)** - build the report from the local heuristic only,
  with **no LLM call** (no API cost). It requires *Report only* (the heuristic
  alone can't decide what to delete). Both options can be combined.

A non-report-only AI job needs a **non-encrypted** LLM model (to run unattended).
On the CLI these map to `--ai-cleanup --ai-report-only` and omitting `--ai-model`.

**Logs** - every scheduled run appends to a rolling log file under
`<config dir>/logs/<job>.log`. In the *Scheduling* tab, click **logs** on any
saved job to view (or download) its run history.

---

## Email notifications

Get an email when a cleanup finishes. Configure it in the **Notifications** tab:

- **SMTP profiles** - save one or more outgoing-mail servers (host, port,
  security, username, password, From address). Works with any provider - Gmail,
  **Amazon SES**, Outlook/Microsoft 365, SendGrid, Mailgun, Postmark, Brevo, etc.
  - the provider dropdown prefills host/port/security and shows **per-provider
  tooltips** on the username/password fields. The password is stored locally
  (SQLite) and can be **encrypted** with a passphrase, exactly like connection
  profiles - the passphrase needs a **confirmation**, a show/hide toggle, and
  meets **strength criteria** before you can save (an encrypted profile can't run
  in scheduled jobs). Each profile has a **test connection** button.
- **One active profile** + a recipient address. Toggle notifications for
  **scheduled jobs** (default) and/or **interactive runs**. A **Send test email**
  button confirms it all works. With **interactive runs** on, you get an email
  when a cleanup or AI run finishes - and *Generate report* emails you the **AI
  report CSV** as an attachment.
- For a **Gmail** account, the email reminds you that the messages were moved to
  the Trash and must be emptied to delete them for good (e.g. schedule an
  *Empty folder* job on `[Gmail]/Trash`).

Sending uses the Python standard library (`smtplib`), so notifications also fire
for scheduled CLI jobs.

**Encrypted active profile - when the passphrase is asked.** The passphrase is
**never stored**, so an encrypted profile can only send when you are there to
provide it:

- **Send test email** (and a `mailto:` **bulk unsubscribe**) prompt for the
  passphrase **at send time**.
- **Interactive runs** - **Run**, **AI Cleanup**, and **Generate report** - with
  *notifications for interactive runs* enabled ask for the passphrase **up front,
  before the run starts**, with three choices: **OK** (the passphrase is
  **required** and **verified** - a wrong or empty one shows an error and asks
  again), **Skip email** (run the cleanup but send **no** email - you're asked
  again on the next run, it is never remembered), or **Cancel run** (stop). Only a
  **verified** passphrase is cached, in memory for that session only.
- **Scheduled jobs** can **not** use an encrypted profile (a cron has no one to
  type the passphrase). In the **Scheduling** tab each job has a **Notification
  SMTP profile** dropdown listing only the **non-encrypted** profiles (or *use
  active*), with a **Test** button to check the connection; if there are none, the
  job form says so and the job simply runs **without** the email even when
  notifications are enabled.

For an **encrypted LLM model** used in an interactive AI run, you enter its
passphrase in the **AI panel** before running (the *Password used to encrypt model*
field, with a show toggle). A **missing or wrong** model passphrase **fails the run
with a clear error** (shown as a banner and in the log). Scheduled AI jobs likewise
need a **non-encrypted** model.

---

## Spam addresses

Every **AI Cleanup** report or run records the flagged senders (the potential
spam) into a per-account list, shown in the **Spam addresses** tab. Each
connected mailbox has its own list.

For every address you see the data the tool computed - the 0-10 **heuristic spam
score**, message count, unread ratio, weekly frequency, the signals
(`List-Unsubscribe`, bulk, sender pattern), and the **LLM verdict** (keep/delete +
reason + confidence) when a model was used.

- **Browse** with search and **pagination** (rows per page configurable). Click
  any **column header to sort** the whole list (score, msgs, unread, msgs/week,
  signals, verdict); pagination is kept.
- **Select** rows (or *select all* across pages) for **bulk** actions.
- **Remove from list** - drops them from this list only (does not touch the
  mailbox).
- **Flag senders as spam** - for each selected sender it scans the **folders
  selected in the Cleanup tab** (just like a run; the popup shows which), finds
  their mail and reports them to the server's spam filter (so **future** mail is
  auto-routed to spam). A popup lets you choose: **move one message to Junk/Spam
  and delete the rest** (same as Run), or **move all to Spam** and delete nothing.
  Moving mail to the Junk/Spam folder (found via its special-use flag, so it works
  with localized names) is the standard "report spam" training signal. It reports
  any addresses that had no mail (e.g. already deleted). You can also flag senders
  **during cleanup** (the *Flag senders as spam* option above).
- **Add a sender manually** - type an address (with an optional 0-10 score) and
  **Add** to put it on this account's list yourself, alongside the AI-flagged ones.
- **Unsubscribe (newsletters)** - select senders and click **Unsubscribe** to use
  their `List-Unsubscribe` header. See [how it works](#bulk-unsubscribe-from-newsletters) below.

### Load saved spam into a Target list

The spam list doubles as a reusable **blocklist**. In the **Cleanup** tab, when
saved spam addresses exist for the connected account, a **Load saved Spam
addresses** box appears under the Target list. Pick a **score** condition
(`is` / `<=` / `>=` / `<` / `>`) and a threshold (default 6, step 0.1) and click
**Load** - every matching sender is appended to the target list (duplicates
skipped). From there you delete or move them with the normal tools (dry-run, move,
expunge...). This closes the loop: **AI finds the junk -> you act on it precisely.**

---

## Bulk unsubscribe from newsletters

One of the most useful things you can do from the **Spam addresses** tab: stop the
newsletters at the source. Many bulk senders include a `List-Unsubscribe` header;
the tool captures it during an AI report and lets you **unsubscribe from the
selected senders in one go** - using the same row checkboxes / *select all* as the
other bulk actions. It is **not** a magic "100% one-click", because the standard
allows different mechanisms:

- **`mailto:`** → unsubscribe by **sending an email** to the listed address. Fully
  **automatic** (sent from your **active SMTP profile** in the Notifications tab).
- **HTTPS one-click** (RFC 8058, the sender advertises `List-Unsubscribe-Post`) →
  a single **HTTPS POST** (`List-Unsubscribe=One-Click`). Fully **automatic**. If the
  endpoint sits behind a redirect, the POST is **re-issued to the new location** (so
  the body isn't dropped), and success is an **HTTP 2xx**.
- **Plain HTTPS link** (no one-click) → usually a **confirmation page** that
  **can't be automated**; you finish it by hand in the browser.

**How the `mailto:` email works (and "is an empty email enough?").** The
`List-Unsubscribe` `mailto:` carries a **unique token in the To address** (e.g.
`unsub+ab12cd@list.example`); the sender identifies *which* subscription to cancel
from that token, **not** from the message content. So the body barely matters - an
empty email would usually work. The tool still sends a minimal, standards-friendly
message: **Subject** and **Body** are taken from the `mailto:`'s `?subject=` /
`?body=` parameters, falling back to `unsubscribe` when the sender doesn't specify
them. The email is sent **from your active SMTP profile's From address**. Because
almost all senders rely on the To token, this works regardless of the From; a
**few** senders verify the From matches the originally-subscribed mailbox - for
those, use an **SMTP profile for the same mailbox you are cleaning**.

So the result is **automatic for most, plus open-the-page for the rest**. The
**Unsub** column shows the state per sender:

- **`✓ done`** - already unsubscribed; hover for the **method, date and result** of
  the request (this is recorded per sender once an automatic unsubscribe succeeds).
- **`auto ✉`** - automatic via a `mailto:` (an email sent from your active SMTP
  profile). **`auto`** - automatic via a one-click HTTPS request (no SMTP needed).
- **`link ↗`** - a confirmation page you open by hand.
- **`rescan`** - a `List-Unsubscribe` was seen but no usable link is stored **yet**;
  just **run a fresh AI report** and it is normally captured. If it still persists,
  **clear the local cache** (connection card) and run the report again.
- **`none`** - no `List-Unsubscribe` at all, so the sender can't be unsubscribed
  from here (you can still flag or remove it). Only senders that actually have the
  header can be unsubscribed.

If your selection includes senders you already unsubscribed (**`✓ done`**), it first
asks whether to **re-do** them (e.g. if the first attempt didn't work) or **skip**
them. If any selected sender can only be unsubscribed by **email** but you have no
active SMTP profile, a banner points you to the **Notifications** tab to set one up.
After
the action you get a summary (*N unsubscribed automatically, M need a manual page,
K failed*). If a sender's **email** send **errors**, it **falls back to the sender's
HTTPS link** when there is one - so it becomes a **manual** row you can finish by
hand rather than a hard failure. (Senders that need email when you have **no active
SMTP profile** are flagged up front by the banner above the list and reported with a
**reason**, rather than quietly using another method.) The summary shows the
**reason** for any failures so you know what to fix. Rather than blasting dozens of browser tabs
(pop-up blockers eat them anyway), the list
then **filters itself to the manual ones** so they are the only rows left - open
each with its per-row **`link ↗`**. You can reach that view any time with the
**Unsub filter** at the top of the tab (`all` / `auto` / `manual` / `none` /
`done` = already unsubscribed).

> ⚠️ This makes **outbound requests** (an email and/or web POST/GET), so it's a
> deliberate step. Use it for **newsletters** (legitimate senders with a
> `List-Unsubscribe`), **not** for real spam - unsubscribing from actual spam just
> confirms your address is live. `mailto:` unsubscribes need an **active SMTP
> profile** configured in the Notifications tab.

---

## Gmail notes

1. Enable 2-Step Verification, then create an **App Password** and use it
   instead of your normal password.
2. Enable IMAP in Gmail settings.
3. Host is `imap.gmail.com`. Folder names are special: `[Gmail]/Trash`,
   `[Gmail]/All Mail`, `[Gmail]/Spam` (localised, e.g. `[Gmail]/Cestino`).
4. Use `--gmail-trash`: a plain delete in `INBOX` does **not** delete - it only
   removes the `INBOX` label, so Gmail **archives the message to All Mail** (it's
   still there, recoverable). To actually delete, move it to Trash (`--gmail-trash`
   / the web UI's *Gmail: move to Trash*) and then empty the Trash. Target
   `[Gmail]/All Mail` to catch archived mail too.
   - **Expunge is disabled on Gmail** in the web UI (it has no effect there): Gmail
     **auto-expunges** deleted messages on its own, and expunging from a folder only
     archives to All Mail. The web UI also warns when you pick a plain delete on Gmail.
5. **Trash is not permanent deletion.** On Gmail, deleting (including AI Cleanup)
   moves mail to `[Gmail]/Trash`; it stays there until the Trash is emptied. The
   web UI shows a reminder after any run that trashed mail and offers to select
   the Trash folder with **Empty folder** ticked - press Run to remove it for
   good. On the CLI, run `--empty-folder` against `[Gmail]/Trash` (or wait for
   Gmail's automatic 30-day purge).

---

## Support

Questions, bugs, or feature ideas? Open an
[issue](https://github.com/mrpickles007/imap-cleanup-tool/issues) or email
**support@imapcleanuptool.com**.

---

## License

**GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)** - see
[LICENSE](LICENSE).

This is free, open-source software with a strong copyleft: you may use, study,
modify, and redistribute it, but **any derivative work - including software that
reuses any part of this code, and modified versions offered over a network as a
service - must also be released as open source under the AGPL-3.0**. You cannot
incorporate this code into a closed-source or proprietary product.

Contributions are welcome - see [CONTRIBUTING.md](CONTRIBUTING.md).
