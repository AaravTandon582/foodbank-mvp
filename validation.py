"""Turning form strings into values the rest of the app can trust.

Every number arriving from a browser is attacker-controlled text. `float()`
alone is not enough: it raises on "abc" (a 500), and it happily accepts
"nan", "inf" and "1e308", which then poison every total they touch.

`number()` is the only way numbers should enter the app.
"""
import math

MAX_NAME = 80
MAX_TEXT = 40


class Invalid(ValueError):
    """Bad input from a form. The app turns this into a 400, not a 500."""


def number(form, key, default=None, low=0.0, high=1_000_000.0, required=False):
    """A finite float inside [low, high], or `Invalid`."""
    raw = form.get(key)
    if raw is None or str(raw).strip() == "":
        if required:
            raise Invalid(f"{key} is required")
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        raise Invalid(f"{key} must be a number")
    if not math.isfinite(value):
        raise Invalid(f"{key} must be a real number")
    if not low <= value <= high:
        raise Invalid(f"{key} must be between {low:g} and {high:g}")
    return value


def text(form, key, default="", limit=MAX_TEXT, required=False):
    """A trimmed, length-capped, control-character-free string."""
    raw = (form.get(key) or "").strip()
    if not raw:
        if required:
            raise Invalid(f"{key} is required")
        return default
    raw = "".join(ch for ch in raw if ch == " " or ch.isprintable())
    raw = " ".join(raw.split())
    if not raw:
        raise Invalid(f"{key} is required")
    return raw[:limit]


def date(form, key, default=None, required=False):
    """An ISO `YYYY-MM-DD` date, or `Invalid`.

    Browsers send `<input type=date>` in this format, but the field is text on
    the wire like any other and a hand-rolled POST can put anything in it.
    """
    import datetime as _dt

    raw = (form.get(key) or "").strip()
    if not raw:
        if required:
            raise Invalid(f"{key} is required")
        return default
    try:
        parsed = _dt.date.fromisoformat(raw)
    except ValueError:
        raise Invalid(f"{key} must be a date like 2026-10-05")
    if not _dt.date(2000, 1, 1) <= parsed <= _dt.date(2100, 1, 1):
        raise Invalid(f"{key} is out of range")
    return parsed.isoformat()
