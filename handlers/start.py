from telegram import Update
from telegram.ext import ContextTypes
import db


HELP = (
    "🦅 *NARUTOHITTERBOT bot*\n\n"
    "*Card Check*\n"
    "`/ch <url>` — Stripe checkout / invoice\n"
    "`/adyen <url>` — Adyen pay-by-link\n"
    "`/epoch <url>` — Epoch/WNU\n"
    "`/checkout <url>` — checkout.com\n"
    "`/paddle <url>` — Paddle\n"
    "`/whop <url>` — Whop\n"
    "`/jio <phone> <amount>` — Jio recharge (INR)\n\n"
    "Reply to your `/ch` message with cards, one per line.\n"
    "Supports `cc|mm|yy|cvv` and `cc|mm|yyyy|cvv`.\n\n"
    "*Proxies*\n"
    "`/px add` — reply with proxy list\n"
    "`/px list` — count + sample\n"
    "`/px clear` — wipe pool\n\n"
    "*Stats*\n"
    "`/stats` — your counters\n"
    "`/hits` — last 10 hits\n"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await db.upsert_user(u.id, u.username or u.first_name or "")
    await update.message.reply_text(
        f"`{u.id}` registered. Send `/help` for commands.", parse_mode="Markdown")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")