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
    # Minimum funding rate to consider farming (0.6% = 0.006 for higher quality)
    EXTREME_RATE_THRESHOLD: float = 0.006
    
    # Very extreme rates for potentially larger positions
    VERY_EXTREME_THRESHOLD: float = 0.01  # 1%
    
    # ==========================================================================
    # ENTRY TIMING
    # ==========================================================================
    # Enter position within this window before settlement (minimize price exposure)
    ENTRY_MIN_MINUTES_BEFORE: float = 0.5   # 30 seconds minimum
    ENTRY_MAX_MINUTES_BEFORE: float = 1.0   # 1 minute maximum
    
    # ==========================================================================
    # RISK MANAGEMENT
    # ==========================================================================
    # Stop loss percentage (e.g. 0.005 = 0.5%)
    STOP_LOSS_PERCENT: float = 0.005
    
    # Maximum daily loss in USD
    MAX_DAILY_LOSS_USD: float = 10.0
    
    # ==========================================================================
    # EXIT TIMING & STRATEGY
    # ==========================================================================
    
    # 1. Trailing Stop (Let winners run)
    TRAILING_STOP_ENABLED: bool = True
    # Activate trailing stop when profit > 0.1%
    TRAILING_ACTIVATION_PERCENT: float = 0.001 
    # Exit if profit drops 0.02% from peak
    TRAILING_CALLBACK_PERCENT: float = 0.0002 
    
    # 2. Base Targets
    # Minimum profit percentage to exit (after fees)
    MIN_PROFIT_PERCENT: float = 0.05  # 0.05%
    
    # Safety cap: force exit if still open this long after settlement (we exit right after settlement regardless of PnL)
    MAX_HOLD_MINUTES_AFTER_SETTLEMENT: int = 5
    
    # "Soft Loss" Exit: Exit immediately if PnL > -0.2% (Funding covers this)
    SOFT_LOSS_EXIT_PERCENT: float = -0.002
    
    # ==========================================================================
    # SAFETY CHECKS
    # ==========================================================================
    # Max allowed spread between Mark and Last price (1%)
    PRICE_SPREAD_THRESHOLD: float = 0.01
    
    # ==========================================================================
    # POSITION SIZING
    # ==========================================================================
    # Fixed margin per position (USD)
    MARGIN_USD: float = 2.0
    
    # Dynamic leverage range (5-10x based on funding rate)
    MIN_LEVERAGE: int = 5
    MAX_LEVERAGE: int = 10
    
    # Use dynamic leverage (5-10x) based on funding rate
    USE_MAX_LEVERAGE: bool = True
    
    # Fallback leverage if asset max is lower
    DEFAULT_LEVERAGE: int = 5
    
    # Minimum 24h volume (USD) to avoid low liquidity slippage
    MIN_VOLUME_24H: float = 1_000_000
    
    # Fallback minimum order value for exchange min quantity checks
    MIN_ORDER_VALUE_USD: float = 8.0
    
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
    
    # Send Telegram notifications when skipping opportunities (can be noisy)
    NOTIFY_SKIPS: bool = False
    
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
