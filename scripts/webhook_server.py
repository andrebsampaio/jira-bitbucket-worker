#!/usr/bin/env python3
"""
JIRA webhook listener. Triggers codex when a ticket is assigned to the configured user.
Tickets are queued and processed sequentially, one at a time.
"""

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

ticket_queue: queue.Queue[str] = queue.Queue()

# Current subprocess — set by the worker, read by cancel_current_job()
_current_proc: subprocess.Popen | None = None
_current_issue_key: str | None = None
_proc_lock = threading.Lock()


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
    """Single worker thread — processes one ticket at a time."""
    global _current_proc, _current_issue_key
    while True:
        issue_key = ticket_queue.get()
        print(f"[worker] Processing {issue_key} (queue size: {ticket_queue.qsize()} remaining)")
        try:
            proc = subprocess.Popen(
                ["python3", "scripts/process_ticket.py", issue_key],
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
                raise subprocess.CalledProcessError(returncode, "process_ticket.py")
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

        threading.Thread(target=handle_event, args=(payload,), daemon=True).start()

    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            pass

    def log_message(self, format, *args):
        print(f"[webhook] {self.address_string()} - {format % args}")


def handle_event(payload: dict):
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
    ticket_queue.put(issue_key)


if __name__ == "__main__":
    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", WEBHOOK_PORT), JiraWebhookHandler)
    print(f"[webhook] Listening on port {WEBHOOK_PORT}")
    server.serve_forever()
