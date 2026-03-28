#!/usr/bin/env python3
"""
Fetches a JIRA ticket, lets codex determine the repos and create worktrees,
then pushes branches, creates Bitbucket PRs, and cleans up worktrees.

Usage: python3 process_ticket.py <ISSUE_KEY>
"""

import json
import os
import re
import shutil
import subprocess
import sys
import traceback

import requests
from dotenv import load_dotenv

load_dotenv()

# Ensure project root is on sys.path so "scripts" package is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts import db

JIRA_URL = os.environ["JIRA_URL"].rstrip("/")
JIRA_USER = os.environ["JIRA_USER"]
JIRA_TOKEN = os.environ["JIRA_TOKEN"]
BITBUCKET_WORKSPACE = os.environ["BITBUCKET_WORKSPACE"]
BITBUCKET_USER = os.environ.get("BITBUCKET_USER", JIRA_USER)
BITBUCKET_TOKEN = os.environ.get("BITBUCKET_TOKEN", JIRA_TOKEN)
WORKSPACE_PATH = os.environ["WORKSPACE_PATH"]

BITBUCKET_API = "https://api.bitbucket.org/2.0"
RUN_MANIFEST = os.path.join(WORKSPACE_PATH, ".jira-bitbucket-worker-run.json")


# ---------------------------------------------------------------------------
# JIRA helpers
# ---------------------------------------------------------------------------

def fetch_issue(issue_key: str) -> dict:
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}"
    response = requests.get(url, auth=(JIRA_USER, JIRA_TOKEN))
    response.raise_for_status()
    return response.json()


def get_transitions(issue_key: str) -> list[dict]:
    """Fetch available transitions for the given issue."""
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}/transitions"
    response = requests.get(url, auth=(JIRA_USER, JIRA_TOKEN))
    response.raise_for_status()
    return response.json().get("transitions", [])


def transition_issue(issue_key: str, target_status: str) -> bool:
    """Transition the issue to the given target status name (case-insensitive).

    Returns True if the transition was performed, False if no matching transition was found.
    """
    transitions = get_transitions(issue_key)
    for t in transitions:
        if t["name"].lower() == target_status.lower():
            url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}/transitions"
            body = {"transition": {"id": t["id"]}}
            response = requests.post(url, auth=(JIRA_USER, JIRA_TOKEN), json=body)
            response.raise_for_status()
            print(f"[process] Transitioned {issue_key} to '{target_status}'")
            return True
    available = [t["name"] for t in transitions]
    print(
        f"[process] WARNING: No transition to '{target_status}' found for {issue_key}. "
        f"Available transitions: {available}",
        file=sys.stderr,
    )
    return False


def extract_text(adf_or_str) -> str:
    """Extract plain text from an ADF (Atlassian Document Format) node or plain string."""
    if adf_or_str is None:
        return ""
    if isinstance(adf_or_str, str):
        return adf_or_str
    if isinstance(adf_or_str, dict):
        parts = []
        if adf_or_str.get("type") == "text":
            return adf_or_str.get("text", "")
        for child in adf_or_str.get("content", []):
            parts.append(extract_text(child))
        return "\n".join(p for p in parts if p)
    if isinstance(adf_or_str, list):
        return "\n".join(extract_text(item) for item in adf_or_str)
    return ""


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def remove_worktree(worktree_dir: str) -> None:
    """Remove a worktree directory and prune the parent repo's worktree list."""
    # Resolve the parent repo from the worktree's .git file
    git_file = os.path.join(worktree_dir, ".git")
    repo_root = None
    if os.path.isfile(git_file):
        with open(git_file) as f:
            content = f.read().strip()
        # Format: "gitdir: /path/to/repo/.git/worktrees/<name>"
        if content.startswith("gitdir:"):
            gitdir = content.split(":", 1)[1].strip()
            # Walk up from .git/worktrees/<name> to the repo root
            candidate = os.path.dirname(os.path.dirname(os.path.dirname(gitdir)))
            if os.path.isdir(os.path.join(candidate, ".git")):
                repo_root = candidate

    subprocess.run(
        ["git", "worktree", "remove", "--force", worktree_dir],
        cwd=repo_root or WORKSPACE_PATH,
        capture_output=True,
    )
    if os.path.isdir(worktree_dir):
        shutil.rmtree(worktree_dir, ignore_errors=True)
    if repo_root:
        subprocess.run(["git", "worktree", "prune"], cwd=repo_root, capture_output=True)


def push_branch(worktree_dir: str, branch: str) -> None:
    subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=worktree_dir,
        check=True,
    )


def current_branch(repo_root: str) -> str | None:
    r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    name = r.stdout.strip() if r.returncode == 0 else ""
    return name if name and name != "HEAD" else None


