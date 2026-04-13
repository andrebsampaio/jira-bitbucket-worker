#!/usr/bin/env python3
"""
Processes a Bitbucket PR comment where the bot was mentioned.
Fetches context, builds a prompt, runs codex in a worktree, pushes the fix,
and replies to the comment.

Usage: python3 process_pr_comment.py <workspace> <repo_slug> <pr_id> <comment_id>
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

BITBUCKET_USER = os.environ.get("BITBUCKET_USER", os.environ.get("JIRA_USER", ""))
BITBUCKET_TOKEN = os.environ.get("BITBUCKET_TOKEN", os.environ.get("JIRA_TOKEN", ""))
WORKSPACE_PATH = os.environ["WORKSPACE_PATH"]
BOT_MENTION = os.environ.get("BOT_MENTION", "@andrebot")

BITBUCKET_API = "https://api.bitbucket.org/2.0"
BOT_REVIEW_KEYWORDS = [
    kw.strip().lower()
    for kw in os.environ.get("BOT_REVIEW_KEYWORDS", "review,please review,review please").split(",")
    if kw.strip()
]
REVIEW_OUTPUT_FILE = ".codex-review.json"


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


def fetch_pr_comments(workspace: str, repo_slug: str, pr_id: str) -> list[dict]:
    """Fetch all comments for a PR, handling pagination."""
    url = f"{BITBUCKET_API}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
    comments = []
    params: dict = {"pagelen": 100}
    while url:
        response = requests.get(url, auth=_bb_auth(), params=params)
        response.raise_for_status()
        data = response.json()
        comments.extend(data.get("values", []))
        url = data.get("next")
        params = {}
    return comments


def build_comment_thread(comment_data: dict, all_comments: list[dict]) -> list[dict]:
    """
    Return the conversation thread leading up to comment_data.

    If comment_data is a reply (has a parent), returns:
      - the parent comment
      - all sibling replies posted before this comment (same parent, sorted by created_on)

    If it is a top-level comment, returns an empty list (no prior thread).
    """
    parent = comment_data.get("parent")
    if not parent:
        return []

    parent_id = parent.get("id")
    comment_id = comment_data.get("id")
    comment_created = comment_data.get("created_on", "")

    parent_comment = next((c for c in all_comments if c.get("id") == parent_id), None)

    prior_replies = [
        c for c in all_comments
        if c.get("parent", {}).get("id") == parent_id
        and c.get("id") != comment_id
        and c.get("created_on", "") <= comment_created
    ]
    prior_replies.sort(key=lambda c: c.get("created_on", ""))

    thread: list[dict] = []
    if parent_comment:
        thread.append(parent_comment)
    thread.extend(prior_replies)
    return thread


def format_comment_thread(thread: list[dict]) -> str:
    """Format a list of comment dicts as a readable conversation string."""
    if not thread:
        return ""
    parts = []
    for c in thread:
        author = c.get("user", {}).get("display_name", "Unknown")
        text = c.get("content", {}).get("raw", "").strip()
        parts.append(f"{author}: {text}")
    return "\n\n".join(parts)


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
                 repo_slug: str, conversation: str = "") -> str:
    from scripts.dashboard import SETTINGS_DEFAULTS

    conversation_section = (
        f"\nPrior conversation in this thread:\n{conversation}\n"
        if conversation else ""
    )

    template_vars = {
        "comment": comment_text,
        "file_path": file_path,
        "line": line,
        "diff": diff,
        "pr_title": pr_title,
        "source_branch": source_branch,
        "repo_slug": repo_slug,
        "conversation": conversation_section,
    }

    template = db.get_setting("prompt_pr_comment", SETTINGS_DEFAULTS["prompt_pr_comment"])
    return template.format_map(template_vars)


def build_review_prompt(pr_title: str, source_branch: str,
                        repo_slug: str, diff: str) -> str:
    from scripts.dashboard import SETTINGS_DEFAULTS

    template_vars = {
        "diff": diff,
        "pr_title": pr_title,
        "source_branch": source_branch,
        "repo_slug": repo_slug,
    }
    template = db.get_setting("prompt_pr_review", SETTINGS_DEFAULTS["prompt_pr_review"])
    return template.format_map(template_vars)


def is_review_request(comment_text: str) -> bool:
    """Determine whether the mention should trigger a PR review."""
    if not BOT_REVIEW_KEYWORDS:
        return False
    normalized = re.sub(r"\s+", " ", (comment_text or "")).strip().lower()
    if not normalized:
        return False
    for keyword in BOT_REVIEW_KEYWORDS:
        key = keyword.lower()
        if not key:
            continue
        if (
            normalized == key
            or normalized.startswith(f"{key} ")
            or normalized.startswith(f"{key}:")
        ):
            return True
    return False


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
    """Create a git worktree inside the repo so the sandbox can access .git metadata."""
    short = os.urandom(3).hex()
    worktree_name = f"pr-fix-{pr_id}-{short}"
    worktree_path = os.path.join(repo_dir, ".worktrees", worktree_name)
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)

    # Fetch latest from remote
    subprocess.run(["git", "fetch", "origin"], cwd=repo_dir, check=True)

    # Create worktree with a local branch tracking the remote (avoids detached HEAD)
    local_branch = f"pr-fix-{pr_id}-{short}"
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


def run_codex_with_prompt(prompt: str, worktree_path: str, issue_key: str) -> None:
    """Run Codex CLI with the provided prompt inside the worktree."""
    db.ticket_phase(issue_key, "codex-running", f"Codex started for {issue_key}")
    db.clear_ticket_logs(issue_key)

    cmd = ["codex", "exec", "--full-auto", "--skip-git-repo-check"]
    model = db.get_setting("model", "")
    if model:
        cmd += ["-m", model]
    effort = db.get_setting("effort", "")
    if effort and effort != "none":
        cmd += ["-c", f"model_reasoning_effort={effort}"]
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


def _parse_line_number(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _format_severity(severity: str) -> str:
    sev = (severity or "").strip().lower()
    if sev in {"major", "minor", "nit"}:
        return sev
    return ""


def load_review_results(review_file: str) -> dict:
    if not os.path.isfile(review_file):
        raise RuntimeError(f"Codex did not produce {review_file}")
    with open(review_file, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid review result format in {review_file}")

    comments = data.get("comments") or []
    if not isinstance(comments, list):
        comments = []

    normalized_comments: list[dict] = []
    for entry in comments:
        if not isinstance(entry, dict):
            continue
        normalized_comments.append(
            {
                "path": (entry.get("path") or entry.get("file") or "").strip(),
                "line": _parse_line_number(entry.get("line") or entry.get("line_number") or entry.get("to")),
                "severity": _format_severity(entry.get("severity", "")),
                "message": (entry.get("message") or entry.get("comment") or "").strip(),
            }
        )

    return {
        "status": (data.get("status") or "changes_requested").strip().lower(),
        "summary": (data.get("summary") or "").strip(),
        "comments": normalized_comments,
    }


def post_inline_comment(workspace: str, repo_slug: str, pr_id: str,
                        file_path: str, line: int | None, body: str) -> dict:
    url = f"{BITBUCKET_API}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
    inline = {"path": file_path}
    if line is not None:
        inline["to"] = int(line)
    payload = {
        "content": {"raw": body},
        "inline": inline,
    }
    response = requests.post(url, auth=_bb_auth(), json=payload)
    response.raise_for_status()
    return response.json()


def summarize_review_reply(review: dict, inline_comments: int, general_notes: list[str]) -> str:
    status = review.get("status", "changes_requested").lower()
    status_label = "Approved" if status == "approve" else "Changes requested"
    status_icon = "✅" if status == "approve" else "⚠️"
    lines = [f"{status_icon} Review status: {status_label}"]

    summary = review.get("summary", "").strip()
    if summary:
        lines.append("")
        lines.append(summary)

    if inline_comments:
        plural = "s" if inline_comments != 1 else ""
        lines.append("")
        lines.append(f"- Left {inline_comments} inline comment{plural} on the PR.")

    if general_notes:
        lines.append("")
        lines.append("Additional notes:")
        for note in general_notes:
            lines.append(f"- {note}")

    if len(lines) == 1:
        lines.append("")
        lines.append("No issues found.")

    return "\n".join(lines).strip()


def post_review_feedback(workspace: str, repo_slug: str, pr_id: str, review: dict) -> tuple[int, list[str]]:
    """Post inline comments for review findings and collect any fallback notes."""
    inline_posted = 0
    general_notes: list[str] = []

    for entry in review.get("comments", []):
        message = entry.get("message", "")
        if not message:
            continue
        severity = entry.get("severity", "")
        severity_prefix = f"[{severity}] " if severity else ""
        file_path = entry.get("path", "")
        line = entry.get("line")

        if file_path and line is not None:
            body_lines = []
            if severity:
                body_lines.append(f"**{severity.title()}**")
            body_lines.append(message)
            body = "\n\n".join(body_lines)
            try:
                post_inline_comment(workspace, repo_slug, pr_id, file_path, line, body)
                inline_posted += 1
                continue
            except requests.HTTPError as exc:
                print(f"[pr-comment] WARNING: failed to post inline comment for {file_path}:{line} — {exc}")

        location = ""
        if file_path:
            location = f" ({file_path}{f':{line}' if line is not None else ''})"
        general_notes.append(f"{severity_prefix}{message}{location}")

    return inline_posted, general_notes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_fix_flow(*, workspace: str, repo_slug: str, pr_id: str, comment_id: str,
                     issue_key: str, source_branch: str, pr_title: str,
                     comment_text: str, file_path: str, line_number: str,
                     diff: str, conversation: str = "") -> None:
    prompt = build_prompt(
        comment_text=comment_text,
        file_path=file_path,
        line=line_number,
        diff=diff,
        pr_title=pr_title,
        source_branch=source_branch,
        repo_slug=repo_slug,
        conversation=conversation,
    )
    print(f"[pr-comment] Prompt:\n{'-'*60}\n{prompt}\n{'-'*60}")

    repo_dir = find_local_repo(repo_slug)
    if not repo_dir:
        raise RuntimeError(f"No local repo found matching '{repo_slug}' under {WORKSPACE_PATH}")

    worktree_path = create_worktree(repo_dir, source_branch, pr_id)
    print(f"[pr-comment] Created worktree at {worktree_path}")

    try:
        run_codex_with_prompt(prompt, worktree_path, issue_key)

        codex_commit_file = os.path.join(worktree_path, ".codex-commit.json")
        codex_commit_message = f"fix: address PR review comment (#{comment_id})"
        codex_reply_template = "Fixed in commit `{hash}`"
        if os.path.isfile(codex_commit_file):
            try:
                with open(codex_commit_file, encoding="utf-8") as f:
                    codex_output = json.load(f)
                codex_commit_message = codex_output.get("commit_message") or codex_commit_message
                codex_reply_template = codex_output.get("reply_message") or codex_reply_template
                print(f"[pr-comment] Loaded codex commit output from .codex-commit.json")
            except (json.JSONDecodeError, OSError) as e:
                print(f"[pr-comment] WARNING: could not parse .codex-commit.json: {e}", file=sys.stderr)
        else:
            print(f"[pr-comment] No .codex-commit.json found; using default commit message")

        db.ticket_phase(issue_key, "pushing", f"Committing and pushing fix for {issue_key}")

        if os.path.isfile(codex_commit_file):
            os.remove(codex_commit_file)

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        if not status.stdout.strip():
            raise RuntimeError("Codex made no file changes")

        subprocess.run(["git", "add", "-A"], cwd=worktree_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", codex_commit_message],
            cwd=worktree_path,
            check=True,
        )

        subprocess.run(
            ["git", "push", "origin", f"HEAD:{source_branch}"],
            cwd=worktree_path,
            check=True,
        )
        commit_hash = get_commit_hash(worktree_path)
        print(f"[pr-comment] Pushed fix to {source_branch} (commit {commit_hash})")

        reply_body = codex_reply_template.replace("{hash}", commit_hash)
        reply_to_comment(workspace, repo_slug, pr_id, comment_id, reply_body)
        print(f"[pr-comment] Replied to comment {comment_id}")
    finally:
        print(f"[pr-comment] Removing worktree {worktree_path}")
        remove_worktree(worktree_path)


def process_review_flow(*, workspace: str, repo_slug: str, pr_id: str, comment_id: str,
                        issue_key: str, source_branch: str, pr_title: str,
                        full_diff: str) -> None:
    prompt = build_review_prompt(
        pr_title=pr_title,
        source_branch=source_branch,
        repo_slug=repo_slug,
        diff=full_diff,
    )
    print(f"[pr-comment] Review prompt:\n{'-'*60}\n{prompt}\n{'-'*60}")

    repo_dir = find_local_repo(repo_slug)
    if not repo_dir:
        raise RuntimeError(f"No local repo found matching '{repo_slug}' under {WORKSPACE_PATH}")

    worktree_path = create_worktree(repo_dir, source_branch, pr_id)
    print(f"[pr-comment] Created worktree at {worktree_path}")

    try:
        run_codex_with_prompt(prompt, worktree_path, issue_key)

        review_file = os.path.join(worktree_path, REVIEW_OUTPUT_FILE)
        review = load_review_results(review_file)
        try:
            os.remove(review_file)
        except OSError:
            pass

        inline_count, general_notes = post_review_feedback(workspace, repo_slug, pr_id, review)
        summary = summarize_review_reply(review, inline_count, general_notes)
        reply_to_comment(workspace, repo_slug, pr_id, comment_id, summary)
        print(f"[pr-comment] Posted review summary reply for comment {comment_id}")
    finally:
        print(f"[pr-comment] Removing worktree {worktree_path}")
        remove_worktree(worktree_path)


def main():
    if len(sys.argv) < 5:
        print("Usage: process_pr_comment.py <workspace> <repo_slug> <pr_id> <comment_id>")
        sys.exit(1)

    workspace = sys.argv[1]
    repo_slug = sys.argv[2].replace(" ", "-").lower()
    pr_id = sys.argv[3]
    comment_id = sys.argv[4]
    issue_key = f"PR-{repo_slug}#{pr_id}-C{comment_id}"

    print(f"[pr-comment] Processing comment {comment_id} on PR {pr_id} in {workspace}/{repo_slug}")

    db.ticket_started(issue_key, summary=f"PR comment on {repo_slug}#{pr_id}")

    pr = fetch_pr(workspace, repo_slug, pr_id)
    source_branch = pr["source"]["branch"]["name"]
    pr_title = pr.get("title", "")

    comment_data = fetch_comment(workspace, repo_slug, pr_id, comment_id)
    comment_body = comment_data.get("content", {}).get("raw", "")

    inline = comment_data.get("inline", {})
    file_path = inline.get("path", "")
    line_number = str(inline.get("to", "") or inline.get("from", "") or "")

    full_diff = fetch_pr_diff(workspace, repo_slug, pr_id)
    diff = extract_file_diff(full_diff, file_path) if file_path else full_diff

    all_comments = fetch_pr_comments(workspace, repo_slug, pr_id)
    thread = build_comment_thread(comment_data, all_comments)
    conversation = format_comment_thread(thread)
    if conversation:
        print(f"[pr-comment] Found conversation thread with {len(thread)} prior message(s)")

    comment_text = comment_body.replace(BOT_MENTION, "").strip()
    if is_review_request(comment_text):
        process_review_flow(
            workspace=workspace,
            repo_slug=repo_slug,
            pr_id=pr_id,
            comment_id=comment_id,
            issue_key=issue_key,
            source_branch=source_branch,
            pr_title=pr_title,
            full_diff=full_diff,
        )
    else:
        process_fix_flow(
            workspace=workspace,
            repo_slug=repo_slug,
            pr_id=pr_id,
            comment_id=comment_id,
            issue_key=issue_key,
            source_branch=source_branch,
            pr_title=pr_title,
            comment_text=comment_text,
            file_path=file_path,
            line_number=line_number,
            diff=diff,
            conversation=conversation,
        )

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
