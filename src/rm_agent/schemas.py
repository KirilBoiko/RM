from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

ActionType = Literal["HOLD", "CLOSE_RBD_UP", "OPEN_RBD_DOWN", "ADJUST_FARE"]
PriorityLevel = Literal["HIGH", "MEDIUM", "LOW"]
DecisionStatus = Literal["PENDING", "APPROVED", "MODIFIED", "REJECTED"]


class RBDProtectionRow(BaseModel):
    rbd: str
    tier_index: int
    label: str
    lite_usd: float
    stand_usd: float
    protected_seats: int
    recommended_open: bool


class CompetitorSummary(BaseModel):
    exact_date_nonstop_min_usd: Optional[float] = None
    exact_date_nonstop_airline: Optional[str] = None
    window_pm2d_nonstop_min_usd: Optional[float] = None
    window_pm2d_nonstop_airline: Optional[str] = None
    transfer_min_usd: Optional[float] = None
    recent_competitor_move_pct: Optional[float] = None
    offers: list[dict] = Field(default_factory=list)


class GuardrailEvaluation(BaseModel):
    passed: bool
    min_allowed_lite_usd: float
    min_allowed_rbd: str
    warnings: list[str] = Field(default_factory=list)
    clamped_rbd: Optional[str] = None
    clamped_fare_lite_usd: Optional[float] = None


class FlightDossier(BaseModel):
    flight_id: str
    flight: str
    route: str
    departure_date: date
    weekday: str
    current_depth: int

    # Capacity & Blocks
    physical_capacity: int
    blocked_seats: int
    net_sellable_capacity: int
    on_hand_freesale_pax: int
    remaining_freesale_seats: int
    click_lite_seats: int = 0
    click_stand_seats: int = 0

    # Load Factor & Curve Pace
    freesale_lf_pct: float
    total_flight_lf_pct: float
    benchmark_lf_at_depth_pct: float
    pace_vs_benchmark_pct: float
    pickup_last_7d_pax: int = 0

    # Chain-Ladder Forecast
    forecast_final_freesale_pax: float
    forecast_low95_pax: float
    forecast_high95_pax: float
    forecast_final_freesale_lf_pct: float
    rem_pickup_mean: float
    rem_pickup_sd: float

    # Realized & Current Pricing Estimate
    realized_avg_fare_usd: Optional[float] = None
    last_sold_rbd: Optional[str] = None
    last_sold_fare_usd: Optional[float] = None
    inferred_current_rbd: str
    inferred_current_lite_usd: float

    # Quantitative Optimization Outputs
    emsr_bid_price_usd: float
    emsr_recommended_rbd: str
    smooth_target_fare_lite_usd: float
    smooth_target_fare_stand_usd: float
    suggested_action: ActionType

    # Market & Triage
    competitors: CompetitorSummary
    rbd_protection_table: list[RBDProtectionRow] = Field(default_factory=list)
    guardrails: GuardrailEvaluation
    priority_score: float
    priority_level: PriorityLevel
    alert_flags: list[str] = Field(default_factory=list)


class RecommendationRecord(BaseModel):
    id: int
    flight_id: str
    flight: str
    route: str
    departure_date: date
    current_depth: int
    source: str = "deterministic"  # deterministic | claude | ollama
    action_type: ActionType
    current_rbd: str
    current_fare_lite_usd: float
    recommended_rbd: str
    recommended_fare_lite_usd: float
    recommended_fare_stand_usd: float
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    key_risks: str = ""
    guardrail_passed: bool = True
    status: DecisionStatus = "PENDING"
    rm_final_rbd: Optional[str] = None
    rm_final_fare_lite_usd: Optional[float] = None
    rm_notes: Optional[str] = None
    created_at: datetime
    decided_at: Optional[datetime] = None


class DecisionInput(BaseModel):
    status: Literal["APPROVED", "MODIFIED", "REJECTED"]
    rm_final_rbd: Optional[str] = None
    rm_final_fare_lite_usd: Optional[float] = None
    rm_notes: str = ""
