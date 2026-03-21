from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.ext import Application

from app.database import Database
from app.telegram_handler import format_summary_message


logger = logging.getLogger(__name__)


def build_scheduler(
    application: Application,
    database: Database,
    timezone_name: str,
    hour: int,
    minute: int,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone_name)
    scheduler.add_job(
        send_monthly_summaries,
        trigger=CronTrigger(day="last", hour=hour, minute=minute, timezone=timezone_name),
        kwargs={"application": application, "database": database},
        id="monthly-summary",
        replace_existing=True,
    )
    return scheduler


async def send_monthly_summaries(application: Application, database: Database) -> None:
    user_ids = database.get_all_user_ids()
    if not user_ids:
        logger.info("No users with expenses yet; skipping monthly summary.")
        return

    target_date = datetime.now(timezone.utc)
    for user_id in user_ids:
        try:
            summary = database.get_monthly_summary_for_date(user_id, target_date)
            message = "End-of-month report\n" + format_summary_message(summary)
            await application.bot.send_message(chat_id=user_id, text=message)
        except Exception:
            logger.exception("Failed to send monthly summary to user %s", user_id)
