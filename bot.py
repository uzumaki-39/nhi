import logging
import compat  # noqa — registers compat/ on sys.path
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
)
import config, db
from handlers import start, check, proxy, stats, admin

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("gokuhitter")


async def _post_init(app: Application):
    await db.init()
    log.info("DB ready. gokuhitter online.")


async def _post_shutdown(app: Application):
    await db.close()
    log.info("Shutdown.")


def main():
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start.cmd_start))
    app.add_handler(CommandHandler("help", start.cmd_help))
    app.add_handler(CommandHandler("ch", check.cmd_ch))
    app.add_handler(CommandHandler("adyen", check.cmd_adyen))
    app.add_handler(CommandHandler("epoch", check.cmd_epoch))
    app.add_handler(CommandHandler("checkout", check.cmd_checkout))
    app.add_handler(CommandHandler("paddle", check.cmd_paddle))
    app.add_handler(CommandHandler("whop", check.cmd_whop))
    app.add_handler(CommandHandler("jio", check.cmd_jio))
    app.add_handler(CommandHandler("px", proxy.cmd_px))
    app.add_handler(CommandHandler("stats", stats.cmd_stats))
    app.add_handler(CommandHandler("hits", stats.cmd_hits))
    app.add_handler(CommandHandler("ban", admin.cmd_ban))
    app.add_handler(CommandHandler("unban", admin.cmd_unban))
    app.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND, check.on_reply))

    log.info("Starting polling…")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()