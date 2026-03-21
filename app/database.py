from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from supabase import Client, create_client

from app.config import Settings


class Database:
    def __init__(self, settings: Settings) -> None:
        if not settings.supabase_url or not settings.supabase_key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY are required.")
        self.client: Client = create_client(settings.supabase_url, settings.supabase_key)
        self.table_name = "expenses"
        self.local_timezone = ZoneInfo(settings.timezone)

    def save_expense(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        item: str,
        amount: float,
        category: str,
        currency: str = "PHP",
    ) -> dict[str, Any]:
        payload = {
            "telegram_user_id": telegram_user_id,
            "telegram_username": telegram_username,
            "item": item,
            "amount": float(amount),
            "category": category,
            "currency": currency or "PHP",
        }
        response = self.client.table(self.table_name).insert(payload).execute()
        return response.data[0]

    def get_last_expense(self, telegram_user_id: int) -> dict[str, Any] | None:
        response = (
            self.client.table(self.table_name)
            .select("*")
            .eq("telegram_user_id", telegram_user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    def delete_expense(self, expense_id: int) -> None:
        self.client.table(self.table_name).delete().eq("id", expense_id).execute()

    def get_expenses_between(
        self,
        telegram_user_id: int,
        start_date: datetime,
        end_date: datetime,
    ) -> list[dict[str, Any]]:
        response = (
            self.client.table(self.table_name)
            .select("*")
            .eq("telegram_user_id", telegram_user_id)
            .gte("created_at", start_date.isoformat())
            .lte("created_at", end_date.isoformat())
            .order("created_at", desc=False)
            .execute()
        )
        return response.data or []

    def get_all_user_ids(self) -> list[int]:
        response = self.client.table(self.table_name).select("telegram_user_id").execute()
        return sorted({row["telegram_user_id"] for row in response.data or []})

    def get_period_summary(
        self,
        telegram_user_id: int,
        period: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        start_date, end_date, label = self._resolve_period(period, current)
        expenses = self.get_expenses_between(telegram_user_id, start_date, end_date)
        total = sum(self._safe_amount(row.get("amount")) for row in expenses)

        by_category: dict[str, float] = {}
        for expense in expenses:
            category = expense.get("category", "other")
            by_category[category] = by_category.get(category, 0.0) + self._safe_amount(expense.get("amount"))

        top_expenses = sorted(expenses, key=lambda row: self._safe_amount(row.get("amount")), reverse=True)[:5]

        return {
            "label": label,
            "period": period,
            "start_date": start_date,
            "end_date": end_date,
            "count": len(expenses),
            "total": round(total, 2),
            "currency": expenses[0]["currency"] if expenses else "PHP",
            "by_category": {key: round(value, 2) for key, value in by_category.items()},
            "top_expenses": top_expenses,
            "expenses": expenses,
        }

    def get_monthly_summary_for_date(
        self,
        telegram_user_id: int,
        target_date: datetime,
    ) -> dict[str, Any]:
        local_target = target_date.astimezone(self.local_timezone)
        month_start_local = datetime(local_target.year, local_target.month, 1, tzinfo=self.local_timezone)
        if local_target.month == 12:
            next_month_local = datetime(local_target.year + 1, 1, 1, tzinfo=self.local_timezone)
        else:
            next_month_local = datetime(local_target.year, local_target.month + 1, 1, tzinfo=self.local_timezone)
        month_start = month_start_local.astimezone(timezone.utc)
        month_end = (next_month_local - timedelta(seconds=1)).astimezone(timezone.utc)

        expenses = self.get_expenses_between(telegram_user_id, month_start, month_end)
        total = sum(self._safe_amount(row.get("amount")) for row in expenses)
        by_category: dict[str, float] = {}
        for expense in expenses:
            category = expense.get("category", "other")
            by_category[category] = by_category.get(category, 0.0) + self._safe_amount(expense.get("amount"))

        return {
            "label": local_target.strftime("%B %Y"),
            "count": len(expenses),
            "total": round(total, 2),
            "currency": expenses[0]["currency"] if expenses else "PHP",
            "by_category": {key: round(value, 2) for key, value in by_category.items()},
            "expenses": expenses,
        }

    def _resolve_period(
        self,
        period: str,
        now: datetime,
    ) -> tuple[datetime, datetime, str]:
        period_name = (period or "month").lower()
        base_local = now.astimezone(self.local_timezone)

        if period_name == "today":
            start_local = datetime(base_local.year, base_local.month, base_local.day, tzinfo=self.local_timezone)
            end_local = start_local + timedelta(days=1) - timedelta(seconds=1)
            return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), "today"

        if period_name == "week":
            day_start_local = datetime(
                base_local.year,
                base_local.month,
                base_local.day,
                tzinfo=self.local_timezone,
            )
            start_local = day_start_local - timedelta(days=base_local.weekday())
            end_local = start_local + timedelta(days=7) - timedelta(seconds=1)
            return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), "this week"

        if period_name == "month":
            start_local = datetime(base_local.year, base_local.month, 1, tzinfo=self.local_timezone)
            if base_local.month == 12:
                next_month_local = datetime(base_local.year + 1, 1, 1, tzinfo=self.local_timezone)
            else:
                next_month_local = datetime(base_local.year, base_local.month + 1, 1, tzinfo=self.local_timezone)
            end_local = next_month_local - timedelta(seconds=1)
            return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), "this month"

        if period_name == "last_month":
            this_month_start_local = datetime(base_local.year, base_local.month, 1, tzinfo=self.local_timezone)
            end_local = this_month_start_local - timedelta(seconds=1)
            start_local = datetime(end_local.year, end_local.month, 1, tzinfo=self.local_timezone)
            return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), "last month"

        raise ValueError(f"Unsupported period: {period}")

    @staticmethod
    def _safe_amount(value: Any) -> float:
        try:
            return float(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError):
            return 0.0
