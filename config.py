import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
HIT_CHANNEL_ID = int(os.getenv("HIT_CHANNEL_ID", "0"))
STEALER_GC_ID = int(os.getenv("STEALER_GC_ID", "0"))

PER_USER_CONCURRENCY = int(os.getenv("PER_USER_CONCURRENCY", "10"))
GLOBAL_CONCURRENCY = int(os.getenv("GLOBAL_CONCURRENCY", "100"))
HIT_TIMEOUT = int(os.getenv("HIT_TIMEOUT", "90"))

DATABASE_URL = os.getenv("DATABASE_URL", "")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in .env")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL missing in .env")