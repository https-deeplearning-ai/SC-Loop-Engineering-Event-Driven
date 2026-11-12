"""A webhook receiver that runs the Requirements Review Agent from either a
real Jira ticket or a real Confluence page.

Two Atlassian Automation rules POST here (trigger: "Manual trigger", action:
"Send web request"). This is Lesson 4's verification loop
(`build_review_agent(rubric_iterations=...)` + `run_verification_loop_live`,
unchanged) behind two real triggers instead of a notebook cell: nothing about
the agent changes here, only how it's invoked and where the requirement
document comes from. The comment written back is the grader-approved result,
not the first draft.

The requirement document itself always lives in Confluence, written by
whoever owns the spec. Two distinct real moments call for two distinct
triggers:

- **Confluence** (`/confluence/webhook/analyze`, primary): the doc owner
  reviews their own page directly, any time, before a ticket even exists.
  Writes back a Confluence comment only - there's no ticket in this path.
- **Jira** (`/jira/webhook/analyze`, secondary): an engineer re-checks the
  spec behind a specific ticket, via the ticket's "remote link" to its
  Confluence page (Link -> Confluence page, from either product's UI).
  Writes back both a Jira comment and a Confluence comment, since both a
  ticket-specific and a doc-level record are useful here.

Neither path ever edits the requirement page's actual content - only adds a
comment next to it. A shared doc can be linked from more than one ticket, so
editing the source from a single ticket's trigger would make "whose edit
wins" meaningless; flagging ambiguity without silently rewriting it is the
same trust model the agent itself already enforces (see AGENTS.md).

Both automation webhooks can time out waiting for a synchronous response,
and verification passes mean the loop can take a minute or two, so both
routes respond immediately (202) and do the fetch + review + write-back in a
background thread.

Needs a public URL (Atlassian Cloud has to reach this from the outside):
run `ngrok http 5001` and point both Automation rules' webhooks at the
ngrok URL. Needs ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN / ATLASSIAN_SITE_URL
and ATLASSIAN_WEBHOOK_SECRET in .env (see .env.example). Each rule's "Send
web request" action must send a header X-Webhook-Secret with that same
value, and a JSON body of {"issueKey": "{{issue.key}}"} (Jira rule) or
{"pageId": "{{page.id}}"} (Confluence rule).

Run:
    python jira_webhook.py
"""

import os
import re
import shutil
import threading
from pathlib import Path
from uuid import uuid4

import mistune
import requests
from flask import Flask, jsonify, request

import deepagent_helpers as dh

REPO_ROOT = Path(__file__).resolve().parent
RUN_WORKSPACE_ROOT = REPO_ROOT / ".jira_webhook_workspace"

# Same cap used in L5_event_driven_no_notebook/automation.py: a handful of
# grader-send-back passes, not unlimited revision.
MAX_RUBRIC_ITERATIONS = 2

ATLASSIAN_SITE_URL = os.environ["ATLASSIAN_SITE_URL"].rstrip("/")
ATLASSIAN_AUTH = (os.environ["ATLASSIAN_EMAIL"], os.environ["ATLASSIAN_API_TOKEN"])
WEBHOOK_SECRET = os.environ["ATLASSIAN_WEBHOOK_SECRET"]

# A Confluence page URL looks like .../wiki/spaces/<SPACE>/pages/<ID>/<title>
# or the short form .../wiki/x/<tinyui-id>. Only the numeric-id form is
# resolved; that's what remote links created via native Jira<->Confluence
# linking use.
_PAGE_ID_RE = re.compile(r"/wiki/spaces/[^/]+/pages/(\d+)")


def _fresh_workspace() -> Path:
    """One throwaway workspace per run. Same pattern as slack_bot.py and
    lesson_5_automation_loop/automation.py's `_fresh_workspace`.
    """
    run_dir = RUN_WORKSPACE_ROOT / uuid4().hex[:8]
    (run_dir / "memories").mkdir(parents=True, exist_ok=True)
    shutil.copy(dh.AGENTS_BASELINE, run_dir / "memories" / "AGENTS.md")
    return run_dir


def _linked_confluence_page_id(issue_key: str) -> str | None:
    """The Confluence page id behind a ticket's remote link, or None.

    Reads the issue's remote links (Jira's native cross-link feature) rather
    than a custom field, since that's what "the ticket links to the
    requirement" means in real Jira/Confluence. If more than one is linked,
    the first Confluence link wins - good enough for one requirement per
    ticket.
    """
    resp = requests.get(
        f"{ATLASSIAN_SITE_URL}/rest/api/3/issue/{issue_key}/remotelink",
        auth=ATLASSIAN_AUTH,
    )
    resp.raise_for_status()
    for link in resp.json():
        match = _PAGE_ID_RE.search(link["object"]["url"])
        if match:
            return match.group(1)
    return None


