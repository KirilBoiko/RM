from __future__ import annotations

import datetime as dt
import math
from typing import Any, Optional

from scipy.stats import norm

from .config import get_guardrails_config, get_route_ladder
from .db import get_conn
from .schemas import (
    ActionType,
    CompetitorSummary,
    FlightDossier,
    GuardrailEvaluation,
    PriorityLevel,
    RBDProtectionRow,
)


def compute_emsr_b(
    ladder: list[dict[str, Any]],
    remaining_seats: int,
    rem_demand_mean: float,
    rem_demand_sd: float,
) -> tuple[float, str, list[RBDProtectionRow]]:
    """
    Belobaba EMSR-b nested protection & marginal bid-price calculation.
    Ladder is ordered lowest fare (index 0) -> highest fare (index N-1).
    """
    if remaining_seats <= 0:
        top = ladder[-1]
        rows = [
            RBDProtectionRow(
                rbd=t["rbd"],
                tier_index=t["tier_index"],
                label=t["label"],
                lite_usd=t["lite_usd"],
                stand_usd=t["stand_usd"],
                protected_seats=0,
                recommended_open=(i == len(ladder) - 1),
            )
            for i, t in enumerate(ladder)
        ]
        return float(top["lite_usd"]), str(top["rbd"]), rows

    # Order high -> low for EMSR-b cumulative aggregation (class 1 = highest fare)
    high_to_low = list(reversed(ladder))
    total_mean = max(rem_demand_mean, 0.5)
    cv = (rem_demand_sd / total_mean) if (rem_demand_sd and total_mean > 0) else 0.35
    cv = min(max(cv, 0.20), 0.75)

    # Protection y_k: seats protected for classes 0..k-1 against class k
    protections_for_higher: dict[str, int] = {}
    cum_mean = 0.0
    cum_var = 0.0
    weighted_fare_num = 0.0

    for idx in range(len(high_to_low) - 1):
        curr = high_to_low[idx]
        lower = high_to_low[idx + 1]
        share = float(curr.get("demand_share", 1.0 / len(high_to_low)))
        mu_i = total_mean * share
        sd_i = max(mu_i * cv, 0.4)

        cum_mean += mu_i
        cum_var += sd_i**2
        weighted_fare_num += mu_i * float(curr["lite_usd"])

        if cum_mean <= 0.05:
            prot = 0
        else:
            p_bar = weighted_fare_num / cum_mean
            p_next = float(lower["lite_usd"])
            ratio = p_next / p_bar
            if ratio >= 0.999:
                prot = 0
            else:
                z = float(norm.ppf(1.0 - ratio))
                prot = max(0, int(round(cum_mean + z * math.sqrt(cum_var))))

        protections_for_higher[lower["rbd"]] = min(prot, remaining_seats)

    # Top bucket never closed if >= 1 seat remains
    protections_for_higher[high_to_low[0]["rbd"]] = 0

    rows: list[RBDProtectionRow] = []
    lowest_open_rbd: Optional[str] = None
    lowest_open_fare: Optional[float] = None

    for t in ladder:
        rbd = str(t["rbd"])
        prot = protections_for_higher.get(rbd, 0)
        is_open = remaining_seats > prot
        if is_open and lowest_open_rbd is None:
            lowest_open_rbd = rbd
            lowest_open_fare = float(t["lite_usd"])
        rows.append(
            RBDProtectionRow(
                rbd=rbd,
                tier_index=int(t["tier_index"]),
                label=str(t["label"]),
                lite_usd=float(t["lite_usd"]),
                stand_usd=float(t["stand_usd"]),
                protected_seats=int(prot),
                recommended_open=is_open,
            )
        )

    if lowest_open_rbd is None:
        lowest_open_rbd = str(ladder[-1]["rbd"])
        lowest_open_fare = float(ladder[-1]["lite_usd"])

    # Continuous bid price interpolation
    scarcity_ratio = total_mean / max(remaining_seats, 1)
    bid_price = float(lowest_open_fare) * min(max(0.85 + 0.15 * scarcity_ratio, 0.80), 1.12)
    return round(bid_price, 2), lowest_open_rbd, rows


