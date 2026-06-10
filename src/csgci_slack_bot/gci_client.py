"""
GCI Platform API client for the Slack bot.
Thin wrapper around the GCI REST API.
"""

from __future__ import annotations

import os
from typing import Any

import logging

import httpx

logger = logging.getLogger(__name__)

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
            if not r.is_success:
                raise RuntimeError(
                    f"GCI API {r.status_code} on POST {path}: {r.text[:500]}"
                )
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
        min_message_length: int = 5,
        seed_propositions: list[dict] | None = None,
    ) -> dict:
        """
        Call POST /api/jams/import-from-conversation.

        messages items: {platform_user_id, display_name, email, text, timestamp}
        seed_propositions: if provided, skips LLM extraction — [{text, platform_user_id, probability_estimate}]
        Returns: {jam_id, jam_url, propositions_created, matched_users, unmatched_count}
        """
        body: dict = {
            "jam_prompt":          prompt,
            "platform":            platform,
            "messages":            messages,
            "owner_id":            owner_id,
            "custom_title":        title or prompt[:80],
            "min_message_length":  min_message_length,
        }
        if seed_propositions is not None:
            body["seed_propositions"] = seed_propositions
        return await self._post("/api/jams/import-from-conversation", body)

    async def extract_propositions(
        self,
        *,
        owner_id: str,
        prompt: str,
        platform: str,
        messages: list[dict],
        min_message_length: int = 5,
    ) -> list[dict]:
        """
        Preview-only: extract propositions via LLM without creating a jam.
        Returns [{text, platform_user_id, display_name, probability_estimate}].
        """
        data = await self._post(
            "/api/jams/extract-propositions",
            {
                "jam_prompt":         prompt,
                "platform":           platform,
                "messages":           messages,
                "owner_id":           owner_id,
                "min_message_length": min_message_length,
            },
        )
        return data.get("propositions", [])

    async def import_messages_to_jam(
        self,
        *,
        jam_id: str,
        owner_id: str,
        prompt: str,
        platform: str,
        messages: list[dict],
        min_message_length: int = 5,
    ) -> dict:
        """Add new messages to an existing jam (POST /api/jams/{jam_id}/import-messages)."""
        return await self._post(
            f"/api/jams/{jam_id}/import-messages",
            {
                "jam_prompt":         prompt,
                "platform":           platform,
                "messages":           messages,
                "owner_id":           owner_id,
                "min_message_length": min_message_length,
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

    # ── Jam questions ─────────────────────────────────────────────────────────

    async def get_questions(self, jam_id: str) -> list[dict]:
        """GET /api/jams/{jam_id}/questions — returns sorted list of prompt dicts."""
        data = await self._get(f"/api/jams/{jam_id}/questions")
        questions = data.get("questions", [])
        questions.sort(key=lambda q: q.get("order", q.get("prompt_order", 0)))
        return questions

    # ── Submit a single response ──────────────────────────────────────────────

    async def submit_response(
        self,
        *,
        jam_id: str,
        participant_id: str,
        participant_name: str,
        participant_email: str,
        prompt_id: str | None,
        prompt_text: str,
        probability: float,
        reasoning: str,
    ) -> dict:
        """POST /api/user-responses/submit — submit one response (direct Supabase write)."""
        body: dict = {
            "jam_id":               jam_id,
            "template_id":          jam_id,
            "user_id":              participant_id,
            "user_name":            participant_name,
            "user_email":           participant_email,
            "probability_estimate": probability,
            "reasoning_text":       reasoning,
            "question_text":        prompt_text,
            "question_order":       0,
        }
        if prompt_id:
            body["question_id"] = prompt_id

        return await self._post("/api/user-responses/submit", body, timeout=120.0)

    # ── Peer review samples ───────────────────────────────────────────────────

    async def get_propositions(
        self,
        jam_id: str,
        reviewer_id: str,
        n: int = 5,
    ) -> list[dict]:
        """
        Fetch propositions for peer review via GET /api/jams/{jam_id}/propositions.
        Excludes the reviewer's own propositions. Returns [] on any failure.
        """
        try:
            data = await self._get(f"/api/jams/{jam_id}/propositions")
            all_props = data.get("propositions", [])
            others = [p for p in all_props if p.get("contributor_id") != reviewer_id]
            return others[:n]
        except Exception as exc:
            logger.warning(f"get_propositions failed: {exc}")
            return []

    async def get_beta_samples(
        self,
        jam_id: str,
        reasoning: str,
        probability: float,
        reviewer_id: str,
        n: int = 5,
    ) -> list[dict]:
        """
        Call /api/beta-sampling/generate with correct required fields.
        Maps response to {proposition_id, text, contributor_name, contributor_rating}.
        Returns [] on failure or timeout so caller can fall back.
        """
        try:
            data = await self._post(
                "/api/beta-sampling/generate",
                {
                    "jam_id":               jam_id,
                    "user_reasoning":       reasoning,
                    "probability_estimate": probability,
                    "reviewer_id":          reviewer_id,
                    "sample_size":          n,
                    "mode":                 "exploration",
                    "lambda_value":         0.5,
                },
                timeout=15.0,
            )
            return [
                {
                    "proposition_id":   s.get("id", ""),
                    "text":             s.get("text", ""),
                    "contributor_name": s.get("metadata", {}).get("contributor_name", "Colleague"),
                    "contributor_rating": None,
                }
                for s in data.get("samples", [])
                if s.get("text")
            ]
        except Exception as exc:
            logger.warning(f"get_beta_samples failed: {exc}")
            return []

    async def submit_peer_reviews_batch(
        self,
        jam_id: str,
        reviewer_id: str,
        reviews: list[dict],
    ) -> dict:
        """POST /api/peer-review/submit-batch — submit all peer review sentiments at once."""
        return await self._post(
            "/api/peer-review/submit-batch",
            {"jam_id": jam_id, "reviewer_id": reviewer_id, "reviews": reviews},
            timeout=30.0,
        )

    # ── BBN / Collective View ─────────────────────────────────────────────────

    async def get_bbn(self, jam_id: str) -> dict:
        """GET /api/bbn/calculate/{jam_id} — returns 4-layer BBN result."""
        return await self._get(f"/api/bbn/calculate/{jam_id}")

    # ── Collective Voice ──────────────────────────────────────────────────────

    async def collective_voice_query(
        self,
        jam_id: str,
        question: str,
        max_sources: int = 5,
    ) -> dict:
        """POST /api/collective-voice/query — RAG answer over jam propositions."""
        return await self._post(
            "/api/collective-voice/query",
            {
                "jam_id":      jam_id,
                "question":    question,
                "max_sources": max_sources,
            },
            timeout=60.0,
        )

    # ── URL helpers ───────────────────────────────────────────────────────────

    def jam_url(self, jam_id: str) -> str:
        return f"{CSWEB_BASE}/collaborate/{jam_id}"

    def claim_url(self, invite_token: str) -> str:
        return f"{CSWEB_BASE}/claim/{invite_token}"
