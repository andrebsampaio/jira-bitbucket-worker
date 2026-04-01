"""
AI-assisted JIRA ticket creation.

Uses Codex CLI to enhance a raw description into a structured ticket, then
creates it in JIRA and adds it to the active sprint when one is found.
"""

import json
import os
import subprocess
import tempfile

import requests
from dotenv import load_dotenv

load_dotenv()

JIRA_URL = os.environ["JIRA_URL"].rstrip("/")
JIRA_USER = os.environ["JIRA_USER"]
JIRA_TOKEN = os.environ["JIRA_TOKEN"]
WORKSPACE_PATH = os.environ.get("WORKSPACE_PATH", tempfile.gettempdir())


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
) -> dict:
    """Run Codex to turn a raw description into structured ticket fields.

    Outputs two files to avoid JSON encoding issues with multi-line descriptions:
      .ticket_draft_meta.json  — summary, issue_type, components (simple values)
      .ticket_draft_desc.txt   — full description as plain text
    """
    meta_path = os.path.join(WORKSPACE_PATH, ".ticket_draft_meta.json")
    desc_path = os.path.join(WORKSPACE_PATH, ".ticket_draft_desc.txt")

    components_str = ", ".join(components) if components else "none"
    types_str = ", ".join(issue_types) if issue_types else "Story, Bug, Task"

    _prompts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")
    with open(os.path.join(_prompts_dir, "create_ticket.md"), encoding="utf-8") as f:
        prompt_tpl = f.read()

    prompt = prompt_tpl.format(
        components=components_str,
        issue_types=types_str,
        raw_description=raw_description,
        meta_path=meta_path,
        desc_path=desc_path,
    )

    # Build codex command mirroring the pattern in process_ticket.py
    from scripts import db
    cmd = ["codex", "exec", "--full-auto", "--skip-git-repo-check"]
    model = db.get_setting("model", "")
    if model:
        cmd += ["-m", model]
    effort = db.get_setting("effort", "")
    if effort and effort != "none":
        cmd += ["-c", f"reasoning_effort={effort}"]
    cmd.append(prompt)

    # Remove stale output files before running
    for path in (meta_path, desc_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    result = subprocess.run(
        cmd,
        cwd=WORKSPACE_PATH,
        capture_output=True,
        text=True,
        timeout=120,
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

def enhance_ticket_description(project_key: str, raw_description: str) -> dict:
    """
    Step 1 of the two-step flow: run Codex to enhance the description and
    return the structured fields WITHOUT creating the ticket.

    Returns a dict with: summary, issue_type, components, description,
    available_components, available_issue_types.
    """
    components = get_project_components(project_key)
    issue_types = get_issue_types(project_key)
    enhanced = _enhance_with_codex(raw_description, components, issue_types)
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
