"""
GCI Slack Bot

Slash commands:
  /jam "<question>"  — import channel conversation → create Jam → pop-up modal
                       to submit your own view → post summary card to channel
  /jam connect       — DM a link to connect your GCI account
  /cv  <jam_id>      — pop-up Collective View: CScore + themes + ask Collective Voice

Flow:
  1. /jam opens a loading modal immediately (trigger_id expires in 3s)
  2. Bot creates Jam + imports channel conversation in the background
  3. Modal updates to participation form (question, confidence slider, reasoning)
  4. User submits → proposition posted → summary card in channel
  5. /cv fetches BBN → shows collective score/themes → lets user ask Collective Voice

Environment variables: see .env.example
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from dotenv import load_dotenv
load_dotenv()  # must run before any module that reads env vars at import time

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .gci_client import GCIClient

logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN      = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN      = os.environ["SLACK_APP_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
GCI_OWNER_ID         = os.environ.get("GCI_OWNER_ID", "")
CSWEB_BASE           = os.environ.get("CSWEB_BASE_URL", "https://collectivereasoningcommons.com").rstrip("/")

THREAD_HISTORY_LIMIT = 50

app = AsyncApp(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
gci = GCIClient()


# ── /jam command ──────────────────────────────────────────────────────────────

@app.command("/jam")
async def handle_jam_command(ack, command, client):
    await ack()

    text       = (command.get("text") or "").strip()
    trigger_id = command["trigger_id"]
    user_id    = command["user_id"]
    channel    = command["channel_id"]

    if text.lower() == "connect":
        await _handle_connect(user_id, client)
        return

    if not text:
        await client.chat_postEphemeral(
            channel=channel,
            user=user_id,
            text="Please provide a question.\nUsage: `/jam Should we expand into enterprise accounts?`",
        )
        return

    # Open loading modal immediately — trigger_id expires after 3 seconds
    res = await client.views_open(
        trigger_id=trigger_id,
        view=_loading_modal(f"Setting up Jam: _{text[:60]}_…"),
    )
    view_id = res["view"]["id"]

    # Create jam + build participation modal in background
    asyncio.create_task(
        _build_jam_modal(view_id, text, channel, user_id, client)
    )


async def _build_jam_modal(
    view_id: str,
    prompt: str,
    channel: str,
    user_id: str,
    client,
) -> None:
    try:
        messages      = await _fetch_channel_messages(channel, client, THREAD_HISTORY_LIMIT)
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

        result  = await gci.import_conversation(
            owner_id=GCI_OWNER_ID,
            prompt=prompt,
            platform="slack",
            messages=gci_messages,
            title=prompt[:80],
        )

        jam_id    = result.get("jam_id", "")
        jam_url   = result.get("jam_url", f"{CSWEB_BASE}/collaborate/{jam_id}")
        n_props   = result.get("propositions_created", 0)
        all_users = result.get("matched_users", [])
        unmatched = [u for u in all_users if not u.get("matched")]

        # Find the invoking user's GCI participant_id from identity resolution
        invoker   = next((u for u in all_users if u.get("platform_user_id") == user_id), None)
        gci_pid   = invoker.get("gci_participant_id", GCI_OWNER_ID) if invoker else GCI_OWNER_ID

        # Fetch prompt details
        questions   = await gci.get_questions(jam_id)
        first_q     = questions[0] if questions else {}
        prompt_id   = first_q.get("id", "")
        prompt_text = first_q.get("text", prompt)

        # Get invoking user's profile
        user_profile = user_profiles.get(user_id, {})

        meta = json.dumps({
            "jam_id":        jam_id,
            "jam_url":       jam_url,
            "prompt_id":     prompt_id,
            "prompt_text":   prompt_text,
            "channel":       channel,
            "user_id":       user_id,
            "gci_pid":       gci_pid,
            "display_name":  user_profile.get("real_name", user_id),
            "email":         user_profile.get("email", ""),
            "n_props":       n_props,
            "unmatched":     unmatched[:5],
        })

        await client.views_update(
            view_id=view_id,
            view=_participate_modal(prompt_text, jam_url, meta),
        )

    except Exception as exc:
        logger.exception("Failed to build jam modal")
        await client.views_update(
            view_id=view_id,
            view=_error_modal(str(exc)),
        )


@app.view("jam_participate")
async def handle_jam_participate(ack, body, client, view):
    await ack()

    values    = view["state"]["values"]
    prob_raw  = (values.get("prob_block", {}).get("prob_input", {}).get("value") or "50").strip()
    reasoning = (values.get("reasoning_block", {}).get("reasoning_input", {}).get("value") or "").strip()
    meta      = json.loads(view.get("private_metadata", "{}"))

    jam_id      = meta.get("jam_id", "")
    jam_url     = meta.get("jam_url", "")
    prompt_id   = meta.get("prompt_id", "")
    prompt_text = meta.get("prompt_text", "")
    channel     = meta.get("channel", "")
    gci_pid     = meta.get("gci_pid", GCI_OWNER_ID)
    display_name = meta.get("display_name", "")
    email       = meta.get("email", "")
    n_props     = meta.get("n_props", 0)
    unmatched   = meta.get("unmatched", [])

    try:
        pct  = float(prob_raw.replace("%", "").strip())
        prob = max(0.0, min(1.0, pct / 100))
    except ValueError:
        prob = 0.5

    if reasoning and jam_id:
        try:
            await gci.submit_response(
                jam_id=jam_id,
                participant_id=gci_pid,
                participant_name=display_name,
                participant_email=email,
                prompt_id=prompt_id,
                prompt_text=prompt_text,
                probability=prob,
                reasoning=reasoning,
            )
            n_props += 1
        except Exception as exc:
            logger.warning(f"Modal response submission failed: {exc}")

    if channel:
        await client.chat_postMessage(
            channel=channel,
            blocks=_jam_summary_blocks(
                prompt=prompt_text,
                jam_url=jam_url,
                n_props=n_props,
                unmatched=unmatched,
            ),
            text=f"GCI Jam ready: {prompt_text[:60]} — {jam_url}",
        )


# ── /cv command ───────────────────────────────────────────────────────────────

@app.command("/cv")
async def handle_cv_command(ack, command, client):
    await ack()

    jam_id     = (command.get("text") or "").strip()
    trigger_id = command["trigger_id"]
    channel    = command["channel_id"]
    user_id    = command["user_id"]

    if not jam_id:
        await client.chat_postEphemeral(
            channel=channel,
            user=user_id,
            text="Usage: `/cv <jam_id>`",
        )
        return

    res = await client.views_open(
        trigger_id=trigger_id,
        view=_loading_modal("Loading Collective View…"),
    )
    view_id = res["view"]["id"]
    asyncio.create_task(_build_cv_modal(view_id, jam_id, channel, client))


async def _build_cv_modal(view_id: str, jam_id: str, channel: str, client) -> None:
    try:
        bbn = await gci.get_bbn(jam_id)
        if not bbn.get("success"):
            await client.views_update(
                view_id=view_id,
                view=_error_modal(
                    f"Collective View not yet available — submit more responses first.\n"
                    f"({bbn.get('error', 'insufficient data')})"
                ),
            )
            return

        cscore  = bbn.get("final_cscore")
        layers  = bbn.get("layers", {})
        themes  = layers.get("layer_2_themes", {}).get("themes", [])
        reasons = layers.get("layer_1_reasons", {}).get("reasons", [])

        meta = json.dumps({"jam_id": jam_id, "channel": channel})
        await client.views_update(
            view_id=view_id,
            view=_cv_modal(jam_id, cscore, themes, reasons, meta),
        )
    except Exception as exc:
        logger.exception("Failed to build CV modal")
        await client.views_update(
            view_id=view_id,
            view=_error_modal(str(exc)),
        )


@app.view("cv_query")
async def handle_cv_query(ack, body, client, view):
    await ack()

    values   = view["state"]["values"]
    question = (values.get("cv_q_block", {}).get("cv_q_input", {}).get("value") or "").strip()
    meta     = json.loads(view.get("private_metadata", "{}"))
    jam_id   = meta.get("jam_id", "")
    channel  = meta.get("channel", "")

    if not question or not jam_id or not channel:
        return

    try:
        result  = await gci.collective_voice_query(jam_id, question)
        answer  = result.get("answer", "No answer returned.")
        sources = result.get("sources", [])
        await client.chat_postMessage(
            channel=channel,
            blocks=_cv_answer_blocks(question, answer, sources, jam_id),
            text=f"Collective Voice — {answer[:120]}",
        )
    except Exception as exc:
        logger.warning(f"CV query failed: {exc}")
        await client.chat_postMessage(
            channel=channel,
            text=f"❌ Collective Voice error: {exc}",
        )


# ── /jam connect ──────────────────────────────────────────────────────────────

async def _handle_connect(user_id: str, client) -> None:
    connect_url = f"{CSWEB_BASE}/connect/slack?slack_user_id={user_id}"
    try:
        await client.chat_postMessage(
            channel=user_id,
            text=f"👋 Connect your GCI account to get full attribution in Jams:\n{connect_url}",
        )
    except Exception as exc:
        logger.warning(f"DM to {user_id} failed: {exc}")


# ── Slack API helpers ─────────────────────────────────────────────────────────

async def _fetch_channel_messages(channel: str, client, limit: int = 50) -> list[dict]:
    result = await client.conversations_history(channel=channel, limit=limit)
    return result.get("messages", [])


async def _fetch_user_profiles(user_ids: set[str], client) -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    for uid in user_ids:
        try:
            info = await client.users_info(user=uid, include_locale=False)
            u = info.get("user", {})
            profiles[uid] = {
                "real_name": u.get("real_name") or u.get("name", uid),
                "email":     (u.get("profile") or {}).get("email", ""),
            }
        except Exception:
            profiles[uid] = {"real_name": uid, "email": ""}
    return profiles


# ── Block Kit helpers ─────────────────────────────────────────────────────────

def _loading_modal(message: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "jam_loading",
        "title": {"type": "plain_text", "text": "GCI Jam"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"⏳ {message}"}},
        ],
    }


def _error_modal(message: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "jam_error",
        "title": {"type": "plain_text", "text": "GCI Jam — Error"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"❌ {message[:300]}"}},
        ],
    }


def _participate_modal(prompt_text: str, jam_url: str, meta: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "jam_participate",
        "private_metadata": meta,
        "title": {"type": "plain_text", "text": "GCI Jam — Your View"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "View online instead"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{prompt_text}*"},
            },
            {"type": "divider"},
            {
                "type": "input",
                "block_id": "prob_block",
                "label": {
                    "type": "plain_text",
                    "text": "Your confidence — 0 (very unlikely) to 100 (certain)",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "prob_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. 70"},
                    "initial_value": "50",
                },
                "hint": {"type": "plain_text", "text": "Enter a whole number, 0–100"},
            },
            {
                "type": "input",
                "block_id": "reasoning_block",
                "label": {"type": "plain_text", "text": "Your reasoning"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reasoning_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "What drives your view on this question?",
                    },
                },
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"<{jam_url}|Open full Jam online> "
                            "to see all perspectives, the Bayesian score, and analytics."
                        ),
                    }
                ],
            },
        ],
    }


def _jam_summary_blocks(
    *,
    prompt: str,
    jam_url: str,
    n_props: int,
    unmatched: list[dict],
) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*GCI Jam ready* 🧠\n*Question:* {prompt}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Propositions seeded:* {n_props}"},
                {"type": "mrkdwn", "text": "*Platform:* Slack"},
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Join Jam →"},
                    "url": jam_url,
                    "style": "primary",
                }
            ],
        },
    ]
    invite_lines = [
        f"• <{CSWEB_BASE}/claim/{u['invite_token']}|{u.get('display_name', 'Participant')}>"
        for u in unmatched[:5]
        if u.get("invite_token")
    ]
    if invite_lines:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*These participants don't have a GCI account yet:*\n"
                        + "\n".join(invite_lines),
            },
        })
    return blocks


def _cv_modal(
    jam_id: str,
    cscore,
    themes: list[dict],
    reasons: list[dict],
    meta: str,
) -> dict:
    score_pct  = f"{cscore * 100:.0f}%" if isinstance(cscore, (int, float)) else "—"
    filled     = int((cscore or 0) * 20)
    bar        = "█" * filled + "░" * (20 - filled)

    theme_lines = [
        f"• {(t.get('name') or t.get('label', '?'))[:45]}  —  "
        f"{(t.get('probability') or t.get('theme_prob') or 0) * 100:.0f}%"
        for t in themes[:5]
    ]

    top = sorted(reasons, key=lambda r: r.get("reason_relevancy") or 0, reverse=True)[:3]
    reason_lines = [
        f"• _{r.get('contributor_name', '—')}_: {(r.get('text') or '')[:75]}…"
        for r in top
    ]

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Collective Score:* {score_pct}\n`{bar}`",
            },
        },
    ]
    if theme_lines:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Themes:*\n" + "\n".join(theme_lines)},
        })
    if reason_lines:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Top propositions:*\n" + "\n".join(reason_lines)},
        })
    blocks += [
        {"type": "divider"},
        {
            "type": "input",
            "block_id": "cv_q_block",
            "label": {"type": "plain_text", "text": "Ask the Collective Voice"},
            "element": {
                "type": "plain_text_input",
                "action_id": "cv_q_input",
                "placeholder": {
                    "type": "plain_text",
                    "text": "e.g. What are the main risks? What did people agree on?",
                },
            },
            "optional": True,
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Jam ID: `{jam_id}`"}],
        },
    ]

    return {
        "type": "modal",
        "callback_id": "cv_query",
        "private_metadata": meta,
        "title": {"type": "plain_text", "text": "Collective View"},
        "submit": {"type": "plain_text", "text": "Ask"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": blocks,
    }


def _cv_answer_blocks(
    question: str,
    answer: str,
    sources: list[dict],
    jam_id: str,
) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Collective Voice* 🧠\n*Q:* {question}",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": answer[:2900]},
        },
    ]
    if sources:
        src_lines = [
            f"_{s.get('contributor_name', '—')}_: {(s.get('text') or '')[:65]}…"
            for s in sources[:3]
        ]
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Sources: " + " | ".join(src_lines)}],
        })
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Full Jam Analytics →"},
                "url": f"{CSWEB_BASE}/collaborate/{jam_id}",
            }
        ],
    })
    return blocks


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
