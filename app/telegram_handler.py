from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.claude_handler import ClaudeHandler
from app.config import Settings
from app.database import Database


logger = logging.getLogger(__name__)


def build_application(
    token: str,
    database: Database,
    claude_handler: ClaudeHandler,
    settings: Settings,
) -> Application:
    application = Application.builder().token(token).build()
    application.bot_data["db"] = database
    application.bot_data["claude"] = claude_handler
    application.bot_data["settings"] = settings

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("undo", undo_command))
    application.add_handler(CommandHandler("summary", summary_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    return application


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "See how to log expenses"),
            BotCommand("help", "Show examples and supported categories"),
            BotCommand("summary", "Get this month's summary"),
            BotCommand("undo", "Delete your latest expense"),
            BotCommand("stats", "Show bot usage stats for admins"),
        ]
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user_context(update, context, event_type="start_command")
    message = (
        "Send expenses naturally, like:\n"
        "`lunch jollibee 150`\n"
        "`grab to work 180 pesos`\n\n"
        "You can also ask:\n"
        "`how much did I spend this week?`\n"
        "`summary this month`\n"
        "`undo`"
    )
    await update.effective_message.reply_text(message, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user_context(update, context, event_type="help_command")
    categories = ", ".join(ClaudeHandler.VALID_CATEGORIES)
    message = (
        "I track expenses in PHP by default.\n"
        f"Categories I use: {categories}.\n\n"
        "If your message is unclear, I’ll ask a quick follow-up instead of saving bad data."
    )
    await update.effective_message.reply_text(message)


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    user = update.effective_user
    await track_user_context(update, context, event_type="summary_command")
    summary = db.get_period_summary(user.id, "month")
    await update.effective_message.reply_text(format_summary_message(summary))


async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    user = update.effective_user
    await track_user_context(update, context, event_type="undo_command")
    last_expense = db.get_last_expense(user.id)
    if not last_expense:
        await update.effective_message.reply_text("I couldn’t find any saved expense to undo yet.")
        return

    db.delete_expense(last_expense["id"])
    await update.effective_message.reply_text(
        f"Removed your latest expense: {last_expense['item']} ({format_money(last_expense['amount'], last_expense['currency'])})."
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    settings: Settings = context.application.bot_data["settings"]
    user = update.effective_user
    await track_user_context(update, context, event_type="stats_command")

    if not user or user.id not in settings.admin_telegram_user_ids:
        await update.effective_message.reply_text("This command is only available to bot admins.")
        return

    stats = db.get_usage_stats()
    await update.effective_message.reply_text(format_stats_message(stats))


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    claude: ClaudeHandler = context.application.bot_data["claude"]
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return

    await track_user_context(update, context, event_type="message_received", message_text=message.text)

    text = message.text.strip()
    if text.lower() == "undo":
        await undo_command(update, context)
        return

    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    try:
        parsed = await claude.parse_message(text)
    except Exception:
        logger.exception("Claude parsing failed")
        db.log_event(user.id, "claude_parse_error", message_text=text)
        await message.reply_text("I had trouble reading that. Please try again in a moment.")
        return

    intent = parsed["intent"]
    if parsed["needs_clarification"]:
        db.log_event(
            user.id,
            "clarification_requested",
            message_text=text,
            metadata={"intent": intent},
        )
        await message.reply_text(parsed["clarification_message"] or "I need a bit more detail before I save that.")
        return

    if intent == "expense":
        amount = parsed.get("amount")
        item = parsed.get("item")
        category = parsed.get("category")
        if amount is None or amount <= 0 or not item or not category:
            await message.reply_text("Please send the expense with an item and amount, like `coffee 120`.", parse_mode="Markdown")
            return

        saved = db.save_expense(
            telegram_user_id=user.id,
            telegram_username=user.username,
            item=item,
            amount=amount,
            category=category,
            currency=parsed.get("currency") or "PHP",
        )
        db.log_event(
            user.id,
            "expense_logged",
            message_text=text,
            metadata={"category": saved["category"], "amount": float(saved["amount"])},
        )
        await message.reply_text(
            "Saved: "
            f"{saved['item']} for {format_money(saved['amount'], saved['currency'])} "
            f"under {saved['category']}."
        )
        return

    if intent == "summary":
        summary = db.get_period_summary(user.id, parsed.get("period") or "month", now=datetime.now(timezone.utc))
        db.log_event(
            user.id,
            "summary_requested",
            message_text=text,
            metadata={"period": parsed.get("period") or "month"},
        )
        await message.reply_text(format_summary_message(summary))
        return

    if intent == "undo":
        db.log_event(user.id, "undo_requested", message_text=text)
        await undo_command(update, context)
        return

    db.log_event(user.id, "unknown_message", message_text=text)
    await message.reply_text(
        "I can help track expenses and show summaries. Try `snacks 85`, `grab to work 180`, or ask `how much did I spend this week?`"
    )


async def track_user_context(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    event_type: str,
    message_text: str | None = None,
) -> None:
    db: Database = context.application.bot_data["db"]
    user = update.effective_user
    if not user:
        return

    db.upsert_user(
        telegram_user_id=user.id,
        telegram_username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )
    db.log_event(
        telegram_user_id=user.id,
        event_type=event_type,
        message_text=message_text,
    )


def format_summary_message(summary: dict) -> str:
    if summary["count"] == 0:
        return f"You have no expenses recorded for {summary['label']} yet."

    currency = summary["currency"]
    if summary.get("analytical"):
        return format_analytical_monthly_summary(summary)

    lines = [
        f"Summary for {summary['label']}",
        f"Total: {format_money(summary['total'], currency)}",
        f"Entries: {summary['count']}",
        "",
        "By category:",
    ]

    for category, amount in sorted(summary["by_category"].items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- {category}: {format_money(amount, currency)}")

    if summary["top_expenses"]:
        lines.extend(["", "Top expenses:"])
        for expense in summary["top_expenses"][:3]:
            lines.append(f"- {expense['item']}: {format_money(expense['amount'], expense['currency'])}")

    return "\n".join(lines)


def format_money(amount: float | int | str, currency: str) -> str:
    symbol = "₱" if (currency or "").upper() == "PHP" else f"{currency.upper()} "
    return f"{symbol}{float(amount):,.2f}"


def format_analytical_monthly_summary(summary: dict) -> str:
    currency = summary["currency"]
    change = summary.get("change_vs_last_month")
    change_text = "No last-month baseline yet"
    if change is not None:
        direction = "up" if change > 0 else "down"
        change_text = f"{abs(change):.1f}% {direction} vs last month"
    elif summary.get("previous_total", 0) == 0:
        change_text = "First month with spending data"

    lines = [
        f"Monthly insight for {summary['label']}",
        f"Total: {format_money(summary['total'], currency)} ({change_text})",
        f"Entries: {summary['count']}",
    ]

    top_category = summary.get("top_category")
    if top_category:
        lines.append(
            f"Top category: {top_category['name']} at {format_money(top_category['amount'], currency)} "
            f"({top_category['percentage']:.1f}% of total)"
        )

    highest_day = summary.get("highest_spending_day")
    if highest_day:
        lines.append(
            f"Highest spending day: {highest_day['label']} with {format_money(highest_day['amount'], currency)}"
        )

    lines.extend(
        [
            "",
            "By category:",
        ]
    )

    for category, amount in sorted(summary["by_category"].items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- {category}: {format_money(amount, currency)}")

    insight = summary.get("insight")
    if insight:
        lines.extend(["", f"Insight: {insight}"])

    return "\n".join(lines)


def format_stats_message(stats: dict) -> str:
    lines = [
        "Bot usage stats",
        f"Total users: {stats['total_users']}",
        f"Active this week: {stats['active_users_this_week']}",
        f"Active this month: {stats['active_users_this_month']}",
        f"Tracked events: {stats['total_events']}",
        "",
        "Event breakdown:",
    ]

    if stats["event_counts"]:
        for event_type, count in sorted(stats["event_counts"].items(), key=lambda item: item[1], reverse=True):
            lines.append(f"- {event_type}: {count}")
    else:
        lines.append("- No events yet")

    if stats["recent_users"]:
        lines.extend(["", "Recent users:"])
        for user in stats["recent_users"]:
            label = user.get("telegram_username") or user.get("first_name") or str(user["telegram_user_id"])
            lines.append(f"- {label} ({user['telegram_user_id']})")

    return "\n".join(lines)
