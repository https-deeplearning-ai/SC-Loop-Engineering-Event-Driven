# Event-Driven Requirements Review Agent

A Requirements Review Agent (built with LangChain / `deepagents`) that runs behind
two real triggers instead of a notebook cell: a **Slack bot** you can message
directly, and a **Jira/Confluence webhook** triggered by a real button on a
ticket or a page. The agent itself never changes between the two — only how
it gets invoked, and where the requirement document comes from.

Do Slack first — it's the simpler setup (no public URL needed). Jira/Confluence
is the second section.

Want to adapt this to your own tools instead of Slack/Jira/Confluence, or
change how the agent decides it's done? See **[EXTENDING.md](EXTENDING.md)**
— it's the design spec this repo was built from, written to be handed
directly to a coding agent.

---

## Prerequisites

- Python 3.12+
- An OpenAI API key (the agent makes live model calls)
- A Slack workspace where you can install an app (for the Slack section)
- An Atlassian (Jira + Confluence) Cloud site where you can create API tokens
  and Automation rules (for the Jira/Confluence section)

```bash
git clone <this-repo-url>
cd loop-engineering-event-driven
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

You'll fill in `.env` as you go through each section below.

---

## Part 1: Slack bot

### What you're running

`slack_bot.py` — DM it directly, or `@mention` it in a channel it's been
invited to, with a requirement document as the message. It replies in the
same thread with the agent's analysis.

### 1. Set your OpenAI key

In `.env`:
```
OPENAI_API_KEY=sk-...
```

### 2. Create the Slack App

1. Go to **api.slack.com/apps** → **Create New App** → **From scratch**.
   Name it, pick your workspace.
2. **OAuth & Permissions** (left sidebar) → **Scopes** → **Bot Token Scopes**
   → add:
   - `app_mentions:read`
   - `chat:write`
   - `im:history`
   - `channels:history`
   - `groups:history` (needed for private channels, and for the bot to read
     a thread's parent message when it's @mentioned with little/no text)
3. **Socket Mode** (left sidebar) → turn it **on**. This generates an
   **App-Level Token** (`xapp-...`) — copy it.
4. **Event Subscriptions** (left sidebar) → turn it **on** → under
   **Subscribe to bot events**, add:
   - `app_mention`
   - `message.im`

   Both the scope *and* the event subscription are required — a scope only
   grants permission, the event subscription is what actually makes Slack
   push that event to you.
5. **App Home** (left sidebar) → turn on the **Messages Tab**. Without this,
   Slack silently drops DMs to the bot before your code ever sees them.
6. **Install App** (left sidebar) → **Install to Workspace** → Allow. This
   generates the **Bot Token** (`xoxb-...`) — copy it.

### 3. Set the Slack tokens

In `.env`:
```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

### 4. Run it

```bash
python slack_bot.py
```

Leave this running in its own terminal tab.

### 5. Try it

- **DM**: open a direct message with the bot, paste a requirement document,
  send.
- **Channel**: invite the bot to a channel (`/invite @YourBotName`), post a
  requirement document, then `@mention` the bot underneath it (or in the
  same message).
- **Short mention in a thread**: post a requirement as one message, then
  reply in that thread with just `@YourBotName` — it infers the document
  from the thread's parent message.

If DMs don't seem to work even after this setup, it's almost always a
desktop-client cache issue — reload the Slack app (Cmd+R / Ctrl+R).

---

## Part 2: Jira + Confluence webhook

### What you're running

`jira_webhook.py` — a small server with two routes. It needs to be
**publicly reachable**, because (unlike Slack) Jira and Confluence can only
call *out* to a URL you give them — they have no "stay connected and listen"
mode. That's what `ngrok` is for below.

- **Confluence** (primary): a button on the requirement page itself. Click
  it any time — before a ticket even exists — and the review lands as a
  comment on the page.
- **Jira** (secondary): a button on a ticket. Click it to re-check the
  requirement linked to that specific ticket. Writes back to *both* the
  ticket and the linked Confluence page.

### 1. Get Atlassian API credentials

1. Go to **id.atlassian.com** → Account Settings → Security → **API tokens**
   → **Create API token**.
2. In `.env`:
   ```
   ATLASSIAN_EMAIL=you@example.com
   ATLASSIAN_API_TOKEN=<the token you just created>
   ATLASSIAN_SITE_URL=https://your-site.atlassian.net
   ATLASSIAN_WEBHOOK_SECRET=<any random string you choose>
   ```
   `ATLASSIAN_WEBHOOK_SECRET` isn't an Atlassian value — you make it up. It's
   the shared secret the Automation rules will send back to prove the
   request is really coming from your Jira/Confluence site.

