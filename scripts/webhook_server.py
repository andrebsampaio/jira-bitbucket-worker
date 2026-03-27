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
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv

load_dotenv()

TRIGGER_ASSIGNEE = os.environ["TRIGGER_ASSIGNEE"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8080"))

ticket_queue: queue.Queue[str] = queue.Queue()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def verify_signature(body: bytes, signature_header: str) -> bool:
    if not WEBHOOK_SECRET:
        return True
    expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


def worker():
    """Single worker thread — processes one ticket at a time."""
    while True:
        issue_key = ticket_queue.get()
        print(f"[worker] Processing {issue_key} (queue size: {ticket_queue.qsize()} remaining)")
        try:
            subprocess.run(
                ["python3", "scripts/process_ticket.py", issue_key],
                cwd=PROJECT_ROOT,
                check=True,
            )
            print(f"[worker] Finished {issue_key}")
        except subprocess.CalledProcessError as e:
            print(f"[worker] codex failed for {issue_key}: {e}")
        finally:
            ticket_queue.task_done()


class JiraWebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        signature = self.headers.get("X-Hub-Signature-256", "")
        if WEBHOOK_SECRET and not verify_signature(body, signature):
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
    ticket_queue.put(issue_key)


if __name__ == "__main__":
    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), JiraWebhookHandler)
    print(f"[webhook] Listening on port {WEBHOOK_PORT}")
    server.serve_forever()
