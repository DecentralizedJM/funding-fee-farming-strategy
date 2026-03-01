"""
Configuration Management
========================

All configurable parameters for the Funding Fee Farming Strategy.
"""

import os
from dataclasses import dataclass, field
from typing import Optional, List, Tuple
from dotenv import load_dotenv

load_dotenv()

# Max number of API accounts (MUDREX_API_SECRET_1 .. _10)
MAX_ACCOUNTS = 10


@dataclass
class FarmingConfig:
    """Configuration for Funding Fee Farming Bot"""
    
    # ==========================================================================
    # API CREDENTIALS
    # ==========================================================================
    # Single account (legacy): MUDREX_API_SECRET
    MUDREX_API_SECRET: str = field(default_factory=lambda: os.getenv("MUDREX_API_SECRET", ""))
    
    # Multi-account: MUDREX_API_SECRET_1, MUDREX_API_SECRET_2, ... MUDREX_API_SECRET_10
    # Secret_1 is primary; 2..10 are friends' accounts. Same strategy runs for each.
    
    # Telegram: one token for all
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    # Legacy single-account: TELEGRAM_CHAT_ID (comma-separated)
    TELEGRAM_CHAT_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    
    @property
    def TELEGRAM_CHAT_IDS(self) -> List[str]:
        """Parse TELEGRAM_CHAT_ID into list (comma-separated). Legacy single-account."""
        raw = (self.TELEGRAM_CHAT_ID or "").strip()
        return [x.strip() for x in raw.split(",") if x.strip()]
    
    def get_account_configs(self) -> List[Tuple[str, List[str]]]:
        """
        Return list of (api_secret, chat_ids) for each configured account.
        - If MUDREX_API_SECRET_1 is set: use _1.._10 (each TELEGRAM_CHAT_ID_i for account i).
        - Else: single account (MUDREX_API_SECRET, TELEGRAM_CHAT_IDS).
        """
        configs = []
        for i in range(1, MAX_ACCOUNTS + 1):
            secret = (os.getenv(f"MUDREX_API_SECRET_{i}") or "").strip()
            if not secret:
                continue
            raw_chat = (os.getenv(f"TELEGRAM_CHAT_ID_{i}", "") or "").strip()
            chat_ids = [x.strip() for x in raw_chat.split(",") if x.strip()]
            if not chat_ids and self.TELEGRAM_CHAT_IDS:
                chat_ids = self.TELEGRAM_CHAT_IDS  # fallback to legacy for account 1
            configs.append((secret, chat_ids))
        if configs:
            return configs
        # Legacy: single account
        if self.MUDREX_API_SECRET and self.TELEGRAM_CHAT_IDS:
            return [(self.MUDREX_API_SECRET, self.TELEGRAM_CHAT_IDS)]
        return []
    
    # ==========================================================================
    # FUNDING RATE THRESHOLDS
    # ==========================================================================
    # Minimum funding rate to consider farming (0.6% = 0.006 for higher quality)
    EXTREME_RATE_THRESHOLD: float = 0.005
    
    # Very extreme rates for potentially larger positions
    VERY_EXTREME_THRESHOLD: float = 0.01  # 1%
    
    # ==========================================================================
    # ENTRY TIMING
    # ==========================================================================
    # Enter position in the last N seconds before settlement (minimize price exposure)
    # Entry allowed when seconds until settlement is between min and max
    ENTRY_MIN_SECONDS_BEFORE: int = 5    # At least 5 seconds before settlement
    ENTRY_MAX_SECONDS_BEFORE: int = 300  # Up to 5 minutes before settlement
    # When any opportunity has <= this many seconds to settlement, scan every ENTRY_FAST_SCAN_SECONDS
    ENTRY_FAST_SCAN_WHEN_SECONDS_LEFT: int = 600  # Fast-scan within 10 minutes of settlement
    ENTRY_FAST_SCAN_SECONDS: int = 3               # Every 3s in fast mode
    
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
    
    # Profit target for reversed position: % of MARGIN (0.2% = 0.002)
    # Must clear round-trip fees (~0.12% of notional); with leverage this scales
    REVERSAL_PROFIT_TARGET_PERCENT: float = 0.002
    
    # Maximum hold time for reversed position (minutes)
    # Exit reversed position after this time regardless of PnL
    REVERSAL_MAX_HOLD_MINUTES: int = 3
    
    # Seconds after settlement time before reversing (e.g. settlement 7:30 -> reverse at 7:30:01+)
    # Ensures exchange has applied settlement; avoid closing before settlement
    REVERSAL_CHECK_SECONDS_AFTER_SETTLEMENT: int = 1
    
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
    
    # Leverage range: auto-scales up to meet MIN_ORDER_VALUE_USD
    MIN_LEVERAGE: int = 2
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
        account_configs = self.get_account_configs()
        if not account_configs:
            if not self.MUDREX_API_SECRET:
                warnings.append("MUDREX_API_SECRET not set - set it or MUDREX_API_SECRET_1 for multi-account")
            if not self.TELEGRAM_CHAT_IDS and not any(os.getenv(f"TELEGRAM_CHAT_ID_{i}") for i in range(1, MAX_ACCOUNTS + 1)):
                warnings.append("TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID_1..10 not set - notifications disabled")
        else:
            for i, (secret, chat_ids) in enumerate(account_configs):
                if not chat_ids:
                    warnings.append(f"Account {i + 1}: TELEGRAM_CHAT_ID_{i + 1} not set - no notifications for this account")
        if self.MARGIN_PERCENTAGE is None:
            warnings.append("MARGIN_PERCENTAGE not set (set in Railway variables) - will not open new positions")
        elif self.MARGIN_PERCENTAGE <= 0 or self.MARGIN_PERCENTAGE > 100:
            warnings.append("MARGIN_PERCENTAGE must be between 1 and 100")
        if not self.TELEGRAM_BOT_TOKEN:
            warnings.append("TELEGRAM_BOT_TOKEN not set - notifications disabled")
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