def _nearest_rbd_for_fare(ladder: list[dict[str, Any]], target_lite_usd: float) -> dict[str, Any]:
    return min(ladder, key=lambda t: abs(float(t["lite_usd"]) - target_lite_usd))


def evaluate_guardrails(
    ladder: list[dict[str, Any]],
    current_depth: int,
    current_rbd: str,
    current_lite_usd: float,
    proposed_rbd: str,
    proposed_lite_usd: float,
    comp_nonstop_min_usd: Optional[float],
    pace_vs_benchmark_pct: float,
) -> GuardrailEvaluation:
    g = get_guardrails_config()
    warnings: list[str] = []

    min_lite_usd = 80.0
    min_rbd = ladder[0]["rbd"]
    for rule in sorted(g.get("min_fare_by_dbd", []), key=lambda x: x["max_dbd"]):
        if current_depth <= int(rule["max_dbd"]):
            min_lite_usd = float(rule["min_lite_usd"])
            min_rbd = str(rule["min_rbd"])
            break

    max_lite_usd = float(g.get("max_lite_usd", 435.0))
    rbd_to_idx = {t["rbd"]: i for i, t in enumerate(ladder)}
    curr_idx = rbd_to_idx.get(current_rbd, 1)
    prop_idx = rbd_to_idx.get(proposed_rbd, curr_idx)
    min_idx = rbd_to_idx.get(min_rbd, 0)

    clamped_idx = prop_idx
    clamped_fare = proposed_lite_usd

    # 1. No price drop within D-5
    no_drop_dbd = int(g.get("no_price_drop_within_dbd", 5))
    if current_depth <= no_drop_dbd and (proposed_lite_usd < current_lite_usd or prop_idx < curr_idx):
        warnings.append(f"Guardrail: No price/RBD drops inside D-{no_drop_dbd} (current DBD={current_depth}).")
        clamped_idx = max(clamped_idx, curr_idx)
        clamped_fare = max(clamped_fare, current_lite_usd)

    # 2. Max daily RBD step up / down (anti-whiplash)
    max_up = int(g.get("max_rbd_steps_up_per_day", 2))
    max_down = int(g.get("max_rbd_steps_down_per_day", 1))
    if clamped_idx - curr_idx > max_up:
        warnings.append(f"Guardrail: Step-up capped at +{max_up} RBD tiers/day (proposed +{clamped_idx - curr_idx}).")
        clamped_idx = curr_idx + max_up
        clamped_fare = float(ladder[clamped_idx]["lite_usd"])
    elif curr_idx - clamped_idx > max_down:
        warnings.append(f"Guardrail: Step-down capped at -{max_down} RBD tier/day (proposed -{curr_idx - clamped_idx}).")
        clamped_idx = curr_idx - max_down
        clamped_fare = float(ladder[clamped_idx]["lite_usd"])

    # 3. Minimum fare by DBD floor
    if clamped_idx < min_idx or clamped_fare < min_lite_usd:
        warnings.append(f"Guardrail: DBD={current_depth} requires floor >= ${min_lite_usd:.0f} ({min_rbd}).")
        clamped_idx = max(clamped_idx, min_idx)
        clamped_fare = max(clamped_fare, min_lite_usd)

    # 4. Competitor ceiling check when behind pace
    max_prem = float(g.get("max_premium_over_nonstop_comp_when_behind_pace_usd", 20.0))
    if comp_nonstop_min_usd and pace_vs_benchmark_pct < -10.0:
        if clamped_fare > comp_nonstop_min_usd + max_prem and clamped_idx > min_idx:
            warnings.append(
                f"Market warning: Flight is {pace_vs_benchmark_pct:.1f}% behind curve and ${clamped_fare - comp_nonstop_min_usd:.0f} above nonstop competitor (${comp_nonstop_min_usd:.0f})."
            )

    clamped_fare = min(max(round(clamped_fare, 0), min_lite_usd), max_lite_usd)
    clamped_rbd = str(ladder[min(max(clamped_idx, 0), len(ladder) - 1)]["rbd"])

    return GuardrailEvaluation(
        passed=(len(warnings) == 0),
        min_allowed_lite_usd=min_lite_usd,
        min_allowed_rbd=min_rbd,
        warnings=warnings,
        clamped_rbd=clamped_rbd,
        clamped_fare_lite_usd=clamped_fare,
    )


