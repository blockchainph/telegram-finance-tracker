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
        self.users_table = "users"
        self.events_table = "events"
        self.budgets_table = "budgets"

    def upsert_user(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        payload = {
            "telegram_user_id": telegram_user_id,
            "telegram_username": telegram_username,
            "first_name": first_name,
            "last_name": last_name,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        }
        self.client.table(self.users_table).upsert(
            payload,
            on_conflict="telegram_user_id",
        ).execute()

    def log_event(
        self,
        telegram_user_id: int,
        event_type: str,
        message_text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "telegram_user_id": telegram_user_id,
            "event_type": event_type,
            "message_text": message_text,
            "metadata": metadata or {},
        }
        self.client.table(self.events_table).insert(payload).execute()

    def save_expense(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        item: str,
        store: str | None,
        amount: float,
        category: str,
        currency: str = "PHP",
    ) -> dict[str, Any]:
        payload = {
            "telegram_user_id": telegram_user_id,
            "telegram_username": telegram_username,
            "item": item,
            "store": store,
            "amount": float(amount),
            "category": category,
            "currency": currency or "PHP",
        }
        response = self.client.table(self.table_name).insert(payload).execute()
        return response.data[0]

    def upsert_budget(
        self,
        telegram_user_id: int,
        amount: float,
        category: str | None = None,
        period: str = "month",
        currency: str = "PHP",
    ) -> dict[str, Any]:
        normalized_category = category or "__overall__"
        payload = {
            "telegram_user_id": telegram_user_id,
            "category": normalized_category,
            "amount": float(amount),
            "period": period,
            "currency": currency or "PHP",
            "alert_state": "none",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        response = self.client.table(self.budgets_table).upsert(
            payload,
            on_conflict="telegram_user_id,category,period",
        ).execute()
        return response.data[0]

    def get_budgets(self, telegram_user_id: int, period: str = "month") -> list[dict[str, Any]]:
        response = (
            self.client.table(self.budgets_table)
            .select("*")
            .eq("telegram_user_id", telegram_user_id)
            .eq("period", period)
            .order("category", desc=False)
            .execute()
        )
        return response.data or []

    def get_budget_statuses(
        self,
        telegram_user_id: int,
        period: str = "month",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        start_date, end_date, _ = self._resolve_period(period, current)
        expenses = self.get_expenses_between(telegram_user_id, start_date, end_date)
        budgets = self.get_budgets(telegram_user_id, period=period)
        by_category = self._sum_by_category(expenses)
        total_spend = sum(self._safe_amount(row.get("amount")) for row in expenses)

        statuses: list[dict[str, Any]] = []
        for budget in budgets:
            category = None if budget["category"] == "__overall__" else budget["category"]
            spent = total_spend if category is None else by_category.get(category, 0.0)
            budget_amount = self._safe_amount(budget.get("amount"))
            remaining = round(budget_amount - spent, 2)
            percent_used = round((spent / budget_amount) * 100, 1) if budget_amount > 0 else 0.0
            statuses.append(
                {
                    "category": category,
                    "budget_amount": round(budget_amount, 2),
                    "spent": round(spent, 2),
                    "remaining": remaining,
                    "percent_used": percent_used,
                    "currency": budget.get("currency") or "PHP",
                    "period": budget.get("period") or period,
                    "alert_state": budget.get("alert_state") or "none",
                }
            )
        return statuses

    def get_budget_alerts_to_send(
        self,
        telegram_user_id: int,
        period: str = "month",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        statuses = self.get_budget_statuses(telegram_user_id, period=period, now=now)
        for status in statuses:
            next_alert_state = self._determine_alert_state(status["percent_used"])
            current_alert_state = status["alert_state"]
            if not next_alert_state or next_alert_state == current_alert_state:
                continue

            self._update_budget_alert_state(
                telegram_user_id=telegram_user_id,
                category=status["category"],
                period=status["period"],
                alert_state=next_alert_state,
            )
            status["next_alert_state"] = next_alert_state
            alerts.append(status)
        return alerts

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
        users_response = self.client.table(self.users_table).select("telegram_user_id").execute()
        expenses_response = self.client.table(self.table_name).select("telegram_user_id").execute()
        user_ids = {row["telegram_user_id"] for row in users_response.data or []}
        user_ids.update({row["telegram_user_id"] for row in expenses_response.data or []})
        return sorted(user_ids)

    def get_usage_stats(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        week_start, _, _ = self._resolve_period("week", current)
        month_start, _, _ = self._resolve_period("month", current)

        users_response = self.client.table(self.users_table).select("*", count="exact").execute()
        events_response = self.client.table(self.events_table).select("*", count="exact").execute()
        active_week_response = (
            self.client.table(self.users_table)
            .select("telegram_user_id", count="exact")
            .gte("last_seen_at", week_start.isoformat())
            .execute()
        )
        active_month_response = (
            self.client.table(self.users_table)
            .select("telegram_user_id", count="exact")
            .gte("last_seen_at", month_start.isoformat())
            .execute()
        )
        event_breakdown_response = (
            self.client.table(self.events_table)
            .select("event_type")
            .order("created_at", desc=True)
            .limit(5000)
            .execute()
        )

        event_counts: dict[str, int] = {}
        for row in event_breakdown_response.data or []:
            event_type = row.get("event_type", "unknown")
            event_counts[event_type] = event_counts.get(event_type, 0) + 1

        recent_users_response = (
            self.client.table(self.users_table)
            .select("telegram_user_id, telegram_username, first_name, last_seen_at")
            .order("last_seen_at", desc=True)
            .limit(5)
            .execute()
        )

        return {
            "total_users": users_response.count or 0,
            "total_events": events_response.count or 0,
            "active_users_this_week": active_week_response.count or 0,
            "active_users_this_month": active_month_response.count or 0,
            "event_counts": event_counts,
            "recent_users": recent_users_response.data or [],
        }

    def get_period_summary(
        self,
        telegram_user_id: int,
        period: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        if period in {"month", "last_month"}:
            target_date = current if period == "month" else self._shift_months(current, -1)
            return self.get_analytical_monthly_summary(telegram_user_id, target_date)

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

    def get_store_report(
        self,
        telegram_user_id: int,
        period: str = "month",
        category: str | None = None,
        store: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        start_date, end_date, label = self._resolve_period(period, current)
        expenses = self.get_expenses_between(telegram_user_id, start_date, end_date)

        filtered = expenses
        if category:
            filtered = [expense for expense in filtered if expense.get("category") == category]
        if store:
            target = store.strip().lower()
            filtered = [expense for expense in filtered if (expense.get("store") or "").strip().lower() == target]

        by_store: dict[str, float] = {}
        for expense in filtered:
            store_name = expense.get("store") or "Unknown store"
            by_store[store_name] = by_store.get(store_name, 0.0) + self._safe_amount(expense.get("amount"))

        total = sum(self._safe_amount(row.get("amount")) for row in filtered)
        sorted_stores = sorted(by_store.items(), key=lambda item: item[1], reverse=True)

        return {
            "label": label,
            "period": period,
            "category": category,
            "store": store,
            "currency": filtered[0]["currency"] if filtered else "PHP",
            "total": round(total, 2),
            "count": len(filtered),
            "stores": [{"name": name, "amount": round(amount, 2)} for name, amount in sorted_stores],
        }

    def get_analytical_monthly_summary(
        self,
        telegram_user_id: int,
        target_date: datetime,
    ) -> dict[str, Any]:
        current_start, current_end, label = self._resolve_month_window(target_date)
        previous_month_anchor = self._shift_months(target_date, -1)
        previous_start, previous_end, _ = self._resolve_month_window(previous_month_anchor)

        current_expenses = self.get_expenses_between(telegram_user_id, current_start, current_end)
        previous_expenses = self.get_expenses_between(telegram_user_id, previous_start, previous_end)

        current_total = sum(self._safe_amount(row.get("amount")) for row in current_expenses)
        previous_total = sum(self._safe_amount(row.get("amount")) for row in previous_expenses)
        by_category = self._sum_by_category(current_expenses)
        top_expenses = sorted(current_expenses, key=lambda row: self._safe_amount(row.get("amount")), reverse=True)[:5]
        top_category_name, top_category_amount = self._top_category(by_category)
        top_category_percentage = round((top_category_amount / current_total) * 100, 1) if current_total > 0 else 0.0
        highest_day = self._highest_spending_day(current_expenses)
        change_vs_last_month = self._calculate_change_percent(current_total, previous_total)
        insight = self._build_monthly_insight(
            current_total=current_total,
            previous_total=previous_total,
            top_category_name=top_category_name,
            top_category_percentage=top_category_percentage,
            highest_day=highest_day,
            expense_count=len(current_expenses),
        )

        return {
            "label": label,
            "period": "month",
            "start_date": current_start,
            "end_date": current_end,
            "count": len(current_expenses),
            "total": round(current_total, 2),
            "currency": current_expenses[0]["currency"] if current_expenses else "PHP",
            "by_category": {key: round(value, 2) for key, value in by_category.items()},
            "top_expenses": top_expenses,
            "expenses": current_expenses,
            "previous_total": round(previous_total, 2),
            "change_vs_last_month": change_vs_last_month,
            "top_category": {
                "name": top_category_name,
                "amount": round(top_category_amount, 2),
                "percentage": top_category_percentage,
            }
            if top_category_name
            else None,
            "highest_spending_day": highest_day,
            "insight": insight,
            "analytical": True,
        }

    def get_monthly_summary_for_date(
        self,
        telegram_user_id: int,
        target_date: datetime,
    ) -> dict[str, Any]:
        return self.get_analytical_monthly_summary(telegram_user_id, target_date)

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

    def _resolve_month_window(self, target_date: datetime) -> tuple[datetime, datetime, str]:
        local_target = target_date.astimezone(self.local_timezone)
        month_start_local = datetime(local_target.year, local_target.month, 1, tzinfo=self.local_timezone)
        if local_target.month == 12:
            next_month_local = datetime(local_target.year + 1, 1, 1, tzinfo=self.local_timezone)
        else:
            next_month_local = datetime(local_target.year, local_target.month + 1, 1, tzinfo=self.local_timezone)
        month_start = month_start_local.astimezone(timezone.utc)
        month_end = (next_month_local - timedelta(seconds=1)).astimezone(timezone.utc)
        return month_start, month_end, local_target.strftime("%B %Y")

    def _update_budget_alert_state(
        self,
        telegram_user_id: int,
        category: str | None,
        period: str,
        alert_state: str,
    ) -> None:
        normalized_category = category or "__overall__"
        (
            self.client.table(self.budgets_table)
            .update(
                {
                    "alert_state": alert_state,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("telegram_user_id", telegram_user_id)
            .eq("category", normalized_category)
            .eq("period", period)
            .execute()
        )

    @staticmethod
    def _determine_alert_state(percent_used: float) -> str | None:
        if percent_used >= 100:
            return "100"
        if percent_used >= 80:
            return "80"
        return None

    def _sum_by_category(self, expenses: list[dict[str, Any]]) -> dict[str, float]:
        by_category: dict[str, float] = {}
        for expense in expenses:
            category = expense.get("category", "other")
            by_category[category] = by_category.get(category, 0.0) + self._safe_amount(expense.get("amount"))
        return by_category

    @staticmethod
    def _top_category(by_category: dict[str, float]) -> tuple[str | None, float]:
        if not by_category:
            return None, 0.0
        name, amount = max(by_category.items(), key=lambda item: item[1])
        return name, amount

    def _highest_spending_day(self, expenses: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not expenses:
            return None

        daily_totals: dict[str, float] = {}
        for expense in expenses:
            created_at_raw = expense.get("created_at")
            if not created_at_raw:
                continue
            created_at = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))
            local_day = created_at.astimezone(self.local_timezone).strftime("%Y-%m-%d")
            daily_totals[local_day] = daily_totals.get(local_day, 0.0) + self._safe_amount(expense.get("amount"))

        if not daily_totals:
            return None

        day_key, amount = max(daily_totals.items(), key=lambda item: item[1])
        day_label = datetime.strptime(day_key, "%Y-%m-%d").strftime("%b %d")
        return {"date": day_key, "label": day_label, "amount": round(amount, 2)}

    @staticmethod
    def _calculate_change_percent(current_total: float, previous_total: float) -> float | None:
        if previous_total <= 0:
            return None
        return round(((current_total - previous_total) / previous_total) * 100, 1)

    def _build_monthly_insight(
        self,
        current_total: float,
        previous_total: float,
        top_category_name: str | None,
        top_category_percentage: float,
        highest_day: dict[str, Any] | None,
        expense_count: int,
    ) -> str:
        if expense_count == 0:
            return "No spending recorded this month yet, so there is not enough data for a pattern-based insight."

        if previous_total > 0:
            change_pct = self._calculate_change_percent(current_total, previous_total)
            if change_pct is not None and change_pct >= 20:
                return (
                    f"Your spending is up {abs(change_pct):.1f}% versus last month. "
                    f"Review {top_category_name or 'your biggest category'} first to slow the increase."
                )
            if change_pct is not None and change_pct <= -15:
                return (
                    f"You spent {abs(change_pct):.1f}% less than last month. "
                    f"Whatever changed is working, so try to keep that habit consistent next month."
                )

        if top_category_name and top_category_percentage >= 40:
            return (
                f"{top_category_name.capitalize()} made up {top_category_percentage:.1f}% of your spending this month. "
                f"That category is driving most of your total, so even a small cut there would have the biggest impact."
            )

        if highest_day and current_total > 0:
            highest_day_share = round((float(highest_day["amount"]) / current_total) * 100, 1)
            if highest_day_share >= 25:
                return (
                    f"Your heaviest spending happened on {highest_day['label']}, which was {highest_day_share:.1f}% of the month. "
                    f"Watch for one-off high-spend days because they are shaping your monthly total."
                )

        if expense_count >= 20:
            return (
                "You are logging expenses consistently, which is great. "
                "The next step is setting a category budget so the tracking turns into a spending decision tool."
            )

        return (
            f"Your spending looks fairly spread out this month, with {top_category_name or 'other expenses'} leading. "
            "Keep logging consistently so the bot can spot stronger trends and make sharper recommendations."
        )

    def _shift_months(self, target_date: datetime, months: int) -> datetime:
        local_target = target_date.astimezone(self.local_timezone)
        month_index = (local_target.year * 12 + local_target.month - 1) + months
        year = month_index // 12
        month = month_index % 12 + 1
        day = min(local_target.day, 28)
        shifted_local = datetime(
            year,
            month,
            day,
            local_target.hour,
            local_target.minute,
            local_target.second,
            tzinfo=self.local_timezone,
        )
        return shifted_local.astimezone(timezone.utc)

    @staticmethod
    def _safe_amount(value: Any) -> float:
        try:
            return float(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError):
            return 0.0
