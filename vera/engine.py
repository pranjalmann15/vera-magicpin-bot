"""VeraEngine — the stateful core behind the HTTP API.

Tick planning rules (why a trigger does or doesn't become a message):
  * resolvable      trigger, merchant and category must be loaded (customer too, for customer-scope)
  * not suppressed  a suppression_key is sent at most once
  * consent         customer-facing sends need recorded opt-in (and reminder opt-in where relevant)
  * merchant state  no sends to merchants who opted out, were hostile, or asked us to wait
  * one per merchant per tick for merchant-facing messages — the rest roll to later ticks
    (the judge keeps re-listing active triggers), so a merchant never gets a burst
  * priority        urgency, deadline proximity, recent engagement, compliance/supply risk
  * budget          compositions run in parallel under a hard deadline; anything the LLM
                    hasn't polished in time ships as its (already valid) playbook draft
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from .composer import Composed, Composer
from .context import Ctx
from .conversation import ConversationManager, ConvState
from .llm import LLM, from_env
from .store import ContextStore
from .textutil import parse_dt

log = logging.getLogger("vera.engine")

HIGH_STAKES = {"regulation_change", "supply_alert", "chronic_refill_due", "renewal_due", "appointment_tomorrow", "perf_dip"}
MAX_ACTIONS = 20


class VeraEngine:
    def __init__(self, llm: Optional[LLM] = "env", tick_budget_s: Optional[float] = None) -> None:
        self.llm = from_env() if llm == "env" else llm
        self.store = ContextStore()
        self.composer = Composer(self.llm)
        self.convs = ConversationManager(self.llm)
        self.sent_keys: set[str] = set()
        self.merchant_last_sent: dict[str, float] = {}
        self.tick_budget_s = tick_budget_s or float(os.environ.get("VERA_TICK_BUDGET_S", "9"))
        self._lock = threading.RLock()
        self._pool = cf.ThreadPoolExecutor(max_workers=8)

    # ------------------------------------------------------------------ context
    def push(self, scope: str, context_id: str, version: int, payload: dict) -> tuple[str, int]:
        return self.store.put(scope, context_id, version, payload)

    def teardown(self) -> None:
        with self._lock:
            self.store.clear()
            self.convs.clear()
            self.sent_keys.clear()
            self.merchant_last_sent.clear()
            self.composer._cache.clear()

    # ------------------------------------------------------------------ tick
    def tick(self, now_iso: Optional[str], available: list[str]) -> list[dict]:
        started = time.time()
        now = parse_dt(now_iso) or datetime.now(timezone.utc)
        candidates = []
        for tid in dict.fromkeys(available or []):
            cand = self._resolve(tid, now)
            if cand:
                candidates.append(cand)
        candidates.sort(key=lambda c: -c["priority"])

        chosen, busy_merchants = [], set()
        for cand in candidates:
            if len(chosen) >= MAX_ACTIONS:
                break
            if cand["customer"] is None:
                if cand["merchant_id"] in busy_merchants:
                    continue
                busy_merchants.add(cand["merchant_id"])
            chosen.append(cand)

        # Playbook drafts are instant; LLM polish (if any) runs in parallel under the budget.
        drafts = {}
        for cand in chosen:
            composed, ctx = self.composer.draft(cand["category"], cand["merchant"], cand["trigger"], cand["customer"], now)
            drafts[cand["trigger_id"]] = (composed, ctx)
        if self.llm is not None and drafts:
            remaining = max(1.0, self.tick_budget_s - (time.time() - started))
            futures = {self._pool.submit(self.composer.polish, comp, ctx, remaining): tid for tid, (comp, ctx) in drafts.items()}
            done, _ = cf.wait(futures, timeout=remaining)
            for f in done:
                try:
                    drafts[futures[f]] = (f.result(), drafts[futures[f]][1])
                except Exception as e:  # keep the draft
                    log.warning("polish failed: %s", e)

        actions = []
        with self._lock:
            for cand in chosen:
                composed, ctx = drafts[cand["trigger_id"]]
                if composed.suppression_key in self.sent_keys:
                    continue
                conv_id = self._conv_id(cand)
                self.convs.open(self._state_from(conv_id, cand, composed, ctx), composed.body)
                self.sent_keys.add(composed.suppression_key)
                self.merchant_last_sent[cand["merchant_id"]] = time.time()
                actions.append({
                    "conversation_id": conv_id,
                    "merchant_id": cand["merchant_id"],
                    "customer_id": cand["customer_id"],
                    "send_as": composed.send_as,
                    "trigger_id": cand["trigger_id"],
                    "template_name": composed.template_name,
                    "template_params": composed.template_params,
                    "body": composed.body,
                    "cta": composed.cta,
                    "suppression_key": composed.suppression_key,
                    "rationale": composed.rationale,
                })
        return actions

    def _resolve(self, tid: str, now: datetime) -> Optional[dict]:
        trg = self.store.get("trigger", tid)
        if not trg:
            return None
        mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
        merchant = self.store.get("merchant", mid)
        if not merchant:
            return None
        category = self.store.get("category", merchant.get("category_slug"))
        if not category:
            return None
        cid = trg.get("customer_id")
        customer = self.store.get("customer", cid) if cid else None
        if trg.get("scope") == "customer" and not customer:
            return None
        key = trg.get("suppression_key") or tid
        if key in self.sent_keys:
            return None
        if self.convs.is_blocked(mid):
            return None
        ctx = Ctx(category, merchant, trg, customer, now)
        if customer and not ctx.consent_ok():
            log.info("skip %s: no consent for customer outreach", tid)
            return None
        return {"trigger_id": tid, "trigger": trg, "merchant_id": mid, "merchant": merchant, "category": category,
                "customer_id": cid if customer else None, "customer": customer,
                "priority": self._priority(ctx)}

    def _priority(self, c: Ctx) -> float:
        p = c.urgency * 10.0
        if c.kind in HIGH_STAKES:
            p += 5
        exp = parse_dt(c.trigger.get("expires_at"))
        if exp:
            hours = (exp - c.ref_now).total_seconds() / 3600
            if 0 <= hours <= 72:
                p += 8
        if c.last_merchant_msg and c.last_merchant_msg.get("engagement", "").startswith("intent"):
            p += 6  # merchant is mid-thread: keep momentum
        if c.customer:
            p += 2
        if c.is_placeholder:
            p -= 3  # grounded, specific triggers first
        last = self.merchant_last_sent.get(c.mid)
        if last and time.time() - last < 3600:
            p -= 15  # we just messaged this merchant
        return p

    @staticmethod
    def _conv_id(cand: dict) -> str:
        m = re.sub(r"^m_\d+_", "", cand["merchant_id"])[:24]
        kind = cand["trigger"].get("kind", "msg")
        cust = f"_{re.sub(r'^c_\d+_', '', cand['customer_id'])[:12]}" if cand["customer_id"] else ""
        n = re.search(r"trg_(\d+)", cand["trigger_id"])
        return f"conv_{m}_{kind}{cust}_{n.group(1) if n else abs(hash(cand['trigger_id'])) % 10000}"

    def _state_from(self, conv_id: str, cand: dict, composed: Composed, ctx: Ctx) -> ConvState:
        return ConvState(
            conversation_id=conv_id, merchant_id=cand["merchant_id"], customer_id=cand["customer_id"],
            trigger_id=cand["trigger_id"], kind=ctx.kind, send_as=composed.send_as, lang=composed.lang,
            offer=composed.offer, on_accept=composed.on_accept, slots=composed.slots, alt=composed.alt,
            explain=composed.body.split("\n")[0][:300], owner=ctx.cust_addressee if ctx.customer else ctx.salutation,
        )

    # ------------------------------------------------------------------ reply
    def reply(self, body: dict) -> dict:
        return self.convs.handle(
            conversation_id=body.get("conversation_id") or f"conv_adhoc_{body.get('merchant_id')}",
            merchant_id=body.get("merchant_id"), customer_id=body.get("customer_id"),
            from_role=body.get("from_role") or "merchant", message=body.get("message") or "",
            turn_number=int(body.get("turn_number") or 0), fallback_state=self._state_for_unknown,
        )

    def _state_for_unknown(self, conv_id: str, merchant_id: Optional[str], customer_id: Optional[str]) -> ConvState:
        """A reply for a conversation we never opened (e.g. the judge's replay scenarios).
        Rebuild the most useful open offer for this merchant so a 'yes' still gets real work."""
        merchant = self.store.get("merchant", merchant_id) if merchant_id else None
        if not merchant:
            return ConvState(conv_id, merchant_id, customer_id)
        category = self.store.get("category", merchant.get("category_slug")) or {}
        triggers = [t for t in self.store.all("trigger") if t and t.get("merchant_id") == merchant_id and not t.get("customer_id")]
        trigger = max(triggers, key=lambda t: t.get("urgency", 0)) if triggers else {
            "id": "synthetic", "kind": _best_kind(merchant), "scope": "merchant", "payload": {}, "urgency": 2,
            "merchant_id": merchant_id}
        composed, ctx = self.composer.draft(category, merchant, trigger, None, None)
        return ConvState(conv_id, merchant_id, customer_id, trigger_id=trigger.get("id"), kind=ctx.kind,
                         lang=composed.lang, offer=composed.offer, on_accept=composed.on_accept, alt=composed.alt,
                         explain=composed.body.split("\n")[0][:300], owner=ctx.salutation)

    # ------------------------------------------------------------------ health
    def health(self) -> dict:
        return {"status": "ok", "uptime_seconds": int(time.time() - self.store.started),
                "contexts_loaded": self.store.counts()}


def _best_kind(merchant: dict) -> str:
    signals = " ".join(map(str, merchant.get("signals") or []))
    ident = merchant.get("identity") or {}
    delta = (merchant.get("performance") or {}).get("delta_7d") or {}
    if ident.get("verified") is False or "unverified" in signals:
        return "gbp_unverified"
    if (merchant.get("subscription") or {}).get("status") == "expired":
        return "winback_eligible"
    if any(isinstance(v, (int, float)) and v <= -0.15 for v in delta.values()):
        return "perf_dip"
    return "milestone_reached"
