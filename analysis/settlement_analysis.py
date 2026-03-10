"""
Settlement Price Behavior Analysis
===================================
Fetches Bybit kline + funding rate data and analyzes how prices move
around funding settlements for high-funding-rate assets.
"""

import requests
import time
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

BASE = "https://api.bybit.com"
session = requests.Session()


def get_funding_rate_history(symbol: str, limit: int = 50):
    """Get historical funding rates for a symbol."""
    url = f"{BASE}/v5/market/funding/history"
    params = {"category": "linear", "symbol": symbol, "limit": limit}
    resp = session.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("retCode") != 0:
        print(f"  Error fetching funding history for {symbol}: {data.get('retMsg')}")
        return []
    return data.get("result", {}).get("list", [])


def get_klines(symbol: str, interval: str, start_ms: int, end_ms: int):
    """Get kline data. interval: '1' = 1min, '5' = 5min, etc."""
    url = f"{BASE}/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "start": start_ms,
        "end": end_ms,
        "limit": 200,
    }
    resp = session.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("retCode") != 0:
        print(f"  Error fetching klines: {data.get('retMsg')}")
        return []
    return data.get("result", {}).get("list", [])


def get_current_extreme_symbols(threshold: float = 0.005, top_n: int = 10):
    """Get symbols with extreme current funding rates."""
    url = f"{BASE}/v5/market/tickers"
    params = {"category": "linear"}
    resp = session.get(url, params=params, timeout=15)
    data = resp.json()
    tickers = data.get("result", {}).get("list", [])
    extremes = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        rate = float(t.get("fundingRate", 0))
        vol = float(t.get("volume24h", 0))
        if abs(rate) >= threshold and vol > 500_000:
            extremes.append({
                "symbol": sym,
                "rate": rate,
                "absRate": abs(rate),
                "volume24h": vol,
                "intervalHours": int(t.get("fundingIntervalHour", 8)),
            })
    extremes.sort(key=lambda x: x["absRate"], reverse=True)
    return extremes[:top_n]


def analyze_settlement(symbol: str, settlement_ms: int, funding_rate: float):
    """
    Analyze price behavior around a single settlement event.
    Fetches 1-min klines from 30 min before to 30 min after settlement.
    Returns dict with price movement metrics.
    """
    window_before_ms = 30 * 60 * 1000  # 30 min before
    window_after_ms = 30 * 60 * 1000   # 30 min after
    start = settlement_ms - window_before_ms
    end = settlement_ms + window_after_ms

    klines = get_klines(symbol, "1", start, end)
    if not klines:
        return None

    # Klines are returned newest-first; reverse to chronological
    klines = list(reversed(klines))

    # Parse: [startTime, open, high, low, close, volume, turnover]
    parsed = []
    for k in klines:
        ts = int(k[0])
        parsed.append({
            "time": ts,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })

    # Split into before/after settlement
    before = [k for k in parsed if k["time"] < settlement_ms]
    after = [k for k in parsed if k["time"] >= settlement_ms]

    if not before or not after:
        return None

    settlement_price = before[-1]["close"]  # price at settlement
    if settlement_price == 0:
        return None

    # Price at various points before settlement
    prices_before = {}
    for mins in [1, 2, 3, 5, 10, 15, 30]:
        target_ms = settlement_ms - mins * 60 * 1000
        closest = min(before, key=lambda k: abs(k["time"] - target_ms))
        if abs(closest["time"] - target_ms) < 90_000:  # within 1.5 min
            prices_before[f"T-{mins}m"] = closest["close"]

    # Price at various points after settlement
    prices_after = {}
    for mins in [1, 2, 3, 5, 10, 15, 30]:
        target_ms = settlement_ms + mins * 60 * 1000
        closest = min(after, key=lambda k: abs(k["time"] - target_ms))
        if abs(closest["time"] - target_ms) < 90_000:
            prices_after[f"T+{mins}m"] = closest["close"]

    # Max adverse move and max favorable move in the 5 min window around settlement
    window_5m = [k for k in parsed if abs(k["time"] - settlement_ms) <= 5 * 60 * 1000]
    if window_5m:
        highs = [k["high"] for k in window_5m]
        lows = [k["low"] for k in window_5m]
        max_high = max(highs)
        min_low = min(lows)
    else:
        max_high = settlement_price
        min_low = settlement_price

    # Volume spike around settlement
    vol_before_5m = sum(k["volume"] for k in before[-5:]) if len(before) >= 5 else 0
    vol_after_5m = sum(k["volume"] for k in after[:5]) if len(after) >= 5 else 0
    vol_normal = sum(k["volume"] for k in before[-30:-5]) / 25 if len(before) >= 30 else 0  # avg per minute

    # Key metrics
    # The hypothesis: with positive funding (longs pay shorts), price tends to dip before
    # settlement (longs close to avoid paying) and bounce after (pressure removed)
    # With negative funding (shorts pay longs), price tends to spike before settlement
    # (shorts close to avoid paying) and drop after

    result = {
        "symbol": symbol,
        "settlement_time": datetime.fromtimestamp(settlement_ms / 1000, tz=timezone.utc).isoformat(),
        "funding_rate": funding_rate,
        "funding_rate_pct": funding_rate * 100,
        "settlement_price": settlement_price,
        "prices_before": {},
        "prices_after": {},
        "pct_changes_before": {},
        "pct_changes_after": {},
        "max_range_5m_pct": (max_high - min_low) / settlement_price * 100,
        "vol_spike_ratio": (vol_after_5m / vol_before_5m) if vol_before_5m > 0 else 0,
        "vol_per_min_normal": vol_normal,
        "vol_per_min_settlement": vol_after_5m / 5 if vol_after_5m > 0 else 0,
    }

    for label, price in prices_before.items():
        pct = (price - settlement_price) / settlement_price * 100
        result["prices_before"][label] = price
        result["pct_changes_before"][label] = pct

    for label, price in prices_after.items():
        pct = (price - settlement_price) / settlement_price * 100
        result["prices_after"][label] = price
        result["pct_changes_after"][label] = pct

    return result


