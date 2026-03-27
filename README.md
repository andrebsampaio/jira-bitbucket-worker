# AndreBot

Automated code implementation pipeline. When a JIRA ticket is assigned to a designated bot account, this service fetches the ticket details and runs Codex CLI to implement the solution and open a Bitbucket PR — all without human intervention.

## How It Works

1. A JIRA ticket is assigned to the bot account
2. JIRA sends a webhook event to this server
3. The server queues the ticket and processes it sequentially
4. `process_ticket.py` fetches the full ticket from JIRA and builds a prompt
5. Codex CLI runs in the workspace root, picks the right repo, implements the solution, and opens a Bitbucket PR

---

## Prerequisites

- Python 3.9+
- [Codex CLI](https://github.com/openai/codex) installed and authenticated on the server
- A workspace directory containing all your repos as subdirectories
- A server reachable from the internet (JIRA Cloud requires HTTPS for webhooks)

---

## 1. Create a JIRA Service Account

1. Go to [admin.atlassian.com](https://admin.atlassian.com) and navigate to your organization
2. Click **Directory → Users → Invite users**
3. Create a new user with a dedicated email (e.g. `andrebot@your-company.com`)
4. Assign it to the relevant JIRA projects with at least **Developer** role so it can be assigned tickets

### Get the Service Account's API Token

1. Log in to JIRA as the service account
2. Go to [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
3. Click **Create API token**, give it a name (e.g. `andrebot`), and copy the token
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

1. In Bitbucket Cloud, go to your workspace **Settings → Access tokens**
2. Create a new workspace access token with **Repositories: Write** and **Pull requests: Write** permissions
3. Copy the token into `BITBUCKET_API_KEY` in your `.env`

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
BITBUCKET_API_KEY=your_bitbucket_access_token

# Absolute path to the directory containing all your repos
WORKSPACE_PATH=/home/user/workspace

# Account ID of the JIRA user that triggers the bot when assigned a ticket
TRIGGER_ASSIGNEE=5e4135a3393ea90c94b2efa5

# Webhook server
WEBHOOK_PORT=8080
# Must match the secret you set in the JIRA webhook (leave empty to disable)
WEBHOOK_SECRET=your_shared_secret
```

---

## 4. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 5. Set Up HTTPS with Cloudflare Tunnel (Required for JIRA Cloud)

JIRA Cloud only sends webhooks to HTTPS endpoints. Cloudflare Tunnel exposes your local server to the internet over HTTPS without opening firewall ports or managing certificates.

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
cloudflared tunnel create andrebot

# Route a hostname to the tunnel (must be a domain managed by Cloudflare)
cloudflared tunnel route dns andrebot webhook.your-domain.com
```

### Configure the Tunnel

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: andrebot
credentials-file: /home/user/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: webhook.your-domain.com
    service: http://localhost:8080
  - service: http_status:404
```

### Run the Tunnel

```bash
cloudflared tunnel run andrebot
```

Your webhook server is now reachable at `https://webhook.your-domain.com`.

### Run as a Background Service

```bash
sudo cloudflared service install
sudo systemctl start cloudflared
```

---

## 6. Set Up the JIRA Webhook

1. In JIRA, go to **Project Settings → Webhooks** (or **Settings → System → Webhooks** for org-level)
2. Click **Create webhook**
3. Set the URL to `https://your-server.example.com/`
4. Under **Secret**, enter the same value as `WEBHOOK_SECRET` in your `.env`
5. Under **Events**, check **Issue → updated**
6. Optionally scope it with a JQL filter to only fire for your project:
   ```
   project = YOUR_PROJECT AND assignee = "<bot-account-id>"
   ```
7. Click **Create**

### Verify Incoming Events

To find tickets recently assigned to the bot and verify events are firing:

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
# /etc/systemd/system/andrebot.service
[Unit]
Description=AndreBot JIRA webhook listener
After=network.target

[Service]
WorkingDirectory=/path/to/andrebot
ExecStart=/usr/bin/python3 scripts/webhook_server.py
Restart=always
EnvironmentFile=/path/to/andrebot/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable andrebot
sudo systemctl start andrebot
```

---

## Manual Testing

You can trigger processing for a specific ticket without waiting for a webhook:

```bash
python3 scripts/process_ticket.py WEB-123
```
