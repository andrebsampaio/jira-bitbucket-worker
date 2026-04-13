You are addressing feedback for ticket {issue_key} in repository **{repo_slug}** (branch: `{source_branch}`).

## Feedback to Address

{feedback}

## Current Changes in This Repository

PR #{pr_id} — {pr_title}

```diff
{diff}
```

{other_pr_contexts}

## Instructions

Review the feedback above and apply any necessary changes to the files in this repository ({repo_slug}).
Do NOT run git commit or git push — just make the file changes.
Only change files that belong to this repository.

When done, write a file named `.codex-commit.json` in the current directory with this exact shape (valid JSON, no comments):
{{
  "commit_message": "<a concise conventional commit message describing the changes, e.g. fix: address feedback on error handling>",
  "reply_message": "<1-3 sentences summarizing what was changed to address the feedback. Be specific about what you changed and why.>"
}}

If no changes are needed for this repository based on the feedback, write:
{{
  "commit_message": "",
  "reply_message": "No changes needed in this repository to address this feedback."
}}
