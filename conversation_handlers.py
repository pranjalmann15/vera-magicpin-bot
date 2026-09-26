"""Optional multi-turn interface (challenge brief §7.4).

    from conversation_handlers import ConversationState, respond

    state = ConversationState.start(category, merchant, trigger, customer)   # composes the opener
    respond(state, "Ok let's do it")   # -> {"action": "send", "body": ..., "cta": ..., "rationale": ...}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from vera.composer import Composer
from vera.conversation import ConversationManager, ConvState

_composer = Composer()


@dataclass
class ConversationState:
    manager: ConversationManager = field(default_factory=ConversationManager)
    conversation_id: str = "conv_local"
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    opener: str = ""
    turn: int = 1

    @classmethod
    def start(cls, category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> "ConversationState":
        composed, ctx = _composer.draft(category, merchant, trigger, customer)
        st = cls(merchant_id=merchant.get("merchant_id"), customer_id=(customer or {}).get("customer_id"),
                 conversation_id=f"conv_{merchant.get('merchant_id')}_{trigger.get('id')}", opener=composed.body)
        st.manager.open(ConvState(
            conversation_id=st.conversation_id, merchant_id=st.merchant_id, customer_id=st.customer_id,
            trigger_id=trigger.get("id"), kind=ctx.kind, send_as=composed.send_as, lang=composed.lang,
            offer=composed.offer, on_accept=composed.on_accept, slots=composed.slots, alt=composed.alt,
            explain=composed.body.split("\n")[0][:300], owner=ctx.cust_addressee if customer else ctx.salutation,
        ), composed.body)
        return st


def respond(state: ConversationState, merchant_message: str) -> dict:
    state.turn += 1
    return state.manager.handle(state.conversation_id, state.merchant_id, state.customer_id,
                                "customer" if state.customer_id else "merchant", merchant_message, state.turn)
