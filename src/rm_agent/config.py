from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
PLAYBOOK_DIR = PROJECT_ROOT / "playbooks"
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = Path(os.environ.get("RM_DUCKDB_PATH", str(DATA_DIR / "rm_warehouse.duckdb")))


def load_yaml(filename: str) -> dict[str, Any]:
    path = CONFIG_DIR / filename
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def get_sources_config() -> dict[str, str]:
    return load_yaml("sources.yaml").get("sources", {})


def get_guardrails_config() -> dict[str, Any]:
    return load_yaml("guardrails.yaml").get("guardrails", {})


def get_route_ladder(route: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cfg = load_yaml("rbd_ladder.yaml")
    routes = cfg.get("routes", {})
    default_cfg = routes.get("DEFAULT", {})
    route_cfg = routes.get(route, default_cfg)
    ladder = route_cfg.get("ladder") or default_cfg.get("ladder", [])
    merged_meta = {
        "currency": route_cfg.get("currency", default_cfg.get("currency", "USD")),
        "budget_slf_pct": route_cfg.get("budget_slf_pct", default_cfg.get("budget_slf_pct", 85.0)),
        "budget_avg_fare_usd": route_cfg.get("budget_avg_fare_usd", default_cfg.get("budget_avg_fare_usd", 135.0)),
        "direct_competitors": route_cfg.get("direct_competitors", []),
        "stand_upsell_usd": cfg.get("default_stand_upsell_usd", 50.0),
    }
    return ladder, merged_meta


def load_playbook_text() -> str:
    pb = PLAYBOOK_DIR / "rm_strategy.md"
    return pb.read_text(encoding="utf-8") if pb.exists() else ""
