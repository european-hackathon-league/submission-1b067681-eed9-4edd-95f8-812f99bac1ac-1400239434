"""Turn strings as found on the sites into values: decimals (de-DE / en-US formats), dates, references; and back into
the German display format the bid form expects. Pure functions, unit tested; no scenario knowledge."""
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_ZERO_WIDTH = re.compile(r"[​-‍⁠﻿­]")
CENT = Decimal("0.01")


def strip_invisible(text) -> str:
    return _ZERO_WIDTH.sub("", str(text or "")).strip()


def norm_ref(text) -> str:
    """Comparable form of a reference/key: no invisible characters, no spaces, upper case."""
    return re.sub(r"\s+", "", strip_invisible(text)).upper()


def to_decimal(text, locale: str | None = None) -> Decimal | None:
    """'1.234,56' (de-DE), '1,234.56' (en-US), '238,098 (en-US)', '85,00 EUR', 12 -> Decimal. None if no number.

    A locale given in the text, e.g. '(en-US)' or 'Zahlenformat: en-US', wins over `locale`. Without any hint a single
    separator followed by exactly three digits is ambiguous; it is read as a thousands separator only when the other
    separator style is impossible (e.g. '1.234' -> 1234 for de-DE hints, else 1.234).
    """
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float, Decimal)):
        return Decimal(str(text)) if Decimal(str(text)).is_finite() else None
    s = strip_invisible(text)
    hint = re.search(r"\b(de-DE|en-US|de|en)\b", s, re.I)
    loc = (hint.group(1) if hint else (locale or "")).lower()
    m = re.search(r"-?\d[\d.,\s' ]*", s)
    if not m:
        return None
    num = re.sub(r"[\s' ]", "", m.group(0)).rstrip(".,")
    if loc.startswith("de"):
        num = num.replace(".", "").replace(",", ".")
    elif loc.startswith("en"):
        num = num.replace(",", "")
    elif "," in num and "." in num:
        num = num.replace(".", "").replace(",", ".") if num.rfind(",") > num.rfind(".") else num.replace(",", "")
    elif "," in num:
        num = num.replace(",", ".") if num.count(",") == 1 else num.replace(",", "")
    elif num.count(".") > 1:
        num = num.replace(".", "")
    try:
        result = Decimal(num)
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def to_int(text, locale: str | None = None) -> int | None:
    value = to_decimal(text, locale)
    return int(value) if value is not None and value == value.to_integral_value() else None


def round_cents(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def de_number(value, places: int = 2) -> str:
    """Decimal -> German display format: 1234.5 -> '1.234,50'."""
    q = Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    whole, _, frac = f"{abs(q):f}".partition(".")
    whole = f"{int(whole):,}".replace(",", ".")
    return f"{sign}{whole},{frac}" if places else f"{sign}{whole}"


_MONTHS = {"jan": 1, "feb": 2, "mär": 3, "mar": 3, "apr": 4, "mai": 5, "may": 5, "jun": 6, "jul": 7, "aug": 8,
           "sep": 9, "okt": 10, "oct": 10, "nov": 11, "dez": 12, "dec": 12}


def to_datetime(text) -> datetime | None:
    """ISO strings ('2027-01-24T18:00:00Z'), German dates ('24.01.2027, 20:00'), with an optional explicit order hint
    such as '01/05 (MM/TT) 2027' or '17/01 (TT/MM) 2027'. Naive results are treated as UTC."""
    if text is None:
        return None
    if isinstance(text, datetime):
        return text if text.tzinfo else text.replace(tzinfo=timezone.utc)
    s = strip_invisible(text)
    iso = re.search(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?", s)
    if iso:
        raw = iso.group(0).replace("Z", "+00:00").replace(" ", "T")
        try:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    time_m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    hh, mm, ss = (int(time_m.group(1)), int(time_m.group(2)), int(time_m.group(3) or 0)) if time_m else (0, 0, 0)
    de = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if de:
        d, m, y = map(int, de.groups())
        return _safe(y, m, d, hh, mm, ss)
    slash = re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", s)
    if slash:
        a, b = int(slash.group(1)), int(slash.group(2))
        year = slash.group(3) or (re.search(r"\b(\d{4})\b", s[slash.end():] + " " + s).group(1)
                                  if re.search(r"\b\d{4}\b", s) else None)
        if year:
            month_first = bool(re.search(r"MM\s*/\s*(TT|DD)", s, re.I)) or (
                not re.search(r"(TT|DD)\s*/\s*MM", s, re.I) and b > 12)
            m, d = (a, b) if month_first else (b, a)
            return _safe(int(year), m, d, hh, mm, ss)
    word = re.search(r"(\d{1,2})\.?\s+([A-Za-zä]{3})[a-zä]*\.?\s+(\d{4})", s)
    if word and word.group(2).lower()[:3] in _MONTHS:
        return _safe(int(word.group(3)), _MONTHS[word.group(2).lower()[:3]], int(word.group(1)), hh, mm, ss)
    return None


def _safe(y, m, d, hh=0, mm=0, ss=0):
    try:
        return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)
    except ValueError:
        return None


def validity_end(value):
    """Extend date-only endpoints; preserve explicitly stated midnight timestamps."""
    dt = to_datetime(value)
    if dt and not re.search(r"\d{1,2}:\d{2}|T\d", str(value)):
        return dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt
