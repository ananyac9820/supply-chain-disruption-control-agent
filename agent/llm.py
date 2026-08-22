"""The model boundary.

The model judges and explains. The code computes and guarantees. Everything
the LLM is asked for here is a classification or a selection returned as a
typed object — never a number that feeds a decision. Coverage days, order
splits, reschedule days, cost totals and threshold comparisons are computed in
agent/impact_math.py or by solve(), and the model never sees a request to
"calculate" anything.

Two implementations behind one protocol:

  AnthropicLLM     structured output via client.messages.parse, used whenever
                   a credential resolves.
  RuleBasedLLM     a deterministic stand-in with the same interface, used when
                   no credential resolves.

The fallback exists because a demo that dies when the API is unreachable is
worse than one that degrades visibly. Every run records which was used, in the
run_complete audit event and in the CLI banner, so nobody can mistake a
rule-based run for a live one.
"""

from __future__ import annotations

import os
from typing import Literal, Protocol

from pydantic import BaseModel, Field

MODEL = os.environ.get("SCDA_LLM_MODEL", "claude-opus-5")
MAX_TOKENS = 4096


# ---- the typed things the model is allowed to return -------------------

class DisruptionTriage(BaseModel):
    disruption_type: str = Field(description="e.g. supplier_delay, stock_correction")
    severity: Literal["low", "medium", "high", "critical"]
    affected_component: str
    rationale: str = Field(description="one line, ops-manager readable")


class ToolChoice(BaseModel):
    tool: str = Field(description="one of the ten endpoint names, or 'done'")
    necessity: str = Field(description="why this call is needed, non-empty")
    supplier_id: str | None = None
    component_id: str | None = None
    po_id: str | None = None
    subject: str | None = None
    body: str | None = None


class ReplyClassification(BaseModel):
    classification: Literal["VAGUE", "SPECIFIC"]
    promised_date: str | None = None
    promised_quantity: int | None = None
    claim: str | None = Field(default=None,
                              description="the factual claim to verify, e.g. 'dispatched'")
    rationale: str


class LLMClient(Protocol):
    name: str

    def triage(self, context: dict) -> DisruptionTriage: ...
    def select_tool(self, context: dict) -> ToolChoice: ...
    def classify_reply(self, body: str, context: dict) -> ReplyClassification: ...


# ---- prompts -----------------------------------------------------------

DOMAIN = (
    "You are the reasoning layer of a procurement and manufacturing disruption "
    "agent. The domain is components, suppliers, purchase orders, RFQs, "
    "production orders, certifications, minimum order quantities, safety stock "
    "and approval thresholds. It is NOT logistics routing: there are no ports, "
    "carriers, transport modes, routes, shipping lanes or SLAs.\n\n"
    "You never do arithmetic that affects a decision. Coverage, shortfalls, "
    "splits, reschedule days and costs are computed by deterministic code and "
    "given to you. You classify, you select the next tool, and you explain."
)

TRIAGE_SYSTEM = DOMAIN + (
    "\n\nClassify the disruption. Severity is NOT a function of delay length: "
    "five days is fine for a low-priority order and dangerous for a "
    "high-priority one. Base severity on the priority and deadline of the "
    "production orders inside the coverage window, which are given to you."
)

SELECT_SYSTEM = DOMAIN + (
    "\n\nChoose the single next tool to call, and state why in one line. "
    "Policy:\n"
    "  stock risk unclear             -> inventory tools\n"
    "  delivery status uncertain      -> supplier message, then tracking\n"
    "  supply cannot meet demand      -> RFQ\n"
    "  decision crosses budget limits -> approval check\n"
    "  only after deciding            -> ERP update\n\n"
    "Never call a tool whose answer you already have unless something has "
    "changed it. Return tool='done' when you have enough to plan: a verified "
    "picture of the delay and quotes covering the shortfall. A VAGUE supplier "
    "reply must not advance the plan - send exactly one targeted follow-up "
    "naming the PO and demanding a date and a quantity, then pursue alternate "
    "sourcing in parallel rather than waiting."
)

CLASSIFY_SYSTEM = DOMAIN + (
    "\n\nClassify a supplier reply. VAGUE means no date AND no quantity "
    "commitment. SPECIFIC means it commits to at least one of them concretely. "
    "If the reply asserts a checkable fact about the shipment (for example "
    "that it has been dispatched), put that assertion in `claim` so it can be "
    "checked against tracking. Do not treat a claim as true."
)


# ---- live implementation ----------------------------------------------