def has_unpushed_commits(worktree_dir: str) -> bool:
    r = subprocess.run(
        ["git", "log", "--oneline", "HEAD", "--not", "--remotes"],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
    )
    return bool(r.stdout.strip())


# ---------------------------------------------------------------------------
# Bitbucket helpers
# ---------------------------------------------------------------------------

def _bb_auth() -> tuple[str, str]:
    return (BITBUCKET_USER, BITBUCKET_TOKEN)


def parse_bitbucket_remote(url: str) -> tuple[str, str] | None:
    u = url.strip()
    m = re.search(r"bitbucket\.org[:/]([^/]+)/([^/\s]+?)(?:\.git)?\s*$", u)
    if not m:
        return None
    return m.group(1), m.group(2)


def git_remote_origin(repo_root: str) -> str | None:
    r = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def fetch_repo_mainbranch(workspace: str, repo_slug: str) -> str:
    url = f"{BITBUCKET_API}/repositories/{workspace}/{repo_slug}"
    response = requests.get(url, auth=_bb_auth())
    response.raise_for_status()
    data = response.json()
    main = (data.get("mainbranch") or {}).get("name")
    return main if main else "main"


def create_bitbucket_pr(
    workspace: str,
    repo_slug: str,
    title: str,
    description: str,
    source_branch: str,
    dest_branch: str,
) -> dict:
    url = f"{BITBUCKET_API}/repositories/{workspace}/{repo_slug}/pullrequests"
    body = {
        "title": title,
        "description": description,
        "source": {"branch": {"name": source_branch}},
        "destination": {"branch": {"name": dest_branch}},
    }
    response = requests.post(url, auth=_bb_auth(), json=body)
    if response.status_code == 409:
        raise RuntimeError(
            "Pull request already exists or branch state conflicts. "
            f"Bitbucket response: {response.text}"
        )
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_prompt(issue: dict) -> str:
    from scripts.dashboard import SETTINGS_DEFAULTS

    fields = issue["fields"]
    key = issue["key"]
    summary = fields.get("summary", "")
    description = extract_text(fields.get("description"))
    components = ", ".join(c["name"] for c in fields.get("components", []))
    labels = ", ".join(fields.get("labels", []))
    priority = (fields.get("priority") or {}).get("name", "")
    issue_type = (fields.get("issuetype") or {}).get("name", "")

    acceptance_criteria = ""
    for field_key, field_val in fields.items():
        if "acceptance" in field_key.lower() or "criteria" in field_key.lower():
            acceptance_criteria = extract_text(field_val)
            break

    # Template variables available for substitution
    template_vars = {
        "key": key,
        "summary": summary,
        "description": description or "(no description provided)",
        "components": components,
        "labels": labels,
        "priority": priority,
        "issue_type": issue_type,
        "acceptance_criteria": acceptance_criteria or "(none)",
        "workspace_path": WORKSPACE_PATH,
        "run_manifest": RUN_MANIFEST,
    }

    context_tpl = db.get_setting("prompt_context", SETTINGS_DEFAULTS["prompt_context"])
    instructions_tpl = db.get_setting("prompt_instructions", SETTINGS_DEFAULTS["prompt_instructions"])

    context = context_tpl.format_map(template_vars)
    instructions = instructions_tpl.format_map(template_vars)

    return f"{context}\n\nInstructions:\n{instructions}"


# ---------------------------------------------------------------------------
# Post-codex: push, PR, cleanup
# ---------------------------------------------------------------------------

