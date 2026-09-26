#!/usr/bin/env python3
"""Score the 30 canonical test pairs with the official judge prompt (judge_simulator.LLMScorer).

    $env:JUDGE_PROVIDER="gemini"; $env:JUDGE_API_KEY="..."; $env:JUDGE_MODEL="gemini-2.5-flash"
    python scripts/score_pairs.py [--all] [--out results.json]

The key is read from the environment only — never put it in a file in this repo.
Uses the same scoring prompt and parsing as judge_simulator.py, but calls compose()
directly on the expanded dataset (all 30 pairs, including customer-facing ones).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import judge_simulator as js  # noqa: E402
from generate_submission import ensure_expanded, load_dir, EXPANDED  # noqa: E402
from bot import compose_full  # noqa: E402

DIMS = ["specificity", "category_fit", "merchant_fit", "decision_quality", "engagement_compulsion"]


def make_llm():
    provider = os.environ.get("JUDGE_PROVIDER", "gemini")
    key = os.environ.get("JUDGE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    model = os.environ.get("JUDGE_MODEL", "gemini-2.5-flash")
    cls = {"gemini": js.GeminiProvider, "openai": js.OpenAIProvider, "anthropic": js.AnthropicProvider,
           "groq": js.GroqProvider, "deepseek": js.DeepSeekProvider, "openrouter": js.OpenRouterProvider}[provider]
    return cls(key, model)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="score all 100 triggers instead of the 30 pairs")
    ap.add_argument("--out", default=None)
    ap.add_argument("--workers", type=int, default=int(os.environ.get("JUDGE_WORKERS", "6")))
    args = ap.parse_args()

    ensure_expanded()
    cats, ms = load_dir("categories", "slug"), load_dir("merchants", "merchant_id")
    cs, ts = load_dir("customers", "customer_id"), load_dir("triggers", "id")
    pairs = json.loads((EXPANDED / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]
    if args.all:
        pairs = [{"test_id": t, "trigger_id": t, "merchant_id": v["merchant_id"], "customer_id": v.get("customer_id")} for t, v in ts.items()]

    llm = make_llm()
    scorer = js.LLMScorer(llm, None)
    js.print_llm = lambda *_: None  # silence per-call chatter
    js.print_warn = lambda *_: None
    print(f"judge: {llm.name()} | pairs: {len(pairs)} | workers: {args.workers}\n", flush=True)

    def score_one(p):
        t, m = ts[p["trigger_id"]], ms[p["merchant_id"]]
        cust = cs.get(p["customer_id"]) if p.get("customer_id") else None
        cat = cats[m["category_slug"]]
        c = compose_full(cat, m, t, cust)
        action = {"body": c.body, "cta": c.cta, "send_as": c.send_as}
        for attempt in range(6):
            s = scorer.score(action, cat, m, t, cust)
            if s.hint != "LLM scoring failed - using basic heuristics":
                break
            time.sleep(5 * (attempt + 1))  # 503 overload / rate limit: back off and retry
        row = {"test_id": p["test_id"], "kind": t["kind"], "merchant": m["identity"]["name"], "total": s.total,
               **{d: getattr(s, d) for d in DIMS},
               "reasons": {"specificity": s.specificity_reason, "category_fit": s.category_fit_reason,
                           "merchant_fit": s.merchant_fit_reason, "decision_quality": s.decision_quality_reason,
                           "engagement_compulsion": s.engagement_reason},
               "hint": s.hint, "body": c.body, "source": c.source,
               "fallback": s.hint == "LLM scoring failed - using basic heuristics"}
        dims = " ".join(f"{d[:4]}={getattr(s, d):2}" for d in DIMS)
        print(f"{p['test_id']:>4} {t['kind'][:24]:24} {row['total']:2}/50  {dims}{'  (FALLBACK)' if row['fallback'] else ''}", flush=True)
        return row

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(score_one, pairs))

    ok = [r for r in rows if not r["fallback"]]
    print(f"\nscored by LLM: {len(ok)}/{len(rows)}")
    if ok:
        print("AVERAGE  " + f"{sum(r['total'] for r in ok) / len(ok):.1f}/50  " +
              " ".join(f"{d[:4]}={sum(r[d] for r in ok) / len(ok):.1f}" for d in DIMS))
    out = args.out or str(Path(os.environ.get("TEMP", ".")) / "vera_scores.json")
    Path(out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"details -> {out}")


if __name__ == "__main__":
    main()
