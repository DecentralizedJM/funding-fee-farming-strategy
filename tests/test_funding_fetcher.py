"""
Test Funding Fetcher
====================

Quick test to verify the funding fetcher is working.
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from funding_fetcher import FundingDataFetcher


def main():
    print("=" * 60)
    print("FUNDING FETCHER TEST")
    print("=" * 60)
    
    fetcher = FundingDataFetcher()
    
    # Test 1: Get all symbols
    print("\n📊 Fetching all perpetual symbols...")
    symbols = fetcher.get_all_perpetual_symbols_with_intervals()
    print(f"✅ Found {len(symbols)} symbols")
    
    # Show sample
    if symbols:
        sample = list(symbols.items())[:3]
        print("\nSample symbols:")
        for sym, info in sample:
            print(f"  - {sym}: {info['fundingIntervalHours']}h interval, rate: {info['currentRate']*100:.4f}%")
    
    # Test 2: Get extreme opportunities
    print("\n🎯 Scanning for extreme funding opportunities (≥0.5%)...")
    opportunities = fetcher.get_extreme_funding_opportunities(threshold=0.005)
    print(f"✅ Found {len(opportunities)} opportunities")
    
    if opportunities:
        print("\nTop 5 opportunities:")
        for opp in opportunities[:5]:
            print(f"  - {opp['symbol']}: {opp['fundingRate']*100:+.4f}% → {opp['recommendedSide']}")
    else:
        print("  (No extreme rates right now - this is normal)")
    
    # Test 3: Get instrument info
    print("\n📐 Testing instrument info for BTCUSDT...")
    info = fetcher.get_instrument_info("BTCUSDT")
    if info:
        print(f"  - Min Qty: {info['minOrderQty']}")
        print(f"  - Max Leverage: {info['maxLeverage']}x")
        print(f"  - Qty Step: {info['qtyStep']}")
    else:
        print("  ❌ Could not fetch instrument info")
    
    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
