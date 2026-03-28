# jira-bitbucket-worker

Automated code implementation pipeline. When a JIRA ticket is assigned to a designated bot account, this service fetches the ticket details and runs Codex CLI to implement the solution and open a Bitbucket PR. It also responds to PR comment mentions, applying fixes directly to the source branch.

## How It Works

### JIRA Ticket Processing

1. A JIRA ticket is assigned to the bot account
2. JIRA sends a webhook event to this server
3. The server queues the ticket and processes it sequentially
4. `process_ticket.py` fetches the full ticket from JIRA and builds a prompt
5. Codex CLI runs in the workspace root, picks the right repo, implements the solution, and opens a Bitbucket PR

### PR Comment Bot

1. Someone mentions the bot in a Bitbucket PR comment (e.g., `@andrebot fix the null check`)
2. Bitbucket sends a `pullrequest:comment_created` webhook event
3. `process_pr_comment.py` fetches the PR context, comment, and diff
4. Codex CLI runs in a temporary worktree from the source branch
5. The fix is committed and pushed to the PR's source branch
6. The bot replies to the comment with the commit hash

The PR comment bot supports both inline comments (on specific files/lines) and general PR comments. It only processes comments on open PRs and ignores its own comments to prevent loops.

---

## Web Dashboard

Access the dashboard at `http://localhost:8080/dashboard` (or your configured port).

**Features:**
- **Live worker status** with current job, queue size, and uptime
- **Ticket timeline** showing all jobs with status badges (queued, processing, codex-running, pushing, done, failed, cancelled)
- **Live log viewer** — click any ticket to stream Codex output in real time via SSE
- **Pull requests panel** with direct links to Bitbucket
- **Statistics** — tickets today/this week/total, success rate, average duration, total PRs
- **Webhook health** — last received time, total count, signature failures
- **Settings page** (`/dashboard/settings`) — edit the model and prompt templates without restarting
- **Job cancellation** — cancel a running job from the dashboard (kills the Codex process group)

---

## Prerequisites

