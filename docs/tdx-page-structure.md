# TDX dashboard page structure

What the reviewed dashboard capture (payload 02, taken 2026-10-08) shows about the IT Tickets dashboard in TDWorkManagement, in structural terms, and why `tdx rows` uses the selectors it does. The capture itself stays out of git; the markup sketches below are written by hand, not copied from it.

The selectors live in `src/ops_suite/tdx/rows.py` (constants at the top). The synthetic app's pages in `src/ops_suite/tdx/demo/pages/` and the test fixtures in `tests/fixtures/tdx_synthetic/workmgmt/` copy this structure with invented data.

## Rules the selectors follow

- Built only from tag names, ARIA roles and class names that are the same in every capture: Kendo UI classes (`k-…`) and TDX's own BEM-style classes (`tdx-…`).
- Never an `id`: the ids in the capture are generated (GUIDs or widget numbers) and differ per page load or per widget.
- Never a class or id that was masked in the capture (a `#` or `*` inside it). Where a stable Kendo class is masked (for example the pager's info label and the data cells' class), the code does not use it.
- Never page text, except the column headers (kept in the capture) and the widget titles, which come from config (`tdx.widgets.*_title`) because the capture shows them only as placeholders (`[text:5]` and `[text:8]`, matching the five and eight words of the two titles).

## Frame layout

The dashboard capture has two frames:

| Frame | Path | What it holds |
|---|---|---|
| Page (frame 0) | `/TDWorkManagement` | The TDWorkManagement shell: navigation and an application container whose content is an `iframe`. |
| Child frame | `/TDWorkManagement/WorkManagement/Dashboard/Dashboard` | The dashboard with its widgets. |

The ticket capture shows the same dashboard frame staying loaded behind an open ticket (the shell then has five frames). So `tdx rows` does not pick a frame by index or URL: it searches every frame of every open page for widget containers (below) and matches their titles. The `iframe` itself has only masked classes and ids, so nothing selects it.

## How the dashboard is chosen

Added after the first live run of `tdx rows` exited 5 with the claimed widget not found.

- **What happened:** the shell opens each person's *homepage* dashboard. On the owner's account that homepage has the Unclaimed widget but not "Claimed by Others". Choosing another dashboard (IT Tickets) happens inside the page: the shell's address stays the same and only the child frame changes. So no top-level address, and no `tdx.dashboard_url` pointing at the shell, reaches the IT Tickets dashboard.
- **What the captures show:** the widgets sit in the child frame with its own path, `/TDWorkManagement/WorkManagement/Dashboard/Dashboard`. The captures drop query strings, so it is not known how that frame's address names the chosen dashboard. It is assumed to be a query parameter or a server-side choice.
- **`tdx set-dashboard`:**
  - The person opens a dashboard showing both widgets by hand, in a headed, unguarded window, and the tool clicks nothing.
  - The tool then looks through every frame of every open page for **one** frame holding both configured titles, and saves that frame's full address, query included, to `<data>/tdx/dashboard.json`. If several frames qualify, the first one on the most recently opened page wins.
  - It then opens the address on its own in a new guarded browser and checks that both widgets load and parse. If they do not (the frame may need its shell, or the dashboard choice may be stored server-side), the file is removed and the command exits 5. The fallback, clicking the dashboard switcher, is a later payload.
- **Where `tdx rows` starts:** `tdx.dashboard_url` if set, else the saved address, else `tdx.base_url`. The first output line names the source. The widget search is the same in every case: all frames, matched by title.
- **Privacy:** the saved address is specific to the account. It is shown and logged only as its path with long digit runs masked (`describe_path`), never with its query.
- **Synthetic stand-in:** the synthetic shell (`src/ops_suite/tdx/demo/pages/shell.html`) starts on a homepage without the claimed widget, and `switchDashboard("d=2")` changes only the child frame's `src`. `d=3` is an IT Tickets frame that renders only inside a parent frame, which covers the failure path.

## Widgets

The dashboard frame lays widgets out in rows and columns:

```html
<div class="tdx-dashboard">
  <div class="tdx-dashboard__row tdx-dashboard__row--layout-medium">
    <div class="tdx-dashboard__column"><div>
      <div class="tdx-dashboard__widget-container"> … one widget … </div>
    </div></div>
    …
```

