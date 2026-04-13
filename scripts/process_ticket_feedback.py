#!/usr/bin/env python3
"""
Applies feedback to all open PRs for a JIRA ticket.

Fetches each PR's diff, then runs Codex in each repo's worktree with the
full cross-repo context plus the provided feedback text.

Usage: python3 process_ticket_feedback.py <original_issue_key> <job_key> <feedback_b64>
"""

import base64
import json
import os
import subprocess
import sys
import traceback

import requests
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts import db
from scripts.process_pr_comment import (
    fetch_pr,
    fetch_pr_diff,
    find_local_repo,
    create_worktree,
    remove_worktree,
    get_commit_hash,
    run_codex_with_prompt,
    _bb_auth,
)

BITBUCKET_API = "https://api.bitbucket.org/2.0"
WORKSPACE_PATH = os.environ["WORKSPACE_PATH"]


# ---------------------------------------------------------------------------
# Bitbucket API helpers
# ---------------------------------------------------------------------------

def post_pr_comment(workspace: str, repo_slug: str, pr_id: str, body: str) -> dict:
    """Post a top-level comment on a PR (not a reply to any specific comment)."""
    url = f"{BITBUCKET_API}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
    payload = {"content": {"raw": body}}
    response = requests.post(url, auth=_bb_auth(), json=payload)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_feedback_prompt(*, feedback: str, issue_key: str, this_pr: dict,
                           all_pr_contexts: list[dict]) -> str:
    from scripts.dashboard import SETTINGS_DEFAULTS

    this_pr_id = this_pr["pr_id"]
    other_contexts = [p for p in all_pr_contexts if p["pr_id"] != this_pr_id]

    other_pr_section = ""
    if other_contexts:
        parts = ["## Changes in Other Repositories (for context)\n"]
        for p in other_contexts:
            parts.append(
                f"### {p['repo_slug']} — PR #{p['pr_id']} (branch: {p['source_branch']})\n\n"
                f"```diff\n{p['diff']}\n```"
            )
        other_pr_section = "\n\n".join(parts)

    template_vars = {
        "feedback": feedback,
        "issue_key": issue_key,
        "repo_slug": this_pr["repo_slug"],
        "pr_title": this_pr["title"],
        "source_branch": this_pr["source_branch"],
        "pr_id": this_pr_id,
        "diff": this_pr["diff"],
        "other_pr_contexts": other_pr_section,
    }

    template = db.get_setting(
        "prompt_ticket_feedback",
        SETTINGS_DEFAULTS.get("prompt_ticket_feedback", ""),
    )
    return template.format_map(template_vars)


# ---------------------------------------------------------------------------
# Per-PR processing
# ---------------------------------------------------------------------------

