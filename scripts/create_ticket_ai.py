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
    priority: str,
    component_names: list[str],
) -> str:
    """Create a JIRA ticket and return its key."""
    body: dict = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary,
            "description": _text_to_adf(description_text),
            "issuetype": {"name": issue_type},
            "priority": {"name": priority},
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
    """Run Codex to turn a raw description into structured ticket fields."""
    output_path = os.path.join(WORKSPACE_PATH, ".ticket_draft.json")

    components_str = ", ".join(components) if components else "none"
    types_str = ", ".join(issue_types) if issue_types else "Story, Bug, Task"

    prompt = (
        f"You are a technical product manager. Analyse the following raw ticket description "
        f"and write a structured JIRA ticket as JSON to the file: {output_path}\n\n"
        f"Available components: {components_str}\n"
        f"Available issue types: {types_str}\n\n"
        f"Raw description:\n{raw_description}\n\n"
        f"Write exactly this JSON structure to {output_path} (no other output):\n"
        "{{\n"
        '  "summary": "concise title (max 100 chars)",\n'
        '  "description": "improved description with context, technical details, and acceptance criteria (plain text, paragraphs separated by blank lines)",\n'
        '  "issue_type": "one of the available issue types",\n'
        '  "priority": "Highest|High|Medium|Low|Lowest",\n'
        '  "components": ["array of component names from the available list — may be empty"]\n'
        "}}"
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

    # Remove stale output file before running
    try:
        os.remove(output_path)
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

    if not os.path.isfile(output_path):
        raise RuntimeError(
            f"Codex did not write the expected output file: {output_path}\n"
            f"stdout: {result.stdout[:500]}"
        )

    with open(output_path, encoding="utf-8") as f:
        data = json.load(f)

    try:
        os.remove(output_path)
    except FileNotFoundError:
        pass

    return data


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

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
        priority=enhanced.get("priority", "Medium"),
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
        "priority": enhanced.get("priority"),
        "components": enhanced.get("components", []),
        "added_to_sprint": added_to_sprint,
    }
