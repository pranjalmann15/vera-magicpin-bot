"""Submission entrypoint (challenge brief §7.1).

    from bot import compose
    compose(category, merchant, trigger, customer)  ->  {body, cta, send_as, suppression_key, rationale}

Also exposes the HTTP server for the judge harness:

    uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

from typing import Optional

from vera.composer import Composed, Composer
from vera.llm import from_env

_composer = Composer(from_env())


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """Deterministic for the same inputs (playbooks are pure; LLM output is cached, temperature 0)."""
    return compose_full(category, merchant, trigger, customer).public()


def compose_full(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> Composed:
    return _composer.compose(category, merchant, trigger, customer)


def __getattr__(name: str):
    # Lazy so `import bot` for compose() does not require FastAPI to be importable.
    if name == "app":
        from vera.app import app
        return app
    raise AttributeError(name)
