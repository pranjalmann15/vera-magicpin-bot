"""Formatting, parsing and encoding helpers shared by every layer."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

IST = timezone(timedelta(hours=5, minutes=30))

# Markers that show up when UTF-8 bytes were decoded as cp1252 (e.g. the judge
# simulator opens the dataset without encoding= on Windows: "₹" -> "â‚¹").
_MOJIBAKE_MARKERS = ("â", "Ã", "Â", "ð")


def fix_mojibake(value: Any) -> Any:
    """Recursively repair cp1252-decoded UTF-8 strings. Leaves clean text untouched."""
    if isinstance(value, str):
        if not any(m in value for m in _MOJIBAKE_MARKERS):
            return value
        try:
            return value.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return value
    if isinstance(value, dict):
        return {fix_mojibake(k): fix_mojibake(v) for k, v in value.items()}
    if isinstance(value, list):
        return [fix_mojibake(v) for v in value]
    return value


# --------------------------------------------------------------------------- numbers

def indian_group(n: int) -> str:
    """12345678 -> '1,23,45,678' (Indian digit grouping)."""
    s = str(abs(int(n)))
    if len(s) <= 3:
        out = s
    else:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        out = ",".join(parts + [tail])
    return ("-" if n < 0 else "") + out


def num(n: Any) -> str:
    """Human integer with Indian grouping; passes through non-numbers."""
    try:
        f = float(n)
    except (TypeError, ValueError):
        return str(n)
    if f.is_integer():
        return indian_group(int(f))
    return f"{f:.1f}"


def inr(n: Any) -> str:
    try:
        return "₹" + indian_group(int(round(float(n))))
    except (TypeError, ValueError):
        return f"₹{n}"


def pct(fraction: Any, signed: bool = False, decimals: Optional[int] = None) -> str:
    """0.021 -> '2.1%', -0.5 -> '50%' (or '-50%' when signed)."""
    try:
        f = float(fraction) * 100
    except (TypeError, ValueError):
        return str(fraction)
    v = f if signed else abs(f)
    if decimals is None:
        decimals = 0 if abs(v - round(v)) < 0.05 or abs(v) >= 10 else 1
    s = f"{v:.{decimals}f}"
    if signed and v > 0:
        s = "+" + s
    return s + "%"


def ratio_phrase(mine: float, peer: float) -> str:
    """Compact comparison: 'about half', 'about 2x', '30% below'."""
    if not peer:
        return ""
    r = mine / peer
    if r >= 1.9:
        return f"{r:.1f}x".replace(".0x", "x")
    if r >= 1.1:
        return f"{round((r - 1) * 100)}% above"
    if r > 0.9:
        return "in line with"
    if 0.4 <= r <= 0.6:
        return "about half"
    return f"{round((1 - r) * 100)}% below"


# --------------------------------------------------------------------------- dates

def parse_dt(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    t = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        try:
            dt = datetime.fromisoformat(t[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


def parse_date(s: Any) -> Optional[date]:
    dt = parse_dt(s)
    return dt.astimezone(IST).date() if dt else None


def day_month(d: date) -> str:
    return f"{d.day} {d.strftime('%b')}"


def dow_day_month(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def clock(dt: datetime) -> str:
    """18:00 -> '6pm', 19:30 -> '7:30pm'."""
    local = dt.astimezone(IST)
    h, m = local.hour, local.minute
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{suffix}" if m == 0 else f"{h12}:{m:02d}{suffix}"


def slot_label(iso: str, fallback: str = "") -> str:
    """Render a slot from its ISO timestamp so the weekday is always correct."""
    dt = parse_dt(iso)
    if not dt:
        return fallback
    return f"{dow_day_month(dt.astimezone(IST).date())}, {clock(dt)}"


def months_between(a: date, b: date) -> float:
    return (b - a).days / 30.44


def weeks_phrase(days: int) -> str:
    if days < 14:
        return f"{days} days"
    weeks = round(days / 7)
    if weeks < 9:
        return f"about {weeks} weeks"
    months = round(days / 30.44)
    return f"about {months} months"


# --------------------------------------------------------------------------- text

def humanize_token(s: str) -> str:
    """'6_month_cleaning' -> '6-month cleaning', 'delivery_late' -> 'late delivery'."""
    special = {
        "delivery_late": "late delivery",
        "wait_time": "waiting time",
        "saturday_wait": "Saturday waiting time",
        "doctor_manner": "the doctor's manner",
        "morning_crowd": "morning crowding",
        "weekend_busy": "weekend rush",
    }
    if s in special:
        return special[s]
    s = re.sub(r"(\d+)_(month|week|day|year)", r"\1-\2", s)
    return s.replace("_", " ").strip()


def clean_owner_name(raw: str) -> str:
    """'Dr. Asha' -> 'Asha'."""
    return re.sub(r"^(dr\.?\s*)", "", (raw or "").strip(), flags=re.I).strip()


def split_parent(name: str) -> tuple[str, Optional[str]]:
    """'Karthik (parent: Sumitra)' -> ('Karthik', 'Sumitra')."""
    m = re.match(r"\s*([^()]+?)\s*\(\s*parent\s*:\s*([^)]+)\)", name or "")
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return (name or "").strip(), None


def normalize_msg(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^\w\sऀ-ॿ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def squash(s: str) -> str:
    """Collapse whitespace inside lines, keep intentional line breaks."""
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.strip().split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln:
            if not blank and out:
                out.append("")
            blank = True
            continue
        out.append(ln)
        blank = False
    return "\n".join(out).strip()
