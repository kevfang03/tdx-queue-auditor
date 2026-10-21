# Design

How the auditor is put together, and the decisions behind it. Each section states a principle and why it holds. The rules themselves are in [tdx-rules.md](tdx-rules.md), the commands in [USAGE.md](USAGE.md), and the data handling in [PRIVACY.md](PRIVACY.md).

## Architecture

```text
src/ops_suite/
  cli.py            the ops-suite command
  tdx/              the queue auditor
    browser.py, session.py, guard.py     Playwright, the persistent profile, the request guard
    dashboard.py, rows.py, feed.py       dashboard widgets -> TicketRow, activity feed -> TicketFeed
    event_form.py, events.py, week.py    event request forms, event titles, "events this week"
    rules.py                             TicketRow + TicketFeed -> findings (pure Python)
    followup.py                          the optional LLM classification of the client's message
    alerts.py, watch.py                  de-duplication, Teams cards, shift summaries, the loop
    desk_memory.py, names.py             who counts as desk staff
    capture.py, skeleton.py              structure-only page captures
    demo/                                synthetic TDX pages and the offline demo
  common/           config, paths, logging, scrubber, LLM client and budget, Teams client,
                    desk-hours schedule, start-at-login (launchd, Windows Startup folder)
  mail/             an offline scan of a shared mailbox's drop folder (separate tool)
```

A cycle runs in one direction: the browser reads the dashboard and the feeds it needs through the request guard, the rules turn rows and feeds into findings, the optional LLM step may lower the priority of one kind of finding, and the alert step decides what is new or due before posting one card. Rows and feed text live in memory for the length of a run; only ids, finding kinds and timestamps are written down.

