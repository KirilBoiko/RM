from __future__ import annotations

from typing import Optional

from mcp.server.mcpserver import MCPServer

from .config import get_guardrails_config, get_route_ladder
from .db import get_conn
from .engine import (
    get_single_flight_dossier,
    get_triage_dossiers,
    run_walkforward_backtest,
    save_recommendation,
    simulate_action,
)
from .schemas import ActionType, PriorityLevel

mcp = MCPServer("oneclick-rm")


@mcp.tool()
def get_triage_queue(
    limit: int = 15,
    route: Optional[str] = None,
    max_dbd: int = 60,
    min_priority: PriorityLevel = "LOW",
) -> list[dict]:
    """
    Return the prioritized list of upcoming OneClick (4L) flights needing RM attention.
    Compact summary (<120 tokens/flight) to minimize LLM token usage.
    """
    dossiers = get_triage_dossiers(limit=limit, route=route, max_dbd=max_dbd, min_priority=min_priority)
    return [
        {
            "flight": d.flight,
            "route": d.route,
            "departure_date": str(d.departure_date),
            "weekday": d.weekday,
            "dbd": d.current_depth,
            "priority": f"{d.priority_level} ({d.priority_score})",
            "net_cap": d.net_sellable_capacity,
            "blocked": d.blocked_seats,
            "sold_pax": d.on_hand_freesale_pax,
            "freesale_lf_pct": d.freesale_lf_pct,
            "pace_vs_curve_pct": d.pace_vs_benchmark_pct,
            "forecast_lf_pct": d.forecast_final_freesale_lf_pct,
            "current_est": f"{d.inferred_current_rbd} (${d.inferred_current_lite_usd:.0f})",
            "emsr_bid_usd": d.emsr_bid_price_usd,
            "engine_suggestion": f"{d.suggested_action} -> {d.guardrails.clamped_rbd} (${d.smooth_target_fare_lite_usd:.0f} Lite / ${d.smooth_target_fare_stand_usd:.0f} Stand)",
            "comp_nonstop_min_usd": d.competitors.exact_date_nonstop_min_usd or d.competitors.window_pm2d_nonstop_min_usd,
            "alerts": d.alert_flags,
        }
        for d in dossiers
    ]


@mcp.tool()
def get_flight_dossier(flight: str, departure_date: str) -> dict:
    """
    Return the full quantitative FlightDossier (EMSR-b protection table, Chain-Ladder 95% CI,
    competitor breakdown, realized fares, and guardrails) for a specific flight (e.g. '4L101', '2026-11-29').
    """
    dossier = get_single_flight_dossier(flight, departure_date)
    if not dossier:
        return {"error": f"Flight {flight} on {departure_date} not found."}
    return dossier.model_dump(mode="json")


@mcp.tool()
def simulate_pricing_action(
    flight: str,
    departure_date: str,
    proposed_rbd: str,
    proposed_fare_lite_usd: float,
) -> dict:
    """
    Simulate expected remaining pickup, expected remaining revenue delta, and guardrail compliance
    for a proposed RBD letter and Click Lite fare ($) on a specific flight.
    """
    return simulate_action(flight, departure_date, proposed_rbd, proposed_fare_lite_usd)


@mcp.tool()
def submit_recommendation(
    flight: str,
    departure_date: str,
    action_type: ActionType,
    recommended_rbd: str,
    recommended_fare_lite_usd: float,
    confidence: float,
    rationale: str,
    key_risks: str = "",
) -> dict:
    """
    Save an AI RM recommendation into DuckDB so it appears as an Approve/Modify/Reject card
    in the OneClick RM Web Console.
    """
    dossier = get_single_flight_dossier(flight, departure_date)
    if not dossier:
        return {"error": f"Flight {flight} on {departure_date} not found."}
    rec_id = save_recommendation(
        dossier=dossier,
        action_type=action_type,
        recommended_rbd=recommended_rbd,
        recommended_fare_lite_usd=recommended_fare_lite_usd,
        confidence=confidence,
        rationale=rationale,
        key_risks=key_risks,
        source="claude-mcp",
    )
    return {
        "status": "saved",
        "recommendation_id": rec_id,
        "flight": flight,
        "departure_date": departure_date,
        "action_type": action_type,
        "recommended_rbd": recommended_rbd.upper(),
        "recommended_fare_lite_usd": recommended_fare_lite_usd,
    }


@mcp.tool()
def get_rbd_ladder_and_guardrails(route: str = "TBS-TLV") -> dict:
    """Return the configured RBD tariff ladder and commercial guardrails for a route."""
    ladder, meta = get_route_ladder(route)
    return {
        "route": route,
        "meta": meta,
        "ladder": ladder,
        "guardrails": get_guardrails_config(),
    }


@mcp.tool()
def get_recent_decisions(limit: int = 20) -> list[dict]:
    """
    Return recent Revenue Manager decisions (APPROVED / MODIFIED / REJECTED + RM notes)
    so the agent learns from human overrides.
    """
    conn = get_conn(read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT id, flight, route, departure_date, action_type,
                   recommended_rbd, recommended_fare_lite_usd,
                   status, rm_final_rbd, rm_final_fare_lite_usd, rm_notes, decided_at
            FROM recommendations
            WHERE status <> 'PENDING'
            ORDER BY decided_at DESC NULLS LAST
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [
            {
                "id": r[0],
                "flight": r[1],
                "route": r[2],
                "departure_date": str(r[3]),
                "proposed": f"{r[4]} -> {r[5]} (${r[6]:.0f})",
                "rm_status": r[7],
                "rm_final": f"{r[8] or r[5]} (${(r[9] if r[9] is not None else r[6]):.0f})",
                "rm_notes": r[10] or "",
                "decided_at": str(r[11]) if r[11] else None,
            }
            for r in rows
        ]
    finally:
        conn.close()


@mcp.tool()
def get_backtest_accuracy() -> list[dict]:
    """
    Return walk-forward out-of-sample forecast accuracy (MAE, Bias, Within +/-10 & +/-15 pax)
    by Route and DBD on departed flights so the agent can calibrate confidence scientifically.
    """
    return run_walkforward_backtest()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
