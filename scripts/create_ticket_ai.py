"""
AI-assisted JIRA ticket creation.

Uses Codex CLI to enhance a raw description into a structured ticket, then
creates it in JIRA and adds it to the active sprint when one is found.
"""

import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()

JIRA_URL = os.environ["JIRA_URL"].rstrip("/")
JIRA_USER = os.environ["JIRA_USER"]
JIRA_TOKEN = os.environ["JIRA_TOKEN"]
WORKSPACE_PATH = os.environ.get("WORKSPACE_PATH", tempfile.gettempdir())

# Directory of this project (a git repo, so Codex --full-auto can write files here)
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Codex temp outputs (gitignored); not the workspace root — see _enhance_with_codex docstring
_TICKET_DRAFT_DIR = os.path.join(_PROJECT_DIR, ".ticket_draft")


def _ensure_ticket_draft_dir() -> None:
    os.makedirs(_TICKET_DRAFT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Cancellable subprocess slot for preview runs
# ---------------------------------------------------------------------------

_preview_proc: subprocess.Popen | None = None
_preview_lock = threading.Lock()
_current_preview_job_key: str | None = None
_preview_job_queue: queue.Queue[str] = queue.Queue()
_preview_worker_thread: threading.Thread | None = None


class PreviewCancelledError(RuntimeError):
    pass


def cancel_preview(job_key: str | None = None) -> bool:
    """Kill the currently running preview Codex subprocess. Returns True if one was running."""
    with _preview_lock:
        proc = _preview_proc
        current_key = _current_preview_job_key
    if proc is None:
        return False
    if job_key and job_key != current_key:
        return False
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    return True


_PREVIEW_LOG_KEY = "__preview__"
_PREVIEW_LOG_PREFIX = "preview:"


def preview_log_key(job_id: str) -> str:
    return f"{_PREVIEW_LOG_PREFIX}{job_id}"


def _run_codex(
    cmd: list[str],
    cwd: str = WORKSPACE_PATH,
    *,
    log_key: str | None = None,
    clear_logs: bool = True,
    preview_job_key: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a Codex command, streaming each output line to the DB log and storing
    the process reference so it can be cancelled."""
    from scripts import db
    global _preview_proc

    log_key = log_key or _PREVIEW_LOG_KEY
    if clear_logs:
        db.clear_ticket_logs(log_key)

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    with _preview_lock:
        global _current_preview_job_key
        _preview_proc = proc
        _current_preview_job_key = preview_job_key

    lines: list[str] = []
    timeout_hit = False

    def _kill_on_timeout():
        nonlocal timeout_hit
        timeout_hit = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass

    try:
        timeout_secs = int(db.get_setting("create_ticket_timeout", "") or 600)
    except (ValueError, TypeError):
        timeout_secs = 600

    timer = threading.Timer(timeout_secs, _kill_on_timeout)
    timer.start()
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            db.log_line(log_key, line)
        proc.wait()
    finally:
        timer.cancel()
        with _preview_lock:
            _preview_proc = None
            _current_preview_job_key = None

    if timeout_hit:
        raise RuntimeError(f"Codex timed out after {timeout_secs} seconds")
    if proc.returncode in (-signal.SIGTERM, -signal.SIGKILL):
        raise PreviewCancelledError("Preview was cancelled")

    return subprocess.CompletedProcess(cmd, proc.returncode, "\n".join(lines), "")


def _new_preview_job_id() -> str:
    return f"pv-{uuid.uuid4().hex[:12]}"


def _new_preview_batch_id() -> str:
    return f"pvb-{uuid.uuid4().hex[:10]}"


def _ensure_preview_worker() -> None:
    global _preview_worker_thread
    if _preview_worker_thread and _preview_worker_thread.is_alive():
        return
    from scripts import db

    _preview_worker_thread = threading.Thread(target=_preview_worker_loop, daemon=True, name="preview-worker")
    _preview_worker_thread.start()
    # Requeue any jobs that were pending before the server started
    pending_ids = db.get_preview_job_ids_by_status(("queued", "processing"))
    for job_id in pending_ids:
        _preview_job_queue.put(job_id)


def _preview_worker_loop():
    from scripts import db

    while True:
        job_id = _preview_job_queue.get()
        try:
            job = db.get_preview_job(job_id)
            if not job or job.get("status") not in {"queued", "processing"}:
                continue
            db.preview_job_started(job_id)
            try:
                preview = enhance_ticket_description(
                    job["project_key"],
                    job["description"],
                    enrich_with_code=bool(job.get("enrich_with_code")),
                    code_context_repos=job.get("code_context_repos") or "",
                    log_key=preview_log_key(job_id),
                    preview_job_key=job_id,
                )
                db.preview_job_finished(job_id, preview)
            except PreviewCancelledError:
                db.preview_job_cancelled(job_id)
            except Exception as exc:
                db.preview_job_failed(job_id, str(exc))
        finally:
            _preview_job_queue.task_done()


def enqueue_preview_jobs(
    project_key: str,
    ticket_inputs: list[dict],
    *,
    batch_id: str | None = None,
) -> list[dict]:
    """Queue preview jobs for async processing."""
    from scripts import db

    valid_inputs = []
    for item in ticket_inputs:
        text = (item.get("text") or "").strip()
        if text:
            valid_inputs.append({**item, "text": text})
    if not valid_inputs:
        return []

    _ensure_preview_worker()
    batch = batch_id or _new_preview_batch_id()
    jobs: list[dict] = []

    for item in valid_inputs:
        description = item["text"]
        job_id = _new_preview_job_id()
        db.preview_job_created(
            job_id=job_id,
            batch_id=batch,
            project_key=project_key,
            description=description,
            enrich_with_code=bool(item.get("enrich_with_code")),
            code_context_repos=(item.get("code_context_repos") or "").strip(),
        )
        _preview_job_queue.put(job_id)
        jobs.append(db.get_preview_job(job_id))

    return jobs


def cancel_preview_job(job_id: str) -> bool:
    """Cancel a queued or running preview job."""
    from scripts import db

    job = db.get_preview_job(job_id)
    if not job:
        return False

    status = job.get("status")
    if status in {"done", "failed", "cancelled", "created"}:
        return False

    if status == "processing":
        cancelled = cancel_preview(job_id)
        if not cancelled:
            return False
        db.preview_job_cancelled(job_id)
        return True

    if status == "queued":
        db.preview_job_cancelled(job_id)
        return True

    return False


# ---------------------------------------------------------------------------
# JIRA helpers
# ---------------------------------------------------------------------------

def get_projects() -> list[dict]:
    url = f"{JIRA_URL}/rest/api/3/project/search"
    resp = requests.get(url, auth=(JIRA_USER, JIRA_TOKEN))
    resp.raise_for_status()
    return [{"key": p["key"], "name": p["name"]} for p in resp.json().get("values", [])]


def get_project_components(project_key: str) -> list[str]:
    url = f"{JIRA_URL}/rest/api/3/project/{project_key}/components"
    resp = requests.get(url, auth=(JIRA_USER, JIRA_TOKEN))
    resp.raise_for_status()
    return [c["name"] for c in resp.json()]


def get_issue_types(project_key: str) -> list[str]:
    url = f"{JIRA_URL}/rest/api/3/project/{project_key}"
    resp = requests.get(url, auth=(JIRA_USER, JIRA_TOKEN))
    resp.raise_for_status()
    subtask_names = {"Subtask", "Sub-task", "subtask"}
    return [
        it["name"]
        for it in resp.json().get("issueTypes", [])
        if it["name"] not in subtask_names
    ]


def _text_to_adf(text: str) -> dict:
    """Wrap plain text paragraphs in Atlassian Document Format."""
    content = []
    for para in text.split("\n\n"):
        para = para.strip()
        if para:
            content.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": para}],
            })
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]
    return {"type": "doc", "version": 1, "content": content}


def create_jira_ticket(
    project_key: str,
    summary: str,
    description_text: str,
    issue_type: str,
    component_names: list[str],
) -> str:
    """Create a JIRA ticket and return its key."""
    body: dict = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary,
            "description": _text_to_adf(description_text),
            "issuetype": {"name": issue_type},
        }
    }
    if component_names:
        body["fields"]["components"] = [{"name": n} for n in component_names]

    resp = requests.post(
        f"{JIRA_URL}/rest/api/3/issue",
        auth=(JIRA_USER, JIRA_TOKEN),
        json=body,
    )
    resp.raise_for_status()
    return resp.json()["key"]


def get_active_sprint_id(project_key: str) -> int | None:
    """Return the active sprint ID for the first board associated with the project."""
    resp = requests.get(
        f"{JIRA_URL}/rest/agile/1.0/board",
        params={"projectKeyOrId": project_key},
        auth=(JIRA_USER, JIRA_TOKEN),
    )
    if not resp.ok:
        return None
    boards = resp.json().get("values", [])
    if not boards:
        return None

    board_id = boards[0]["id"]
    resp = requests.get(
        f"{JIRA_URL}/rest/agile/1.0/board/{board_id}/sprint",
        params={"state": "active"},
        auth=(JIRA_USER, JIRA_TOKEN),
    )
    if not resp.ok:
        return None
    sprints = resp.json().get("values", [])
    return sprints[0]["id"] if sprints else None


def add_to_sprint(sprint_id: int, issue_key: str):
    resp = requests.post(
        f"{JIRA_URL}/rest/agile/1.0/sprint/{sprint_id}/issue",
        auth=(JIRA_USER, JIRA_TOKEN),
        json={"issues": [issue_key]},
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Codex enhancement
# ---------------------------------------------------------------------------

def _enhance_with_codex(
    raw_description: str,
    components: list[str],
    issue_types: list[str],
    include_code_instructions: bool = False,
    code_context_repos: str = "",
    *,
    log_key: str | None = None,
    preview_job_key: str | None = None,
    clear_logs: bool = True,
) -> dict:
    """Run Codex to turn a raw description into structured ticket fields.

    Writes under ``.ticket_draft/`` (project root, gitignored) to avoid JSON
    encoding issues with multi-line descriptions:
      meta.json  — summary, issue_type, components (simple values)
      desc.txt   — full description as plain text
    """
    from scripts import db

    # Use the project dir (a git repo) so Codex's sandbox can write these files.
    # WORKSPACE_PATH is typically a non-git parent directory of multiple repos,
    # which Codex's --full-auto sandbox blocks writes to.
    meta_path = os.path.join(_TICKET_DRAFT_DIR, "meta.json")
    desc_path = os.path.join(_TICKET_DRAFT_DIR, "desc.txt")

    components_str = ", ".join(components) if components else "none"
    types_str = ", ".join(issue_types) if issue_types else "Story, Bug, Task"

    # Build per-issue-type templates section for the prompt
    templates_raw = db.get_setting("create_ticket_templates", "{}")
    try:
        templates_map = json.loads(templates_raw) if templates_raw else {}
    except (json.JSONDecodeError, TypeError):
        templates_map = {}
    if templates_map:
        lines = ["Issue type templates (follow the matching template when writing the description):"]
        for itype, tpl in templates_map.items():
            if tpl and tpl.strip():
                lines.append(f"- {itype}: {tpl.strip()}")
        templates_str = "\n".join(lines) + "\n"
    else:
        templates_str = ""

    prompt_tpl = db.get_setting("prompt_create_ticket", "")
    if not prompt_tpl:
        _prompts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")
        with open(os.path.join(_prompts_dir, "create_ticket.md"), encoding="utf-8") as f:
            prompt_tpl = f.read()

    code_context_str = ""
    if include_code_instructions:
        prompt_code_tpl = db.get_setting("prompt_code_context", "")
        if not prompt_code_tpl:
            _prompts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")
            with open(os.path.join(_prompts_dir, "code_context.md"), encoding="utf-8") as f:
                prompt_code_tpl = f.read()
        repos_hint = code_context_repos.strip() or "the relevant repositories in the workspace"
        inline_placeholder = "this response (summarize inline; do not create files)"
        format_kwargs = {
            "raw_description": raw_description,
            "code_context_repos": repos_hint,
            "context_path": inline_placeholder,
        }
        try:
            code_context_instructions = prompt_code_tpl.format(**format_kwargs).strip()
        except KeyError as exc:  # surface a clearer error for misconfigured prompts
            raise RuntimeError(
                f"Code context prompt is missing placeholder: {exc}"
            ) from exc
        if code_context_instructions:
            code_context_str = f"{code_context_instructions}\n\n"

    prompt = prompt_tpl.format(
        components=components_str,
        issue_types=types_str,
        raw_description=raw_description,
        meta_path=meta_path,
        desc_path=desc_path,
        templates=templates_str,
        code_context=code_context_str,
    )

    # Build codex command mirroring the pattern in process_ticket.py
    cmd = ["codex", "exec", "--full-auto", "--skip-git-repo-check"]
    model = db.get_setting("create_ticket_model", "") or db.get_setting("model", "")
    if model:
        cmd += ["-m", model]
    effort = db.get_setting("create_ticket_effort", "") or db.get_setting("effort", "")
    if effort and effort != "none":
        cmd += ["--effort", effort]
    cmd.append(prompt)

    # Remove stale output files before running
    for path in (meta_path, desc_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    _ensure_ticket_draft_dir()
    result = _run_codex(
        cmd,
        cwd=_PROJECT_DIR,
        log_key=log_key,
        clear_logs=clear_logs,
        preview_job_key=preview_job_key,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Codex exited with code {result.returncode}:\n{result.stderr or result.stdout}"
        )

    if not os.path.isfile(meta_path):
        raise RuntimeError(
            f"Codex did not write the expected metadata file: {meta_path}\n"
            f"stdout: {result.stdout[:500]}"
        )
    if not os.path.isfile(desc_path):
        raise RuntimeError(
            f"Codex did not write the expected description file: {desc_path}\n"
            f"stdout: {result.stdout[:500]}"
        )

    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    with open(desc_path, encoding="utf-8") as f:
        description = f.read().strip()

    for path in (meta_path, desc_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    return {
        "summary": meta["summary"],
        "issue_type": meta.get("issue_type"),
        "components": meta.get("components", []),
        "description": description,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def enhance_ticket_description(
    project_key: str,
    raw_description: str,
    enrich_with_code: bool = False,
    code_context_repos: str = "",
    *,
    log_key: str | None = None,
    preview_job_key: str | None = None,
    clear_logs: bool = True,
) -> dict:
    """
    Step 1 of the two-step flow: run Codex once to enhance the description and
    return the structured fields WITHOUT creating the ticket. When
    enrich_with_code is True, the prompt includes inline instructions for Codex
    to inspect the repository code before writing the ticket.

    Returns a dict with: summary, issue_type, components, description,
    available_components, available_issue_types.
    """
    from scripts import db

    components = get_project_components(project_key)
    issue_types = get_issue_types(project_key)

    # Filter issue types to the configured allowlist (if set)
    allowed_raw = db.get_setting("create_ticket_issue_types", "")
    if allowed_raw and allowed_raw.strip():
        allowed = [t.strip() for t in allowed_raw.split(",") if t.strip()]
        issue_types = [it for it in issue_types if it in allowed] or issue_types

    enhanced = _enhance_with_codex(
        raw_description,
        components,
        issue_types,
        include_code_instructions=enrich_with_code,
        code_context_repos=code_context_repos,
        log_key=log_key,
        preview_job_key=preview_job_key,
        clear_logs=clear_logs,
    )
    return {
        **enhanced,
        "available_components": components,
        "available_issue_types": issue_types,
    }


def create_ticket_from_enhanced(
    project_key: str,
    summary: str,
    description: str,
    issue_type: str,
    components: list[str],
) -> dict:
    """
    Step 2 of the two-step flow: create the JIRA ticket from pre-enhanced
    (and possibly user-edited) data. Skips the Codex enhancement step.
    """
    issue_key = create_jira_ticket(
        project_key=project_key,
        summary=summary,
        description_text=description,
        issue_type=issue_type,
        component_names=components,
    )

    sprint_id = get_active_sprint_id(project_key)
    added_to_sprint = False
    if sprint_id:
        try:
            add_to_sprint(sprint_id, issue_key)
            added_to_sprint = True
        except Exception:
            pass

    return {
        "issue_key": issue_key,
        "issue_url": f"{JIRA_URL}/browse/{issue_key}",
        "summary": summary,
        "description": description,
        "issue_type": issue_type,
        "components": components,
        "added_to_sprint": added_to_sprint,
    }


def create_ticket_from_description(project_key: str, raw_description: str) -> dict:
    """
    Full pipeline:
      1. Fetch project metadata (components, issue types)
      2. Ask Codex to enhance the description and suggest fields
      3. Create the JIRA ticket
      4. Add to active sprint (best-effort)

    Returns a dict with ticket info.
    """
    components = get_project_components(project_key)
    issue_types = get_issue_types(project_key)

    enhanced = _enhance_with_codex(raw_description, components, issue_types)

    issue_key = create_jira_ticket(
        project_key=project_key,
        summary=enhanced["summary"],
        description_text=enhanced["description"],
        issue_type=enhanced.get("issue_type", issue_types[0] if issue_types else "Story"),
        component_names=enhanced.get("components", []),
    )

    sprint_id = get_active_sprint_id(project_key)
    added_to_sprint = False
    if sprint_id:
        try:
            add_to_sprint(sprint_id, issue_key)
            added_to_sprint = True
        except Exception:
            pass

    return {
        "issue_key": issue_key,
        "issue_url": f"{JIRA_URL}/browse/{issue_key}",
        "summary": enhanced["summary"],
        "description": enhanced["description"],
        "issue_type": enhanced.get("issue_type"),
        "components": enhanced.get("components", []),
        "added_to_sprint": added_to_sprint,
    }
