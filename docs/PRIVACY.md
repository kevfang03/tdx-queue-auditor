# Privacy and data handling

The auditor reads ticket data that can include student and staff records: ticket titles, names and messages. This page says where each kind of data goes, what never leaves the person's computer, and what the tool will never do. The reasoning behind the controls is in [DESIGN.md](DESIGN.md).

Each person runs the tool on their own computer, with their own sign-in, under their institution's approval for read-only access. Nothing is shared between people.

## Data flow

| Where | What goes there | What never does |
|---|---|---|
| **The person's own computer** | The browser profile with the saved session. The alert state (ticket ids, finding kinds, times). The desk memory (salted hashes and dates). The verdict cache (ticket ids, times, verdict codes). Daily token counts. The session log (sign-in and expiry times). Logs (counts, reason codes, ticket ids). Rows, feed text and an event form's description are held in memory during a run only. | Names, titles or message text, in any file or log. |
| **The person's own Teams chat** | One card per cycle when something is new or due again: ticket id, link, title (cut to 80 characters), the finding and its age; a counts-only preview line. The "sign in again" and "auditor stopped" notices, and at each desk day's close a notice with the day's source comparison (counts only) and when the API token needs refreshing. | Requester names, email addresses, the responsible person, feed text. |
| **A shared desk chat** (optional) | Only after the runner turns on an approval switch in their own config: shift summaries with each finding's ticket id, link and age, and "Event soon" cards with the ticket id, link, the finding and the days to the event. Titles only if a second switch is on. | Titles (by default), names, email addresses, feed text, operator notices. |
| **The Claude API** (optional, off by default) | Only if the LLM step is turned on: the client's latest message on a *client waiting* ticket, scrubbed and capped (below). | Everything else: titles, names, ticket ids, other entries, event forms. |
| **Development** | Structure-only page captures, reviewed locally first; synthetic fixtures; counts from live checks. | Live pages, ticket text, real cards, screenshots of either. |

## What never leaves the machine

- The browser profile and its session cookies. It lives in the tool's own data folder, separate from the person's everyday browser, and is never logged, uploaded or committed.
- Ticket rows and feed text. They are read into memory for one run and dropped.
- The event request form's answers. The submission metadata block is cut off before parsing and never kept; only dates, yes/no flags and one count survive, with no text.
- The alert state, desk memory, verdict cache and logs. They stay in the `platformdirs` data folder, outside any repository.

## No AI by default

The default configuration makes no API calls at all. Every rule, card and summary is plain Python, and with the LLM step off, *client waiting* findings simply keep their neutral wording ("reply or check in"). A run with nothing to classify costs nothing.

## What the optional LLM step sends

Only when the person turns it on, and for live data only when they also turn on a second, separate switch in their own config:

- **Which text:** the client's latest visible message on a *client waiting* ticket, once per message.
- **Cleaned first:** quoted earlier mail and everything from the sign-off onward are dropped, and the length is capped.
- **Scrubbed:** names known to the run (the requester, every author in the feed, desk staff, greeting names) become `[NAME]`; emails, phone numbers, ID numbers, usernames and URLs are replaced. If any word of a known name survives, the call is refused. The client accepts only text produced by the scrubber.
- **Bounded:** a daily token budget hard-stops calls. Each call logs the model, token counts, an estimated cost and the ticket id, never text.
- **Limited power:** the answer is one of four labels, and it can only lower one finding's priority. It never triggers a reply, a write or any other action.

## Secrets in the keychain

The Teams webhook URLs and the API key are stored in the operating system's keychain through `keyring` (macOS Keychain, Windows Credential Manager, Linux Secret Service). They are never in config files, logs, output or the repository, and they are entered at a prompt that does not echo. TLS verification stays on, through the operating system's trust store.

The optional TDX Web API reader uses a token that the ticketing system's own SSO endpoint issues for the person's signed-in session: no new account, password or service key. It lives only in the keychain, is never printed or logged (only its expiry is shown), and is wiped once it expires or is refused. The reader sends only `GET` requests on a short allowlist, checked before anything is sent, and keeps the answers in memory only. When the API is made the source of the cards (`tdx.source = "api"`), the data flow is the same: the same fields reach the same rules, cards and state files, the token stays in the keychain, and the browser reads any run the API cannot.

## Logs and state hold ids and counts only

- Logs carry counts, reason codes and ticket ids. Browser failures carry a reason code, never the raw message, which can contain a path.
- State files hold ticket ids, finding kinds, timestamps and verdict codes.
- The desk memory holds salted SHA-256 hashes of staff names, forgotten after 180 days unseen. This is **pseudonymisation, not anonymisation**: the salt is stored beside the hashes, so someone with the file and a staff list could test names. It is acceptable only as a local file on the person's own computer; deleting it resets the memory.
- Every test uses invented names, addresses and ids, an in-memory keyring and temporary folders.

## What the tool never does

- Draft or send a reply to a client. Event cards carry a fixed checklist, never a drafted message.
- Write to the ticketing system: no status changes, notes, assignments or other edits. The only element it clicks is a widget's "next page" button.
- Publish to a wiki or knowledge base.
- Store a password or automate a sign-in.
