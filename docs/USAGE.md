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
| TDX API shadow-mode record `tdx/api-shadow.jsonl` (times, seconds, counts, ticket ids and reason codes only; 30 days) | `%LOCALAPPDATA%\ops-suite\` | `~/Library/Application Support/ops-suite/` | `~/.local/share/ops-suite/` |
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

   In the window it opens, sign in if asked, switch to a dashboard that shows both "Unclaimed Tickets in My Group(s)" and "Active Tickets Claimed by Others in My Group(s)" (on the desk this is the IT Tickets dashboard), wait until both have loaded, and press Enter. The tool clicks nothing; it saves the address of the inner page that holds both widgets, closes the window, and checks in the background that this address shows both widgets on its own. On success it prints `Saved. ...`; if the check fails it saves nothing and says so (exit `5`; choose another dashboard and run it again). Run it again whenever you want `tdx rows` to use a different dashboard. If your widgets have other titles, set them in `[tdx.widgets]` first.

## TDX structure capture

Two commands to run on your own computer. They open a visible Chromium window with the tool's own browser profile (never your everyday Chrome profile). You do all the signing in and clicking by hand; the tool only opens the start page, reads what is on screen when you press Enter, and never clicks, types or submits anything in TeamDynamix. It handles no passwords.

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
3. If anything personal is left, delete the folder. Describe the problem by its shape ("an email address inside a checkbox id"), never by the text you found.

Keep reviewed captures on your own computer, outside git (`tests/fixtures/tdx/captures/` is ignored). They are what to compare against when a TDX update breaks a selector. When you share what a capture shows, share only what the review found (counts, element and attribute shapes), never page text.

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

**Read-only by design.** The only thing the command ever clicks is a widget's "next page" button. Every request the browser makes passes a guard: page loads and other reads (`GET`) go through, and anything else is aborted unless it is on a short, reviewed allowlist. The built-in entries are the request the dashboard makes to load a widget's rows and, for `tdx audit`, the request a ticket page makes to load its activity feed. If a run reports `Blocked: ...` lines, review each request before adding it to `tdx.allowed_requests`: what the page was doing when it made the request, and whether the request changes anything in TDX. Allow only reads, and writes the page makes on its own just to display itself, like the built-in entries ([docs/tdx-page-structure.md](tdx-page-structure.md) explains each one). The page structure the selectors rely on is described in [docs/tdx-page-structure.md](tdx-page-structure.md).

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
Checked: 18 unclaimed (16 feeds needed, 0 missing), 20 claimed (counted only). Events: 11, 1 recurring, 2 multi-date, 0 with an unreadable date, 1 without a location. Form-titled tickets: 2 (0 recurring, 0 unreadable); renamed: 3 with a form, 1 without. Event states (no finding): 1 waiting on requester, 2 in progress, 0 outreach not due yet, 0 marketing only, 0 no outreach. Desk members known: 9.
Unreadable dates: 0. Duplicates: 0. Blocked requests: 0. Live updates blocked: 37. Allowed non-GET: 21.
```

