"""
Configuration Management
========================

All configurable parameters for the Funding Fee Farming Strategy.
"""

import os
from dataclasses import dataclass, field
from typing import Optional, List
from dotenv import load_dotenv

load_dotenv()


@dataclass
class FarmingConfig:
    """Configuration for Funding Fee Farming Bot"""
    
    # ==========================================================================
    # API CREDENTIALS
    # ==========================================================================
    MUDREX_API_SECRET: str = field(default_factory=lambda: os.getenv("MUDREX_API_SECRET", ""))
    
    # Telegram notifications (comma-separated chat IDs for multiple recipients)
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_CHAT_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    
    @property
    def TELEGRAM_CHAT_IDS(self) -> List[str]:
        """Parse TELEGRAM_CHAT_ID into list (comma-separated)."""
        raw = (self.TELEGRAM_CHAT_ID or "").strip()
        return [x.strip() for x in raw.split(",") if x.strip()]
    
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
    # Enter position in the last N seconds before settlement (minimize price exposure)
    # Entry allowed when seconds until settlement is between min and max
    ENTRY_MIN_SECONDS_BEFORE: int = 1    # At least 1 second before (avoid race)
    ENTRY_MAX_SECONDS_BEFORE: int = 10   # Up to 10 seconds before settlement
    # When any opportunity has <= this many seconds to settlement, scan every ENTRY_FAST_SCAN_SECONDS
    # so we don't miss the 1-10s window (normal 30s scan would skip past it)
    ENTRY_FAST_SCAN_WHEN_SECONDS_LEFT: int = 60
    ENTRY_FAST_SCAN_SECONDS: int = 3
    
    # ==========================================================================
    # RISK MANAGEMENT
    # ==========================================================================
    # Stop loss percentage of MARGIN (not notional) - e.g. 0.05 = 5% of margin at risk
    # With $2 margin, 5% stop loss = $0.10 max loss before exit
    # Previously was 0.5% of notional which was 10x too tight with leverage
    STOP_LOSS_PERCENT: float = 0.05
    
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
    
    # Safety cap: force exit if still open this long after settlement
    MAX_HOLD_MINUTES_AFTER_SETTLEMENT: int = 5
    
    # "Soft Loss" Exit: Exit if loss is small (second ideal - avoids larger losses)
    # Exit if total PnL (including funding) > this threshold (e.g. -0.2% = small loss)
    SOFT_LOSS_EXIT_PERCENT: float = -0.002
    
    # ==========================================================================
    # SETTLEMENT REVERSAL STRATEGY
    # ==========================================================================
    # Enable settlement reversal: after funding settlement, close position and 
    # open opposite side to capture post-settlement price movement
    SETTLEMENT_REVERSAL_ENABLED: bool = True
    
    # Profit target for reversed position (0.05% = 0.0005)
    # Exit reversed position when profit >= this percentage
    REVERSAL_PROFIT_TARGET_PERCENT: float = 0.0005
    
    # Maximum hold time for reversed position (minutes)
    # Exit reversed position after this time regardless of PnL
    REVERSAL_MAX_HOLD_MINUTES: int = 3
    
    # ==========================================================================
    # SAFETY CHECKS
    # ==========================================================================
    # Max allowed spread between Mark and Last price (1%)
    PRICE_SPREAD_THRESHOLD: float = 0.01
    
    # Max acceptable slippage on entry (0.3% = 0.003)
    # If execution price differs from expected by more than this, close immediately
    MAX_SLIPPAGE_PERCENT: float = 0.003
    
    # ==========================================================================
    # POSITION SIZING
    # ==========================================================================
    # Margin as percentage of available futures wallet balance (e.g. 50 = 50%)
    # Set via Railway variable MARGIN_PERCENTAGE - no default (required for opening positions)
    MARGIN_PERCENTAGE: Optional[float] = field(
        default_factory=lambda: (
            lambda v: float(v) if v and str(v).strip() else None
        )(os.getenv("MARGIN_PERCENTAGE"))
    )
    
    # Leverage range (hardcoded 10x-25x)
    MIN_LEVERAGE: int = 10
    MAX_LEVERAGE: int = 25
    
    # Minimum total order value (notional) in USD - position size scaled to meet this
    MIN_ORDER_VALUE_USD: float = 7.0
    
    # Minimum 24h volume (USD) to avoid low liquidity slippage
    MIN_VOLUME_24H: float = 1_000_000
    
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
        if self.MARGIN_PERCENTAGE is None:
            warnings.append("MARGIN_PERCENTAGE not set (set in Railway variables) - will not open new positions")
        elif self.MARGIN_PERCENTAGE <= 0 or self.MARGIN_PERCENTAGE > 100:
            warnings.append("MARGIN_PERCENTAGE must be between 1 and 100")
        if not self.TELEGRAM_BOT_TOKEN:
            warnings.append("TELEGRAM_BOT_TOKEN not set - notifications disabled")
        if not self.TELEGRAM_CHAT_IDS:
            warnings.append("TELEGRAM_CHAT_ID not set (comma-separated for multiple) - notifications disabled")
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
