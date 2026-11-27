import random
import asyncio
import aiohttp
import re
from typing import Optional, Dict, List
from urllib.parse import urlsplit, urlunsplit
import db


_SCHEME_RE = re.compile(r"^(https?|socks[45]h?)://", re.I)


def parse_proxy(line: str) -> Optional[Dict]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    raw = line
    scheme = "http"
    m = _SCHEME_RE.match(line)
    if m:
        scheme = m.group(1)
        line = line[len(m.group(0)):]

    parts = line.split(":")
    host = port = user = pw = None
    if len(parts) == 4:
        if "." in parts[0] and parts[1].isdigit():
            host, port, user, pw = parts
        elif "." in parts[2] and parts[3].isdigit():
            user, pw, host, port = parts
        elif parts[1].isdigit():
            host, port, user, pw = parts
        else:
            user, pw, host, port = parts
    elif len(parts) == 2:
        host, port = parts
    else:
        return None
    if not (host and port and port.isdigit()):
        return None

    server = f"{scheme}://{host}:{port}"
    return {
        "raw": raw, "server": server,
        "username": user, "password": pw,
    }


def build_proxies_dict(p: Dict) -> Optional[Dict[str, str]]:
    if not p:
        return None
    server = p["server"]
    if p.get("username") and p.get("password"):
        parts = urlsplit(server)
        netloc = f"{p['username']}:{p['password']}@{parts.netloc}"
        purl = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    else:
        purl = server
    return {"http": purl, "https": purl}


async def geo_lookup(p: Dict, timeout: float = 3.0) -> Optional[str]:
    """Return ISO country code for the proxy, or None."""
    purl = build_proxies_dict(p)["http"]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("http://ip-api.com/json/?fields=countryCode",
                             proxy=purl,
                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status == 200:
                    d = await r.json()
                    return (d.get("countryCode") or "").upper() or None
    except Exception:
        return None
    return None


async def pick_proxy(user_id: int, target_country: Optional[str] = None) -> Optional[Dict]:
    pool = await db.get_proxies(user_id, alive_only=True)
    if not pool:
        return None

    if target_country:
        target = target_country.upper()
        matches = [p for p in pool if (p.get("country") or "").upper() == target]
        if matches:
            return random.choice(matches)

        # try to enrich country for up to 5 uncached proxies
        uncached = [p for p in pool if not p.get("country")][:5]
        tasks = [geo_lookup(p) for p in uncached]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for p, cc in zip(uncached, results):
            if isinstance(cc, str) and cc:
                await db.set_proxy_country(user_id, p["raw"], cc)
                if cc == target:
                    return p

    return random.choice(pool)