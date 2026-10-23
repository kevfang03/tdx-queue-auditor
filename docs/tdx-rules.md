# TDX stale rules, in plain language

The queue auditor (Module 3) turns the two dashboard widgets and some ticket feeds into **findings**: tickets the desk should look at, and why. The code is `src/ops_suite/tdx/rules.py`; this page says what it does.

All ages are in **real hours**, weekends included, because the desk is open seven days a week. A run that crosses a daylight-saving change counts the hour that the clock skipped or repeated. "Stale" means 48 hours or more (`stale_hours`).

## Whose tickets

Student assistants never claim tickets: a ticket stays **unclaimed** while the desk works on it. Only special tickets get claimed, by a full-timer, and students must not act on those. So:

- **The auditor checks the Unclaimed widget only**, the desk's working queue.
- **Claimed tickets** ("Active Tickets Claimed by Others") are counted, and their Prim Resp names tell the auditor who is on the desk. Nothing else: no finding, no event rules, no feed.

## Cheapest first

1. The grid settles many tickets. An unclaimed ticket created in the last 48 hours is not stale, and its feed is never opened.
2. Only unclaimed, non-event tickets created 48+ hours ago (or with no Created time) have their feed opened, plus every unclaimed **event request form** whatever its age, and every unclaimed, non-recurring **renamed event** whose next date is 14 days away or less (`outreach_days`; below). `plan_feeds` lists them, oldest Created first, so a run cut short still reads the oldest ones; all of them count toward `max_feeds_per_run`.
3. Renamed event tickets further out, and past ones, need no page: their title says everything the rules use for them (`event_over`, TBD).
4. The rules use no LLM. The one optional LLM step, the [follow-up classifier](#the-follow-up-classifier), runs after them on a single kind of finding.

## The rules

| Input | Condition | Finding | Priority | Reason code |
|---|---|---|---|---|
| Claimed row | Always | none (counted `rows_claimed`; Prim Resp marks a desk member) | | |
| Unclaimed row | Created under 48 hours ago | none (counted `unclaimed_recent`) | | |
| Unclaimed row | Created unreadable | counted `unclaimed_created_unreadable`, and its feed is opened | | |
| Unclaimed, feed needed | Feed not read | none (counted `feed_missing`) | | |
| Unclaimed, feed needed | No entry the requestor can see | client waiting, aged from Created (none when a desk member opened the ticket, below) | high | `no_reply_yet` |
| Unclaimed, feed needed | Latest visible entry by the requestor, 48+ hours old | client waiting (the follow-up classifier may change it, below; none when a desk member opened the ticket) | high | `requestor_last` |
| Unclaimed, feed needed | Latest visible entry by the requestor, under 48 hours | none (counted `client_recent`) | | |
| Unclaimed, feed needed | Latest visible entry by the requestor, no date | needs review | normal | `requestor_last_undated` |
| Unclaimed, feed needed | Latest visible entry by a desk member (known in this run, or remembered), under 3 days old (`close_after_days`), or undated | none (counted `desk_replied_last`) | | |
| Unclaimed, feed needed | Latest visible entry by a desk member, 3+ days old (`close_after_days`), and the requester is not a desk member (below) | ready to close: summaries only by default (see "Ready to close" below) | low | `desk_replied_last` |
| Unclaimed, feed needed | Latest visible entry by anyone else, 48+ hours old | needs review | normal | `other_last` |
| Unclaimed, feed needed | Latest visible entry by anyone else, under 48 hours | none (counted `other_recent`) | | |
| Unclaimed, feed needed | Latest visible entry by anyone else, no date | needs review | normal | `other_last` |
| Unclaimed event title | A recurring event (weekly, daily, ...) | none (counted `event_recurring`) | | |
| Unclaimed event title | Date read, and the **last** date more than 24 hours past (`event_over_hours`) | event over | low | `event_over` |
| Unclaimed event title | TBD in the title, and the **next upcoming** date 0 to 3 days away (`event_tbd_days`); skipped when every date is past | location TBD soon | normal | `tbd_soon` |
| Unclaimed event title | **No location** in its leading bracket (below), and the next upcoming date 0 to 3 days away | location TBD soon (same wording) | normal | `location_missing` |
| Unclaimed event title | Next upcoming date 14 days away or less | the form and the feed are read too: "Event state" below | | |
| Unclaimed event title | No date could be read | none (counted `event_date_unreadable`, id listed) | | |

A renamed event further out than 14 days, or past, never gets its page read. Within 14 days it gets the event state rules below; "client waiting" comes only from those (`event_requester_replied`). It can give both event findings if it is over and still says TBD. A title with one date has one date that is both its "last" and its "next upcoming" date, so the two rows read as before. Event tickets with 2 or more dates are counted `event_multi_date`. Being unclaimed for 48 hours is not a finding on its own: the desk often works a ticket without claiming it.

### Words used above

- **Event title:** starts with the word "event" (any capitalisation) followed by a space, bracket, colon or dash. "CCI Event Request Form" is not an event title: it has not been handled yet and follows the event request rules below. Neither is "Eventbrite login issue".
- **Recurring event:** an event title with the whole word weekly, bi-weekly, biweekly, bi weekly, daily, monthly, recurring or every after "EVENT", in any case. It has no single date to check, so it is skipped and counted, not reported as an unreadable date.
- **Event date:** a date after "EVENT", read leniently: `10/15`, `10/15/26`, `10-15`, `10.15`, `Oct 15`, `October 15th`, `Oct. 15, 2026`, `15 Oct`, with or without brackets or a weekday. The time after it (`2pm`, `2:30 PM`, `14:00`, `noon`; a range gives its start) makes it exact; with no time the event counts as ending at 23:59 that day. With no year, the year that puts the date closest to the run is used, so a run on Dec 30 reads `1/2` as next January.
- **Date list:** an event title can list several dates, and every one is read:
  - if a `[...]` or `(...)` group comes right after "EVENT", every date inside it;
  - otherwise (or when that group holds no date), the run of dates starting at the first date, separated only by commas, `&`, `and`, `-`, `–`, `to`, `/`, `;` or spaces, and ending at the first other word. Dates in the event name are never read.
  - A month-name day range, `Oct 20-22` or `October 20 – 22`, is two dates: the 20th and the 22nd. A numeric `10/20-22` is not guessed and stays the single date 10/20; `10/20 - 10/22` is two dates.
  - Each date takes the first time after it and before the next date. A date without one takes the time after the last date, as in `10/20, 10/27 3pm`. With no time at all, each date ends at 23:59.
  - Each year-less date gets its own year (closest to the run); the list is then sorted and duplicates removed.
- **TBD:** the word TBD, any capitalisation, anywhere in the title.
- **No location**: a leading `[...]` or `(...)` group that holds a date **and** a time and nothing else once weekdays and joining words (at, from, to, and, on, until, EST/EDT/ET) are set aside, as in `EVENT [10/21/26 6PM-7PM] Resume Review`. Any other word or number left (`Room 101`, `Zoom`, `3675 Market`) is taken as the location. A title with TBD is TBD, not this; a bracket with a date but no time, or a title with no leading bracket, is never read as missing a location, since where the room would be cannot be told. Counted `event_location_missing`.
- **Visible entry:** a reply or comment the requestor can see. Private notes and status-change-only entries are skipped, so a private note after the client's message still leaves the client waiting.
- **Same person:** names match ignoring case, punctuation and order ("Example, Alex" is "Alex Example"), and a middle name on one side only still matches. There is no fuzzy matching: a typo is someone else.
- **Desk member:** built once per run, before any ticket is judged, from:
  - every Prim Resp name in the claimed widget;
  - everyone who wrote a private note in any feed read in the run (only staff can);
  - whoever wrote a merge notice (or another entry the feed reader marks as staff-only);
  - whoever wrote a visible reply whose **own** text contains the desk signature (see "The desk signature" below);
  - everyone other than the ticket's requestor who appears in the feeds of **2 or more different tickets** in the run (a student assistant replies on many tickets; a CC'd colleague usually on one);
  - the optional local list `extra_staff_names` (an override, normally empty);
  - anyone in the **desk-member memory** (below).

  The requestor is checked first, so a staff member who opened their own ticket is treated as the requestor. The run summary prints how many desk members it knew in the run; the memory is not part of that count.

