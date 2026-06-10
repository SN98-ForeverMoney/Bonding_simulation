#!/usr/bin/env python3
"""
Emissions Payback Simulation — ForeverMoney / SN98

Models the business case: sell miner alpha emissions at 1x/1.1x for LP tokens,
deploy into ForeverMoney vaults earning fees, track how quickly fees pay back
the alpha sold.

Usage:
    python scripts/emissions_sim.py                     # auto-detect best vault, live data
    python scripts/emissions_sim.py --list-vaults       # show available vaults
    python scripts/emissions_sim.py --multiplier 1.1    # 10% premium on alpha
    python scripts/emissions_sim.py --target "xTAO/USDC Protocol 4"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "output"

NANO = 1e9
NETUID = 98
TAOSTATS_BASE = "https://api.taostats.io/api"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd"

# Rate limit: >= 5s between taostats calls
_last_call_ts = 0.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class VaultInfo:
    pair: str
    vault_type: str
    capital_usd: float
    fees_usd: float
    days_active: int
    efficiency: float
    apr_7d: float | None
    apr_30d: float | None
    apr_all: float | None
    vault_name: str = ""

    @property
    def display_id(self) -> str:
        return f"{self.pair} {self.vault_name}".strip()

    def apr_for_window(self, window: str) -> float | None:
        return {"7d": self.apr_7d, "30d": self.apr_30d, "all": self.apr_all}.get(window)


@dataclass
class SimConfig:
    daily_miner_alpha: float
    alpha_price_usd: float
    alpha_price_tao: float
    tao_usd: float
    exchange_multiplier: float
    apr: float
    max_days: int


@dataclass
class DaySnapshot:
    day: int
    cumulative_alpha_sold_usd: float
    cumulative_lp_deployed: float
    cumulative_fees_earned: float

    @property
    def net_pnl(self) -> float:
        return self.cumulative_fees_earned - self.cumulative_alpha_sold_usd


# ---------------------------------------------------------------------------
# HTML parser for output/vaults.html
# ---------------------------------------------------------------------------

class _VaultsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.vaults: list[VaultInfo] = []
        self._attrs: dict | None = None
        self._in_row = False
        self._td_count = 0
        self._capture_name = False
        self._name_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict(attrs)
        if tag == "tr" and "data-pair" in d:
            self._attrs = d
            self._in_row = True
            self._td_count = 0
            self._name_parts = []
        elif tag == "td" and self._in_row:
            self._td_count += 1
            if self._td_count == 2:
                self._capture_name = True
                self._name_parts = []
        elif tag == "span" and self._capture_name:
            self._capture_name = False

    def handle_data(self, data: str) -> None:
        if self._capture_name:
            self._name_parts.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._in_row and self._attrs:
            a = self._attrs
            self.vaults.append(VaultInfo(
                pair=a.get("data-pair", ""),
                vault_type=a.get("data-vault-type", ""),
                capital_usd=_float(a.get("data-capital", "0")),
                fees_usd=_float(a.get("data-fees", "0")),
                days_active=int(_float(a.get("data-days-active", "0"))),
                efficiency=_float(a.get("data-eff", "0")),
                apr_7d=_float_or_none(a.get("data-apr-7d")),
                apr_30d=_float_or_none(a.get("data-apr-30d")),
                apr_all=_float_or_none(a.get("data-apr-all")),
                vault_name=" ".join(self._name_parts).strip(),
            ))
            self._in_row = False
            self._attrs = None
            self._capture_name = False


def _float(s: str) -> float:
    try:
        v = float(s)
        return 0.0 if v != v or v == float("inf") or v == float("-inf") else v
    except (ValueError, TypeError):
        return 0.0


def _float_or_none(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    v = _float(s)
    return v if v != 0.0 or s == "0" or s == "0.0" else None


def parse_vaults(path: Path | None = None) -> list[VaultInfo]:
    path = path or OUTPUT_DIR / "vaults.html"
    if not path.exists():
        print(f"  [!] {path} not found — run scripts/sync-from-prod.sh first", file=sys.stderr)
        sys.exit(1)
    parser = _VaultsParser()
    parser.feed(path.read_text())
    return parser.vaults


# ---------------------------------------------------------------------------
# Taostats API (stdlib only — no requests dependency)
# ---------------------------------------------------------------------------

def _taostats_get(path: str, params: dict | None = None) -> dict:
    global _last_call_ts
    api_key = os.environ.get("TAOSTATS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TAOSTATS_API_KEY not set")

    elapsed = time.time() - _last_call_ts
    if elapsed < 5.0:
        time.sleep(5.0 - elapsed)

    qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"{TAOSTATS_BASE}{path}{'?' + qs if qs else ''}"
    req = urllib.request.Request(url, headers={
        "Authorization": api_key,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        _last_call_ts = time.time()
        return json.loads(resp.read())


def _coingecko_tao_usd() -> float | None:
    try:
        req = urllib.request.Request(COINGECKO_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return float(data["bittensor"]["usd"])
    except Exception:
        return None


def fetch_live_emissions() -> dict | None:
    """Fetch miner daily emissions + alpha price from taostats API.

    Returns dict with daily_miner_alpha, alpha_price_tao, tao_usd, alpha_price_usd
    or None if API is unavailable.
    """
    try:
        # Get metagraph (2 pages for 256 UIDs)
        print("  Fetching metagraph from taostats...", file=sys.stderr)
        rows: list[dict] = []
        page = 1
        while True:
            body = _taostats_get(
                "/metagraph/latest/v1",
                {"netuid": NETUID, "page": page, "limit": 200},
            )
            batch = body.get("data") or []
            rows.extend(batch)
            nxt = (body.get("pagination") or {}).get("next_page")
            if not nxt or not batch:
                break
            page = int(nxt)

        total_mining_nano = sum(int(r.get("daily_mining_alpha") or 0) for r in rows)
        daily_miner_alpha = total_mining_nano / NANO

        # Alpha price
        print("  Fetching alpha price...", file=sys.stderr)
        pool_hist = _taostats_get(
            "/dtao/pool/history/v1", {"netuid": NETUID, "limit": 1},
        )
        pool_data = (pool_hist.get("data") or [{}])[0]
        alpha_price_tao = float(pool_data.get("price", 0))

        # TAO/USD
        print("  Fetching TAO/USD...", file=sys.stderr)
        tao_usd = _coingecko_tao_usd()
        if tao_usd is None:
            tao_usd = 0.0

        return {
            "daily_miner_alpha": daily_miner_alpha,
            "alpha_price_tao": alpha_price_tao,
            "tao_usd": tao_usd,
            "alpha_price_usd": alpha_price_tao * tao_usd,
            "source": "live",
        }
    except Exception as e:
        print(f"  [!] Live fetch failed: {e}", file=sys.stderr)
        return None


def load_fallback_emissions(path: Path | None = None) -> dict:
    """Read emissions defaults from output/tokenomics.json."""
    path = path or OUTPUT_DIR / "tokenomics.json"
    if not path.exists():
        print(f"  [!] {path} not found", file=sys.stderr)
        sys.exit(1)
    data = json.loads(path.read_text())
    alpha_price_tao = float(data.get("current_alpha_price_tao", 0))
    tao_usd = float(data.get("current_tao_usd", 0))
    # delta_24h_alpha is the OWNER's daily accrual — not miner total.
    # Use it as a rough proxy with a warning.
    daily_owner = float(data.get("delta_24h_alpha", 0))
    return {
        "daily_miner_alpha": daily_owner,
        "alpha_price_tao": alpha_price_tao,
        "tao_usd": tao_usd,
        "alpha_price_usd": alpha_price_tao * tao_usd,
        "source": "fallback (tokenomics.json)",
        "warning": "Using owner daily accrual as proxy — set --daily-alpha for accuracy",
        "snapshot": data.get("generated_at", "unknown"),
    }


# ---------------------------------------------------------------------------
# Vault selection
# ---------------------------------------------------------------------------

def select_vault(
    vaults: list[VaultInfo], target: str | None, window: str,
) -> VaultInfo:
    if target and target.lower() != "best":
        needle = target.lower()
        for v in vaults:
            if needle in v.display_id.lower():
                return v
        print(f"  [!] No vault matching '{target}'. Use --list-vaults.", file=sys.stderr)
        sys.exit(1)

    # Auto-select: highest APR for the window, with at least 7 days active
    best = None
    best_apr = -1.0
    for v in vaults:
        apr = v.apr_for_window(window)
        if apr is not None and apr > best_apr and v.days_active >= 7:
            best_apr = apr
            best = v
    if best is None:
        print("  [!] No vault with valid APR found.", file=sys.stderr)
        sys.exit(1)
    return best


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def run_simulation(cfg: SimConfig) -> list[DaySnapshot]:
    daily_rate = cfg.apr / 100.0 / 365.0
    daily_alpha_usd = cfg.daily_miner_alpha * cfg.alpha_price_usd

    cumulative_alpha_sold_usd = 0.0
    cumulative_lp_deployed = 0.0
    cumulative_fees_earned = 0.0
    snapshots: list[DaySnapshot] = []

    for day in range(1, cfg.max_days + 1):
        # Sell today's alpha for LP
        alpha_usd_today = daily_alpha_usd
        lp_added = alpha_usd_today * cfg.exchange_multiplier
        cumulative_alpha_sold_usd += alpha_usd_today
        cumulative_lp_deployed += lp_added

        # Earn fees on all deployed LP
        fees_today = cumulative_lp_deployed * daily_rate
        cumulative_lp_deployed += fees_today  # compounding
        cumulative_fees_earned += fees_today

        snapshots.append(DaySnapshot(
            day=day,
            cumulative_alpha_sold_usd=cumulative_alpha_sold_usd,
            cumulative_lp_deployed=cumulative_lp_deployed,
            cumulative_fees_earned=cumulative_fees_earned,
        ))

    return snapshots


def find_payback_day(snapshots: list[DaySnapshot]) -> int | None:
    for s in snapshots:
        if s.cumulative_fees_earned >= s.cumulative_alpha_sold_usd:
            return s.day
    return None


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _fmt_usd(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:,.2f}M"
    return f"${v:,.0f}"


def _fmt_alpha(v: float) -> str:
    return f"{v:,.1f}"


def print_vault_table(vaults: list[VaultInfo]) -> None:
    print(f"\n{'Pair':<18} {'Vault':<16} {'TVL':>10} {'Fees':>10} {'Days':>5} "
          f"{'APR 7d':>8} {'APR 30d':>8} {'APR All':>8}")
    print("-" * 100)
    for v in vaults:
        a7 = f"{v.apr_7d:.1f}%" if v.apr_7d is not None else "N/A"
        a30 = f"{v.apr_30d:.1f}%" if v.apr_30d is not None else "N/A"
        a_all = f"{v.apr_all:.1f}%" if v.apr_all is not None else "N/A"
        print(f"{v.pair:<18} {v.vault_name:<16} {_fmt_usd(v.capital_usd):>10} "
              f"{_fmt_usd(v.fees_usd):>10} {v.days_active:>5} "
              f"{a7:>8} {a30:>8} {a_all:>8}")


def print_results(
    cfg: SimConfig,
    vault: VaultInfo,
    window: str,
    snapshots: list[DaySnapshot],
    payback_day: int | None,
    data_source: str,
) -> None:
    daily_usd = cfg.daily_miner_alpha * cfg.alpha_price_usd

    print("\n" + "=" * 64)
    print("  ForeverMoney — Emissions Payback Simulation")
    print("=" * 64)

    print(f"\n  Data source:            {data_source}")
    print(f"  Daily miner emissions:  {_fmt_alpha(cfg.daily_miner_alpha)} \u03b1/day  ({_fmt_usd(daily_usd)}/day)")
    print(f"  Alpha price:            ${cfg.alpha_price_usd:.4f}  ({cfg.alpha_price_tao:.6f} \u03c4 \u00d7 ${cfg.tao_usd:.2f})")
    print(f"  Exchange multiplier:    {cfg.exchange_multiplier:.1f}x")
    print(f"  Target vault:           {vault.display_id}")
    print(f"  APR used:               {cfg.apr:.2f}% ({window})")
    print(f"  Vault TVL:              {_fmt_usd(vault.capital_usd)}  |  Lifetime fees: {_fmt_usd(vault.fees_usd)}")

    # Milestone table
    milestones = [30, 90, 180, 365, 730, 1095]
    if payback_day and payback_day not in milestones:
        milestones.append(payback_day)
        milestones.sort()

    print(f"\n  {'Day':>6}  {'Alpha Sold $':>14}  {'LP Deployed':>14}  {'Fees Earned':>14}  {'Net P&L':>14}")
    print(f"  {'─' * 6}  {'─' * 14}  {'─' * 14}  {'─' * 14}  {'─' * 14}")

    for m in milestones:
        if m > len(snapshots):
            break
        s = snapshots[m - 1]
        marker = "  ***" if m == payback_day else ""
        pnl_color = "" if s.net_pnl >= 0 else ""
        print(f"  {s.day:>6}  {_fmt_usd(s.cumulative_alpha_sold_usd):>14}  "
              f"{_fmt_usd(s.cumulative_lp_deployed):>14}  "
              f"{_fmt_usd(s.cumulative_fees_earned):>14}  "
              f"{_fmt_usd(s.net_pnl):>14}{marker}")

    print()
    if payback_day:
        months = payback_day / 30.44
        print(f"  >> PAYBACK in {payback_day} days (~{months:.1f} months)")
    else:
        print(f"  >> No payback within {cfg.max_days} days ({cfg.max_days / 365:.0f} years)")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Simulate selling miner alpha emissions for LP tokens and "
                    "track fee-based payback.",
    )
    p.add_argument("--daily-alpha", type=float, default=None,
                   help="Daily miner alpha emissions (default: live from taostats)")
    p.add_argument("--alpha-price", type=float, default=None,
                   help="Alpha price in USD (default: live from taostats)")
    p.add_argument("--multiplier", type=float, default=1.0,
                   help="Exchange multiplier: LP value per alpha (default: 1.0)")
    p.add_argument("--target", type=str, default=None,
                   help="Vault display ID or 'best' (default: best)")
    p.add_argument("--apr-window", type=str, default="all",
                   choices=["7d", "30d", "all"],
                   help="APR window to use (default: all)")
    p.add_argument("--max-days", type=int, default=1095,
                   help="Simulation horizon in days (default: 1095)")
    p.add_argument("--list-vaults", action="store_true",
                   help="List available vaults and exit")
    return p


def main() -> None:
    args = build_parser().parse_args()

    # Parse vaults
    vaults = parse_vaults()
    if not vaults:
        print("No vaults found in output/vaults.html", file=sys.stderr)
        sys.exit(1)

    if args.list_vaults:
        print_vault_table(vaults)
        return

    # Get emissions data
    emissions = fetch_live_emissions()
    data_source = "live (taostats API)"
    if emissions is None:
        emissions = load_fallback_emissions()
        data_source = emissions.get("source", "fallback")
        if "warning" in emissions:
            print(f"  [!] {emissions['warning']}", file=sys.stderr)
        if "snapshot" in emissions:
            data_source += f" — snapshot {emissions['snapshot']}"

    daily_alpha = args.daily_alpha or emissions["daily_miner_alpha"]
    alpha_price_usd = args.alpha_price or emissions["alpha_price_usd"]
    alpha_price_tao = emissions["alpha_price_tao"]
    tao_usd = emissions["tao_usd"]

    if daily_alpha <= 0:
        print("  [!] Daily miner alpha is 0 — pass --daily-alpha", file=sys.stderr)
        sys.exit(1)
    if alpha_price_usd <= 0:
        print("  [!] Alpha price is $0 — pass --alpha-price", file=sys.stderr)
        sys.exit(1)

    # Select vault
    vault = select_vault(vaults, args.target, args.apr_window)
    apr = vault.apr_for_window(args.apr_window)
    if apr is None or apr <= 0:
        print(f"  [!] {vault.display_id} has no valid APR for window '{args.apr_window}'",
              file=sys.stderr)
        sys.exit(1)

    # Run simulation
    cfg = SimConfig(
        daily_miner_alpha=daily_alpha,
        alpha_price_usd=alpha_price_usd,
        alpha_price_tao=alpha_price_tao,
        tao_usd=tao_usd,
        exchange_multiplier=args.multiplier,
        apr=apr,
        max_days=args.max_days,
    )
    snapshots = run_simulation(cfg)
    payback_day = find_payback_day(snapshots)

    print_results(cfg, vault, args.apr_window, snapshots, payback_day, data_source)


if __name__ == "__main__":
    main()