def _fetch_competitor_summary(conn: Any, route: str, dep_date: dt.date) -> CompetitorSummary:
    exact_rows = conn.execute(
        """
        SELECT airline, stops, MIN(price_usd) AS min_p, AVG(price_diff_1d_pct) AS diff_pct
        FROM competitor_fares
        WHERE route = ? AND depart_date = ?
        GROUP BY 1, 2
        ORDER BY 3 ASC
        """,
        [route, dep_date],
    ).fetchall()

    win_rows = conn.execute(
        """
        SELECT airline, depart_date, MIN(price_usd) AS min_p
        FROM competitor_fares
        WHERE route = ?
          AND depart_date BETWEEN ? - INTERVAL 2 DAY AND ? + INTERVAL 2 DAY
          AND (LOWER(stops) LIKE '%nonstop%' OR LOWER(stops) LIKE '%direct%' OR stops = '0')
        GROUP BY 1, 2
        ORDER BY 3 ASC
        LIMIT 5
        """,
        [route, dep_date, dep_date],
    ).fetchall()

    exact_nonstop = [
        r for r in exact_rows if r[1] and ("nonstop" in r[1].lower() or "direct" in r[1].lower() or r[1] == "0")
    ]
    exact_transfer = [r for r in exact_rows if r not in exact_nonstop]

    offers = [
        {
            "airline": r[0],
            "stops": r[1],
            "min_price_usd": round(float(r[2]), 0),
            "move_1d_pct": round(float(r[3]) * 100.0, 1) if r[3] is not None and abs(r[3]) < 5 else (round(float(r[3]), 1) if r[3] is not None else None),
        }
        for r in exact_rows[:8]
    ]

    return CompetitorSummary(
        exact_date_nonstop_min_usd=round(float(exact_nonstop[0][2]), 0) if exact_nonstop else None,
        exact_date_nonstop_airline=str(exact_nonstop[0][0]) if exact_nonstop else None,
        window_pm2d_nonstop_min_usd=round(float(win_rows[0][2]), 0) if win_rows else None,
        window_pm2d_nonstop_airline=f"{win_rows[0][0]} ({win_rows[0][1]})" if win_rows else None,
        transfer_min_usd=round(float(exact_transfer[0][2]), 0) if exact_transfer else None,
        recent_competitor_move_pct=offers[0]["move_1d_pct"] if offers else None,
        offers=offers,
    )


