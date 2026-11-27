from telegram import Update
from telegram.ext import ContextTypes
import db, config


def _is_owner(uid: int) -> bool:
    return uid == config.OWNER_ID


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update.effective_user.id):
        return
    if not ctx.args:
        return await update.message.reply_text("Usage: /ban <user_id>")
    try:
        uid = int(ctx.args[0])
    except ValueError:
        return
    await db.ban_user(uid, True)
    await update.message.reply_text(f"Banned {uid}.")


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update.effective_user.id):
        return
    if not ctx.args:
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        return
    await db.ban_user(uid, False)
    await update.message.reply_text(f"Unbanned {uid}.")