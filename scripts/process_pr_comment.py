#!/usr/bin/env python3
"""
Processes a Bitbucket PR comment where the bot was mentioned.
Fetches context, builds a prompt, runs codex in a worktree, pushes the fix,
and replies to the comment.

Usage: python3 process_pr_comment.py <workspace> <repo_slug> <pr_id> <comment_id>
"""

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

BITBUCKET_USER = os.environ.get("BITBUCKET_USER", os.environ.get("JIRA_USER", ""))
BITBUCKET_TOKEN = os.environ.get("BITBUCKET_TOKEN", os.environ.get("JIRA_TOKEN", ""))
WORKSPACE_PATH = os.environ["WORKSPACE_PATH"]
BOT_MENTION = os.environ.get("BOT_MENTION", "@andrebot")

BITBUCKET_API = "https://api.bitbucket.org/2.0"


def _bb_auth() -> tuple[str, str]:
    return (BITBUCKET_USER, BITBUCKET_TOKEN)


# ---------------------------------------------------------------------------
# Bitbucket API helpers
# ---------------------------------------------------------------------------

def fetch_pr(workspace: str, repo_slug: str, pr_id: str) -> dict:
    url = f"{BITBUCKET_API}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}"
    response = requests.get(url, auth=_bb_auth())
    response.raise_for_status()
    return response.json()


def fetch_pr_diff(workspace: str, repo_slug: str, pr_id: str) -> str:
    url = f"{BITBUCKET_API}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/diff"
    response = requests.get(url, auth=_bb_auth())
    response.raise_for_status()
    return response.text


def fetch_comment(workspace: str, repo_slug: str, pr_id: str, comment_id: str) -> dict:
    url = f"{BITBUCKET_API}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/comments/{comment_id}"
    response = requests.get(url, auth=_bb_auth())
    response.raise_for_status()
    return response.json()


def reply_to_comment(workspace: str, repo_slug: str, pr_id: str, parent_id: str, body: str):
    url = f"{BITBUCKET_API}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
    payload = {
        "content": {"raw": body},
        "parent": {"id": int(parent_id)},
    }
    response = requests.post(url, auth=_bb_auth(), json=payload)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

def extract_file_diff(full_diff: str, file_path: str) -> str:
    """Extract the diff section for a specific file from the full PR diff."""
    pattern = rf"^diff --git a/{re.escape(file_path)} b/{re.escape(file_path)}$"
    lines = full_diff.splitlines(keepends=True)
    collecting = False
    result = []
    for line in lines:
        if re.match(pattern, line.rstrip()):
            collecting = True
            result.append(line)
        elif collecting and line.startswith("diff --git "):
            break
        elif collecting:
            result.append(line)
    return "".join(result)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_prompt(comment_text: str, file_path: str, line: str,
                 diff: str, pr_title: str, source_branch: str,
                 repo_slug: str) -> str:
    from scripts.dashboard import SETTINGS_DEFAULTS

    template_vars = {
        "comment": comment_text,
        "file_path": file_path,
        "line": line,
        "diff": diff,
        "pr_title": pr_title,
        "source_branch": source_branch,
        "repo_slug": repo_slug,
    }

    template = db.get_setting("prompt_pr_comment", SETTINGS_DEFAULTS["prompt_pr_comment"])
    return template.format_map(template_vars)


# ---------------------------------------------------------------------------
# Git / worktree helpers
# ---------------------------------------------------------------------------

def find_local_repo(repo_slug: str) -> str | None:
    """Find a local repo directory matching the repo_slug under WORKSPACE_PATH."""
    for entry in os.scandir(WORKSPACE_PATH):
        if not entry.is_dir():
            continue
        # Exact match or slug is contained in directory name
        if entry.name == repo_slug or repo_slug in entry.name:
            git_marker = os.path.join(entry.path, ".git")
            if os.path.isdir(git_marker) or os.path.isfile(git_marker):
                return entry.path
    return None


def create_worktree(repo_dir: str, source_branch: str, pr_id: str) -> str:
    """Create a git worktree tracking the PR's source branch. Returns worktree path."""
    worktree_name = f"pr-fix-{pr_id}-{os.urandom(3).hex()}"
    worktree_path = os.path.join(WORKSPACE_PATH, "worktrees", worktree_name)
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)

    # Fetch latest from remote
    subprocess.run(["git", "fetch", "origin"], cwd=repo_dir, check=True)

    # Create worktree with a local branch tracking the remote (avoids detached HEAD)
    local_branch = f"pr-fix-{pr_id}-{os.urandom(3).hex()}"
    subprocess.run(
        ["git", "worktree", "add", "-b", local_branch, worktree_path, f"origin/{source_branch}"],
        cwd=repo_dir,
        check=True,
    )
    return worktree_path


