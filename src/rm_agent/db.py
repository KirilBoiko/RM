from __future__ import annotations

import duckdb

from .config import DATA_DIR, DB_PATH


def get_conn(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(DB_PATH), read_only=read_only)
    if not read_only:
        init_schema(conn)
    return conn


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flights_pickup (
            flight_id VARCHAR,
            flight VARCHAR,
            route VARCHAR,
            departure_date DATE,
            weekday VARCHAR,
            season VARCHAR,
            completion_status VARCHAR,
            depth INTEGER,
            current_depth INTEGER,
            capacity INTEGER,
            blocked_seats INTEGER,
            net_sellable_capacity INTEGER,
            cumulative_net_bookings INTEGER,
            daily_net_bookings INTEGER,
            daily_revenue_usd DOUBLE,
            avg_price_at_depth DOUBLE,
            commercial_lf_pct DOUBLE,
            total_flight_lf_pct DOUBLE,
            click_lite_seats INTEGER,
            click_stand_seats INTEGER,
            current_onhand_pax INTEGER,
            benchmark_lf_at_depth_pct DOUBLE,
            pace_vs_benchmark_pct DOUBLE,
            live_forecast_final_pax DOUBLE,
            live_forecast_low95_pax DOUBLE,
            live_forecast_high95_pax DOUBLE,
            rem_pickup_mean DOUBLE,
            rem_pickup_sd DOUBLE,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS ticket_sales (
            pnr VARCHAR,
            e_ticket VARCHAR,
            booking_date TIMESTAMP,
            departure_date DATE,
            flight VARCHAR,
            route VARCHAR,
            origin VARCHAR,
            destination VARCHAR,
            depth INTEGER,
            rbd VARCHAR,
            booking_class VARCHAR,
            fare_class VARCHAR,
            brand VARCHAR,
            doc_type VARCHAR,
            total_usd DOUBLE,
            segments INTEGER
        );

        CREATE TABLE IF NOT EXISTS competitor_fares (
            origin VARCHAR,
            destination VARCHAR,
            route VARCHAR,
            depart_date DATE,
            airline VARCHAR,
            stops VARCHAR,
            dep_time VARCHAR,
            price_usd DOUBLE,
            price_prev_usd DOUBLE,
            price_diff_1d_pct DOUBLE,
            snapshot_ts VARCHAR,
            source VARCHAR
        );

        CREATE SEQUENCE IF NOT EXISTS seq_rec_id START 1;

        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY DEFAULT nextval('seq_rec_id'),
            flight_id VARCHAR,
            flight VARCHAR,
            route VARCHAR,
            departure_date DATE,
            current_depth INTEGER,
            source VARCHAR,
            action_type VARCHAR,
            current_rbd VARCHAR,
            current_fare_lite_usd DOUBLE,
            recommended_rbd VARCHAR,
            recommended_fare_lite_usd DOUBLE,
            recommended_fare_stand_usd DOUBLE,
            confidence DOUBLE,
            rationale VARCHAR,
            key_risks VARCHAR,
            guardrail_passed BOOLEAN,
            status VARCHAR DEFAULT 'PENDING',
            rm_final_rbd VARCHAR,
            rm_final_fare_lite_usd DOUBLE,
            rm_notes VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            decided_at TIMESTAMP
        );
    """)
