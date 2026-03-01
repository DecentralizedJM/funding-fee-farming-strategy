"""
Strategy Engine
================

Main orchestration engine for funding fee farming.
"""

import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict

from config import FarmingConfig
from funding_fetcher import FundingDataFetcher
from trade_executor import TradeExecutor
from position_manager import PositionManager, FarmingPosition
from telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class StrategyEngine:
    """Main strategy orchestration engine. Supports multiple API accounts (e.g. MUDREX_API_SECRET_1..10)."""
    
    def __init__(self, config: FarmingConfig, account_configs: List[tuple]):
        """
        account_configs: List of (api_secret, chat_ids) per account. From config.get_account_configs().
        """
        self.config = config
        self.running = False
        self.account_configs = account_configs
        self._n_accounts = len(account_configs)
        
        self.fetcher = FundingDataFetcher(config.FUNDING_API_BASE_URL)
        
        # One executor and one position manager per account
        self.executors: List[TradeExecutor] = []
        self.position_managers: List[PositionManager] = []
        for i, (api_secret, _) in enumerate(account_configs):
            ex = TradeExecutor(api_secret=api_secret)
            self.executors.append(ex)
            state_file = config.STATE_FILE.replace(".json", f"_{i + 1}.json")
            trades_log = config.TRADES_LOG_FILE.replace(".json", f"_{i + 1}.json")
            self.position_managers.append(PositionManager(executor=ex, state_file=state_file, trades_log_file=trades_log))
        
        # Backward compat: first executor/PM as default for code that indexes by 0
        self.executor = self.executors[0] if self.executors else None
        self.position_manager = self.position_managers[0] if self.position_managers else None
        
        chat_ids_by_account = [ac[1] for ac in account_configs]
        self.notifier = TelegramNotifier(bot_token=config.TELEGRAM_BOT_TOKEN, chat_ids_by_account=chat_ids_by_account)
        
        # Daily summary tracking (per account)
        self._last_summary_date = None
        self._daily_trades: List[int] = [0] * self._n_accounts
        self._daily_pnl: List[float] = [0.0] * self._n_accounts
        self._daily_funding: List[float] = [0.0] * self._n_accounts
        
        self._paused = False
        self._skip_notification_cache = {}
        self._last_reconciliation = None
        self._reconciliation_interval = timedelta(minutes=5)
        
        # Caches: symbol/instrument shared; balance per account
        self._symbol_available_cache: Dict[str, bool] = {}
        self._instrument_cache: Dict[str, Optional[Dict]] = {}
        self._cache_ttl = timedelta(minutes=5)
        self._symbol_cache_times: Dict[str, datetime] = {}
        self._cached_balance: List[Optional[float]] = [None] * self._n_accounts
        self._balance_cache_time: List[Optional[datetime]] = [None] * self._n_accounts
        
        logger.info("Strategy engine initialized with %d account(s)", self._n_accounts)
    
    def _get_cached_symbol_available(self, symbol: str) -> bool:
        """Check symbol availability from cache, fetching if stale/missing."""
        now = datetime.now(timezone.utc)
        cached_time = self._symbol_cache_times.get(symbol)
        if cached_time and (now - cached_time) < self._cache_ttl and symbol in self._symbol_available_cache:
            return self._symbol_available_cache[symbol]
        available = self.executor.check_symbol_available(symbol)
        self._symbol_available_cache[symbol] = available
        self._symbol_cache_times[symbol] = now
        return available

    def _get_cached_instrument_info(self, symbol: str) -> Optional[Dict]:
        """Get instrument info from cache, fetching if stale/missing."""
        now = datetime.now(timezone.utc)
        cached_time = self._symbol_cache_times.get(f"inst_{symbol}")
        if cached_time and (now - cached_time) < self._cache_ttl and symbol in self._instrument_cache:
            return self._instrument_cache[symbol]
        info = self.fetcher.get_instrument_info(symbol)
        self._instrument_cache[symbol] = info
        self._symbol_cache_times[f"inst_{symbol}"] = now
        return info

    def _get_cached_balance(self, account_index: int = 0) -> Optional[float]:
        """Get futures balance for the given account from cache, fetching if stale/missing."""
        now = datetime.now(timezone.utc)
        ct = self._balance_cache_time[account_index]
        cb = self._cached_balance[account_index]
        if ct and (now - ct) < self._cache_ttl and cb is not None:
            return cb
        balance = self.executors[account_index].get_futures_balance()
        self._cached_balance[account_index] = balance
        self._balance_cache_time[account_index] = now
        return balance

    def _invalidate_balance_cache(self, account_index: int) -> None:
        """Invalidate balance cache for one account (e.g. after trade)."""
        self._cached_balance[account_index] = None
        self._balance_cache_time[account_index] = None

    def _prefetch_opportunity_data(self, opportunities: List[Dict]) -> None:
        """Pre-fetch symbol availability, instrument info, and balance for upcoming opportunities.
        Called when opportunities are detected but still outside the entry window,
        so all data is warm in cache when the entry window opens."""
        for opp in opportunities:
            symbol = opp["symbol"]
            if symbol not in self._symbol_available_cache:
                self._get_cached_symbol_available(symbol)
            if symbol not in self._instrument_cache:
                self._get_cached_instrument_info(symbol)
        for i in range(self._n_accounts):
            if self._cached_balance[i] is None or self._balance_cache_time[i] is None:
                self._get_cached_balance(i)

    def _notify_skip_throttled(self, symbol: str, reason: str, account_index: Optional[int] = None) -> None:
        """Send skip notification at most once per symbol per hour."""
        now = datetime.now(timezone.utc)
        last_entry = self._skip_notification_cache.get(symbol)
        if last_entry:
            _, last_time = last_entry
            if (now - last_time) < timedelta(hours=1):
                return
        self.notifier.notify_skipped(symbol, reason, account_index=account_index)
        self._skip_notification_cache[symbol] = (reason, now)
        logger.debug(f"Sent skip notification for {symbol}: {reason}")
    
    async def run(self) -> None:
        """Main strategy loop"""
        self.running = True
        
        # Send startup notification
        self._notify_startup()
        
        # Initialize daily tracking
        self._last_summary_date = datetime.now(timezone.utc).date()
        
        logger.info("Starting funding fee farming strategy...")
        logger.info(f"Scan interval: {self.config.SCAN_INTERVAL_SECONDS}s")
        logger.info(f"Entry window: last {self.config.ENTRY_MIN_SECONDS_BEFORE}-{self.config.ENTRY_MAX_SECONDS_BEFORE}s before settlement")
        logger.info(f"Threshold: {self.config.EXTREME_RATE_THRESHOLD * 100:.2f}%")
        
        while self.running:
            try:
                # Check for daily summary (at midnight UTC)
                await self._check_daily_summary()
                
                # Periodic position reconciliation with exchange
                await self._reconcile_positions()
                
                # Scan for opportunities and enter if appropriate
                min_seconds_to_settlement = await self.scan_and_enter()
                
                # Manage existing positions (check exit conditions)
                await self.manage_exits()
                
                # Adaptive sleep: when an opportunity is close to settlement, scan every second
                # so we don't miss the entry window (30s scan would skip past it)
                if (min_seconds_to_settlement is not None and
                    0 < min_seconds_to_settlement <= self.config.ENTRY_FAST_SCAN_WHEN_SECONDS_LEFT):
                    sleep_seconds = self.config.ENTRY_FAST_SCAN_SECONDS
                    logger.debug(f"Fast scan: {min_seconds_to_settlement:.0f}s to settlement, sleeping {sleep_seconds}s")
                else:
                    sleep_seconds = self.config.SCAN_INTERVAL_SECONDS
                await asyncio.sleep(sleep_seconds)
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                self.notifier.notify_error("Main Loop Error", str(e))
                await asyncio.sleep(60)  # Wait a bit before retrying
    
    async def _check_daily_summary(self) -> None:
        """Check if we need to send daily summary (at midnight UTC). One summary per account."""
        today = datetime.now(timezone.utc).date()
        
        if self._last_summary_date and today > self._last_summary_date:
            for account_index in range(self._n_accounts):
                stats = self.position_managers[account_index].get_performance_stats()
                self.notifier.notify_daily_summary(
                    trades_count=self._daily_trades[account_index],
                    total_pnl=self._daily_pnl[account_index],
                    total_funding=self._daily_funding[account_index],
                    win_rate=stats.get("win_rate", 0.0),
                    account_index=account_index
                )
                logger.info(
                    f"Daily summary sent (account {account_index + 1}): {self._daily_trades[account_index]} trades, ${self._daily_pnl[account_index]:.4f} PnL"
                )
            # Reset daily counters for all accounts
            self._daily_trades = [0] * self._n_accounts
            self._daily_pnl = [0.0] * self._n_accounts
            self._daily_funding = [0.0] * self._n_accounts
            self._last_summary_date = today
    
    def _record_trade_for_daily(self, account_index: int, pnl: float, funding: float) -> None:
        """Record a completed trade for daily summary for the given account."""
        self._daily_trades[account_index] += 1
        self._daily_pnl[account_index] += pnl
        self._daily_funding[account_index] += funding
    
    async def _reconcile_positions(self) -> None:
        """
        Periodically reconcile local positions with exchange.
        Detects liquidated/closed positions and cleans up local state.
        """
        now = datetime.now(timezone.utc)
        
        # Only run every 5 minutes
        if self._last_reconciliation:
            if now - self._last_reconciliation < self._reconciliation_interval:
                return
        
        self._last_reconciliation = now
        
        for account_index in range(self._n_accounts):
            local_positions = self.position_managers[account_index].get_active_positions()
            if not local_positions:
                continue
            try:
                exchange_positions = self.executors[account_index].get_open_positions()
                exchange_position_ids = {p["position_id"] for p in exchange_positions}
                for position in local_positions:
                    if position.position_id not in exchange_position_ids:
                        logger.warning(
                            f"Reconciliation (account {account_index + 1}): Position {position.position_id} ({position.symbol}) not found on exchange. Cleaning up."
                        )
                        tickers = self.fetcher.get_tickers([position.symbol])
                        ticker_data = tickers.get(position.symbol, {})
                        recon_price = ticker_data.get("lastPrice", position.entry_price)
                        success, _, _ = self.position_managers[account_index].execute_exit(
                            position_id=position.position_id,
                            reason="Reconciliation: Position closed/liquidated on exchange",
                            exit_price=recon_price
                        )
                        if success:
                            self.notifier.notify_error(
                                "Position Reconciliation",
                                f"{position.symbol} position was closed/liquidated externally",
                                account_index=account_index
                            )
                logger.debug(
                    f"Position reconciliation complete (account {account_index + 1}): {len(local_positions)} local, {len(exchange_positions)} on exchange"
                )
            except Exception as e:
                logger.error(f"Error during position reconciliation (account {account_index + 1}): {e}")
    
    def pause(self) -> None:
        """Pause the strategy (stop entering new positions)"""
        self._paused = True
        logger.info("Strategy PAUSED - will not enter new positions")
    
    def resume(self) -> None:
        """Resume the strategy"""
        self._paused = False
        logger.info("Strategy RESUMED - actively scanning for opportunities")
    
    def stop(self) -> None:
        """Stop the strategy"""
        self.running = False
        logger.info("Strategy stopped")
    
    async def scan_and_enter(self) -> Optional[float]:
        """
        Scan for extreme funding opportunities and enter positions.
        Runs per account: each account can open up to MAX_CONCURRENT_POSITIONS.
        Returns minimum seconds until settlement among considered opportunities, or None.
        """
        if self._paused:
            return None

        opportunities = self.fetcher.get_extreme_funding_opportunities(
            threshold=self.config.EXTREME_RATE_THRESHOLD
        )
        if not opportunities:
            logger.debug("No extreme funding opportunities found")
            return None

        logger.info(f"Found {len(opportunities)} extreme funding opportunities")
        self._prefetch_opportunity_data(opportunities)
        min_seconds_to_settlement: Optional[float] = None

        for opp in opportunities:
            time_to_settlement = self.fetcher.get_time_to_next_settlement(opp["nextFundingTime"])
            secs = time_to_settlement.total_seconds()
            if min_seconds_to_settlement is None or secs < min_seconds_to_settlement:
                min_seconds_to_settlement = secs

            volume_24h = opp.get("volume24h", 0) or 0
            if volume_24h < self.config.MIN_VOLUME_24H:
                logger.debug(f"Skipping {opp['symbol']}: volume ${volume_24h:,.0f} < ${self.config.MIN_VOLUME_24H:,.0f}")
                continue

            if not self._is_in_entry_window(opp["nextFundingTime"]):
                reason = f"Outside entry window ({secs:.0f}s until settlement, window: {self.config.ENTRY_MIN_SECONDS_BEFORE}-{self.config.ENTRY_MAX_SECONDS_BEFORE}s)"
                if secs <= self.config.ENTRY_FAST_SCAN_WHEN_SECONDS_LEFT:
                    logger.info(f"Skipping {opp['symbol']}: {reason}")
                else:
                    logger.debug(f"Skipping {opp['symbol']}: {reason}")
                if self.config.NOTIFY_SKIPS:
                    self._notify_skip_throttled(opp["symbol"], reason, account_index=None)
                continue

            # In entry window: try each account
            for account_index in range(self._n_accounts):
                if self._daily_pnl[account_index] <= -self.config.MAX_DAILY_LOSS_USD:
                    logger.debug(f"Account {account_index + 1}: daily loss limit reached, skipping new entries")
                    continue
                active_count = self.position_managers[account_index].get_active_count()
                if active_count >= self.config.MAX_CONCURRENT_POSITIONS:
                    logger.debug(f"Account {account_index + 1}: max positions ({active_count}/{self.config.MAX_CONCURRENT_POSITIONS})")
                    continue
                active_symbols = {
                    p.symbol for p in self.position_managers[account_index].get_active_positions()
                }
                if opp["symbol"] in active_symbols:
                    reason = "Already have active position"
                    logger.info(f"Skipping {opp['symbol']} (account {account_index + 1}): {reason}")
                    if self.config.NOTIFY_SKIPS:
                        self._notify_skip_throttled(opp["symbol"], reason, account_index=account_index)
                    continue
                await self._execute_entry(opp, account_index)

        return min_seconds_to_settlement
    
    def _is_in_entry_window(self, next_funding_time_ms: int) -> bool:
        """
        Check if we're in the entry window before settlement (last 1-10 seconds).
        
        Args:
            next_funding_time_ms: Next funding time in milliseconds
        
        Returns:
            True if in entry window
        """
        if not next_funding_time_ms:
            return False
        
        time_to_settlement = self.fetcher.get_time_to_next_settlement(next_funding_time_ms)
        seconds_remaining = time_to_settlement.total_seconds()
        
        return (
            self.config.ENTRY_MIN_SECONDS_BEFORE <= seconds_remaining <= 
            self.config.ENTRY_MAX_SECONDS_BEFORE
        )
    
    async def _execute_entry(self, opportunity: Dict, account_index: int = 0) -> bool:
        """
        Execute entry for a funding opportunity for the given account.
        Uses pre-cached data to minimize API latency in the critical entry path.
        """
        symbol = opportunity["symbol"]
        side = opportunity["recommendedSide"]
        funding_rate = opportunity["fundingRate"]
        price = opportunity["lastPrice"]
        mark_price = opportunity.get("markPrice", price)
        next_funding_time = opportunity["nextFundingTime"]
        executor = self.executors[account_index]
        position_manager = self.position_managers[account_index]

        time_to_settlement = self.fetcher.get_time_to_next_settlement(next_funding_time)
        
        logger.info(f"Attempting entry: {symbol} {side} @ rate {funding_rate*100:.4f}%")
        
        # Use cached symbol availability (pre-fetched in scan_and_enter)
        if not self._get_cached_symbol_available(symbol):
            logger.warning(f"Symbol {symbol} not available on Mudrex - skipping")
            return False
        
        # Price spread check using data already in the opportunity dict (no extra API call)
        if price > 0:
            spread_percent = abs(mark_price - price) / price
            if spread_percent > self.config.PRICE_SPREAD_THRESHOLD:
                logger.warning(f"Entry rejected: Price spread too high ({spread_percent*100:.2f}%) Mark: {mark_price}, Last: {price}")
                return False
        
        self.notifier.notify_opportunity_detected(
            symbol=symbol,
            funding_rate=funding_rate,
            recommended_side=side,
            time_to_settlement=str(time_to_settlement).split('.')[0],
            price=price,
            account_index=account_index
        )
        
        if self.config.MARGIN_PERCENTAGE is None or self.config.MARGIN_PERCENTAGE <= 0 or self.config.MARGIN_PERCENTAGE > 100:
            logger.warning("MARGIN_PERCENTAGE not set or invalid (use 1-100) - skipping entry")
            return False
        
        balance = self._get_cached_balance(account_index)
        if balance is None or balance <= 0:
            logger.warning(f"Cannot get futures balance or balance is zero - skipping {symbol}")
            return False
        
        margin_usd = balance * (self.config.MARGIN_PERCENTAGE / 100.0)
        min_order = self.config.MIN_ORDER_VALUE_USD
        min_lev, max_lev = self.config.MIN_LEVERAGE, self.config.MAX_LEVERAGE
        
        if margin_usd * max_lev < min_order:
            logger.warning(
                f"Insufficient margin for {symbol}: ${margin_usd:.2f} * {max_lev}x = ${margin_usd * max_lev:.2f} < ${min_order}. Need at least ${min_order / max_lev:.2f} margin."
            )
            return False
        
        leverage_needed = math.ceil(min_order / margin_usd) if margin_usd > 0 else max_lev
        leverage = max(min_lev, min(max_lev, leverage_needed))
        
        # Use cached instrument info (pre-fetched in scan_and_enter)
        instrument_info = self._get_cached_instrument_info(symbol)
        if instrument_info:
            max_asset = int(instrument_info.get("maxLeverage", 100))
            leverage = min(leverage, max_asset)
            if leverage < min_lev:
                logger.warning(f"Asset {symbol} max leverage {max_asset} < min {min_lev}x - skipping")
                return False
        
        quantity = executor.calculate_position_size(
            symbol=symbol,
            price=price,
            leverage=leverage,
            margin_usd=margin_usd,
            min_order_value_usd=min_order
        )
        
        if not quantity:
            logger.error(f"Could not calculate position size for {symbol}")
            return False
        
        notional = margin_usd * leverage
        if notional < min_order:
            logger.warning(f"Position notional ${notional:.2f} < min ${min_order} for {symbol} - skipping")
            return False

        # SL based on Bybit LTP (Mudrex SL triggers on LTP, not mark price)
        price_stop_percent = self.config.STOP_LOSS_PERCENT / leverage
        if side == "LONG":
            sl_price = f"{price * (1 - price_stop_percent):.4f}"
        else:
            sl_price = f"{price * (1 + price_stop_percent):.4f}"

        # Only abort if settlement has already passed (not just "close to it")
        seconds_remaining = self.fetcher.get_time_to_next_settlement(next_funding_time).total_seconds()
        if seconds_remaining < 0:
            logger.warning(f"Entry aborted: Settlement already passed ({seconds_remaining:.0f}s ago)")
            return False

        # Re-verify funding rate is still extreme (opportunity data may be stale)
        fresh_tickers = self.fetcher.get_tickers([symbol])
        fresh_data = fresh_tickers.get(symbol)
        if fresh_data:
            fresh_rate = fresh_data.get("fundingRate", 0)
            if abs(fresh_rate) < self.config.EXTREME_RATE_THRESHOLD:
                logger.warning(
                    f"Entry aborted: {symbol} funding rate dropped to {fresh_rate*100:.4f}% "
                    f"(threshold {self.config.EXTREME_RATE_THRESHOLD*100:.2f}%)"
                )
                return False
            price = fresh_data.get("lastPrice", price)
            funding_rate = fresh_rate

        result = executor.open_position(
            symbol=symbol,
            side=side,
            quantity=quantity,
            leverage=leverage,
            stop_loss_price=sl_price
        )
        
        if result.success:
            self._invalidate_balance_cache(account_index)
            settlement_time = datetime.fromtimestamp(
                next_funding_time / 1000, tz=timezone.utc
            )
            position = FarmingPosition(
                position_id=result.position_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                entry_price=result.entry_price or price,
                leverage=leverage,
                expected_funding_rate=funding_rate,
                funding_settlement_time=settlement_time,
                entry_time=datetime.now(timezone.utc)
            )
            position_manager.add_position(position)
            actual_entry = result.entry_price or price
            slippage = abs(actual_entry - price) / price if price > 0 else 0
            if slippage > self.config.MAX_SLIPPAGE_PERCENT:
                logger.error(f"Excessive slippage on {symbol}: {slippage*100:.3f}% > {self.config.MAX_SLIPPAGE_PERCENT*100:.3f}%. Closing position immediately.")
                success, _, _ = position_manager.execute_exit(
                    position_id=result.position_id,
                    reason=f"Excessive slippage: {slippage*100:.3f}%",
                    exit_price=actual_entry
                )
                if success:
                    self.notifier.notify_error(
                        "Slippage Protection",
                        f"{symbol}: Entry slippage {slippage*100:.3f}% exceeded max {self.config.MAX_SLIPPAGE_PERCENT*100:.3f}%. Position closed.",
                        account_index=account_index
                    )
                return False
            self.notifier.notify_entry(
                symbol=symbol,
                side=side,
                quantity=quantity,
                entry_price=actual_entry,
                leverage=leverage,
                expected_funding_rate=funding_rate,
                position_id=result.position_id,
                account_index=account_index
            )
            logger.info(f"Entry successful (account {account_index + 1}): {symbol} {side} qty={quantity} leverage={leverage}x slippage={slippage*100:.3f}%")
            return True
        logger.error(f"Entry failed (account {account_index + 1}): {result.error}")
        self.notifier.notify_error("Entry Failed", f"{symbol}: {result.error}", account_index=account_index)
        return False
    
    async def manage_exits(self) -> None:
        """
        Check exit conditions for all active positions and execute exits.
        Runs per account: each account's positions are managed with its executor/PM.
        """
        for account_index in range(self._n_accounts):
            executor = self.executors[account_index]
            position_manager = self.position_managers[account_index]
            positions = position_manager.get_active_positions()
            for position in positions:
                try:
                    tickers = self.fetcher.get_tickers([position.symbol])
                    ticker_data = tickers.get(position.symbol, {})
                    ltp = ticker_data.get("lastPrice", position.entry_price)
                    exit_price = ltp
                    current_funding_rate = ticker_data.get("fundingRate")
                    qty = float(position.quantity)
                    direction = 1.0 if position.side == "LONG" else -1.0
                    current_pnl = (ltp - position.entry_price) * qty * direction
                    if not ticker_data:
                        logger.warning(f"No Bybit ticker for {position.symbol}. Checking if position exists on exchange...")
                        open_positions = executor.get_open_positions()
                        is_open = any(p["position_id"] == position.position_id for p in open_positions)
                        if not is_open:
                            logger.warning(f"Position {position.position_id} confirmed missing. Closing locally.")
                            success, _, _ = position_manager.execute_exit(
                                position_id=position.position_id,
                                reason="Force Close: Missing on exchange",
                                exit_price=ltp
                            )
                            continue
                
                    now = datetime.now(timezone.utc)
                    entry_value = float(position.quantity) * position.entry_price
                    if position.phase == "pre_settlement" and not position.funding_received and now > position.funding_settlement_time:
                        time_since = now - position.funding_settlement_time
                        if time_since >= timedelta(seconds=30):
                            settlement_ms = int(position.funding_settlement_time.timestamp() * 1000)
                            verification = self.fetcher.verify_funding_settlement(
                                position.symbol, settlement_ms
                            )
                            if verification and verification.get("verified"):
                                actual_rate = verification["fundingRate"]
                                actual_funding = entry_value * abs(actual_rate)
                                position_manager.mark_funding_received(
                                    position.position_id, funding_amount=actual_funding
                                )
                                logger.info(f"Verified funding for {position.symbol}: actual rate={actual_rate*100:.4f}%, amount=${actual_funding:.4f}")
                            else:
                                estimated_funding = entry_value * abs(position.expected_funding_rate)
                                position_manager.mark_funding_received(
                                    position.position_id, funding_amount=estimated_funding
                                )
                                logger.warning(f"Could not verify funding for {position.symbol}, using estimate: ${estimated_funding:.4f}")
                    seconds_after_settlement = (now - position.funding_settlement_time).total_seconds() if now > position.funding_settlement_time else 0
                    if (self.config.SETTLEMENT_REVERSAL_ENABLED and 
                        position.phase == "pre_settlement" and 
                        seconds_after_settlement >= self.config.REVERSAL_CHECK_SECONDS_AFTER_SETTLEMENT):
                        funding_for_pnl = position.funding_amount if position.funding_received else (entry_value * abs(position.expected_funding_rate))
                        if not position.funding_received:
                            position_manager.mark_funding_received(position.position_id, funding_amount=funding_for_pnl)
                        logger.info(f"Settlement done for {position.symbol}: mandatory reversal (PnL=${current_pnl:.4f}, funding=${funding_for_pnl:.4f})")
                        await self._execute_settlement_reversal(position, current_pnl, exit_price, account_index)
                        continue
                    should_exit, reason = position_manager.should_exit(
                    position=position,
                    current_pnl=current_pnl,
                    current_funding_rate=current_funding_rate,
                    min_profit_percent=self.config.MIN_PROFIT_PERCENT,
                    stop_loss_percent=self.config.STOP_LOSS_PERCENT,
                    soft_loss_percent=self.config.SOFT_LOSS_EXIT_PERCENT,
                    trailing_stop_enabled=self.config.TRAILING_STOP_ENABLED,
                    trailing_activation_percent=self.config.TRAILING_ACTIVATION_PERCENT,
                    trailing_callback_percent=self.config.TRAILING_CALLBACK_PERCENT,
                    max_hold_minutes=self.config.MAX_HOLD_MINUTES_AFTER_SETTLEMENT,
                    # Settlement reversal parameters
                    settlement_reversal_enabled=self.config.SETTLEMENT_REVERSAL_ENABLED,
                    reversal_profit_target_percent=self.config.REVERSAL_PROFIT_TARGET_PERCENT,
                    reversal_max_hold_minutes=self.config.REVERSAL_MAX_HOLD_MINUTES
                )
                
                    if should_exit:
                        logger.info(f"Exiting {position.symbol}: {reason}")
                        success, realized_pnl, funding_amount = position_manager.execute_exit(
                            position_id=position.position_id,
                            reason=reason,
                            exit_price=exit_price
                        )
                        if success:
                            pnl = realized_pnl
                            entry_value = float(position.quantity) * position.entry_price
                            pnl_percent = (pnl / entry_value * 100) if entry_value > 0 else 0
                            if position.phase == "reversed":
                                funding_for_notification = position.first_leg_funding
                            else:
                                funding_for_notification = funding_amount
                            self._record_trade_for_daily(account_index, pnl, funding_for_notification)
                            self.notifier.notify_exit(
                                symbol=position.symbol,
                                side=position.side,
                                entry_price=position.entry_price,
                                exit_price=exit_price,
                                pnl=pnl,
                                pnl_percent=pnl_percent,
                                funding_received=funding_for_notification,
                                reason=reason,
                                hold_time=str(position.hold_duration).split('.')[0],
                                account_index=account_index
                            )
                except Exception as e:
                    logger.error(f"Error managing position {position.position_id}: {e}")
    
    async def _execute_settlement_reversal(
        self,
        position: FarmingPosition,
        current_pnl: float,
        exit_price: float,
        account_index: int
    ) -> None:
        """Execute settlement reversal for the given account: close pre_settlement, open opposite."""
        symbol = position.symbol
        original_position_id = position.position_id
        opposite_side = "SHORT" if position.side == "LONG" else "LONG"
        executor = self.executors[account_index]
        position_manager = self.position_managers[account_index]
        logger.info(f"Executing settlement reversal for {symbol} (account {account_index + 1})")
        success, first_leg_pnl, first_leg_funding = position_manager.execute_exit(
            position_id=original_position_id,
            reason="Settlement reversal",
            exit_price=exit_price,
            skip_trade_log=True
        )
        if not success:
            logger.error(f"Failed to close pre_settlement position {original_position_id} for reversal")
            self.notifier.notify_error(
                "Reversal Failed",
                f"{symbol}: Could not close pre_settlement position",
                account_index=account_index
            )
            return
        logger.info(f"Pre_settlement position closed. First leg PnL: ${first_leg_pnl:.4f}, Funding: ${first_leg_funding:.4f}")
        self._invalidate_balance_cache(account_index)
        await asyncio.sleep(3)
        tickers = self.fetcher.get_tickers([symbol])
        ticker_data = tickers.get(symbol, {})
        fresh_ltp = ticker_data.get("lastPrice", exit_price)
        price_stop_percent = self.config.STOP_LOSS_PERCENT / position.leverage
        if opposite_side == "LONG":
            sl_price_val = fresh_ltp * (1 - price_stop_percent)
        else:
            sl_price_val = fresh_ltp * (1 + price_stop_percent)
        sl_price = f"{sl_price_val:.4f}"
        result = None
        for attempt in range(1, 4):
            result = executor.open_position(
                symbol=symbol,
                side=opposite_side,
                quantity=position.quantity,
                leverage=position.leverage,
                stop_loss_price=sl_price
            )
            if result.success:
                break
            if attempt < 3:
                wait = 2 * attempt
                logger.warning(f"Reversed open failed (attempt {attempt}/3): {result.error}. Retrying in {wait}s...")
                await asyncio.sleep(wait)
        if not result or not result.success:
            err_msg = result.error if result else "Unknown error"
            logger.error(f"Failed to open reversed position for {symbol}: {err_msg}")
            self.notifier.notify_error(
                "Reversal Failed",
                f"{symbol}: Pre_settlement closed (PnL: ${first_leg_pnl:.4f}) but reversed open failed: {err_msg}",
                account_index=account_index
            )
            self._record_trade_for_daily(account_index, first_leg_pnl, first_leg_funding)
            return
        reversed_position = FarmingPosition(
            position_id=result.position_id,
            symbol=symbol,
            side=opposite_side,
            quantity=position.quantity,
            entry_price=result.entry_price or fresh_ltp,
            leverage=position.leverage,
            expected_funding_rate=0.0,
            funding_settlement_time=position.funding_settlement_time,
            entry_time=datetime.now(timezone.utc),
            phase="reversed",
            parent_position_id=original_position_id,
            first_leg_pnl=first_leg_pnl,
            first_leg_funding=first_leg_funding
        )
        position_manager.add_position(reversed_position)
        logger.info(f"Reversed position opened: {symbol} {opposite_side} @ {result.entry_price}")
        self.notifier.notify_reversal_opened(
            symbol=symbol,
            original_side=position.side,
            reversed_side=opposite_side,
            first_leg_pnl=first_leg_pnl,
            first_leg_funding=first_leg_funding,
            entry_price=result.entry_price or exit_price,
            position_id=result.position_id,
            account_index=account_index
        )
    
    def _notify_startup(self) -> None:
        """Send startup notification with config summary"""
        
        config_summary = f"""
<b>Mode:</b> LIVE
<b>Threshold:</b> {self.config.EXTREME_RATE_THRESHOLD * 100:.2f}%
<b>Entry Window:</b> last {self.config.ENTRY_MIN_SECONDS_BEFORE}-{self.config.ENTRY_MAX_SECONDS_BEFORE}s
<b>Max Positions:</b> {self.config.MAX_CONCURRENT_POSITIONS}
<b>Margin:</b> {self.config.MARGIN_PERCENTAGE or 'NOT SET'}% of futures balance
<b>Leverage:</b> {self.config.MIN_LEVERAGE}-{self.config.MAX_LEVERAGE}x
<b>Min Order:</b> ${self.config.MIN_ORDER_VALUE_USD}
"""
        self.notifier.notify_startup(config_summary.strip())
    
    def get_status(self) -> dict:
        """Get current strategy status (aggregated across accounts)."""
        total_active = sum(pm.get_active_count() for pm in self.position_managers)
        return {
            "running": self.running,
            "active_positions": total_active,
            "max_positions": self.config.MAX_CONCURRENT_POSITIONS * self._n_accounts,
            "accounts": self._n_accounts,
            "performance": self.position_managers[0].get_performance_stats() if self.position_managers else {}
        }
