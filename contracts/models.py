"""Frozen domain interface — Track A §3.1 / master plan §8.3.

Shapes marked (PS §5.x) are verbatim from the problem statement. Do not
"improve" them: the organisers' sandbox may match them exactly. Fields
marked OUR ADDITION are sandbox-internal and justified in master plan §8.3.

FROZEN AT HOUR 1.5. No change without both track leads agreeing in the
team channel and both re-running `pytest tests/contract/`.
"""

from pydantic import BaseModel
from datetime import datetime, date
from typing import Literal


class Component(BaseModel):                    # PS §5.1
    component_id: str                          # "COMP-104"
    name: str                                  # "Motor Driver IC"
    current_stock: int                         # 420
    usable_stock: int                          # 390  <- ALWAYS reason from this
    daily_usage: int                           # 90
    safety_stock: int                          # 150
    warehouse: str                             # "Pune-Plant-1"
    last_updated: datetime
    required_certifications: list[str] = []    # OUR ADDITION
    min_quality: float = 0.0                   # OUR ADDITION


class Supplier(BaseModel):                     # PS §5.3
    supplier_id: str; supplier_name: str; component_id: str
    unit_price: float; lead_time_days: int; available_quantity: int
    quality_score: float; reliability_score: float
    min_order_quantity: int; certifications: list[str]


class PurchaseOrder(BaseModel):                # PS §5.2
    po_id: str; component_id: str; supplier_id: str
    quantity: int; expected_delivery: date
    status: Literal["in_transit","delayed","delivered","cancelled"]
    unit_price: float; total_value: float
    approval_required_above: float             # 150000


class ProductionOrder(BaseModel):              # PS §5.4
    production_order_id: str; product: str; required_component: str
    units_planned: int; component_required_per_unit: int
    deadline: date; priority: Literal["high","medium","low"]
    max_delay_days: int = 0                    # OUR ADDITION -> solver r[p]


class Quote(BaseModel):                        # PS §5.7
    supplier_id: str; component_id: str
    quantity_available: int; unit_price: float; delivery_days: int
    expedite_available: bool; expedite_fee: float
    quote_valid_hours: int; issued_at: datetime


class Message(BaseModel):                      # PS §5.5 / §5.6
    message_id: str; sender: str; recipient: str
    subject: str; body: str
    related_po_id: str | None = None
    ts: datetime


class TrackingRecord(BaseModel):               # PS §5.10
    po_id: str
    supplier_claim: str                        # "dispatched"
    tracking_status: str                       # "label_created_no_pickup"
    last_movement: datetime | None


class ApprovalResult(BaseModel):               # PS §5.8
    action: str; estimated_cost: float
    approval_required: bool; approval_reason: str | None


# ---- solver interface (Track A implements, Track B calls) ----

class SolverSupplier(BaseModel):
    supplier_id: str; unit_price: float; lead_time_days: int
    available_quantity: int; min_order_quantity: int
    effective_reliability: float; certified: bool; quality_score: float
    quote_expired: bool = False
    claim_contradicted: bool = False


class SolverProdOrder(BaseModel):
    production_order_id: str; units_required: int
    deadline_day: int                          # days from now
    priority_weight: float                     # high 5.0 / medium 2.0 / low 1.0
    max_delay_days: int


class SolverInput(BaseModel):
    component_id: str
    usable_stock: int
    safety_stock: int
    daily_usage: int
    suppliers: list[SolverSupplier]
    production_orders: list[SolverProdOrder]
    budget_cap: float
    approval_limit: float = 150000
    allow_reschedule: bool = True
    allow_partial: bool = False


class Allocation(BaseModel):
    supplier_id: str; units: int; cost: float; arrival_day: int


class Reschedule(BaseModel):
    production_order_id: str; delay_days: int


class SolverOutput(BaseModel):
    status: Literal["OPTIMAL","FEASIBLE","INFEASIBLE"]
    allocations: list[Allocation] = []
    reschedules: list[Reschedule] = []
    total_cost: float = 0.0
    requires_approval: bool = False
    binding_constraint: str | None = None      # set when INFEASIBLE
    relaxation_used: str | None = None         # "none"|"reschedule"|"partial"


# ---- guardrail interface (Track A implements, Track B calls) ----

class Verdict(BaseModel):
    passed: bool
    fired: list[str] = []                      # ["G2","G5"]
    reasons: list[str] = []
    forced_escalation: bool = False
