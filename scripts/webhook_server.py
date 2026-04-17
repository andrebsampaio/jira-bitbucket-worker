#!/usr/bin/env python3
"""
JIRA webhook listener and Bitbucket PR comment bot.
Triggers codex when a ticket is assigned to the configured user,
or when the bot is mentioned in a Bitbucket PR comment.
Jobs are queued and processed sequentially, one at a time.
"""

import base64
import hashlib
import hmac
import json
import os
import queue
import signal
import subprocess
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

from dotenv import load_dotenv

load_dotenv()

# Ensure project root is on sys.path so "scripts" package is importable
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts import db
from scripts.dashboard import handle_dashboard_request

TRIGGER_ASSIGNEE = os.environ["TRIGGER_ASSIGNEE"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8080"))
BOT_MENTION = os.environ.get("BOT_MENTION", "@andrebot")
BITBUCKET_USER = os.environ.get("BITBUCKET_USER", os.environ.get("JIRA_USER", ""))

# Typed queue: ("ticket", issue_key) or ("pr_comment", workspace, repo_slug, pr_id, comment_id)
ticket_queue: queue.Queue[tuple] = queue.Queue()

# Current subprocess — set by the worker, read by cancel_current_job()
_current_proc: subprocess.Popen | None = None
_current_issue_key: str | None = None
_proc_lock = threading.Lock()


def requeue_ticket(issue_key: str) -> bool:
    """Re-queue a finished ticket for reprocessing. Returns True if queued."""
    db.ticket_queued(issue_key)
    ticket_queue.put(("ticket", issue_key))
    return True


def queue_ticket_feedback(issue_key: str, feedback: str) -> str:
    """Queue a feedback job for all PRs of a ticket. Returns the feedback job key."""
    hex_id = os.urandom(3).hex()
    job_key = f"{issue_key}-FB-{hex_id}"
    feedback_b64 = base64.b64encode(feedback.encode()).decode()
    db.ticket_queued(job_key, summary=f"Feedback for {issue_key}")
    ticket_queue.put(("ticket_feedback", issue_key, job_key, feedback_b64))
    return job_key


def remove_queued_ticket(issue_key: str) -> bool:
    """Cancel a queued (not yet started) ticket. Returns True if it was queued."""
    return db.ticket_remove_queued(issue_key)


def cancel_current_job() -> str | None:
    """Kill the current process_ticket subprocess and all its children. Returns the issue key or None."""
    with _proc_lock:
        proc = _current_proc
        key = _current_issue_key
    if proc is None or key is None:
        return None
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    return key


def verify_signature(body: bytes, signature_header: str) -> bool:
    if not WEBHOOK_SECRET:
        return True
    expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    # Jira sends the raw hex digest; GitHub prefixes with "sha256="
    sig = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, sig)


def worker():
    """Single worker thread — processes one job at a time."""
    global _current_proc, _current_issue_key
    while True:
        job = ticket_queue.get()
        job_type = job[0]

        if job_type == "ticket":
            issue_key = job[1]
            cmd = ["python3", "scripts/process_ticket.py", issue_key]
        elif job_type == "pr_comment":
            _, workspace, repo_slug, pr_id, comment_id = job
            issue_key = f"PR-{repo_slug}#{pr_id}-C{comment_id}"
            cmd = ["python3", "scripts/process_pr_comment.py", workspace, repo_slug, pr_id, comment_id]
        elif job_type == "ticket_feedback":
            _, original_key, job_key, feedback_b64 = job
            issue_key = job_key
            cmd = ["python3", "scripts/process_ticket_feedback.py", original_key, job_key, feedback_b64]
        else:
            print(f"[worker] Unknown job type: {job_type}")
            ticket_queue.task_done()
            continue

        # Skip jobs that were removed from the queue before they started.
        if db.ticket_is_cancelled(issue_key):
            print(f"[worker] Skipping cancelled job {issue_key}")
            ticket_queue.task_done()
            continue

        print(f"[worker] Processing {issue_key} (queue size: {ticket_queue.qsize()} remaining)")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=PROJECT_ROOT,
                start_new_session=True,
            )
            with _proc_lock:
                _current_proc = proc
                _current_issue_key = issue_key
            returncode = proc.wait()
            with _proc_lock:
                _current_proc = None
                _current_issue_key = None
            if returncode == 0:
                print(f"[worker] Finished {issue_key}")
            elif returncode < 0:
                # Negative return code = killed by signal (cancel)
                print(f"[worker] {issue_key} was cancelled (signal {-returncode})")
                db.ticket_cancelled(issue_key)
            else:
                script = cmd[1].split("/")[-1]
                raise subprocess.CalledProcessError(returncode, script)
        except subprocess.CalledProcessError as e:
            tb = traceback.format_exc()
            print(f"[worker] codex failed for {issue_key}:\n{tb}")
            db.ticket_finished(issue_key, error=tb)
        finally:
            ticket_queue.task_done()


class JiraWebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if handle_dashboard_request(self):
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if handle_dashboard_request(self, method="POST"):
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        db.webhook_received()

        signature = self.headers.get("X-Hub-Signature", "")
        if WEBHOOK_SECRET and not verify_signature(body, signature):
            db.webhook_sig_failure()
            db.log_event(None, "sig_failure", "Webhook signature validation failed")
            self.send_response(401)
            self.end_headers()
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        self.send_response(200)
        self.end_headers()

        event_key = self.headers.get("X-Event-Key", "")
        threading.Thread(target=handle_event, args=(payload, event_key), daemon=True).start()

    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            pass

    def log_message(self, format, *args):
        print(f"[webhook] {self.address_string()} - {format % args}")


def handle_event(payload: dict, event_key: str = ""):
    # Bitbucket PR comment event
    if event_key == "pullrequest:comment_created":
        handle_pr_comment_event(payload)
        return

    # JIRA event (has webhookEvent field)
    event = payload.get("webhookEvent", "")
    if event != "jira:issue_updated":
        return

    changelog = payload.get("changelog", {})
    items = changelog.get("items", [])
    assignee_change = next((i for i in items if i.get("field") == "assignee"), None)
    if not assignee_change:
        return

    new_assignee = assignee_change.get("to") or assignee_change.get("toString", "")
    if new_assignee != TRIGGER_ASSIGNEE:
        return

    issue_key = payload.get("issue", {}).get("key")
    if not issue_key:
        print("[webhook] No issue key in payload")
        return

    print(f"[webhook] Queuing {issue_key} (queue size: {ticket_queue.qsize()})")
    db.ticket_queued(issue_key)
    ticket_queue.put(("ticket", issue_key))


def handle_pr_comment_event(payload: dict):
    """Handle Bitbucket pullrequest:comment_created webhook."""
    comment = payload.get("comment", {})
    comment_body = comment.get("content", {}).get("raw", "")

    # Ignore if bot is not mentioned
    if BOT_MENTION not in comment_body:
        return

    # Ignore comments authored by the bot itself (prevent loops)
    comment_author = comment.get("user", {}).get("username", "")
    if comment_author and comment_author == BITBUCKET_USER:
        print("[webhook] Ignoring comment authored by bot user")
        return

    # Only process comments on OPEN PRs
    pr = payload.get("pullrequest", {})
    pr_state = pr.get("state", "").upper()
    if pr_state != "OPEN":
        print(f"[webhook] Ignoring comment on PR in state: {pr_state}")
        return

    # Extract identifiers
    pr_id = str(pr.get("id", ""))
    comment_id = str(comment.get("id", ""))
    repo = payload.get("repository", {})
    repo_slug = (repo.get("name", "") or repo.get("full_name", "").split("/")[-1]).replace(" ", "-").lower()
    workspace = repo.get("full_name", "").split("/")[0] if "/" in repo.get("full_name", "") else ""

    if not all([pr_id, comment_id, repo_slug, workspace]):
        print("[webhook] Missing identifiers in PR comment payload")
        return

    issue_key = f"PR-{repo_slug}#{pr_id}-C{comment_id}"
    comment_text = comment_body[:200]
    print(f"[webhook] Queuing PR comment job {issue_key} (queue size: {ticket_queue.qsize()})")
    db.ticket_queued(issue_key, summary=comment_text)
    ticket_queue.put(("pr_comment", workspace, repo_slug, pr_id, comment_id))


if __name__ == "__main__":
    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    def _shutdown(signum, frame):
        """Kill any running child process group before exiting."""
        cancel_current_job()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    server = ThreadingHTTPServer(("0.0.0.0", WEBHOOK_PORT), JiraWebhookHandler)
    print(f"[webhook] Listening on port {WEBHOOK_PORT}")
    server.serve_forever()
