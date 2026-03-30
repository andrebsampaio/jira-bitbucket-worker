A reviewer commented on PR "{pr_title}" in repo {repo_slug} (branch: {source_branch}).

Reviewer's comment:
{comment}

File: {file_path}
Line: {line}

Diff:
{diff}

Apply the requested fix. Do NOT run git commit or git push — just make the file changes. The commit and push will be handled externally.

After making the changes, write a JSON file named .codex-commit.json in the current working directory with this exact shape (valid JSON, no comments):
{{
  "commit_message": "<a concise conventional commit message describing the fix, e.g. fix: correct null check in Foo.bar>",
  "reply_message": "<a short message to post as a reply to the reviewer, e.g. Fixed in commit `{{hash}}` — adjusted the null check as requested>"
}}
Use the placeholder {{hash}} literally in reply_message; it will be replaced with the real commit hash.