def build_flight_dossier(conn: Any, row: tuple[Any, ...]) -> FlightDossier:
    (
        flight_id, flight, route, dep_date, weekday, current_depth,
        capacity, blocked_seats, net_cap, onhand_pax, avg_price_depth,
        comm_lf, total_lf, lite_seats, stand_seats, bm_lf, pace_vs_bm,
        fc_final_pax, fc_low95, fc_high95, rem_mean, rem_sd, pickup_7d
    ) = row

    ladder, route_meta = get_route_ladder(route)
    stand_spread = float(route_meta["stand_upsell_usd"])

    net_cap = max(int(net_cap or (capacity - (blocked_seats or 0))), 1)
    onhand_pax = int(onhand_pax or 0)
    rem_seats = max(net_cap - onhand_pax, 0)
    freesale_lf_pct = round(float(comm_lf if comm_lf is not None else (onhand_pax / net_cap * 100.0)), 1)
    total_lf_pct = round(float(total_lf if total_lf is not None else ((onhand_pax + (blocked_seats or 0)) / max(capacity, 1) * 100.0)), 1)
    bm_lf_pct = round(float(bm_lf or 0.0), 1)
    pace_pct = round(float(pace_vs_bm or (freesale_lf_pct - bm_lf_pct)), 1)

    fc_final = round(float(fc_final_pax if fc_final_pax is not None else onhand_pax + (rem_mean or 0.0)), 1)
    fc_low = round(float(fc_low95 if fc_low95 is not None else max(onhand_pax, fc_final - 12)), 1)
    fc_high = round(float(fc_high95 if fc_high95 is not None else min(net_cap, fc_final + 12)), 1)
    fc_final_lf = round(fc_final / net_cap * 100.0, 1)

    rem_mean_val = float(rem_mean if rem_mean is not None else max(fc_final - onhand_pax, 1.0))
    rem_sd_val = float(rem_sd if rem_sd is not None else max(rem_mean_val * 0.35, 2.0))

    # Lookup latest ticket sales on this flight
    tkt = conn.execute(
        """
        SELECT
            AVG(CASE WHEN total_usd > 20 THEN total_usd END) AS avg_f,
            ARG_MAX(rbd, booking_date) AS last_rbd,
            ARG_MAX(total_usd, booking_date) AS last_fare
        FROM ticket_sales
        WHERE flight = ? AND departure_date = ? AND doc_type = 'ETICKET' AND total_usd > 20
        """,
        [flight, dep_date],
    ).fetchone()

    realized_avg = round(float(tkt[0]), 1) if tkt and tkt[0] else (round(float(avg_price_depth), 1) if avg_price_depth else None)
    last_rbd = str(tkt[1]) if tkt and tkt[1] else None
    last_fare = round(float(tkt[2]), 1) if tkt and tkt[2] else None

    # Infer current open RBD & Click Lite price
    rbd_map = {t["rbd"]: t for t in ladder}
    if last_rbd and last_rbd in rbd_map:
        inferred_rbd = last_rbd
        inferred_lite_usd = float(rbd_map[last_rbd]["lite_usd"])
    elif realized_avg:
        nearest = _nearest_rbd_for_fare(ladder, realized_avg)
        inferred_rbd = str(nearest["rbd"])
        inferred_lite_usd = float(nearest["lite_usd"])
    else:
        # Estimate baseline tier by current_depth
        default_by_dbd = 115.0 if current_depth > 21 else (134.0 if current_depth > 10 else 153.0)
        nearest = _nearest_rbd_for_fare(ladder, default_by_dbd)
        inferred_rbd = str(nearest["rbd"])
        inferred_lite_usd = float(nearest["lite_usd"])

    # EMSR-b calculation
    bid_price, emsr_rbd, prot_table = compute_emsr_b(ladder, rem_seats, rem_mean_val, rem_sd_val)
    emsr_lite_usd = float(rbd_map[emsr_rbd]["lite_usd"])

    # Competitor context
    comp = _fetch_competitor_summary(conn, route, dep_date)
    comp_anchor = comp.exact_date_nonstop_min_usd or comp.window_pm2d_nonstop_min_usd

    # Smooth target fare: blends EMSR-b tier fare, curve pace adjustment, and competitor anchor
    pace_adj = max(min(pace_pct * 0.45, 25.0), -20.0)
    raw_target = emsr_lite_usd + pace_adj
    if comp_anchor:
        if pace_pct < -15.0 and int(current_depth) > 5:
            # Stimulate gently slightly below nonstop competitor
            raw_target = min(raw_target, comp_anchor - 12.0)
        elif pace_pct > 5.0 and comp_anchor > raw_target + 35.0:
            # Capture margin when competitors are much higher and pace is healthy
            raw_target = 0.65 * raw_target + 0.35 * (comp_anchor - 15.0)

    target_tier = _nearest_rbd_for_fare(ladder, raw_target)
    guard = evaluate_guardrails(
        ladder=ladder,
        current_depth=int(current_depth),
        current_rbd=inferred_rbd,
        current_lite_usd=inferred_lite_usd,
        proposed_rbd=str(target_tier["rbd"]),
        proposed_lite_usd=round(raw_target, 0),
        comp_nonstop_min_usd=comp_anchor,
        pace_vs_benchmark_pct=pace_pct,
    )

    final_rbd = guard.clamped_rbd or str(target_tier["rbd"])
    final_lite = guard.clamped_fare_lite_usd or float(target_tier["lite_usd"])
    final_stand = round(final_lite + stand_spread, 0)

    rbd_idx = {t["rbd"]: i for i, t in enumerate(ladder)}
    c_i = rbd_idx.get(inferred_rbd, 1)
    f_i = rbd_idx.get(final_rbd, c_i)

    if f_i > c_i:
        action: ActionType = "CLOSE_RBD_UP"
    elif f_i < c_i:
        action = "OPEN_RBD_DOWN"
    elif abs(final_lite - inferred_lite_usd) >= 12.0:
        action = "ADJUST_FARE"
    else:
        action = "HOLD"

    # Exception / Triage Priority Scoring (0-100)
    alerts: list[str] = []
    score = 0.0

    if int(current_depth) <= 14:
        score += 25.0
    elif int(current_depth) <= 30:
        score += 12.0

    if pace_pct <= -25.0:
        score += 30.0
        alerts.append(f"Behind curve ({pace_pct:+.1f}% vs benchmark)")
    elif pace_pct >= 12.0:
        score += 25.0
        alerts.append(f"Strong pace ({pace_pct:+.1f}% ahead of curve)")

    if fc_final_lf >= 92.0 and rem_seats > 0:
        score += 22.0
        alerts.append(f"Spoilage/Spill risk: Forecast LF {fc_final_lf:.0f}%")
    elif fc_final_lf < 55.0 and int(current_depth) <= 21:
        score += 20.0
        alerts.append(f"Spoilage risk: Forecast LF only {fc_final_lf:.0f}% at D-{current_depth}")

    if comp_anchor:
        gap = inferred_lite_usd - comp_anchor
        if gap >= 25.0 and pace_pct < 0:
            score += 18.0
            alerts.append(f"Priced +${gap:.0f} above {comp.exact_date_nonstop_airline or 'competitor'}")
        elif gap <= -40.0 and pace_pct >= -5.0:
            score += 18.0
            alerts.append(f"Undercutting {comp.exact_date_nonstop_airline or 'competitor'} by ${abs(gap):.0f}")

    if (blocked_seats or 0) >= 80 and 7 <= int(current_depth) <= 16:
        score += 10.0
        alerts.append(f"High block allocation ({blocked_seats} seats) entering D-14 release window")

    if action != "HOLD":
        score += 12.0

    score = min(round(score, 1), 100.0)
    priority: PriorityLevel = "HIGH" if score >= 55 else ("MEDIUM" if score >= 32 else "LOW")

    return FlightDossier(
        flight_id=str(flight_id),
        flight=str(flight),
        route=str(route),
        departure_date=dep_date,
        weekday=str(weekday or dep_date.strftime("%a")),
        current_depth=int(current_depth),
        physical_capacity=int(capacity or 189),
        blocked_seats=int(blocked_seats or 0),
        net_sellable_capacity=net_cap,
        on_hand_freesale_pax=onhand_pax,
        remaining_freesale_seats=rem_seats,
        click_lite_seats=int(lite_seats or 0),
        click_stand_seats=int(stand_seats or 0),
        freesale_lf_pct=freesale_lf_pct,
        total_flight_lf_pct=total_lf_pct,
        benchmark_lf_at_depth_pct=bm_lf_pct,
        pace_vs_benchmark_pct=pace_pct,
        pickup_last_7d_pax=int(pickup_7d or 0),
        forecast_final_freesale_pax=fc_final,
        forecast_low95_pax=fc_low,
        forecast_high95_pax=fc_high,
        forecast_final_freesale_lf_pct=fc_final_lf,
        rem_pickup_mean=round(rem_mean_val, 1),
        rem_pickup_sd=round(rem_sd_val, 1),
        realized_avg_fare_usd=realized_avg,
        last_sold_rbd=last_rbd,
        last_sold_fare_usd=last_fare,
        inferred_current_rbd=inferred_rbd,
        inferred_current_lite_usd=inferred_lite_usd,
        emsr_bid_price_usd=bid_price,
        emsr_recommended_rbd=emsr_rbd,
        smooth_target_fare_lite_usd=final_lite,
        smooth_target_fare_stand_usd=final_stand,
        suggested_action=action,
        competitors=comp,
        rbd_protection_table=prot_table,
        guardrails=guard,
        priority_score=score,
        priority_level=priority,
        alert_flags=alerts,
    )


