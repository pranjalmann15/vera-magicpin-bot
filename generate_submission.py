#!/usr/bin/env python3
"""Build submission.jsonl for the 30 canonical test pairs.

    python generate_submission.py                 # writes submission.jsonl
    python generate_submission.py --print         # also prints every message for review
    python generate_submission.py --all --print   # compose for all 100 triggers (review only)

Expands the dataset first if dataset/expanded/ does not exist yet.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
EXPANDED = ROOT / "dataset" / "expanded"

sys.path.insert(0, str(ROOT))
from bot import compose_full  # noqa: E402


def ensure_expanded() -> None:
    if (EXPANDED / "test_pairs.json").exists():
        return
    env = {**os.environ, "PYTHONUTF8": "1"}
    subprocess.run([sys.executable, str(ROOT / "dataset" / "generate_dataset.py"),
                    "--seed-dir", str(ROOT / "dataset"), "--out", str(EXPANDED)], check=True, env=env)


def load_dir(name: str, key: str) -> dict:
    out = {}
    for f in sorted((EXPANDED / name).glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        out[d.get(key, f.stem)] = d
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "submission.jsonl"))
    ap.add_argument("--print", action="store_true", dest="show")
    ap.add_argument("--all", action="store_true", help="compose for every trigger (review only, no file)")
    args = ap.parse_args()

    ensure_expanded()
    categories = load_dir("categories", "slug")
    merchants = load_dir("merchants", "merchant_id")
    customers = load_dir("customers", "customer_id")
    triggers = load_dir("triggers", "id")
    pairs = json.loads((EXPANDED / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]
    if args.all:
        pairs = [{"test_id": t, "trigger_id": t, "merchant_id": tr["merchant_id"], "customer_id": tr.get("customer_id")}
                 for t, tr in triggers.items()]

    rows, flagged = [], 0
    for p in pairs:
        trg = triggers[p["trigger_id"]]
        m = merchants[p["merchant_id"]]
        cat = categories[m["category_slug"]]
        cust = customers.get(p.get("customer_id")) if p.get("customer_id") else None
        c = compose_full(cat, m, trg, cust)
        row = {"test_id": p["test_id"], **c.public(), "template_name": c.template_name,
               "template_params": c.template_params}
        rows.append(row)
        if c.issues:
            flagged += 1
        if args.show:
            print(f"\n=== {p['test_id']} | {trg['kind']} | {m['identity']['name']} ({m['identity']['city']})"
                  f"{' -> ' + cust['identity']['name'] if cust else ''} | lang={c.lang} cta={c.cta} src={c.source}")
            print(c.body)
            if c.issues:
                print("!! ISSUES:", c.issues)

    if not args.all:
        with open(args.out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\nwrote {len(rows)} rows -> {args.out}")
    print(f"validator flags: {flagged}/{len(rows)}")


if __name__ == "__main__":
    main()