def load_manifest(issue_key: str) -> list[dict] | None:
    """Load the manifest written by codex. Returns list of worktree entries or None."""
    if not os.path.isfile(RUN_MANIFEST):
        return None
    with open(RUN_MANIFEST, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("issue_key") != issue_key:
        print(
            f"[process] Manifest issue_key mismatch (expected {issue_key!r}, "
            f"got {data.get('issue_key')!r}); skipping.",
            file=sys.stderr,
        )
        return None
    worktrees = data.get("worktrees", [])
    if not worktrees:
        print("[process] Manifest has no worktree entries.", file=sys.stderr)
        return None
    return worktrees


def infer_worktrees(issue_key: str) -> list[dict]:
    """Fallback: scan the worktrees/ directory for directories whose name contains the issue key."""
    worktrees_dir = os.path.join(WORKSPACE_PATH, "worktrees")
    if not os.path.isdir(worktrees_dir):
        return []
    results = []
    for entry in os.scandir(worktrees_dir):
        if not entry.is_dir() or issue_key not in entry.name:
            continue
        git_marker = os.path.join(entry.path, ".git")
        if not (os.path.isdir(git_marker) or os.path.isfile(git_marker)):
            continue
        branch = current_branch(entry.path)
        if branch:
            results.append({"worktree_path": entry.path, "branch": branch})
    return results


def process_worktree(issue: dict, issue_key: str, worktree_path: str, branch: str) -> None:
    """Push branch, create Bitbucket PR, then remove the worktree."""
    if not os.path.isdir(worktree_path):
        print(f"[process] Worktree path does not exist: {worktree_path}", file=sys.stderr)
        return

    if not has_unpushed_commits(worktree_path):
        print(f"[process] No new commits in {worktree_path}; skipping push/PR.")
        return

    # Push
    print(f"[process] Pushing branch {branch}")
    push_branch(worktree_path, branch)

    # Resolve Bitbucket remote
    remote_url = git_remote_origin(worktree_path)
    if not remote_url:
        print(f"[process] Could not read git remote for {worktree_path}; skipping PR.", file=sys.stderr)
        return
    parsed = parse_bitbucket_remote(remote_url)
    if not parsed:
        print(f"[process] Remote is not Bitbucket Cloud: {remote_url!r}; skipping PR.", file=sys.stderr)
        return

    workspace, repo_slug = parsed
    dest = fetch_repo_mainbranch(workspace, repo_slug)

    summary = issue["fields"].get("summary", "")
    title = f"[{issue_key}] {summary}"
    description_text = extract_text(issue["fields"].get("description"))
    body = f"Implements {issue_key}.\n\n{description_text}".strip()

    print(f"[process] Creating PR: {workspace}/{repo_slug} {branch} -> {dest}")
    pr = create_bitbucket_pr(workspace, repo_slug, title, body, branch, dest)
    html = (pr.get("links", {}).get("html") or {}).get("href", "")
    pr_id = str(pr.get("id", ""))
    print(f"[process] Pull request: {html or pr_id}")

    db.pr_created(
        issue_key=issue_key, repo_slug=repo_slug, workspace=workspace,
        branch=branch, dest_branch=dest, pr_url=html, pr_id=pr_id,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: process_ticket.py <ISSUE_KEY>")
        sys.exit(1)

    issue_key = sys.argv[1]
    print(f"[process] Fetching {issue_key} from JIRA...")
    issue = fetch_issue(issue_key)

    fields = issue.get("fields", {})
    db.ticket_started(
        issue_key,
        summary=fields.get("summary", ""),
        priority=(fields.get("priority") or {}).get("name", ""),
        issue_type=(fields.get("issuetype") or {}).get("name", ""),
        components=", ".join(c["name"] for c in fields.get("components", [])),
    )

    # Move ticket to In Progress
    transition_issue(issue_key, "In Progress")

    # Clean up any old manifest
    try:
        os.remove(RUN_MANIFEST)
    except FileNotFoundError:
        pass

    # Build prompt and run codex in the workspace root
    prompt = build_prompt(issue)
    print(f"[process] Starting codex for {issue_key} in {WORKSPACE_PATH}")
    print(f"[process] Prompt:\n{'-'*60}\n{prompt}\n{'-'*60}")

    db.ticket_phase(issue_key, "codex-running", f"Codex started for {issue_key}")
    db.clear_ticket_logs(issue_key)

    proc = subprocess.Popen(
        ["codex", "exec", "--skip-git-repo-check", prompt],
        cwd=WORKSPACE_PATH,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in proc.stdout:
        line = line.rstrip("\n")
        print(f"[codex] {line}")
        db.log_line(issue_key, line)
    proc.wait()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, "codex")
    print(f"[process] codex finished for {issue_key}")

    # Load worktree info from manifest or infer from filesystem
    worktrees = load_manifest(issue_key)
    if not worktrees:
        print("[process] No manifest found, inferring worktrees from filesystem...")
        worktrees = infer_worktrees(issue_key)
    if not worktrees:
        print("[process] No worktrees found; nothing to push.", file=sys.stderr)
        return

    print(f"[process] Found {len(worktrees)} worktree(s) to process")
    db.ticket_phase(issue_key, "pushing", f"Pushing {len(worktrees)} branch(es) for {issue_key}")

    # Push branches, create PRs, then clean up each worktree
    any_pr_created = False
    for entry in worktrees:
        wt_path = entry["worktree_path"]
        branch = entry["branch"]
        try:
            process_worktree(issue, issue_key, wt_path, branch)
            any_pr_created = True
        except Exception as e:
            print(f"[process] Error processing {wt_path}: {e}", file=sys.stderr)
        finally:
            print(f"[process] Removing worktree {wt_path}")
            remove_worktree(wt_path)

    # Move ticket to Review if at least one PR was created
    if any_pr_created:
        transition_issue(issue_key, "Review")

    # Clean up manifest
    try:
        os.remove(RUN_MANIFEST)
    except FileNotFoundError:
        pass

    db.ticket_finished(issue_key)
    print(f"[process] Done processing {issue_key}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        issue_key = sys.argv[1] if len(sys.argv) > 1 else None
        if issue_key:
            db.ticket_finished(issue_key, error=traceback.format_exc())
        raise
