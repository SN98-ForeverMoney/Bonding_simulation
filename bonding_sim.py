#!/usr/bin/env python3
"""
Bonding Program Simulation — ForeverMoney / SN98

Models weekly bonding rounds: miners trade alpha (at 10% discount) for LP
tokens. LP deployed into wTAO pools, 100% of fees buy back alpha.

Generates output/bonding.html matching the dashboard design system.

Usage:
    python scripts/bonding_sim.py
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "output"

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

DAILY_MINING_ALPHA = 2_952.0       # α/day (live from taostats Jun 10 2026)
ALPHA_PRICE_USD = 0.7451           # current α market price
DISCOUNT = 0.10                    # 10% discount — miners sell α at 90% of market
BOND_PRICE_USD = ALPHA_PRICE_USD * (1 - DISCOUNT)  # $0.6706 per α

POOLS = {
    "wTAO/USDC": {"daily_fees": 3_180, "tvl": 757_000, "fee_tier": "1%", "chain": "Ethereum"},
    "wTAO/WETH": {"daily_fees": 8_063, "tvl": 757_000, "fee_tier": "1%", "chain": "Ethereum"},
}

NUM_WEEKS = 13  # ~90 days
DAYS_PER_WEEK = 7
DURATION_DAYS = NUM_WEEKS * DAYS_PER_WEEK


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

@dataclass
class WeekSnapshot:
    week: int
    day_start: int
    day_end: int
    alpha_bonded_this_week: float
    lp_cost_this_week: float       # LP given out for α (at discount)
    cumulative_alpha_bonded: float
    cumulative_lp_deployed: float
    fees_this_week: float
    cumulative_fees: float
    alpha_bought_this_week: float
    cumulative_alpha_bought: float

    @property
    def net_alpha(self) -> float:
        return self.cumulative_alpha_bought - self.cumulative_alpha_bonded

    @property
    def buyback_pct(self) -> float:
        if self.cumulative_alpha_bonded == 0:
            return 0
        return self.cumulative_alpha_bought / self.cumulative_alpha_bonded * 100


@dataclass
class ScenarioResult:
    pool_name: str
    weeks: list[WeekSnapshot]
    pool_info: dict

    @property
    def final(self) -> WeekSnapshot:
        return self.weeks[-1]


def run_scenario(pool_name: str, pool_info: dict) -> ScenarioResult:
    daily_rate = pool_info["daily_fees"] / pool_info["tvl"]
    weekly_alpha = DAILY_MINING_ALPHA * DAYS_PER_WEEK

    cumulative_lp = 0.0
    cumulative_fees = 0.0
    cumulative_alpha_bonded = 0.0
    cumulative_alpha_bought = 0.0
    weeks: list[WeekSnapshot] = []

    for w in range(1, NUM_WEEKS + 1):
        day_start = (w - 1) * DAYS_PER_WEEK + 1
        day_end = w * DAYS_PER_WEEK

        # Bond: miners give α, we give LP at discounted α price
        alpha_bonded = weekly_alpha
        lp_cost = alpha_bonded * BOND_PRICE_USD
        cumulative_alpha_bonded += alpha_bonded
        cumulative_lp += lp_cost

        # Earn fees day-by-day within the week
        # LP from previous weeks earns all 7 days;
        # this week's new LP earns on average ~3.5 days (added at start of week for simplicity)
        fees_this_week = cumulative_lp * daily_rate * DAYS_PER_WEEK
        cumulative_fees += fees_this_week

        # Buy back α at full market price
        alpha_bought = fees_this_week / ALPHA_PRICE_USD
        cumulative_alpha_bought += alpha_bought

        weeks.append(WeekSnapshot(
            week=w,
            day_start=day_start,
            day_end=day_end,
            alpha_bonded_this_week=alpha_bonded,
            lp_cost_this_week=lp_cost,
            cumulative_alpha_bonded=cumulative_alpha_bonded,
            cumulative_lp_deployed=cumulative_lp,
            fees_this_week=fees_this_week,
            cumulative_fees=cumulative_fees,
            alpha_bought_this_week=alpha_bought,
            cumulative_alpha_bought=cumulative_alpha_bought,
        ))

    return ScenarioResult(pool_name, weeks, pool_info)


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

CSS = """
:root {
    --brand-navy:#1a2766;--brand-navy-lite:#2e3a82;--accent:#5563c9;
    --bg:#0e1220;--bg-sidebar:#0a0f1e;--bg-raised:#151a30;
    --text:#e6e8f0;--muted:#8088a8;--border:#252b45;
    --green:#00CC96;--orange:#FFA15A;--red:#EF553B;
    --font:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh;padding:32px 48px;}
