# Vera, rebuilt: a grounded merchant assistant

**Approach.** Every message comes from a **playbook**: a strategy for one trigger kind. That covers all 26 kinds in the dataset, plus the brief's `weather_heatwave`, `local_news_event` and `category_trend_movement`. For any kind it has never seen, a fallback surfaces the trigger's own payload (headline, numbers, linked digest item) as the reason to message now. A playbook reads the four contexts through one resolved object (`vera/context.py`) and picks the **one signal** that should drive the message. Secondary facts are held back for the follow-up, and the rationale says what was chosen and what was left out. It then writes one message with one ask, ending on the CTA. Every number found in the contexts, and every number a playbook derives, goes into a **fact ledger**. A validator rejects any body containing a number not in that ledger. It also rejects taboo words, preambles, a missing final CTA, more than one CTA, ALL-CAPS and over-long messages. Result: all 100 dataset triggers compose with **zero** unsupported numbers. An LLM is optional. When one is configured it only *polishes* the playbook draft, and the polished text goes back through the same validator: if it adds a fact, the draft ships instead.

**Judgement, not templating.** Examples from the 30 test pairs:
- **IPL match on a weekend:** recommends delivery, not dine-in, because the category digest shows weekend matches cut covers by ~12%. It also notes the merchant's BOGO only runs Tue–Thu and flags their late-delivery reviews.
- **Competitor at ₹199:** advises *not* matching the price. The merchant's reviews show they win on trust; the bot closes the weak spot instead (wait time, 3 reviews).
- **Refill for a senior patient:** cross-checks atorvastatin against this month's recall alert before dispatch.
- **Customer recall slots:** weekdays are computed from the ISO timestamps. The payload's labels were wrong ("Wed 5 Nov" is a Thursday).
- **`perf_dip` when the numbers are actually up:** says so honestly and names the real risk (the plan lapsed 39 days ago).
- **Placeholder triggers (75 of the 100 generated):** the bot never invents a competitor, a festival date or a milestone. It derives "why now" from the merchant's strongest or weakest metric against peer stats.

**Conversations** (`vera/conversation.py`) run on a deterministic state machine:
- **Auto-reply:** canned-phrase or verbatim-repeat detection, tracked *per merchant* across conversation IDs. The bot sends one owner-flag nudge, then waits 24h, then ends.
- **Commitment** ("let's do it", "haan kar do", "judna hai"): switches to **action** at once and sends the deliverable the playbook prepared, never another qualifying question.
- **Opt-out:** ends the conversation and suppresses the merchant.
- **Hostility:** one apology with an opt-out path, then ends.
- **Off-topic** (GST, loans): one-line decline, then back to the open offer.
- **Customer booking:** slot picks confirmed exactly as offered.
- **Language:** follows the merchant's language each turn. Deliverables are pre-rendered in English and Hinglish.

**Tick planning** (`vera/engine.py`): the bot skips a trigger when it is suppressed, lacks customer consent, or targets a blocked merchant. It sends at most one merchant-facing message per merchant per tick and ranks triggers by urgency, deadline and engagement. Playbook drafts are instant; LLM polish runs in parallel within a 9s budget.

## Run
```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080          # or: docker build -t vera . && docker run -p 8080:8080 vera
python generate_submission.py                       # -> submission.jsonl (30 test pairs)
python -m pytest tests -q                           # 48 tests: compose, grounding, fresh-scenario injection, conversations, API
python scripts/offline_judge.py all http://127.0.0.1:8080   # judge_simulator flows without an API key
```
Optional LLM: set `VERA_LLM_PROVIDER` (anthropic | openai | gemini | groq | deepseek | openrouter | ollama), the provider's API key, and optionally `VERA_LLM_MODEL`. On Windows, use `127.0.0.1`, not `localhost`: `localhost` resolution alone adds seconds.

## Tradeoffs
- Playbooks over free generation: less stylistic range, but zero fabrication and deterministic output with no API dependency.
- One merchant-facing message per merchant per tick favours restraint. Deferred triggers go out on later ticks.
- Store merges partial pushes with earlier versions, so a digest-only category update doesn't wipe the offer catalog.
- `POST /v1/context` returns 200 plus `idempotent: true` for an identical re-push, and 409 for older or conflicting versions.

## What would have helped most
Real payloads for the 75 placeholder triggers. Booking slots on appointment triggers. Review counts and ratings on the merchant, since milestones and competitor comparisons need them. A customer→trigger consent-scope mapping.
