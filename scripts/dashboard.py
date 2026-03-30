"""
Dashboard HTTP handler — serves the HTML dashboard and JSON API endpoints.
Designed to be mixed into the existing webhook server.
"""

import json
import os
import time

from scripts import db

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_server_start_time = time.time()


def handle_dashboard_request(handler, method="GET") -> bool:
    """Handle dashboard-related requests. Returns True if handled, False otherwise."""
    path = handler.path.split("?")[0]

    if method == "POST":
        post_routes = {
            "/api/settings": _api_settings_save,
            "/api/cancel": _api_cancel,
        }
        route_fn = post_routes.get(path)
        if route_fn:
            route_fn(handler)
            return True
        return False

    routes = {
        "/dashboard": _serve_html,
        "/dashboard/settings": _serve_html,
        "/api/status": _api_status,
        "/api/queue": _api_queue,
        "/api/tickets": _api_tickets,
        "/api/events": _api_events,
        "/api/prs": _api_prs,
        "/api/errors": _api_errors,
        "/api/stats": _api_stats,
        "/api/webhook-health": _api_webhook_health,
        "/api/settings": _api_settings_get,
        "/api/stream": _api_stream,
    }

    route_fn = routes.get(path)
    if route_fn:
        route_fn(handler)
        return True

    # Dynamic routes
    if path.startswith("/api/logs/"):
        _api_logs(handler, path[len("/api/logs/"):])
        return True

    return False


def _send_json(handler, data):
    body = json.dumps(data, default=str).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _serve_html(handler):
    html_path = os.path.join(STATIC_DIR, "dashboard.html")
    with open(html_path, "rb") as f:
        content = f.read()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)


def _api_status(handler):
    status = db.get_worker_status()
    status["uptime_seconds"] = round(time.time() - _server_start_time, 1)
    _send_json(handler, status)


def _api_queue(handler):
    _send_json(handler, db.get_queue())


def _api_tickets(handler):
    _send_json(handler, db.get_recent_tickets())


def _api_events(handler):
    _send_json(handler, db.get_recent_events())


def _api_prs(handler):
    _send_json(handler, db.get_pull_requests())


def _api_errors(handler):
    _send_json(handler, db.get_errors())


def _api_stats(handler):
    _send_json(handler, db.get_stats())


def _api_logs(handler, issue_key: str):
    """Return log lines for a ticket. Supports ?since_id=N for incremental fetches."""
    query = handler.path.split("?", 1)[1] if "?" in handler.path else ""
    since_id = 0
    for param in query.split("&"):
        if param.startswith("since_id="):
            try:
                since_id = int(param.split("=", 1)[1])
            except ValueError:
                pass
    _send_json(handler, db.get_ticket_logs(issue_key, since_id))


def _api_webhook_health(handler):
    _send_json(handler, db.get_webhook_health())


def _api_cancel(handler):
    import sys
    # The server runs as __main__, so importing scripts.webhook_server would
    # create a separate module with its own globals (where _current_proc is
    # always None).  Reach into the actual running module instead.
    main_mod = sys.modules.get("__main__")
    cancel_current_job = getattr(main_mod, "cancel_current_job", None)
    if cancel_current_job is None:
        _send_json(handler, {"cancelled": False, "error": "Cancel not available"})
        return
    issue_key = cancel_current_job()
    if issue_key:
        _send_json(handler, {"cancelled": True, "issue_key": issue_key})
    else:
        _send_json(handler, {"cancelled": False, "error": "No job is currently running"})


# -- Default prompt templates -------------------------------------------------

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")


def _load_prompt(filename: str) -> str:
    path = os.path.join(_PROMPTS_DIR, filename)
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


DEFAULT_PROMPT_CONTEXT = _load_prompt("ticket_context.md")
DEFAULT_PROMPT_INSTRUCTIONS = _load_prompt("ticket_instructions.md")
DEFAULT_PROMPT_PR_COMMENT = _load_prompt("pr_comment.md")

SETTINGS_DEFAULTS = {
    "prompt_context": DEFAULT_PROMPT_CONTEXT,
    "prompt_instructions": DEFAULT_PROMPT_INSTRUCTIONS,
    "prompt_pr_comment": DEFAULT_PROMPT_PR_COMMENT,
    "model": "",
    "effort": "medium",
}


def _api_settings_get(handler):
    saved = db.get_all_settings()
    # Merge defaults with saved values
    result = {k: saved.get(k, v) for k, v in SETTINGS_DEFAULTS.items()}
    # Include any extra saved keys not in defaults
    result.update({k: v for k, v in saved.items() if k not in result})
    _send_json(handler, {"settings": result, "defaults": SETTINGS_DEFAULTS})


def _api_settings_save(handler):
    content_length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(content_length)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        handler.send_response(400)
        handler.end_headers()
        return
    settings = payload.get("settings", {})
    for key, value in settings.items():
        db.set_setting(key, value)
    _send_json(handler, {"ok": True})


def _api_stream(handler):
    """Server-Sent Events endpoint for live updates."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()

    q = db.subscribe()
    try:
        while True:
            if q:
                msg = q.pop(0)
                event_name = msg["event"]
                data = json.dumps(msg["data"], default=str)
                handler.wfile.write(f"event: {event_name}\ndata: {data}\n\n".encode())
                handler.wfile.flush()
            else:
                # Send keepalive every 15 seconds
                handler.wfile.write(b": keepalive\n\n")
                handler.wfile.flush()
                time.sleep(1)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        db.unsubscribe(q)
