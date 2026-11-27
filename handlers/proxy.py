from telegram import Update
from telegram.ext import ContextTypes
import db
from proxies import parse_proxy


async def cmd_px(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await db.upsert_user(u.id, u.username or u.first_name or "")
    if not ctx.args:
        return await update.message.reply_text(
            "`/px add` (reply with list) | `/px list` | `/px clear`",
            parse_mode="Markdown")
    action = ctx.args[0].lower()

    if action == "add":
        if not update.message.reply_to_message:
            return await update.message.reply_text("Reply to a message containing proxies.")
        text = update.message.reply_to_message.text or ""
        parsed = []
        for line in text.splitlines():
            p = parse_proxy(line)
            if p:
                parsed.append(p)
        if not parsed:
            return await update.message.reply_text("No valid proxies.")
        n = await db.add_proxies(u.id, parsed)
        total = await db.proxy_count(u.id)
        return await update.message.reply_text(f"✅ Added {n}. Total alive: {total}")

    if action == "list":
        n = await db.proxy_count(u.id)
        pool = await db.get_proxies(u.id)
        sample = "\n".join(f"`{p['raw']}`" for p in pool[:5])
        return await update.message.reply_text(
            f"Total alive: {n}\n\n{sample}" if pool else "Empty pool.",
            parse_mode="Markdown")

    if action == "clear":
        n = await db.clear_proxies(u.id)
        return await update.message.reply_text(f"Cleared {n} proxies.")