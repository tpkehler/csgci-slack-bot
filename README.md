# csgci-slack-bot

Slack bot for taking channel conversations to GCI Jams.

## Slash commands

| Command | Description |
|---|---|
| `/jam "question"` | Import the last 50 messages from the current channel into a new GCI Jam |
| `/jam connect` | DM the user a link to connect their GCI account |

## How it works

1. User runs `/jam "Should we pivot to enterprise?"`
2. Bot fetches the last N messages from the channel via Slack API
3. Calls `POST /api/jams/import-from-conversation` on the GCI backend
4. Backend resolves Slack user identities → GCI participants (email match or placeholder)
5. LLM extracts distinct propositions from the conversation thread
6. Posts back the Jam URL + Block Kit card with an "Open Jam" button
7. Unmatched users get a personal claim-account invite link

## Setup

### 1. Create a Slack App

1. Go to https://api.slack.com/apps → **Create New App** → From manifest
2. Use the manifest in `slack_app_manifest.yaml`
3. Install to your workspace

Required scopes:
- `channels:history`, `groups:history`, `im:history`
- `users:read`, `users:read.email`
- `chat:write`, `commands`

Enable **Socket Mode** (Settings → Socket Mode) and generate an App-level token.

### 2. Configure environment

```bash
cp .env.example .env
# Fill in SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_SIGNING_SECRET
# Set GCI_API_KEY to your service account crc_... key
# Set GCI_OWNER_ID to the GCI participant UUID for the bot account
```

### 3. Install and run

```bash
uv sync
uv run python -m csgci_slack_bot.bot
```

### 4. Deploy on Render

Add environment variables from `.env.example` to the Render service dashboard.
Start command: `python -m csgci_slack_bot.bot`

## Identity resolution

When a user `/jam`s for the first time, the bot tries to match their Slack email
to an existing GCI account. If matched, the link is stored in `platform_identity_links`
and the user gets full attribution in future Jams without any action on their part.

Unmatched users receive a one-time claim link (`/claim/{token}`) to register and
retrospectively claim their propositions.

Users can also run `/jam connect` at any time to get a verified link flow.
