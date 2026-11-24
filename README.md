# OneClick Airways (`4L`) — AI Revenue Management Agent

Decision-support agent and quantitative pricing/RBD engine for OneClick Airways, designed for **Claude Code (`MCP`)** today and seamless cutover to a **locally hosted LLM (`Ollama` / `vLLM`)** tomorrow.

## Architecture Highlights
- **Deterministic Math in Python + DuckDB (`src/rm_agent/engine.py`):**
  - Separates **Net Sellable Freesale Capacity** (`189 - Blocked Seats`) from tour-operator blocks.
  - Evaluates **Chain-Ladder booking pace** & 95% confidence bands against route benchmarks.
  - Computes **Belobaba EMSR-b marginal seat bid prices** & nested RBD protection levels across OneClick's Economy ladder (`U -> Q -> O -> N -> V -> M -> L -> T -> B -> R -> K -> Y`).
  - Synthesizes a **Smooth Target Fare ($)** (`Click Lite` + `$50` `ClickStand`) anchored against nonstop competitors (`EL AL`, `Arkia`, `ISRAIR`, `Georgian Airways`) and clamped by deterministic guardrails (`config/guardrails.yaml`).
- **Claude Code Native (`CLAUDE.md` + `.mcp.json`):**
  - `.claudeignore` blocks raw CSV/Hyper files from entering LLM context (keeping daily Claude token spend around `~$0.10/run`).
  - `.mcp.json` auto-registers the `oneclick-rm` MCP server (`get_triage_queue`, `get_flight_dossier`, `simulate_pricing_action`, `submit_recommendation`, `get_recent_decisions`).
- **Human-in-the-Loop Web Console (`src/rm_agent/web.py`):**
  - Local cockpit at `http://127.0.0.1:8080` with **Approve / Modify / Reject** cards, EMSR-b protection tables, What-If simulator, and persistent Decision Ledger.
- **Zero-Rewrite Local LLM Cutover (`src/rm_agent/llm_runner.py`):**
  - Run with `--provider anthropic` now, or `--provider ollama --model qwen2.5:14b` when switching to local hardware.

## Quickstart

```bash
cd /Users/kirilboiko/Documents/OneClick/RM

# 1. Install dependencies & ingest OneClick Hyper/CSV sources into DuckDB
uv sync
uv run rm-ingest

# 2. Launch the Revenue Manager Web Console (http://127.0.0.1:8080)
uv run rm-web

# 3. Launch Claude Code inside this repo (MCP tools attach automatically)
claude
```

## Updating Official RBD Tariffs & Guardrails
- Edit `config/rbd_ladder.yaml` once the RM provides the official filed PSS tariff sheet.
- Edit `config/guardrails.yaml` and `playbooks/rm_strategy.md` to tune floors, max daily RBD steps, and route rules.