- **Widget container:** `.tdx-dashboard__widget-container`. The capture has three: the two ticket widgets, fully loaded, and a third (the "Closed Tickets in My Group(s)" widget) that was still loading. That one sat inside an extra `.tdx-dashboard__widget-placeholder` wrapper and held only its title and a `.tdx-loading-animation` element.
- **Title:** `h1.tdx-control-bar__title` inside `header.tdx-header-container`. Matched case-insensitively against the config titles after collapsing whitespace, which also covers a title that wraps across lines. A widget whose title matches neither config title is ignored, so the Closed widget, loaded or still loading, and any other widget are never read. The header also holds a filter drop-down (a `form` with buttons) and refresh/help icon buttons; the code never touches them.
- **Grid:** below the header, a plain `div` holds the Kendo UI grid:

```html
<div class="k-overflow-x-auto k-grid k-grid-md" id="(generated)">
  <table class="k-grid-table k-table k-table-md" role="grid">
    <colgroup>…</colgroup>
    <thead class="k-table-thead k-grid-header" role="rowgroup">
      <tr class="k-table-row" role="row">
        <th class="k-table-th k-header" role="columnheader">
          <span class="k-cell-inner"><span class="k-link"><span class="k-column-title">ID</span></span></span>
        </th>
        …
    <tbody class="k-table-tbody" role="rowgroup">
      <tr class="k-table-row k-master-row" role="row"> <td role="gridcell">…</td> … </tr>
      <tr class="k-alt k-table-row k-table-alt-row k-master-row" role="row"> … </tr>
  </table>
  <div class="k-pager k-grid-pager k-pager-md" role="application"> … </div>
</div>
```

The header and the rows are in one table (no separate scrolling header table). A widget counts as loaded once its container has a `.k-grid` with a header row and no `.tdx-loading-animation` or `.k-loading-mask` inside. `tdx rows` also waits until the container's HTML is the same on two polls in a row before reading it.

## Columns

- **Header cells:** `thead tr` → `th` (`role="columnheader"`). The label is the text of `.k-column-title` when present, otherwise the cell's whole text. The header text is collapsed, case-folded and stripped of sort-link text ("Sort by", "Ascending", "Descending"). The dashboard widgets show no sort text, but the group report's headers have a screen-reader label plus a sort link ("ID", then "Sort by" "ID" "Ascending"), so a label repeated twice is kept once.
- **Columns in the capture:**
  - Unclaimed widget (10): ID, Created, Title, Requestor, Affiliation, Resp Group, Acct/Dept, Priority, Due, Service.
  - Claimed by Others widget (11): Resp Group, Modified, ID, Title, Requestor, Acct/Dept, Status, Priority, Prim Resp, Due, Service.
  - The two widgets order their columns differently, so cells are mapped by header position, never by a fixed index.
- **Required:** unclaimed needs ID, Created, Title, Requestor, Resp Group; claimed needs ID, Modified, Title, Requestor, Resp Group, Status, Prim Resp. Created is read in the claimed widget only if a later layout adds it. A missing column stops the run with exit 5. The message names the missing columns and the found columns that are known column names; any other header labels are only counted.
- **Not read** (data minimisation): Affiliation, Acct/Dept, Priority, Due, Service.

## Rows and cells

