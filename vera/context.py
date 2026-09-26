"""Ctx — one resolved view over (category, merchant, trigger, customer).

Every playbook reads facts through this object, never from raw dicts. Two reasons:
  1. Sparse/generated contexts (placeholder trigger payloads, merchants with no
     offers or history) are handled once, here, with honest fallbacks.
  2. It keeps a *fact ledger*: every number present anywhere in the four contexts,
     plus every number a playbook derives, is registered. The validator refuses a
     message containing a number that is not in the ledger — that is how the bot
     guarantees it never fabricates a statistic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .textutil import (
    IST, clean_owner_name, humanize_token, parse_date, parse_dt, split_parent,
)

# Reference "today" of the dataset (all seed triggers are written relative to it).
DATASET_ANCHOR = datetime(2026, 4, 26, 10, 0, tzinfo=IST)

HINDI_BELT = {
    "delhi", "new delhi", "mumbai", "pune", "jaipur", "lucknow", "chandigarh",
    "ahmedabad", "hyderabad", "noida", "gurgaon", "gurugram", "indore", "bhopal",
    "kanpur", "patna", "nagpur", "surat", "vadodara",
}
LOCAL_GREETING = {"ta": "Vanakkam", "te": "Namaskaram", "kn": "Namaskara", "mr": "Namaskar"}

_NUM_RE = re.compile(r"(?<![\d.])-?\d[\d,]*(?:\.\d+)?")


def _numbers_in(value: Any, sink: set[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        _add_number(float(value), sink)
    elif isinstance(value, str):
        for tok in _NUM_RE.findall(value):
            try:
                _add_number(float(tok.replace(",", "")), sink)
            except ValueError:
                pass
    elif isinstance(value, dict):
        for k, v in value.items():
            _numbers_in(k, sink)
            _numbers_in(v, sink)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _numbers_in(v, sink)


def _add_number(f: float, sink: set[float]) -> None:
    for v in (f, abs(f)):
        sink.add(round(v, 2))
        sink.add(round(v))
        if abs(v) <= 1.5:  # fractions are rendered as percentages
            sink.add(round(v * 100, 1))
            sink.add(round(v * 100))


@dataclass
class Voice:
    """Language register for one recipient."""
    lang: str = "en"               # "en" | "hinglish" | "hi"
    greeting: Optional[str] = None  # local-language greeting for south-Indian customers

    @property
    def hindi(self) -> bool:
        return self.lang in ("hinglish", "hi")

    def t(self, en: str, hi: Optional[str] = None) -> str:
        return hi if (self.hindi and hi) else en


@dataclass
class Ctx:
    category: dict
    merchant: dict
    trigger: dict
    customer: Optional[dict] = None
    now: Optional[datetime] = None
    lang_override: Optional[str] = None
    ledger: set = field(default_factory=set)

    def __post_init__(self) -> None:
        self.category = self.category or {}
        self.merchant = self.merchant or {}
        self.trigger = self.trigger or {}
        for part in (self.category, self.merchant, self.trigger, self.customer or {}):
            _numbers_in(part, self.ledger)
        self.ref_now = self._reference_now()
        self.allow(self.ref_now.day, self.ref_now.year)

    # ------------------------------------------------------------ ledger
    def allow(self, *values: Any) -> None:
        for v in values:
            _numbers_in(v, self.ledger)

    def has_number(self, f: float) -> bool:
        return round(f, 2) in self.ledger or round(f) in self.ledger or round(f, 1) in self.ledger

    # ------------------------------------------------------------ time
    def _reference_now(self) -> datetime:
        """The judge's clock when it agrees with the trigger's own timeline,
        otherwise the dataset anchor (the local simulator uses wall-clock time,
        which is months past every seed trigger's expiry)."""
        exp = parse_dt(self.trigger.get("expires_at"))
        now = self.now
        if now and (exp is None or now <= exp + timedelta(days=1)):
            return now.astimezone(IST)
        if exp and DATASET_ANCHOR > exp:
            return (exp - timedelta(days=1)).astimezone(IST)
        return DATASET_ANCHOR

    @property
    def today(self) -> date:
        return self.ref_now.date()

    # ------------------------------------------------------------ trigger
    @property
    def kind(self) -> str:
        return self.trigger.get("kind", "generic")

    @property
    def payload(self) -> dict:
        p = self.trigger.get("payload") or {}
        return {} if p.get("placeholder") else p

    @property
    def is_placeholder(self) -> bool:
        return bool((self.trigger.get("payload") or {}).get("placeholder")) or not self.payload

    @property
    def urgency(self) -> int:
        try:
            return int(self.trigger.get("urgency", 2))
        except (TypeError, ValueError):
            return 2

    # ------------------------------------------------------------ category
    @property
    def slug(self) -> str:
        return self.category.get("slug") or self.merchant.get("category_slug") or "general"

    @property
    def peer(self) -> dict:
        return self.category.get("peer_stats") or {}

    @property
    def taboos(self) -> list[str]:
        v = self.category.get("voice") or {}
        return list(v.get("vocab_taboo") or v.get("taboos") or [])

    def digest_item(self, item_id: Optional[str] = None, kinds: tuple = ()) -> Optional[dict]:
        digest = self.category.get("digest") or []
        if item_id:
            for d in digest:
                if d.get("id") == item_id:
                    return d
        if kinds:
            for d in digest:
                if d.get("kind") in kinds:
                    return d
        return None if (item_id or kinds) else (digest[0] if digest else None)

    def digest_mentioning(self, word: str) -> Optional[dict]:
        w = word.lower()
        for d in self.category.get("digest") or []:
            if w in json.dumps(d).lower():
                return d
        return None

    def seasonal_beat(self, month: Optional[int] = None) -> Optional[dict]:
        m = month or self.today.month
        names = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
        for beat in self.category.get("seasonal_beats") or []:
            rng = str(beat.get("month_range", "")).lower()
            parts = re.findall(r"[a-z]{3}", rng)
            idx = [names.index(p) + 1 for p in parts if p in names]
            if not idx:
                continue
            lo, hi = idx[0], idx[-1]
            inside = lo <= m <= hi if lo <= hi else (m >= lo or m <= hi)
            if inside:
                return beat
        return None

    def beat_matching(self, *words: str) -> Optional[dict]:
        for beat in self.category.get("seasonal_beats") or []:
            note = str(beat.get("note", "")).lower()
            if any(w in note for w in words):
                return beat
        return None

    def trend(self, *words: str) -> Optional[dict]:
        signals = self.category.get("trend_signals") or []
        for w in words:
            for s in signals:
                if w in str(s.get("query", "")).lower():
                    return s
        return None

    def top_trend(self) -> Optional[dict]:
        signals = [s for s in self.category.get("trend_signals") or [] if isinstance(s.get("delta_yoy"), (int, float))]
        return max(signals, key=lambda s: s["delta_yoy"]) if signals else None

    def catalog(self, *types: str) -> list[dict]:
        cat = self.category.get("offer_catalog") or []
        return [o for o in cat if not types or o.get("type") in types]

    def catalog_offer(self, *words: str) -> Optional[dict]:
        for w in words:
            for o in self.category.get("offer_catalog") or []:
                if w in str(o.get("title", "")).lower():
                    return o
        return None

    # ------------------------------------------------------------ merchant
    @property
    def mid(self) -> str:
        return self.merchant.get("merchant_id", "")

    @property
    def ident(self) -> dict:
        return self.merchant.get("identity") or {}

    @property
    def biz(self) -> str:
        return self.ident.get("name") or "your business"

    @property
    def owner(self) -> str:
        return clean_owner_name(self.ident.get("owner_first_name", "")) or ""

    @property
    def is_doctor(self) -> bool:
        return self.slug == "dentists" or str(self.ident.get("owner_first_name", "")).lower().startswith("dr")

    @property
    def salutation(self) -> str:
        if not self.owner:
            return f"{self.biz} team"
        return f"Dr. {self.owner}" if self.is_doctor else self.owner

    @property
    def signoff_name(self) -> str:
        """How the merchant refers to itself when messaging its own customers. Clinics put the
        doctor's name first — patients book the doctor, not the signboard."""
        if self.is_doctor and self.owner and self.owner.lower() not in self.biz.lower():
            return f"Dr. {self.owner}'s clinic ({self.biz})"
        return self.biz

    @property
    def locality(self) -> str:
        return self.ident.get("locality") or self.ident.get("city") or ""

    @property
    def city(self) -> str:
        return self.ident.get("city") or ""

    @property
    def perf(self) -> dict:
        return self.merchant.get("performance") or {}

    @property
    def delta(self) -> dict:
        return self.perf.get("delta_7d") or {}

    @property
    def agg(self) -> dict:
        return self.merchant.get("customer_aggregate") or {}

    @property
    def sub(self) -> dict:
        return self.merchant.get("subscription") or {}

    @property
    def signals(self) -> list[str]:
        return [str(s) for s in self.merchant.get("signals") or []]

    def has_signal(self, prefix: str) -> bool:
        return any(s.startswith(prefix) for s in self.signals)

    def signal_value(self, prefix: str) -> Optional[str]:
        for s in self.signals:
            if s.startswith(prefix) and ":" in s:
                return s.split(":", 1)[1]
        return None

    @property
    def active_offers(self) -> list[dict]:
        return [o for o in self.merchant.get("offers") or [] if o.get("status") == "active"]

    @property
    def expired_offers(self) -> list[dict]:
        return [o for o in self.merchant.get("offers") or [] if o.get("status") in ("expired", "paused")]

    def offer_matching(self, *words: str) -> Optional[dict]:
        for w in words:
            for o in self.active_offers:
                if w in str(o.get("title", "")).lower():
                    return o
        return None

    @property
    def verified(self) -> Optional[bool]:
        return self.ident.get("verified")

    @property
    def history(self) -> list[dict]:
        return self.merchant.get("conversation_history") or []

    @property
    def last_merchant_msg(self) -> Optional[dict]:
        for turn in reversed(self.history):
            if turn.get("from") == "merchant":
                return turn
        return None

    @property
    def last_vera_msg(self) -> Optional[dict]:
        for turn in reversed(self.history):
            if turn.get("from") == "vera":
                return turn
        return None

    @property
    def first_contact(self) -> bool:
        return not self.history

    def review(self, sentiment: Optional[str] = None, theme: Optional[str] = None) -> list[dict]:
        out = []
        for r in self.merchant.get("review_themes") or []:
            if sentiment and r.get("sentiment") != sentiment:
                continue
            if theme and r.get("theme") != theme:
                continue
            out.append(r)
        return sorted(out, key=lambda r: -(r.get("occurrences_30d") or 0))

    # ------------------------------------------------------------ performance judgement
    def peer_gap(self, metric: str) -> Optional[tuple[float, float]]:
        mine = self.perf.get(metric)
        peer_key = {"views": "avg_views_30d", "calls": "avg_calls_30d",
                    "directions": "avg_directions_30d", "ctr": "avg_ctr"}.get(metric)
        peer = self.peer.get(peer_key) if peer_key else None
        if isinstance(mine, (int, float)) and isinstance(peer, (int, float)) and peer:
            return float(mine), float(peer)
        return None

    def worst_delta(self) -> Optional[tuple[str, float]]:
        items = [(k.replace("_pct", ""), v) for k, v in self.delta.items() if isinstance(v, (int, float))]
        items = [i for i in items if i[1] < 0]
        return min(items, key=lambda i: i[1]) if items else None

    def best_delta(self) -> Optional[tuple[str, float]]:
        items = [(k.replace("_pct", ""), v) for k, v in self.delta.items() if isinstance(v, (int, float))]
        items = [i for i in items if i[1] > 0]
        return max(items, key=lambda i: i[1]) if items else None

    def standout_strength(self) -> Optional[tuple[str, float, float]]:
        """The metric where this merchant beats its peer group by the widest margin."""
        best = None
        for metric in ("ctr", "calls", "directions", "views"):
            gap = self.peer_gap(metric)
            if gap and gap[1] and gap[0] / gap[1] >= 1.15:
                if best is None or gap[0] / gap[1] > best[1] / best[2]:
                    best = (metric, gap[0], gap[1])
        return best

    def standout_weakness(self) -> Optional[tuple[str, float, float]]:
        worst = None
        for metric in ("calls", "ctr", "views", "directions"):
            gap = self.peer_gap(metric)
            if gap and gap[1] and gap[0] / gap[1] <= 0.8:
                if worst is None or gap[0] / gap[1] < worst[1] / worst[2]:
                    worst = (metric, gap[0], gap[1])
        return worst

    # ------------------------------------------------------------ customer
    @property
    def cust_ident(self) -> dict:
        return (self.customer or {}).get("identity") or {}

    @property
    def cust_names(self) -> tuple[str, Optional[str]]:
        """(person the message is about, parent to address if a minor)."""
        name = self.cust_ident.get("name") or ""
        if name.startswith("("):  # "(walk-in, no profile)"
            return "", None
        return split_parent(name)

    @property
    def cust_addressee(self) -> str:
        person, parent = self.cust_names
        return parent or person

    @property
    def rel(self) -> dict:
        return (self.customer or {}).get("relationship") or {}

    @property
    def prefs(self) -> dict:
        return (self.customer or {}).get("preferences") or {}

    @property
    def cust_state(self) -> str:
        return (self.customer or {}).get("state", "")

    def last_visit(self) -> Optional[date]:
        return parse_date(self.rel.get("last_visit"))

    def consent_ok(self) -> bool:
        if not self.customer:
            return True
        consent = self.customer.get("consent") or {}
        if not consent.get("opted_in_at"):
            return False
        if self.prefs.get("reminder_opt_in") is False and self.kind not in ("appointment_tomorrow",):
            return False
        if str(self.prefs.get("channel", "")).startswith("none"):
            return False
        return True

    # ------------------------------------------------------------ language
    def merchant_voice(self) -> Voice:
        langs = [str(l).lower() for l in self.ident.get("languages") or []]
        code_mix = str((self.category.get("voice") or {}).get("code_mix", ""))
        city = self.city.lower()
        # The latest merchant message wins when it clearly shows a language.
        last = self.last_merchant_msg
        if last and detect_hinglish(last.get("body", "")):
            return Voice("hinglish")
        if "hi" in langs and city in HINDI_BELT and code_mix != "english_only":
            return Voice("hinglish")
        return Voice("en")

    def customer_voice(self) -> Voice:
        pref = str(self.cust_ident.get("language_pref", "")).lower()
        if pref in ("hi", "hindi"):
            return Voice("hi")
        if "hi" in pref and ("mix" in pref or "en" in pref):
            return Voice("hinglish")
        for code, greet in LOCAL_GREETING.items():
            if pref.startswith(code):
                return Voice("en", greeting=greet)
        return Voice("en")

    def voice(self) -> Voice:
        natural = self.customer_voice() if self.customer else self.merchant_voice()
        if self.lang_override:
            return Voice(self.lang_override, greeting=natural.greeting)
        return natural


_HINGLISH_WORDS = {
    "hai", "hain", "kya", "nahi", "nahin", "haan", "han", "karo", "kar", "karna", "mujhe",
    "aap", "aapka", "aapke", "hum", "humein", "chahiye", "bhej", "bhejo", "theek", "thik",
    "acha", "accha", "achha", "abhi", "kal", "baad", "mein", "ka", "ki", "ke", "ko", "se",
    "bolo", "batao", "judna", "judrna", "kitna", "kaise", "kab", "kyun", "matlab", "ji",
    "bilkul", "zaroor", "sahi", "chalega", "dijiye", "karein", "hoga", "raha", "rahi",
}


def detect_hinglish(text: str) -> bool:
    if re.search(r"[ऀ-ॿ]", text or ""):
        return True
    words = re.findall(r"[a-z]+", (text or "").lower())
    hits = sum(1 for w in words if w in _HINGLISH_WORDS)
    return hits >= 2 or (hits >= 1 and len(words) <= 3)


def detect_english(text: str) -> bool:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return len(words) >= 4 and not detect_hinglish(text)


def pretty_services(services: list) -> list[str]:
    return [humanize_token(str(s)) for s in services or [] if s and s != "..."]
