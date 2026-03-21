from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.claude_handler import ClaudeHandler
from app.database import Database


logger = logging.getLogger(__name__)


def build_application(
    token: str,
    database: Database,
    claude_handler: ClaudeHandler,
) -> Application:
    application = Application.builder().token(token).build()
    application.bot_data["db"] = database
    application.bot_data["claude"] = claude_handler

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("undo", undo_command))
    application.add_handler(CommandHandler("summary", summary_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    return application


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "See how to log expenses"),
            BotCommand("help", "Show examples and supported categories"),
            BotCommand("summary", "Get this month's summary"),
            BotCommand("undo", "Delete your latest expense"),
        ]
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    summary = db.get_period_summary(user.id, "month")
    await update.effective_message.reply_text(format_summary_message(summary))


async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    user = update.effective_user
    last_expense = db.get_last_expense(user.id)
    if not last_expense:
        await update.effective_message.reply_text("I couldn’t find any saved expense to undo yet.")
        return

    db.delete_expense(last_expense["id"])
    await update.effective_message.reply_text(
        f"Removed your latest expense: {last_expense['item']} ({format_money(last_expense['amount'], last_expense['currency'])})."
    )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    claude: ClaudeHandler = context.application.bot_data["claude"]
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return

    text = message.text.strip()
    if text.lower() == "undo":
        await undo_command(update, context)
        return

    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    try:
        parsed = await claude.parse_message(text)
    except Exception:
        logger.exception("Claude parsing failed")
        await message.reply_text("I had trouble reading that. Please try again in a moment.")
        return

    intent = parsed["intent"]
    if parsed["needs_clarification"]:
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
        await message.reply_text(
            "Saved: "
            f"{saved['item']} for {format_money(saved['amount'], saved['currency'])} "
            f"under {saved['category']}."
        )
        return

    if intent == "summary":
        summary = db.get_period_summary(user.id, parsed.get("period") or "month", now=datetime.now(timezone.utc))
        await message.reply_text(format_summary_message(summary))
        return

    if intent == "undo":
        await undo_command(update, context)
        return

    await message.reply_text(
        "I can log expenses, undo the latest one, or show summaries. Try `snacks 85` or `how much did I spend this week?`"
    )


def format_summary_message(summary: dict) -> str:
    if summary["count"] == 0:
        return f"You have no expenses recorded for {summary['label']} yet."

    currency = summary["currency"]
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