- **Data rows:** `tbody tr.k-master-row` (alternate rows add `k-alt k-table-alt-row`). Cells are the row's direct `td` children (`role="gridcell"`); their class was masked in the capture and is not used.
- **Links:** in both widgets the ID, Title and Requestor cells hold `a.tdx-link.tdx-link--inline-block` with an `href` (masked as `[url]`). The claimed widget also links Acct/Dept and Prim Resp; the unclaimed widget links Acct/Dept. The ticket id is the ID cell's text (`[number]` in the capture); a cell without a plain integer is counted as "row without a ticket id", and more than half such rows on a page stops the run. `detail_url` is the ID cell's link (else the Title cell's), made absolute against the frame URL; `#` and `javascript:` links are ignored. Whether the real link opens the ticket page directly or an in-app tab is not visible in the capture, which is why the link is optional. Payload 04 will check it.
- **Dates:** shown only as `[datetime]` in the capture. The parser accepts the formats listed in payload 03 (US month/day order, `Fri 10/2/26 3:30 PM` and others) in `tdx.display_timezone`. An unreadable cell is reported only by its shape (each digit `9`, each letter `a`).
- **Empty widget:** not visible in the capture (both widgets had rows). Kendo's standard empty state is a `tbody` with a `tr.k-grid-norecords` row and no `k-master-row` rows; the parser counts that as zero rows. A `tbody` with rows but none marked `k-master-row` and no "no records" row is a structure error rather than a silent zero.

## Pager

Both ticket widgets show a Kendo pager; in the capture each had two numbered page buttons (more than 10 rows):

```html
<div class="k-pager k-grid-pager k-pager-md" role="application">
  <div class="k-pager-numbers-wrap">
    <button class="k-pager-nav … k-pager-first k-disabled"><span class="… k-svg-i-caret-alt-to-left"></span></button>
    <button class="k-pager-nav … k-disabled"><span class="… k-svg-i-caret-alt-left"></span></button>
    <select class="k-dropdown …"> (page numbers, for narrow screens) </select>
    <div class="k-pager-numbers">
      <button class="k-button … k-selected"><span class="k-button-text">1</span></button>
      <button class="k-button …"><span class="k-button-text">2</span></button>
    </div>
    <button class="k-pager-nav …"><span class="… k-svg-i-caret-alt-right"></span></button>
    <button class="k-pager-nav … k-pager-last"><span class="… k-svg-i-caret-alt-to-right"></span></button>
  </div>
  <span class="k-label …">(item range, masked)</span>
</div>
```

- **Next control:** `.k-pager .k-pager-nav:not(.k-pager-first):not(.k-pager-last)` containing the right-caret icon `.k-svg-i-caret-alt-right`. Older Kendo icon names (`k-i-caret-alt-right`, `k-i-arrow-60-right`, `k-i-arrow-e`) are accepted too, in case of a theme update. The buttons' `aria-label`/`title` attributes are not in the capture (the capture kept only structural attributes), so the selector does not rely on them.
- **Last page:** the next button carries `k-disabled` (on page 1 of the capture, "first" and "previous" were disabled this way). A `disabled` or `aria-disabled="true"` attribute counts too.
- **Page indicator:** the `k-selected` button inside `.k-pager-numbers`. After a click on "next", `tdx rows` waits until the first row's ticket id or this number changes, and the grid has settled. The item-range label's class was masked, so it is not used.
- How a page turn fetches its rows is not in the capture, because nothing was paged. It is most likely the same widget request as on load (below). The first live run will show it: either `Allowed non-GET` grows by one per page turn, or a `Blocked:` line names the request.

## Requests on load and the guard's decisions

`requests.txt` for the dashboard lists 57 distinct method/resource-type/path lines to the TDX host: documents, scripts, stylesheets, fonts and images, a few `GET` fetch/xhr calls (user settings, pinned items, saved tabs, report filters, customer notifications, open application) and **one non-GET request**:

| Request (digits masked) | Seen | Decision | Reason |
|---|---|---|---|
| `POST /TDWorkManagement/WorkManagement/Widgets/#` (fetch) | 2 on load | **Allowed** (`BUILT_IN_ALLOWED`) for resource types `fetch` and `xhr` only; the exact masked path, the TDX host only | Loads one dashboard widget's report rows (Kendo grid read); one per widget on load, likely also paging. |

Why it is allowed (architect decision, 2026-10-08):

- The `#` is the widget's id (a run of digits, masked by the capture tool). Two calls match the two ticket widgets that had finished loading; the third (Closed) widget was still showing its loading animation.
- The same request appears twice in the group-report and ticket captures too, where the dashboard frame is loaded in the background.
- Kendo UI's ASP.NET MVC data source sends its "read" (page, page size, sort, filter) as `POST` by default, so a `POST` here is the normal way a Kendo grid reads data, not a write. It is a widget/report endpoint and cannot change a ticket.
- Without it no widget renders, so `tdx rows` could not work.
- Limits: only `POST`, only to the TDX host, only script requests (`fetch`/`xhr`). A `document` POST to the same path (a form postback) stays blocked, and so does any longer path (`…/Widgets/#/Something`) or a different case.
- Every allowed non-GET request is counted and printed (`Allowed non-GET: N`, with a per-path line under `--verbose`). The live check compares that number with "2 on load, plus 1 per page turn"; anything else is a finding for the architect.

All `GET`, `HEAD` and `OPTIONS` requests are allowed to any host (page loads, scripts, styles, images and the sign-in redirect are all reads). Everything else is aborted, and each aborted request is reported as `METHOD /masked/path` with no host, query or fragment.

Not on load but seen in other captures: the group report's `POST /TDNext/Apps/Reporting/ReportBuilder.aspx/GetReportColumns` (not allowed) and the ticket page's `POST /TDNext/Apps/Feed/Search` (allowed since payload 04a; see "Ticket page and feed" below).

## WebSockets and service workers

- The dashboard capture shows no WebSocket or SignalR traffic. The ticket capture loads SignalR (a hub script, plus `negotiate` and `start` calls, all `GET`), so the ticket page opens a WebSocket. Since payload 04c, guarded runs refuse all of SignalR; see "Ticket page and feed", "Live updates (SignalR)".
- Guarded runs launch with service workers blocked, because a service worker's requests would bypass `context.route`. The capture shows no service worker script.

## Ticket page and feed

Written from the reviewed ticket capture (taken 2026-10-08). The selectors live in `src/ops_suite/tdx/feed.py` (constants at the top); the synthetic stand-ins are `ticket-shell.html` and `ticket.html` in `src/ops_suite/tdx/demo/pages/`, with the test feeds in `tests/fixtures/tdx_synthetic/workmgmt/feeds/`.

### Frames

| Frame | Path | What it holds |
|---|---|---|
| Page (frame 0) | `/TDWorkManagement` | The shell. The ticket opened as an in-app tab, so the page address stayed the shell's. |
| Child frame | `/TDWorkManagement/WorkManagement/Dashboard/Dashboard` | The dashboard, still loaded behind the ticket. |
| Child frame | `/TDNext/Apps/#/Tickets/TicketDet.aspx` | The older TDNext ticket page, with the feed. |
| Two more child frames | `about:blank` | The comment editor (CKEditor's editable body) and an empty frame. |

- **The feed frame is recognised by its content, never by its path or index:** the one frame with an element of class `feed` that holds entries (below) or the feed's refresh indicator. The dashboard frame has no feed classes at all, so it is never mistaken for it.
- The capture cannot show what the grid's ID link opens as a top-level address: the TDNext page on its own, or the shell with a tab. `tdx audit` opens it in a new page and searches every frame of that page.

### Feed layout

The ticket page is an ASP.NET form. Above the feed sit the attachments panel (`#divAttachments .js-attachments-panel`, a collapsed panel with its own per-file private checkboxes) and the comment form (`#divFeed`: the comment box, the notify list and the editor). None of them is part of the feed, and the reader never touches them. The feed itself is a separate script app (`/TDNext/Scripts/Feed/js/app.js`) mounted in `#ticketFeed`:

```html
<div id="ticketFeed">
  <div class="feed">
    <div class="gutter-top-sm gutter-bottom-sm"> nav pills: two view buttons </div>
    <div class="gutter-top-sm gutter-bottom-sm"> a filter form: three checkboxes and a search box </div>
    <p class="alert alert-info gutter-bottom-none">(a sentence)</p>
    <div class="refresh-indicator-wrapper NoPrint">
      <div class="refresh-indicator"><span class="fa fa-spinner fa-pulse fa-nopad fa-4x"></span>…</div>
    </div>
    <div class="(masked)-center NoPrint"><button class="… sr-only sr-only-focusable"><span class="fa fa-clock-o"></span>(3 words)</button></div>
    <div class="feed-(masked)" role="group"> one entry </div>
    …
    <div class="(masked)-center NoPrint"></div>
    <object type="text/html"></object>
  </div>
</div>
```

`NoPrint` shows as `No*` in the capture: "Print" is page text, so the capture tool masked that part of the class.

### Entries

```html
<div class="feed-(masked)" role="group">
  <div class="feed-profile-(masked)"><div class="profile-(masked) clickable blue-bg initials" role="link">(initials)</div></div>
  <div class="feed-body">
    <div class="feed-heading">
      <span class="feed-participant-name"><a href="…">(author, 2 words)</a></span>
      <span class="feed-private">(1 word)</span>              <!-- private notes only -->
    </div>
    <div class="feed-breadcrumbs gray wrap-(masked) feed-context"></div>
    <div class="wrap-(masked) feed-(masked)-(masked)"> the comment: text, br, b </div>
    <div class="truncate gray feed-context">(the notified addresses) <button class="btn …">(2 words)</button></div>
    <div class="feed-context"><span class="feed-timestamp gray"><span>(datetime)</span><span>(3 words)</span></span></div>
    <div class="feed-actions NoPrint"> buttons and a drop-down menu </div>
    <div class="feed-reply feed-(masked)-box"> a reply to this entry, see below </div>
  </div>
</div>
```

- **Entry:** an element with `role="group"` inside the feed root that has a direct `.feed-body` child. Its own class is masked, so it is not used. The capture has 5 entries.
- **Author:** the text of `.feed-heading .feed-participant-name`.
- **Timestamp:** the first `span` of `.feed-timestamp` in the entry's own `.feed-context`, an absolute date and time (the capture tool recognised it as one), read with `parse_tdx_datetime`. The second `span` (3 words) is most likely a relative age; it is not read.
- **Comment:** the `.feed-body` child that is none of `.feed-heading`, `.feed-breadcrumbs`, `.feed-context`, `.feed-actions` and `.feed-reply`. Its classes are masked, so it is found by elimination.
- **"Notified" list:** `.truncate.gray.feed-context`, the addresses as one text plus a button. Present on the 4 visible entries, absent on the private note. It has no timestamp and is skipped.
- **Replies to an entry:** `.feed-reply` blocks inside the parent's `.feed-body` (2 in the capture). Each has its own `.feed-participant-name` (a direct child), its own comment (with what looks like quoted email history: bold header lines and addresses) and its own timestamp in `.reply-context .feed-timestamp`. This is how email replies thread under the update they answer, so **each `.feed-reply` is read as its own entry**, with its own author and time. It counts as private if its own `.feed-private` marker is rendered, or if its parent's marker is rendered and the reply has no author of its own other than the parent's (payload 04c; it was "any reply under a private note" in 04a). See "Private notes: rendered, not present" below.
- **Attachments:** not inside entries in the capture; they sit in the separate attachments panel. In case an entry ever carries one, any element inside the comment whose class contains `attachment` is skipped, and so are `blockquote` and elements whose class contains `quote` (quoted email history).

### Telling the kinds apart

| Kind | How it shows | Rule in `feed.py` |
|---|---|---|
| Private note | a **rendered** `span.feed-private` in `.feed-heading` (structure; its 1-word label is not read) | `private`, whatever else the entry holds, unless it is a merge notice |
| Merge notice | the entry's own comment opens with "Merged incident" (`MERGE_NOTE_PREFIXES`) | `system`, one entry; its author counts as desk |
| Reassignment | the entry's own comment opens with "Reassigned this incident from" (`SYSTEM_NOTE_PREFIXES`) | `system`; its author counts as desk |
| Status change | written inside the comment as a line with bold values, the TDX interface phrase "Changed … from … to …" | a line (between `br`s) that holds a `b`/`strong` and whose text starts with `Changed`, followed later by `from` and `to`, is a field-change line |
| System entry | no comment text left once field-change lines, attachments and quotes are removed | `system` |
| Reply | any other comment text, including a reply with a status change, and every `.feed-reply` that is not private | `reply` |

- The private marker is structural. The field-change phrase is the only text the reader checks, and it only tests the phrase; it keeps nothing. One visible entry in the capture opens with a line of that shape (text, bold, text, bold, text) before the rest of the comment, which fits a status change written into a reply. The capture has no status-only entry, so the system rule is checked on synthetic pages only, and the exact wording of the phrase is not visible in the capture. The live check's `system` count shows whether it fires.
- An entry with no author is counted as unreadable and dropped. An entry whose timestamp cannot be read is kept with no date and counted as unreadable. Under `--verbose` both show only as shapes (`no author`, or the date's shape with each digit `9` and each letter `a`).
- Entries are returned newest first, sorted by time. Undated entries keep their place in page order.

### Loading and empty feeds

- **Loading:** the feed app shows `.refresh-indicator` (a spinning icon) while it searches. It is still in the captured page after loading, so it is hidden rather than removed. The capture drops `style`, so how it is hidden is not visible; the reader counts it as hidden when it or an ancestor inside the feed has `display: none` in its `style`, the `hidden` attribute or a `hidden` class.
- **Ready:** the feed root is there, and it holds at least one entry, or it holds none and the indicator is hidden. `tdx audit` also waits until the frame's HTML is the same on two polls in a row, and accepts an empty feed only after the page's `Feed/Search` request has finished, so a feed that has not searched yet is never read as empty.
- **Empty feed:** not in the capture. It is assumed to be the feed root with no entries and the indicator hidden; any message it shows is not read. A real empty feed on an opened ticket gives "client waiting, no reply yet", so the live check should look at one such ticket by hand.
- **Paging or "show more":** the capture shows the whole feed of a ticket with 5 entries and no visible paging control. The empty `(masked)-center NoPrint` block after the last entry may hold a "load more" button on longer feeds. The reader clicks nothing; if the first part of a long feed held only private or system entries, an older visible reply would be missed. Not seen yet; to revisit if the live check shows such a ticket.

### Ticket description

Added for event request forms, from the same reviewed ticket capture. The description is a separate area of the TDNext ticket frame, above the feed, rendered by the server with the page:

```html
<div id="(masked)Description">
  <div id="divDescription" class="gutter-bottom-lg">
    <h2>(1 word)</h2>
    <div class="gutter-top-sm"></div>
    <div class="wrap-(masked) ticket-desc">
      <div>(text)</div> <div><br></div> <div>(text)</div> ... <div><b>(2 words)</b></div> <div>(email)</div> ...
    </div>
  </div>
</div>
```

- **Container:** `#divDescription .ticket-desc` (both kept unmasked in the capture), falling back to `.ticket-desc`. Its own lines are `div`s, with `br` for blank lines and `b` for bold text. The selectors are `DESCRIPTION_SELECTORS` in `src/ops_suite/tdx/event_form.py`.
- **How it is read:** only for event request form tickets, after the feed has loaded, with one read-only `frame.evaluate` of the container's `innerText` in the feed frame (the frame's HTML text, one line per block, when the script fails). No extra request: in the capture's request list the description comes with the ticket page's own document (`GET TicketDet.aspx`), and the synthetic tests see no new or blocked request.
- **What is kept:** the text goes straight to `parse_event_form` and only the `EventForm` (dates, flags, one count) is kept. The capture shows the container's shape only (its text is placeholders), so the form's labels come from the owner's description of the blank form, not from a capture. The live check confirms them; an unreadable form is counted and described by its shape only.
- **Synthetic stand-in:** `ticket.html` has the same container; the app renders `descriptions/<id>.html` from its page source into it (`tests/fixtures/tdx_synthetic/workmgmt/descriptions/` for the tests, `tdx/demo/dataset.py` for the demo).

### Requests on the ticket page and the guard's decisions

The ticket capture's `requests.txt` lists 2 non-GET lines:

| Request (digits masked) | Seen | Decision | Reason |
|---|---|---|---|
| `POST /TDWorkManagement/WorkManagement/Widgets/#` (fetch) | 2 | Already allowed (dashboard) | The dashboard frame loaded behind the ticket. |
| `POST /TDNext/Apps/Feed/Search` (xhr) | 1 | **Allowed** (`BUILT_IN_ALLOWED`): exact path, resource types `xhr`/`fetch`, TDX host only | Loads the ticket's activity feed (search by ticket). A search is a read; without it the feed stays empty. |

Everything else on the ticket page is `GET`: the page itself, scripts and styles (the feed app and SignalR's hub script among them), and SignalR's `negotiate` and `start` calls.

### Private notes: rendered, not present (payload 04c)

The first live run read exactly as many private entries as visible ones (48 and 48), and 7 of 14 "client waiting" findings said "no reply yet". The suspicion: the live page carries `span.feed-private` on entries where it is not shown, which a skeleton cannot reveal, because the capture tool drops `style` and `hidden`.

**What the capture shows** (ticket capture, 5 entries): `span.feed-private` appears on **one** entry only, in its `.feed-heading`. The other four have no such element at all, hidden or not. Replies to an entry (`.feed-reply`) sit under 2 of the 5 entries, neither of them private, so the capture has no reply under a private note. The capture does not support "a marker on every entry" for that ticket, but it is one ticket, and it cannot show whether a present marker was displayed. So the reader now decides by rendering:

- In `read_feeds`, once the feed has settled, one read-only `frame.evaluate` in the feed frame returns, in document order, whether each `.feed-private` element is rendered: it has a layout box (`getClientRects`) and its computed `visibility` is `visible`. A marker hidden by `display: none` anywhere up the tree, by the `hidden` attribute or by `visibility: hidden` counts as not shown. The script reads nothing else and changes nothing.
- `parse_feed(..., private_marker_visible=...)` maps those flags onto the markers in the HTML. If the counts differ (the page changed in between), or the feed's entries have no layout at all (a hidden tab), it falls back to "present counts" for that feed and counts a **visibility fallback**. Fixture tests without a browser pass no list and get the old presence rule.
- `--verbose` prints one line of counts for the owner's check, for example `Entry markers: 97 entries, 31 nested; private marker present on 96, visible on 12; inherited-private 3; merge notes 2; reassignments 1; signature desk replies 14; visibility fallback 0.` "Present on" close to "entries" with "visible on" much lower confirms the hidden-marker theory.

### Entry types: no structural marker in the capture (payload 04c)

The feed's filter form offers three checkboxes whose labels have the word counts of "Edits", "Status Changes" and "Comments" (1, 2 and 1 words). The question was whether entries carry a matching type class or attribute that would tell a system entry from a comment without reading text. The capture cannot show one:

- all 5 entries have exactly the same class shape, `feed-(masked)`, whatever they hold (the private note, the reply opening with a field change, plain replies): the second word of the class appears in the page's text, so the capture tool masked it, and it may or may not differ per entry type;
- the capture tool keeps only `id`, `class`, `role`, `type`, `colspan`, `rowspan` and `name`, so any `data-*` attribute the feed app sets is not in the capture at all.

So the reader stays with text: the fixed TDX phrases above and the field-change line. A later capture could settle this if the capture tool recorded, structure only, whether entries differ in their class or `data-*` attributes.

### Merged tickets and reassignments (payload 04c addenda)

The owner's spot-check found that many private entries come from merging tickets: a merge notice, and entries carried over from the merged ticket. The capture has none.

- **The fixed wording**, as the owner reported it from live tickets (interface text only; `X` stands for ticket data):
  - merge notice: `Merged incident X (X, originally created by X):`, followed by the merged ticket's carried-over text, a whole email thread with quoted `From:`/`Sent:`/`To:`/`Subject:` blocks and signatures;
  - reassignment: `Reassigned this incident from X to X.`, followed by a `Notified: X <X>` line.

  On one ticket the owner checked, all 4 private entries were 3 merge notices and 1 reassignment, so these probably explain much of the first run's 48/48 split.
- The phrases are in `feed.MERGE_NOTE_PREFIXES` (`"Merged incident"`) and `feed.SYSTEM_NOTE_PREFIXES` (`"Reassigned this incident from"`). They are matched case-insensitively at the start of the entry's **own** comment text, after trimming and with quoted history (`blockquote`, quote classes) and attachments left out, so a phrase inside a quoted or carried-over message never counts. Both give `system`, whatever the private marker says.
- **A merge notice is one entry.** The carried-over thread is text inside the notice's comment, and the reader only ever makes entries from `role="group"` blocks and `.feed-reply` blocks, so `From:`/`Sent:` blocks inside it are never split off. The notice's author is passed on as a desk member (`TicketFeed.desk_authors`), as for a private note; so is a reassignment's author (architect decision: only staff can reassign).
- The diagnostic counts them as "merge notes" and "reassignments" in the `Entry markers:` line.
- **Carried-over entries** keep their own kind, author and timestamp; newest-first order puts them where they belong. A carried-over reply from a different client gives "needs review" through the normal "someone else" path.

### Live updates (SignalR), decided in payloads 04a and 04c

- The ticket page loads SignalR 2.4 (`/TDSignalR/signalr/hubs`) for live feed updates. Its `negotiate` and `start` calls are `GET`s. The WebSocket it opens between them is not in `requests.txt`, because the capture tool lists HTTP requests only.
- **Guarded runs refuse all of SignalR:** every request whose masked path starts with `/TDSignalR/` is aborted, `GET` included, and every WebSocket is closed before it connects (`context.route_web_socket`). A read-only audit has no use for live updates.
- In 04a only the WebSocket was closed. SignalR then fell back to long polling, and the first live run showed 15 to 17 of its posts as "blocked" requests. Now SignalR never gets that far.
- These are counted together as **live updates** (`WS /masked/path` and `GET /TDSignalR/...` lines, no host, no query), **not** among the blocked requests. `tdx audit` prints "Live updates blocked: N" next to "Blocked requests", so a non-zero "Blocked requests" again means that something the page asked for, other than live updates, was stopped.
- The synthetic ticket pages load SignalR's hub script, call `negotiate` and open a WebSocket; with all three refused, the feed still loads, because it comes from `Feed/Search`. Whether the real feed app waits for SignalR before it searches is for the live check: if it did, every feed would time out and the run would stop with exit 5 after three tickets.
