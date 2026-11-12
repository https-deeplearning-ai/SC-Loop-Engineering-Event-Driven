"""A Slack bot that runs the Requirements Review Agent on demand.

Either mention the bot in a channel it's been invited to, or just DM it
directly, with a requirement document as the message text. It replies (in
the thread, for a channel mention) with the agent's analysis. This is
Lesson 4's verification loop (`build_review_agent(rubric_iterations=...)` +
`run_verification_loop_live`, unchanged) behind a Slack trigger instead of a
notebook cell: nothing about the agent changes here, only how it's invoked.
The reply is the grader-approved result, not the first draft - the same
agent the course built the verification loop on top of, not the bare
execution loop underneath it.

A mention can also be short or empty, as a reply in a thread whose parent
message is the actual document - post the doc, then just @mention the bot
underneath it. See MENTION_FALLBACK_CHARS / `_thread_parent_text`.

Socket Mode: no public URL, no webhook endpoint, no signing secret to verify.
The bot opens an outbound websocket to Slack and receives events over it,
which is what makes this runnable from a laptop during a recording.

Setup: see the Slack app walkthrough in lesson_5_automation_loop's extension
notes. Needs SLACK_BOT_TOKEN (xoxb-...) and SLACK_APP_TOKEN (xapp-...) in
.env (see .env.example), the `app_mentions:read` / `chat:write` / `im:history`
/ `channels:history` (add `groups:history` too for private channels) bot
scopes, and the Messages Tab turned on under App Home (DMs are silently
dropped by Slack otherwise, not just unhandled by this script).

Run:
    python slack_bot.py
"""

import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import deepagent_helpers as dh

REPO_ROOT = Path(__file__).resolve().parent
RUN_WORKSPACE_ROOT = REPO_ROOT / ".slack_bot_workspace"

# Same cap used in L5_event_driven_no_notebook/automation.py: a handful of
# grader-send-back passes, not unlimited revision.
MAX_RUBRIC_ITERATIONS = 2

# One emoji per heading in AGENTS.md's fixed analysis.md skeleton (the title,
# then its five "## " sections, in order). Anything outside that fixed set
# falls back to _DEFAULT_SECTION_EMOJI rather than guessing - this is a
# lookup table, not a model call, so it only knows the headings that exist
# today.
_SECTION_EMOJI = {
    "Requirements Analysis": "📋",
    "Confirmed Facts": "✅",
    "Ambiguities": "❓",
    "Clarification Questions": "💬",
    "Assumptions": "🧩",
    "Summary": "📝",
}
_DEFAULT_SECTION_EMOJI = "▪️"

# Below this length, a mention's own text is treated as a trigger ("@bot" or
# "@bot please review") rather than a pasted document, and the thread's
# parent message is used instead. A real requirement document runs well
# past this; a bare mention does not.
MENTION_FALLBACK_CHARS = 20

_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

# Slack's plain-text `chat.postMessage` rendering collapses genuinely empty
# lines, so a blank line from the source markdown doesn't survive as visible
# space - confirmed by testing against a real workspace, not an assumption.
# Two substitutes, for two different scales of gap:
_SECTION_DIVIDER = "─" * 20  # between "## " sections: a real visible rule
_SPACER = " "  # between bullets in one section: a line that *looks*
                     # blank but isn't - it holds a single non-breaking
                     # space, which Slack does not treat as empty.


def _md_bold_to_slack(text: str) -> str:
    """**bold** (the agent's markdown) -> *bold* (Slack's mrkdwn)."""
    return _MD_BOLD_RE.sub(r"*\1*", text)


def _format_for_slack(markdown: str) -> str:
    """A Slack-native rendering of analysis.md: headings become bold +
    emoji with a rule between sections, "- " bullets become Slack bullets
    with a spacer between them, the indented "Reason:" / "Resolved by:" /
    "Edge case:" lines under each bullet become nested sub-points, and
    **bold** becomes Slack's *bold*.

    Purely cosmetic, and deliberately simple (line-based, not a markdown
    parser) because the input shape is fixed by AGENTS.md's skeleton, not
    arbitrary markdown. The agent's own output is never touched - this
    builds a second string for display; `_review_and_reply` sends the real
    one back verbatim too, in a code block, right under this.
    """
    lines = []
    in_bullet_run = False  # whether the line just emitted was part of a
                            # bullet (its own line, or a sub-point under it)

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("# ") and not stripped.startswith("##"):
            title = stripped[2:].strip()
            emoji = _SECTION_EMOJI.get(title, _DEFAULT_SECTION_EMOJI)
            lines.append(f"{emoji}  *{title}* {emoji}")
            in_bullet_run = False
        elif stripped.startswith("## "):
            heading = stripped[3:].strip()
            emoji = _SECTION_EMOJI.get(heading, _DEFAULT_SECTION_EMOJI)
            lines.append(_SECTION_DIVIDER)
            lines.append(f"{emoji}  *{heading}*")
            in_bullet_run = False
        elif stripped.startswith("- "):
            if in_bullet_run:
                lines.append(_SPACER)
            lines.append("• " + _md_bold_to_slack(stripped[2:].strip()))
            in_bullet_run = True
        elif line.startswith("  ") and stripped:
            # An indented continuation line under the bullet above (Reason:/
            # Resolved by:/Edge case:), not a new top-level bullet.
            lines.append("    ↳ " + _md_bold_to_slack(stripped))
        elif stripped:
            lines.append(_md_bold_to_slack(stripped))
            in_bullet_run = False
        # A genuinely blank source line is dropped rather than kept: the
        # divider and spacer above already do that job, deliberately, since
        # a bare blank line doesn't survive Slack's rendering anyway.
    return "\n".join(lines)


