# Roadmap

## Phase 1 — Security Hardening

The current implementation is functional but not production-safe.

- **Webhook signature validation** — enforce `WEBHOOK_SECRET` as required, reject unsigned requests
- **Secrets management** — move credentials out of `.env` files into a secrets manager (AWS Secrets Manager, HashiCorp Vault, or 1Password Secrets Automation)
- **Least-privilege service account** — scope the JIRA bot account to only the projects it needs; scope the Bitbucket token to specific repositories
- **Request allowlisting** — only accept webhook requests from [Atlassian's IP ranges](https://support.atlassian.com/organization-administration/docs/ip-addresses-and-domains-for-atlassian-cloud-products/)
- **Audit logging** — persist a structured log of every ticket received, processed, and its outcome (success / failure / skipped) to a file or external sink
- **Rate limiting** — cap how many tickets can be enqueued per hour to prevent abuse or runaway assignment loops
- **Codex sandbox** — run the Codex process in an isolated environment (Docker container, firejail, or a dedicated VM) so it cannot access credentials or other repos outside the workspace

---

## Phase 2 — Guardrails

Prevent bad output from reaching main branches.

- **Required tests** — prompt Codex to always write tests; fail the pipeline if the test suite does not pass before the PR is opened
- **Static analysis gate** — run linters and type checkers on Codex output before pushing; block the PR if they fail
- **PR size limit** — reject or flag PRs that touch more than N files, as a signal that Codex may have gone off-track
- **Branch protection** — ensure target repos require at least one human approval before merging any bot-opened PR
- **Dry-run mode** — an environment flag that runs the full pipeline but stops before pushing or opening a PR, for safe testing
- **Rollback hook** — if a bot-opened PR is closed without merging after N days, automatically comment on the JIRA ticket and transition it back to "To Do"

---

## Phase 3 — Dashboard

Visibility into what the bot is doing.

- **Ticket queue view** — real-time list of tickets: queued, in progress, completed, failed
- **Per-ticket timeline** — timestamps for when a ticket was received, when Codex started, when the PR was opened
- **PR links** — each ticket row links directly to the Bitbucket PR
- **Log viewer** — tail the Codex output for in-progress tickets directly in the UI
- **Failure details** — for failed tickets, show the error and a button to requeue
- **Tech**: lightweight Python web server (FastAPI + HTMX or a simple React frontend) backed by a SQLite database that the worker writes to

---

## Phase 4 — Slack Integration

Human-in-the-loop for cases where Codex needs clarification.

- **Question detection** — parse Codex stdout for signals that it is blocked or uncertain (e.g. it outputs a question or stops early)
- **Slack notification** — when a question is detected, send a message to a configured Slack user or channel with the ticket key, the question, and a link to the JIRA ticket
- **Reply-to-continue** — listen for a reply in the Slack thread, feed it back to Codex as additional context, and resume the pipeline
- **Completion notifications** — notify the same channel when a PR is opened, including the PR link and a one-line summary of what was implemented
- **Failure alerts** — ping on pipeline failure with the error and a requeue button (Slack Block Kit action → webhook)

---

## Phase 5 — Feedback Loop (Dogfooding)

The system improves itself by processing its own tickets.

- **PR review feedback** — when a bot-opened PR receives review comments, extract them and store them as structured feedback tied to the ticket type and component
- **Prompt improvement pipeline** — periodically summarize accumulated feedback into prompt refinements; create a JIRA ticket in this project's own board to apply them
- **Outcome tracking** — track whether bot PRs are merged as-is, merged after revisions, or closed; use this as a signal for prompt quality
- **Self-assignment** — create a JIRA project for this tool's own backlog; assign tickets to the bot so it can implement its own improvements
- **Prompt versioning** — version the Codex prompt template so you can A/B test changes and correlate them with merge rates

---

## Out of Scope (for now)

- Multi-bot parallelism (intentionally sequential for safety)
- Auto-merging PRs without human approval
- Support for GitHub or GitLab (Bitbucket only for now)
