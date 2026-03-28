#!/usr/bin/env python3
"""
Fetches a JIRA ticket and runs codex with full context in the workspace root.
Codex is instructed to use a git worktree per ticket so the main clone is not modified directly.
After Codex finishes, creates the Bitbucket pull request via the REST API (not in Codex).
Usage: python3 process_ticket.py <ISSUE_KEY>
"""

import json
import os
import re
import subprocess
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

JIRA_URL = os.environ["JIRA_URL"].rstrip("/")
JIRA_USER = os.environ["JIRA_USER"]
JIRA_TOKEN = os.environ["JIRA_TOKEN"]
BITBUCKET_WORKSPACE = os.environ["BITBUCKET_WORKSPACE"]
BITBUCKET_API_KEY = os.environ["BITBUCKET_API_KEY"]
WORKSPACE_PATH = os.environ["WORKSPACE_PATH"]

RUN_MANIFEST_NAME = ".jira-bitbucket-worker-run.json"
BITBUCKET_API = "https://api.bitbucket.org/2.0"


def fetch_issue(issue_key: str) -> dict:
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}"
    response = requests.get(url, auth=(JIRA_USER, JIRA_TOKEN))
    response.raise_for_status()
    return response.json()


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


def build_prompt(issue: dict) -> str:
    fields = issue["fields"]
    key = issue["key"]
    summary = fields.get("summary", "")
    description = extract_text(fields.get("description"))
    components = ", ".join(c["name"] for c in fields.get("components", []))
    labels = ", ".join(fields.get("labels", []))
    priority = (fields.get("priority") or {}).get("name", "")
    issue_type = (fields.get("issuetype") or {}).get("name", "")

    # Pull acceptance criteria from custom field if present (common field name variants)
    acceptance_criteria = ""
    for field_key, field_val in fields.items():
        if "acceptance" in field_key.lower() or "criteria" in field_key.lower():
            acceptance_criteria = extract_text(field_val)
            break

    sections = [
        f"You are implementing JIRA ticket {key}.",
        f"Type: {issue_type}" if issue_type else "",
        f"Priority: {priority}" if priority else "",
        f"Summary: {summary}",
        f"Components: {components}" if components else "",
        f"Labels: {labels}" if labels else "",
        "",
        "Description:",
        description if description else "(no description provided)",
    ]

    if acceptance_criteria:
        sections += ["", "Acceptance Criteria:", acceptance_criteria]

    sections += [
        "",
        "Instructions:",
        "1. Examine the components listed above to identify which repository to work in.",
        "   The workspace contains all repositories as subdirectories.",
        "2. Use a git worktree for this ticket so the main clone stays untouched. From the chosen repo's",
        "   root (the subdirectory under the workspace), run something like:",
        "   git worktree add ../worktrees/<ISSUE-KEY>-<short-slug> -b <branch-name>",
        "   (create ../worktrees if needed). Put the worktree under the workspace root, e.g.",
        "   worktrees/PROJ-123-feature-name relative to the workspace.",
        "3. Do all implementation, tests, commits, and pushes only inside that worktree directory.",
        "4. Use a branch name that includes the ticket key (e.g. feature/PROJ-123-short-description).",
        "5. Commit with a meaningful message referencing the ticket key.",
        "6. Push the branch from the worktree directory.",
        "7. Do NOT create a pull request yourself. After a successful push, write a JSON file at the",
        f"   workspace root (same directory that contains all repos): {RUN_MANIFEST_NAME}",
        "   with this exact shape (valid JSON, no comments):",
        '   {"issue_key": "<same as this ticket>", "repo_path": "<worktree path relative to workspace root>", "branch": "<pushed branch name>"}',
        f"   Example repo_path if the worktree is {WORKSPACE_PATH}/worktrees/PROJ-123-my-feature: "
        '"worktrees/PROJ-123-my-feature".',
    ]

    return "\n".join(line for line in sections)


def _bb_headers() -> dict:
    return {
        "Authorization": f"Bearer {BITBUCKET_API_KEY}",
        "Content-Type": "application/json",
    }


