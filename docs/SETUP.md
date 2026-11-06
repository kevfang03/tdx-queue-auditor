# Setting up the queue auditor on a new PC

A step-by-step guide for someone setting the auditor up for the first time, for example to take over as the desk's runner. No coding needed. Plan on about 30 minutes.

Every step starts with **where you do it**:

| Label | Where |
|---|---|
| **[POWERSHELL]** | The PowerShell window on the PC (press the Windows key, type `PowerShell`, press Enter) |
| **[TEAMS]** | The Microsoft Teams app |
| **[POWER AUTOMATE]** | The Power Automate website, `make.powerautomate.com`, in your normal web browser |
| **[TDX WINDOW]** | A browser window the auditor opens for you to sign in to TDX |
| **[WINDOWS]** | Windows settings |

Each step ends with **✅ You should see**, so you know it worked before moving on. If something else happens, look in [Troubleshooting](#troubleshooting) at the end.

**Before you start, you need:**

- A Windows 10 or 11 PC that stays **on and plugged in during desk hours**. The desk has **one** runner; if a runner already posts to the desk chat, it is switched off at [step 7.1](#71-switch-off-the-old-runner), never before.
- Your own TDX account, with access to the desk's ticket groups.
- Membership in the desk's Teams group chat.
- The **settings text** from the desk's current runner (their `.env` file; it holds no passwords).

---

## Part 1: Install the tools

### 1.1 Install Git and uv

**[POWERSHELL]** Copy each line, paste it into PowerShell (right-click pastes), and press Enter. Wait for each to finish.

```powershell
winget install Git.Git
```

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then **close PowerShell and open it again**, so it finds the new tools.

✅ You should see: `uv --version` prints a version number.

### 1.2 Download the auditor

**[POWERSHELL]**

```powershell
cd $HOME
```

```powershell
git clone https://github.com/kevfang03/tdx-queue-auditor
```

```powershell
cd tdx-queue-auditor
```

✅ You should see: the prompt now ends in `tdx-queue-auditor>`. **Every later PowerShell step runs from this folder.** If you open a new PowerShell window, run `cd $HOME\tdx-queue-auditor` first.

### 1.3 Install it

**[POWERSHELL]**

```powershell
uv sync
```

```powershell
uv run playwright install chromium
```

✅ You should see: both finish without a red error. The second one downloads a browser and can take a few minutes.

---

## Part 2: Add the settings

### 2.1 Create the settings file

**[POWERSHELL]**

```powershell
notepad .env
```

Notepad asks whether to create a new file: click **Yes**. Paste the settings text from the desk's current runner, then **File > Save** and close Notepad.

✅ You should see: `Get-Content .env` prints the settings back.

The file holds the TDX address, the desk's report numbers, summary times and similar settings. It never holds a password, a token or a webhook link; those are kept in Windows Credential Manager, as in [Part 3](#part-3-connect-your-own-teams-chat) and [Part 4](#part-4-sign-in-to-tdx).

---

## Part 3: Connect your own Teams chat

The auditor posts to Teams through a **Workflows** flow: a small automation with a private web link. Anyone who has that link can post to the chat, so **treat it like a password**: never paste it into a chat, an email or a file.

You make one flow for **your own chat** now (sign-in reminders and test cards go there), and one for the **desk chat** in [Part 6](#part-6-connect-the-desk-chat).

This part moves between Teams and the Power Automate website. That's expected: Teams creates the flow, and the website is where you fix its settings.

### 3.1 Create the flow from a template

**[TEAMS]**

1. In the left bar, click **Apps** (or **…**), search for **Workflows**, and open it.
2. Click **Create** (or **+ New flow**).
3. In the template search, type **Send webhook alerts to a chat** and click that template.
4. Name it, for example `Queue auditor – my chat`. If Teams asks you to sign in to a connection, do so. Click **Next**.
5. If it asks where to post, pick anything; you fix it in the next step. Click **Create flow**.
6. Teams shows a web link. **Ignore it**; you copy the right one in step 3.3.

<!-- screenshot: setup-3-1-template.png (the template card in Workflows; crop out names) -->

✅ You should see: a message that the flow was created.

### 3.2 Fix where the flow posts

The template's default target ("chat with yourself") does not work, so change it.

**[POWER AUTOMATE]**

1. Open **make.powerautomate.com** in your browser and sign in with your work account.
2. Click **My flows** on the left, click the flow you just made, then click **Edit** at the top.
3. The flow is a chain of boxes. Find **both** boxes named **Post card in a chat or channel**. One is under **True** and one under **False**, and they may sit inside a **For each** box: click a box to open it. In **each** of the two, set:
   - **Post as:** `Flow bot`
   - **Post in:** `Chat with Flow bot`
   - **Recipient:** your own name or email
   - **Adaptive Card:** this box empties when you change "Post in". Click into it, click the **lightning-bolt** icon (dynamic content), and choose **Attachments Adaptive Card**.
4. Under **False**, click the **For each** box and set its input the same way (lightning bolt) to **Attachments**.
5. Click the **first box** at the top, **When a Teams webhook request is received**, and set **Who can trigger the flow** to **Anyone**. The auditor posts without signing in to Microsoft, so the flow must accept that.
6. Click **Save** (top right).

<!-- screenshot: setup-3-2-post-card.png (one "Post card in a chat or channel" box with the four fields set; crop out the recipient) -->
<!-- screenshot: setup-3-2-trigger.png (the trigger box with "Who can trigger the flow: Anyone") -->

✅ You should see: "Your flow is ready" or a saved confirmation, and no red warning on any box.

### 3.3 Copy the flow's link

**[POWER AUTOMATE]** Still in **Edit**, click the first box again (**When a Teams webhook request is received**). It shows a long **HTTP URL**. Click the **copy** icon next to it.

<!-- screenshot: setup-3-3-http-url.png (the copy icon next to HTTP URL; blur the link itself) -->

✅ You should see: the link is copied. Don't paste it anywhere except the next step.

### 3.4 Save the link in the auditor

**[POWERSHELL]**

```powershell
uv run ops-suite teams set-webhook
```

At the prompt, **right-click** to paste. **Nothing appears on screen**; the prompt hides what you paste, like a password. Then press **Enter** once.

✅ You should see: `Webhook saved in the system keychain.`

### 3.5 Send a test card

**[POWERSHELL]**

```powershell
uv run ops-suite teams test
```

✅ You should see: a test card in **[TEAMS]**, in your chat with **Workflows** (Flow bot).

---

## Part 4: Sign in to TDX

### 4.1 Sign in

**[POWERSHELL]**

```powershell
uv run ops-suite tdx login
```

**[TDX WINDOW]** A browser window opens. Sign in with your own account, MFA included. When the TDX start page shows, go back to PowerShell and press **Enter**. The auditor then asks for a second sign-in for the TDX Web API in the same window: sign in again if asked. The window closes by itself.

If **Windows Hello** pops up (face, fingerprint or PIN) and gets stuck, click **Cancel**, then **Sign in another way**, and choose your **password** or **Authenticator app** approval.

✅ You should see: `TDX API token: saved, expires …`

### 4.2 Choose the dashboard (optional)

The auditor reads tickets through the TDX Web API. The dashboard is only a backup it uses if the API cannot answer, so this step can be skipped if it gives you trouble.

**[POWERSHELL]**

```powershell
uv run ops-suite tdx set-dashboard
```

**[TDX WINDOW]** Switch to the desk's shared tickets dashboard: the one that shows both **Unclaimed Tickets in My Group(s)** and **Active Tickets Claimed by Others in My Group(s)**. Your own home dashboard may not. Wait until both lists load, then press **Enter** in PowerShell. It can take a minute or two with nothing printed.

✅ You should see: a line starting `Saved.` If you get "does not open on its own", skip this step.

---

## Part 5: Check that it reads the queue

Nothing in this part posts to Teams.

### 5.1 The quick check

**[POWERSHELL]**

```powershell
uv run ops-suite tdx api check
```

✅ You should see: two lines like `Unclaimed report: 26 row(s), 10 column(s), every needed column found.` An error such as `forbidden` means your account cannot see the desk's saved reports: ask whoever owns them to share them with you in TDX.

### 5.2 Compare with the current runner

**[POWERSHELL]** You and the current runner each run this at about the same time:

```powershell
uv run ops-suite tdx audit
```

Compare the **Unclaimed** line and the **Findings** line. They should match. One or two "ready to close" more or less is normal for a brand-new runner: the auditor learns who is on the desk as it runs. When you share the output, copy from the first line it printed, **not** the `PS C:\Users\…>` prompt line, which shows your account name.

---

## Part 6: Connect the desk chat

### 6.1 Make the desk flow

Repeat **[Part 3, steps 3.1 to 3.3](#31-create-the-flow-from-a-template)** with two differences:

- Name it, for example, `Queue auditor – desk`.
- In step 3.2, set **Post in** to **Group chat** and pick the **desk's group chat**, instead of "Chat with Flow bot". If the chat isn't listed, you are not a member of it yet.

### 6.2 Save the desk link

**[POWERSHELL]**

```powershell
uv run ops-suite teams set-webhook --to desk
```

Right-click to paste (nothing shows), press **Enter**.

✅ You should see: `Desk webhook saved in the system keychain.` **Don't send a test card to the desk chat**; the first real card in Part 7 shows that it works.

### 6.3 Optional: a readable pop-up

For any card a flow posts, Teams' pop-up only says "Sent a card", and the card can't change that. A flow can also post a short **text** line right **after** each card. Teams shows that line in its own pop-up, the newest one on top, and in the chat list preview, for example `3 tickets for the help desk`. The line has counts only, never ticket titles, because pop-ups show on screen. Posting it means two messages per post in the chat: the card, then the line under it.

**[POWER AUTOMATE]** In the desk flow, click **Edit**. Then, in both branches, **below** each **Post card in a chat or channel** box:

1. Click the **+** under the card box and choose **Add an action**. Search for **Post message in a chat or channel** and pick it.
2. Set **Post as**, **Post in** and the chat exactly as in the card box above it.
3. Click into **Message**, choose **Expression** (the **fx** icon), paste this and click **Add**:

   ```text
   triggerBody()?['attachments']?[0]?['content']?['fallbackText']
   ```

4. **Save.**

Try it on your **own** flow first, and check how the pop-up looks before adding it to the desk flow.

---

## Part 7: Start it

### 7.1 Switch off the old runner

**On the current runner's PC [POWERSHELL]**, close the `ops-suite tdx watch` window, then:

```powershell
uv run ops-suite tdx agent uninstall
```

✅ You should see: `uv run ops-suite tdx agent status` says `installed no`. Two runners would post every card twice.

### 7.2 Install and start yours

**[POWERSHELL]** on your PC:

```powershell
uv run ops-suite tdx agent install --to desk
```

This makes the auditor start by itself every time you sign in to Windows. To start it right now:

```powershell
Start-Process "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\ops-suite tdx watch.cmd"
```

✅ You should see: a small minimized window called **ops-suite tdx watch** on the taskbar. Leave it open; closing it stops the auditor.

### 7.3 Check it

**[POWERSHELL]**

```powershell
uv run ops-suite tdx agent status
```

✅ You should see: `running yes`. At the next summary time, one card appears in the desk chat.

### 7.4 Keep the PC awake

**[WINDOWS]**

1. **Settings > System > Power & battery > Screen and sleep:** set **When plugged in, put my device to sleep after** to **Never**.
2. On a laptop: **Control Panel > Hardware and Sound > Power Options > Choose what closing the lid does:** set **When I close the lid / Plugged in** to **Do nothing**, then **Save changes**.
3. **Settings > Windows Update > Advanced options > Active hours:** cover the desk hours, so updates don't restart the PC mid-shift.

A locked screen (Win+L) is fine: the auditor keeps running. Asleep, shut down or on battery with the lid closed, it pauses until the PC is awake again.

---

## Part 8: The daily routine

**Each evening**, the auditor posts a short card to **your own chat** saying whether the TDX sign-in lasts through the next desk day. When it says to refresh:

**[POWERSHELL]**

```powershell
cd $HOME\tdx-queue-auditor
```

```powershell
uv run ops-suite tdx api token --headed
```

**[TDX WINDOW]** Sign in (MFA included; cancel Windows Hello if it gets stuck, as in [step 4.1](#41-sign-in)).

✅ You should see: `TDX API token: saved, expires …`. If you skip it, the desk cards stop the next day until you sign in.

**After a restart**, the auditor starts by itself when you sign in to Windows. To check: `uv run ops-suite tdx agent status`. If it doesn't say `running yes`, run the `Start-Process` line from [step 7.2](#72-install-and-start-yours).

**To watch it work**, run the line below. Every 30 minutes during desk hours, a line starting `source: api` appears. **Ctrl+C** closes the view; the auditor keeps running.

```powershell
Get-Content -Wait -Tail 20 "$env:LOCALAPPDATA\ops-suite\Logs\watch.out.log"
```

---

## Troubleshooting

| What you see | What it means | What to do |
|---|---|---|
| `not an https Power Automate address` when saving a link | The paste didn't reach the hidden prompt | Run it again, **right-click** to paste (or Ctrl+V), press Enter once, type nothing else |
| The same, ending `(the host must end with …)` | The link is on a web domain the auditor doesn't allow yet | Note only the part between `https://` and the first `/`, and add it to `teams.webhook_host_suffixes` in your settings |
| Test card fails with `not a ChatThread` | A "Post card" box still posts to the default target | Redo [step 3.2](#32-fix-where-the-flow-posts) in **both** boxes |
| Test card fails, card empty or `http_400` | The **Adaptive Card** field is empty | Step 3.2, item 3: choose **Attachments Adaptive Card** again |
| Test card fails with `http_401` or `http_403` | The flow isn't open to the auditor | Step 3.2, item 5: **Anyone**, then **Save** |
| The desk chat isn't in the "Post in" list | You're not in that group chat | Ask to be added, then redo step 6.1 |
| Sign-in window stuck on Windows Hello | Hello doesn't work in the auditor's browser window | **Cancel**, **Sign in another way**, then password or Authenticator |
| `sign_in_timeout` or `window_closed` | The sign-in didn't finish within 5 minutes | Run the command again |
| `set-dashboard`: "does not open on its own" | The dashboard didn't load by itself | Skip [step 4.2](#42-choose-the-dashboard-optional); it's optional |
| `tdx api check`: `forbidden` or `not found` | Your account can't see the desk's saved reports | Ask the reports' owner to share them with you in TDX |
| Every card shows up twice in the desk chat | Two runners are on | Switch one off ([step 7.1](#71-switch-off-the-old-runner)) |
| No cards at all | Outside desk hours, PC asleep, or the evening sign-in was missed | Check `tdx agent status`, then run `tdx api token --headed` |

More detail on every command is in [USAGE.md](USAGE.md).