_LATEST_FLIGHTS_SQL = """
WITH latest_snap AS (
    SELECT
        flight_id,
        flight,
        route,
        departure_date,
        weekday,
        current_depth,
        capacity,
        blocked_seats,
        net_sellable_capacity,
        current_onhand_pax,
        avg_price_at_depth,
        commercial_lf_pct,
        total_flight_lf_pct,
        click_lite_seats,
        click_stand_seats,
        benchmark_lf_at_depth_pct,
        pace_vs_benchmark_pct,
        live_forecast_final_pax,
        live_forecast_low95_pax,
        live_forecast_high95_pax,
        rem_pickup_mean,
        rem_pickup_sd,
        ROW_NUMBER() OVER (PARTITION BY flight, departure_date ORDER BY ABS(depth - current_depth) ASC, depth ASC) AS rn
    FROM flights_pickup
    WHERE completion_status <> 'Departed'
      AND departure_date >= CURRENT_DATE
),
pickup_7d AS (
    SELECT
        flight,
        departure_date,
        SUM(COALESCE(daily_net_bookings, 0)) AS pax_7d
    FROM flights_pickup
    WHERE completion_status <> 'Departed'
      AND depth BETWEEN current_depth AND current_depth + 7
    GROUP BY 1, 2
)
SELECT
    l.flight_id, l.flight, l.route, l.departure_date, l.weekday, l.current_depth,
    l.capacity, l.blocked_seats, l.net_sellable_capacity, l.current_onhand_pax,
    l.avg_price_at_depth, l.commercial_lf_pct, l.total_flight_lf_pct,
    l.click_lite_seats, l.click_stand_seats, l.benchmark_lf_at_depth_pct,
    l.pace_vs_benchmark_pct, l.live_forecast_final_pax, l.live_forecast_low95_pax,
    l.live_forecast_high95_pax, l.rem_pickup_mean, l.rem_pickup_sd,
    COALESCE(p.pax_7d, 0) AS pickup_7d
FROM latest_snap l
LEFT JOIN pickup_7d p ON l.flight = p.flight AND l.departure_date = p.departure_date
WHERE l.rn = 1
"""


