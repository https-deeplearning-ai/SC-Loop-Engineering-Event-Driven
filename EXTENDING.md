# Spec: Turn the Verification Loop into an Event-Driven Agent

This is the design spec behind the two triggers already working in this
repo (`slack_bot.py`, `jira_webhook.py`). It's written to be handed directly
to a coding agent, so it reads as an instruction document rather than prose.

Two ways to use it:

1. **Understand why the existing files look the way they do** — read it
   straight through once.
2. **Build a new trigger** (a GitHub webhook, a cron job, a different chat
   tool, whatever your real system needs) against the same agent — copy
   this file, replace section 4 or 5 with your new trigger's requirements,
   and hand the result to a coding agent the same way this repo's triggers
   were built.

---

## 0. Goal

You already have a working **Requirements Review Agent** with a verification
loop: `build_review_agent(rubric_iterations=...)` builds it,
`run_verification_loop_live(agent, requirement_doc, ...)` runs it end to end
— a first draft, a grader that checks it against a rubric, and up to a few
revision passes until the grader is satisfied or the attempts run out.

The goal of this spec is to make the **same agent, unchanged**, reachable
from three real triggers:

1. A **Slack bot** — DM it, or `@mention` it in a channel.
2. A **Jira button** — on a ticket, triggers a review of the requirement
   document linked to that ticket.
3. A **Confluence button** — on the requirement page itself, triggers a
   review of that page directly.

**Non-goal**: none of this changes what the agent does or how it decides
when it's done. If you find yourself wanting to edit `deepagent_helpers.py`
or `workspace/memories/AGENTS.md` to make a trigger work, stop — the trigger
is wrong, not the agent. This rule is the whole point of the exercise: a
production agent should get *more* trigger surfaces over time without its
core logic drifting to accommodate each one.

---

## 1. Starting point (do not modify)

You already have, in this repo:

- `deepagent_helpers.py` — exposes (at minimum) these two functions, used
  exactly as-is:
  ```python
  def build_review_agent(
      model: str = DEFAULT_MODEL,
      max_turns: int = MAX_TURNS,
      workspace_dir: str = WORKSPACE_DIR,
      rubric_iterations: int = 0,       # > 0 turns on the verification loop
      grader_model: Optional[str] = None,
  ): ...

  def run_verification_loop_live(
      agent,
      requirement_doc: Optional[str] = None,
      rubric: str = DEFAULT_RUBRIC,
      workspace_dir: str = WORKSPACE_DIR,
      ticket_id: str = DEFAULT_TICKET_ID,
  ) -> VerificationRun: ...   # .analysis_markdown is the final, grader-approved result
  ```
  Also exposes `AGENTS_BASELINE` (path to the baseline `AGENTS.md`) and
  `DEFAULT_RUBRIC`.
- `workspace/memories/AGENTS.md` — the agent's instructions.
- `jira_tickets.json` — sample tickets; `deepagent_helpers.py` reads this at
  import time, so it must stay a sibling file even though the new code below
  never looks up a ticket by id (it always passes a real fetched document).

If any of these function signatures differ in your actual `deepagent_helpers.py`
**trust your file, not this spec** — read it first and adapt.

---

## 2. What you're building

Two files, nothing else:

- `slack_bot.py` — the Slack trigger.
- `jira_webhook.py` — both the Jira trigger and the Confluence trigger (one
  small server, two routes — they share a deployment and a secret, there's
  no reason to split them into separate processes).

If you're building a *new* trigger instead of using these two, the same
rule applies: one new file (or a small server with a few routes), nothing
else, and nothing in `deepagent_helpers.py` changes.

---

## 3. Shared design rules (apply to both files)

1. **Always build the agent with the verification loop on**, not the bare
   execution loop:
   ```python
   agent = build_review_agent(rubric_iterations=2, max_turns=40, workspace_dir=...)
   run = run_verification_loop_live(agent, requirement_doc, rubric=DEFAULT_RUBRIC, workspace_dir=..., ticket_id=...)
   ```
   `max_turns=40` (higher than a single-draft default) because grading passes
   cost more turns than a single draft. What you write back is
   `run.analysis_markdown` — the grader-approved result, not the first draft.