## The desk-member memory

A student assistant who replies on one ticket in a run, writes no private note and is not Prim Resp anywhere looks like "anyone else". So the auditor remembers desk members across runs (without it, a live `tdx watch` run flagged a desk reply an hour old):

- **What is remembered:** only names seen through a **strong** signal: Prim Resp of a claimed ticket, the author of a private note, the author of a merge notice, or the author of a reply signed with the desk signature. From then on, one reply is enough for that person to count as desk.
- **What is not:** the "2 or more tickets in one run" guess, so a colleague who is often CC'd cannot become desk for good. `extra_staff_names` is not copied into the memory either; it stays in your config.
- **How:** in `tdx/desk-members.json` in the data folder (see the README), next to the alert state. Each person is stored as a salted SHA-256 hash of their normalised name ("Example, Alex" and "Alex Example" give the same hash), with the date last seen. No name is ever written. The salt is random per file, so the hashes mean nothing on another computer.
- **Forgetting:** a member not seen for 180 days (`tdx.rules.desk_memory_forget_days`, 30 to 730) is dropped. Delete the file to reset it; a corrupt file is moved aside to `desk-members.json.bad` and the memory starts empty.
- **Switch:** `tdx.rules.desk_memory = false` turns it off: no file is read or written, and only the in-run rules apply. `tdx audit --verbose` prints `Desk members remembered: N.` when it is on.
- **Override:** `tdx.rules.extra_staff_names` is optional and normally empty. Normally not needed: desk members are learned from Prim Resp, private notes, merge notices and the desk signature in their replies. If someone on the desk is still flagged, add their name there in your own config (never in the repository).

## The desk signature

Student assistants rotate every quarter, and some only ever write public replies, so a hand-kept name list goes stale. Every desk reply ends with the team signature, so it is one more strong signal:

- **The rule:** a visible reply whose own text contains one of `tdx.rules.desk_signatures` (default "SCIS Commons Support" and "CCI Commons Support"), ignoring case and extra spaces, makes its author a desk member at once. The author is remembered in the desk-member memory like the other strong signals.
- **Own text only:** quoted mail is cut first (an "On ... wrote:" line, an Outlook `From:`/`Sent:` block, lines starting with `>`, and quoted history blocks), so a client quoting a desk reply does not qualify. The sign-off is kept, because the signature comes after it.
- **Never counted:** anything inside a merge notice (its carried-over thread and replies under it), private notes (already a strong signal) and the ticket's requester, even with a signature.
- **No text is kept:** the reader only tests the text and keeps the answer. `tdx audit --verbose` shows how many replies matched, as "signature desk replies" in the `Entry markers:` line.
- **Limit:** an entry carried over from a merged ticket as its own feed entry looks like any other entry on the page, so it is judged like one: a carried-over desk reply with the signature still marks its (desk) author.

The 48-hour gate on `other_last` works with or without the memory: someone just acted on the ticket, so it is not stale yet.

## Tickets a desk member opened

A manager sometimes opens a ticket as a reminder or notice for the queue. Nobody is waiting for a reply on it, so "client waiting" there is wrong (a live check flagged one):