- **Feeds:** `failed` tickets had no usable link, or no feed showed within `page_timeout_seconds`; they are counted as "missing" and get no finding this run. `deferred` tickets were beyond `max_feeds_per_run` and are read on a later run. Three failures in a row stop the run with exit `5`.
- **Entries:** what the feeds held, by kind. `unreadable` entries had no author or no readable date; `--verbose` shows their shapes (`no author`, `undated: aaa 99/9/99 ...`), never their text.
- **Events:** renamed `EVENT [...]` tickets. `without a location` titles have a date and a time in their bracket but no room; they are treated like TBD.
- **Form-titled tickets:** the unclaimed tickets still titled like the event request form. `unreadable` forms had no "Response Summary:" block or no readable date; `--show-ids` lists them by id (`  event_form_unreadable  1234567`). Look at one in TDX and describe its shape only (for example "the date is written as a weekday and month name"), never its text. `renamed` counts the renamed events within 14 days whose page was read: `with a form` held a form block that could be read, `without` held none (made by hand), so their space and A/V are unknown (`  event_form_missing  1234567` under `--show-ids`). The desk renames a form ticket at first handling, so a queue of handled events shows `Form-titled tickets: 0` and its forms under `renamed`.
- **Event states (no finding):** the upcoming events whose page was read and that raised nothing, by who wrote last ([docs/tdx-rules.md](tdx-rules.md#event-state)). `in progress` means the requester has answered and the desk wrote last: the tool cannot tell finished from unfinished without the events spreadsheet, so it stays quiet.
- **Desk members known:** how many people the auditor recognised as desk staff in this run: Prim Resp names, private-note and merge-notice authors, authors of replies signed with the desk signature (`tdx.rules.desk_signatures`), anyone other than the requestor seen on 2 or more tickets, and your `extra_staff_names` if you set any.
- **Live updates blocked:** each ticket page asks for live updates (SignalR: a few requests and a WebSocket). A read-only audit refuses all of them, so this number grows with every ticket opened. That is expected, and it is counted apart from `Blocked requests`, which should stay `0`.
- **Allowed non-GET:** the dashboard's widget loads plus one feed search per ticket opened.
- **LLM:** the optional follow-up classifier (off by default). With `llm.enabled` and `llm.allow_institutional_data` set in your own config, each "requestor wrote last" finding is classified once from the scrubbed text of the requestor's latest reply, and the line reads `LLM: 2 to check, 1 cached, 1 classified (expects reply 0, will follow up 1, no reply needed 0, unsure 0); skipped 0, failed 0. Today 812 of 100,000 tokens (~$0.0001).` A client who said they will follow up becomes "follow up with client" (normal priority); anything unclear stays "client waiting". Details in [docs/tdx-rules.md](tdx-rules.md#the-follow-up-classifier).
- Nothing is sent anywhere unless you add `--send` (see below), and nothing is ever written to TDX. Names, titles and feed text stay in memory and are never printed or logged.

**`--verbose`** adds unreadable-entry shapes, a per-path line for the live updates and allowed requests, one line about private markers, for checking that private notes are read right, and, when event forms were read, one line about how the page shows their descriptions:

```text
Entry markers: 97 entries, 31 nested; private marker present on 96, visible on 12; inherited-private 3; merge notes 2; reassignments 1; signature desk replies 14; visibility fallback 0.
```

"Present on" counts entries whose page carries a private marker; "visible on" counts the ones where the browser actually shows it, which is what decides "private". Merge notices ("Merged incident ...") and reassignments ("Reassigned this incident from ...") are counted on their own and never count as private notes or replies. "Signature desk replies" counts visible replies whose own text (quoted mail cut first) holds a desk signature, which made their authors desk members. `visibility fallback` should be `0`.

```text
Event form descriptions: 5 read; with hidden text 5, clipped 0, with a toggle 5.
Event form labels (space/services) found in: shown 0/0, container 5/5, outside 0/0, text box 0/0, template 0/0, attribute 0/0, script 0/0, anywhere 5/5.
Event form blocks: 5 forms, 10 Response Summary blocks, 5 with all 6 answers; used from container 5, outside 0, text box 0, template 0, attribute 0, script 0.
```

Counts and place names only, never text:

- The first line: how many form descriptions the page partly hides (`with hidden text`), caps in height (`clipped`), or shows next to a "show more"-style control (`with a toggle`).
- The second: in how many descriptions the "What type of space" and "Please select all that apply" labels appear in each place: `shown` (the text the page displays), `container` (the description's whole DOM, hidden parts included), `outside` (the rest of its section), `text box`, `template`, `attribute`, `script`, and `anywhere` in the page's HTML. `anywhere 0/0` means the full form is not in the page at all.
- The third: what the reader made of it: the Response Summary blocks it found over every copy, the forms whose block held all six labels it reads, and where the block it used came from.

The form is read from every copy in the page's HTML whatever these say, without clicking. With `--source api` there are no such lines.

**`--show-ids`** adds one line per finding (and per event title whose date could not be read, per event request form that could not be read, and per failed or deferred feed): ticket id, reason code and age in hours, for example `  client_waiting  1234567  requestor_last  73h`. It also adds one line per upcoming event ticket whose page was read, finding or not, so every event can be checked: `  event_state  1234567  waiting_on_requester  A/V on form: yes  classroom`. The state is `marketing_only` or `no_outreach` from the form, else who wrote last on the feed (`no_outreach_yet`, `waiting_on_requester`, `requester_replied`, `in_progress`); then whether the form's services ask for A/V (`—` without a form) and the type of space (`lobby`, `classroom`, `other`, or `—`). These lines are for your own check in TDX: look the tickets up there. **Do not paste them into a chat**; paste only the default output (and the `Entry markers:` line, which holds counts only).

Other options: `--no-llm` skips the LLM step for one run (also on `tdx watch`), `--headed` shows the browser window, `--send` posts the findings to Teams (see the next sections), `--config PATH` picks a config file. Exit codes are the same as for `tdx rows`. Under `--verbose` the `Reading feeds:` line is printed every 5 tickets even on a terminal, so the debug log lines on stderr do not break it.

Settings (in [config.example.toml](../config.example.toml)):

- `[tdx]`: `feed_delay_seconds` (pause between tickets), `max_feeds_per_run`, and `ticket_url_template`. The template is optional and normally empty: a ticket address on the same host with `{id}` for the ticket id, tried when the dashboard's ticket link does not lead to a feed.
- `[tdx.rules]`: `stale_hours`, `event_over_hours`, `event_tbd_days`, `close_after_days` ("ready to close", 3 days), `duplicate_title_similarity`, `desk_signatures` and `extra_staff_names`.
  - `desk_signatures` (default "SCIS Commons Support" and "CCI Commons Support"): a visible reply whose own text, quoted mail cut first, contains one of these phrases marks its author as desk staff and is remembered like the other strong signals. Matched ignoring case and extra spaces. The requester never becomes desk this way, and no reply text is kept.
  - `extra_staff_names` is an optional override, normally empty. Normally not needed: desk members are learned from Prim Resp, private notes, merge notices and the desk signature in their replies. If you do use it, these are real people's names: keep them only in your own config file, never in the repository.
- `[tdx.events]`: the event request forms. `form_titles` (the titles that mark an unhandled request, default "CCI Event Request Form" and "SCIS Event Request Form"); `outreach_days` (14: also how far ahead a renamed `EVENT [...]` ticket gets its form and feed read), `details_pending_days` (7), `urgent_days` (3, when "event outreach due" turns high) and `near_event_days` (2, 0 to 30: within this many days, "waiting on the requester" and "requester replied" are raised at once, at high priority); and the option prefixes, matched ignoring case at the start of an answer: `marketing_only_prefixes`, `av_service_prefixes`, `room_space_prefixes` and `known_option_prefixes` (the form's other options, so they are not counted as unknown; both "CCI Operational Assistance" and "SCIS Operational Assistance"). If the desk also reaches out for lobby bookings, add `"Lobby Area"` to `room_space_prefixes`.

## TDX API (proof of concept)

A second, read-only way to read the same queue: through the TDX Web API instead of the dashboard in a browser. It gives the same rows, feeds and event forms to the same rules, so the two can be compared. **The browser stays the default source.** `tdx watch` reads through the API beside the browser in [shadow mode](#shadow-mode-browser-and-api-side-by-side), and instead of it, with the browser as a fallback, once `tdx.source = "api"` ([Reading through the API](#reading-through-the-api)). It is off until you set `tdx.api.enabled = true` in your own config.

**What it reads, all with `GET`:**

| Request (below `tdx.api.base_path`) | What for |
|---|---|
| `GET /auth/getuser` | `tdx api check`: who the token belongs to (only the role is printed). |
| `GET /reports` | `tdx api reports`: the reports you can see. |
| `GET /reports/{id}?withData=true` | The rows: two Report Builder reports that show the same tickets and columns as the two dashboard widgets. |
| `GET /{app id}/tickets/{id}/feed` | A ticket's feed entries, for the same tickets the browser audit opens. |
| `GET /feed/{entry id}` | An entry's replies (the ticket feed leaves them out and only counts them), only for entries that have some. |
| `GET /{app id}/tickets/{id}` | An event request form ticket's description. |
| `GET /tickets/{id}` | Only when `tdx.api.app_id` is 0: the first ticket's app id. |

Every request is checked against that list in code before it is sent: any other method (`POST`, `PUT`, `PATCH`, `DELETE`, the searches included), any other path, unknown query parameters and other hosts are refused, and so are the endpoints that sign in with a user name and password or with service-account keys, whatever the method. Requests are paced at least `min_request_interval_ms` apart (TDX allows 60 a minute per address on most of these and 30 a minute per person on report reads); a `429` waits for TDX's reset time (5 to 65 seconds) and is tried once more.

**The token.** The Web API's SSO endpoint (`GET /auth/loginsso`) issues a token for the person who signs in through it, with your own rights and nothing more. The Web API has **a sign-in of its own**, apart from the TDX pages': the endpoint sends the browser to your identity provider, which may ask you to sign in again (MFA included) even while your TDX session is fine. You always do that sign-in yourself; the tool only waits for the token to arrive and never fills in or submits the sign-in pages.

- `uv run ops-suite tdx login` asks for a token right after the TDX sign-in. It prints `The TDX API uses its own sign-in. If a sign-in page appears, sign in again (MFA included).` and opens the endpoint in the same window. Sign in if asked; the command ends by itself when the token arrives (no Enter needed) with `TDX API token: saved, expires Wed 09:14.` and closes the window. If you do not finish within `tdx.api.token_sign_in_minutes` (default 5), it prints `TDX API token: not saved (sign_in_timeout).`; your TDX session is saved either way.
- `uv run ops-suite tdx api token --headed` does only that second sign-in, in a window, the same way.
- `uv run ops-suite tdx api token` (no `--headed`) tries headless first, with the saved browser profile. If the identity provider wants you to sign in, it prints `TDX API token: needs sign-in; run "uv run ops-suite tdx api token --headed".` and exits `3`. That is about the API's sign-in only: it is not an expired TDX session, so nothing is written to the session log and `tdx watch` is not affected.
- the API commands ask again by themselves, headless, when the stored token has expired or has less than 30 minutes left, or when the API refuses it (`401`). When that headless request needs a sign-in, they use the stored token while it lasts, and otherwise stop with the same `needs sign-in` line and exit `3`.

**How often:** tokens last 24 hours. On some setups the headless request gets a fresh one with the saved sign-in; where the identity provider asks for a sign-in every time (the second sign-in's session is not kept for a new browser), plan on running `uv run ops-suite tdx api token --headed` **once a day** while you use the API. Where a background loop reads through the API, a refresh that reaches the sign-in page sends one plain notice a day to your own chat ("TDX API token needs a sign-in: run ... tdx api token --headed"); it never counts as a failed cycle.

If the identity provider shows an error page instead of a sign-in (an HTTP error status), the request stops at once with `idp_refused`. The error can be temporary: try again once. If it keeps failing, whoever runs the sign-in service may need to allow the Web API. An error page that the provider serves as a normal page looks like a sign-in page to the tool, so it ends with `sign_in_timeout` (or `needs_sign_in` headless) instead; the window shows its message.

The token is kept only in the system keychain (service `ops-suite`, user `tdx-api-token`; a token longer than one Windows Credential Manager entry is split over `tdx-api-token-1`, `-2`, ...). It is never printed, logged, written to a file or put in config or `.env`. Its expiry is read from the token itself and only the expiry is shown. It is wiped once it has expired or the API refuses it.

The headless request goes through the read-only guard, which lets the browser load only the token endpoint under the Web API's auth path; if the sign-in on the way needs a request the guard refuses, the command lists it as `Blocked: METHOD /path` (review it as for `tdx rows`). The sign-in in a window (`tdx login`, `tdx api token --headed`) runs without the guard, as `tdx login` always has: you are at the window, and the identity provider's sign-in form posts to the provider and then back to TDX, writes the guard would refuse. The tool itself only opens the token endpoint there. The log lists, by path only, the TDX writes seen on the way.

Reason codes when no token is saved: `needs_sign_in` (headless; run `--headed`), `sign_in_timeout`, `window_closed` (the window was closed before the token arrived), `idp_refused`, `blocked_request`, `not_a_jwt` (the endpoint answered with something that is not a token), `http_<status>` and `no_answer`.

**Setup, once:**

1. Set `[tdx.api] enabled = true` in your own config, then run `uv run ops-suite tdx login` (or `uv run ops-suite tdx api token --headed` if you are still signed in to TDX) and finish the second sign-in.
2. `uv run ops-suite tdx api reports` lists the ids and names of the reports you can see (names only, no rows). Pick the two that show the same tickets as the dashboard's "Unclaimed" and "Claimed by Others" widgets, and set `unclaimed_report_id` and `claimed_report_id`.
3. `uv run ops-suite tdx api check` prints counts only:

   ```text
   API: signed in as Technician.
   TDX API token: saved, expires Wed 09:14.
   Unclaimed report: 7 row(s), 10 column(s), every needed column found.
   Claimed report: 23 row(s), 11 column(s), every needed column found.
   App id: 43 (tdx.api.app_id).
   One feed: 6 entries (3 reply, 2 private, 1 system); 1 of them replies, 0 unreadable.
   API requests: 6 (GET /auth/getuser 1, GET /reports/{n} 2, GET /{n}/tickets/{n}/feed 1, GET /feed/{n} 1).
   ```

   A report that lacks a column the rules need stops the check with its name (`claimed report: required column(s) missing: Prim Resp`). The columns are found by their header text, as on the dashboard: ID, Created, Title, Requestor and Resp Group for the unclaimed report; ID, Modified, Title, Requestor, Resp Group, Status and Prim Resp for the claimed one.

**Reading the queue through the API:**

```bash
uv run ops-suite tdx rows --source api
uv run ops-suite tdx audit --source api
```

Both print the same summary lines as the browser commands, after `Source: the TDX Web API (read-only trial; the browser stays the default).`, so the `Findings:` and `Checked:` lines of the two sources can be compared by eye. With `--show-ids`, the `event_state` lines of the two sources can be compared too: the same ids should show the same state, A/V and space. `Blocked requests` and `Allowed non-GET` are always `0` here, and a report that returned as many rows as its own row limit is flagged as `Stopped at the report's row limit (MaxResults)`. Unless `tdx.source = "api"`, `tdx audit --source api` takes no `--send` and never runs the LLM step; with it, `--source api` reads through the API only, with no browser fallback (`Source: the TDX Web API only (--source api: no browser fallback).`). `--verbose` adds the `API requests:` line.

How the API's answers become the same data:

- **Feed entries:** `private` from the entry's own private flag; `system` for merges, merge notices and reassignments (their authors count as desk), the ticket's creation and "My Work" changes, and entries with no comment text besides field changes; `reply` otherwise. Replies have no private flag of their own: a reply under a private entry is private when that entry's author wrote it, as on the feed page. The desk signature is checked on each reply, and the newest reply's text is kept in memory only.
- **Dates** come in UTC and are shown in `tdx.display_timezone`, like the dashboard's.
- **Ticket links** on cards: `tdx.ticket_url_template` if set, else the TDNext ticket page.

Settings (`[tdx.api]` in [config.example.toml](../config.example.toml)): `enabled` (default `false`), `base_path` (`/TDWebApi/api`, on `tdx.base_url`'s host), `app_id` (`0` takes it from `tdx.ticket_url_template`'s `/Apps/<id>/`, else from the first ticket's record, and `tdx api check` says which), `unclaimed_report_id` and `claimed_report_id`, `timeout_seconds` (30), `min_request_interval_ms` (1100, at least 1000) and `token_sign_in_minutes` (5, from 1 to 15: how long the sign-in in a window waits for you). Unknown `[tdx.api]` keys are refused.

Exit codes: as for `tdx rows`, except that `3` here means the API's own sign-in is needed (`needs sign-in`, run `tdx api token --headed`), never an expired TDX session; plus `8` when the API refused or failed a request, or no token could be had (the message names the endpoint pattern and a reason code such as `forbidden`, `unauthorised`, `rate_limited` or `sign_in_timeout`).

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

The optional Claude API step is off by default (`llm.enabled = false`), and text from TDX or the shared mailbox also needs `llm.allow_institutional_data = true`, set only in your own config, and only once sending scrubbed ticket text to the API is approved. Every call goes through the local scrubber first, stops at a daily token budget (`llm.daily_token_budget`) and logs token counts and an estimated cost, never text.

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

## Shadow mode (browser and API side by side)

For a trial of the TDX Web API against the browser over days or weeks: each `tdx watch` cycle reads the queue through **both** sources and records how well they agree, in counts only. **The browser stays the source of every card, summary and state file**; the API's result is only compared.

**Turning it on:** set up the API first ([TDX API (proof of concept)](#tdx-api-proof-of-concept): `tdx.api.enabled`, both report ids, a token), then set `source = "shadow"` under `[tdx]` in your own config, or `OPS_SUITE_TDX__SOURCE=shadow` in `.env`, and restart `tdx watch` (or the start-at-login agent). It starts with one more line:

```text
API shadow mode: each run also reads the same tickets through the TDX Web API and records counts only; cards come from the browser. "uv run ops-suite tdx api compare" shows the comparison.
```

`tdx watch` and `tdx agent install` refuse to start (exit `2`) when `tdx.source` is `shadow` without `tdx.api.enabled` and both report ids. Set it back to `browser` (the default) to end the trial; the records stay for 30 days. The third value, `api`, swaps the roles: see [Reading through the API](#reading-through-the-api).

**What each cycle does:** after the browser's audit and its Teams post, the same cycle reads the two reports through the API, then the feeds and event forms of **the tickets the browser's audit picked**, at the browser run's time. The rules run over each source's data with the same settings and the desk-member memory read only, so the two sets of findings differ only where the data does. The API part:

- never posts, and never writes the alert, summary or verdict state or the desk-member memory;
- sends no request after `tdx.shadow.max_seconds` (default 300, 30 to 1200) and records `time_box`;
- never fails the cycle: a failure is recorded with its reason code (`time_box`, `needs_sign_in`, `unauthorised`, `rate_limited`, `not_found`, `structure`, ...) and never counts toward `failures_before_stop`. A token that needs the API's own sign-in sends one notice a day to your own chat ("TDX API token needs a sign-in: run ... tdx api token --headed").

The cycle line ends with the result:

```text
14:05 26 unclaimed, 7 feeds, 6 findings; Teams: nothing new. API shadow: findings identical. Next run 14:35.
14:35 26 unclaimed, 7 feeds, 6 findings; Teams: nothing new. API shadow: 2 differences (see tdx api compare). Next run 15:05.
15:05 26 unclaimed, 7 feeds, 6 findings; Teams: nothing new. API shadow: not compared (needs_sign_in). Next run 15:35.
```

"Differences" are findings, as (ticket id, kind, reason), that only one source raised.

**What is compared**, per cycle:

- **Rows**, per widget and report: the counts, the ticket ids in one source only, and per column the tickets whose values differ. Created and Modified match within 1 minute; Requestor and Prim Resp match as the same person (the rules' name matching); Title, Status and Resp Group match as text, ignoring case and spacing.
- **Feeds**, per ticket both sources read: entry counts by kind (reply, private, system), and the newest entry someone wrote (a reply or a private note): its kind (`private flag differs`), its author and its time (within 1 minute). Feeds that only one source read are counted too.
- **Findings:** the (ticket id, kind, reason) set of each source.
- **Timing:** seconds per source (the browser's read and rules; the API's whole part, token included), API requests, and the browser's allowed non-GET requests.

**The record:** `api-shadow.jsonl` in the TDX data folder (next to `alerts-state.json`), one line per cycle, kept 30 days. It holds the time, which source made the cards (`primary`), seconds, counts, ticket ids for the "only in one source" and "differs" lists, and reason codes: never a name, a title or any text.

**The report:**

```bash
uv run ops-suite tdx api compare --days 14
```

```text
API shadow, last 14 days: 212 cycles; 208 compared, 4 API failures (needs_sign_in 3, time_box 1).
Rows matched: 99.6% (8012 of 8044). Feeds matched: 98.8% (1450 of 1468). Cycles with identical findings: 98.6% (205 of 208).
Mismatches: Created differs: 2 (1 ticket), latest author differs: 3 (2 tickets), private flag differs: 1 (1 ticket), findings only from the browser: 2 (1 ticket), findings only from the API: 1 (1 ticket).
Median per compared cycle: browser 95.2s, API 41.0s; API requests 31; browser allowed non-GET requests 10.
Verdict: API matched the browser in 205 of 212 cycles; API reads were 54.2s faster.
```

- **Rows / Feeds matched:** the share of tickets, over all compared cycles, whose row (or feed) matched on every check; a ticket in one source only counts as not matched.
- **Mismatches:** each category with how often it was seen (once per cycle and ticket) and in how many different tickets.
- **Verdict:** the cycles whose findings were identical, out of every cycle recorded (a failed API part counts as not matched), and the difference between the median seconds.
- `--days` (1 to 30, default 14) sets how far back to look. It reads only the local record: no token, no TDX request.
- `--show-ids` adds one line per mismatch and ticket (`  latest_author  1234567  3 cycles`; a finding as `  finding_only_api  1234567  client_waiting  requestor_last  1 cycle`), for your own check in TDX. Do not paste these lines into a chat.

## Reading through the API

With `tdx.source = "api"`, the TDX Web API becomes the source: `tdx watch` and `tdx audit` read the rows, feeds and event forms through it, and every finding, card and summary, the alert and summary state, the desk-member memory and the verdict cache come from that read, exactly as they come from the browser's otherwise. **The browser stays as an automatic fallback**: any run the API cannot read, the browser reads instead, as it always has. Nothing is ever written to TDX either way, and the data flow is unchanged: the API's answers stay in memory, and the token stays in the keychain.

Switch to it only after a few days of clean [shadow mode](#shadow-mode-browser-and-api-side-by-side) comparisons (`tdx api compare`: identical findings, no `private flag differs`).

**Switching on:** with the API already set up (`tdx.api.enabled`, both report ids, a token), set `source = "api"` under `[tdx]` in your own config, or `OPS_SUITE_TDX__SOURCE=api` in `.env`, and restart `tdx watch` (or the start-at-login agent). `tdx watch` and `tdx agent install` refuse to start (exit `2`) without `tdx.api.enabled` and both report ids. It starts with:

```text
Reading the queue through the TDX Web API; the browser reads any run the API cannot (each run's line names its source).
Browser shadow: each run also reads the same tickets through the browser and records counts only; cards come from the API. "uv run ops-suite tdx api compare" shows the comparison.
```

**Switching back:** set `source = "browser"` (or remove the `.env` line) and restart. Nothing else changes: the state files are the same ones.

**The fallback.** A run falls back to the browser, with a reason code, when:

- there is no usable token: `needs_sign_in` (the API's own sign-in is needed), `no_token` and the other token codes;
- the API fails: `unauthorised`, `forbidden`, `rate_limited`, `timeout`, `connection`, `http_<status>`, `bad_json`, ...;
- a report does not look as expected: `structure` (a needed column missing) or `config` (a report id not set);
- the API part does not finish within `tdx.api.max_cycle_seconds` (default 300, 30 to 1200): no request is sent after it, `time_box`.

The browser then reads that run as usual, and the run posts once, from the browser. A fallback is **never** a failed run and never counts toward `failures_before_stop`; only the browser's own problems do, handled as always (an expired TDX session sends its notice and is retried quietly). The next run tries the API again. Each run's line starts with its source:

```text
14:05 source: api; 26 unclaimed, 7 feeds, 6 findings; Teams: nothing new. Browser shadow: findings identical. Next run 14:35.
14:35 source: browser (fallback: needs_sign_in); 26 unclaimed, 7 feeds, 6 findings; Teams: nothing new. Next run 15:05.
```

The log records `tdx watch source` with the source and the reason code. Fallbacks send at most **one notice a day** to your own chat: `Reading the queue through the browser: the TDX Web API is unavailable (timeout); each run tries the API again.` (with `run ... tdx api token --headed` for a token problem, or `run ... tdx api check` for a structure problem). For `needs_sign_in` the token notice ("TDX API token needs a sign-in: run ... tdx api token --headed") is sent instead, also once a day, never both. An expired TDX session or a browser that cannot start sends no fallback notice: the browser's own read meets the same problem and says so.

**The daily token step** is the same as in the trial: tokens last 24 hours, so run `uv run ops-suite tdx api token --headed` (or `tdx login`) once a desk day, before the first run. Until you do, runs fall back to the browser and nothing is missed.

**The browser beside the API.** While `tdx.shadow.enabled` (default `true`), each run that the API read also reads the same tickets through the browser, after the post: the dashboard, then the feeds and forms of **the tickets the API run picked**, at its time. As in shadow mode it only compares: it never posts or writes any state, and its failures are recorded with a reason code and never fail the run. It has no time box of its own; it takes about as long as a browser run. If the browser finds the TDX session expired while the API still answers, the run uses the API and the comparison is skipped (`Browser shadow: skipped (TDX sign-in expired; the API still answers).`, recorded as `browser_signed_out`): that is not an error. Sign in again with `tdx login` when convenient, since the fallback needs it. Set `tdx.shadow.enabled = false` to stop the comparison; each run is still recorded by source.

**`tdx audit`** follows `tdx.source` when no `--source` is given: with `api` it reads through the API, prints `Source: the TDX Web API (tdx.source = api); the browser reads this run if the API cannot.`, and, if the browser read it, `Read through the browser instead: the TDX Web API is unavailable (<reason>).` `--send` and the LLM step work as with the browser. `--source browser` reads through the browser only.

**The report** (`tdx api compare`) shows the shadow period and the API period each on its own, so it covers the whole trial:

```text
API shadow, last 14 days: 140 cycles; 138 compared, 2 API failures (needs_sign_in 2).
...
Verdict: API matched the browser in 138 of 140 cycles; API reads were 54.2s faster.
API primary, last 14 days: 72 cycles; 69 read through the API, 3 browser fallbacks (needs_sign_in 2, timeout 1).
Browser shadow: 66 compared, 3 skipped (browser_signed_out 3).
Rows matched: 100.0% (2772 of 2772). Feeds matched: 100.0% (462 of 462). Cycles with identical findings: 100.0% (66 of 66).
Mismatches: none.
Median per compared cycle: browser 96.0s, API 40.5s; API requests 31; browser allowed non-GET requests 10.
Verdict: the browser matched the API in 66 of 66 cycles; API reads were 55.5s faster.
```

In the API period, "read through the API" counts the runs the API read (compared or not), and the fallbacks are listed by reason; the comparison lines cover the runs where the browser was read beside it. Settings: `tdx.api.max_cycle_seconds` and `tdx.shadow.enabled`, in [config.example.toml](../config.example.toml).

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
- **Shadow mode** (`tdx.source = "shadow"`): each line also ends with the comparison of the browser and the TDX Web API, such as `API shadow: findings identical.`; see [Shadow mode](#shadow-mode-browser-and-api-side-by-side).

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

The demo card keeps its DEMO title. Like every desk card, it shows each ticket's id with its link, the finding and its age, and **no ticket title**, because titles can contain names. `teams.desk.show_titles` (default `false`) turns titles back on; leave it off unless the desk has agreed to titles in its chat.

**The approval switch.** Real ticket cards go to the desk chat only when `approved` under `[teams.desk]` is `true` in **your own** config file (never in the repository's example files). Set it only once the desk's lead has approved real ticket cards in the shared chat, in writing. While it is `false` (the default), `tdx audit --send --to desk` and `tdx watch --to desk` stop with exit `2` and one line naming `teams.desk.approved`, before the browser opens. Once it is `true` (on the Mac, under `caffeinate -i` as in [Watching the queue](#watching-the-queue-tdx-watch)):

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

**A full summary at the first slot, deltas after.** Each day's **first** summary posted is the **full** summary below, with the @mention: it replaces the manual check-in. If the first slot is missed (skipped past its grace window, or the auditor was not running), the next slot posted that day is full instead, so every day gets one full post. Later slots post a short **delta**:

```text
Since 07:30: 1 new · 1 cleared · 6 open
```

It has a row only for each finding that is new since the last summary posted (marked `(new)`) and each one due again under the [re-alert policy](tdx-rules.md#teams-alerts-tdx-audit---send-tdx-watch) (`· reminder`), grouped by kind; cleared findings are only counted. There is no "Events this week", no "Ready to close" and no mention. A delta with nothing new, nothing cleared and no reminder due is **not posted**: the terminal says `Teams (desk): 12:00 summary skipped (no change).` and nothing goes to your personal chat, because that is not a miss. `Queue clear` is only ever a full summary's heading. New and cleared are counted against the findings open at the last post: a finding whose ticket could not be judged this run (its feed failed, or a widget hit its page limit) is neither new nor cleared, and "Ready to close" findings are left out of both, as they are never `(new)`.

**What the full summary shows.** First **Events this week**, then **every** open finding of that cycle, not only the new ones, grouped by kind in the usual order with each kind's count, and each ticket's linked id, ending with **Ready to close**. The exact rules are in [tdx-rules.md](tdx-rules.md#shift-summaries). New findings are marked `(new)`. Desk summaries carry no titles while `teams.desk.show_titles` is `false` (the default); set it to `true` in the desk runner's own config only once the desk has approved titles in the shared chat. Past `tdx.alerts.max_rows_per_card` (25) rows, each kind ends with `+N more`. An empty queue still posts, so each shift knows:

```text
TDX queue at 07:30: 6 open (2 new since yesterday)
Queue clear at 07:30: nothing needs attention.
TDX queue at 08:12 (late: due 07:30): 6 open (2 new since yesterday)
```

"New" means not open at the last summary posted ("since yesterday", or `since Fri 17:00` after a gap; just `(2 new)` before the first post).

**Where alert cards go.** While summaries go to the desk chat, the desk runner posts **no alert card to the desk chat** on its 30-minute cycles: the desk gets summaries only. Those cards go to the runner's **personal** chat instead, with the personal chat's own state (`alerts-state.json`) and titles, just as a `tdx watch` to the personal chat would send them. The start line says `Alert cards go to the personal chat; the desk chat gets summaries only.` With no personal webhook saved, `tdx watch` warns once at start and sends no alert cards (`Teams: no alert card (no personal webhook).`).

```toml
[tdx.summary]
desk_alerts = false      # true: alert cards to the desk chat as well as the summaries
personal_alerts = true   # false: no alert cards at all while summaries go to the desk chat
```

With summaries off (`enabled = false`), `tdx watch --to desk` alerts the desk chat exactly as before. Every new or due finding a summary shows is recorded as alerted in the desk's own state (`alerts-state-desk.json`), so its reminders follow the re-alert policy; a due finding counted only in a `+N more` line stays due. The terminal lines are `Teams (desk): summary posted (6 open, 2 new).` and `Teams (desk): delta posted (1 new, 1 cleared, 6 open).`

**The one exception: events about to happen.** An event that still needs the desk and is `tdx.events.near_event_days` (2) days away or less should not wait for the next slot, so it goes to the desk chat **at once**, in the cycle it first appears, on one short card:

```text
Event soon: 2 need action
```

Its rows look like the summary's (the linked id, the title only with `teams.desk.show_titles`, the wording), grouped by kind; it has no mention and its preview line holds the count only. Several such findings in one cycle share one card. The kinds that count are listed in [tdx-rules.md](tdx-rules.md#urgent-event-findings): outreach due, waiting on the requester, a location still TBD or missing, and a requester's reply on an event ticket. Every other finding still waits for the summaries.

- **Once per ticket and kind.** The card's findings are remembered in the desk summary state: the next delta does not list them as new (and counts them as cleared when they clear), and the next full summary still lists them as open. The same finding is not posted again in later cycles; only one that clears and comes back is posted again.
- **No second post in a summary cycle.** When a summary is due in the same cycle, the findings go into that summary, marked `(event soon)`, instead of a card of their own.
- **Your personal chat is unchanged:** the 30-minute alert cards still go there, these findings included.
- The terminal line adds `Teams (desk): urgent event card (2).` (or `Teams (desk): urgent events in the summary (1).`). A failed post records nothing and is tried again on the next cycle.
- It applies only to the desk runner (`tdx watch --to desk`) while summaries go to the desk chat and `teams.desk.approved = true`. In shadow mode and with `tdx.source = "api"`, it posts from the cycle's own run only, never from the comparison read beside it.

It is on by default. To keep the desk chat to summaries only, turn it off in the desk runner's config and restart `tdx watch`:

```toml
[tdx.summary]
urgent_events_to_desk = false
```

**Events this week, ready to close and possible duplicates.**

- **Events this week** lists every unclaimed event ticket with a date in the next `events_days` (7, 1 to 30) days from the summary time, sorted by date: `Wed 10/21 15:00–17:00 · Lobby · A/V on form: yes · no outreach yet`. Space and A/V come from the event request form, renamed `EVENT [...]` tickets' included (`—` without a form). "A/V on form" is the form's services checklist only: A/V asked for later, in a comment or elsewhere, is not seen, so `no` does not mean "no A/V needed". The state comes from the title, the form and who wrote last on the feed: `no outreach yet`, `waiting on requester since Tue 08:26`, `requester replied`, `in progress (requester answered)`, `location missing`, `location TBD`, `marketing only`, `no outreach needed`, or `page not read` when the run could not read the ticket. Never names, and never the form's free-text answers or feed text.
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

**When.** The loop wakes at the next summary time as well as at its normal interval. A slot is **due** from its time until `grace_minutes` (90, 15 to 240) after it, inside desk hours, until it is posted. `summary-state-desk.json` in the data folder holds the last slot dealt with (posted, or skipped with no change), the last slot posted, the ticket id and finding kind of each finding open at that post (plus any posted on an "Event soon" card since), and of each finding the desk chat has been shown that has not cleared since, nothing else. So a restart in the same slot does not post again, a restart mid-day keeps counting from the last post, and an urgent event is not posted twice. A file in the older format (the last slot only) keeps its slot, and the next slot posted is full; one without the shown set starts it as its open set.

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

Every command runs from the folder you cloned the repository into (adjust the path to yours):

```bash
cd ~/tdx-queue-auditor
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

Every command runs from the folder you cloned the repository into, in PowerShell or a terminal (adjust the path to yours):

```powershell
cd $HOME\tdx-queue-auditor
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

**4. Check the new environment.** This prints the project's own interpreter, `.venv\Scripts\python.exe` inside your clone:

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

The desk needs **one** runner, a computer that is on through desk hours, for example a desk member's own always-on Windows PC. Run it only where your desk's approval covers running the tool. On that PC:

1. Follow [TDX setup](#tdx-setup-any-desk-member), [Teams setup](#teams-setup) for your **personal** webhook (sign-in and "auditor stopped" notices go there), and [Desk chat](#desk-chat) for the desk webhook.
2. In your own `config.toml`: `approved = true` and `show_titles = true` under `[teams.desk]` (each only once the desk has approved it), and `enabled = true` under `[tdx.summary]` ([Shift summaries](#shift-summaries)). The desk chat then gets **summaries only**: one full summary at the day's first slot and short deltas after, skipped when nothing changed. The 30-minute alert cards come to your **personal** chat (`tdx.summary.desk_alerts = false` and `personal_alerts = true`, the defaults).
3. Install the start-at-login script with `--to desk` ([Start at login (Windows)](#start-at-login-windows)) and keep the PC awake ([Keeping the PC awake](#keeping-the-pc-awake)).
4. **Remove any other runner.** Two runners on the desk chat post every card twice. On a Mac that ran the login agent before:

   ```bash
   uv run ops-suite tdx agent uninstall
   ```

5. Use disk encryption and a screen lock on the PC (BitLocker on Windows 11 Pro, or Device encryption; a password or PIN on wake, and Win+L when you step away), because the tool's browser profile holds a live TDX session.

If other tools on that PC can read your files (sync clients, coding assistants, backup agents), keep them out of the ops-suite data and log folder (`%LOCALAPPDATA%\ops-suite`): the browser profile there holds a live TDX session. Keep the folders at their default location, so any such rule covers them.

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