2. **One throwaway workspace directory per run.** `build_review_agent`
   requires `memories/AGENTS.md` to exist inside whatever `workspace_dir` you
   give it. Before each run: create a fresh directory (e.g. named with a
   short uuid), copy `AGENTS_BASELINE` into `<dir>/memories/AGENTS.md`, run
   the agent against that directory, then delete the directory in a
   `finally` block. This keeps concurrent runs (two Slack messages close
   together, or a Slack message during a Jira run) from seeing each other's
   leftover `analysis.md`.

3. **Never edit the requirement document.** Both the Jira and Confluence
   write-backs are new *comments*, never edits to the page/ticket body. This
   matters even more here than you might expect: a single Confluence page
   can be linked from more than one Jira ticket, so "whose trigger gets to
   edit the source" has no good answer — don't build toward one.

4. **Respond to external webhooks fast, do the real work after.** A
   verification-loop run can take over a minute. Any system calling you
   synchronously (Jira/Confluence Automation) may treat a slow response as a
   failure even if the work is still progressing fine. Pattern: validate the
   request, return a 2xx immediately, and do the fetch + review + write-back
   in a background thread. (Slack's Socket Mode doesn't have this problem —
   there's no synchronous caller waiting on a response — so this rule is
   specific to the Jira/Confluence webhook, not the Slack bot.)

5. **On failure, write back the error, don't just log it.** If the review
   throws partway through, catch it and post a short failure message to
   wherever the result was supposed to go (the Slack thread, the Jira
   comment, the Confluence comment), rather than letting the background
   thread fail silently.

---

## 4. `slack_bot.py` spec

**Trigger mechanism**: Slack **Socket Mode**, via the `slack-bolt` Python
package. The bot opens an outbound websocket to Slack and receives events
over it — no public URL needed. (Don't use Slack's HTTP Events API / request
URL mode for this; Socket Mode is simpler and is what the rest of this spec
and the companion README assume.)

**Environment variables needed**: `SLACK_BOT_TOKEN` (`xoxb-...`),
`SLACK_APP_TOKEN` (`xapp-...`).

**Two event handlers**:

- `app_mention` — fires when the bot is `@mentioned` in a channel. Strip the
  leading `<@BOTID>` mention text; whatever's left is the candidate
  requirement document.
- `message`, filtered to `channel_type == "im"` (a DM), excluding messages
  with a `subtype` (edits, joins, etc.) or a `bot_id` (the bot's own replies
  — without this check it will review its own analysis forever).

