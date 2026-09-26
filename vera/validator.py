"""Post-composition checks. Every outbound body passes through here.

Used two ways:
  * as a hard gate on LLM output (any issue → repair or fall back to the playbook draft)
  * as a regression guard on playbook drafts in the test-suite
"""

from __future__ import annotations

import re
from typing import Optional

from .context import Ctx

GLOBAL_TABOO = [
    "guaranteed", "guarantee", "100% safe", "miracle", "best in city", "best in town",
    "completely cure", "instant results", "amazing deal", "hurry", "act now",
    "limited time only", "once in a lifetime",
]
HEALTH_TABOO = [r"\bcures?\b"]
PREAMBLES = [
    "hope you are doing well", "hope you're doing well", "i hope this message finds you",
    "i am reaching out", "i'm reaching out", "i wanted to reach out", "greetings of the day",
]
REINTRO = [r"\bvera (from magicpin )?here\b", r"\bi am vera\b", r"\bi'm vera\b", r"\bmain vera\b", r"\bthis is vera\b"]
ALLOWED_CAPS = {"YES", "STOP", "CONFIRM", "RENEW", "HAAN", "GBP", "CTR", "OTC", "RCT", "OPG", "IOPA", "RVG", "DCI",
                "IDA", "JIDA", "CDSCO", "FDA", "GST", "PT", "HIIT", "ORS", "SPF", "LDL", "CDE", "WA", "YOY", "AOV",
                "PDF", "CAD", "CAM", "DC", "MI", "IPL", "ICMR", "DGCI", "SOP", "FSSAI", "BP", "PFM", "MRP", "NO"}

_NUM = re.compile(r"(?<![\w])(\d[\d,]*(?:\.\d+)?)")
_MIN = re.compile(r"(\d+)\s*[-‑]?\s*(?:min|mins|minute|minutes|sec|seconds|hour|hours|hr|hrs)\b", re.I)


def ungrounded_numbers(body: str, c: Ctx) -> list[str]:
    effort = {m.start(1) for m in _MIN.finditer(body) if int(m.group(1)) <= 60}
    bad = []
    for m in _NUM.finditer(body):
        tok = m.group(1).rstrip(",.")
        if m.start(1) in effort:
            continue
        try:
            f = float(tok.replace(",", ""))
        except ValueError:
            continue
        if f <= 3 or 2024 <= f <= 2027:
            continue
        if not c.has_number(f):
            bad.append(tok)
    return bad


def validate(body: str, c: Ctx, *, cta: str = "binary_yes_no", is_reply: bool = False,
             max_len: Optional[int] = None) -> list[str]:
    issues: list[str] = []
    text = (body or "").strip()
    low = text.lower()
    if not text:
        return ["empty_body"]

    for phrase in GLOBAL_TABOO + [re.sub(r"\s*\(.*?\)", "", t).lower() for t in c.taboos]:
        if phrase and phrase in low:
            issues.append(f"taboo:{phrase}")
    if c.slug in ("dentists", "pharmacies", "gyms"):
        for pat in HEALTH_TABOO:
            if re.search(pat, low):
                issues.append("taboo:cure")

    bad_nums = ungrounded_numbers(text, c)
    if bad_nums:
        issues.append("ungrounded_numbers:" + ",".join(sorted(set(bad_nums))))

    if re.search(r"https?://|www\.", low) and not any(u in str(c.merchant) for u in re.findall(r"https?://\S+", text)):
        issues.append("unapproved_url")

    for p in PREAMBLES:
        if p in low:
            issues.append(f"preamble:{p}")
    if is_reply:
        for p in REINTRO:
            if re.search(p, low):
                issues.append("reintroduction")

    if text.count("?") > 2:
        issues.append("too_many_questions")
    replies = re.findall(r"\breply\s+([A-Z0-9]+)", text)
    if len({r for r in replies if r not in ("STOP",)}) > 2:
        issues.append("multiple_ctas")
    if cta not in ("none", "") and not is_reply:
        tail = text.strip().split("\n")[-1].lower()
        if not ("?" in tail or "reply" in tail or "bhej" in tail or "likhiye" in tail or "confirm" in tail):
            issues.append("cta_not_last")

    context_caps = set(re.findall(r"\b[A-Z]{3,}\b", f"{c.merchant} {c.category} {c.trigger} {c.customer or ''}"))
    shout = [w for w in re.findall(r"\b[A-Z]{4,}\b", text) if w not in ALLOWED_CAPS and w not in context_caps]
    if shout or "!!" in text:
        issues.append("shouting")

    limit = max_len or (480 if c.customer else 750)
    if c.kind == "active_planning_intent":
        limit = max(limit, 950)
    if len(text) > limit:
        issues.append(f"too_long:{len(text)}>{limit}")
    return issues
