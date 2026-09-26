#!/usr/bin/env python3
"""Run the provided judge_simulator.py against a running bot WITHOUT an LLM key.

The HTTP flows (warmup, context push, tick, auto-reply / intent / hostile replays,
full evaluation) are exercised for real; the 0-10 dimension scores are a stub
and meaningless. Use it to verify plumbing and replay behaviour; use the real
simulator with an API key for scores.

    python scripts/offline_judge.py [scenario] [bot_url]
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import judge_simulator as js  # noqa: E402


class StubLLM(js.LLMProvider):
    def name(self):
        return "stub (plumbing only — scores are not real)"

    def complete(self, prompt, system=None):
        return json.dumps({k: 0 for k in ("specificity", "category_fit", "merchant_fit", "decision_quality",
                                          "engagement_compulsion")} | {"hint": "stub"})


if __name__ == "__main__":
    scenario = sys.argv[1] if len(sys.argv) > 1 else "all"
    if len(sys.argv) > 2:
        js.BOT_URL = sys.argv[2]
    judge = js.JudgeSimulator(StubLLM())
    ok = judge.run(scenario)
    sys.exit(0 if ok else 1)
