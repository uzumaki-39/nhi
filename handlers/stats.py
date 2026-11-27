from telegram import Update
from telegram.ext import ContextTypes
import db


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await db.upsert_user(u.id, u.username or u.first_name or "")
    s = await db.user_stats(u.id)
    await update.message.reply_text(
        f"📊 *Stats for* `{u.id}`\n\n"
        f"Total hits: *{s['total']}*\n"
        f"Charged: *{s['charged']}*\n"
        f"Live (3DS/charge): *{s['live']}*",
        parse_mode="Markdown",
    )


async def cmd_hits(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await db.upsert_user(u.id, u.username or u.first_name or "")
    hits = await db.recent_hits(u.id, 10)
    if not hits:
        return await update.message.reply_text("No hits yet.")
    lines = []
    for h in hits:
        ts = h["created_at"].strftime("%m-%d %H:%M")
        lines.append(f"`{ts}` `{h['card']}` — *{h['status']}* ({h['gateway']}, {h['response_ms']}ms)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")