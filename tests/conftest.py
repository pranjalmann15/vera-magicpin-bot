import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["VERA_LLM_PROVIDER"] = "none"  # tests exercise the deterministic path
EXPANDED = ROOT / "dataset" / "expanded"


def _load(name: str, key: str) -> dict:
    out = {}
    for f in sorted((EXPANDED / name).glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        out[d.get(key, f.stem)] = d
    return out


@pytest.fixture(scope="session")
def data():
    if not (EXPANDED / "test_pairs.json").exists():
        subprocess.run([sys.executable, str(ROOT / "dataset" / "generate_dataset.py"), "--seed-dir",
                        str(ROOT / "dataset"), "--out", str(EXPANDED)], check=True, env={**os.environ, "PYTHONUTF8": "1"})
    return {
        "categories": _load("categories", "slug"),
        "merchants": _load("merchants", "merchant_id"),
        "customers": _load("customers", "customer_id"),
        "triggers": _load("triggers", "id"),
        "pairs": json.loads((EXPANDED / "test_pairs.json").read_text(encoding="utf-8"))["pairs"],
    }


def bundle(data, trigger_id):
    t = data["triggers"][trigger_id]
    m = data["merchants"][t["merchant_id"]]
    c = data["categories"][m["category_slug"]]
    cust = data["customers"].get(t.get("customer_id")) if t.get("customer_id") else None
    return c, m, t, cust
