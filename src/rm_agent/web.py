from __future__ import annotations

from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .db import get_conn
from .engine import (
    get_single_flight_dossier,
    get_triage_dossiers,
    run_walkforward_backtest,
    seed_deterministic_recommendations,
    simulate_action,
)
from .schemas import DecisionInput

app = FastAPI(title="OneClick (4L) RM Decision Console")


@app.get("/api/triage")
def api_triage(limit: int = 30, route: Optional[str] = None, max_dbd: int = 60) -> dict:
    dossiers = get_triage_dossiers(limit=limit, route=route or None, max_dbd=max_dbd)
    conn = get_conn(read_only=True)
    try:
        rec_rows = conn.execute(
            """
            SELECT id, flight, departure_date, source, action_type,
                   current_rbd, current_fare_lite_usd,
                   recommended_rbd, recommended_fare_lite_usd, recommended_fare_stand_usd,
                   confidence, rationale, key_risks, guardrail_passed, status,
                   rm_final_rbd, rm_final_fare_lite_usd, rm_notes
            FROM recommendations
            ORDER BY id DESC
            """
        ).fetchall()
    finally:
        conn.close()

    rec_by_key: dict[tuple[str, str], dict] = {}
    for r in rec_rows:
        key = (str(r[1]), str(r[2]))
        if key not in rec_by_key:
            rec_by_key[key] = {
                "id": r[0],
                "source": r[3],
                "action_type": r[4],
                "current_rbd": r[5],
                "current_fare_lite_usd": r[6],
                "recommended_rbd": r[7],
                "recommended_fare_lite_usd": r[8],
                "recommended_fare_stand_usd": r[9],
                "confidence": r[10],
                "rationale": r[11],
                "key_risks": r[12],
                "guardrail_passed": r[13],
                "status": r[14],
                "rm_final_rbd": r[15],
                "rm_final_fare_lite_usd": r[16],
                "rm_notes": r[17],
            }

    items = []
    for d in dossiers:
        d_json = d.model_dump(mode="json")
        d_json["recommendation"] = rec_by_key.get((d.flight, str(d.departure_date)))
        items.append(d_json)

    high_cnt = sum(1 for d in dossiers if d.priority_level == "HIGH")
    avg_lf = round(sum(d.freesale_lf_pct for d in dossiers) / max(len(dossiers), 1), 1)
    avg_pace = round(sum(d.pace_vs_benchmark_pct for d in dossiers) / max(len(dossiers), 1), 1)
    pending_cnt = sum(
        1 for it in items if it["recommendation"] and it["recommendation"]["status"] == "PENDING"
    )

    return {
        "kpis": {
            "flights_evaluated": len(dossiers),
            "high_priority": high_cnt,
            "avg_freesale_lf_pct": avg_lf,
            "avg_pace_vs_curve_pct": avg_pace,
            "pending_approvals": pending_cnt,
        },
        "flights": items,
    }


@app.get("/api/dossier/{flight}/{departure_date}")
def api_dossier(flight: str, departure_date: str) -> dict:
    d = get_single_flight_dossier(flight, departure_date)
    if not d:
        raise HTTPException(status_code=404, detail="Flight not found")
    return d.model_dump(mode="json")


