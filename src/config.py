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
    # ENTRY TIMING (Post-Settlement Momentum)
    # ==========================================================================
    # Strategy: enter AFTER settlement, riding the post-settlement momentum.
    # Empirical data shows price continues moving in the trend direction after
    # settlement (e.g. -0.7% avg in 5 min for negative-funding assets).
    
    # Wait this many seconds after settlement before entering (let order book settle)
    POST_SETTLEMENT_DELAY_SECONDS: int = 10
    # Enter within this many seconds after settlement (entry window closes after this)
    POST_SETTLEMENT_WINDOW_SECONDS: int = 120
    
    # Pre-settlement watchlist: start tracking symbols this many seconds before settlement
    WATCHLIST_SECONDS_BEFORE_SETTLEMENT: int = 600
    
    # Fast scan: every N seconds when near settlement (before and after)
    FAST_SCAN_WHEN_SECONDS_LEFT: int = 600
    FAST_SCAN_SECONDS: int = 3
    
    # ==========================================================================
    # RISK MANAGEMENT
    # ==========================================================================
    # Stop loss: percentage of NOTIONAL position value
    # Data shows 5-min range around settlement averages 1.2% for high-rate assets
    STOP_LOSS_PERCENT: float = 0.005  # 0.5% of notional
    
    # Maximum daily loss in USD (across all trades)
    MAX_DAILY_LOSS_USD: float = 10.0
    
    # ==========================================================================
    # EXIT STRATEGY (Momentum-based)
    # ==========================================================================
    # Data shows avg post-settlement move of 0.7-0.9% in 5-30 min for extreme rates.
    # Strategy: capture the bulk of the move with trailing stop.
    
    # Take profit: exit if PnL reaches this % of notional
    TAKE_PROFIT_PERCENT: float = 0.008  # 0.8%
    
    # Trailing stop: let winners run, lock in gains
    TRAILING_STOP_ENABLED: bool = True
    TRAILING_ACTIVATION_PERCENT: float = 0.003  # Activate at 0.3% profit
    TRAILING_CALLBACK_PERCENT: float = 0.002    # Exit if drops 0.2% from peak
    
    # Hard time limit: force exit if still open after this many minutes
    MAX_HOLD_MINUTES: int = 15
    
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
        """Total fees for entry + exit (taker + slippage, round-trip)"""
        return (self.TAKER_FEE_PERCENT + self.SLIPPAGE_BUFFER_PERCENT) * 2

# Don't create global instance - let main.py create it after env vars are loaded
# config = FarmingConfig()
