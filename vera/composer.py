"""Composer: playbook draft → validation → (optional) LLM polish → re-validation.

The playbook draft is always produced first and is always safe to send. The LLM
may only make it read better; if its rewrite adds a number, a taboo word, a
second CTA or anything else the validator rejects, the draft wins.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

from .context import Ctx
from .llm import LLM
from .playbooks import Draft, run_playbook
from .validator import validate

log = logging.getLogger("vera.composer")


@dataclass
class Composed:
    body: str
    cta: str
    send_as: str
    suppression_key: str
    rationale: str
    template_name: str
    template_params: list[str]
    on_accept: str = ""
    offer: str = ""
    slots: list[str] = field(default_factory=list)
    lang: str = "en"
    source: str = "playbook"
    issues: list[str] = field(default_factory=list)
    # follow-up material in both registers, so replies can follow the merchant's language per turn
    alt: dict = field(default_factory=dict)

    def public(self) -> dict:
        """The five fields of the §7.1 compose() contract."""
        return {"body": self.body, "cta": self.cta, "send_as": self.send_as,
                "suppression_key": self.suppression_key, "rationale": self.rationale}


POLISH_SYSTEM = """You are Vera, magicpin's WhatsApp assistant for Indian local merchants. You are editing a draft
message that is already factually correct. Make it read like a sharp, warm human colleague wrote it.

Hard rules — breaking any of them makes your output unusable:
1. Do NOT add any number, date, price, name, source, statistic or claim that is not already in the DRAFT or FACTS.
2. Keep every specific fact the draft uses (numbers, sources, dates, offer titles) unless it is redundant.
3. Exactly one call-to-action, and it must be the last sentence. Keep its reply keyword (YES / CONFIRM / 1 or 2).
4. No greetings paragraph, no "hope you're doing well", no hype words, no ALL-CAPS, no emojis beyond what the draft has.
5. Language: {lang_rule}
6. Voice: {voice}. Never use: {taboos}.
7. At most {max_len} characters. Shorter is better if nothing is lost.