def get_triage_dossiers(
    limit: int = 25,
    route: Optional[str] = None,
    max_dbd: int = 90,
    min_priority: PriorityLevel = "LOW",
) -> list[FlightDossier]:
    conn = get_conn(read_only=True)
    try:
        params: list[Any] = [max_dbd]
        where_extra = ""
        if route:
            where_extra = " AND route = ?"
            params.append(route)
        rows = conn.execute(
            f"SELECT * FROM ({_LATEST_FLIGHTS_SQL}) sub WHERE current_depth <= ? {where_extra} ORDER BY departure_date ASC",
            params,
        ).fetchall()
        dossiers = [build_flight_dossier(conn, r) for r in rows]
    finally:
        conn.close()

    rank_map = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    min_r = rank_map.get(min_priority, 0)
    filtered = [d for d in dossiers if rank_map[d.priority_level] >= min_r]
    filtered.sort(key=lambda d: (-d.priority_score, d.departure_date))
    return filtered[:limit]


def get_single_flight_dossier(flight: str, departure_date: str | dt.date) -> Optional[FlightDossier]:
    ddate = dt.date.fromisoformat(departure_date) if isinstance(departure_date, str) else departure_date
    conn = get_conn(read_only=True)
    try:
        row = conn.execute(
            f"SELECT * FROM ({_LATEST_FLIGHTS_SQL}) sub WHERE flight = ? AND departure_date = ?",
            [flight.upper(), ddate],
        ).fetchone()
        if not row:
            return None
        return build_flight_dossier(conn, row)
    finally:
        conn.close()