### 2. Run the webhook receiver

```bash
python jira_webhook.py
```

Leave this running in its own terminal tab. It listens on `localhost:5001`.

### 3. Expose it publicly with ngrok

In a third terminal tab:
```bash
ngrok http 5001
```

Copy the `https://...ngrok-free.app` (or `.dev`) URL it prints. This stays
the same across restarts as long as you don't recreate it — ngrok gives
every free account one permanent "dev domain."

### 4. Link a Confluence page to a Jira ticket

Pick (or create) a Jira ticket and a Confluence page holding the actual
requirement text. Link them natively: from the Jira ticket, **Link** →
**Link to Confluence page** (or the equivalent from the Confluence side).
The webhook finds the linked page through this real link, not a custom
field.

### 5. Create the Jira Automation rule

1. Open the ticket → **⋯** or the **⚡ Automation** icon in the issue panel
   → (or Project settings → Automation) → **Create rule**.
2. **Trigger**: search for and select **Manual trigger**.
3. **Action** → **Send web request**:
   - **URL**: `<your-ngrok-url>/jira/webhook/analyze`
   - **Method**: `POST`
   - **Body**: Custom data —
     ```json
     {"issueKey": "{{issue.key}}"}
     ```
   - **Headers**: `X-Webhook-Secret` = the same value as
     `ATLASSIAN_WEBHOOK_SECRET` in your `.env`
4. Name the rule, **Save and enable**.

### 6. Create the Confluence Automation rule

1. Go to the Confluence **space** (not the Jira project) → the space's
   **"⋯"** menu → **Automation** → **Create flow**.
2. **Trigger**: **Manual trigger from page**.
3. **Action** → **Send web request**:
   - **URL**: `<your-ngrok-url>/confluence/webhook/analyze`
   - **Method**: `POST`
   - **Body**: Custom data —
     ```json
     {"pageId": "{{page.id}}"}
     ```
   - **Headers**: `X-Webhook-Secret` = same value as above
4. **Save and enable**.

### 7. Try it

- **Confluence**: open the requirement page → hover its title in the left
  sidebar's **Content** list → **"⋯"** → **Automate** → click your rule.
  (Atlassian recently moved this out of the page header — if you don't see
  it there, this sidebar path is the current location.) A comment appears
  on the page within a minute or two.
- **Jira**: open the ticket → the **⚡** icon in the issue panel → your
  rule's name. Two comments appear within a minute or two: one on the
  ticket, one on the linked Confluence page.

Both responses come back fast (under a second) even though the actual
review takes longer — the server acknowledges the click immediately and
does the real work in the background, so neither Automation rule times out
waiting.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Slack DMs don't arrive | Messages Tab not enabled under App Home, or `message.im` event subscription missing (scope alone isn't enough) |
| Slack says "sending messages has been turned off" after you just enabled it | Desktop client cache — reload (Cmd+R / Ctrl+R) |
| Nothing prints when you start a bot | stdout is buffered when not attached to a terminal directly — run with `python -u` if redirecting output to a file |
| Jira/Confluence button click does nothing | Check the ngrok URL in the Automation rule still matches what `ngrok http 5001` is currently printing — it changes if you recreate the tunnel |
| Confluence trigger button not where you expect | It's in the left sidebar's content-item **"⋯" → Automate**, not the page header's **"⋯"** menu |
| Webhook returns 401 | `X-Webhook-Secret` header value doesn't match `ATLASSIAN_WEBHOOK_SECRET` in `.env` |

---

## Files

- `slack_bot.py` — the Slack trigger (Socket Mode, no public URL needed).
- `jira_webhook.py` — the Jira + Confluence trigger (`/jira/webhook/analyze`
  and `/confluence/webhook/analyze`), needs a public URL via ngrok.
- `deepagent_helpers.py` — the agent itself (`build_review_agent`,
  `run_verification_loop_live`), unchanged by either trigger.
- `workspace/memories/AGENTS.md` — the agent's instructions, loaded fresh
  into every run.
- `jira_tickets.json` — sample tickets for local testing without any real
  Jira/Confluence account (not used by the two triggers above, which always
  pass a real document in, but required for `deepagent_helpers.py` to import).
- `EXTENDING.md` — the design spec behind this repo. Use it as a starting
  point if you want to adapt the agent to a different trigger (a GitHub
  webhook, a cron job, a different chat tool) or hand it to a coding agent
  to build the next one for you.
