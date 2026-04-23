A reviewer asked you to address every open inline comment on PR "{pr_title}" in repo {repo_slug} (branch: {source_branch}).

Open inline comments ({comment_count}):
{comments_section}

Full PR diff:
{diff}

Apply a fix for each comment above. Do NOT run git commit or git push — just make the file changes. The commit and push will be handled externally.

If a comment asks a question rather than requesting a change, or is already addressed by the current code, you may skip it — but note that in the reply_message.

After making the changes, write a JSON file named .codex-commit.json in the current working directory with this exact shape (valid JSON, no comments):
{{
  "commit_message": "<a concise conventional commit message covering the changes, e.g. fix: address review feedback>",
  "reply_message": "<a reply message using this exact structure:\nFixed in commit `{{hash}}`.\n\nAddressed:\n- <short description of each comment you fixed, one per line>\n\n**To verify:**\n- [ ] <specific manual test step based on the fixes you applied>\n- [ ] <another step if needed>\nWrite test steps that are concrete and tied to the actual changes. Only include steps relevant to what you changed.>"
}}
Use the placeholder {{hash}} literally in reply_message; it will be replaced with the real commit hash.