def simulate_action(
    flight: str,
    departure_date: str | dt.date,
    proposed_rbd: str,
    proposed_fare_lite_usd: float,
) -> dict[str, Any]:
    dossier = get_single_flight_dossier(flight, departure_date)
    if not dossier:
        raise ValueError(f"Flight {flight} on {departure_date} not found.")
    ladder, meta = get_route_ladder(dossier.route)
    comp_anchor = dossier.competitors.exact_date_nonstop_min_usd or dossier.competitors.window_pm2d_nonstop_min_usd

    guard = evaluate_guardrails(
        ladder=ladder,
        current_depth=dossier.current_depth,
        current_rbd=dossier.inferred_current_rbd,
        current_lite_usd=dossier.inferred_current_lite_usd,
        proposed_rbd=proposed_rbd.upper(),
        proposed_lite_usd=proposed_fare_lite_usd,
        comp_nonstop_min_usd=comp_anchor,
        pace_vs_benchmark_pct=dossier.pace_vs_benchmark_pct,
    )

    # Elasticity-based demand impact approximation (airline VFR elasticity ~ -1.1)
    elasticity = -0.6 if dossier.current_depth <= 7 else -1.15
    pct_price_change = (proposed_fare_lite_usd - dossier.inferred_current_lite_usd) / max(dossier.inferred_current_lite_usd, 1.0)
    demand_mult = max(0.35, min(1.65, 1.0 + elasticity * pct_price_change))
    sim_rem_pax = min(dossier.remaining_freesale_seats, round(dossier.rem_pickup_mean * demand_mult, 1))
    base_rem_pax = min(dossier.remaining_freesale_seats, dossier.rem_pickup_mean)

    base_rem_rev = round(base_rem_pax * dossier.inferred_current_lite_usd, 0)
    sim_rem_rev = round(sim_rem_pax * proposed_fare_lite_usd, 0)

    return {
        "flight": dossier.flight,
        "departure_date": str(dossier.departure_date),
        "current_rbd": dossier.inferred_current_rbd,
        "current_lite_usd": dossier.inferred_current_lite_usd,
        "proposed_rbd": proposed_rbd.upper(),
        "proposed_lite_usd": proposed_fare_lite_usd,
        "proposed_stand_usd": proposed_fare_lite_usd + float(meta["stand_upsell_usd"]),
        "emsr_bid_price_usd": dossier.emsr_bid_price_usd,
        "expected_remaining_pax_baseline": round(base_rem_pax, 1),
        "expected_remaining_pax_simulated": sim_rem_pax,
        "expected_remaining_rev_baseline_usd": base_rem_rev,
        "expected_remaining_rev_simulated_usd": sim_rem_rev,
        "expected_revenue_delta_usd": round(sim_rem_rev - base_rem_rev, 0),
        "guardrails": guard.model_dump(),
    }


