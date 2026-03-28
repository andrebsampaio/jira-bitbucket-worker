#!/usr/bin/env python3
"""
Fetches a JIRA ticket and runs codex with full context in the workspace root.
Usage: python3 process_ticket.py <ISSUE_KEY>
"""

import os
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
        "2. Implement a solution that satisfies the ticket requirements.",
        "3. Write tests where appropriate.",
        "4. Create a branch with a descriptive name that includes the ticket key (e.g. feature/PROJ-123-short-description).",
        "5. Commit your changes with a meaningful commit message referencing the ticket key.",
        "6. Push the branch to the remote.",
        f"7. Create a Pull Request on Bitbucket Cloud (workspace: {BITBUCKET_WORKSPACE}) with:",
        f"   - Title: [{key}] {summary}",
        f"   - Description: Implements {key}. Include a summary of changes made.",
        "   - Target branch: main (or the repository's default branch if different).",
        "   Use the Bitbucket REST API (https://api.bitbucket.org/2.0) with the API key",
        "   available in the BITBUCKET_API_KEY environment variable as a Bearer token:",
        "   Authorization: Bearer $BITBUCKET_API_KEY",
    ]

    return "\n".join(line for line in sections)


def main():
    if len(sys.argv) < 2:
        print("Usage: process_ticket.py <ISSUE_KEY>")
        sys.exit(1)

    issue_key = sys.argv[1]
    print(f"[process] Fetching {issue_key} from JIRA...")

    issue = fetch_issue(issue_key)
    prompt = build_prompt(issue)

    print(f"[process] Starting codex for {issue_key} in {WORKSPACE_PATH}")
    print(f"[process] Prompt:\n{'-'*60}\n{prompt}\n{'-'*60}")

    subprocess.run(
        ["codex", "exec", "--skip-git-repo-check", prompt],
        cwd=WORKSPACE_PATH,
        check=True,
    )

    print(f"[process] codex finished for {issue_key}")


if __name__ == "__main__":
    main()
