"""HTTP API for the judge harness (testing brief §2).

    uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .engine import VeraEngine
from .store import SCOPES

logging.basicConfig(level=os.environ.get("VERA_LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("vera.app")

app = FastAPI(title="Vera — magicpin merchant assistant", version=__version__)
engine = VeraEngine()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@app.exception_handler(RequestValidationError)
async def _bad_request(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed", "details": str(exc.errors())[:500]})


# ------------------------------------------------------------------------------ health / metadata

@app.get("/v1/healthz")
def healthz():
    return engine.health()


@app.get("/v1/metadata")
def metadata():
    return {
        "team_name": os.environ.get("VERA_TEAM_NAME", "Vera Reloaded"),
        "team_members": [m.strip() for m in os.environ.get("VERA_TEAM_MEMBERS", "Pranjal Mann").split(",")],
        "model": engine.llm.name if engine.llm else "deterministic-playbooks (no LLM)",
        "approach": ("Fact-ledger grounded playbooks per trigger kind (26 kinds) with judgement rules; optional LLM polish "
                     "re-validated against the ledger; deterministic multi-turn state machine for auto-reply, intent, "
                     "opt-out and off-topic handling"),
        "contact_email": os.environ.get("VERA_CONTACT_EMAIL", "set-VERA_CONTACT_EMAIL@example.com"),
        "version": __version__,
        "submitted_at": os.environ.get("VERA_SUBMITTED_AT", "2026-09-26T00:00:00Z"),
    }


# ------------------------------------------------------------------------------ context

class ContextBody(BaseModel):
    scope: str
    context_id: str = Field(min_length=1)
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
def push_context(body: ContextBody):
    if body.scope not in SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope",
                                                      "details": f"scope must be one of {list(SCOPES)}"})
    status, current = engine.push(body.scope, body.context_id, body.version, body.payload)
    if status == "stale":
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": current})
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": _now_iso(),
            **({"idempotent": True} if status == "duplicate" else {})}


# ------------------------------------------------------------------------------ tick

class TickBody(BaseModel):
    now: Optional[str] = None
    available_triggers: list[str] = []


@app.post("/v1/tick")
def tick(body: TickBody):
    try:
        return {"actions": engine.tick(body.now, body.available_triggers)}
    except Exception:
        log.exception("tick failed")
        return {"actions": []}  # never time out or 500 the judge; restraint beats a malformed action


# ------------------------------------------------------------------------------ reply

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str = ""
    received_at: Optional[str] = None
    turn_number: int = 0


@app.post("/v1/reply")
def reply(body: ReplyBody):
    try:
        return engine.reply(body.model_dump())
    except Exception:
        log.exception("reply failed")
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Internal error while composing; backing off rather than sending something wrong."}


# ------------------------------------------------------------------------------ teardown

@app.post("/v1/teardown")
def teardown():
    engine.teardown()
    return {"ok": True, "wiped_at": _now_iso()}