@app.post("/api/recommendations/{rec_id}/decide")
def api_decide(rec_id: int, body: DecisionInput) -> dict:
    conn = get_conn(read_only=False)
    try:
        existing = conn.execute("SELECT id, recommended_rbd, recommended_fare_lite_usd FROM recommendations WHERE id = ?", [rec_id]).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Recommendation not found")
        final_rbd = (body.rm_final_rbd or existing[1]).upper()
        final_fare = body.rm_final_fare_lite_usd if body.rm_final_fare_lite_usd is not None else existing[2]
        conn.execute(
            """
            UPDATE recommendations
            SET status = ?,
                rm_final_rbd = ?,
                rm_final_fare_lite_usd = ?,
                rm_notes = ?,
                decided_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [body.status, final_rbd, final_fare, body.rm_notes, rec_id],
        )
        return {"ok": True, "id": rec_id, "status": body.status, "rm_final_rbd": final_rbd, "rm_final_fare_lite_usd": final_fare}
    finally:
        conn.close()


@app.post("/api/simulate")
def api_simulate(payload: dict) -> dict:
    return simulate_action(
        flight=str(payload["flight"]),
        departure_date=str(payload["departure_date"]),
        proposed_rbd=str(payload["proposed_rbd"]),
        proposed_fare_lite_usd=float(payload["proposed_fare_lite_usd"]),
    )


@app.post("/api/refresh-baseline")
def api_refresh_baseline() -> dict:
    n = seed_deterministic_recommendations(limit=25)
    return {"ok": True, "refreshed": n}


@app.get("/api/backtest")
def api_backtest() -> list[dict]:
    return run_walkforward_backtest()


@app.get("/api/ledger")
def api_ledger() -> list[dict]:
    conn = get_conn(read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT id, flight, route, departure_date, current_depth, source,
                   action_type, recommended_rbd, recommended_fare_lite_usd,
                   status, rm_final_rbd, rm_final_fare_lite_usd, rm_notes, decided_at
            FROM recommendations
            WHERE status <> 'PENDING'
            ORDER BY decided_at DESC NULLS LAST
            LIMIT 50
            """
        ).fetchall()
        return [
            {
                "id": r[0], "flight": r[1], "route": r[2], "departure_date": str(r[3]),
                "dbd": r[4], "source": r[5], "action_type": r[6],
                "rec_rbd": r[7], "rec_fare": r[8], "status": r[9],
                "final_rbd": r[10], "final_fare": r[11], "notes": r[12] or "",
                "decided_at": str(r[13])[:19] if r[13] else "",
            }
            for r in rows
        ]
    finally:
        conn.close()


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>OneClick (4L) RM Decision Console</title>
<style>
  :root {
    --bg: #f6f8fc; --panel: #ffffff; --panel2: #f1f5fb; --border: #dbe3f0;
    --text: #0f172a; --muted: #64748b; --brand: #053699; --brand-hover: #042a78;
    --brand-soft: rgba(5, 54, 153, 0.08);
    --blue: #053699; --green: #15803d; --amber: #b45309; --red: #dc2626;
  }
  * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Inter", sans-serif; }
  body { margin: 0; background: var(--bg); color: var(--text); font-size: 13px; }
  header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 14px 24px; background: #ffffff;
    border-top: 4px solid var(--brand);
    border-bottom: 1px solid var(--border);
    box-shadow: 0 1px 3px rgba(5, 54, 153, 0.06);
  }
  header h1 { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: -0.01em; color: var(--brand); }
  header .sub { color: var(--muted); font-size: 12px; margin-left: 10px; font-weight: 400; }
  .toolbar { display: flex; gap: 10px; align-items: center; }
  select, input, button {
    background: #ffffff; color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 10px; font-size: 12px;
  }
  select:focus, input:focus { outline: none; border-color: var(--brand); box-shadow: 0 0 0 2px var(--brand-soft); }
  button { cursor: pointer; font-weight: 600; transition: all 0.15s; }
  button:hover { background: var(--panel2); border-color: var(--brand); }
  .btn-primary { background: var(--brand); color: #ffffff; border-color: var(--brand); }
  .btn-primary:hover { background: var(--brand-hover); border-color: var(--brand-hover); color: #ffffff; }
  .btn-approve { background: #16a34a; color: #ffffff; border-color: #15803d; }
  .btn-approve:hover { background: #15803d; color: #ffffff; }
  .btn-modify { background: #d97706; color: #ffffff; border-color: #b45309; }
  .btn-modify:hover { background: #b45309; color: #ffffff; }
  .btn-reject { background: #ffffff; color: #dc2626; border-color: #fecaca; }
  .btn-reject:hover { background: #fef2f2; border-color: #dc2626; }
  .kpis { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; padding: 16px 24px; }
  .kpi {
    background: var(--panel); border: 1px solid var(--border); border-top: 3px solid var(--brand);
    border-radius: 8px; padding: 12px 16px; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
  }
  .kpi .lbl { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; font-weight: 600; }
  .kpi .val { font-size: 22px; font-weight: 700; margin-top: 4px; color: var(--text); }
  main { display: grid; grid-template-columns: 1fr 390px; gap: 16px; padding: 0 24px 24px; }
  .cards { display: flex; flex-direction: column; gap: 12px; }
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    padding: 14px 16px; cursor: pointer; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .card:hover { border-color: #93b4f0; }
  .card.selected { border-color: var(--brand); box-shadow: 0 0 0 2px var(--brand-soft); }
  .card-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .badge { padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: 700; }
  .HIGH { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
  .MEDIUM { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }
  .LOW { background: var(--brand-soft); color: var(--brand); border: 1px solid rgba(5, 54, 153, 0.25); }
  .status-APPROVED { color: var(--green); font-weight: 700; }
  .status-MODIFIED { color: var(--amber); font-weight: 700; }
  .status-REJECTED { color: var(--red); font-weight: 700; }
  .status-PENDING { color: var(--brand); font-weight: 700; }
  .metrics-grid {
    display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px;
    background: #f8fafd; border: 1px solid #e6edf7; padding: 9px 12px; border-radius: 6px; margin-bottom: 10px;
  }
  .m-lbl { color: var(--muted); font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.02em; }
  .m-val { font-weight: 600; font-size: 13px; margin-top: 2px; color: var(--text); }
  .rec-box {
    display: flex; justify-content: space-between; align-items: center;
    background: var(--panel2); padding: 10px 12px; border-radius: 6px;
    border-left: 4px solid var(--brand);
  }
  .rationale { color: #334155; font-size: 12px; margin-top: 8px; line-height: 1.45; }
  .actions { display: flex; gap: 8px; margin-top: 10px; align-items: center; flex-wrap: wrap; }
  .drawer {
    background: var(--panel); border: 1px solid var(--border); border-top: 3px solid var(--brand);
    border-radius: 8px; padding: 16px; position: sticky; top: 16px;
    height: fit-content; max-height: calc(100vh - 32px); overflow-y: auto;
    box-shadow: 0 2px 6px rgba(5, 54, 153, 0.06);
  }
  .drawer h3 { color: var(--brand); }
  .sim-box { background: var(--panel2); border: 1px solid var(--border); padding: 10px; border-radius: 6px; margin-bottom: 14px; }
  table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; }
  th, td { text-align: left; padding: 6px 6px; border-bottom: 1px solid var(--border); }
  th { color: var(--brand); font-weight: 700; font-size: 11px; background: #f8fafd; }
  .open-yes { color: var(--green); font-weight: 700; }
  .open-no { color: var(--red); font-weight: 600; }
  .tabs { display: flex; gap: 6px; padding: 10px 24px 0; background: #ffffff; border-bottom: 1px solid var(--border); }
  .tab-btn { background: transparent; border: none; border-bottom: 3px solid transparent; border-radius: 0; padding: 8px 14px; font-size: 13px; font-weight: 600; color: var(--muted); }
  .tab-btn:hover { color: var(--brand); background: transparent; border-color: transparent; }
  .tab-btn.active { color: var(--brand); border-bottom-color: var(--brand); }
  .audit-wrap { padding: 16px 24px 28px; display: grid; grid-template-columns: 1fr; gap: 18px; }
  .audit-card { background: var(--panel); border: 1px solid var(--border); border-top: 3px solid var(--brand); border-radius: 8px; padding: 16px 20px; box-shadow: 0 1px 3px rgba(5, 54, 153, 0.05); }
  .audit-card h2 { margin: 0 0 4px; font-size: 15px; color: var(--brand); }
  .audit-sub { color: var(--muted); font-size: 12px; margin-bottom: 10px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>OneClick (4L) RM Decision Console <span class="sub">Freesale RBD & Target Fare Triage</span></h1>
  </div>
  <div class="toolbar">
    <select id="routeFilter" onchange="loadTriage()">
      <option value="">All Routes</option>
      <option value="TBS-TLV">TBS-TLV</option>
      <option value="TLV-TBS">TLV-TBS</option>
      <option value="BUS-TLV">BUS-TLV</option>
      <option value="TLV-BUS">TLV-BUS</option>
    </select>
    <select id="dbdFilter" onchange="loadTriage()">
      <option value="30">Next 30 Days</option>
      <option value="60" selected>Next 60 Days</option>
      <option value="120">Next 120 Days</option>
    </select>
    <button class="btn-primary" onclick="loadTriage()">Refresh View</button>
    <button onclick="refreshBaseline()" title="Recalculates deterministic engine cards without overwriting Claude/LLM recommendations">Recalc Baseline</button>
  </div>
</header>

<div class="tabs">
  <button class="tab-btn active" id="tabTriage" onclick="switchTab('triage')">Live Triage</button>
  <button class="tab-btn" id="tabAudit" onclick="switchTab('audit')">Backtest &amp; Decision Audit</button>
</div>

<div id="viewTriage">
  <section class="kpis" id="kpiStrip"></section>
  <main>
    <div class="cards" id="flightCards"></div>
    <aside class="drawer" id="detailDrawer">
      <div style="color:var(--muted)">Click any flight card on the left to inspect its EMSR-b protection ladder, competitor offers, and What-If simulator.</div>
    </aside>
  </main>
</div>

<div id="viewAudit" class="audit-wrap" style="display:none">
  <div class="audit-card">
    <h2>Walk-Forward Out-of-Sample Forecast Backtest</h2>
    <div class="audit-sub">Strictly leave-future-out validation on departed flights: rolling 12-flight additive pickup trained only on earlier departures.</div>
    <table>
      <thead>
        <tr>
          <th>Route</th><th>Depth</th><th>Test Flights</th><th>Avg Actual Final Pax</th>
          <th>MAE (seats)</th><th>Bias (Pred - Act)</th><th>Within ±10 seats</th><th>Within ±15 seats</th>
        </tr>
      </thead>
      <tbody id="backtestBody"></tbody>
    </table>
  </div>
  <div class="audit-card">
    <h2>RM Decision Ledger (Overrides &amp; Approvals)</h2>
    <div class="audit-sub">All Approved, Modified, and Rejected recommendations logged for audit &amp; agent learning.</div>
    <table>
      <thead>
        <tr>
          <th>Flight</th><th>Route</th><th>Dep Date</th><th>DBD</th><th>Source</th>
          <th>Proposed</th><th>RM Status</th><th>Final RBD / $</th><th>RM Notes</th><th>Decided At</th>
        </tr>
      </thead>
      <tbody id="ledgerBody"></tbody>
    </table>
  </div>
</div>

<script>
let FLIGHTS = [];
let SELECTED_IDX = 0;

function switchTab(tab) {
  document.getElementById('viewTriage').style.display = tab === 'triage' ? 'block' : 'none';
  document.getElementById('viewAudit').style.display = tab === 'audit' ? 'grid' : 'none';
  document.getElementById('tabTriage').classList.toggle('active', tab === 'triage');
  document.getElementById('tabAudit').classList.toggle('active', tab === 'audit');
  if (tab === 'audit') loadAudit();
}

async function loadAudit() {
  const [btRes, ledRes] = await Promise.all([fetch('/api/backtest'), fetch('/api/ledger')]);
  const bt = await btRes.json();
  const led = await ledRes.json();
  document.getElementById('backtestBody').innerHTML = bt.map(r => `
    <tr>
      <td><strong>${r.route}</strong></td>
      <td>D-${r.dbd}</td>
      <td>${r.n_test}</td>
      <td>${r.avg_actual_final_pax}</td>
      <td><strong>${r.mae_pax}</strong></td>
      <td style="color:${Math.abs(r.bias_pax)<=3?'var(--green)':'var(--amber)'}">${r.bias_pax>0?'+':''}${r.bias_pax}</td>
      <td>${r.within_10_pct}%</td>
      <td><strong>${r.within_15_pct}%</strong></td>
    </tr>
  `).join('') || '<tr><td colspan="8" style="color:var(--muted)">Not enough departed flights yet</td></tr>';

  document.getElementById('ledgerBody').innerHTML = led.map(r => `
    <tr>
      <td><strong>${r.flight}</strong></td>
      <td>${r.route}</td>
      <td>${r.departure_date}</td>
      <td>D-${r.dbd}</td>
      <td><em>${r.source}</em></td>
      <td>${r.action_type} &rarr; ${r.rec_rbd} ($${r.rec_fare})</td>
      <td class="status-${r.status}">${r.status}</td>
      <td><strong>${r.final_rbd} ($${r.final_fare})</strong></td>
      <td>${r.notes || '—'}</td>
      <td style="color:var(--muted)">${r.decided_at}</td>
    </tr>
  `).join('') || '<tr><td colspan="10" style="color:var(--muted)">No RM decisions logged yet — Approve, Modify, or Reject a card in Live Triage.</td></tr>';
}

async function loadTriage() {
  const route = document.getElementById('routeFilter').value;
  const maxDbd = document.getElementById('dbdFilter').value;
  const res = await fetch(`/api/triage?limit=30&max_dbd=${maxDbd}&route=${encodeURIComponent(route)}`);
  const data = await res.json();
  FLIGHTS = data.flights;
  const k = data.kpis;
  document.getElementById('kpiStrip').innerHTML = `
    <div class="kpi"><div class="lbl">Evaluated Flights</div><div class="val">${k.flights_evaluated}</div></div>
    <div class="kpi"><div class="lbl">High Priority Alerts</div><div class="val" style="color:var(--red)">${k.high_priority}</div></div>
    <div class="kpi"><div class="lbl">Avg Freesale LF</div><div class="val">${k.avg_freesale_lf_pct}%</div></div>
    <div class="kpi"><div class="lbl">Avg Pace vs Curve</div><div class="val" style="color:${k.avg_pace_vs_curve_pct>=0?'var(--green)':'var(--amber)'}">${k.avg_pace_vs_curve_pct>0?'+':''}${k.avg_pace_vs_curve_pct}%</div></div>
    <div class="kpi"><div class="lbl">Pending RM Review</div><div class="val" style="color:var(--blue)">${k.pending_approvals}</div></div>
  `;
  renderCards();
  if (FLIGHTS.length > 0) selectFlight(Math.min(SELECTED_IDX, FLIGHTS.length - 1));
}

function renderCards() {
  const el = document.getElementById('flightCards');
  el.innerHTML = FLIGHTS.map((f, idx) => {
    const rec = f.recommendation || {};
    const comp = f.competitors.exact_date_nonstop_min_usd
      ? `$${f.competitors.exact_date_nonstop_min_usd} (${f.competitors.exact_date_nonstop_airline})`
      : (f.competitors.window_pm2d_nonstop_min_usd ? `~$${f.competitors.window_pm2d_nonstop_min_usd} (±2d)` : '—');
    const paceColor = f.pace_vs_benchmark_pct >= 0 ? 'var(--green)' : (f.pace_vs_benchmark_pct <= -20 ? 'var(--red)' : 'var(--amber)');
    return `
      <div class="card ${idx===SELECTED_IDX?'selected':''}" onclick="selectFlight(${idx})">
        <div class="card-top">
          <div>
            <strong style="font-size:15px">${f.flight}</strong> &middot; <strong>${f.route}</strong> &middot;
            <span>${f.departure_date} (${f.weekday})</span> &middot;
            <span style="color:var(--blue);font-weight:600">D-${f.current_depth}</span>
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="status-${rec.status||'PENDING'}">${rec.status||'NO REC'}</span>
            <span class="badge ${f.priority_level}">${f.priority_level} (${f.priority_score})</span>
          </div>
        </div>

        <div class="metrics-grid">
          <div><div class="m-lbl">Freesale Sold / Net Cap</div><div class="m-val">${f.on_hand_freesale_pax} / ${f.net_sellable_capacity} <span style="color:var(--muted);font-weight:400">(${f.blocked_seats} blk)</span></div></div>
          <div><div class="m-lbl">Freesale LF (Pace)</div><div class="m-val">${f.freesale_lf_pct}% <span style="color:${paceColor}">(${f.pace_vs_benchmark_pct>0?'+':''}${f.pace_vs_benchmark_pct}%)</span></div></div>
          <div><div class="m-lbl">Forecast Final LF</div><div class="m-val">${f.forecast_final_freesale_lf_pct}% (${Math.round(f.forecast_final_freesale_pax)}p)</div></div>
          <div><div class="m-lbl">EMSR-b Bid Price</div><div class="m-val">$${f.emsr_bid_price_usd} (${f.emsr_recommended_rbd})</div></div>
          <div><div class="m-lbl">Nonstop Competitor</div><div class="m-val">${comp}</div></div>
        </div>

        ${rec.id ? `
        <div class="rec-box">
          <div>
            <div style="font-size:11px;color:var(--muted)">Current Est: <strong>${rec.current_rbd} ($${rec.current_fare_lite_usd})</strong> &rarr; Source: <em>${rec.source}</em></div>
            <div style="font-size:14px;font-weight:700;margin-top:2px">
              ${rec.action_type}: Floor RBD <span style="color:var(--blue)">${rec.recommended_rbd}</span> &middot;
              Click Lite <span style="color:var(--green)">$${rec.recommended_fare_lite_usd}</span> / ClickStand <span>$${rec.recommended_fare_stand_usd}</span>
            </div>
          </div>
          <div><span style="color:var(--muted);font-size:11px">Conf: ${Math.round((rec.confidence||0)*100)}%</span></div>
        </div>
        <div class="rationale">${rec.rationale}</div>
        <div class="actions" onclick="event.stopPropagation()">
          <button class="btn-approve" onclick="decide(${rec.id}, 'APPROVED', '${rec.recommended_rbd}', ${rec.recommended_fare_lite_usd})">Approve</button>
          <input type="text" id="rbd_${rec.id}" value="${rec.rm_final_rbd || rec.recommended_rbd}" style="width:48px;text-align:center" title="Override RBD">
          <input type="number" id="fare_${rec.id}" value="${rec.rm_final_fare_lite_usd || rec.recommended_fare_lite_usd}" style="width:75px" title="Override Click Lite USD">
          <input type="text" id="note_${rec.id}" placeholder="RM override reason / note..." value="${rec.rm_notes||''}" style="flex:1;min-width:140px">
          <button class="btn-modify" onclick="decideCustom(${rec.id}, 'MODIFIED')">Modify</button>
          <button class="btn-reject" onclick="decideCustom(${rec.id}, 'REJECTED')">Reject</button>
        </div>
        ` : ''}
      </div>
    `;
  }).join('');
}

function selectFlight(idx) {
  SELECTED_IDX = idx;
  renderCards();
  const f = FLIGHTS[idx];
  if (!f) return;
  const rows = f.rbd_protection_table.map(r => `
    <tr>
      <td><strong>${r.rbd}</strong> <span style="color:var(--muted)">(${r.label})</span></td>
      <td>$${r.lite_usd}</td>
      <td>$${r.stand_usd}</td>
      <td>${r.protected_seats}</td>
      <td class="${r.recommended_open?'open-yes':'open-no'}">${r.recommended_open?'OPEN':'CLOSED'}</td>
    </tr>
  `).join('');

  const comps = (f.competitors.offers || []).map(c => `
    <tr><td>${c.airline}</td><td>${c.stops}</td><td>$${c.min_price_usd}</td><td>${c.move_1d_pct!=null ? c.move_1d_pct+'%' : '—'}</td></tr>
  `).join('') || '<tr><td colspan="4" style="color:var(--muted)">No exact-date competitor rows</td></tr>';

  document.getElementById('detailDrawer').innerHTML = `
    <h3 style="margin:0 0 4px">${f.flight} (${f.route}) &middot; ${f.departure_date}</h3>
    <div style="color:var(--muted);margin-bottom:12px">Remaining Freesale Seats: <strong>${f.remaining_freesale_seats}</strong> | Expected Pickup: <strong>${f.rem_pickup_mean} ± ${f.rem_pickup_sd}</strong></div>

    <div class="sim-box">
      <div style="font-weight:700;color:var(--brand);margin-bottom:6px">What-If Simulator</div>
      <div style="display:flex;gap:6px;align-items:center">
        <input id="simRbd" value="${f.emsr_recommended_rbd}" style="width:48px;text-align:center">
        <input id="simFare" type="number" value="${f.smooth_target_fare_lite_usd}" style="width:80px">
        <button class="btn-primary" onclick="runSim('${f.flight}', '${f.departure_date}')">Simulate</button>
      </div>
      <div id="simOut" style="margin-top:8px;font-size:12px;color:#334155"></div>
    </div>

    <div style="font-weight:700;margin-bottom:4px">EMSR-b RBD Protection Table</div>
    <table>
      <thead><tr><th>RBD</th><th>Lite</th><th>Stand</th><th>Prot.</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>

    <div style="font-weight:700;margin:14px 0 4px">Competitor Market Snapshot</div>
    <table>
      <thead><tr><th>Airline</th><th>Stops</th><th>Min $</th><th>1d Δ</th></tr></thead>
      <tbody>${comps}</tbody>
    </table>
  `;
}

async function decide(id, status, rbd, fare, notes='') {
  await fetch(`/api/recommendations/${id}/decide`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status, rm_final_rbd: rbd, rm_final_fare_lite_usd: Number(fare), rm_notes: notes})
  });
  await loadTriage();
}

async function decideCustom(id, status) {
  const rbd = document.getElementById(`rbd_${id}`).value;
  const fare = document.getElementById(`fare_${id}`).value;
  const notes = document.getElementById(`note_${id}`).value;
  await decide(id, status, rbd, fare, notes);
}

async function runSim(flight, departure_date) {
  const proposed_rbd = document.getElementById('simRbd').value;
  const proposed_fare_lite_usd = Number(document.getElementById('simFare').value);
  const res = await fetch('/api/simulate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({flight, departure_date, proposed_rbd, proposed_fare_lite_usd})
  });
  const s = await res.json();
  const deltaSign = s.expected_revenue_delta_usd >= 0 ? '+' : '';
  const warn = (s.guardrails.warnings || []).join(' ');
  document.getElementById('simOut').innerHTML = `
    Est. Remaining Pickup: <strong>${s.expected_remaining_pax_simulated} pax</strong> (vs ${s.expected_remaining_pax_baseline} base)<br>
    Est. Remaining Revenue: <strong>$${s.expected_remaining_rev_simulated_usd}</strong> (<strong>${deltaSign}$${s.expected_revenue_delta_usd}</strong> vs current)<br>
    ${warn ? `<span style="color:var(--amber)">${warn}</span>` : '<span style="color:var(--green)">All guardrails passed.</span>'}
  `;
}

async function refreshBaseline() {
  await fetch('/api/refresh-baseline', {method: 'POST'});
  await loadTriage();
}

loadTriage();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


def main() -> None:
    uvicorn.run("rm_agent.web:app", host="127.0.0.1", port=8080, reload=False)


if __name__ == "__main__":
    main()
