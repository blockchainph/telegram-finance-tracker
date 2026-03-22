from __future__ import annotations

import json
from json import JSONDecodeError
from textwrap import dedent
from typing import Any

from anthropic import AsyncAnthropic

from app.config import Settings


class ClaudeHandler:
    VALID_CATEGORIES = [
        "food",
        "transport",
        "groceries",
        "bills",
        "shopping",
        "health",
        "entertainment",
        "other",
    ]

    def __init__(self, settings: Settings) -> None:
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required.")
        self.client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.model = settings.anthropic_model

    async def parse_message(self, message_text: str) -> dict[str, Any]:
        system_prompt = dedent(
            """
            You are an expense parsing engine for a Telegram finance tracker.
            Return JSON only. No markdown. No prose. No code fences.

            Supported intents:
            - expense: a message describing money spent
            - summary: a request for totals, summaries, or reports
            - budget_set: user wants to create or update a budget
            - budget_show: user wants to view budgets or remaining budget
            - undo: user wants to remove the latest expense
            - unknown: not enough information or not finance-related

            Valid categories:
            food, transport, groceries, bills, shopping, health, entertainment, other

            Extraction rules:
            - Default currency is PHP unless another clear currency is stated.
            - Convert amount to a number only.
            - Use lowercase category names.
            - If the message is missing an amount or item, set needs_clarification to true.
            - If the message is not an expense, summary, or budget request, use intent=unknown.
            - For summary requests, set period to one of: today, week, month, last_month.
            - If summary period is unclear, default to month.
            - For budget requests, use period=month unless the user clearly asks for something else.
            - For overall budgets, set category to null.
            - Keep clarification_message short and helpful.

            Return this exact JSON shape:
            {
              "intent": "expense|summary|budget_set|budget_show|undo|unknown",
              "item": "string or null",
              "store": "string or null",
              "amount": 0,
              "category": "food|transport|groceries|bills|shopping|health|entertainment|other|null",
              "currency": "PHP",
              "period": "today|week|month|last_month|null",
              "needs_clarification": false,
              "clarification_message": "string or null"
            }
            """
        ).strip()

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=300,
            system=system_prompt,
            messages=[{"role": "user", "content": message_text}],
        )
        content = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()
        parsed = self._load_json(content)
        return self._normalize_result(parsed)

    def _normalize_result(self, result: dict[str, Any]) -> dict[str, Any]:
        intent = (result.get("intent") or "unknown").lower()
        category = result.get("category")
        normalized_category = category.lower() if isinstance(category, str) else None
        if normalized_category not in self.VALID_CATEGORIES:
            normalized_category = "other" if intent == "expense" else None

        currency = (result.get("currency") or "PHP").upper()
        period = (result.get("period") or "").lower() or None
        if period not in {"today", "week", "month", "last_month", None}:
            period = "month"

        amount = result.get("amount")
        try:
            normalized_amount = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            normalized_amount = None

        return {
            "intent": intent if intent in {"expense", "summary", "budget_set", "budget_show", "undo", "unknown"} else "unknown",
            "item": self._clean_string(result.get("item")),
            "store": self._clean_string(result.get("store")),
            "amount": normalized_amount,
            "category": normalized_category,
            "currency": currency,
            "period": period,
            "needs_clarification": bool(result.get("needs_clarification")),
            "clarification_message": self._clean_string(result.get("clarification_message")),
        }

    @staticmethod
    def _load_json(content: str) -> dict[str, Any]:
        try:
            return json.loads(content)
        except JSONDecodeError:
            start = content.find("{")
            end = content.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise
            return json.loads(content[start : end + 1])

    @staticmethod
    def _clean_string(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None