def _confluence_page_text(page_id: str) -> str:
    """The page body as plain(ish) text: storage format's HTML tags
    stripped. Good enough for a requirement doc, which is short paragraphs
    with no tables or macros.
    """
    resp = requests.get(
        f"{ATLASSIAN_SITE_URL}/wiki/api/v2/pages/{page_id}",
        auth=ATLASSIAN_AUTH,
        params={"body-format": "storage"},
    )
    resp.raise_for_status()
    html = resp.json()["body"]["storage"]["value"]
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _post_jira_comment(issue_key: str, body_text: str) -> None:
    """Write the analysis back as a Jira comment, in Atlassian Document
    Format (the only body format the comment endpoint accepts).
    """
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            for line in body_text.splitlines()
            if line.strip()
        ],
    }
    resp = requests.post(
        f"{ATLASSIAN_SITE_URL}/rest/api/3/issue/{issue_key}/comment",
        auth=ATLASSIAN_AUTH,
        json={"body": adf},
    )
    resp.raise_for_status()


def _post_confluence_comment(page_id: str, body_text: str) -> None:
    """Write the analysis back as a comment on the Confluence page itself
    (never into the page body - see the module docstring on why editing the
    requirement is out of scope).

    Confluence's storage format is HTML, and the analysis is markdown, so
    this reuses `mistune.html(...)` - already a project dependency, already
    used the same way in visualization_helpers.py - rather than hand-rolling
    a renderer the way slack_bot.py had to for Slack's non-standard mrkdwn.
    """
    resp = requests.post(
        f"{ATLASSIAN_SITE_URL}/wiki/rest/api/content",
        auth=ATLASSIAN_AUTH,
        json={
            "type": "comment",
            "container": {"id": page_id, "type": "page"},
            "body": {"storage": {"value": mistune.html(body_text), "representation": "storage"}},
        },
    )
    resp.raise_for_status()


def _run_review(requirement_doc: str, ticket_id: str) -> str:
    """Shared by both triggers below: the unchanged verification loop, run in
    a throwaway workspace. Returns the grader-approved analysis markdown, not
    the first draft.
    """
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
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    return run.analysis_markdown


def _review_ticket(issue_key: str) -> None:
    """The /jira/webhook/analyze background-thread body: resolve the ticket's
    linked Confluence page, review it, write back to both the ticket and the
    page. Runs after the webhook has already returned 202, so nothing here
    can make the automation rule report a timeout.
    """
    try:
        page_id = _linked_confluence_page_id(issue_key)
        if page_id is None:
            _post_jira_comment(
                issue_key,
                "Requirements Review Agent: no linked Confluence page found "
                "on this ticket, so there was nothing to review.",
            )
            return
        requirement_doc = _confluence_page_text(page_id)
        analysis_markdown = _run_review(requirement_doc, ticket_id=issue_key)
        _post_jira_comment(issue_key, analysis_markdown)
        _post_confluence_comment(page_id, analysis_markdown)
    except Exception as e:
        _post_jira_comment(issue_key, f"Requirements Review Agent: the review failed: {e}")


def _review_page(page_id: str) -> None:
    """The /confluence/webhook/analyze background-thread body: review the
    page directly - it is the requirement doc, no remote-link lookup needed
    - and write back only to the page. There's no ticket in this path.
    """
    try:
        requirement_doc = _confluence_page_text(page_id)
        analysis_markdown = _run_review(requirement_doc, ticket_id=f"CONF-{page_id}")
        _post_confluence_comment(page_id, analysis_markdown)
    except Exception as e:
        _post_confluence_comment(page_id, f"Requirements Review Agent: the review failed: {e}")


app = Flask(__name__)


def _check_secret():
    return request.headers.get("X-Webhook-Secret") == WEBHOOK_SECRET


@app.route("/jira/webhook/analyze", methods=["POST"])
def analyze_ticket():
    if not _check_secret():
        return jsonify(error="unauthorized"), 401

    issue_key = (request.get_json(silent=True) or {}).get("issueKey")
    if not issue_key:
        return jsonify(error="missing issueKey"), 400

    threading.Thread(target=_review_ticket, args=(issue_key,), daemon=True).start()
    return jsonify(status="started", issueKey=issue_key), 202


@app.route("/confluence/webhook/analyze", methods=["POST"])
def analyze_page():
    if not _check_secret():
        return jsonify(error="unauthorized"), 401

    page_id = (request.get_json(silent=True) or {}).get("pageId")
    if not page_id:
        return jsonify(error="missing pageId"), 400

    threading.Thread(target=_review_page, args=(str(page_id),), daemon=True).start()
    return jsonify(status="started", pageId=page_id), 202


if __name__ == "__main__":
    print("Webhook receiver starting on :5001 (Jira + Confluence routes) ... point ngrok at it.")
    app.run(port=5001, threaded=True)