def main():
    print("=" * 70)
    print("FUNDING SETTLEMENT PRICE BEHAVIOR ANALYSIS")
    print("=" * 70)

    # Step 1: Find symbols with extreme funding rates
    print("\n[1] Finding symbols with extreme funding rates (>= 0.5%)...")
    extremes = get_current_extreme_symbols(threshold=0.005, top_n=10)
    if not extremes:
        print("No extreme funding rate symbols found. Trying lower threshold...")
        extremes = get_current_extreme_symbols(threshold=0.002, top_n=10)
    
    print(f"Found {len(extremes)} symbols:")
    for e in extremes:
        print(f"  {e['symbol']:20s} rate={e['rate']*100:+.4f}%  vol=${e['volume24h']:>12,.0f}  interval={e['intervalHours']}h")

    # Also analyze major coins that may have had extreme rates historically
    analysis_symbols = [e["symbol"] for e in extremes[:5]]
    # Add BTC and ETH for comparison
    for s in ["BTCUSDT", "ETHUSDT"]:
        if s not in analysis_symbols:
            analysis_symbols.append(s)

    print(f"\n[2] Analyzing {len(analysis_symbols)} symbols: {', '.join(analysis_symbols)}")

    # Step 2: For each symbol, get funding history and analyze settlements
    all_results = []
    
    for symbol in analysis_symbols:
        print(f"\n{'='*60}")
        print(f"Analyzing: {symbol}")
        print(f"{'='*60}")
        
        funding_history = get_funding_rate_history(symbol, limit=30)
        if not funding_history:
            print(f"  No funding history available")
            continue

        print(f"  Got {len(funding_history)} funding rate records")
        
        # Focus on high-rate settlements
        high_rate_settlements = [
            f for f in funding_history
            if abs(float(f.get("fundingRate", 0))) >= 0.003  # >= 0.3% for analysis
        ]
        
        if not high_rate_settlements:
            high_rate_settlements = funding_history[:10]  # just use recent ones
            print(f"  No high-rate settlements found, using {len(high_rate_settlements)} recent ones")
        else:
            print(f"  Found {len(high_rate_settlements)} high-rate settlements (>= 0.3%)")

        for f_record in high_rate_settlements[:8]:  # analyze up to 8 per symbol
            rate = float(f_record.get("fundingRate", 0))
            settlement_ms = int(f_record.get("fundingRateTimestamp", 0))
            
            if settlement_ms == 0:
                continue

            settlement_dt = datetime.fromtimestamp(settlement_ms / 1000, tz=timezone.utc)
            print(f"\n  Settlement: {settlement_dt.strftime('%Y-%m-%d %H:%M')} UTC  rate={rate*100:+.4f}%")

            result = analyze_settlement(symbol, settlement_ms, rate)
            if result:
                all_results.append(result)
                
                # Print summary
                print(f"    Price at settlement: ${result['settlement_price']:.4f}")
                print(f"    5-min range: {result['max_range_5m_pct']:.3f}%")
                print(f"    Volume spike: {result['vol_spike_ratio']:.1f}x")
                
                print(f"    Price moves BEFORE settlement (vs settlement price):")
                for label in sorted(result["pct_changes_before"].keys(), key=lambda x: int(x.split("-")[1].replace("m", ""))):
                    pct = result["pct_changes_before"][label]
                    print(f"      {label:8s}: {pct:+.4f}%")

                print(f"    Price moves AFTER settlement (vs settlement price):")
                for label in sorted(result["pct_changes_after"].keys(), key=lambda x: int(x.split("+")[1].replace("m", ""))):
                    pct = result["pct_changes_after"][label]
                    print(f"      {label:8s}: {pct:+.4f}%")
            else:
                print(f"    (insufficient kline data)")
            
            time.sleep(0.15)  # rate limit

    # Step 3: Aggregate analysis
    print("\n" + "=" * 70)
    print("AGGREGATE ANALYSIS")
    print("=" * 70)

    if not all_results:
        print("No results to analyze")
        return

    # Split by funding direction
    positive_rate = [r for r in all_results if r["funding_rate"] > 0]
    negative_rate = [r for r in all_results if r["funding_rate"] < 0]

    for label, group in [("POSITIVE FUNDING (longs pay shorts)", positive_rate), ("NEGATIVE FUNDING (shorts pay longs)", negative_rate)]:
        if not group:
            continue
        print(f"\n{label}: {len(group)} settlements")
        print(f"  Average rate: {sum(r['funding_rate_pct'] for r in group)/len(group):+.4f}%")
        
        # Average price changes at each time point
        timepoints_before = defaultdict(list)
        timepoints_after = defaultdict(list)
        for r in group:
            for tp, pct in r["pct_changes_before"].items():
                timepoints_before[tp].append(pct)
            for tp, pct in r["pct_changes_after"].items():
                timepoints_after[tp].append(pct)
        
        print(f"\n  Average price change BEFORE settlement (vs settlement price):")
        for tp in sorted(timepoints_before.keys(), key=lambda x: int(x.split("-")[1].replace("m", ""))):
            vals = timepoints_before[tp]
            avg = sum(vals) / len(vals)
            print(f"    {tp:8s}: avg={avg:+.4f}%  (n={len(vals)}, min={min(vals):+.4f}%, max={max(vals):+.4f}%)")

        print(f"\n  Average price change AFTER settlement (vs settlement price):")
        for tp in sorted(timepoints_after.keys(), key=lambda x: int(x.split("+")[1].replace("m", ""))):
            vals = timepoints_after[tp]
            avg = sum(vals) / len(vals)
            print(f"    {tp:8s}: avg={avg:+.4f}%  (n={len(vals)}, min={min(vals):+.4f}%, max={max(vals):+.4f}%)")

        ranges = [r["max_range_5m_pct"] for r in group]
        vol_spikes = [r["vol_spike_ratio"] for r in group if r["vol_spike_ratio"] > 0]
        print(f"\n  5-min range: avg={sum(ranges)/len(ranges):.3f}%, max={max(ranges):.3f}%")
        if vol_spikes:
            print(f"  Volume spike: avg={sum(vol_spikes)/len(vol_spikes):.1f}x, max={max(vol_spikes):.1f}x")

    # Strategy-relevant analysis: If we enter SHORT when funding is positive,
    # what's our P&L at various exit points?
    print("\n" + "=" * 70)
    print("STRATEGY SIMULATION")
    print("=" * 70)
    
    for entry_label in ["T-5m", "T-3m", "T-1m"]:
        for exit_label in ["T+1m", "T+3m", "T+5m", "T+10m"]:
            pnls = []
            for r in all_results:
                entry_pct = r["pct_changes_before"].get(entry_label)
                exit_pct = r["pct_changes_after"].get(exit_label)
                if entry_pct is None or exit_pct is None:
                    continue
                
                rate = r["funding_rate"]
                # If positive funding -> we go SHORT to receive funding
                # SHORT P&L = -(exit_price - entry_price) / entry_price
                # entry_price relative to settlement = settlement * (1 + entry_pct/100)
                # exit_price relative to settlement = settlement * (1 + exit_pct/100)
                # P&L from price = entry_pct - exit_pct (since short)
                if rate > 0:
                    # Go SHORT: receive funding, profit if price drops
                    price_pnl = entry_pct - exit_pct  # short: sell high buy low
                    funding_pnl = abs(rate) * 100  # funding received as pct
                else:
                    # Go LONG: receive funding, profit if price rises
                    price_pnl = exit_pct - entry_pct  # long: buy low sell high
                    funding_pnl = abs(rate) * 100
                
                total_pnl = price_pnl + funding_pnl
                pnls.append({
                    "price_pnl": price_pnl,
                    "funding_pnl": funding_pnl,
                    "total_pnl": total_pnl,
                    "rate": rate,
                    "symbol": r["symbol"],
                })
            
            if not pnls:
                continue
            
            avg_price = sum(p["price_pnl"] for p in pnls) / len(pnls)
            avg_funding = sum(p["funding_pnl"] for p in pnls) / len(pnls)
            avg_total = sum(p["total_pnl"] for p in pnls) / len(pnls)
            win_rate = sum(1 for p in pnls if p["total_pnl"] > 0) / len(pnls) * 100
            worst = min(p["total_pnl"] for p in pnls)
            best = max(p["total_pnl"] for p in pnls)
            
            print(f"\n  Entry={entry_label}, Exit={exit_label} (n={len(pnls)} trades)")
            print(f"    Avg price P&L: {avg_price:+.4f}%")
            print(f"    Avg funding:   {avg_funding:+.4f}%")
            print(f"    Avg total P&L: {avg_total:+.4f}%")
            print(f"    Win rate:      {win_rate:.0f}%")
            print(f"    Range:         {worst:+.4f}% to {best:+.4f}%")

    # Save raw data
    output_file = "analysis/settlement_data.json"
    with open(output_file, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nRaw data saved to {output_file}")


if __name__ == "__main__":
    main()