def _raw_markdown_message(analysis_markdown: str) -> str:
    """The raw markdown, verbatim, in its own code block - a separate
    message from the pretty rendering (see `_review_and_reply`), not
    concatenated onto it. Slack silently splits an overly long single
    message into two at some internal length limit; when that split landed
    inside one shared code fence, the second half's closing ``` had no
    opening in the same message and rendered as plain text instead of a
    code block. Two independently fenced messages can't break that way.
    """
    return f"_Raw Markdown (for copy/paste):_\n```\n{analysis_markdown}\n```"


def _fresh_workspace() -> Path:
    """One throwaway workspace per message, seeded with the tracked AGENTS.md.

    Same pattern as lesson_5_automation_loop/automation.py's
    `_fresh_workspace`: `build_review_agent` requires memories/AGENTS.md to
    already exist in the workspace it's given, and a fresh directory per
    message means two mentions answered back to back never see each other's
    leftover /analysis.md.
    """
    run_dir = RUN_WORKSPACE_ROOT / uuid4().hex[:8]
    (run_dir / "memories").mkdir(parents=True, exist_ok=True)
    shutil.copy(dh.AGENTS_BASELINE, run_dir / "memories" / "AGENTS.md")
    return run_dir


def _requirement_text(event_text: str) -> str:
    """Strip the leading "<@BOTID>" mention, keep everything typed after it."""
    return event_text.split(">", 1)[-1].strip()


def _thread_parent_text(client, event) -> str | None:
    """The text of the message this mention is a thread reply to, or None.

    Handles "post the document, then @mention me underneath it with no
    other text" - a thread reply whose own text is too short to be the
    document itself. Returns None (rather than guessing) if this mention
    isn't inside a thread, if the parent is itself too short to plausibly
    be a document, or if the parent was posted by a bot (including this
    one, so a stray re-mention under the bot's own prior report doesn't
    get treated as a new requirement).
    """
    thread_ts = event.get("thread_ts")
    if not thread_ts or thread_ts == event["ts"]:
        return None  # not a reply - nothing to fall back to

    replies = client.conversations_replies(channel=event["channel"], ts=thread_ts)
    messages = replies.get("messages", [])
    if not messages:
        return None

    parent = messages[0]
    if "bot_id" in parent:
        return None

    parent_text = parent.get("text", "").strip()
    if len(parent_text) < MENTION_FALLBACK_CHARS:
        return None

    return parent_text


def _review_and_reply(requirement_doc: str, thread_ts: str, say) -> None:
    """Shared by both triggers below: run the loop, reply with the result.

    `thread_ts` is the message being responded to. In a channel this threads
    the reply under the mention; in a DM there's no thread to speak of, but
    Slack accepts the same `thread_ts` argument there without complaint.
    """
    if not requirement_doc:
        say(text="Send me a requirement document and I'll review it.", thread_ts=thread_ts)
        return

    say(text="On it, reviewing now - verification passes mean this can take a minute or two.", thread_ts=thread_ts)

    # One ticket id per Slack message, so each run's simulated-Jira entry is
    # distinct even if two messages arrive close together.
    ticket_id = f"SLACK-{thread_ts.replace('.', '')}"
    run_dir = _fresh_workspace()
    try:
        agent = dh.build_review_agent(
            rubric_iterations=MAX_RUBRIC_ITERATIONS,
            max_turns=40,  # several verification passes cost more turns than one review
            workspace_dir=str(run_dir),
        )
        run = dh.run_verification_loop_live(
            agent, requirement_doc, rubric=dh.DEFAULT_RUBRIC,
            workspace_dir=str(run_dir), ticket_id=ticket_id,
        )
    except Exception as e:
        say(text=f"The review failed: {e}", thread_ts=thread_ts)
        return
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)

    say(text=_format_for_slack(run.analysis_markdown), thread_ts=thread_ts)
    say(text=_raw_markdown_message(run.analysis_markdown), thread_ts=thread_ts)


app = App(token=os.environ["SLACK_BOT_TOKEN"])


@app.event("app_mention")
def handle_mention(event, say, client):
    requirement_doc = _requirement_text(event["text"])
    if len(requirement_doc) < MENTION_FALLBACK_CHARS:
        # Too short to plausibly be the document itself - check whether this
        # is a reply in a thread whose parent message actually is one.
        parent_text = _thread_parent_text(client, event)
        if parent_text:
            requirement_doc = parent_text
    _review_and_reply(requirement_doc, event["ts"], say)


@app.event("message")
def handle_dm(event, say):
    # Only act on direct messages. channel_type distinguishes a DM ("im")
    # from a channel message, which already has its own handler above.
    # subtype is set for things like message_changed/message_deleted/channel
    # joins, none of which are a human typing a new requirement. bot_id is
    # set on messages Slack delivers from a bot, including this bot's own
    # replies - without this check, the bot would receive its own analysis
    # as a new "message" event and try to review it, forever.
    if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id"):
        return
    _review_and_reply(event.get("text", "").strip(), event["ts"], say)


if __name__ == "__main__":
    print("Slack bot starting (Socket Mode)... mention it in a channel, or DM it directly.")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