- Python 3.9+
- [Codex CLI](https://github.com/openai/codex) installed and authenticated on the server
- A workspace directory containing all your repos as subdirectories
- A server reachable from the internet (JIRA Cloud and Bitbucket Cloud require HTTPS for webhooks)

---

## 1. Create a JIRA Service Account

1. Go to [admin.atlassian.com](https://admin.atlassian.com) and navigate to your organization
2. Click **Directory > Users > Invite users**
3. Create a new user with a dedicated email (e.g. `jira-bitbucket-worker@your-company.com`)
4. Assign it to the relevant JIRA projects with at least **Developer** role so it can be assigned tickets

### Get the Service Account's API Token

1. Log in to JIRA as the service account
2. Go to [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
3. Click **Create API token**, give it a name (e.g. `jira-bitbucket-worker`), and copy the token
4. This token goes into `JIRA_TOKEN` in your `.env`
5. The service account's email goes into `JIRA_USER`

### Get the Service Account's JIRA Account ID

You need the account ID (not the display name) for `TRIGGER_ASSIGNEE`. Run:

```bash
curl -u service-account@your-company.com:your-api-token \
  "https://your-org.atlassian.net/rest/api/3/myself"
```

Look for the `"accountId"` field in the response. It looks like `5e4135a3393ea90c94b2efa5`.

---

## 2. Configure Bitbucket

1. In Bitbucket Cloud, go to your workspace **Settings > Access tokens**
2. Create a new workspace access token with **Repositories: Write** and **Pull requests: Write** permissions
3. Copy the token into `BITBUCKET_TOKEN` in your `.env`

---

## 3. Configure Environment

```bash
cp env.example .env
```

Edit `.env`:

```env
# JIRA Cloud
JIRA_URL=https://your-org.atlassian.net
JIRA_USER=service-account@your-company.com
JIRA_TOKEN=your_jira_api_token

# Bitbucket Cloud
BITBUCKET_WORKSPACE=your-bitbucket-workspace-slug
# Optional: override Bitbucket credentials (defaults to JIRA_USER/JIRA_TOKEN)
BITBUCKET_USER=your-email@example.com
BITBUCKET_TOKEN=your_atlassian_api_token

# Absolute path to the directory containing all your repos
WORKSPACE_PATH=/home/user/workspace

# Account ID of the JIRA user that triggers the bot when assigned a ticket
TRIGGER_ASSIGNEE=5e4135a3393ea90c94b2efa5

# Webhook server
WEBHOOK_PORT=8080
# Must match the secret you set in the JIRA webhook (leave empty to disable)
WEBHOOK_SECRET=your_shared_secret

# PR Comment Bot
# The mention trigger in Bitbucket PR comments (e.g., "@andrebot fix this")
BOT_MENTION=@andrebot
```

---

## 4. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 5. Set Up HTTPS with Cloudflare Tunnel (Required for JIRA/Bitbucket Cloud)

JIRA Cloud and Bitbucket Cloud only send webhooks to HTTPS endpoints. Cloudflare Tunnel exposes your local server to the internet over HTTPS without opening firewall ports or managing certificates.

### Install cloudflared

```bash
# macOS
brew install cloudflare/cloudflare/cloudflared

# Linux
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared
```

### Authenticate and Create a Tunnel

```bash
# Log in (opens browser)
cloudflared tunnel login

# Create a named tunnel
cloudflared tunnel create jira-bitbucket-worker

# Route a hostname to the tunnel (must be a domain managed by Cloudflare)
cloudflared tunnel route dns jira-bitbucket-worker webhook.your-domain.com
```

### Configure the Tunnel

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: jira-bitbucket-worker
credentials-file: /home/user/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: webhook.your-domain.com
    service: http://localhost:8080
  - service: http_status:404
```

### Run the Tunnel

```bash
cloudflared tunnel run jira-bitbucket-worker
```

Your webhook server is now reachable at `https://webhook.your-domain.com`.

### Run as a Background Service

```bash
sudo cloudflared service install
sudo systemctl start cloudflared
```

---

## 6. Set Up Webhooks

### JIRA Webhook (for ticket processing)

1. In JIRA, go to **Project Settings > Webhooks** (or **Settings > System > Webhooks** for org-level)
2. Click **Create webhook**
3. Set the URL to `https://webhook.your-domain.com/`
4. Under **Secret**, enter the same value as `WEBHOOK_SECRET` in your `.env`
5. Under **Events**, check **Issue > updated**
6. Optionally scope it with a JQL filter:
   ```
   project = YOUR_PROJECT AND assignee = "<bot-account-id>"
   ```
7. Click **Create**

### Bitbucket Webhook (for PR comment bot)

1. In each Bitbucket repository, go to **Repository settings > Webhooks**
2. Click **Add webhook**
3. Set the URL to `https://webhook.your-domain.com/`
4. Under **Secret**, enter the same value as `WEBHOOK_SECRET`
5. Under **Triggers**, select **Pull Request > Comment created**
6. Click **Save**

### Verify Incoming Events

To find tickets recently assigned to the bot:

```
assignee = "<bot-account-id>" AND updated >= -1d ORDER BY updated DESC
```

---

## 7. Run the Server

```bash
python3 scripts/webhook_server.py
```

You should see:
```
[webhook] Listening on port 8080
```

When a ticket is assigned to the bot account:
```
[webhook] Queuing WEB-123 (queue size: 0)
[worker] Processing WEB-123 (queue size: 0 remaining)
[worker] Finished WEB-123
```

### Run as a Background Service (systemd)

```ini
# /etc/systemd/system/jira-bitbucket-worker.service
[Unit]
Description=jira-bitbucket-worker JIRA webhook listener
After=network.target

[Service]
WorkingDirectory=/path/to/jira-bitbucket-worker
ExecStart=/usr/bin/python3 scripts/webhook_server.py
Restart=always
EnvironmentFile=/path/to/jira-bitbucket-worker/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable jira-bitbucket-worker
sudo systemctl start jira-bitbucket-worker
```

---

## Dashboard Settings

The settings page at `/dashboard/settings` lets you configure the bot without restarting:

- **Model**: The model to pass to `codex exec -m` (e.g., `o3`, `claude-sonnet-4-20250514`). Leave empty to use the Codex default.
- **Ticket prompt context**: Template for building the JIRA ticket prompt. Available variables: `{key}`, `{summary}`, `{description}`, `{components}`, `{labels}`, `{priority}`, `{issue_type}`, `{acceptance_criteria}`, `{workspace_path}`, `{run_manifest}`
- **Ticket prompt instructions**: Workflow instructions appended to the ticket prompt.
- **PR comment prompt**: Template for building the PR comment prompt. Available variables: `{comment}`, `{file_path}`, `{line}`, `{diff}`, `{pr_title}`, `{source_branch}`, `{repo_slug}`

All settings are persisted to a local SQLite database and can be reset to defaults from the settings page.

---

## Manual Testing

You can trigger processing for a specific ticket without waiting for a webhook:

```bash
python3 scripts/process_ticket.py WEB-123
```
