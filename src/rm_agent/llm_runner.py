from __future__ import annotations

import argparse
import json
import os
from typing import Any

import httpx

from .config import load_playbook_text
from .engine import get_triage_dossiers, save_recommendation, seed_deterministic_recommendations
from .schemas import FlightDossier

SYSTEM_PROMPT = """You are OneClick Airways' Revenue Management Decision-Support Agent.
Given a compact quantitative FlightDossier JSON and OneClick's commercial playbook, output ONLY a valid JSON object matching:
{
  "action_type": "HOLD" | "CLOSE_RBD_UP" | "OPEN_RBD_DOWN" | "ADJUST_FARE",
  "recommended_rbd": "<single letter RBD from ladder>",
  "recommended_fare_lite_usd": <number>,
  "confidence": <number between 0.0 and 1.0>,
  "rationale": "<concise 2-sentence data-backed explanation for the Revenue Manager>",
  "key_risks": "<concise 1-sentence risk or block-release watch>"
}
Never invent numbers. Respect guardrails (min_allowed_lite_usd, min_allowed_rbd, no drops inside D-5).
"""


def _compact_prompt_payload(d: FlightDossier) -> dict[str, Any]:
    return {
        "flight": d.flight,
        "route": d.route,
        "departure_date": str(d.departure_date),
        "weekday": d.weekday,
        "current_depth_dbd": d.current_depth,
        "net_sellable_capacity": d.net_sellable_capacity,
        "blocked_seats": d.blocked_seats,
        "on_hand_freesale_pax": d.on_hand_freesale_pax,
        "remaining_freesale_seats": d.remaining_freesale_seats,
        "freesale_lf_pct": d.freesale_lf_pct,
        "benchmark_lf_at_depth_pct": d.benchmark_lf_at_depth_pct,
        "pace_vs_benchmark_pct": d.pace_vs_benchmark_pct,
        "pickup_last_7d_pax": d.pickup_last_7d_pax,
        "forecast_final_pax": d.forecast_final_freesale_pax,
        "forecast_final_lf_pct": d.forecast_final_freesale_lf_pct,
        "inferred_current_rbd": d.inferred_current_rbd,
        "inferred_current_lite_usd": d.inferred_current_lite_usd,
        "emsr_bid_price_usd": d.emsr_bid_price_usd,
        "emsr_recommended_rbd": d.emsr_recommended_rbd,
        "engine_smooth_target_lite_usd": d.smooth_target_fare_lite_usd,
        "engine_suggested_action": d.suggested_action,
        "competitors": d.competitors.model_dump(),
        "guardrails": d.guardrails.model_dump(),
        "open_rbd_candidates": [
            {"rbd": r.rbd, "lite_usd": r.lite_usd, "protected_seats": r.protected_seats}
            for r in d.rbd_protection_table
        ],
    }


def call_anthropic(payload: dict[str, Any], playbook: str, model: str) -> dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in environment.")
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 450,
            "temperature": 0.1,
            "system": f"{SYSTEM_PROMPT}\n\nPlaybook:\n{playbook}",
            "messages": [
                {"role": "user", "content": json.dumps(payload)}
            ],
        },
        timeout=45.0,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def call_local_openai_compat(
    payload: dict[str, Any],
    playbook: str,
    model: str,
    base_url: str,
) -> dict[str, Any]:
    """
    Calls Ollama (http://localhost:11434/v1), vLLM, LM Studio, or MLX OpenAI-compatible server.
    """
    resp = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        json={
            "model": model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": f"{SYSTEM_PROMPT}\n\nPlaybook:\n{playbook}"},
                {"role": "user", "content": json.dumps(payload)},
            ],
        },
        timeout=90.0,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    return json.loads(text)


def run_batch(
    provider: str = "deterministic",
    model: str = "",
    limit: int = 12,
    base_url: str = "",
) -> int:
    if provider == "deterministic":
        return seed_deterministic_recommendations(limit=limit)

    playbook = load_playbook_text()
    dossiers = get_triage_dossiers(limit=limit, max_dbd=60)
    saved = 0

    for d in dossiers:
        payload = _compact_prompt_payload(d)
        if provider == "anthropic":
            chosen_model = model or os.environ.get("RM_ANTHROPIC_MODEL", "claude-sonnet-4-5")
            out = call_anthropic(payload, playbook, chosen_model)
            src = f"claude:{chosen_model}"
        elif provider in ("ollama", "local", "vllm"):
            chosen_model = model or os.environ.get("RM_LOCAL_MODEL", "qwen2.5:14b")
            url = base_url or os.environ.get("RM_LOCAL_BASE_URL", "http://localhost:11434/v1")
            out = call_local_openai_compat(payload, playbook, chosen_model, url)
            src = f"local:{chosen_model}"
        else:
            raise ValueError(f"Unknown provider: {provider}")

        save_recommendation(
            dossier=d,
            action_type=out["action_type"],
            recommended_rbd=out["recommended_rbd"],
            recommended_fare_lite_usd=float(out["recommended_fare_lite_usd"]),
            confidence=float(out.get("confidence", 0.8)),
            rationale=str(out["rationale"]),
            key_risks=str(out.get("key_risks", "")),
            source=src,
        )
        saved += 1

    return saved


def main() -> None:
    parser = argparse.ArgumentParser(description="Run OneClick RM Advisor (deterministic, anthropic, or local LLM)")
    parser.add_argument("--provider", choices=["deterministic", "anthropic", "ollama", "vllm"], default=os.environ.get("RM_LLM_PROVIDER", "deterministic"))
    parser.add_argument("--model", default="", help="Model ID (e.g. claude-sonnet-4-5 or qwen2.5:14b)")
    parser.add_argument("--base-url", default="", help="OpenAI-compatible base URL for local models (default http://localhost:11434/v1)")
    parser.add_argument("--limit", type=int, default=12, help="Number of top prioritized flights to evaluate")
    args = parser.parse_args()

    n = run_batch(provider=args.provider, model=args.model, limit=args.limit, base_url=args.base_url)
    print(f"Generated and saved {n} RM recommendations using provider='{args.provider}'.")


if __name__ == "__main__":
    main()
