# PR Comment Bot — Design Spec

**Date:** 2026-03-28
**Status:** Approved

## Overview

Add the ability to mention the bot in Bitbucket PR comments (e.g., `@andrebot fix the null check`) so it processes the reviewer's feedback, applies the fix, pushes to the PR's source branch, and replies confirming the commit.

## Trigger

- Bitbucket webhook event: `pullrequest:comment_created`
- Bot is mentioned in the comment body (configurable via `BOT_MENTION` env var, default `@andrebot`)
- Only processes comments on PRs in `OPEN` state
- Ignores comments authored by `BITBUCKET_USER` (prevents loops)

## Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_MENTION` | `@andrebot` | Trigger phrase in PR comments |

### Prompt template

New setting `prompt_pr_comment` in the `settings` table (editable from dashboard). Template variables:

- `{comment}` — reviewer's comment text (with bot mention stripped)
- `{file_path}` — file the inline comment is on (empty for general comments)
- `{line}` — line number (empty for general comments)
- `{diff}` — the PR diff (full diff, or file-scoped diff for inline comments)
- `{pr_title}` — PR title
- `{source_branch}` — the PR's source branch name
- `{repo_slug}` — the repository slug

## Architecture

### Webhook handling (`webhook_server.py`)

The `handle_event` function branches based on payload shape:

- **JIRA event** (`webhookEvent` field present) — existing flow
- **Bitbucket PR comment** (`X-Event-Key: pullrequest:comment_created`) — new flow

The webhook handler header `X-Event-Key` is read in `do_POST` and passed to `handle_event`.

The queue becomes typed. Items are tuples:
- `("ticket", issue_key)` — existing JIRA ticket jobs
- `("pr_comment", workspace, repo_slug, pr_id, comment_id)` — PR comment jobs

The worker dispatches to either `process_ticket.py` or `process_pr_comment.py` based on the job type.

### PR comment processing (`process_pr_comment.py`)

New script, parallel to `process_ticket.py`. Receives args: `<workspace> <repo_slug> <pr_id> <comment_id>`.

**Step 1 — Fetch context from Bitbucket API:**
- PR details: source branch, destination branch, title, state
- The specific comment: text, and if inline: file path + line number
- The PR diff (full diff, plus file-specific diff for inline comments)

**Step 2 — Build prompt:**
- Reviewer's comment (stripped of `@andrebot` mention)
- File path and line context (if inline comment)
- Relevant diff section
- Instructions to fix the issue and commit

**Step 3 — Worktree flow:**
1. Find the repo locally by matching `repo_slug` to a directory under `WORKSPACE_PATH`
2. Create a worktree from the PR's source branch:
   ```
   git worktree add ../../worktrees/pr-fix-<pr_id>-<short> origin/<source_branch>
   ```
   (No new branch created — works in detached HEAD or tracks the remote branch)
3. Run Codex inside the worktree
4. Push the fix to the existing source branch:
   ```
   git push origin HEAD:<source_branch>
   ```
5. Clean up the worktree

**Step 4 — Reply to comment:**
- Post a reply to the original comment via Bitbucket API
- Include the commit hash in the reply (e.g., "Fixed in commit `abc123`")

**Step 5 — DB tracking:**
- Reuse the `tickets` table with synthetic key: `PR-<repo_slug>#<pr_id>-C<comment_id>`
- Summary field stores the comment text (truncated to 200 chars)
- Shows up in dashboard alongside ticket jobs with no UI changes

## Bitbucket API Endpoints Used

| Purpose | Method | Endpoint |
|---------|--------|----------|
| Get PR details | GET | `/2.0/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}` |
| Get PR diff | GET | `/2.0/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/diff` |
| Get comment | GET | `/2.0/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/comments/{comment_id}` |
| Reply to comment | POST | `/2.0/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/comments` |

## Error Handling

- **Bot mentions itself** — ignore comments authored by `BITBUCKET_USER`
- **Multiple mentions in one comment** — single job
- **Comment on outdated diff** — best-effort; if file no longer exists, Codex will see that
- **PR not OPEN** — skip processing, log a message
- **No matching local repo** — fail with clear error in dashboard
- **Codex fails** — same error handling as `process_ticket.py` (mark failed, log error)
- **Webhook signature** — validate with `WEBHOOK_SECRET` if set (same as JIRA)

## Database Changes

None. Reuses existing `tickets` table with synthetic keys and existing `settings` table for the prompt template.

## Files Changed

| File | Change |
|------|--------|
| `scripts/webhook_server.py` | Branch on event type, typed queue, dispatch to new script |
| `scripts/process_pr_comment.py` | **New** — PR comment processing flow |
| `scripts/dashboard.py` | Add `prompt_pr_comment` to `SETTINGS_DEFAULTS` |
| `env.example` | Add `BOT_MENTION` |
