from __future__ import annotations

import csv
import datetime as dt
import glob
import os
import tempfile
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from tableauhyperapi import Connection, HyperProcess, Telemetry

from .config import get_sources_config
from .db import get_conn


def _to_py(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, Decimal):
        return float(val)
    if hasattr(val, "year") and hasattr(val, "hour"):
        return dt.datetime(val.year, val.month, val.day, val.hour, val.minute, val.second)
    if hasattr(val, "year") and hasattr(val, "month") and hasattr(val, "day"):
        return dt.date(val.year, val.month, val.day)
    return val


def ingest_all(seed_baseline: bool = True) -> dict[str, int]:
    sources = get_sources_config()
    conn = get_conn(read_only=False)
    counts: dict[str, int] = {}

    with HyperProcess(
        telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU,
        parameters={"log_config": ""},
    ) as hp:
        # 1. Pickup & Booking Curve Forecast Hyper
        pickup_path = sources.get("pickup_hyper", "")
        if pickup_path and os.path.exists(pickup_path):
            with Connection(endpoint=hp.endpoint, database=pickup_path) as hconn:
                tbl = hconn.catalog.get_table_names("Extract")[0]
                rows = hconn.execute_list_query(f"""
                    SELECT
                        "Flight_ID",
                        "Flight",
                        "Route",
                        "Departure Date",
                        "Weekday",
                        "Season",
                        "Flight Completion Status",
                        "Depth",
                        "Current_Depth",
                        "Capacity",
                        "Blocked Seats Clean",
                        "Net Sellable Capacity",
                        "Cumulative_Net_Bookings",
                        "Daily_Net_Bookings",
                        "Daily_Revenue_USD",
                        "Avg_Price_At_Depth",
                        "Commercial_Load_Factor_%",
                        "Total_Flight_Load_Factor_%",
                        "ClickLite_Seats",
                        "ClickStand_Seats",
                        "Current_OnHand_Pax",
                        COALESCE("Enhanced_Benchmark_LF_%_At_Depth", "Benchmark_LF_%_At_Depth"),
                        COALESCE("Pace_vs_Enhanced_Benchmark_%", "Current_Pace_vs_Benchmark_%"),
                        "Live_Forecast_Final_Pax",
                        "Live_Forecast_Low95_Pax",
                        "Live_Forecast_High95_Pax",
                        "Rem_Pickup_Mean",
                        "Rem_Pickup_SD"
                    FROM {tbl}
                """)
                py_rows = [tuple(_to_py(v) for v in r) for r in rows]
                conn.execute("DELETE FROM flights_pickup")
                conn.executemany(
                    """
                    INSERT INTO flights_pickup (
                        flight_id, flight, route, departure_date, weekday, season,
                        completion_status, depth, current_depth, capacity, blocked_seats,
                        net_sellable_capacity, cumulative_net_bookings, daily_net_bookings,
                        daily_revenue_usd, avg_price_at_depth, commercial_lf_pct,
                        total_flight_lf_pct, click_lite_seats, click_stand_seats,
                        current_onhand_pax, benchmark_lf_at_depth_pct, pace_vs_benchmark_pct,
                        live_forecast_final_pax, live_forecast_low95_pax, live_forecast_high95_pax,
                        rem_pickup_mean, rem_pickup_sd
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    py_rows,
                )
                counts["flights_pickup"] = len(py_rows)

        # 2. Ticket Sales & RBD History (.tdsx)
        sales_tdsx = sources.get("sales_tdsx", "")
        if sales_tdsx and os.path.exists(sales_tdsx):
            with zipfile.ZipFile(sales_tdsx, "r") as zf, tempfile.TemporaryDirectory() as tmp:
                hyper_entry = [n for n in zf.namelist() if n.endswith(".hyper")][0]
                extracted_hyper = zf.extract(hyper_entry, tmp)
                with Connection(endpoint=hp.endpoint, database=extracted_hyper) as hconn:
                    tbl = hconn.catalog.get_table_names("Extract")[0]
                    rows = hconn.execute_list_query(f"""
                        SELECT
                            "PNR",
                            "E-Ticket",
                            "Booking Date",
                            CAST("Departure Date" AS DATE),
                            "Flight",
                            COALESCE("Route", "from" || '-' || "to"),
                            "from",
                            "to",
                            "Depth",
                            COALESCE("RBD", CASE WHEN "Class" LIKE 'Y/%' THEN SUBSTR("Class", 3, 1) ELSE "Class" END),
                            "Class",
                            "Fare Class",
                            "Fare Rule Name",
                            "Type",
                            "Total_USD",
                            "segments"
                        FROM {tbl}
                    """)
                    py_rows = [tuple(_to_py(v) for v in r) for r in rows]
                    conn.execute("DELETE FROM ticket_sales")
                    conn.executemany(
                        """
                        INSERT INTO ticket_sales (
                            pnr, e_ticket, booking_date, departure_date, flight, route,
                            origin, destination, depth, rbd, booking_class, fare_class,
                            brand, doc_type, total_usd, segments
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        py_rows,
                    )
                    counts["ticket_sales"] = len(py_rows)

        # 3. Competitor Fares (Hyper + Scraped CSVs)
        conn.execute("DELETE FROM competitor_fares")
        comp_rows: list[tuple[Any, ...]] = []

        price_diff = sources.get("price_diff_hyper", "")
        if price_diff and os.path.exists(price_diff):
            with Connection(endpoint=hp.endpoint, database=price_diff) as hconn:
                tbl = hconn.catalog.get_table_names("Extract")[0]
                rows = hconn.execute_list_query(f"""
                    SELECT
                        "origin", "destination", "Route", "departure_date",
                        "airline", 'Nonstop', CAST("comp_dep_datetime_utc" AS VARCHAR),
                        "price_usd_today", "price_usd_yesterday", "1day_price_diff%",
                        CAST("snapshot_ts " AS VARCHAR), 'Price_Difference.hyper'
                    FROM {tbl}
                    WHERE "price_usd_today" IS NOT NULL
                """)
                comp_rows.extend(tuple(_to_py(v) for v in r) for r in rows)

        comp_hist = sources.get("comp_price_history_hyper", "")
        if comp_hist and os.path.exists(comp_hist):
            with Connection(endpoint=hp.endpoint, database=comp_hist) as hconn:
                tbl = hconn.catalog.get_table_names("Extract")[0]
                rows = hconn.execute_list_query(f"""
                    SELECT
                        "origin", "destination",
                        COALESCE("Route", "origin" || '-' || "destination"),
                        "depart_date", "airline", "stops", "dep_time",
                        "price_usd", NULL, NULL,
                        CAST("snapshot_ts " AS VARCHAR), 'Comp_price_history.hyper'
                    FROM {tbl}
                    WHERE "price_usd" IS NOT NULL
                """)
                comp_rows.extend(tuple(_to_py(v) for v in r) for r in rows)

        csv_glob = sources.get("scraped_csv_glob", "")
        if csv_glob:
            for csv_file in sorted(glob.glob(csv_glob)):
                with open(csv_file, "r", encoding="utf-8-sig", errors="replace") as fh:
                    reader = csv.DictReader(fh)
                    for r in reader:
                        try:
                            price = float(r["price_usd"]) if r.get("price_usd") else None
                            ddate = dt.date.fromisoformat(r["depart_date"][:10]) if r.get("depart_date") else None
                        except (ValueError, TypeError):
                            continue
                        if price and ddate:
                            orig = (r.get("origin") or "").strip()
                            dest = (r.get("destination") or "").strip()
                            comp_rows.append((
                                orig,
                                dest,
                                f"{orig}-{dest}",
                                ddate,
                                (r.get("airline") or "").strip(),
                                (r.get("stops") or "").strip(),
                                (r.get("dep_time") or "").strip(),
                                price,
                                None,
                                None,
                                (r.get("snapshot_ts") or "").strip(),
                                Path(csv_file).name,
                            ))

        if comp_rows:
            conn.executemany(
                """
                INSERT INTO competitor_fares (
                    origin, destination, route, depart_date, airline, stops,
                    dep_time, price_usd, price_prev_usd, price_diff_1d_pct,
                    snapshot_ts, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                comp_rows,
            )
        counts["competitor_fares"] = len(comp_rows)

    conn.close()

    if seed_baseline:
        from .engine import seed_deterministic_recommendations
        counts["seeded_recommendations"] = seed_deterministic_recommendations(limit=25)

    return counts


def main() -> None:
    counts = ingest_all(seed_baseline=True)
    print("OneClick RM Warehouse ingested successfully:")
    for k, v in counts.items():
        print(f"  - {k}: {v:,} rows")


if __name__ == "__main__":
    main()