Return ONLY JSON: {{"body": "<message>", "rationale": "<one sentence: why this message, why now>"}}"""

LANG_RULE = {
    "en": "English.",
    "hinglish": "Natural Hindi-English code-mix in Roman script (the way Delhi/Mumbai business owners text): English for facts and numbers, Hindi for connective phrases. Do not translate technical terms.",
    "hi": "Simple, respectful Hindi in Roman script; keep medicine names and numbers as-is.",
}


class Composer:
    def __init__(self, llm: Optional[LLM] = None) -> None:
        self.llm = llm
        self._cache: dict[str, Composed] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def draft(self, category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
              now: Optional[datetime] = None) -> tuple[Composed, Ctx]:
        c = Ctx(category, merchant, trigger, customer, now)
        d = run_playbook(c)
        composed = self._wrap(c, d)
        composed.issues = validate(composed.body, c, cta=composed.cta)
        composed.alt[composed.lang] = {"on_accept": d.on_accept, "offer": d.offer}
        other = "en" if composed.lang != "en" else "hinglish"
        try:
            d2 = run_playbook(Ctx(category, merchant, trigger, customer, now, lang_override=other))
            composed.alt[other] = {"on_accept": d2.on_accept, "offer": d2.offer}
        except Exception:
            pass
        if composed.issues:
            log.warning("playbook draft issues for %s/%s: %s", c.kind, c.mid, composed.issues)
        return composed, c

    def compose(self, category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
                now: Optional[datetime] = None, use_llm: bool = True, timeout: Optional[float] = None) -> Composed:
        key = self._key(category, merchant, trigger, customer, now)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        composed, c = self.draft(category, merchant, trigger, customer, now)
        if use_llm and self.llm is not None:
            composed = self.polish(composed, c, timeout=timeout)
        with self._lock:
            self._cache[key] = composed
        return composed

    def polish(self, composed: Composed, c: Ctx, timeout: Optional[float] = None) -> Composed:
        if self.llm is None:
            return composed
        voice = c.category.get("voice") or {}
        max_len = 480 if c.customer else (950 if c.kind == "active_planning_intent" else 700)
        system = POLISH_SYSTEM.format(
            lang_rule=LANG_RULE.get(composed.lang, LANG_RULE["en"]),
            voice=f"{voice.get('tone', '')}, {voice.get('register', '')}".strip(", "),
            taboos=", ".join(c.taboos) or "hype",
            max_len=max_len,
        )
        user = json.dumps({
            "recipient": "customer of the merchant (message sent from the merchant's number)" if c.customer else "merchant owner",
            "trigger_kind": c.kind,
            "facts": facts_digest(c),
            "draft": composed.body,
        }, ensure_ascii=False)
        out = self.llm.complete_json(system, user, max_tokens=600, timeout=timeout)
        if not out or not isinstance(out.get("body"), str):
            return composed
        body = out["body"].strip()
        issues = validate(body, c, cta=composed.cta, max_len=max_len)
        if issues:
            log.info("LLM polish rejected for %s: %s", c.kind, issues)
            return composed
        polished = Composed(**{**asdict(composed), "body": body, "source": f"llm:{self.llm.name}",
                               "template_params": template_params(c, body)})
        if out.get("rationale"):
            polished.rationale = f"{composed.rationale} {out['rationale']}".strip()
        return polished

    # ------------------------------------------------------------------ internals
    def _wrap(self, c: Ctx, d: Draft) -> Composed:
        on_behalf = bool(c.customer) or c.trigger.get("scope") == "customer"
        return Composed(
            body=d.body,
            cta=d.cta,
            send_as="merchant_on_behalf" if on_behalf else "vera",
            suppression_key=c.trigger.get("suppression_key") or f"{c.kind}:{c.mid}:{(c.customer or {}).get('customer_id', '')}",
            rationale=d.rationale + (f" Levers: {', '.join(d.levers)}." if d.levers else ""),
            template_name=f"{'merchant' if on_behalf else 'vera'}_{c.kind}_v1",
            template_params=template_params(c, d.body),
            on_accept=d.on_accept,
            offer=d.offer,
            slots=d.slots,
            lang=c.voice().lang,
        )

    @staticmethod
    def _key(*parts) -> str:
        blob = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def template_params(c: Ctx, body: str) -> list[str]:
    """WhatsApp template parameters {{1}} name, {{2}} context, {{3}} call-to-action."""
    name = c.cust_addressee if c.customer else c.salutation
    lines = [ln for ln in body.split("\n") if ln.strip()]
    cta = lines[-1] if lines else ""
    middle = " ".join(lines[:-1]) if len(lines) > 1 else body
    return [name or c.biz, middle, cta]


def facts_digest(c: Ctx) -> dict:
    """Compact, LLM-friendly view of the facts a message may use."""
    m, p = c.merchant, c.perf
    out = {
        "merchant": {"name": c.biz, "owner": c.salutation, "locality": c.locality, "city": c.city,
                     "verified": c.verified, "active_offers": [o.get("title") for o in c.active_offers],
                     "performance_30d": {k: p.get(k) for k in ("views", "calls", "directions", "ctr", "leads") if k in p},
                     "delta_7d": c.delta, "customers": c.agg, "signals": c.signals,
                     "reviews": m.get("review_themes") or [], "subscription": c.sub,
                     "last_turns": c.history[-3:]},
        "peer_stats": c.peer,
        "trigger": {"kind": c.kind, "payload": c.payload, "urgency": c.urgency},
    }
    item_id = c.payload.get("top_item_id") or c.payload.get("digest_item_id") or c.payload.get("alert_id")
    if item_id:
        out["digest_item"] = c.digest_item(item_id)
    if c.customer:
        out["customer"] = {k: c.customer.get(k) for k in ("identity", "relationship", "state", "preferences")}
    return out
