A reviewer commented on PR "{pr_title}" in repo {repo_slug} (branch: {source_branch}).
{conversation}
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
  "reply_message": "<a reply message using this exact structure:\nFixed in commit `{{hash}}`.\n\n**To verify:**\n- [ ] <specific manual test step based on the fix you applied>\n- [ ] <another step if needed>\nWrite test steps that are concrete and tied to the actual change (e.g. 'Click the trim handle and drag left — the clip should shorten without freezing' not 'Test the fix'). Only include steps relevant to what you changed.>"
}}
Use the placeholder {{hash}} literally in reply_message; it will be replaced with the real commit hash.
