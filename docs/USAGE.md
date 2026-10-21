# Usage

How to install, configure and run each command. The [README](../README.md) is the overview, with links to the design notes and to how the data is handled.

## Demo

`tdx demo` runs the real auditor offline. It serves invented TDX pages on `127.0.0.1`, reads them through the same read-only request guard as `tdx audit`, and applies the same rules. It needs no accounts, no TDX access and no real data. Its browser profile, alert state, desk memory and verdict cache live in a temporary folder that is deleted when it ends; your real ones are never touched.

```bash
uv sync
```

```bash
uv run playwright install chromium
```

```bash
uv run ops-suite tdx demo
```

The queue is generated at start, with times relative to now, so the same findings appear on every run: client waiting 5 (two with no reply yet, three where the requestor wrote last), event outreach due 1, event details pending 1, event over 1, location TBD soon 1, needs review 1 and ready to close 1. A recurring event, an upcoming event with its room, two recent tickets and two the desk answered under 3 days ago raise nothing, and the six claimed tickets are only counted. The output lists each finding by its invented ticket id.

- **LLM step:** with an API key saved (`uv run ops-suite llm set-key`), the demo classifies the three "requestor wrote last" messages as synthetic data, even with `llm.enabled = false`. Classified correctly, two of them become "follow up with client". Without a key it prints `LLM: skipped (no API key saved; "uv run ops-suite llm set-key" to see this step).` `--no-llm` turns the step off. Demo tokens count against your real daily budget (`llm-usage.json`). The verdict cache is temporary, so every run classifies again.
- **`--card-json PATH`** writes the Adaptive Card JSON, for pasting into the [Adaptive Cards Designer](https://adaptivecards.io/designer/).
- **`--summary`** builds the [shift summary](#shift-summaries) instead of the alert card, as if a summary time were due now: "Events this week" (4 events), every open finding including "Ready to close", and `(possible duplicate of #1000001)` on ticket 1000023. Combine it with `--card-json` or `--send`; `--to desk` shows it without titles. `--summary-kind full|delta` (it implies `--summary`) picks which summary card: `full`, the default, is the day's first post; `delta` is a later slot's card, as if the last post were 4.5 hours ago: `Since 18:42: 2 new · 1 cleared · 11 open`, with rows for the two new findings only ([Shift summaries](#shift-summaries)).
- **`--send`** posts the card to your saved Teams webhook. It is titled "ops-suite DEMO · synthetic data", and its preview line starts with `DEMO:`. The card's ticket links point at the demo server, which stops when the command ends.
- Other options: `--headed` shows the browser window, `--verbose`, `--config PATH`.

Exit codes: as for `tdx audit` below; `2` also means `--send` with no webhook saved, and `6` means Teams did not accept the demo card.

## Quick start

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/). Everything below runs on the synthetic sample mail in `tests/fixtures/`; no network, no accounts and no secrets are involved.

Commands, here and in the tool's own hints, are shown as `uv run ops-suite …` because the project runs through `uv`; `ops-suite …` on its own works only inside an activated virtual environment.

Install:

```bash
uv sync
```

Run the checks:

```bash
uv run ruff check .
```

```bash
uv run pytest -q
```

The tests marked `browser` start Chromium on local sample pages (the capture tests in a visible window, the dashboard tests headless); run `uv run playwright install chromium` once before the first test run.

**Continuous integration.** [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push and pull request with read-only repository access and no secrets. The `lint` job runs `ruff check`. The `test` job runs the full suite, browser tests included, on Ubuntu (under `xvfb-run` so the visible-window tests have a display), and `pytest -m "not browser"` on Windows and macOS. Every test uses an in-memory keyring and temporary folders, so CI never touches a keychain, a webhook, the Claude API or TDX. To check the keyring locally, run the suite with `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring`; it still passes.

Code for one OS is tested with injected paths of that OS, so the test runs on every CI runner: the Windows Startup script tests build their paths with `PureWindowsPath`, and a check that a home folder never reaches the output looks for the test's own fake home, never a prefix such as `/Users/` that the runner's own paths share. Only tests that need the real OS (a real `cmd`, a real `launchctl`, a display) are skipped elsewhere. Local runs happen on one OS only, so check the CI run after each push.

Try the work-mailbox scan as a dry run. It classifies the sample messages and prints the counts, and writes, moves and deletes nothing:

```bash
uv run ops-suite mail scan --profile work --config tests/fixtures/config.test.toml --drop-dir tests/fixtures/mail_drop --dry-run
```

Expected first line: `Scanned 8, flagged 3, skipped 5, duplicates 0, rejected 1`.

Without `--dry-run` the scan appends flagged messages to the report, remembers which messages it has seen, and moves handled files into `processed/` and `rejected/` inside the drop folder. Run that only on a copy of the fixtures, never on `tests/fixtures/mail_drop` itself.

### Where files end up

Nothing the scan writes lands in the repository. With the default settings:

| What | Windows | macOS | Linux |
|---|---|---|---|
| Report `work/reports/direct-requests.csv` and state `work/state/seen.json` | `%LOCALAPPDATA%\ops-suite\` | `~/Library/Application Support/ops-suite/` | `~/.local/share/ops-suite/` |
| TDX alert state `tdx/alerts-state.json` (ticket ids, kinds and times only) | `%LOCALAPPDATA%\ops-suite\` | `~/Library/Application Support/ops-suite/` | `~/.local/share/ops-suite/` |
| TDX shift-summary state `tdx/summary-state-desk.json` (the last slot's and the last post's date and time, and the ticket id and finding kind of each finding open at that post) | `%LOCALAPPDATA%\ops-suite\` | `~/Library/Application Support/ops-suite/` | `~/.local/share/ops-suite/` |
| TDX desk-member memory `tdx/desk-members.json` (salted hashes and dates only) | `%LOCALAPPDATA%\ops-suite\` | `~/Library/Application Support/ops-suite/` | `~/.local/share/ops-suite/` |
| Log `ops-suite.log` (one JSON object per line) | `%LOCALAPPDATA%\ops-suite\Logs\` | `~/Library/Logs/ops-suite/` | `~/.local/state/ops-suite/log/` |
| TDX session log `tdx/session-log.json` (sign-in and expiry timestamps only) | `%LOCALAPPDATA%\ops-suite\` | `~/Library/Application Support/ops-suite/` | `~/.local/share/ops-suite/` |
| `tdx agent` output `watch.out.log`, `watch.err.log` (counts only) | `%LOCALAPPDATA%\ops-suite\Logs\` | `~/Library/Logs/ops-suite/` | (no agent) |
| Optional `config.toml` | `%LOCALAPPDATA%\ops-suite\` | `~/Library/Application Support/ops-suite/` | `~/.config/ops-suite/` |

The report holds sender and subject; the log holds counts, reason codes, message ids and file names only. `desk-members.json` stays on your computer and holds only salted hashes of the desk members the auditor has recognised, never names; delete it to reset the memory ([docs/tdx-rules.md](tdx-rules.md#the-desk-member-memory)). The Windows column assumes uv's own Python; on a `.venv` built on the Microsoft Store Python these files sit in a hidden private copy instead, see [Windows: use uv's Python](#windows-use-uvs-python). To change the folders or any other setting, copy [config.example.toml](../config.example.toml) and pass it with `--config`, or use the `OPS_SUITE_` environment variables shown in [.env.example](../.env.example).

## TDX setup (any desk member)

The queue auditor is a general program: each desk member who uses it runs it on their own computer, with their own TDX sign-in, read-only. Nothing is shared between people; the browser profile, the chosen dashboard and all output stay in that person's data folder.

1. Install:

   ```bash
   uv sync
   ```

   ```bash
   uv run playwright install chromium
   ```

2. Set `tdx.base_url` to the TDX web app's start page (`https://<your TDX host>/TDWorkManagement/`), in your `config.toml` (`[tdx]` section, see [config.example.toml](../config.example.toml)) or as `OPS_SUITE_TDX__BASE_URL` in `.env`. The real host goes only in your own config, never in the repository.

3. Sign in once, MFA included, in the tool's own browser window (never your everyday Chrome profile). Later runs reuse the saved session until it expires; when it does, `tdx rows` exits `3` and you run this again:

   ```bash
   uv run ops-suite tdx login
   ```

4. Choose your dashboard once. TDX opens your own homepage dashboard, which may not show both ticket widgets, and switching dashboards does not change the page address. So:

   ```bash
   uv run ops-suite tdx set-dashboard
   ```

   In the window it opens, sign in if asked, switch to a dashboard that shows both "Unclaimed Tickets in My Group(s)" and "Active Tickets Claimed by Others in My Group(s)" (on the desk this is the IT Tickets dashboard), wait until both have loaded, and press Enter. The tool clicks nothing; it saves the address of the inner page that holds both widgets, closes the window, and checks in the background that this address shows both widgets on its own. On success it prints `Saved. ...`; if the check fails it saves nothing and says so (exit `5`; report that line to the architect session). Run it again whenever you want `tdx rows` to use a different dashboard. If your widgets have other titles, set them in `[tdx.widgets]` first.

## TDX structure capture

Two commands for the owner's own computer. They open a visible Chromium window with the tool's own browser profile (never your everyday Chrome profile). You do all the signing in and clicking by hand; the tool only opens the start page, reads what is on screen when you press Enter, and never clicks, types or submits anything in TeamDynamix. It handles no passwords.

### First-time setup

Steps 1 to 3 of [TDX setup](#tdx-setup-any-desk-member) above: install, set `tdx.base_url`, and `tdx login`.

### The three captures to take

For each one, the command opens the browser; go to the page, wait until it has fully loaded, then press Enter in the terminal.

1. The IT Tickets dashboard:

   ```bash
   uv run ops-suite tdx capture --name dashboard
   ```

2. The "Active Tickets in My Group(s)" report from the left menu:

   ```bash
   uv run ops-suite tdx capture --name group-report
   ```

3. One ticket whose activity feed has at least one client reply and one technician reply:

   ```bash
   uv run ops-suite tdx capture --name ticket
   ```

Exit codes: `0` done, `2` configuration problem (for example `tdx.base_url` not set), `3` not signed in, `4` the browser could not start.

### What a capture contains, and how to review it

Each capture is a folder `<name>-<YYYYMMDD-HHMMSS>` holding the **structure** of every open page and frame: tag names, `id`/`class`/`role` and a few other structural attributes, and short column-header labels. Every other text becomes a placeholder such as `[datetime]`, `[number]`, `[email]` or `[text:2]` (two words); links become `[url]`. In attribute values, runs of three or more digits become `#`, words that also appear in the page text (such as names) become `*`, and the ids and names of form fields become `[id]` and `[name]`. It also lists the frames and the requests the pages made (host and path only, no query strings).

Before you share a capture:

1. Open `kept-text.txt`. Only column names (for example `Modified`, `Requestor`) may be there.
2. Search every `frame-*.html` for the names, email addresses, ticket numbers and ticket titles you saw on screen, and for `@`. None may appear.
3. If anything personal is left, delete the folder and report the problem instead of sharing it.

Then copy the reviewed folder into `tests/fixtures/tdx/captures/` (ignored by git for now) and tell the architect session.

### Where the files go

| What | Windows | macOS | Linux |
|---|---|---|---|
| Captures `tdx/captures/`, the browser profile `tdx/browser-profile/` and the chosen dashboard `tdx/dashboard.json` | `%LOCALAPPDATA%\ops-suite\` | `~/Library/Application Support/ops-suite/` | `~/.local/share/ops-suite/` |

The browser profile holds your live TDX session cookies. It stays in that folder: never copy, share or commit it. Set `tdx.profile_dir` to move it somewhere else outside the repository. `dashboard.json` holds the address `tdx set-dashboard` saved; it is specific to your account, so it is not shared either, and the tool only ever shows it without its query string.

## Reading the dashboard (tdx rows)

One read-only command, run on your own computer after the [TDX setup](#tdx-setup-any-desk-member):

```bash
uv run ops-suite tdx rows
```

It starts the tool's browser with the saved session (no window by default), opens your dashboard, finds the widgets "Unclaimed Tickets in My Group(s)" and "Active Tickets Claimed by Others in My Group(s)", reads every row on every page of both, and prints counts only:

```text
Dashboard: saved (tdx set-dashboard)
Unclaimed: 7 ticket(s) on 1 page(s).
Claimed by others: 23 ticket(s) on 3 page(s).
Unreadable dates: 0. Duplicates: 0. Blocked requests: 0. Allowed non-GET: 5.
```

The first line says where it started: `setting` (`tdx.dashboard_url`, which wins if set), `saved (tdx set-dashboard)`, or `start page` (`tdx.base_url`, when neither is set). If it starts on the start page and a widget is missing, it exits `5` and suggests running `tdx set-dashboard`.

Ticket titles, names and links stay in memory; nothing is written to disk, and the output and log hold counts, date shapes (`aaa 9/9/99 9:99 aa`) and request paths only, so the output can be pasted into a chat. Options: `--headed` shows the browser window (use it if the headless run fails), `--verbose` adds a per-path line for the allowed requests, `--config PATH` picks a config file.

**Read-only by design.** The only thing the command ever clicks is a widget's "next page" button. Every request the browser makes passes a guard: page loads and other reads (`GET`) go through, and anything else is aborted unless it is on a short, reviewed allowlist. The built-in entries are the request the dashboard makes to load a widget's rows and, for `tdx audit`, the request a ticket page makes to load its activity feed. If a run reports `Blocked: ...` lines, paste them into the architect chat; a request is added to `tdx.allowed_requests` only after that review. The page structure the selectors rely on is described in [docs/tdx-page-structure.md](tdx-page-structure.md).

Settings (`[tdx]` and `[tdx.widgets]` in [config.example.toml](../config.example.toml)): `dashboard_url` (overrides the dashboard saved by `tdx set-dashboard`), `display_timezone`, `headless`, `max_pages_per_widget`, `page_timeout_seconds`, `browser_launch_timeout_seconds` (seconds Chromium gets to start, 15 to 180, default 60), `allowed_requests`, and the two widget titles.

Exit codes: `0` done, `2` configuration problem, `3` the saved session has expired (run `uv run ops-suite tdx login` again), `4` the browser could not start (including a `tdx login` window that still has the profile open), `5` the page did not have the expected structure (the message names the widget, column or step, and lists any blocked requests).

## Auditing the queue (tdx audit)

The auditor: one read-only command that finds the tickets in the desk's queue that need attention.

```bash
uv run ops-suite tdx audit
```

It reads the dashboard exactly as `tdx rows` does, then applies the stale rules ([docs/tdx-rules.md](tdx-rules.md)):

- **Only the unclaimed queue is audited.** Student assistants never claim tickets, so the Unclaimed widget is the desk's working queue. Its non-event tickets created 48 hours ago or more get their activity feed opened: one at a time, oldest first, each in its own page that is closed again after reading, with a short pause in between.
- **Claimed tickets** belong to full-timers. They are counted, and their Prim Resp names tell the auditor who is on the desk, but their feeds are never opened and they never raise a finding.
- **Event tickets** follow the event rules only ("event over", "location TBD soon"); recurring events (weekly, daily, ...) are skipped.
- **Event requests** still titled "CCI Event Request Form" or "SCIS Event Request Form" get their page opened whatever their age; both titles match because the college is now the SCIS (formerly CCI). The request form in the ticket's description is read in memory, after cutting off the submission metadata, and only the dates, the recurring answer, what the requester is looking for, the space and the services are kept. If the event needs A/V or a classroom, they give "event outreach due" (no desk reply yet, 14 days or less to go) or "event details pending" (the desk asked 2+ days ago, 7 days or less to go). Marketing-only requests get no outreach. The card shows a fixed checklist, never a drafted reply. Details in [docs/tdx-rules.md](tdx-rules.md#event-requests).
- In a feed, only entries the requestor can see count: replies, including the requestor's own email replies. Private notes are recognised by whether the page actually *shows* their private marker. They, and status-change-only entries, are skipped.

While it reads feeds, it shows `Reading feeds: 7/16` on the terminal (stderr): one line rewritten in place, or a line every 5 tickets when the output is not a terminal. The result goes to stdout and holds counts only:

```text
Dashboard: saved (tdx set-dashboard)
Unclaimed: 18 ticket(s) on 2 page(s).
Claimed by others: 20 ticket(s) on 2 page(s).
Feeds: 16 needed, 16 read, 0 failed, 0 deferred. Entries: 97 (70 reply, 12 private, 15 system), 0 unreadable.
Findings: client waiting 2, follow up with client 0, event outreach due 1, event details pending 0, event over 1, location TBD soon 0, needs review 1.
LLM: off; 2 "requestor wrote last" findings keep their wording.
Checked: 18 unclaimed (16 feeds needed, 0 missing), 20 claimed (counted only). Events: 11, 1 recurring, 2 multi-date, 0 with an unreadable date, 1 without a location. Event forms: 2 (0 recurring, 0 unreadable); 1 renamed without a form. Event states (no finding): 1 waiting on requester, 2 in progress, 0 outreach not due yet, 0 marketing only, 0 no outreach. Desk members known: 9.
Unreadable dates: 0. Duplicates: 0. Blocked requests: 0. Live updates blocked: 37. Allowed non-GET: 21.
```

- **Feeds:** `failed` tickets had no usable link, or no feed showed within `page_timeout_seconds`; they are counted as "missing" and get no finding this run. `deferred` tickets were beyond `max_feeds_per_run` and are read on a later run. Three failures in a row stop the run with exit `5`.
- **Entries:** what the feeds held, by kind. `unreadable` entries had no author or no readable date; `--verbose` shows their shapes (`no author`, `undated: aaa 99/9/99 ...`), never their text.
- **Events:** renamed `EVENT [...]` tickets. `without a location` titles have a date and a time in their bracket but no room; they are treated like TBD.
- **Event forms:** the unclaimed event request forms. `unreadable` forms had no "Response Summary:" block or no readable date; `--show-ids` lists them by id (`  event_form_unreadable  1234567`). Look at one in TDX and describe its shape only (for example "the date is written as a weekday and month name"), never its text. `renamed without a form` are renamed events within 14 days whose page held no form block (made by hand): their space and A/V are unknown (`  event_form_missing  1234567` under `--show-ids`).
- **Event states (no finding):** the upcoming events whose page was read and that raised nothing, by who wrote last ([docs/tdx-rules.md](tdx-rules.md#event-state-payload-22)). `in progress` means the requester has answered and the desk wrote last: the tool cannot tell finished from unfinished without the events spreadsheet, so it stays quiet.
- **Desk members known:** how many people the auditor recognised as desk staff in this run: Prim Resp names, private-note and merge-notice authors, authors of replies signed with the desk signature (`tdx.rules.desk_signatures`), anyone other than the requestor seen on 2 or more tickets, and your `extra_staff_names` if you set any.
- **Live updates blocked:** each ticket page asks for live updates (SignalR: a few requests and a WebSocket). A read-only audit refuses all of them, so this number grows with every ticket opened. That is expected, and it is counted apart from `Blocked requests`, which should stay `0`.
- **Allowed non-GET:** the dashboard's widget loads plus one feed search per ticket opened.
- **LLM:** the optional follow-up classifier (off by default). With `llm.enabled` and `llm.allow_drexel_data` set in your own config, each "requestor wrote last" finding is classified once from the scrubbed text of the requestor's latest reply, and the line reads `LLM: 2 to check, 1 cached, 1 classified (expects reply 0, will follow up 1, no reply needed 0, unsure 0); skipped 0, failed 0. Today 812 of 100,000 tokens (~$0.0001).` A client who said they will follow up becomes "follow up with client" (normal priority); anything unclear stays "client waiting". Details in [docs/tdx-rules.md](tdx-rules.md#the-follow-up-classifier-payload-06b).
- Nothing is sent anywhere unless you add `--send` (see below), and nothing is ever written to TDX. Names, titles and feed text stay in memory and are never printed or logged.

**`--verbose`** adds unreadable-entry shapes, a per-path line for the live updates and allowed requests, and one line about private markers, for checking that private notes are read right:

```text
Entry markers: 97 entries, 31 nested; private marker present on 96, visible on 12; inherited-private 3; merge notes 2; reassignments 1; signature desk replies 14; visibility fallback 0.
```

"Present on" counts entries whose page carries a private marker; "visible on" counts the ones where the browser actually shows it, which is what decides "private". Merge notices ("Merged incident ...") and reassignments ("Reassigned this incident from ...") are counted on their own and never count as private notes or replies. "Signature desk replies" counts visible replies whose own text (quoted mail cut first) holds a desk signature, which made their authors desk members. `visibility fallback` should be `0`.

**`--show-ids`** adds one line per finding (and per event title whose date could not be read, per event request form that could not be read, and per failed or deferred feed): ticket id, reason code and age in hours, for example `  client_waiting  1234567  requestor_last  73h`. These lines are for your own check in TDX: look the tickets up there. **Do not paste them into a chat**; paste only the default output (and the `Entry markers:` line, which holds counts only).

Other options: `--no-llm` skips the LLM step for one run (also on `tdx watch`), `--headed` shows the browser window, `--send` posts the findings to Teams (see the next sections), `--config PATH` picks a config file. Exit codes are the same as for `tdx rows`. Under `--verbose` the `Reading feeds:` line is printed every 5 tickets even on a terminal, so the debug log lines on stderr do not break it.

Settings (in [config.example.toml](../config.example.toml)):

- `[tdx]`: `feed_delay_seconds` (pause between tickets), `max_feeds_per_run`, and `ticket_url_template`. The template is optional and normally empty: a ticket address on the same host with `{id}` for the ticket id, tried when the dashboard's ticket link does not lead to a feed.
- `[tdx.rules]`: `stale_hours`, `event_over_hours`, `event_tbd_days`, `close_after_days` ("ready to close", 3 days), `duplicate_title_similarity`, `desk_signatures` and `extra_staff_names`.
  - `desk_signatures` (default "SCIS Commons Support" and "CCI Commons Support"): a visible reply whose own text, quoted mail cut first, contains one of these phrases marks its author as desk staff and is remembered like the other strong signals. Matched ignoring case and extra spaces. The requester never becomes desk this way, and no reply text is kept.
  - `extra_staff_names` is an optional override, normally empty. Normally not needed: desk members are learned from Prim Resp, private notes, merge notices and the desk signature in their replies. If you do use it, these are real people's names: keep them only in your own config file, never in the repository.
- `[tdx.events]`: the event request forms. `form_titles` (the titles that mark an unhandled request, default "CCI Event Request Form" and "SCIS Event Request Form"); `outreach_days` (14: also how far ahead a renamed `EVENT [...]` ticket gets its form and feed read), `details_pending_days` (7), `urgent_days` (3, when "event outreach due" turns high) and `near_event_days` (2, 0 to 30: within this many days, "waiting on the requester" and "requester replied" are raised at once, at high priority); and the option prefixes, matched ignoring case at the start of an answer: `marketing_only_prefixes`, `av_service_prefixes`, `room_space_prefixes` and `known_option_prefixes` (the form's other options, so they are not counted as unknown; both "CCI Operational Assistance" and "SCIS Operational Assistance"). If the desk also reaches out for lobby bookings, add `"Lobby Area"` to `room_space_prefixes`.

## Teams setup

Alerts go to your own Teams chat through a Power Automate **Workflows** webhook. The flow runs on a standard licence (no premium connector). Set it up once:

1. In Teams, open **Workflows** and create a flow from the template **"Send webhook alerts to a chat"**.
2. The template's default target, your chat with yourself, fails with "Call made for a thread which is not a ChatThread". In both **"Post card in a chat or channel"** actions, set **Post as: Flow bot**, **Post in: Chat with Flow bot** and **Recipient:** yourself.
3. Changing "Post in" clears the **Adaptive Card** field: fill it again with the trigger's **"Attachments Adaptive Card"**, and set the False branch's **For each** input to the trigger's **Attachments**. (The editor may wrap the True branch's post in a "For each 1"; that is fine.)
4. On the trigger ("When a Teams webhook request is received"), set **Who can trigger the flow** to **Anyone**, so the script can post without signing in to Microsoft. Save.
5. Copy the trigger's URL: make.powerautomate.com, **My flows**, the flow, **Edit**, click the trigger. Treat it as a password (anyone holding it can post to your chat), and do not paste it anywhere else.

Save it in the system keychain (Keychain on macOS, Credential Manager on Windows, Secret Service on Linux). The prompt hides what you paste:

```bash
uv run ops-suite teams set-webhook
```

It must be an `https` address on a Power Automate host (`teams.webhook_host_suffixes`). It never goes into a config file, `.env`, a log or the output. Running the command again asks before replacing the saved address. Then post a synthetic test card, which arrives in your chat with Flow bot:

```bash
uv run ops-suite teams test
```

Exit codes: `0` sent, `2` no webhook saved or another configuration problem, `6` Teams did not accept the post (the reason code, such as `http_404` or `timeout`, is printed).

This is your own chat, the `personal` destination. For the desk's shared chat, see [Desk chat](#desk-chat).

## LLM setup

The optional Claude API step is off by default (`llm.enabled = false`), and text from TDX or the shared mailbox also needs `llm.allow_drexel_data = true`, set only in your own config, and only once sending scrubbed ticket text to the API is approved. Every call goes through the local scrubber first, stops at a daily token budget (`llm.daily_token_budget`) and logs token counts and an estimated cost, never text.

1. Create an API key in the Anthropic Console, and set a monthly spend limit there as a second stop behind the daily budget.
2. Save the key in the system keychain. The prompt hides what you paste, and it asks before replacing a saved key:

   ```bash
   uv run ops-suite llm set-key
   ```

3. Check the settings, the key and today's use. This makes no network call:

   ```bash
   uv run ops-suite llm status
   ```

4. Send one fixed synthetic sentence to check the key and the connection. It works with `llm.enabled = false`:

   ```bash
   uv run ops-suite llm test
   ```

   It prints `LLM test: ok (in …, out … tokens, ~$…).`

Exit codes: `0` ok, `2` no key saved or another configuration problem, `7` the call failed or was refused (the reason code, such as `http_401` or `timeout`, is printed).

## Sending alerts (tdx audit --send)

```bash
uv run ops-suite tdx audit --send
```

Runs the audit and prints the same summary as `tdx audit`, then posts **one card** with the findings that are new or due again, and prints one line:

- `Teams: sent 1 card (3 new, 1 reminder).`
- `Teams: nothing new.` (nothing is due, so no card)
- `Teams: not sent (http_400); will retry next run.` (nothing is recorded, so the same findings are due next time)

With no webhook saved it prints `Teams: not sent; run "uv run ops-suite teams set-webhook" first.` and still exits `0`, because the audit itself worked. Without `--send`, nothing is posted and the alert state is not touched.

**What the card holds:** each ticket's id with a link to it, its title (cut to 80 characters), and what the finding is and how old, for example "Requestor wrote last 3 days ago: reply or check in". It never holds a requestor's name, an email address, the responsible person or feed text. How often a finding is sent again, and the full wording table, are in [docs/tdx-rules.md](tdx-rules.md#teams-alerts-tdx-audit---send-tdx-watch).

Settings: `[tdx.alerts]` (`realert_days`, `old_finding_days`, `old_realert_days`, `title_chars`, `max_rows_per_card`, `alert_ready_to_close`) and `[teams]` (`webhook_host_suffixes`, `timeout_seconds`, and `[teams.personal]` / `[teams.desk]`) in [config.example.toml](../config.example.toml). To send to the desk's shared chat instead, add `--to desk`; see [Desk chat](#desk-chat).

## Watching the queue (tdx watch)

```bash
uv run ops-suite tdx watch
```

Runs the audit-and-send cycle every 30 minutes (`tdx.watch.interval_minutes`, 30 to 60), **only during desk hours**, in the foreground until you press Ctrl+C. Desk hours are one window per weekday in `[schedule]`. The defaults are Monday to Friday 07:30–22:00, Saturday 08:00–18:00 and Sunday 12:00–17:00, in `America/New_York`. Each cycle prints one line of counts:

```text
Watching the TDX queue every 30 min during desk hours (America/New_York). Ctrl+C stops.
14:05 26 unclaimed, 7 feeds, 6 findings; Teams: sent 1 card (2 new, 0 reminders). Next run 14:35.
Outside desk hours; next run Sat 08:00.
```

- It refuses to start (exit `2`) until a webhook is saved.
- **Session expired:** the TDX session lasts less than a day, so expect to sign in about once per desk day. The loop keeps running: it posts **one** plain card ("TDX sign-in expired ...", no ticket data) and prints `14:05 TDX sign-in expired; run "uv run ops-suite tdx login". Checking again every 10 min.` Run `uv run ops-suite tdx login` on the Mac in a second terminal and sign in. The loop checks quietly every 10 minutes inside desk hours (`tdx.watch.signed_out_retry_minutes`, 5 to 30), prints `14:25 Signed in again; resuming.` once you have signed in, and goes back to its normal interval by itself. If you are still signed out on a new day, it sends the notice once more. An expired session never counts toward the failure limit below, and it resets that count, because the browser started and reached TDX.
- **Profile in use:** while a `tdx login` (or `tdx capture`) window has the browser profile open, the loop cannot start its own browser. That is never a failure, signed in or not: it prints one line per busy spell, `14:05 TDX browser profile in use (a "tdx login" window?); checking again every 10 min.`, checks quietly every `signed_out_retry_minutes`, and goes back to its normal interval after the next successful cycle.
- **Other failures** (the browser would not start, the page structure changed) print a line such as `14:05 cycle failed (DashboardStructureError); trying again at 14:35.` A browser that would not start also names a reason code: `cycle failed (BrowserStartError: launch_timeout)`. The codes are `not_installed` (run `uv run playwright install chromium`), `launch_timeout` (Chromium did not start within `tdx.browser_launch_timeout_seconds`, default 60; raise it if this repeats), `no_display` (a visible window was asked for where there is no screen), `profile_in_use` (handled as above) and `other`. Playwright's own message is never printed or logged, because it can contain the profile folder and so your user name. After 3 failed cycles in a row (`tdx.watch.failures_before_stop`) it posts one plain "auditor stopped" card and exits with that error's code; the card and the line name the reason code too. A successful cycle resets the count. A Teams post that fails is not a failed cycle: the findings stay due for the next one.
- Every card carries a short counts-only preview line, such as `ops-suite: 3 tickets need attention (2 new, 1 reminder)`, for places where Teams cannot draw the card (notification banners, the chat list). It holds no ticket ids or titles.
- A cycle that starts inside desk hours runs to the end. If the next run time falls outside them, the loop waits for the next opening.

**On the Mac**, the computer must stay awake: keep it on power with the lid open, and run the loop under `caffeinate -i` so it does not sleep while idle:

```bash
caffeinate -i uv run ops-suite tdx watch
```

**On Windows**, `tdx watch` itself asks Windows not to sleep while it runs; see [Keeping the PC awake](#keeping-the-pc-awake) for the power settings it still needs.

If it does sleep through a run time, the loop runs as soon as it wakes, if that is inside desk hours. Options: `--headed`, `--verbose`, `--config PATH`. There is no progress line in watch mode.

To start the loop at login instead of in a terminal, see [Start at login (Mac)](#start-at-login-mac) or [Start at login (Windows)](#start-at-login-windows), and for the desk's set-up, [Recommended setup: one always-on runner](#recommended-setup-one-always-on-runner). For queue summaries at shift starts, see [Shift summaries](#shift-summaries).

## Desk chat

Cards can also go to the desk's shared Teams chat. It is a second, separate **destination**: `personal` (your own chat, the default everywhere) and `desk`. Each has its own webhook in the keychain and its own alert state. Every command that posts takes `--to personal` or `--to desk`; leave it out and nothing changes.

**Set up the second flow.** Follow the steps in [Teams setup](#teams-setup) again, with one difference: in both "Post card in a chat or channel" actions, set **Post in** to the desk's group chat (or channel) and pick it, instead of "Chat with Flow bot". Copy that flow's trigger URL and save it under the desk entry. The prompt hides what you paste, and it asks before replacing a saved desk webhook:

```bash
uv run ops-suite teams set-webhook --to desk
```

**Test it with synthetic cards only.** Both of these are allowed at any time, with or without the approval switch below:

```bash
uv run ops-suite teams test --to desk
```

```bash
uv run ops-suite tdx demo --send --to desk
```

The demo card keeps its DEMO title. Like every desk card, it shows each ticket's id with its link, the finding and its age, and **no ticket title**, because titles can contain names. `teams.desk.show_titles` (default `false`) turns titles back on; leave it off unless the manager has agreed to titles in the desk chat.

**The approval switch.** Real ticket cards go to the desk chat only when `approved` under `[teams.desk]` is `true` in **your own** config file (never in the repository's example files). Set it only once the desk's manager has approved it in writing. While it is `false` (the default), `tdx audit --send --to desk` and `tdx watch --to desk` stop with exit `2` and one line naming `teams.desk.approved`, before the browser opens. Once it is `true` (on the Mac, under `caffeinate -i` as in [Watching the queue](#watching-the-queue-tdx-watch)):

```bash
uv run ops-suite tdx audit --send --to desk
```

```bash
caffeinate -i uv run ops-suite tdx watch --to desk
```

The `Teams:` line names the destination, for example `Teams (desk): sent 1 card (3 new, 0 reminders).`

- **Separate state.** The desk chat's alert state is `alerts-state-desk.json`, beside `alerts-state.json` in the data folder. So the first desk run posts every open finding once, even the ones your own chat already had, and the two chats' reminders never affect each other.
- **Operator notices stay with you.** "TDX sign-in expired" and "auditor stopped" always go to your **personal** webhook, never the desk chat. If you run `tdx watch --to desk` with no personal webhook saved, it prints one warning at start ("sign-in notices need a personal webhook") and runs anyway; you will then only see those notices in the terminal.
- **One runner per desk chat.** The alert state is per person, so if two people run `--to desk`, the desk chat gets every card twice. Agree on one runner; everyone else keeps using their personal chat (the default).
- **Handing over.** The old runner stops their `tdx watch --to desk` first. The new runner's state starts empty, so their **first cycle re-posts every open finding once** as new. That is expected; after it, reminders follow the normal schedule. The new runner sets up their own desk flow (the steps above), saves it with `teams set-webhook --to desk` and sets `approved = true` in their own config; a webhook URL is never passed between people. The old runner can then turn their desk flow off in Power Automate.

## Shift summaries

At each shift start, `tdx watch` can post a **queue summary** to the desk chat, so every shift sees the state of the queue first thing. It is off by default (`[tdx.summary]`, `enabled = false`), and it is sent only by the **desk runner**: the one `tdx watch --to desk` (see [Desk chat](#desk-chat)), which already needs `teams.desk.approved = true`. A `tdx watch` that posts to another chat than `tdx.summary.destination` (default `desk`) prints one line at start and sends no summaries.

```toml
[tdx.summary]
enabled = true

[tdx.summary.times]
mon = ["07:30", "12:00", "17:00"]
tue = ["07:30", "12:00", "17:00"]
wed = ["07:30", "12:00", "17:00"]
thu = ["07:30", "12:00", "17:00"]
fri = ["07:30", "12:00", "17:00"]
sat = ["08:00", "12:00", "17:00"]
sun = ["12:00"]
```

These are the defaults. A `[tdx.summary.times]` table in your config replaces all of them, so list every day you want; a day left out has no summaries. Each time must lie inside that weekday's desk hours (the closing time itself is outside), or every command stops with one line naming the time and the day.

**A full summary at the first slot, deltas after (payload 17).** Each day's **first** summary posted is the **full** summary below, with the @mention: it replaces the manual check-in. If the first slot is missed (skipped past its grace window, or the auditor was not running), the next slot posted that day is full instead, so every day gets one full post. Later slots post a short **delta**:

```text
Since 07:30: 1 new · 1 cleared · 6 open
```

It has a row only for each finding that is new since the last summary posted (marked `(new)`) and each one due again under the [re-alert policy](tdx-rules.md#teams-alerts-tdx-audit---send-tdx-watch) (`· reminder`), grouped by kind; cleared findings are only counted. There is no "Events this week", no "Ready to close" and no mention. A delta with nothing new, nothing cleared and no reminder due is **not posted**: the terminal says `Teams (desk): 12:00 summary skipped (no change).` and nothing goes to your personal chat, because that is not a miss. `Queue clear` is only ever a full summary's heading. New and cleared are counted against the findings open at the last post: a finding whose ticket could not be judged this run (its feed failed, or a widget hit its page limit) is neither new nor cleared, and "Ready to close" findings are left out of both, as they are never `(new)`.

**What the full summary shows.** First **Events this week**, then **every** open finding of that cycle, not only the new ones, grouped by kind in the usual order with each kind's count, and each ticket's linked id, ending with **Ready to close**. The exact rules are in [tdx-rules.md](tdx-rules.md#shift-summaries-payload-14). New findings are marked `(new)`. Desk summaries carry no titles while `teams.desk.show_titles` is `false` (the default); the manager approved titles on 2026-10-19, so the desk runner may set it to `true` in their own config. Past `tdx.alerts.max_rows_per_card` (25) rows, each kind ends with `+N more`. An empty queue still posts, so each shift knows:

```text
TDX queue at 07:30: 6 open (2 new since yesterday)
Queue clear at 07:30: nothing needs attention.
TDX queue at 08:12 (late: due 07:30): 6 open (2 new since yesterday)
```

"New" means not open at the last summary posted ("since yesterday", or `since Fri 17:00` after a gap; just `(2 new)` before the first post).

**Where alert cards go (payload 17).** While summaries go to the desk chat, the desk runner posts **no alert card to the desk chat** on its 30-minute cycles: the desk gets summaries only. Those cards go to the runner's **personal** chat instead, with the personal chat's own state (`alerts-state.json`) and titles, just as a `tdx watch` to the personal chat would send them. The start line says `Alert cards go to the personal chat; the desk chat gets summaries only.` With no personal webhook saved, `tdx watch` warns once at start and sends no alert cards (`Teams: no alert card (no personal webhook).`).

```toml
[tdx.summary]
desk_alerts = false      # true: alert cards to the desk chat instead, as before payload 17
personal_alerts = true   # false: no alert cards at all while summaries go to the desk chat
```

With summaries off (`enabled = false`), `tdx watch --to desk` alerts the desk chat exactly as before. Every new or due finding a summary shows is recorded as alerted in the desk's own state (`alerts-state-desk.json`), so its reminders follow the re-alert policy; a due finding counted only in a `+N more` line stays due. The terminal lines are `Teams (desk): summary posted (6 open, 2 new).` and `Teams (desk): delta posted (1 new, 1 cleared, 6 open).`

**Events this week, ready to close and possible duplicates (payload 16).**

- **Events this week** lists every unclaimed event ticket with a date in the next `events_days` (7, 1 to 30) days from the summary time, sorted by date: `Wed 10/21 15:00–17:00 · Lobby · A/V: yes · no outreach yet`. Space and A/V come from the event request form, renamed `EVENT [...]` tickets' included (`—` without a form). The state comes from the title, the form and who wrote last on the feed: `no outreach yet`, `waiting on requester since Tue 08:26`, `requester replied`, `in progress (requester answered)`, `location missing`, `location TBD`, `marketing only`, `no outreach needed`, or `page not read` when the run could not read the ticket. Never names, and never the form's free-text answers or feed text.
- **Ready to close** lists tickets where the desk replied last at least `close_after_days` (3, 1 to 30) days ago and the requester has been silent since. By default it appears **only in summaries**, never marked `(new)` and never on an alert card; set `alert_ready_to_close = true` under `[tdx.alerts]` to alert it like any other finding.
- **Possible duplicates:** a row whose requester has an older open ticket with a similar title gets `(possible duplicate of #<older id>)`. `duplicate_title_similarity` (0.6) sets how similar; it is never stored or logged, and names are never shown.

```toml
[tdx.rules]
close_after_days = 3
duplicate_title_similarity = 0.6

[tdx.alerts]
alert_ready_to_close = false

[tdx.summary]
events_days = 7
```

**When.** The loop wakes at the next summary time as well as at its normal interval. A slot is **due** from its time until `grace_minutes` (90, 15 to 240) after it, inside desk hours, until it is posted. `summary-state-desk.json` in the data folder holds the last slot dealt with (posted, or skipped with no change), the last slot posted, and the ticket id and finding kind of each finding open at that post, nothing else. So a restart in the same slot does not post again, and a restart mid-day keeps counting from the last post. A file from before payload 17 keeps its slot, and the next slot posted is full.

- **Late:** if the slot's cycle could not run (signed out, a busy profile, a browser failure, a failed Teams post), the next successful cycle inside the grace window posts it. One that starts more than 15 minutes after the slot says `(late: due 07:30)` in its title, and one plain notice goes to your **personal** chat: `07:30 summary posted late at 08:12`.
- **Skipped:** if no cycle posts it within the grace window (or before the desk closes), the slot is skipped, and one notice goes to your personal chat naming the last failure: `07:30 summary not posted: TDX sign-in expired` (or the reason code, such as `BrowserStartError: launch_timeout` or `Teams post failed (http_400)`).
- A slot that passed while the auditor was not running at all is not reported.

**@mentions (optional).** `mention` under `[tdx.summary]` is `"none"` (the default), `"everyone"` or `"people"`. With `"people"`, list the desk members' **sign-in addresses** in `mention_people`. They are personal data: put them only in the runner's own config file, never in the repository's example files. Each one shows as `@` and the part of the address before the `@`, never a name read from TDX, and the addresses are never printed or logged (only the mode and a count).

```toml
[tdx.summary]
mention = "people"
mention_people = ["abc123@example.edu", "xyz45@example.edu"]
```

The card uses Microsoft's documented Adaptive Card mention format for Teams: an `<at>name</at>` run in the card and a `mention` entity under `msteams.entities`, with the sign-in address (UPN) as the id. Microsoft documents user mentions by UPN for Incoming Webhooks and bots; it documents **no "everyone" mention for cards** (only for a message posted through Microsoft Graph as a user). So `"everyone"` sends the closest candidate, an `<at>Everyone</at>` run with a `mention` entity of id `everyone`, and **it needs your live test** before you rely on it. Whether a Workflows (Flow bot) card notifies people in a group chat also needs that test. Check with a synthetic card, never a real one:

```bash
uv run ops-suite teams test --to desk --mention
```

It posts the synthetic test card with the configured mention (it needs no approval) and prints `Test card sent to the desk chat with a mention (everyone); check in Teams who was notified.` Try `mention = "everyone"` first, then `"people"` with your own address, and note which one notified you. The webhook answers before the flow runs, so if no card appears at all, open the flow's run history in Power Automate: the flow may have rejected the mention. Keep `mention = "none"` until one of them works.

## Start at login (Mac)

On macOS, `tdx agent install` sets up a launchd **user** agent that starts `tdx watch` at every login, so the loop no longer needs a Terminal left open. launchd only starts the program: desk hours, sign-in recovery and the "auditor stopped" limit work exactly as in [Watching the queue](#watching-the-queue-tdx-watch). It runs under `caffeinate -i`, and it is **never restarted** by launchd (`KeepAlive` is off), so "auditor stopped" stays stopped until you log in again or reinstall. Windows has its own version, see [Start at login (Windows)](#start-at-login-windows). Linux has no agent: there, `tdx agent` exits `2` with "start at login is for macOS and Windows only", and you run `tdx watch` in a terminal.

Every command runs from the repository folder (adjust the path if you cloned it somewhere else):

```bash
cd ~/Documents/GitHub/enterprise-ai-ops-suite
```

**Install.** Stop any `tdx watch` running in a Terminal first (Ctrl+C), because two runners would share one browser profile. Then:

```bash
uv run ops-suite tdx agent install
```

- It checks what `tdx watch` would check at start, because nobody sees a failure at login: the webhook for the destination must be saved, and `--to desk` needs `teams.desk.approved = true` ([Desk chat](#desk-chat)). Otherwise it exits `2` and installs nothing.
- It starts `tdx watch` right away, and again at every login.
- If an agent is already installed, it asks `Replace the installed start-at-login agent? [y/N]`. Replacing stops the running loop and starts the new one.
- Options: `--to desk` (cards go to the desk chat; **one runner per desk chat** applies here too), `--headed`, and `--config PATH` (the agent passes the same file to `tdx watch`).
- The agent file is `~/Library/LaunchAgents/com.ops-suite.tdx-watch.plist`. It holds the `uv` path, the repository path and the options, and no secrets: the webhooks and the API key stay in the keychain.

The Mac must stay awake with the lid open. When TDX asks you to sign in, run `uv run ops-suite tdx login` in a Terminal. While the agent is loaded, `tdx login` first prints `The start-at-login loop pauses while this window is open and resumes by itself afterwards.`: the loop prints its "profile in use" line, waits, and resumes by itself once you close the window.

The agent runs at launchd's default process type. An earlier version set `ProcessType` to `Background`, which lets macOS slow the process down enough that Chromium can miss its start timeout; if your agent was installed before this change, run `tdx agent install` again (answer `y` to replace it) to rewrite the agent file.

**Check it:**

```bash
uv run ops-suite tdx agent status
```

```text
Agent: installed yes; loaded yes; last exit code 0.
Agent file: /Users/you/Library/LaunchAgents/com.ops-suite.tdx-watch.plist
Logs: /Users/you/Library/Logs/ops-suite/watch.out.log
      /Users/you/Library/Logs/ops-suite/watch.err.log
```

"Last exit code" is the loop's exit code from the last time it stopped (for example `5` after "auditor stopped" on a changed page structure; the codes are listed under [Reading the dashboard](#reading-the-dashboard-tdx-rows)), or "no exit recorded" while it has not stopped yet.

**The logs.** `watch.out.log` and `watch.err.log` sit in the ops-suite log folder, beside `ops-suite.log`, and hold the same counts-only lines the Terminal would show: the "Watching the TDX queue ..." start line, one line per cycle, and sign-in or failure lines. To follow the loop:

```bash
tail -f ~/Library/Logs/ops-suite/watch.out.log
```

**Stop it for a day.** Uninstalling stops the loop now and keeps it from starting at the next login; run `tdx agent install` again when you want it back:

```bash
uv run ops-suite tdx agent uninstall
```

To stop only the running loop and keep the agent for the next login:

```bash
launchctl bootout gui/$(id -u)/com.ops-suite.tdx-watch
```

## Start at login (Windows)

On Windows, `tdx agent install` writes a small script, `ops-suite tdx watch.cmd`, into your **Startup folder** (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`). Windows opens it each time you sign in, and it starts the same `tdx watch` in a **minimised** window titled `ops-suite tdx watch`. Nothing else is set up: no Task Scheduler task and no Windows service. Desk hours, sign-in recovery and the "auditor stopped" limit work exactly as in [Watching the queue](#watching-the-queue-tdx-watch). When the loop stops ("auditor stopped" or Ctrl+C), its window closes, and nothing restarts it until you next sign in.

Every command runs from the repository folder, in PowerShell or a terminal (adjust the path if you cloned it somewhere else):

```powershell
cd $HOME\Documents\enterprise-ai-ops-suite
```

**Install.** Stop any `tdx watch` running in a terminal first (Ctrl+C), because two runners would share one browser profile and post twice. Then, for the desk runner:

```bash
uv run ops-suite tdx agent install --to desk
```

- It makes the same checks as on the Mac: the destination's webhook must be saved, and `--to desk` needs `teams.desk.approved = true`. Otherwise it exits `2` and writes nothing.
- It does **not** start the loop now. It starts at your next sign-in to Windows; to start it right away, open the Startup file it prints (double-click it).
- If the script is already there, it asks `Replace the installed start-at-login agent? [y/N]`. Replacing rewrites the script; a loop that is already running keeps running with its old options until you close its window.
- Options: `--to desk`, `--headed` and `--config PATH`, as on the Mac.
- The script holds the full path to `uv`, the repository folder and the options, and no secrets: the webhooks and the API key stay in Windows Credential Manager. Its output is appended to `watch.out.log` and `watch.err.log` in `%LOCALAPPDATA%\ops-suite\Logs\`.

**Check it:**

```bash
uv run ops-suite tdx agent status
```

```text
Agent: installed yes; tdx watch window running yes.
Startup file: C:\Users\you\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\ops-suite tdx watch.cmd
Logs: C:\Users\you\AppData\Local\ops-suite\Logs\watch.out.log
      C:\Users\you\AppData\Local\ops-suite\Logs\watch.err.log
Last session: signed in Mon 07:12, expiry seen Mon 19:40 (12.5 h). Median of the last 4: 11.8 h.
```

"Running" is a best-effort check: it asks `tasklist` for a window titled `ops-suite tdx watch`. It says `not found` when there is none, and `unknown` when `tasklist` could not be asked. If Windows Terminal is your default terminal app, the window title may not be visible to `tasklist`, so `not found` is not proof; the last lines of `watch.out.log` are. The last line is the [session lifetime](#tdx-session-lifetime).

**The logs.** To follow the loop in PowerShell:

```powershell
Get-Content -Wait -Tail 20 "$env:LOCALAPPDATA\ops-suite\Logs\watch.out.log"
```

**The loop did not start at login.** If there is no minimised window and no `watch.out.log`, `tdx agent status` says `The start-at-login loop has not written its log yet.`; the usual cause is a missing `Logs` folder (cmd cannot append to a log there, so `uv` never starts), which a script from an older install does not create, so run `tdx agent install` again (answer `y`), then sign out and back in. With the Microsoft Store Python, folders that Python creates under `AppData` land in a private copy that cmd cannot see; every command then warns, and the fix is [Windows: use uv's Python](#windows-use-uvs-python).

**Signing in again.** When TDX asks you to sign in, run `uv run ops-suite tdx login` in a terminal on the PC. While the loop's window is running, `tdx login` first prints that the loop pauses while the sign-in window is open; it resumes by itself.

**Stop it.** To stop the running loop, open its minimised window from the taskbar and press Ctrl+C (answer `Y` if cmd asks "Terminate batch job"), or close the window. To keep it from starting at the next sign-in as well:

```bash
uv run ops-suite tdx agent uninstall
```

Uninstalling only removes the script: a loop it already started keeps running until you close its window, and the command says so.

### Keeping the PC awake

While it runs, `tdx watch` asks Windows not to go to sleep (`SetThreadExecutionState`, the Windows counterpart of `caffeinate -i`), and it clears that request when it stops. The screen may still turn off; that is fine. The request only stops **idle** sleep, so also:

- keep the PC plugged in, and in **Settings > System > Power & battery > Screen and sleep**, set "When plugged in, put my device to sleep after" to **Never** (or longer than the gap between desk-hour runs);
- don't choose Sleep or Shut down from the Start menu while the desk is open;
- set **Windows Update > Advanced options > Active hours** to cover the desk hours. After any restart, the loop starts again only once you sign in to Windows.

## Windows: use uv's Python

`uv sync` builds the project's `.venv` on a CPython 3.12 that uv manages itself (`python-preference = "only-managed"` in `pyproject.toml`, and `3.12` in `.python-version`), and downloads it when it is not installed; this holds on every OS. A `.venv` built earlier may still run the **Microsoft Store Python**. Windows gives a Store app a private copy of `%LOCALAPPDATA%`, so everything the tool wrote went to `%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.3.12_<id>\LocalCache\Local\ops-suite\` instead of `%LOCALAPPDATA%\ops-suite\`: the browser profile with the TDX session cookies, the alert and summary state, the desk memory, the LLM usage and the logs. Python sees that copy as if it were `%LOCALAPPDATA%\ops-suite`; other programs, the Startup script's cmd among them, do not. The project's Claude deny rule covers both folders.

On a Store Python the live commands (`tdx login`, `tdx set-dashboard`, `tdx rows`, `tdx audit`, `tdx watch`, `tdx agent install`, `tdx capture`) exit `2` with `This Python is the Microsoft Store build, which hides the data folder from other programs. Rebuild the environment: see USAGE, "Windows: use uv's Python".` Every other command prints a one-line warning and runs.

To see which Python the environment uses, look at the `home` line of `.venv\pyvenv.cfg`:

```powershell
Get-Content .venv\pyvenv.cfg
```

If `home` contains `WindowsApps` or `PythonSoftwareFoundation.Python`, move once, in one sitting, from the repository folder in PowerShell. The webhooks and the API key are in Windows Credential Manager, which the Store copy does not affect, so they stay as they are.

**1. Stop the loop first.** Open its minimised `ops-suite tdx watch` window and press Ctrl+C (answer `Y`), or close the window. Do this before any other `uv` command: from this version on, `uv run` rebuilds the environment by itself, and it cannot replace files that a running loop holds open. (`tdx agent uninstall` alone does not stop a running loop, and step 6 replaces the script anyway.)

**2. Install uv's Python 3.12:**

```powershell
uv python install 3.12
```

**3. Rebuild the environment.** Delete `.venv`:

```powershell
Remove-Item -Recurse -Force .venv
```

If Windows says a file is in use, the loop is still running; go back to step 1. Then build it again:

```powershell
uv sync
```

**4. Check the new environment.** This prints the project's own interpreter, ending in `enterprise-ai-ops-suite\.venv\Scripts\python.exe`:

```powershell
uv run python -c "import sys; print(sys.executable)"
```

And `home` in `pyvenv.cfg` is now under uv's Python folder (`%APPDATA%\uv\python\cpython-3.12…`), not `WindowsApps`:

```powershell
Get-Content .venv\pyvenv.cfg
```

**5. Move the data to the real folder.** Find the `<id>` part of the Store package's folder name (the folder is `PythonSoftwareFoundation.Python.3.12_<id>`):

```powershell
Get-ChildItem $env:LOCALAPPDATA\Packages -Filter PythonSoftwareFoundation*
```

Make sure the real folder exists:

```powershell
New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\ops-suite"
```

Copy everything across, merging into what is already there (replace `<id>` with the part you found; files in the private copy win, because that is what the tool used):

```powershell
Copy-Item -Recurse -Force "$env:LOCALAPPDATA\Packages\PythonSoftwareFoundation.Python.3.12_<id>\LocalCache\Local\ops-suite\*" "$env:LOCALAPPDATA\ops-suite\"
```

**6. Install the loop again and check it.** Answer `y` when it asks to replace the installed agent:

```powershell
uv run ops-suite tdx agent install --to desk
```

To start it now, open the Startup file it printed (or sign out and back in), then:

```powershell
uv run ops-suite tdx agent status
```

It prints no Store warning, and the loop's log in the **real** folder gets its start line:

```powershell
Get-Content -Tail 5 "$env:LOCALAPPDATA\ops-suite\Logs\watch.out.log"
```

If TDX asks you to sign in after the move, run `uv run ops-suite tdx login` once.

**7. Delete the old private copy** once the loop runs from the real folder, because it still holds session cookies:

```powershell
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\Packages\PythonSoftwareFoundation.Python.3.12_<id>\LocalCache\Local\ops-suite"
```

This removes only the tool's folder inside the Store package; the Store Python itself stays installed until you remove it in Settings > Apps.

## Recommended setup: one always-on runner

The desk needs **one** runner, a computer that is on through desk hours. The owner's choice (2026-10-19) is their own always-on Windows PC, covered by the manager's "each desk member on their own computer" approval. On that PC:

1. Follow [TDX setup](#tdx-setup-any-desk-member), [Teams setup](#teams-setup) for your **personal** webhook (sign-in and "auditor stopped" notices go there), and [Desk chat](#desk-chat) for the desk webhook.
2. In your own `config.toml`: `approved = true` and `show_titles = true` under `[teams.desk]` (both approved by the manager), and `enabled = true` under `[tdx.summary]` ([Shift summaries](#shift-summaries)). The desk chat then gets **summaries only**: one full summary at the day's first slot and short deltas after, skipped when nothing changed. The 30-minute alert cards come to your **personal** chat (`tdx.summary.desk_alerts = false` and `personal_alerts = true`, the defaults).
3. Install the start-at-login script with `--to desk` ([Start at login (Windows)](#start-at-login-windows)) and keep the PC awake ([Keeping the PC awake](#keeping-the-pc-awake)).
4. **Remove the Mac's agent.** It is not needed while the PC runs, and two runners on the desk chat post every card twice. On the Mac:

   ```bash
   uv run ops-suite tdx agent uninstall
   ```

5. Use disk encryption and a screen lock on the PC (BitLocker on Windows 11 Pro, or Device encryption; a password or PIN on wake, and Win+L when you step away), because the tool's browser profile holds a live TDX session.

The same PC is where the Claude development sessions run, so the repository's `.claude/settings.json` denies those sessions any reading or editing of the ops-suite data and log folder (`%LOCALAPPDATA%\ops-suite`, and the Microsoft Store Python's private copy of it). Keep the data and log folders at their default location on that PC, or the rule will not cover them.

## TDX session lifetime

The TDX session lasts less than a day, but nobody knows exactly how long. So the tool keeps a small log: each time `tdx login` finishes signed in, it writes the time; the first time `tdx audit`, `tdx watch` or `tdx rows` then finds the session expired, it writes that time too. The file is `tdx/session-log.json` in the data folder and holds only those two timestamps per session, for the last 30 sessions. `tdx watch` also logs `tdx session expired` with `lasted_hours` in `ops-suite.log`.

```bash
uv run ops-suite tdx session
```

```text
Current session: signed in Tue 07:05, no expiry seen.
Last session: signed in Mon 07:12, expiry seen Mon 19:40 (12.5 h). Median of the last 4: 11.8 h.
```

The "Current session" line shows while the newest session has not been seen to expire. `Last session` reads `unknown` until an expiry has been seen after a `tdx login`, and the median shows from 2 ended sessions on. The same lines end `tdx agent status`. An expiry is only noticed when something runs, and `tdx watch` runs only during desk hours, so each lifetime is an upper bound: a session that expired overnight shows as lasting until the first run the next morning. A `tdx login` while the old session is still open starts a new session; the old one is left without an end and is not counted.

**"Stay signed in?"** At the Microsoft sign-in in the `tdx login` window, answering **Yes** to "Stay signed in?" can make the session in the tool's browser profile last longer, so you sign in less often (the lifetime log shows whether it does). The trade-off: a longer-lived session sits in that profile on disk. Answer Yes only on a computer with disk encryption (BitLocker or FileVault) and a screen lock.