def save_recommendation(
    dossier: FlightDossier,
    action_type: ActionType,
    recommended_rbd: str,
    recommended_fare_lite_usd: float,
    confidence: float,
    rationale: str,
    key_risks: str = "",
    source: str = "deterministic",
) -> int:
    ladder, meta = get_route_ladder(dossier.route)
    comp_anchor = dossier.competitors.exact_date_nonstop_min_usd or dossier.competitors.window_pm2d_nonstop_min_usd
    guard = evaluate_guardrails(
        ladder=ladder,
        current_depth=dossier.current_depth,
        current_rbd=dossier.inferred_current_rbd,
        current_lite_usd=dossier.inferred_current_lite_usd,
        proposed_rbd=recommended_rbd,
        proposed_lite_usd=recommended_fare_lite_usd,
        comp_nonstop_min_usd=comp_anchor,
        pace_vs_benchmark_pct=dossier.pace_vs_benchmark_pct,
    )
    stand_usd = round(recommended_fare_lite_usd + float(meta["stand_upsell_usd"]), 0)

    conn = get_conn(read_only=False)
    try:
        # Supersede any existing PENDING recommendation for the same flight & departure_date
        conn.execute(
            "DELETE FROM recommendations WHERE flight = ? AND departure_date = ? AND status = 'PENDING'",
            [dossier.flight, dossier.departure_date],
        )
        rec_id = conn.execute(
            """
            INSERT INTO recommendations (
                flight_id, flight, route, departure_date, current_depth, source,
                action_type, current_rbd, current_fare_lite_usd,
                recommended_rbd, recommended_fare_lite_usd, recommended_fare_stand_usd,
                confidence, rationale, key_risks, guardrail_passed, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            RETURNING id
            """,
            [
                dossier.flight_id,
                dossier.flight,
                dossier.route,
                dossier.departure_date,
                dossier.current_depth,
                source,
                action_type,
                dossier.inferred_current_rbd,
                dossier.inferred_current_lite_usd,
                recommended_rbd.upper(),
                recommended_fare_lite_usd,
                stand_usd,
                round(confidence, 2),
                rationale,
                key_risks,
                guard.passed,
            ],
        ).fetchone()[0]
        return int(rec_id)
    finally:
        conn.close()


def seed_deterministic_recommendations(limit: int = 20, preserve_ai: bool = True) -> int:
    dossiers = get_triage_dossiers(limit=limit, max_dbd=60)
    existing_ai: set[tuple[str, str]] = set()
    if preserve_ai:
        conn = get_conn(read_only=True)
        try:
            rows = conn.execute(
                "SELECT flight, departure_date FROM recommendations WHERE status = 'PENDING' AND source <> 'deterministic'"
            ).fetchall()
            existing_ai = {(str(r[0]), str(r[1])) for r in rows}
        finally:
            conn.close()

    count = 0
    for d in dossiers:
        if preserve_ai and (d.flight, str(d.departure_date)) in existing_ai:
            continue
        comp_str = (
            f"Cheapest nonstop competitor ({d.competitors.exact_date_nonstop_airline}) is ${d.competitors.exact_date_nonstop_min_usd:.0f}."
            if d.competitors.exact_date_nonstop_min_usd
            else (
                f"Nearby +/-2d nonstop min is ${d.competitors.window_pm2d_nonstop_min_usd:.0f} ({d.competitors.window_pm2d_nonstop_airline})."
                if d.competitors.window_pm2d_nonstop_min_usd
                else "No nonstop competitor scrape on exact date."
            )
        )
        rationale = (
            f"D-{d.current_depth} | Freesale LF {d.freesale_lf_pct:.1f}% ({d.on_hand_freesale_pax}/{d.net_sellable_capacity} net seats, {d.blocked_seats} blocked) "
            f"vs {d.benchmark_lf_at_depth_pct:.1f}% curve ({d.pace_vs_benchmark_pct:+.1f}%). "
            f"Chain-ladder projects {d.forecast_final_freesale_pax:.0f} pax ({d.forecast_final_freesale_lf_pct:.0f}% LF); "
            f"EMSR-b bid price = ${d.emsr_bid_price_usd:.0f} (tier {d.emsr_recommended_rbd}). {comp_str}"
        )
        risks = "; ".join(d.guardrails.warnings or d.alert_flags[:2]) or "Standard booking volatility on thin weekly schedule."
        save_recommendation(
            dossier=d,
            action_type=d.suggested_action,
            recommended_rbd=d.guardrails.clamped_rbd or d.emsr_recommended_rbd,
            recommended_fare_lite_usd=d.smooth_target_fare_lite_usd,
            confidence=0.82 if d.competitors.exact_date_nonstop_min_usd else 0.72,
            rationale=rationale,
            key_risks=risks,
            source="deterministic",
        )
        count += 1
    return count
