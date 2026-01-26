"""
Configuration Management
========================

All configurable parameters for the Funding Fee Farming Strategy.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class FarmingConfig:
    """Configuration for Funding Fee Farming Bot"""
    
    # ==========================================================================
    # API CREDENTIALS
    # ==========================================================================
    MUDREX_API_SECRET: str = field(default_factory=lambda: os.getenv("MUDREX_API_SECRET", ""))
    
    # Telegram notifications
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_CHAT_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    
    # ==========================================================================
    # FUNDING RATE THRESHOLDS
    # ==========================================================================
    # Minimum funding rate to consider farming (0.5% = 0.005)
    EXTREME_RATE_THRESHOLD: float = 0.005
    
    # Very extreme rates for potentially larger positions
    VERY_EXTREME_THRESHOLD: float = 0.01  # 1%
    
    # ==========================================================================
    # ENTRY TIMING
    # ==========================================================================
    # Enter position within this window before settlement
    ENTRY_MIN_MINUTES_BEFORE: int = 1   # Minimum 1 minute before
    ENTRY_MAX_MINUTES_BEFORE: int = 5   # Maximum 5 minutes before
    
    # ==========================================================================
    # EXIT TIMING
    # ==========================================================================
    # Minimum profit percentage to exit (after fees)
    MIN_PROFIT_PERCENT: float = 0.05  # 0.05%
    
    # Maximum time to hold after settlement (force exit)
    MAX_HOLD_MINUTES_AFTER_SETTLEMENT: int = 30
    
    # ==========================================================================
    # POSITION SIZING (TEST MODE)
    # ==========================================================================
    # Use minimum order amount for testing
    USE_MINIMUM_ORDER_SIZE: bool = True
    
    # Fallback minimum order value if not available from API
    MIN_ORDER_VALUE_USD: float = 8.0
    
    # Use maximum leverage available for the asset
    USE_MAX_LEVERAGE: bool = True
    
    # Fallback leverage if max not available
    DEFAULT_LEVERAGE: int = 20
    
    # Maximum concurrent positions
    MAX_CONCURRENT_POSITIONS: int = 3
    
    # ==========================================================================
    # FEE ESTIMATES
    # ==========================================================================
    # Mudrex trading fee (taker)
    TAKER_FEE_PERCENT: float = 0.06
    
    # Slippage buffer
    SLIPPAGE_BUFFER_PERCENT: float = 0.02
    
    # ==========================================================================
    # DATA STORAGE
    # ==========================================================================
    DATA_DIR: str = "data"
    STATE_FILE: str = "data/state.json"
    TRADES_LOG_FILE: str = "data/trades.json"
    LOG_FILE: str = "logs/farming.log"
    
    # ==========================================================================
    # MONITORING
    # ==========================================================================
    # How often to scan for opportunities (seconds)
    SCAN_INTERVAL_SECONDS: int = 30
    
    # ==========================================================================
    # API ENDPOINTS
    # ==========================================================================
    FUNDING_API_BASE_URL: str = "https://api.bybit.com"
    
    def __post_init__(self):
        """Validate configuration after initialization"""
        # Validation moved to main.py for better error handling
        pass
    
    def validate(self):
        """Validate settings and return warnings (non-blocking)"""
        warnings = []
        if not self.MUDREX_API_SECRET:
            warnings.append("MUDREX_API_SECRET not set - trading will not work")
        if not self.TELEGRAM_BOT_TOKEN:
            warnings.append("TELEGRAM_BOT_TOKEN not set - notifications disabled")
        if not self.TELEGRAM_CHAT_ID:
            warnings.append("TELEGRAM_CHAT_ID not set - notifications disabled")
        return warnings
    
    @property
    def total_fee_percent(self) -> float:
        """Total fees for entry + exit"""
        return (self.TAKER_FEE_PERCENT + self.SLIPPAGE_BUFFER_PERCENT) * 2
    
    def min_profitable_rate(self) -> float:
        """Minimum funding rate needed to be profitable after fees"""
        return self.total_fee_percent + self.MIN_PROFIT_PERCENT

# Don't create global instance - let main.py create it after env vars are loaded
# config = FarmingConfig()
