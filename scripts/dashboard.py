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


def handle_dashboard_request(handler) -> bool:
    """Handle dashboard-related requests. Returns True if handled, False otherwise."""
    path = handler.path.split("?")[0]

    routes = {
        "/dashboard": _serve_html,
        "/api/status": _api_status,
        "/api/queue": _api_queue,
        "/api/tickets": _api_tickets,
        "/api/events": _api_events,
        "/api/prs": _api_prs,
        "/api/errors": _api_errors,
        "/api/stats": _api_stats,
        "/api/webhook-health": _api_webhook_health,
        "/api/stream": _api_stream,
    }

    route_fn = routes.get(path)
    if route_fn:
        route_fn(handler)
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


def _api_webhook_health(handler):
    _send_json(handler, db.get_webhook_health())


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