**Short-mention inference**: if the `app_mention` text (after stripping the
mention) is shorter than some threshold (e.g. 20 characters), it's probably
not the document itself — it's something like `@bot` or `@bot please check
this`. In that case, if this mention is a reply inside a thread, fetch the
thread's parent message and use *that* as the document instead, as long as:
the parent itself is long enough to plausibly be a document, and the parent
wasn't posted by a bot (so a stray re-mention under the bot's own prior
report doesn't get treated as a new review request).

**Reply behavior**: acknowledge immediately ("reviewing now..."), then once
the run finishes, reply in the same thread (`thread_ts`) with the analysis.
On exception, reply with a short failure message instead of raising.

**Polish (optional, do this after the above works)**: Slack's message
rendering isn't standard markdown — `**bold**` needs converting to `*bold*`,
`- ` bullets read better as `• `, and Slack collapses genuinely blank lines
in plain-text messages (a visible divider or a non-breaking-space line gets
around this). If you do this, build it as a *display-only* transform — never
alter `run.analysis_markdown` itself, and consider sending the raw markdown
too (e.g. in a code block) so it's still copy-pasteable.

---

## 5. `jira_webhook.py` spec

**Trigger mechanism**: a small Flask (or equivalent) HTTP server with two
routes, fronted by a tunnel (e.g. ngrok) for local development since Jira
Cloud and Confluence Cloud both need a real public URL to call. Both Jira
Automation and Confluence Automation support a **Manual Trigger** rule type
whose action is **Send web request** — that's what calls into this server.

**Environment variables needed**: `ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN`
(basic auth for both Jira's and Confluence's REST APIs), `ATLASSIAN_SITE_URL`,
and a secret you make up yourself, e.g. `ATLASSIAN_WEBHOOK_SECRET` — both
Automation rules will be configured to send this back as a header, and it's
this server's only auth check (a lightweight, appropriate amount of security
for this use case — not full OAuth).

### Route 1: `POST /confluence/webhook/analyze` (primary trigger)

- Request body: `{"pageId": "<confluence page id>"}`.
- Check the shared-secret header; `401` if missing/wrong.
- `400` if `pageId` is missing.
- Otherwise: kick off a background thread, return `202` immediately.
- Background work: fetch the page's body via the Confluence REST API
  (`GET /wiki/api/v2/pages/{id}?body-format=storage`), strip it down to
  plain(ish) text (the body is HTML — stripping tags and turning `</p>` /
  `<br>` into newlines is enough for a short requirement doc with no tables),
  run the verification loop against it, write the result back as a
  **Confluence comment** on that same page (see "Write-back" below). No
  ticket is involved in this path.

### Route 2: `POST /jira/webhook/analyze` (secondary trigger)

- Request body: `{"issueKey": "<jira issue key>"}`.
- Same auth/validation/202-then-background pattern as above.
- Background work:
  1. Resolve the Confluence page linked to this ticket via Jira's **remote
     links** API (`GET /rest/api/3/issue/{key}/remotelink`) — this is the
     ticket's real, native link to a Confluence page (Link → Confluence
     page, from either product's UI), not a custom field. If more than one
     Confluence link is present, the first one is fine to use. If none is
     found, write back a comment saying so and stop.
  2. Fetch and review the page the same way Route 1 does.
  3. Write the result back **twice**: once as a comment on the Jira ticket
     (`POST /rest/api/3/issue/{key}/comment`), and once as a comment on the
     Confluence page (same write-back as Route 1). Both, not either —
     the ticket view and the doc view are both useful audiences for this
     result.

### Write-back: Jira comment

Jira's comment endpoint requires **Atlassian Document Format**, not plain
text or markdown. The simplest valid shape: one paragraph node per non-empty
line of `run.analysis_markdown`.

### Write-back: Confluence comment

Confluence's comment endpoint (`POST /wiki/rest/api/content` with
`type: "comment"`, a `container` pointing at the page, and a
`body.storage.value`) expects **storage format**, which is HTML. Convert
`run.analysis_markdown` to HTML with a real markdown renderer (don't
hand-roll one — this is a case where a standard converter is a clean fit,
unlike Slack's non-standard mrkdwn).

---

## 6. Acceptance checks

Before wiring up the real Slack app / Jira Automation rule / Confluence
Automation rule, verify each piece works on its own:

1. `curl` both webhook routes with no secret header → expect `401`.
2. `curl` both routes with the correct secret but a missing required field →
   expect `400`.
3. `curl` both routes with a valid body → expect a fast `202` (well under a
   second — if this is slow, the background-thread handoff isn't working).
4. Confirm the actual review completes a little later (poll the Confluence
   page's comments, or the Jira ticket's comments) and that its content is
   the grader-approved analysis, not an error message.
5. For the Jira route specifically: confirm **both** write-backs land (the
   ticket comment and the Confluence comment), from a single request.
6. For Slack: confirm a DM gets a reply, a channel `@mention` with a pasted
   document gets a reply in-thread, and a short `@mention` reply under a
   previously-posted document correctly uses that document instead of
   asking for one.

---

## 7. Explicit non-requirements

Don't build any of these unless asked — they're out of scope for a working
version of this:

- No database. A throwaway per-run workspace directory is enough state.
- No queue/worker system. A background thread per request is enough at this
  scale.
- No OAuth flow for the webhook. A shared-secret header is appropriate here.
- No retry/backoff logic for failed API calls — surface the failure as a
  written-back comment instead.
- No changes to `deepagent_helpers.py`, `AGENTS.md`, or the rubric.