The rules depend only on two data types, `TicketRow` (a dashboard row) and `TicketFeed` (a ticket's visible entries). That boundary is what makes a future read-only TDX API adapter a drop-in second source.

## The read-only request guard

**Principle:** the browser can read and nothing else, and every exception is visible.

Every request the page makes passes a Playwright route handler before it leaves the machine:

- Reads (`GET`, `HEAD`, `OPTIONS`) go through.
- Any other method is aborted unless it goes to the TDX host and matches an entry on a short allowlist: the method, the request type (fetch or XHR only, never a form postback) and the URL path, with only numeric ids treated as variable. The built-in entries are the dashboard widget's data load and the feed search, both reads that the web app happens to send as `POST`.
- The ticket page's live-update requests and its WebSocket are refused.
- Every allowed non-`GET` request and every blocked one is counted in the run summary, so a reviewer can check them against "one per widget load, one per page turn, one per feed".
- The only element the tool ever clicks is a widget's "next page" button.

**Why:** with no API, the tool drives the same web app a person uses, signed in as that person. A bug in a selector must not be able to change a ticket. Allowlisting known reads, rather than blocking known write endpoints, means an unknown request fails closed. New entries are added only after the blocked request has been reviewed.

## Structure-only captures, and the review rule

**Principle:** nobody building the tool needs to see real tickets.

Selectors were built from captures that keep page **structure** only: tag names, `id`, `class`, `role` and column-header text. Every other text node becomes a shape placeholder such as `[datetime]`, `[number]` or `[text:2]`, and every other attribute value is removed. Attribute values that survive are cleaned further: a value containing `@` becomes `[email]`, form-control `id` and `name` values become `[id]` and `[name]`, and any word in an `id`, `class` or `name` that also appears in the page text is masked.

The review rule: the person who takes a capture reviews it locally before anyone else sees it, and only results leave that machine (a count, a column name, "no matches"). A problem is described by its shape ("an address inside a checkbox id"), never its content. Captures are never committed; the tests run on synthetic pages built to copy their structure.

**Why:** markup carries data in unexpected places, such as people's names inside the ids of a notify-list. A capture tool that keeps only an allowlist of structure, plus a mandatory human review, catches what a denylist would miss.

## Deterministic rules first, an optional LLM step second

**Principle:** rules decide; a model may only make one finding quieter, and every failure keeps it loud.

The rules run cheapest first. The dashboard grid settles most tickets without opening anything; a feed is opened only for unclaimed tickets 48+ hours old, event request forms and events within 14 days. Only entries the client can see count, and the latest visible entry decides: client, desk or someone else. Claimed tickets are only counted, since they belong to full-time staff.

One question is beyond a rule: when the client wrote last, is the desk still expected to reply? The optional step classifies that message as `expects_reply`, `client_will_follow_up`, `no_reply_needed` or `unsure`. The two middle labels turn *client waiting* (high priority) into *follow up with client* (normal). Everything else, including `unsure`, a refusal, an API error, a closed data gate or a spent budget, leaves the finding exactly as the rules made it.

**Why:** the costly mistake is a client who never hears back, not one extra card. Making the model's only power a downgrade, limited to a four-value schema, means a wrong or manipulated answer can at worst show "check in" instead of "reply". It can never trigger an action, a message or a write. The step is off by default, so the product is complete without it.

## The scrubber, enforced by type; the data gate; the budget

**Principle:** the safe path is the only path the code allows.

- **By type:** the LLM client accepts only a `ScrubbedText`, which only the scrubber can construct. "Scrub before every call" is checked by the type system, not left to each caller.
- **The scrubber:** quoted earlier mail and everything from a sign-off onward are dropped first, and the text is capped in length. Then names known to the run (the requester, every author in the feed, the desk staff, greeting names) become `[NAME]`, and emails, phone numbers, ID numbers, usernames and URLs are replaced. If any word of a known name survives, the call is refused.
- **The data gate:** every text carries a data class set by the run mode, never by config: live runs are `institutional`, and the demo and the eval are `synthetic`. Live text reaches the API only when two separate switches are on in the person's own config; both default to off.
- **The budget:** a daily token budget is checked before every call and hard-stops at the limit. Every call logs the model, token counts, an estimated cost and the ticket id, never text. Each client message is classified once (a cache of ids and timestamps), and the fixed system prompt uses prompt caching.

**Why:** ticket text can hold student records. Making the unsafe call impossible to write is stronger than reviewing every call site, and the budget turns a runaway loop into a bounded cost.

## Stateful de-duplication, and shift summaries

**Principle:** alert on state, not on events, and say each thing once.

The alert state is a small file of (ticket id, finding kind) pairs with timestamps. A finding alerts once, again every 3 days while it stays open, and weekly once it is 30+ days old. A cleared finding is forgotten, so a return alerts as new. A failed feed read or a truncated dashboard read never clears a record, and the state is written only after Teams accepted the card.

A shared desk chat gets **summaries only**. The day's first slot is the full summary: every open finding grouped by kind, the events this week and the tickets ready to close. Later slots are deltas ("Since 07:30: 2 new · 1 cleared · 11 open") with rows only for new findings and due reminders, and they are skipped when nothing changed. A missed first slot makes the next posted slot full. The 30-minute alert cards go to the runner's own chat instead.

**Why:** the existing alerts fire once, when something happens; silence is exactly what they cannot see. Re-checking the whole queue every cycle catches silence, and the state file keeps that from turning into the same card every 30 minutes. In a shared chat, one full picture per shift plus short deltas is useful; a card every half hour is noise.

## The desk memory, pseudonymised

**Principle:** remember who is staff without storing who they are.

To tell a desk reply from an outsider's, the rules need to know who is on the desk. Within a run, that comes from strong signals (below). Across runs, staff seen through a strong signal are remembered in a local file as **salted SHA-256 hashes** of their normalised names, never names, and forgotten after 180 days unseen. The weak signal, a person who appears on two or more tickets in one run, is used for that run only and never remembered, so a frequently copied outsider cannot become "staff".

**The caveat:** this is pseudonymisation, not anonymisation. The salt sits beside the hashes, so anyone holding the file and a staff list could test names. That is acceptable only because the file stays on the person's own computer, and the docs say so rather than calling it anonymous.

## Strong signals and the desk signature

**Principle:** only evidence that a person outside the desk could not produce marks someone as staff.

The strong signals are: the primary-responsible name on a claimed ticket, the author of a private note (only staff can write one), the author of a merge notice, and the author of a visible reply whose **own** text contains a configured desk signature. The signature check is a substring test after quoted mail is cut, so a client quoting the desk does not qualify; the text is never kept, entries carried over from a merged ticket do not count, and the requester never becomes staff this way. A per-person `extra_staff_names` list remains as an override.

A ticket opened by a strong-signal staff member counts as internal and raises no *client waiting* finding; the weak signal is not enough, so a frequent client cannot be hidden.

**Why:** student staff rotate every term, so any hand-kept name list goes stale. Every desk reply carries the team signature, which makes it a signal that maintains itself.

## Event state from the form and the feed

**Principle:** a ticket's title says what the event is; only the conversation says how far along it is.

Event requests arrive through one web form. Its answers sit in the ticket description behind a block of submission metadata, which is cut off before anything is parsed and never kept. Six answers are read in memory (dates, times, the recurring answer, what the requester is looking for, the type of space, the services), and what remains is a value of dates, yes/no flags and one count, with no text at all.

Every unclaimed event within 14 days has its form and feed read, renamed or not, and its state comes from who wrote last:

- no desk reply yet, and the event needs A/V or a room: *event outreach due*;
- the desk asked and the requester has not answered: *event details pending*, high priority within 2 days of the event;
- the requester wrote last, 48+ hours ago: *client waiting*, high and immediate within 2 days;
- the requester has answered and the desk wrote last: **in progress, no finding**.

A ticket gets one row: when outreach is due and the requester also wrote, the outreach finding carries both. Titles add *event over* (from the **last** of several dates) and *location TBD soon* (from the **next** date), including a renamed title with no location at all.

**Why:** the desk renames event tickets early, so a rename says nothing about completeness. "In progress" stays quiet because, without the desk's events spreadsheet, the tool cannot tell a final confirmation from an open question, and alerting on it would flood the last week before every event.

## TLS through the operating system's trust store

**Principle:** never turn verification off to make a connection work.

The Teams and Claude clients verify certificates against the operating system's trust store through `truststore`, scoped to those clients rather than injected globally. Hostname checks stay on. Every new HTTPS client must do the same.

**Why:** uv's managed Python on macOS ships without a CA bundle, so default verification fails there. The OS store is what the machine already trusts, including any certificate an institution installs.

## Sign-in recovery without getting around MFA

**Principle:** the tool never handles credentials; it waits for a person.

The person signs in once, MFA included, in the tool's own persistent browser profile, which is separate from their everyday browser and never leaves the machine. When the session expires, the loop sends one plain "sign in again" notice to the runner's own chat, retries quietly every 10 minutes inside desk hours, sends the notice again on a new day, and resumes by itself after `tdx login`. A busy profile (a sign-in window still open) is not a failure; after three real failures in a row it sends "auditor stopped" and exits. It logs only when each sign-in happened and when the first expiry was seen, to learn the session's real lifetime.

**Why:** automating a sign-in would mean storing a password and defeating MFA. A notice and a patient retry make expiry a 30-second task for the person, with no secret in the tool.

## The cross-platform runner

**Principle:** one program, scheduled from inside itself, on any OS.

- **Scheduling is in-process:** desk hours are one window per weekday in config, and the loop also wakes at shift-summary times. No cron and no Task Scheduler.
- **Start at login** only starts that same process: a launchd user agent on macOS (`RunAtLoad` on, `KeepAlive` off so "auditor stopped" stays stopped, under `caffeinate -i`), and a script in the Windows Startup folder. On Windows the loop itself asks the OS not to sleep while it runs.
- **uv's own Python on every OS:** the Microsoft Store Python silently redirects writes under the user's local app-data folder into a private copy, so the tool's data and logs would land somewhere other tools cannot see. The project pins uv-managed Python, and live commands refuse to run on a Store Python.
- **Paths and secrets:** folders come from `platformdirs`; webhook URLs and the API key live in the OS keychain through `keyring`.
- **Tests:** CI runs on Linux, Windows and macOS. Code for one OS is tested with that OS's paths injected (for example `PureWindowsPath`), so those tests run everywhere.

**Why:** the desk's machines are a mix of Macs and Windows PCs, and each person runs the tool on their own computer. Keeping the schedule inside the program means one behaviour to test, and the OS only has to start it.
