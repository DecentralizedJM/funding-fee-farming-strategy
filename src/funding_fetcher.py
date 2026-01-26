"""
Funding Rate Data Fetcher
=========================

Fetches funding rate data from the perpetual futures API.
Designed for Mudrex Futures trading.
"""

import logging
import requests
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
import time

logger = logging.getLogger(__name__)


class FundingDataFetcher:
    """Fetch funding rate data from perpetual futures API"""
    
    def __init__(self, base_url: str = "https://api.bybit.com"):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json"
        })
        self._symbols_cache = None
        self._cache_timestamp = None
        self._cache_ttl = 3600  # 1 hour cache
    
    def get_all_perpetual_symbols_with_intervals(self) -> Dict[str, Dict]:
        """
        Get all available USDT perpetual symbols with their funding intervals
        
        Returns:
            Dict mapping symbol to info including funding interval
        """
        try:
            url = f"{self.base_url}/v5/market/tickers"
            params = {"category": "linear"}
            
            response = self.session.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            if data.get("retCode") != 0:
                logger.error(f"API error: {data.get('retMsg')}")
                return {}
            
            result = {}
            for ticker in data.get("result", {}).get("list", []):
                symbol = ticker.get("symbol", "")
                if not symbol.endswith("USDT"):
                    continue
                
                interval_hours = int(ticker.get("fundingIntervalHour", 8))
                
                result[symbol] = {
                    "symbol": symbol,
                    "fundingIntervalHours": interval_hours,
                    "nextFundingTime": int(ticker.get("nextFundingTime", 0)),
                    "currentRate": float(ticker.get("fundingRate", 0)),
                    "lastPrice": float(ticker.get("lastPrice", 0)),
                    "volume24h": float(ticker.get("volume24h", 0)),
                }
            
            logger.info(f"Found {len(result)} USDT perpetual symbols")
            return result
            
        except Exception as e:
            logger.error(f"Error fetching perpetual symbols: {e}")
            return {}
    
    def get_tickers(self, symbols: List[str] = None) -> Dict[str, Dict]:
        """
        Get current ticker data including funding rates
        
        Args:
            symbols: Optional list of specific symbols to filter
        
        Returns:
            Dict mapping symbol to ticker data including funding rate
        """
        try:
            url = f"{self.base_url}/v5/market/tickers"
            params = {"category": "linear"}
            
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get("retCode") != 0:
                logger.error(f"API error: {data.get('retMsg')}")
                return {}
            
            result = {}
            ticker_list = data.get("result", {}).get("list", [])
            
            for ticker in ticker_list:
                symbol = ticker.get("symbol", "")
                
                # Filter by symbols if specified
                if symbols and symbol not in symbols:
                    continue
                
                # Only include perpetuals
                if not symbol.endswith("USDT"):
                    continue
                
                funding_rate = ticker.get("fundingRate", "0")
                next_funding_time = ticker.get("nextFundingTime", "0")
                
                result[symbol] = {
                    "symbol": symbol,
                    "lastPrice": float(ticker.get("lastPrice", 0)),
                    "fundingRate": float(funding_rate) if funding_rate else 0,
                    "nextFundingTime": int(next_funding_time) if next_funding_time else 0,
                    "fundingIntervalHours": int(ticker.get("fundingIntervalHour", 8)),
                    "price24hPcnt": float(ticker.get("price24hPcnt", 0)),
                    "volume24h": float(ticker.get("volume24h", 0)),
                    "openInterest": float(ticker.get("openInterest", 0)),
                    "markPrice": float(ticker.get("markPrice", 0)) if ticker.get("markPrice") else float(ticker.get("lastPrice", 0)),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            
            logger.debug(f"Fetched {len(result)} tickers")
            return result
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching tickers: {e}")
            return {}
        except Exception as e:
            logger.error(f"Unexpected error fetching tickers: {e}")
            return {}
    
    def get_extreme_funding_opportunities(
        self, 
        threshold: float = 0.005
    ) -> List[Dict]:
        """
        Find all symbols with extreme funding rates
        
        Args:
            threshold: Minimum absolute funding rate (0.005 = 0.5%)
        
        Returns:
            List of opportunities sorted by absolute funding rate
        """
        tickers = self.get_tickers()
        opportunities = []
        
        for symbol, data in tickers.items():
            rate = data["fundingRate"]
            abs_rate = abs(rate)
            
            if abs_rate >= threshold:
                # Determine recommended side
                # Positive rate = longs pay shorts -> go SHORT to receive
                # Negative rate = shorts pay longs -> go LONG to receive
                recommended_side = "SHORT" if rate > 0 else "LONG"
                
                opportunities.append({
                    "symbol": symbol,
                    "fundingRate": rate,
                    "absRate": abs_rate,
                    "recommendedSide": recommended_side,
                    "nextFundingTime": data["nextFundingTime"],
                    "fundingIntervalHours": data["fundingIntervalHours"],
                    "lastPrice": data["lastPrice"],
                    "markPrice": data["markPrice"],
                    "volume24h": data["volume24h"],
                })
        
        # Sort by absolute rate (highest first)
        opportunities.sort(key=lambda x: x["absRate"], reverse=True)
        
        logger.info(f"Found {len(opportunities)} extreme funding opportunities (>= {threshold*100:.2f}%)")
        return opportunities
    
    def get_time_to_next_settlement(self, next_funding_time_ms: int) -> timedelta:
        """
        Calculate time remaining to next funding settlement
        
        Args:
            next_funding_time_ms: Next funding time in milliseconds
        
        Returns:
            timedelta to next settlement
        """
        if not next_funding_time_ms:
            return timedelta(hours=8)
        
        next_time = datetime.fromtimestamp(next_funding_time_ms / 1000, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        
        return next_time - now
    
    def get_funding_rate_history(self, symbol: str, limit: int = 10) -> List[Dict]:
        """
        Get historical funding rates for a symbol
        
        Args:
            symbol: Symbol name (e.g., "BTCUSDT")
            limit: Number of records to fetch (1-200)
        
        Returns:
            List of funding rate records
        """
        try:
            url = f"{self.base_url}/v5/market/funding/history"
            params = {
                "category": "linear",
                "symbol": symbol,
                "limit": min(limit, 200)
            }
            
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get("retCode") != 0:
                logger.error(f"API error for {symbol}: {data.get('retMsg')}")
                return []
            
            records = []
            for item in data.get("result", {}).get("list", []):
                records.append({
                    "symbol": item.get("symbol"),
                    "fundingRate": float(item.get("fundingRate", 0)),
                    "fundingRateTimestamp": int(item.get("fundingRateTimestamp", 0))
                })
            
            return records
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching funding history for {symbol}: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching funding history: {e}")
            return []
    
    def get_instrument_info(self, symbol: str) -> Optional[Dict]:
        """
        Get instrument details including min order size and max leverage
        
        Args:
            symbol: Symbol name
        
        Returns:
            Instrument info dict or None
        """
        try:
            url = f"{self.base_url}/v5/market/instruments-info"
            params = {
                "category": "linear",
                "symbol": symbol
            }
            
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get("retCode") != 0:
                logger.error(f"API error for {symbol}: {data.get('retMsg')}")
                return None
            
            instruments = data.get("result", {}).get("list", [])
            if not instruments:
                return None
            
            inst = instruments[0]
            lot_filter = inst.get("lotSizeFilter", {})
            leverage_filter = inst.get("leverageFilter", {})
            
            return {
                "symbol": symbol,
                "minOrderQty": float(lot_filter.get("minOrderQty", 0)),
                "maxOrderQty": float(lot_filter.get("maxOrderQty", 0)),
                "qtyStep": float(lot_filter.get("qtyStep", 0)),
                "minLeverage": float(leverage_filter.get("minLeverage", 1)),
                "maxLeverage": float(leverage_filter.get("maxLeverage", 100)),
                "leverageStep": float(leverage_filter.get("leverageStep", 0.01)),
            }
            
        except Exception as e:
            logger.error(f"Error fetching instrument info for {symbol}: {e}")
            return None