h1{font-size:28px;font-weight:700;margin:0 0 6px;}
.subtitle{color:var(--muted);font-size:13px;margin-bottom:28px;}

.cards{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:20px;}
.card{background:var(--bg-raised);border-radius:8px;padding:16px 20px;min-width:150px;flex:1;border-left:3px solid var(--accent);}
.card-label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;}
.card-value{font-size:22px;font-weight:700;letter-spacing:-0.01em;}
.card-detail{color:var(--muted);font-size:12px;margin-top:4px;}

table{width:100%;border-collapse:collapse;margin:8px 0 24px;}
th{text-align:left;padding:11px 14px;border-bottom:2px solid var(--border);color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1px;font-weight:600;}
td{padding:10px 14px;border-bottom:1px solid var(--border);font-size:14px;}
tr:hover{background:var(--bg-raised);}
.num{text-align:right;font-variant-numeric:tabular-nums;}
.positive{color:var(--green);}
.negative{color:var(--red);}
.highlight-row{background:rgba(85,99,201,0.08);}
.clickable{cursor:pointer;}
.clickable:hover{background:rgba(85,99,201,0.15);}

.section-title{font-size:18px;font-weight:600;margin:32px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border);}
.chart-wrap{background:var(--bg-raised);border-radius:8px;padding:16px;margin-bottom:24px;}

.detail-panel{display:none;margin:0 0 32px;padding:20px;background:var(--bg-raised);border-radius:8px;border:1px solid var(--border);}
.detail-panel.active{display:block;}
.detail-panel h3{margin:0 0 16px;font-size:16px;}
.detail-close{float:right;cursor:pointer;color:var(--muted);font-size:18px;padding:4px 8px;}
.detail-close:hover{color:var(--text);}