- **The rule:** when the requestor of an unclaimed, non-event ticket is a desk member through a **strong** signal, the ticket raises no `client_waiting` finding, neither `no_reply_yet` nor `requestor_last`. It is counted `internal_requestor` instead.
- **Strong signals only:** a Prim Resp name of a claimed row, a private-note, merge-notice or desk-signature author in this run, the desk-member memory, or `extra_staff_names`. The "2 or more tickets in one run" guess does **not** count, so a client who writes on many tickets is never hidden.
- **Everything else stays:** a requestor reply under 48 hours is still `client_recent`; an undated one is still `needs_review`; `other_last`, `desk_replied_last`, events and recent tickets are unchanged.
- **Switch:** `tdx.rules.skip_desk_requestors = false` brings the findings back.
- **Still flagged?** A manager who is never Prim Resp and never writes a private note has no strong signal. Add that person to `tdx.rules.extra_staff_names` in your own config (never in the repository).

## Event requests

Event requests come from one standard web form and arrive titled "CCI Event Request Form". The college is now the SCIS (formerly CCI), so "SCIS Event Request Form" matches too until the form follows (`tdx.events.form_titles`; `known_option_prefixes` likewise lists both "CCI Operational Assistance" and "SCIS Operational Assistance"). For an event that needs A/V or a room, a student assistant reaches out about two weeks before. The requester books the room with the room scheduler, and the desk asks for the room number so it can prepare A/V. If the event is hybrid or remote, the requester creates the Zoom meeting, sends the link, adds the desk's shared mailbox as a Zoom alternative host (so a student assistant can start and record the meeting on the room's computer) and says whether to record. The form has no hybrid or remote question, so the outreach row lists the Zoom asks for every event, and names the mailbox to add (`tdx.events.alt_host_mailbox`, or "our shared mailbox" when it is not set) so the sender can copy it. The event then goes on the events spreadsheet. The desk renames the ticket `EVENT [...]` early, at first handling, so a rename says nothing about whether the details are in (found on the first live summary): within 14 days of the event, a renamed ticket's form and feed are read like a form ticket's. Marketing-only requests get no outreach, and every event ticket is resolved after the event. The code is `tdx/event_form.py` and `evaluate`. The tool only reminds: no drafted replies, no writes to TDX, nothing on the spreadsheet.

**Which tickets:** unclaimed tickets whose title is exactly a form title, ignoring case and spacing: their page is opened whatever their age. And unclaimed, non-recurring renamed `EVENT [...]` tickets whose next title date is 14 days away or less (`outreach_days`). For both, the description and the feed are read, under the same guard and pacing, within `max_feeds_per_run`. Claimed ones are only counted. A renamed ticket keeps its **title's** dates (the desk's own); its form gives only the space, A/V and marketing-only answers. A renamed ticket without a form block (made by hand) has **unknown** services: "—" in the summary, counted `event_form_missing` (listed by id under `--show-ids`), no outreach finding, but the feed state below still applies.

**What is read from the form:** the description holds a `Recipient Data:` block (submission metadata) and then a `Response Summary:` block. The description is read **in full**, from its HTML: the browser reader takes the description container from the page's HTML, hidden parts included, and the Web API reader takes the stored description; both turn it into text the same way (one line per block or line break, `&nbsp;` as a space). The page's rendered text is not used, because a long description shown as a shortened preview behind a "show more" toggle would lose its later answers, the space and the services among them, and the request would look like it needs no outreach. Nothing is clicked to expand it. The page may hold more than one copy of the form: the preview, and the full text, hidden next to it or kept as source text in a text box, template, attribute or script. The browser reader reads every copy that holds "Response Summary". Only **Response Summary blocks** are parsed: each runs from "Response Summary:" to the next "Recipient Data:" or "Response Summary:", so the metadata is never parsed. The block with the most of the six labels below is used, then one without leftover HTML tags, then the longest (a full copy beats a preview cut short), then the first. Only six answers are read, in memory: the recurring answer, the date(s), the start/end times, "I am looking:", the type of space and "Please select all that apply:". Dates and times are read like event titles (above; `2026-10-28` works too), the start time of a range is the event time, and with no time the event counts as ending at 23:59. The options match by **case-insensitive prefix** (`tdx.events`):

- **marketing only:** "I am looking:" starts with `Marketing/advertising`;
- **wants A/V:** the services include `A/V & Technical Support`;
- **wants a room:** the space starts with `Classroom Space`;
- an option that matches none of these nor the form's other options (`known_option_prefixes`) is counted, nothing more.

Outreach applies when the request is not marketing-only and wants A/V or a room. A lobby event without A/V gets none: an assumption still to confirm with the desk; adding `Lobby Area` to `tdx.events.room_space_prefixes` changes it.

| Form ticket | Condition | Finding | Priority | Reason code |
|---|---|---|---|---|
| Page not read | | none (counted `feed_missing`) | | |
| No form, or no date in it | | none (counted `event_forms_unreadable`, id listed) | | |
| Recurring "Yes" | | none (counted `event_forms_recurring`) | | |
| Any services | The **last** date more than 24 hours past | event over | low | `event_over` |
| Upcoming | | the event state rules below | | |

Between the event's start and 24 hours after it, nothing is raised.

### Event state

