"""
Dashboard HTTP handler — serves the HTML dashboard and JSON API endpoints.
Designed to be mixed into the existing webhook server.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import unquote

from scripts import db

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_server_start_time = time.time()
_update_lock = threading.Lock()
_update_thread: threading.Thread | None = None
_update_state = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "message": "",
    "steps": [],
    "error": "",
}


def handle_dashboard_request(handler, method="GET") -> bool:
    """Handle dashboard-related requests. Returns True if handled, False otherwise."""
    path = handler.path.split("?")[0]

    if method == "POST":
        post_routes = {
            "/api/settings": _api_settings_save,
            "/api/cancel": _api_cancel,
            "/api/cancel-preview": _api_cancel_preview,
            "/api/preview-ticket": _api_preview_ticket,
            "/api/create-ticket": _api_create_ticket,
            "/api/rerun-ticket": _api_rerun_ticket,
            "/api/remove-queued": _api_remove_queued,
            "/api/ticket-feedback": _api_ticket_feedback,
            "/api/app-update": _api_app_update_start,
        }
        route_fn = post_routes.get(path)
        if route_fn:
            route_fn(handler)
            return True
        return False

    routes = {
        "/dashboard": _serve_html,
        "/dashboard/settings": _serve_html,
        "/favicon.svg": _serve_favicon,
        "/favicon.ico": _serve_favicon,
        "/site.webmanifest": _serve_manifest,
        "/api/status": _api_status,
        "/api/queue": _api_queue,
        "/api/tickets": _api_tickets,
        "/api/events": _api_events,
        "/api/prs": _api_prs,
        "/api/errors": _api_errors,
        "/api/stats": _api_stats,
        "/api/webhook-health": _api_webhook_health,
        "/api/settings": _api_settings_get,
        "/api/app-update": _api_app_update_status,
        "/api/stream": _api_stream,
        "/api/jira-projects": _api_jira_projects,
        "/api/preview-jobs": _api_preview_jobs,
    }

    route_fn = routes.get(path)
    if route_fn:
        route_fn(handler)
        return True

    # Dynamic routes
    if path.startswith("/api/logs/"):
        _api_logs(handler, path[len("/api/logs/"):])
        return True
    if path.startswith("/api/preview-jobs/"):
        _api_preview_job_detail(handler, path[len("/api/preview-jobs/"):])
        return True
    if path.startswith("/api/preview-logs/"):
        _api_preview_logs(handler, path[len("/api/preview-logs/"):])
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
    handler.send_header("Link", '</favicon.svg>; rel="icon"; type="image/svg+xml"; sizes="any"')
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)


def _serve_favicon(handler):
    icon_path = os.path.join(STATIC_DIR, "favicon.svg")
    if not os.path.exists(icon_path):
        handler.send_response(404)
        handler.end_headers()
        return
    with open(icon_path, "rb") as f:
        content = f.read()
    handler.send_response(200)
    handler.send_header("Content-Type", "image/svg+xml")
    handler.send_header("Cache-Control", "public, max-age=86400")
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)


def _serve_manifest(handler):
    manifest_path = os.path.join(STATIC_DIR, "site.webmanifest")
    if not os.path.exists(manifest_path):
        handler.send_response(404)
        handler.end_headers()
        return
    with open(manifest_path, "rb") as f:
        content = f.read()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/manifest+json")
    handler.send_header("Cache-Control", "public, max-age=86400")
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
    issue_key = unquote(issue_key)
    query = handler.path.split("?", 1)[1] if "?" in handler.path else ""
    since_id = 0
    for param in query.split("&"):
        if param.startswith("since_id="):
            try:
                since_id = int(param.split("=", 1)[1])
            except ValueError:
                pass
    logs = db.get_ticket_logs(issue_key, since_id)
    _send_json(handler, {"logs": logs})


def _api_preview_jobs(handler):
    query = handler.path.split("?", 1)[1] if "?" in handler.path else ""
    limit = 100
    for param in query.split("&"):
        if param.startswith("limit="):
            try:
                limit = max(1, min(500, int(param.split("=", 1)[1])))
            except ValueError:
                pass
    _send_json(handler, {"jobs": db.get_preview_jobs(limit=limit)})


def _api_preview_job_detail(handler, job_id: str):
    job = db.get_preview_job(job_id)
    if not job:
        handler.send_response(404)
        handler.end_headers()
        return
    _send_json(handler, {"job": job})


def _api_preview_logs(handler, job_id: str):
    query = handler.path.split("?", 1)[1] if "?" in handler.path else ""
    since_id = 0
    for param in query.split("&"):
        if param.startswith("since_id="):
            try:
                since_id = int(param.split("=", 1)[1])
            except ValueError:
                pass
    from scripts.create_ticket_ai import preview_log_key
    logs = db.get_ticket_logs(preview_log_key(job_id), since_id)
    _send_json(handler, {"logs": logs})


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


def _api_app_update_status(handler):
    with _update_lock:
        update = dict(_update_state)
    _send_json(handler, {"update": update})


def _api_app_update_start(handler):
    global _update_thread

    active_reason = _active_work_reason()
    if active_reason:
        _send_json(handler, {"ok": False, "error": active_reason, "update": _current_update_state()})
        return

    with _update_lock:
        if _update_state["status"] in ("running", "restarting"):
            update = dict(_update_state)
        else:
            _update_state.update({
                "status": "running",
                "started_at": time.time(),
                "finished_at": None,
                "message": "Starting update",
                "steps": [],
                "error": "",
            })
            _update_thread = threading.Thread(target=_run_app_update, daemon=True, name="app-update")
            _update_thread.start()
            update = dict(_update_state)
    _send_json(handler, {"ok": True, "update": update})


def _current_update_state() -> dict:
    with _update_lock:
        return dict(_update_state)


def _active_work_reason() -> str:
    main_mod = sys.modules.get("__main__")
    proc = getattr(main_mod, "_current_proc", None) if main_mod else None
    if proc is not None and proc.poll() is None:
        key = getattr(main_mod, "_current_issue_key", "") if main_mod else ""
        return f"Cannot update while {key or 'a job'} is running."

    q = getattr(main_mod, "ticket_queue", None) if main_mod else None
    if q is not None and q.qsize() > 0:
        return f"Cannot update while {q.qsize()} job(s) are queued."

    status = db.get_worker_status()
    if status.get("current_ticket"):
        key = status["current_ticket"].get("issue_key") or "a job"
        return f"Cannot update while {key} is marked as running."
    if status.get("queue_size", 0) > 0:
        return f"Cannot update while {status['queue_size']} job(s) are queued."

    preview_ids = db.get_preview_job_ids_by_status(("queued", "processing"))
    if preview_ids:
        return f"Cannot update while {len(preview_ids)} ticket preview job(s) are pending."

    return ""


def _run_app_update():
    try:
        db.log_event(None, "app_update", "Remote app update started")
        git_step = _run_update_step("Pull latest app changes", ["git", "pull", "--ff-only"], PROJECT_ROOT, timeout=180)

        codex_cmd = _codex_update_command()
        if not codex_cmd:
            raise RuntimeError("Could not find npm. Set CODEX_UPDATE_COMMAND to the command that updates Codex on this server.")
        codex_step = _run_update_step("Update Codex CLI", codex_cmd, PROJECT_ROOT, timeout=900)

        message = "Update complete. Restarting server."
        _set_update_state(status="restarting", message=message, steps=[git_step, codex_step], finished_at=time.time())
        db.log_event(None, "app_update", message)
        time.sleep(1.0)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as exc:
        message = str(exc)
        _set_update_state(status="failed", message="Update failed", error=message, finished_at=time.time())
        db.log_event(None, "app_update_failed", message)


def _codex_update_command() -> list[str] | None:
    custom = os.environ.get("CODEX_UPDATE_COMMAND", "").strip()
    if custom:
        return shlex.split(custom)
    if shutil.which("npm"):
        return ["npm", "install", "-g", "@openai/codex@latest"]
    return None


def _run_update_step(label: str, cmd: list[str], cwd: str, timeout: int) -> dict:
    _set_update_state(message=label)
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    step = {
        "label": label,
        "command": " ".join(shlex.quote(part) for part in cmd),
        "returncode": result.returncode,
        "output": output[-4000:],
    }
    _append_update_step(step)
    if result.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {result.returncode}: {output[-1000:]}")
    return step


def _set_update_state(**changes):
    with _update_lock:
        _update_state.update(changes)


def _append_update_step(step: dict):
    with _update_lock:
        _update_state["steps"] = [*_update_state.get("steps", []), step]


# -- Default prompt templates -------------------------------------------------

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")


def _load_prompt(filename: str) -> str:
    path = os.path.join(_PROMPTS_DIR, filename)
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


DEFAULT_PROMPT_CONTEXT = _load_prompt("ticket_context.md")
DEFAULT_PROMPT_INSTRUCTIONS = _load_prompt("ticket_instructions.md")
DEFAULT_PROMPT_PR_COMMENT = _load_prompt("pr_comment.md")
DEFAULT_PROMPT_PR_REVIEW = _load_prompt("pr_review.md")
DEFAULT_PROMPT_PR_FIX_ALL = _load_prompt("pr_fix_all.md")
DEFAULT_PROMPT_CREATE_TICKET = _load_prompt("create_ticket.md")
DEFAULT_PROMPT_CODE_CONTEXT = _load_prompt("code_context.md")
DEFAULT_PROMPT_TICKET_FEEDBACK = _load_prompt("ticket_feedback.md")

SETTINGS_DEFAULTS = {
    "prompt_context": DEFAULT_PROMPT_CONTEXT,
    "prompt_instructions": DEFAULT_PROMPT_INSTRUCTIONS,
    "prompt_pr_comment": DEFAULT_PROMPT_PR_COMMENT,
    "prompt_pr_review": DEFAULT_PROMPT_PR_REVIEW,
    "prompt_pr_fix_all": DEFAULT_PROMPT_PR_FIX_ALL,
    "prompt_create_ticket": DEFAULT_PROMPT_CREATE_TICKET,
    "prompt_code_context": DEFAULT_PROMPT_CODE_CONTEXT,
    "prompt_ticket_feedback": DEFAULT_PROMPT_TICKET_FEEDBACK,
    "model": "",
    "effort": "medium",
    "reviewers": "",
    "create_ticket_model": "",
    "create_ticket_effort": "",
    "create_ticket_issue_types": "",
    "create_ticket_templates": "{}",
    "create_ticket_timeout": "",
    "repo_slug_map": "{}",
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


def _api_rerun_ticket(handler):
    import sys
    content_length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(content_length)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        handler.send_response(400)
        handler.end_headers()
        return
    issue_key = (payload.get("issue_key") or "").strip()
    if not issue_key:
        handler.send_response(400)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps({"error": "issue_key is required"}).encode())
        return
    main_mod = sys.modules.get("__main__")
    requeue_fn = getattr(main_mod, "requeue_ticket", None)
    if requeue_fn is None:
        _send_json(handler, {"ok": False, "error": "Requeue not available"})
        return
    requeue_fn(issue_key)
    _send_json(handler, {"ok": True, "issue_key": issue_key})


def _api_remove_queued(handler):
    import sys
    content_length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(content_length)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        handler.send_response(400)
        handler.end_headers()
        return
    issue_key = (payload.get("issue_key") or "").strip()
    if not issue_key:
        handler.send_response(400)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps({"error": "issue_key is required"}).encode())
        return
    main_mod = sys.modules.get("__main__")
    remove_fn = getattr(main_mod, "remove_queued_ticket", None)
    if remove_fn is None:
        _send_json(handler, {"ok": False, "error": "Remove not available"})
        return
    removed = remove_fn(issue_key)
    _send_json(handler, {"ok": removed, "issue_key": issue_key})


def _api_cancel_preview(handler):
    content_length = int(handler.headers.get("Content-Length", 0) or 0)
    payload = {}
    if content_length:
        body = handler.rfile.read(content_length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}

    job_id = (payload.get("job_id") or "").strip()
    if job_id:
        from scripts.create_ticket_ai import cancel_preview_job
        cancelled = cancel_preview_job(job_id)
        _send_json(handler, {"cancelled": cancelled, "job_id": job_id})
        return

    from scripts.create_ticket_ai import cancel_preview
    cancelled = cancel_preview()
    _send_json(handler, {"cancelled": cancelled})


def _api_jira_projects(handler):
    try:
        from scripts.create_ticket_ai import get_projects
        projects = get_projects()
        _send_json(handler, {"projects": projects})
    except Exception as exc:
        _send_json(handler, {"projects": [], "error": str(exc)})


def _api_preview_ticket(handler):
    content_length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(content_length)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        handler.send_response(400)
        handler.end_headers()
        return

    project_key = payload.get("project_key", "").strip()
    raw_descriptions = payload.get("descriptions", [])

    # Accept both plain strings and {text, enrich_with_code} objects
    ticket_inputs = []
    for item in raw_descriptions:
        if isinstance(item, dict):
            text = item.get("text", "").strip()
            enrich = bool(item.get("enrich_with_code", False))
            repos = item.get("code_context_repos", "").strip()
        else:
            text = str(item).strip()
            enrich = False
            repos = ""
        if text:
            ticket_inputs.append({"text": text, "enrich_with_code": enrich, "code_context_repos": repos})

    if not project_key or not ticket_inputs:
        handler.send_response(400)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps({"error": "project_key and descriptions are required"}).encode())
        return

    from scripts.create_ticket_ai import enqueue_preview_jobs
    jobs = enqueue_preview_jobs(project_key, ticket_inputs)
    _send_json(handler, {"ok": True, "jobs": jobs})


def _api_create_ticket(handler):
    content_length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(content_length)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        handler.send_response(400)
        handler.end_headers()
        return

    project_key = payload.get("project_key", "").strip()
    tickets = payload.get("tickets", [])

    if not project_key or not tickets:
        handler.send_response(400)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps({"error": "project_key and tickets are required"}).encode())
        return

    from scripts.create_ticket_ai import create_ticket_from_enhanced
    db.set_setting("create_ticket_project_key", project_key)

    results = []
    for ticket in tickets:
        preview_job_id = (ticket.get("preview_job_id") or "").strip()
        try:
            result = create_ticket_from_enhanced(
                project_key=project_key,
                summary=ticket.get("summary", "").strip(),
                description=ticket.get("description", "").strip(),
                issue_type=ticket.get("issue_type", "Story").strip(),
                components=[c for c in ticket.get("components", []) if c],
                assign_to_bot=bool(ticket.get("assign_to_bot", False)),
            )
            db.log_event(result["issue_key"], "created", f"Ticket created via dashboard: {result['summary']}")
            if preview_job_id:
                db.preview_job_ticket_created(preview_job_id, result["issue_key"], result["issue_url"])
            results.append({"ok": True, "result": result, "preview_job_id": preview_job_id})
        except Exception as exc:
            results.append({"ok": False, "error": str(exc), "preview_job_id": preview_job_id})

    _send_json(handler, {"ok": True, "results": results})


def _api_ticket_feedback(handler):
    import sys
    content_length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(content_length)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        handler.send_response(400)
        handler.end_headers()
        return
    issue_key = (payload.get("issue_key") or "").strip()
    feedback = (payload.get("feedback") or "").strip()
    if not issue_key or not feedback:
        handler.send_response(400)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps({"error": "issue_key and feedback are required"}).encode())
        return
    main_mod = sys.modules.get("__main__")
    queue_fn = getattr(main_mod, "queue_ticket_feedback", None)
    if queue_fn is None:
        _send_json(handler, {"ok": False, "error": "Feedback queue not available"})
        return
    job_key = queue_fn(issue_key, feedback)
    _send_json(handler, {"ok": True, "issue_key": issue_key, "job_key": job_key})


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