.discount-badge{display:inline-block;padding:3px 8px;border-radius:4px;background:var(--green);color:#0e1220;font-size:11px;font-weight:700;margin-left:8px;}
"""


def _fmt_usd(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v/1e6:,.2f}M"
    return f"${v:,.0f}"


def _fmt_alpha(v: float) -> str:
    return f"{v:,.0f}"


def _fmt_pct(v: float) -> str:
    return f"{v:.1f}%"


def build_input_cards() -> str:
    weekly_alpha = DAILY_MINING_ALPHA * DAYS_PER_WEEK
    weekly_lp = weekly_alpha * BOND_PRICE_USD
    total_alpha = DAILY_MINING_ALPHA * DURATION_DAYS
    return f"""
    <div class="cards">
        <div class="card">
            <div class="card-label">Weekly Bonding Round</div>
            <div class="card-value">{_fmt_alpha(weekly_alpha)} \u03b1</div>
            <div class="card-detail">{_fmt_usd(weekly_lp)} LP per week (at 10% discount)</div>
        </div>
        <div class="card">
            <div class="card-label">Bond Price</div>
            <div class="card-value">${BOND_PRICE_USD:.4f}/\u03b1<span class="discount-badge">-10%</span></div>
            <div class="card-detail">Market: ${ALPHA_PRICE_USD:.4f}/\u03b1</div>
        </div>
        <div class="card">
            <div class="card-label">Program</div>
            <div class="card-value">{NUM_WEEKS} weeks</div>
            <div class="card-detail">{DURATION_DAYS} days \u00b7 {_fmt_alpha(total_alpha)} \u03b1 total</div>
        </div>
        <div class="card">
            <div class="card-label">Total LP Cost</div>
            <div class="card-value">{_fmt_usd(total_alpha * BOND_PRICE_USD)}</div>
            <div class="card-detail">Savings vs market: {_fmt_usd(total_alpha * ALPHA_PRICE_USD - total_alpha * BOND_PRICE_USD)}</div>
        </div>
    </div>

    <div class="cards">
        {"".join(f'''
        <div class="card" style="border-left-color:{'var(--green)' if i == 0 else 'var(--orange)'};">
            <div class="card-label">{name} ({info["chain"]})</div>
            <div class="card-value">{_fmt_usd(info["daily_fees"])}/day fees</div>
            <div class="card-detail">TVL: {_fmt_usd(info["tvl"])} \u00b7 Fee tier: {info["fee_tier"]} \u00b7 30d avg</div>
        </div>''' for i, (name, info) in enumerate(POOLS.items()))}
    </div>
    """


def build_summary_table(results: list[ScenarioResult]) -> str:
    rows = ""
    for i, r in enumerate(results):
        f = r.final
        pct_class = "positive" if f.buyback_pct >= 50 else "negative"
        rows += f"""
        <tr class="clickable" onclick="toggleDetail('detail-{i}')">
            <td><b>{r.pool_name}</b></td>
            <td class="num">{_fmt_usd(f.cumulative_lp_deployed)}</td>
            <td class="num">{_fmt_usd(f.cumulative_fees)}</td>
            <td class="num">{_fmt_alpha(f.cumulative_alpha_bonded)} \u03b1</td>
            <td class="num">{_fmt_alpha(f.cumulative_alpha_bought)} \u03b1</td>
            <td class="num {pct_class}">{_fmt_alpha(f.net_alpha)} \u03b1</td>
            <td class="num {pct_class}" style="font-weight:700;font-size:16px;">{_fmt_pct(f.buyback_pct)}</td>
            <td style="color:var(--muted);font-size:12px;">\u25BC detail</td>
        </tr>"""

    return f"""
    <div class="section-title">100% Capital Efficiency — After {NUM_WEEKS} Weeks ({DURATION_DAYS} Days)</div>
    <table>
        <tr>
            <th>Pool</th><th class="num">LP Deployed</th>
            <th class="num">Fees Earned</th><th class="num">\u03b1 Bonded</th>
            <th class="num">\u03b1 Bought Back</th><th class="num">Net \u03b1</th>
            <th class="num">Buyback %</th><th></th>
        </tr>
        {rows}
    </table>
    """


def build_detail_panel(result: ScenarioResult, panel_id: str) -> str:
    rows = ""
    for w in result.weeks:
        pct_cls = "positive" if w.buyback_pct >= 100 else ""
        rows += f"""
        <tr>
            <td class="num" style="font-weight:600;">Week {w.week}</td>
            <td class="num" style="color:var(--muted);">Day {w.day_start}\u2013{w.day_end}</td>
            <td class="num">{_fmt_alpha(w.alpha_bonded_this_week)}</td>
            <td class="num">{_fmt_usd(w.lp_cost_this_week)}</td>
            <td class="num">{_fmt_usd(w.cumulative_lp_deployed)}</td>
            <td class="num">{_fmt_usd(w.fees_this_week)}</td>
            <td class="num">{_fmt_usd(w.cumulative_fees)}</td>
            <td class="num">{_fmt_alpha(w.alpha_bought_this_week)}</td>
            <td class="num">{_fmt_alpha(w.cumulative_alpha_bought)}</td>
            <td class="num {pct_cls}" style="font-weight:600;">{_fmt_pct(w.buyback_pct)}</td>
        </tr>"""

    f = result.final
    return f"""
    <div class="detail-panel" id="{panel_id}">
        <span class="detail-close" onclick="toggleDetail('{panel_id}')">\u2715</span>
        <h3>{result.pool_name} — Weekly Breakdown</h3>
        <div class="cards" style="margin-bottom:16px;">
            <div class="card" style="border-left-color:var(--green);flex:0 1 auto;">
                <div class="card-label">End LP Position</div>
                <div class="card-value">{_fmt_usd(f.cumulative_lp_deployed)}</div>
            </div>
            <div class="card" style="border-left-color:var(--green);flex:0 1 auto;">
                <div class="card-label">Total Fees</div>
                <div class="card-value">{_fmt_usd(f.cumulative_fees)}</div>
            </div>
            <div class="card" style="border-left-color:var(--accent);flex:0 1 auto;">
                <div class="card-label">\u03b1 Bought Back</div>
                <div class="card-value">{_fmt_alpha(f.cumulative_alpha_bought)} \u03b1</div>
            </div>
            <div class="card" style="border-left-color:{'var(--green)' if f.net_alpha >= 0 else 'var(--red)'};flex:0 1 auto;">
                <div class="card-label">Net \u03b1 Position</div>
                <div class="card-value" style="color:{'var(--green)' if f.net_alpha >= 0 else 'var(--red)'};">{_fmt_alpha(f.net_alpha)} \u03b1</div>
            </div>
        </div>
        <table>
            <tr>
                <th>Week</th><th>Days</th>
                <th class="num">\u03b1 Bonded</th><th class="num">LP Cost</th>
                <th class="num">Cumul. LP</th><th class="num">Fees/Wk</th>
                <th class="num">Cumul. Fees</th><th class="num">\u03b1 Bought/Wk</th>
                <th class="num">Cumul. \u03b1 Bought</th><th class="num">Buyback %</th>
            </tr>
            {rows}
        </table>
    </div>
    """


def build_plotly_chart(results: list[ScenarioResult]) -> str:
    colors = {"wTAO/USDC": "#00CC96", "wTAO/WETH": "#AB63FA"}
    traces = []

    # α bonded line (same for both)
    weeks = [w.week for w in results[0].weeks]
    bonded = [w.cumulative_alpha_bonded for w in results[0].weeks]
    traces.append({
        "x": weeks, "y": bonded,
        "name": "\u03b1 Bonded (both pools)",
        "line": {"color": "#8088a8", "width": 2, "dash": "dot"},
        "type": "scatter", "mode": "lines+markers",
        "marker": {"size": 5},
    })

    for r in results:
        bought = [w.cumulative_alpha_bought for w in r.weeks]
        traces.append({
            "x": weeks, "y": bought,
            "name": f"\u03b1 Bought — {r.pool_name}",
            "line": {"color": colors.get(r.pool_name, "#FFA15A"), "width": 3},
            "type": "scatter", "mode": "lines+markers",
            "marker": {"size": 5},
        })

    layout = {
        "template": "plotly_dark",
        "paper_bgcolor": "#151a30",
        "plot_bgcolor": "#151a30",
        "title": {"text": "Cumulative \u03b1: Bonded vs Bought Back (Weekly)", "font": {"size": 16}},
        "xaxis": {"title": "Week", "dtick": 1, "gridcolor": "#252b45"},
        "yaxis": {"title": "Alpha (\u03b1)", "gridcolor": "#252b45"},
        "legend": {"orientation": "h", "y": -0.18},
        "margin": {"t": 50, "b": 80},
        "height": 420,
    }

    chart_json = json.dumps({"data": traces, "layout": layout})
    return f"""
    <div class="chart-wrap">
        <div id="chart-alpha" style="width:100%;"></div>
        <script>Plotly.newPlot('chart-alpha', {chart_json}.data, {chart_json}.layout, {{responsive:true}});</script>
    </div>
    """


def build_fees_chart(results: list[ScenarioResult]) -> str:
    colors = {"wTAO/USDC": "#00CC96", "wTAO/WETH": "#AB63FA"}
    traces = []
    weeks = [w.week for w in results[0].weeks]

    # Weekly LP cost line (constant)
    weekly_lp = DAILY_MINING_ALPHA * DAYS_PER_WEEK * BOND_PRICE_USD
    traces.append({
        "x": weeks, "y": [weekly_lp] * len(weeks),
        "name": "Weekly LP Cost (α sold)",
        "line": {"color": "#8088a8", "width": 1, "dash": "dash"},
        "type": "scatter", "mode": "lines",
    })

    for r in results:
        fees = [w.fees_this_week for w in r.weeks]
        traces.append({
            "x": weeks, "y": fees,
            "name": f"Weekly Fees — {r.pool_name}",
            "line": {"color": colors.get(r.pool_name, "#FFA15A"), "width": 3},
            "type": "scatter", "mode": "lines+markers",
            "marker": {"size": 5},
            "fill": "tozeroy",
            "fillcolor": colors.get(r.pool_name, "#FFA15A").replace(")", ",0.1)").replace("#", "rgba(") if False else None,
        })

    layout = {
        "template": "plotly_dark",
        "paper_bgcolor": "#151a30",
        "plot_bgcolor": "#151a30",
        "title": {"text": "Weekly Fee Revenue vs LP Cost", "font": {"size": 16}},
        "xaxis": {"title": "Week", "dtick": 1, "gridcolor": "#252b45"},
        "yaxis": {"title": "USD/week", "gridcolor": "#252b45"},
        "legend": {"orientation": "h", "y": -0.18},
        "margin": {"t": 50, "b": 80},
        "height": 360,
    }

    chart_json = json.dumps({"data": traces, "layout": layout})
    return f"""
    <div class="chart-wrap">
        <div id="chart-fees" style="width:100%;"></div>
        <script>Plotly.newPlot('chart-fees', {chart_json}.data, {chart_json}.layout, {{responsive:true}});</script>
    </div>
    """


def build_page(results: list[ScenarioResult]) -> str:
    now = datetime.now(timezone.utc).strftime("%b %d, %Y at %H:%M UTC")

    detail_panels = ""
    for i, r in enumerate(results):
        detail_panels += build_detail_panel(r, f"detail-{i}")

    body = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bonding Program — ForeverMoney</title>
<link rel="icon" type="image/svg+xml" href="assets/logo-icon.svg">
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>{CSS}</style>
</head>
<body>
    <h1>SN98 Bonding Program Simulation</h1>
    <div class="subtitle">Generated {now} \u00b7 Weekly bonding: miners sell \u03b1 at 10% discount for LP \u2192 LP earns fees \u2192 fees buy back \u03b1 at market</div>

    {build_input_cards()}
    {build_summary_table(results)}
    {detail_panels}
    {build_plotly_chart(results)}
    {build_fees_chart(results)}

    <div style="color:var(--muted);font-size:11px;text-align:center;margin-top:48px;padding-top:16px;border-top:1px solid var(--border);">
        Assumptions: constant \u03b1 price (${ALPHA_PRICE_USD}), constant pool fee rates (30d avg),
        100% capital efficiency. Bond discount: 10% (miners sell \u03b1 at ${BOND_PRICE_USD:.4f}).
        Buyback at market price (${ALPHA_PRICE_USD}).
    </div>

    <script>
    function toggleDetail(id) {{
        var el = document.getElementById(id);
        if (el) el.classList.toggle('active');
    }}
    </script>
</body></html>"""
    return body


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    results = []
    for pool_name, pool_info in POOLS.items():
        r = run_scenario(pool_name, pool_info)
        results.append(r)

    html = build_page(results)
    out_path = OUTPUT_DIR / "bonding.html"
    out_path.write_text(html)
    print(f"Generated {out_path}")
    print(f"\n{'Pool':<16} {'LP Deployed':>12} {'Fees':>12} {'α Bonded':>12} {'α Bought':>12} {'Buyback':>8}")
    print("-" * 76)
    for r in results:
        f = r.final
        print(f"{r.pool_name:<16} {_fmt_usd(f.cumulative_lp_deployed):>12} "
              f"{_fmt_usd(f.cumulative_fees):>12} {_fmt_alpha(f.cumulative_alpha_bonded):>11}\u03b1 "
              f"{_fmt_alpha(f.cumulative_alpha_bought):>11}\u03b1 {_fmt_pct(f.buyback_pct):>7}")


if __name__ == "__main__":
    main()