For every upcoming event whose page was read, a form ticket or a renamed one within 14 days, the state comes from **who wrote last** among the visible entries (`event_status`; private notes and status changes are skipped). The *first outreach* is the oldest visible desk reply. "The requester" here is anyone who is not desk staff (the requestor first, as everywhere; an on-site contact answering for the event counts as the requester's side).

**First, on every such event, whatever its services** (marketing only and no outreach included) and whether or not the desk has replied yet: when the requester wrote the latest visible entry, the desk owes a reply, as on any ticket. A requester's follow-up question that the desk never answered must not go silent.

| Services | Latest visible entry | Condition | Finding | Priority | Reason code |
|---|---|---|---|---|---|
| any | by the requester | the next date 2 days away or less (`near_event_days`): at once | client waiting | **high** | `event_requester_replied` |
| any | by the requester | the entry 48+ hours old (`stale_hours`) | client waiting | normal | `event_requester_replied` |
| any | by the requester | otherwise | none (counted `client_recent` when the desk has replied before) | | |

Then the rules below, which may add an event finding of their own. **One row per ticket:** when "event outreach due" and this "client waiting" would both fire (no desk reply yet, outreach due, and a reply owed), only "event outreach due" is raised, because the outreach email answers the requester too. Its reason becomes `outreach_due_requester_wrote`, its wording says "the requester wrote {age} ago" before the checklist, and its priority is high if either finding would have been high. A requester entry with no date near the event raises the plain outreach row at high priority.

| Services | Feed state | Condition | Finding | Priority | Reason code |
|---|---|---|---|---|---|
| Marketing only | any | | none (counted `event_marketing_only`) | | |
| No A/V and no classroom | any | | none (counted `event_no_outreach`) | | |
| A/V or classroom | **not emailed yet**: no visible desk reply | the next date 14 days away or less (`outreach_days`) | event outreach due | normal; high at 3 days or less (`urgent_days`) | `outreach_due` |
| A/V or classroom | not emailed yet | further out | none (counted `event_outreach_not_due`) | | |
| Unknown (no form) | not emailed yet | | none (counted `event_form_missing`) | | |
| A/V, classroom or unknown | **waiting on the requester**: the desk wrote last, and nothing from the requester since the first outreach | the next date 2 days away or less (`near_event_days`): the **near-event override**, at once whatever the quiet time | event details pending | **high** | `details_pending_near` |
| A/V, classroom or unknown | waiting on the requester | the desk's last entry 48+ hours old and the next date 7 days away or less (`details_pending_days`) | event details pending | normal | `details_pending` |
| A/V, classroom or unknown | waiting on the requester | otherwise | none (counted `event_outreach_done`) | | |
| A/V, classroom or unknown | **requester replied**: the requester wrote last, after a desk reply | | judged by the first table | | |
| A/V, classroom or unknown | **in progress (requester answered)**: the desk wrote last, and the requester has written at least once since the first outreach | | **none** (counted `event_in_progress`) | | |

The rows are tried in this order. The requester writing last is the one case where an event ticket raises "client waiting": the desk owes a reply. Its reason is not `requestor_last`, so the follow-up classifier never runs on it, and an event ticket never gives `no_reply_yet` or `needs_review`. A requester who is a desk member (the internal-ticket rule above) raises nothing and is counted `internal_requestor`. "Desk reply" uses the same desk-member test as everywhere else. Before the 7-day window the desk waits on purpose after asking: the requester is often waiting on the room confirmation themselves. `event outreach due` is aged by the hours until the event; `event details pending` and the event's `client waiting` by the hours since the last entry, and their cards also show the time until the event.

**Known limit: renamed tickets further out.** A renamed `EVENT [...]` ticket whose next date is more than 14 days away is not opened, so a requester's unanswered question on it is not seen until the event comes within 14 days. Form-titled tickets are opened at any age, so they have no such gap.

**Why "in progress" is quiet, and the limit it shows.** Without the desk's events spreadsheet, the tool cannot tell a finished event from an unfinished one: once the requester has answered, the desk's last message is often the final confirmation. Alerting on it would flood the final week of every event. So a ticket where the requester has answered and the desk wrote last stays quiet, even if a detail is still missing; the shift summary's "Events this week" still lists it as `in progress (requester answered)`. The spreadsheet integration is in the backlog.

**Worked examples** (run time Thursday 2026-10-08 12:00):

- Ticket 1020, created yesterday, asks for A/V for an event on 10/18 at 14:00, 242 hours away. Nobody from the desk has replied: `event_outreach_due`, normal, "Event in 10 days: email the requester for the room number. If it's hybrid or remote, ..." On 10/15 from 14:00 it becomes high.
- Ticket 1021 asks for a classroom on 10/13 at 15:00, 123 hours away. Pat Helpdesk asked for the details on Monday at 12:00, 72 hours ago, and the requester has not answered: `event_details_pending`, normal, "Event in 5 days; no reply to the help desk for 3 days: follow up". Had the event been on 10/20, there would be nothing yet (counted `event_outreach_done`). Had it been tomorrow, the near-event override would raise it at once, high: "Event in 27 hours: still waiting on the requester (asked 3 days ago): follow up now".
- Ticket 1024 was renamed `EVENT [10/9 2pm Room 101] Example Talk` when Pat first asked for the details, 20 hours ago; its form asks for A/V. The requester answered an hour ago: `client_waiting`, high (the event is 26 hours away), "Event in 26 hours. The requester replied 1 hour ago: read it and confirm the details". Once Pat confirms, the ticket is `in progress (requester answered)`: no finding.
- Ticket 1025 is `EVENT [10/9/26 6PM-7PM] Resume Review`: a date and a time and no room. The event is 30 hours away: `location_tbd_soon`, reason `location_missing`, with the TBD wording.
- Ticket 1022 is marketing only, for 10/12 with no time, so the event counts as ending at 23:59: no finding (counted `event_marketing_only`). From 10/14 00:00, more than 24 hours later, it gives `event_over`, like every event ticket.

## Worked examples

Every ticket below is invented. The run time is **Thursday 2026-10-08 12:00** (New York).

**Requester waiting** (`client_waiting`). Ticket 1001, unclaimed, was created Monday 2026-10-05 at 08:00, 76 hours ago, so its feed is opened. Newest first, the feed holds a private note by Pat Helpdesk (Wednesday), a status change (Wednesday), and a reply by "Example, Alex" (Monday 11:00). The note and the status change are not visible to the client, so the latest visible entry is Alex's reply. Alex Example is the requestor, and the reply is 73 hours old: finding `client_waiting`, reason `requestor_last`, 73 hours, high priority.

If the feed had no visible entry at all, the finding would be `client_waiting`, reason `no_reply_yet`, aged from the Created time (76 hours).

**The desk is on it.** Ticket 1002, unclaimed, created 90 hours ago. Its latest visible entry is a reply from Robin Tier, 20 hours ago. Robin is not in the Prim Resp column, but replied on another ticket in this run too (or wrote a private note somewhere), so Robin counts as desk: no finding, counted `desk_replied_last`. Nobody had to claim the ticket.

**Claimed tickets.** Ticket 2001 is claimed by a full-timer and was last modified 300 days ago. It is counted, nothing more: its feed is not opened and it raises nothing, because students do not act on claimed tickets.

**Event over.** Ticket 1006, unclaimed, is titled "EVENT [10/6 2pm Room 101] Example Talk". The event started Tuesday at 14:00, 46 hours ago, more than 24: finding `event_over`, low priority, 46 hours. Its feed is never opened, and it can never be "client waiting".

**Event with several dates.** Ticket 1010, unclaimed, is titled "EVENT [10/1/26, 10/20/26, 10/27/26 3:00 PM - 5:00 PM Room 101] Example Workshop". All three dates share the time after the last one: Oct 1, 20 and 27, each at 15:00. The first was a week ago, but the **last** date is Oct 27, still ahead, so the event is not over: no finding, counted `event_multi_date`. Once Oct 27 15:00 is more than 24 hours past, it gives `event_over`, aged from Oct 27. Had the title said TBD, "location TBD soon" would count down to Oct 20, the next upcoming date, and fire from Oct 17 15:00.

**Location still TBD, event soon.** Ticket 1003, unclaimed, is titled "EVENT [10/10 2pm TBD] Example Talk". The event is Saturday at 14:00, 50 hours away, within 3 days, and the location is still TBD: finding `location_tbd_soon`, reason `tbd_soon`, 50 hours.

**Recurring event.** Ticket 1009 is titled "EVENT [Bi-Weekly, Mon/Wed 2-3pm] Example Club". It has no single date: counted `event_recurring`, no finding.

**Needs review.** Ticket 1004, unclaimed, created 80 hours ago. Its latest visible entry is a reply from Casey Testperson, 75 hours ago, who is not the requestor, appears on no other ticket in the run, wrote no private note, and is not in `extra_staff_names` (perhaps a CC'd colleague). The rules cannot tell who is holding things up: finding `needs_review`, reason `other_last`, 75 hours. These are only reported, for a person to look at; the follow-up classifier never sees them. The card row says what to check: "The last message, 3 days ago, came from someone other than the requester or the help desk: check whether the requester still needs a reply". The writer is usually a CC'd person (an instructor, a department contact) or a desk member the rules do not recognise yet, so the question is whether the requester is still owed an answer. Had Casey's reply been 3 hours old, there would be no finding (counted `other_recent`): someone just acted on the ticket.

## The run summary

`format_audit_summary` gives two lines (`tdx audit` adds the `LLM:` line between them; see "The follow-up classifier" below):

```
Findings: client waiting 1, follow up with client 0, event outreach due 0, event details pending 0, event over 1, location TBD soon 1, needs review 1, ready to close 0.
Checked: 10 unclaimed (6 feeds needed, 3 missing), 2 claimed (counted only). Events: 5, 1 recurring, 1 multi-date, 1 with an unreadable date, 0 without a location. Form-titled tickets: 0 (0 recurring, 0 unreadable); renamed: 0 with a form, 0 without. Event states (no finding): 0 waiting on requester, 0 in progress, 0 outreach not due yet, 0 marketing only, 0 no outreach. Desk members known: 1.
```

When a desk member opened some tickets (above), the `Checked:` line adds `Internal (desk requester): N.` after the claimed count; with none, the line is as shown. `Event states (no finding)` counts the upcoming events whose page was read and that raised nothing, by state: waiting on the requester before the details-pending window, in progress, not emailed yet before the outreach window, marketing only and no email needed.

With `show_ids`, one line per finding (most urgent first: priority, then longest waiting), one per event with an unreadable date, so the date reader can be improved, one per unreadable event request form, and one per renamed event without a form:

```
  client_waiting  1001  requestor_last  73h
  needs_review  1004  other_last  75h
  event_date_unreadable  1008
  event_form_unreadable  1023
  event_form_missing  1026
```

Only ticket ids, reason codes and hours appear. Titles, names and links are student data (FERPA) and never appear in a summary, log, `repr()` or error from these modules.

## Ready to close

The manual weekly check-in had a "close these if nothing new" list. The `ready_to_close` finding is that list:

- an unclaimed ticket that is not an event, not an event request form, and not opened by a desk member (the internal-ticket rule above);
- its latest visible entry is a **desk** reply (so the requester has written nothing since; a private note after it changes nothing);
- that reply is `tdx.rules.close_after_days` (3, 1 to 30) days old or more.

The age shown is the time since the desk reply. A desk reply younger than that, or undated, is still only counted `desk_replied_last`. Wording: "{Team} replied {age} ago, no reply since: close if nothing new" ([Card wording](#card-wording)).

It is a **summary-only** finding by default: it is listed in the "Ready to close" section of every shift summary while it is open, never marked `(new)`, and kept out of the alert cards and `alerts-state.json`, so it never pings on its own. `tdx.alerts.alert_ready_to_close = true` treats it like any other finding (alert cards, re-alerts, `(new)`). The auditor never closes anything: TDX stays read-only.

## Teams alerts (tdx audit --send, tdx watch)

Findings become one Adaptive Card per run, posted to the own Teams chat of the person running the tool (or to the desk chat, see [USAGE.md](USAGE.md#desk-chat)). The card shows only findings that are **new or due again**, grouped by kind in this order: requester waiting, follow up with requester, needs review, event outreach due, event details pending, location TBD soon, event over, ready to close (only with `alert_ready_to_close`). Rows are ordered by priority, then kind, then oldest first. At most `tdx.alerts.max_rows_per_card` (25) fit on a card; the rest wait for the next one, and the card says how many ("12 more waiting; they come on the next cards.").

### Re-alert policy

- A finding is identified by its ticket id and kind. The first time it is seen, it is sent as **new**.
- While it stays open it is sent again as a **reminder** every `realert_days` (3), or every `old_realert_days` (7) once its age is `old_finding_days` (30) days or more. A finding with no age uses the 3-day interval.
- A record is **cleared** when the ticket was judged this run (it is in the rows, and its feed neither failed nor was deferred) and that finding is gone, which also covers a kind change on the same ticket; or when the ticket is no longer in the rows and both widgets were read in full (no page limit hit). A cleared finding that comes back later is sent as new.
- A failed or deferred feed, or a widget that hit its page limit, never clears a record.
- Records are written only after Teams accepts the post. If the post fails, the same findings are due on the next run.
- The state file `alerts-state.json` holds ticket ids, kinds and times only, never a title or a name. If it cannot be read, it is moved aside to `alerts-state.json.bad` and everything is sent once more.

### Shift summaries

With `tdx.summary.enabled`, the desk runner's `tdx watch` also posts a summary at each configured time (defaults: 07:30, 12:00 and 17:00 on weekdays; 08:00, 12:00 and 17:00 on Saturday; 12:00 on Sunday). It lists **every** open finding, grouped by kind in the order above with each kind's count, and marks the ones not yet alerted `(new)`; the rows use the wording below. It replaces that cycle's alert card, and the new or due findings it shows are recorded as alerted, so the re-alert policy above carries on from there. Settings, late and skipped slots, and mentions: [USAGE.md](USAGE.md#shift-summaries).

Only the day's **first** summary posted is that full list (a missed first slot makes the next one full); it marks `(new)` the alertable findings not open at the last summary posted. Later slots post an **update**, `Since 7:30 AM: 1 new ticket and 2 reminders for {team}`, with rows only for the new findings and the reminders now due, and the cleared ones as a count in its lead line. A finding missing from this run counts as cleared by the same rule that clears alert records (its ticket judged this run, or gone from a queue read in full); otherwise it is still remembered as open. An update posts only when it has a row: a new finding, a due reminder or an urgent event finding. In a shared chat, an update with nothing to act on is noise, so a slot where findings only cleared posts nothing; the cleared ones are forgotten all the same, so a later update does not count them again. While summaries go to the desk chat, the desk runner's alert cards go to the personal chat (`tdx.summary.desk_alerts`, `personal_alerts`); only [urgent event findings](#urgent-event-findings) reach the desk chat between slots.

The summary also has three things, all worked out in memory from the same run, with no LLM:

- **Sections, in order:** Events this week, Requester waiting, Follow up with requester, Someone else wrote last, Event: email the requester, Event: follow up with the requester, Event soon: room still TBD, Event over: resolve, Ready to close.
- **Events this week** (first): every unclaimed event ticket, an event request form or a renamed `EVENT [...]` ticket, with a date from the summary time to `tdx.summary.events_days` (7) days later, one row per event at its next date in that window, sorted by date. A row is the linked id (the title only with `show_titles`), then `Wed 10/21 3:00–5:00 PM · Lobby · A/V on form: yes · not emailed yet`, or `Sat 10/24 6:00–7:00 PM · no form · location missing` for an event without a form (the space and A/V parts are replaced by `no form`, said once):
  - the date and start time (date only when none was given), and the end time when the form or title gives a range, in 12-hour form like the ticket titles (`11:30 AM–12:30 PM` across noon);
  - the space type from the form, renamed tickets' included: `Lobby`, `Classroom`, `Other`, or `—` when the form's answer was not read;
  - `A/V on form: yes/no` from the form's services checklist. A/V asked for later, in a comment or elsewhere, is not seen, so `no` does not mean "no A/V needed";
  - the state. A renamed title first: `location TBD` (TBD in the title) or `location missing` (a date and a time but no room). Then the form: `marketing only`, or `no email needed` (no A/V and no classroom). Otherwise the feed state above: `not emailed yet`, `waiting on requester since Tue 8:26 AM` (the desk's last entry; no time when it is undated), `requester replied` or `in progress (requester answered)`; `not checked yet` when the run could not read the ticket's page (a failed or deferred feed).
  - Recurring events and unreadable dates are left out, as the rules leave them out. An event that already started before the summary time is not listed.
  - Never shown: requester or handler names, the A/V free-text answer or any other form field. An event's finding row, if it has one, still appears in its own section: this section is the week's agenda.
- **Possible duplicates:** among this run's unclaimed tickets, two are possible duplicates when the requester is the same person (matched as names are everywhere else) and the titles are similar: after lower-casing, dropping punctuation and a few stop words ("a", "the", "re", "fw", "please" and similar), their word sets overlap by at least `tdx.rules.duplicate_title_similarity` (0.6, Jaccard), or one title's words appear inside the other's. The newer ticket's row gets `(possible duplicate of #<older id>)`, pointing at the oldest such ticket. Event tickets and request forms are left out (all forms share one title, and two events by one requester look alike). It is never a finding, nothing about it is stored or logged, and names are compared in memory only.

### Urgent event findings

While summaries go to the desk chat, one kind of finding does not wait for the next slot. A finding is **urgent** when its event's next date is `tdx.events.near_event_days` (2) days away or less (whole hours until the event, at most 24 × that) and it is one of:

- `event_outreach_due` (not emailed yet);
- `event_details_pending` (waiting on the requester);
- `location_tbd_soon` (TBD or missing in the title);
- `client_waiting` with reason `event_requester_replied` (the requester wrote last on an event ticket).

Within that window each of these is already high priority, except a TBD location, which stays normal. In the cycle an urgent finding is first new to the desk chat (not shown there by a summary or an urgent card since it opened), it goes to the desk chat on one `Event soon: N need follow-up` card with no mention, or, when a summary is due that cycle, into the summary marked `(event soon)`. It is then remembered as shown, so it is not posted again until it clears by the rule above and comes back; the next update does not list it as new. The personal chat's alert cards are unchanged. `tdx.summary.urgent_events_to_desk = false` turns this off; it never applies without `teams.desk.approved`.

### Card wording

Ages are written out, because desk staff read words faster than unit symbols: `less than an hour`, `1 hour` and `N hours` under 48 hours, then `N days` (whole days, rounded down) from 48 hours (terminal output, logs and `--show-ids` keep their short `73h` form); `{days}` is the time until the event, in the same form, and `{when}` is `in {days}`. `{team}` is `teams.desk.team_name` (default "the help desk") on desk cards and "the help desk" on personal cards; `{Team}` is the same with a capital first letter. `{mailbox}` is `tdx.events.alt_host_mailbox`, the address requesters add as a Zoom alternative host, on desk and personal cards alike (Markdown escaped, so it can be copied as it reads), or "our shared mailbox" when it is not set. A reminder adds ` · reminder`. Section labels are in the desk's own words: "Requester waiting" (`client_waiting`), "Follow up with requester" (`follow_up_with_client`), "Someone else wrote last" (`needs_review`), "Event: email the requester" (`event_outreach_due`), "Event: follow up with the requester" (`event_details_pending`), "Event soon: room still TBD" (`location_tbd_soon`), "Event over: resolve" (`event_over`) and "Ready to close" (`ready_to_close`). The alert card's title counts the rows, `TDX queue: 12 new, 1 reminder`, or just `TDX queue: 12 new` when no reminder is due.

Every time on a card is 12-hour, like the ticket titles: `7:30 AM`, `12:00 PM`, `Tue 1:37 PM`, a range `1:30–2:30 PM` (or `11:30 AM–12:30 PM` across noon). Settings, state files, logs and the terminal keep 24-hour times. The summary headings say how many tickets need a look and for whom, never "open", which reads as open tickets in the queue:

| card | heading |
|---|---|
| Full summary | `TDX queue at 7:30 AM: 3 tickets for {team} (1 new since yesterday)`; late: `TDX queue at 8:50 AM (late: due 7:30 AM): …`; nothing open: `Queue clear at 7:30 AM: nothing needs attention.` |
| Update | `Since 7:30 AM: 2 new tickets for {team}`, `Since 12:00 PM: 1 reminder for {team}` or `Since 7:30 AM: 1 new ticket and 2 reminders for you`: the new rows, then every other row (due reminders, and an urgent finding open at the last post) |

Personal cards say "you" where desk cards name `{team}`.

| kind / reason | text |
|---|---|
| `client_waiting` / `requestor_last` | The requester wrote last, {age} ago: reply or check in |
| `client_waiting` / `no_reply_yet` | No reply yet, opened {age} ago |
| `client_waiting` / `expects_reply` | The requester has waited {age} for a reply |
| `client_waiting` / `event_requester_replied` | Event {when}. The requester replied {age} ago: read it and confirm the details |
| `follow_up_with_client` / `client_follow_up` | The requester said they'd follow up; quiet {age}: check in |
| `follow_up_with_client` / `client_no_reply_needed` | The requester's last message needed no reply; quiet {age}: check in or resolve |
| `needs_review` / `other_last` | The last message, {age} ago, came from someone other than the requester or {team}: check whether the requester still needs a reply |
| `event_outreach_due` / `outreach_due` | Event in {days}: email the requester for the room number. If it's hybrid or remote, also ask for the Zoom link, ask them to add {mailbox} as an alternative host, and ask whether to record. Then update the events spreadsheet. |
| `event_outreach_due` / `outreach_due_requester_wrote` | Event in {days}; the requester wrote {wrote} ago. Email them for the room number. If it's hybrid or remote, also ask for the Zoom link, ask them to add {mailbox} as an alternative host, and ask whether to record. Then update the events spreadsheet. (`{wrote}`: the time since the requester's latest entry) |
| `event_details_pending` / `details_pending_near` | Event {when}: still waiting on the requester (asked {age} ago): follow up now |
| `event_details_pending` / `details_pending` | Event in {days}; the requester hasn't answered for {age}: follow up |
| `location_tbd_soon` (`tbd_soon` and `location_missing`) | Location still TBD on the ticket, event in {age}. Check the events spreadsheet; if a room is listed, update the ticket. |
| `event_over` | Event ended {age} ago: ready to resolve? |
| `ready_to_close` | {Team} replied {age} ago, no reply since: close if nothing new |
| unknown kind or reason, or age `None` | the kind label, and the age if known |

Each row starts with the ticket id, linked to the ticket when its address is on the TDX host, then the title, cut to `title_chars` (80) with its Markdown escaped, so `EVENT [10/20 3pm Room 101] Club` shows its brackets. The card never carries a requestor name, an email address, Prim Resp or feed text, and it is never logged.

**Who each card speaks to.** The line under each card's title says plainly who should act and when the queue was checked. Desk cards name the team (`{team}` as above); personal cards say "you" or speak to you directly:

| card | line under the title | preview line |
|---|---|---|
| Event soon (desk) | {Team}, please follow up on these before the event. Checked Thu 12:41 PM. | N tickets for {team} |
| Full summary | Tickets for {team} to look at (Tickets that need your attention, on a personal card). Checked N unclaimed tickets at 7:30 AM. With nothing open: Nothing for {team} to look at right now (Nothing needs your attention right now). … | N tickets for {team} |
| Update | 5 tickets need a look in all; 1 was handled. Checked at 12:00 PM. (the handled part only when findings cleared since the last post; `1 ticket needs a look in all.` for one) | N tickets for {team} (all N that need a look) |
| Alert card (personal) | Checked Thu 12:05 PM: N unclaimed tickets, M ticket histories read, K could not be read. (the last part only when K > 0) | N tickets need attention |
| Alert card (desk) | {Team}, please follow up on these. Checked Thu 12:05 PM: … as above | N tickets for {team} |

On a personal card the preview line drops the team: `N tickets need attention`. With nothing to count it reads `Nothing for {team} right now` (`Nothing needs your attention right now` on a personal card). Desk cards say "for" the team rather than its possessive, which no single rule spells well for every name. The cards never use internal terms such as "the desk" or "read-only audit"; the read-only design is explained in these docs, not on cards.

## The follow-up classifier

The rules cannot tell "the client is waiting for us" from "the client said *they* would get back to us". The desk SOP says to check in after 2 days without a response either way, but the second case is not urgent and needs different wording. This is the auditor's one LLM step. It is **off by default** (`llm.enabled = false`), and on TDX data it also needs `llm.allow_institutional_data = true` in the person's own config; `--no-llm` turns it off for one run of `tdx audit` or `tdx watch`.

**What it looks at.** Only `client_waiting` / `requestor_last` findings, which are already 48+ hours stale. Everything else stays rules-only.

**Input.** The comment text of the requestor's latest visible reply, the same entry the rule judged. The feed reader keeps it in memory only; it never reaches a log, a file, output or an exception. Before the call, locally:
1. Quoted mail is dropped: everything from an "On … wrote:" line or an Outlook header block (a "From:" line followed within 4 lines by "Sent:"; bold is not relied on) onward, and lines starting with `>`. A reply with several nested blocks is cut at the first one.
2. Everything from a sign-off line ("Thanks", "Best", "Regards", "Sincerely", "Cheers" and similar, alone on its line) onward is dropped, unless nothing comes before it ("Thanks!" alone is kept).
3. The scrubber replaces the requestor's name, every author in that ticket's feed, the run's desk names, greeting names, emails, URLs, phone numbers, 8-digit IDs and usernames, and caps the text at `llm.max_input_chars`. If a known name survives, the call is not made (`scrub refused`).

**Verdicts.** The model (`claude-haiku-5-5`, effort low, structured output with one enum field) answers one of four:

| Verdict | Finding becomes | Priority | Reason code |
|---|---|---|---|
| `expects_reply` | client waiting | high | `expects_reply` |
| `client_will_follow_up` | follow up with client | normal | `client_follow_up` |
| `no_reply_needed` (a thank-you or "that worked", nothing asked) | follow up with client | normal | `client_no_reply_needed` |
| `unsure` | unchanged | high | `requestor_last` |

**It fails towards the louder finding.** `unsure`, a refusal, a failed call, a closed gate, a spent daily budget, a missing key, no text, a refused scrub or the LLM off all leave the finding exactly as the rules made it: `client_waiting` / `requestor_last`, "reply or check in". A missed reply never goes quiet. The client's message is untrusted input; the output is limited to the enum, and a verdict only changes a finding's kind, priority and wording, never an action.

**Stops.** A closed gate, a spent budget or a missing key stops the calls for the rest of the run, and the remaining findings are counted under that reason. So do 3 failed calls in a row.

**Each message is classified once.** `llm-verdicts.json` in the TDX data folder keeps, per ticket id, the time of the classified entry, the verdict and when it was decided; never text or names. A new requestor reply has a new time and is classified again. A record is dropped when its ticket has no `requestor_last` finding any more (unless its feed failed or was deferred this run), or 30 days after it was decided. `tdx watch` therefore does not pay for the same message every 45 minutes. A refusal is not cached, so it is asked again next run.

**Alerts.** A change from `client_waiting` to `follow_up_with_client` is a new kind, so it clears the `client_waiting` record and alerts once as new. A reason change inside `client_waiting` (`requestor_last` to `expects_reply`) keeps the record and does not re-alert.

**Output.** `tdx audit` prints one line after `Findings:`, counts only:

```
LLM: 3 to check, 2 cached, 1 classified (expects reply 0, will follow up 1, no reply needed 0, unsure 0); skipped 0, failed 0. Today 1,234 of 100,000 tokens (~$0.0002).
```

Skips are named when there are any (`skipped 2 (budget)`; `skipped 3 (gate 1, no text 2)`). With the LLM off: `LLM: off; 3 "requestor wrote last" findings keep their wording.` `--verbose` adds this run's input, output and cache-read tokens and how many replies were cut at `llm.max_input_chars`.

**Eval.** `uv run ops-suite llm eval [--set dev|holdout]` runs the synthetic sets in `tests/fixtures/llm/` through the same stripping, scrubbing and prompt as data class `synthetic`, and prints accuracy, a confusion table, the number of **unsafe errors** (a reply was expected, the model said `client_will_follow_up` or `no_reply_needed`), tokens and cost. `unsure` counts as right when a reply was expected and wrong otherwise. A message with nothing left after stripping makes no call and counts as `unsure`, as in the audit. The holdout set is for one final check after tuning on dev.

Results (claude-haiku-5-5, effort low; the prompt as first written, no tuning needed):
- **dev:** 24 of 24 right, 0 unsafe errors; 24 calls, cache read 30,222 tokens on calls 2+, ~$0.0007.
- **holdout, run once:** 12 of 12 right, 0 unsafe errors; 11 calls (the "..." case had no text left after stripping and was graded `unsure` without a call), ~$0.0003.
