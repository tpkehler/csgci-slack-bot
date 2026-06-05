"""
GCI Slack Bot

Slash commands:
  /jam "<question>"       — import the current channel's recent thread into a GCI Jam
  /jam connect            — DM the user a link to connect their GCI account

The bot reads the last N messages from the current channel, calls
POST /api/jams/import-from-conversation, and posts back the Jam URL.

Setup (Slack App manifest):
  - Scopes:  channels:history, groups:history, im:history, users:read,
             users:read.email, chat:write, commands
  - Slash command: /jam
  - Socket Mode enabled (or HTTP endpoint at /slack/events)

Environment variables: see .env.example
"""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .gci_client import GCIClient

load_dotenv()

logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN     = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN     = os.environ["SLACK_APP_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
GCI_OWNER_ID        = os.environ.get("GCI_OWNER_ID", "")

THREAD_HISTORY_LIMIT = 50   # messages to fetch from channel when /jam is invoked

app   = AsyncApp(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
gci   = GCIClient()


# ── /jam command ──────────────────────────────────────────────────────────────

@app.command("/jam")
async def handle_jam_command(ack, command, client, respond):
    await ack()

    text    = (command.get("text") or "").strip()
    channel = command["channel_id"]
    user_id = command["user_id"]

    # /jam connect — identity linking flow
    if text.lower() == "connect":
        await _handle_connect(user_id, client, respond)
        return

    # Require a prompt
    if not text:
        await respond(
            "Please provide a question for the Jam.\n"
            "Usage: `/jam Should we expand into enterprise accounts?`"
        )
        return

    await respond(f"⏳ Creating a GCI Jam: *{text}* — fetching conversation…")

    try:
        messages = await _fetch_channel_messages(channel, client, limit=THREAD_HISTORY_LIMIT)
        user_profiles = await _fetch_user_profiles(
            {m["user"] for m in messages if m.get("user")}, client
        )

        gci_messages = [
            {
                "platform_user_id": m["user"],
                "display_name":     user_profiles.get(m["user"], {}).get("real_name", m["user"]),
                "email":            user_profiles.get(m["user"], {}).get("email", ""),
                "text":             m.get("text", ""),
                "timestamp":        m.get("ts", ""),
            }
            for m in messages
            if m.get("user") and m.get("text")
        ]

        result = await gci.import_conversation(
            owner_id=GCI_OWNER_ID,
            prompt=text,
            platform="slack",
            messages=gci_messages,
            title=text[:80],
        )

        jam_url   = result.get("jam_url", "")
        n_props   = result.get("propositions_created", 0)
        n_unmatched = result.get("unmatched_count", 0)

        blocks = _jam_created_blocks(
            prompt=text,
            jam_url=jam_url,
            n_props=n_props,
            unmatched_users=[
                u for u in result.get("matched_users", []) if not u.get("matched")
            ],
        )
        await respond(blocks=blocks, text=f"GCI Jam created: {jam_url}")

    except Exception as e:
        logger.exception("Failed to create Jam from Slack command")
        await respond(f"❌ Failed to create Jam: {e}")


# ── /jam connect ──────────────────────────────────────────────────────────────

async def _handle_connect(user_id: str, client, respond) -> None:
    csweb = os.environ.get("CSWEB_BASE_URL", "https://crowdsmart.io")
    connect_url = f"{csweb}/connect/slack?slack_user_id={user_id}"
    try:
        await client.chat_postMessage(
            channel=user_id,
            text=(
                f"👋 Connect your GCI account to get full attribution in Jams:\n"
                f"{connect_url}"
            ),
        )
        await respond("📬 Check your DMs — I've sent you a link to connect your GCI account.")
    except Exception as e:
        await respond(f"❌ Couldn't send DM: {e}")


# ── Slack API helpers ─────────────────────────────────────────────────────────

async def _fetch_channel_messages(channel: str, client, limit: int = 50) -> list[dict]:
    result = await client.conversations_history(channel=channel, limit=limit)
    return result.get("messages", [])


async def _fetch_user_profiles(user_ids: set[str], client) -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    for uid in user_ids:
        try:
            info = await client.users_info(user=uid, include_locale=False)
            u    = info.get("user", {})
            profiles[uid] = {
                "real_name": u.get("real_name") or u.get("name", uid),
                "email":     (u.get("profile") or {}).get("email", ""),
            }
        except Exception:
            profiles[uid] = {"real_name": uid, "email": ""}
    return profiles


# ── Block Kit message ─────────────────────────────────────────────────────────

def _jam_created_blocks(
    *,
    prompt: str,
    jam_url: str,
    n_props: int,
    unmatched_users: list[dict],
) -> list[dict]:
    csweb = os.environ.get("CSWEB_BASE_URL", "https://crowdsmart.io")
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*GCI Jam created* 🧠\n*Topic:* {prompt}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Propositions seeded:* {n_props}"},
                {"type": "mrkdwn", "text": f"*Platform:* Slack"},
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open Jam"},
                    "url": jam_url,
                    "style": "primary",
                }
            ],
        },
    ]
    if unmatched_users:
        invite_lines = []
        for u in unmatched_users[:5]:
            if u.get("invite_token"):
                invite_url = f"{csweb}/claim/{u['invite_token']}"
                invite_lines.append(f"• <{invite_url}|{u.get('display_name', 'Participant')}> — claim account")
        if invite_lines:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Unmatched participants (no GCI account yet):*\n" + "\n".join(invite_lines),
                },
            })
    return blocks


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