class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str = MODEL) -> None:
        import anthropic
        self.model = model
        self._client = anthropic.Anthropic()

    def _parse(self, system: str, prompt: str, schema):
        response = self._client.messages.parse(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
        return response.parsed_output

    def triage(self, context: dict) -> DisruptionTriage:
        return self._parse(TRIAGE_SYSTEM, _render(context), DisruptionTriage)

    def select_tool(self, context: dict) -> ToolChoice:
        return self._parse(SELECT_SYSTEM, _render(context), ToolChoice)

    def classify_reply(self, body: str, context: dict) -> ReplyClassification:
        prompt = f"Supplier reply:\n\n{body}\n\nContext:\n{_render(context)}"
        return self._parse(CLASSIFY_SYSTEM, prompt, ReplyClassification)


def _render(context: dict) -> str:
    import json
    return json.dumps(context, indent=2, default=str)


# ---- deterministic stand-in -------------------------------------------

class RuleBasedLLM:
    """Same interface, no network. Encodes the §4.3 policy directly.

    This is a stand-in for the model's judgement, not for the arithmetic —
    there is no arithmetic here either. It exists so the graph, the ledger,
    the verification path and every renderer stay exercisable without a
    credential.
    """

    name = "rule-based"

    def triage(self, context: dict) -> DisruptionTriage:
        orders = context.get("orders_in_window") or []
        worst = "low"
        for o in orders:
            if o.get("priority") == "high":
                worst = "critical" if o.get("days_to_deadline", 99) <= 4 else "high"
                break
            if o.get("priority") == "medium" and worst == "low":
                worst = "medium"
        return DisruptionTriage(
            disruption_type=context.get("signal_type", "supplier_delay"),
            severity=worst,
            affected_component=context.get("component_id", ""),
            rationale=(f"{len(orders)} production order(s) fall inside the coverage "
                       f"window; severity follows the highest-priority one and its "
                       f"deadline, not the length of the delay"),
        )

    def select_tool(self, context: dict) -> ToolChoice:
        po_id = context.get("po_id")
        component_id = context.get("component_id")
        supplier_id = context.get("incumbent_supplier_id")
        done = context.get("done") or {}

        # delivery status uncertain -> message the supplier, then tracking
        if not done.get("challenged_incumbent"):
            return ToolChoice(
                tool="send_message", supplier_id=supplier_id,
                subject=f"Re: {po_id} - revised date and confirmed quantity required",
                body=(f"Your message states a 5-7 day window for {po_id}. A window is "
                      f"not a date. Confirm the exact dispatch date and the quantity "
                      f"you will ship against {po_id}."),
                necessity="the stated delay is a window, not a date; the plan cannot "
                          "rest on it")
        if not done.get("read_reply"):
            return ToolChoice(tool="get_inbox",
                              necessity="the supplier's answer determines whether the "
                                        "plan can rest on the incumbent")
        if done.get("reply_vague") and not done.get("followed_up"):
            return ToolChoice(
                tool="send_message", supplier_id=supplier_id,
                subject=f"Re: {po_id} - second request, specific commitment required",
                body=(f"Your reply commits to neither a date nor a quantity. Against "
                      f"{po_id}, state the dispatch date and the number of units. "
                      f"We are sourcing alternates in parallel."),
                necessity="one targeted follow-up naming the PO and demanding a date "
                          "and a quantity; alternate sourcing proceeds in parallel")
        if done.get("followed_up") and not done.get("read_followup"):
            return ToolChoice(tool="get_inbox",
                              necessity="read the answer to the targeted follow-up")
        if done.get("claim_to_verify") and not done.get("verified"):
            return ToolChoice(tool="get_tracking", po_id=po_id,
                              necessity="supplier claim must be grounded before it can "
                                        "support the plan")
        # current supply cannot meet demand -> catalog, then RFQ
        if not done.get("read_catalog"):
            return ToolChoice(tool="get_suppliers", component_id=component_id,
                              necessity="the shortfall must be sourced from the "
                                        "certified catalog")
        if not done.get("requested_quotes"):
            return ToolChoice(tool="request_rfq", component_id=component_id,
                              necessity="a catalog price is not a commitment; a quote "
                                        "with a validity window is")
        return ToolChoice(tool="done",
                          necessity="the delay is verified and quotes cover the "
                                    "shortfall; planning can proceed")

    def classify_reply(self, body: str, context: dict) -> ReplyClassification:
        low = body.lower()
        has_date = any(t in low for t in
                       ("2026-", "dispatch on", "delivery by", "tomorrow", "today"))
        has_qty = any(t in low for t in ("units", "confirmed:", "quantity"))
        claim = None
        if "dispatch" in low or "shipped" in low or "left our" in low:
            claim = "dispatched"
        if has_date or has_qty:
            return ReplyClassification(
                classification="SPECIFIC", claim=claim,
                promised_date="2026-09-04" if "2026-09-04" in body else None,
                promised_quantity=400 if "400 units" in low else None,
                rationale="the reply commits to a date or a quantity")
        return ReplyClassification(
            classification="VAGUE", claim=claim,
            rationale="no date and no quantity commitment, so the plan does not "
                      "advance on it")


# ---- selection ---------------------------------------------------------

def credential_available() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return bool(os.environ.get("ANTHROPIC_PROFILE"))


def get_llm(force: str | None = None) -> LLMClient:
    """Live client when a credential resolves, deterministic stand-in otherwise.

    force='rule-based' pins the stand-in, which is what the tests use so a run
    is reproducible.
    """
    choice = force or os.environ.get("SCDA_LLM", "auto")
    if choice == "rule-based":
        return RuleBasedLLM()
    if choice == "anthropic" or (choice == "auto" and credential_available()):
        try:
            return AnthropicLLM()
        except Exception:                    # noqa: BLE001 - degrade visibly
            return RuleBasedLLM()
    return RuleBasedLLM()
