"""
GCI Platform API client for the Slack bot.
Thin wrapper around the GCI REST API.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

GCI_API_BASE = os.environ.get("GCI_API_BASE", "https://csgcip.onrender.com").rstrip("/")
GCI_API_KEY  = os.environ.get("GCI_API_KEY", "")
CSWEB_BASE   = os.environ.get("CSWEB_BASE_URL", "https://collectivereasoningcommons.com").rstrip("/")


class GCIClient:
    def __init__(
        self,
        api_base: str = GCI_API_BASE,
        api_key:  str = GCI_API_KEY,
    ) -> None:
        self.api_base = api_base
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    # ── Low-level helpers ─────────────────────────────────────────────────────

    async def _post(self, path: str, body: dict, timeout: float = 180.0) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{self.api_base}{path}",
                headers=self._headers,
                json=body,
            )
            r.raise_for_status()
            return r.json()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{self.api_base}{path}",
                headers=self._headers,
                params=params or {},
            )
            r.raise_for_status()
            return r.json()

    # ── Jam import ────────────────────────────────────────────────────────────

    async def import_conversation(
        self,
        *,
        owner_id: str,
        prompt: str,
        platform: str,
        messages: list[dict],
        title: str = "",
        min_message_length: int = 30,
    ) -> dict:
        """
        Call POST /api/jams/import-from-conversation.

        messages items: {platform_user_id, display_name, email, text, timestamp}
        Returns: {jam_id, jam_url, propositions_created, matched_users, unmatched_count}
        """
        return await self._post(
            "/api/jams/import-from-conversation",
            {
                "jam_prompt":          prompt,
                "platform":            platform,
                "messages":            messages,
                "owner_id":            owner_id,
                "custom_title":        title or prompt[:80],
                "min_message_length":  min_message_length,
            },
        )

    # ── Identity linking ──────────────────────────────────────────────────────

    async def link_identity(
        self,
        *,
        platform: str,
        platform_user_id: str,
        gci_participant_id: str,
        platform_email: str = "",
        platform_display_name: str = "",
    ) -> dict:
        return await self._post(
            "/api/identity/link-platform",
            {
                "platform":               platform,
                "platform_user_id":       platform_user_id,
                "gci_participant_id":     gci_participant_id,
                "platform_email":         platform_email,
                "platform_display_name":  platform_display_name,
            },
        )

    # ── URL helpers ───────────────────────────────────────────────────────────

    def jam_url(self, jam_id: str) -> str:
        return f"{CSWEB_BASE}/join/{jam_id}"

    def claim_url(self, invite_token: str) -> str:
        return f"{CSWEB_BASE}/claim/{invite_token}"