def remove_worktree(worktree_dir: str) -> None:
    """Remove a worktree directory and prune the parent repo's worktree list."""
    git_file = os.path.join(worktree_dir, ".git")
    repo_root = None
    if os.path.isfile(git_file):
        with open(git_file) as f:
            content = f.read().strip()
        if content.startswith("gitdir:"):
            gitdir = content.split(":", 1)[1].strip()
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


def get_commit_hash(repo_dir: str) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else "unknown"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 5:
        print("Usage: process_pr_comment.py <workspace> <repo_slug> <pr_id> <comment_id>")
        sys.exit(1)

    workspace = sys.argv[1]
    repo_slug = sys.argv[2]
    pr_id = sys.argv[3]
    comment_id = sys.argv[4]
    issue_key = f"PR-{repo_slug}#{pr_id}-C{comment_id}"

    print(f"[pr-comment] Processing comment {comment_id} on PR {pr_id} in {workspace}/{repo_slug}")

    # Step 1 — Fetch context from Bitbucket API
    db.ticket_started(issue_key, summary=f"PR comment on {repo_slug}#{pr_id}")

    pr = fetch_pr(workspace, repo_slug, pr_id)
    source_branch = pr["source"]["branch"]["name"]
    pr_title = pr.get("title", "")

    comment_data = fetch_comment(workspace, repo_slug, pr_id, comment_id)
    comment_body = comment_data.get("content", {}).get("raw", "")

    # Determine if inline comment
    inline = comment_data.get("inline", {})
    file_path = inline.get("path", "")
    line_number = str(inline.get("to", "") or inline.get("from", "") or "")

    # Fetch PR diff
    full_diff = fetch_pr_diff(workspace, repo_slug, pr_id)
    diff = extract_file_diff(full_diff, file_path) if file_path else full_diff

    # Step 2 — Build prompt
    comment_text = comment_body.replace(BOT_MENTION, "").strip()
    prompt = build_prompt(
        comment_text=comment_text,
        file_path=file_path,
        line=line_number,
        diff=diff,
        pr_title=pr_title,
        source_branch=source_branch,
        repo_slug=repo_slug,
    )
    print(f"[pr-comment] Prompt:\n{'-'*60}\n{prompt}\n{'-'*60}")

    # Step 3 — Worktree flow
    repo_dir = find_local_repo(repo_slug)
    if not repo_dir:
        raise RuntimeError(f"No local repo found matching '{repo_slug}' under {WORKSPACE_PATH}")

    worktree_path = create_worktree(repo_dir, source_branch, pr_id)
    print(f"[pr-comment] Created worktree at {worktree_path}")

    try:
        # Run codex inside the worktree
        db.ticket_phase(issue_key, "codex-running", f"Codex started for {issue_key}")
        db.clear_ticket_logs(issue_key)

        cmd = ["codex", "exec", "--skip-git-repo-check"]
        model = db.get_setting("model", "")
        if model:
            cmd += ["-m", model]
        cmd.append(prompt)

        proc = subprocess.Popen(
            cmd,
            cwd=worktree_path,
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
        print(f"[pr-comment] Codex finished for {issue_key}")

        # Push the fix to the source branch
        db.ticket_phase(issue_key, "pushing", f"Pushing fix for {issue_key}")
        subprocess.run(
            ["git", "push", "origin", f"HEAD:{source_branch}"],
            cwd=worktree_path,
            check=True,
        )
        commit_hash = get_commit_hash(worktree_path)
        print(f"[pr-comment] Pushed fix to {source_branch} (commit {commit_hash})")

        # Step 4 — Reply to comment
        reply_body = f"Fixed in commit `{commit_hash}`"
        reply_to_comment(workspace, repo_slug, pr_id, comment_id, reply_body)
        print(f"[pr-comment] Replied to comment {comment_id}")

    finally:
        # Step 5 — Clean up worktree
        print(f"[pr-comment] Removing worktree {worktree_path}")
        remove_worktree(worktree_path)

    db.ticket_finished(issue_key)
    print(f"[pr-comment] Done processing {issue_key}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        issue_key = None
        if len(sys.argv) >= 5:
            issue_key = f"PR-{sys.argv[2]}#{sys.argv[3]}-C{sys.argv[4]}"
        if issue_key:
            db.ticket_finished(issue_key, error=traceback.format_exc())
        raise