def process_pr(*, pr_record: dict, feedback: str, original_key: str,
               job_key: str, all_pr_contexts: list[dict]) -> None:
    """Apply feedback to a single PR: run Codex, commit, push, and post a reply."""
    workspace = pr_record["workspace"]
    repo_slug = pr_record["repo_slug"]
    pr_id = pr_record["pr_id"]
    source_branch = pr_record["branch"]

    label = f"{repo_slug}#{pr_id}"
    print(f"[feedback] Processing PR {label} for {original_key}")

    # Find which context entry is ours so build_feedback_prompt can distinguish
    this_pr = next((p for p in all_pr_contexts if p["pr_id"] == pr_id), None)
    if this_pr is None:
        print(f"[feedback] WARNING: context not found for {label}, skipping")
        return

    prompt = build_feedback_prompt(
        feedback=feedback,
        issue_key=original_key,
        this_pr=this_pr,
        all_pr_contexts=all_pr_contexts,
    )
    print(f"[feedback] Prompt for {label}:\n{'-'*60}\n{prompt}\n{'-'*60}")

    repo_dir = find_local_repo(repo_slug)
    if not repo_dir:
        raise RuntimeError(
            f"No local repo found matching '{repo_slug}' under {WORKSPACE_PATH}"
        )

    worktree_path = create_worktree(repo_dir, source_branch, pr_id)
    print(f"[feedback] Created worktree at {worktree_path}")

    try:
        run_codex_with_prompt(prompt, worktree_path, job_key)

        codex_commit_file = os.path.join(worktree_path, ".codex-commit.json")
        commit_message = ""
        reply_message = f"Feedback applied to {repo_slug}."

        if os.path.isfile(codex_commit_file):
            try:
                with open(codex_commit_file, encoding="utf-8") as f:
                    codex_output = json.load(f)
                commit_message = (codex_output.get("commit_message") or "").strip()
                reply_message = (codex_output.get("reply_message") or reply_message).strip()
                print(f"[feedback] Loaded .codex-commit.json for {label}")
            except (json.JSONDecodeError, OSError) as e:
                print(f"[feedback] WARNING: could not parse .codex-commit.json: {e}", file=sys.stderr)
            finally:
                try:
                    os.remove(codex_commit_file)
                except OSError:
                    pass

        # If codex said no changes needed, skip commit/push
        if not commit_message:
            print(f"[feedback] No changes for {label} — posting 'no changes' note")
            post_pr_comment(workspace, repo_slug, pr_id, reply_message)
            return

        # Check if there are actual file changes
        db.ticket_phase(job_key, "pushing", f"Committing and pushing feedback fix for {label}")
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        if not status.stdout.strip():
            print(f"[feedback] Codex produced no file changes for {label}, skipping commit")
            post_pr_comment(workspace, repo_slug, pr_id, reply_message)
            return

        subprocess.run(["git", "add", "-A"], cwd=worktree_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=worktree_path,
            check=True,
        )
        subprocess.run(
            ["git", "push", "origin", f"HEAD:{source_branch}"],
            cwd=worktree_path,
            check=True,
        )
        commit_hash = get_commit_hash(worktree_path)
        print(f"[feedback] Pushed to {source_branch} (commit {commit_hash}) for {label}")

        full_reply = f"{reply_message}\n\nApplied in commit `{commit_hash}`."
        post_pr_comment(workspace, repo_slug, pr_id, full_reply)
        print(f"[feedback] Posted reply on {label}")

    finally:
        print(f"[feedback] Removing worktree {worktree_path}")
        remove_worktree(worktree_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 4:
        print(
            "Usage: process_ticket_feedback.py <original_issue_key> <job_key> <feedback_b64>"
        )
        sys.exit(1)

    original_key = sys.argv[1]
    job_key = sys.argv[2]
    feedback_b64 = sys.argv[3]
    feedback = base64.b64decode(feedback_b64.encode()).decode()

    print(f"[feedback] Job {job_key}: applying feedback to PRs of {original_key}")

    db.ticket_started(job_key, summary=f"Feedback for {original_key}")

    pr_records = db.get_pull_requests(issue_key=original_key)
    open_prs = [p for p in pr_records if (p.get("status") or "open") == "open"]

    if not open_prs:
        print(f"[feedback] No open PRs found for {original_key}")
        db.ticket_finished(job_key, error=f"No open PRs found for {original_key}")
        sys.exit(1)

    print(f"[feedback] Found {len(open_prs)} open PR(s) for {original_key}")

    # Collect context for all PRs upfront so each Codex run sees the full picture
    all_pr_contexts: list[dict] = []
    for pr in open_prs:
        workspace = pr["workspace"]
        repo_slug = pr["repo_slug"]
        pr_id = pr["pr_id"]
        try:
            pr_data = fetch_pr(workspace, repo_slug, pr_id)
            source_branch = pr_data["source"]["branch"]["name"]
            pr_title = pr_data.get("title", "")
            diff = fetch_pr_diff(workspace, repo_slug, pr_id)
            all_pr_contexts.append({
                "pr_id": pr_id,
                "repo_slug": repo_slug,
                "workspace": workspace,
                "source_branch": source_branch,
                "title": pr_title,
                "diff": diff,
            })
            print(f"[feedback] Fetched context for {repo_slug}#{pr_id}")
        except Exception as exc:
            print(
                f"[feedback] WARNING: could not fetch context for {repo_slug}#{pr_id}: {exc}",
                file=sys.stderr,
            )

    if not all_pr_contexts:
        db.ticket_finished(job_key, error="Could not fetch context for any PR")
        sys.exit(1)

    db.ticket_phase(job_key, "codex-running", f"Running Codex for {len(all_pr_contexts)} PR(s)")

    errors: list[str] = []
    for pr in open_prs:
        try:
            process_pr(
                pr_record=pr,
                feedback=feedback,
                original_key=original_key,
                job_key=job_key,
                all_pr_contexts=all_pr_contexts,
            )
        except Exception as exc:
            label = f"{pr.get('repo_slug')}#{pr.get('pr_id')}"
            msg = f"Failed to process {label}: {exc}"
            print(f"[feedback] ERROR: {msg}\n{traceback.format_exc()}", file=sys.stderr)
            errors.append(msg)

    if errors and len(errors) == len(open_prs):
        db.ticket_finished(job_key, error="\n".join(errors))
        sys.exit(1)

    error_summary = "\n".join(errors) if errors else None
    db.ticket_finished(job_key, error=error_summary)
    print(f"[feedback] Done with job {job_key}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        job_key = sys.argv[2] if len(sys.argv) >= 3 else None
        if job_key:
            db.ticket_finished(job_key, error=traceback.format_exc())
        raise
