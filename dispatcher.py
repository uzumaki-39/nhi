"""
Gateway router — takes a URL, returns (engine_class_name, gateway_key).
"""
from urllib.parse import urlparse, parse_qs
from typing import Tuple


def detect(url: str) -> Tuple[str, str]:
    """Return (gateway_key, engine_import_path)."""
    u = url.strip().lower()
    parsed = urlparse(u)
    host = parsed.netloc
    path = parsed.path
    q = parse_qs(parsed.query)

    # Stripe
    if any(h in host for h in ("checkout.stripe.com", "pay.stripe.com", "invoice.stripe.com")):
        return "stripe", "engines.stripe_hit_engine"
    if "/c/pay/cs_" in path or "cs_live_" in u or "cs_test_" in u:
        return "stripe", "engines.stripe_hit_engine"
    if any(u.startswith(p) for p in ("pi_", "seti_")) and "_secret_" in u:
        return "stripe", "engines.stripe_hitter"
    if "/i/" in path and "invoice.stripe.com" in host:
        return "stripe", "engines.stripe_hitter"

    # Adyen
    if "adyen.com" in host or "adyen.link" in host:
        return "adyen", "engines.adyen_hitter"
    if any(k in q for k in ("adyen", "pbl")) or "paybylink" in u:
        return "adyen", "engines.adyen_hitter"

    # Epoch / WNU
    if any(h in host for h in ("epoch.com", "billing.epoch", "wnu.com")):
        return "epoch", "engines.epoch_hitter"

    # Checkout.com
    if "checkout.com" in host and ("pay" in host or "/page/" in path):
        return "checkout", "engines.checkout_hitter"

    # Paddle
    if any(h in host for h in ("paddle.io", "paddle.com", "buy.paddle.com", "pay.paddle.io")):
        return "paddle", "engines.paddle_hitter"
    if "_ptxn" in q or "_ptxn" in u:
        return "paddle", "engines.paddle_hitter"

    # Whop
    if "whop.com" in host:
        return "whop", "engines.whop_hitter"

    return "unknown", ""


SUPPORTED = {
    "stripe": "engines.stripe_hit_engine",
    "adyen":  "engines.adyen_hitter",
    "epoch":  "engines.epoch_hitter",
    "checkout": "engines.checkout_hitter",
    "paddle": "engines.paddle_hitter",
    "whop":   "engines.whop_hitter",
}