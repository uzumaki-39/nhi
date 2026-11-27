import re
from typing import Optional, Dict, List, Tuple


def luhn_ok(card: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", card)]
    if not digits:
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def brand_of(card: str) -> str:
    c = re.sub(r"\D", "", card or "")
    if c.startswith("4"):
        return "VISA"
    if c.startswith(("51", "52", "53", "54", "55")) or (2221 <= int(c[:4] or "0") <= 2720):
        return "MASTERCARD"
    if c.startswith(("34", "37")):
        return "AMEX"
    if c.startswith(("60", "65", "81", "82", "508")):
        return "RUPAY"
    if c.startswith(("6011", "65", "644", "645")):
        return "DISCOVER"
    if c.startswith(("3528", "3529")) or "3528" <= c[:4] <= "3589":
        return "JCB"
    return "UNKNOWN"


def parse_line(line: str) -> Optional[Dict]:
    """Accepts: cc|mm|yy|cvv, cc|mm|yy, cc|mm|yyyy|cvv, cc|mm|yyyy."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in re.split(r"[|:;/\s]+", line) if p.strip()]
    if len(parts) < 3:
        return None
    try:
        cc = re.sub(r"\D", "", parts[0])
        mm = parts[1].zfill(2)
        yy = parts[2]
        if len(yy) == 2:
            yy = "20" + yy
        cvv = parts[3] if len(parts) > 3 else "000"
        if not (13 <= len(cc) <= 19):
            return None
        if not (1 <= int(mm) <= 12):
            return None
        if len(yy) != 4:
            return None
        return {
            "card": cc, "month": mm, "year": yy, "cvv": cvv,
            "brand": brand_of(cc), "luhn": luhn_ok(cc),
            "formatted": f"{cc}|{mm}|{yy[-2:]}|{cvv}",
        }
    except Exception:
        return None


def parse_bulk(text: str) -> Tuple[List[Dict], List[str]]:
    good, bad = [], []
    for line in text.splitlines():
        parsed = parse_line(line)
        if parsed:
            good.append(parsed)
        elif line.strip():
            bad.append(line.strip()[:40])
    return good, bad