def parse_bitbucket_remote(url: str) -> tuple[str, str] | None:
    """Return (workspace, repo_slug) from a Bitbucket Cloud git remote URL."""
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
    response = requests.get(url, headers=_bb_headers())
    response.raise_for_status()
    data = response.json()
    main = (data.get("mainbranch") or {}).get("name")
    if main:
        return main
    return "main"


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
    response = requests.post(url, headers=_bb_headers(), json=body)
    if response.status_code == 409:
        raise RuntimeError(
            "Pull request already exists or branch state conflicts. "
            f"Bitbucket response: {response.text}"
        )
    response.raise_for_status()
    return response.json()


def load_run_manifest(issue_key: str) -> dict | None:
    path = os.path.join(WORKSPACE_PATH, RUN_MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("issue_key") != issue_key:
        print(
            f"[process] Manifest issue_key mismatch (expected {issue_key!r}, "
            f"got {data.get('issue_key')!r}); skipping PR creation.",
            file=sys.stderr,
        )
        return None
    for key in ("repo_path", "branch"):
        if not data.get(key):
            print(f"[process] Manifest missing {key!r}; skipping PR creation.", file=sys.stderr)
            return None
    return data


def _git_text(args: list[str], cwd: str) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        return ""
    return r.stdout


def _current_branch(repo_root: str) -> str | None:
    name = _git_text(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_root).strip()
    if not name or name == "HEAD":
        return None
    return name


def _branch_exists_on_origin(repo_root: str, branch: str) -> bool:
    out = _git_text(["git", "ls-remote", "--heads", "origin", branch], repo_root).strip()
    return bool(out)


def _iter_git_repo_roots(workspace: str):
    """Yield top-level git worktrees/clones under workspace (does not descend into nested repos)."""
    workspace = os.path.abspath(workspace)
    if not os.path.isdir(workspace):
        return
    for root, dirs, _files in os.walk(workspace):
        if ".git" in dirs:
            dirs.remove(".git")
        git_marker = os.path.join(root, ".git")
        if os.path.isdir(git_marker) or os.path.isfile(git_marker):
            yield root
            dirs.clear()


def _branches_containing_ticket(repo_root: str, issue_key: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for ref in ("refs/heads/", "refs/remotes/origin/"):
        out = _git_text(
            ["git", "for-each-ref", "--format=%(refname:short)", ref],
            repo_root,
        )
        for line in out.splitlines():
            short = line.strip()
            if issue_key not in short:
                continue
            if short.startswith("origin/"):
                short = short[len("origin/") :]
            if not short or short == "HEAD":
                continue
            if short not in seen:
                seen.add(short)
                found.append(short)
    return found


def infer_run_from_git(issue_key: str) -> dict | None:
    """
    When Codex omits the manifest, find repo + branch by scanning for branches whose name
    contains the issue key (e.g. feature/WEB-5865-description).
    """
    base = os.path.abspath(WORKSPACE_PATH)
    triples: list[tuple[str, str, str]] = []  # (repo_path_rel, branch, repo_root_abs)
    for repo_root in _iter_git_repo_roots(base):
        rel = os.path.relpath(repo_root, base)
        if rel == ".":
            rel = "."
        for branch in _branches_containing_ticket(repo_root, issue_key):
            triples.append((rel, branch, repo_root))

    if not triples:
        return None

    uniq: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rel, br, root in triples:
        key = (rel, br)
        if key not in seen:
            seen.add(key)
            uniq.append((rel, br, root))

    def pick() -> tuple[str, str, str] | None:
        if len(uniq) == 1:
            return uniq[0]
        # Prefer the checkout whose HEAD is a branch containing the ticket key.
        for rel, br, root in uniq:
            head = _current_branch(root)
            if head and issue_key in head:
                return (rel, head, root)
        # Prefer a unique branch that exists on origin (pushed).
        pushed = [(rel, br, root) for rel, br, root in uniq if _branch_exists_on_origin(root, br)]
        if len(pushed) == 1:
            return pushed[0]
        return None

    chosen = pick()
    if not chosen:
        summary = [(u[0], u[1]) for u in uniq]
        print(
            f"[process] Could not infer repo/branch for {issue_key} (ambiguous): {summary!r}. "
            f"Create {RUN_MANIFEST_NAME} or leave only one matching branch.",
            file=sys.stderr,
        )
        return None

    rel, br, _root = chosen
    print(
        f"[process] No manifest; inferred repo_path={rel!r} branch={br!r} from git.",
        file=sys.stderr,
    )
    return {"issue_key": issue_key, "repo_path": rel, "branch": br}


def create_pr_after_codex(issue: dict, issue_key: str) -> None:
    manifest = load_run_manifest(issue_key)
    if not manifest:
        manifest = infer_run_from_git(issue_key)
    if not manifest:
        print(
            f"[process] No {RUN_MANIFEST_NAME} and could not infer repo/branch; "
            "open the PR manually if needed.",
            file=sys.stderr,
        )
        return

    repo_path = manifest["repo_path"].strip().strip("/")
    branch = manifest["branch"]
    repo_root = os.path.join(WORKSPACE_PATH, repo_path)
    # Worktrees use a .git file; normal repos use a .git directory.
    if not os.path.exists(os.path.join(repo_root, ".git")):
        print(f"[process] Not a git repo at {repo_root}; skipping PR creation.", file=sys.stderr)
        return

    remote_url = git_remote_origin(repo_root)
    if not remote_url:
        print(f"[process] Could not read git remote for {repo_root}; skipping PR creation.", file=sys.stderr)
        return

    parsed = parse_bitbucket_remote(remote_url)
    if not parsed:
        print(
            f"[process] origin URL does not look like Bitbucket Cloud: {remote_url!r}; "
            "skipping PR creation.",
            file=sys.stderr,
        )
        return

    workspace, repo_slug = parsed
    dest = fetch_repo_mainbranch(workspace, repo_slug)

    fields = issue["fields"]
    summary = fields.get("summary", "")
    title = manifest.get("title") or f"[{issue_key}] {summary}"
    description = (
        manifest.get("description")
        or f"Implements {issue_key}.\n\n{(fields.get('description') and extract_text(fields.get('description'))) or ''}".strip()
    )

    print(f"[process] Creating Bitbucket PR: {workspace}/{repo_slug} {branch} -> {dest}")
    pr = create_bitbucket_pr(workspace, repo_slug, title, description, branch, dest)
    links = pr.get("links", {})
    html = (links.get("html") or {}).get("href") if isinstance(links, dict) else None
    if html:
        print(f"[process] Pull request: {html}")
    else:
        print(f"[process] Pull request created: {pr.get('id')}")

    try:
        os.remove(os.path.join(WORKSPACE_PATH, RUN_MANIFEST_NAME))
    except OSError:
        pass


def main():
    if len(sys.argv) < 2:
        print("Usage: process_ticket.py <ISSUE_KEY>")
        sys.exit(1)

    issue_key = sys.argv[1]
    print(f"[process] Fetching {issue_key} from JIRA...")

    issue = fetch_issue(issue_key)
    manifest_path = os.path.join(WORKSPACE_PATH, RUN_MANIFEST_NAME)
    try:
        os.remove(manifest_path)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"[process] Warning: could not remove old manifest: {e}", file=sys.stderr)

    prompt = build_prompt(issue)

    print(f"[process] Starting codex for {issue_key} in {WORKSPACE_PATH}")
    print(f"[process] Prompt:\n{'-'*60}\n{prompt}\n{'-'*60}")

    subprocess.run(
        ["codex", "exec", "--skip-git-repo-check", prompt],
        cwd=WORKSPACE_PATH,
        check=True,
    )

    print(f"[process] codex finished for {issue_key}")
    create_pr_after_codex(issue, issue_key)


if __name__ == "__main__":
    main()
