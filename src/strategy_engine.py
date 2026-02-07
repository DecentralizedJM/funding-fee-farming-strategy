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
    """Main strategy orchestration engine"""
    
    def __init__(self, config: FarmingConfig):
        self.config = config
        self.running = False
        
        # Initialize components
        self.fetcher = FundingDataFetcher(config.FUNDING_API_BASE_URL)
        self.executor = TradeExecutor(
            api_secret=config.MUDREX_API_SECRET
        )
        self.position_manager = PositionManager(
            executor=self.executor,
            state_file=config.STATE_FILE,
            trades_log_file=config.TRADES_LOG_FILE
        )
        self.notifier = TelegramNotifier(
            bot_token=config.TELEGRAM_BOT_TOKEN,
            chat_ids=config.TELEGRAM_CHAT_IDS
        )
        
        # Daily summary tracking
        self._last_summary_date = None
        self._daily_trades = 0
        self._daily_pnl = 0.0
        self._daily_funding = 0.0
        
        # Pause state for telegram control
        self._paused = False
        
        # Skip notification cache (symbol -> (reason, timestamp))
        self._skip_notification_cache = {}
        
        # Position reconciliation tracking
        self._last_reconciliation = None
        self._reconciliation_interval = timedelta(minutes=5)
        
        logger.info("Strategy engine initialized")
    
    def _notify_skip_throttled(self, symbol: str, reason: str) -> None:
        """Send skip notification if not sent recently"""
        now = datetime.now(timezone.utc)
        last_entry = self._skip_notification_cache.get(symbol)
        
        should_send = True
        if last_entry:
            last_reason, last_time = last_entry
            # Don't send if same reason and < 15 mins
            if last_reason == reason and (now - last_time) < timedelta(minutes=15):
                should_send = False
        
        if should_send:
            self.notifier.notify_skipped(symbol, reason)
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
                await self.scan_and_enter()
                
                # Manage existing positions (check exit conditions)
                await self.manage_exits()
                
                # Wait before next scan
                await asyncio.sleep(self.config.SCAN_INTERVAL_SECONDS)
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                self.notifier.notify_error("Main Loop Error", str(e))
                await asyncio.sleep(60)  # Wait a bit before retrying
    
    async def _check_daily_summary(self) -> None:
        """Check if we need to send daily summary (at midnight UTC)"""
        today = datetime.now(timezone.utc).date()
        
        if self._last_summary_date and today > self._last_summary_date:
            # New day - send summary for previous day
            stats = self.position_manager.get_performance_stats()
            
            self.notifier.notify_daily_summary(
                trades_count=self._daily_trades,
                total_pnl=self._daily_pnl,
                total_funding=self._daily_funding,
                win_rate=stats.get("win_rate", 0.0)
            )
            
            logger.info(f"Daily summary sent: {self._daily_trades} trades, ${self._daily_pnl:.4f} PnL")
            
            # Reset daily counters
            self._daily_trades = 0
            self._daily_pnl = 0.0
            self._daily_funding = 0.0
            self._last_summary_date = today
    
    def _record_trade_for_daily(self, pnl: float, funding: float) -> None:
        """Record a completed trade for daily summary"""
        self._daily_trades += 1
        self._daily_pnl += pnl
        self._daily_funding += funding
    
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
        
        local_positions = self.position_manager.get_active_positions()
        if not local_positions:
            return
        
        try:
            exchange_positions = self.executor.get_open_positions()
            exchange_position_ids = {p["position_id"] for p in exchange_positions}
            
            for position in local_positions:
                if position.position_id not in exchange_position_ids:
                    # Position exists locally but not on exchange - likely liquidated/closed
                    logger.warning(f"Reconciliation: Position {position.position_id} ({position.symbol}) not found on exchange. Cleaning up.")
                    
                    success, _, _ = self.position_manager.execute_exit(
                        position_id=position.position_id,
                        reason="Reconciliation: Position closed/liquidated on exchange"
                    )
                    
                    # Notify about the discrepancy
                    if success:
                        self.notifier.notify_error(
                            "Position Reconciliation",
                            f"{position.symbol} position was closed/liquidated externally"
                        )
            
            logger.debug(f"Position reconciliation complete: {len(local_positions)} local, {len(exchange_positions)} on exchange")
            
        except Exception as e:
            logger.error(f"Error during position reconciliation: {e}")
    
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
    
    async def scan_and_enter(self) -> None:
        """
        Scan for extreme funding opportunities and enter positions
        """
        # Skip if paused via /kill command
        if self._paused:
            return
            
        # Check daily loss limit
        if self._daily_pnl <= -self.config.MAX_DAILY_LOSS_USD:
            logger.warning(f"Daily loss limit reached (${self._daily_pnl:.2f} <= -${self.config.MAX_DAILY_LOSS_USD}). Pausing new entries.")
            return
        
        # Check if we can open more positions
        active_count = self.position_manager.get_active_count()
        if active_count >= self.config.MAX_CONCURRENT_POSITIONS:
            logger.debug(f"Max positions reached ({active_count}/{self.config.MAX_CONCURRENT_POSITIONS})")
            return
        
        # Scan for opportunities
        opportunities = self.fetcher.get_extreme_funding_opportunities(
            threshold=self.config.EXTREME_RATE_THRESHOLD
        )
        
        if not opportunities:
            logger.debug("No extreme funding opportunities found")
            return
        
        logger.info(f"Found {len(opportunities)} extreme funding opportunities")
        
        # Filter to entry window and execute
        for opp in opportunities:
            # Bug #6 fix: Re-fetch active count before each entry (not just once at start)
            # Between async entries, the actual count might have changed
            active_count = self.position_manager.get_active_count()
            if active_count >= self.config.MAX_CONCURRENT_POSITIONS:
                logger.debug(f"Max positions reached ({active_count}/{self.config.MAX_CONCURRENT_POSITIONS})")
                break
            
            # Skip if we already have a position for this symbol
            active_symbols = {
                p.symbol for p in self.position_manager.get_active_positions()
            }
            if opp["symbol"] in active_symbols:
                reason = "Already have active position"
                logger.info(f"Skipping {opp['symbol']}: {reason}")
                if self.config.NOTIFY_SKIPS:
                    self._notify_skip_throttled(opp["symbol"], reason)
                continue

            # Volume filter: skip low liquidity to avoid slippage
            volume_24h = opp.get("volume24h", 0) or 0
            if volume_24h < self.config.MIN_VOLUME_24H:
                logger.debug(f"Skipping {opp['symbol']}: volume ${volume_24h:,.0f} < ${self.config.MIN_VOLUME_24H:,.0f}")
                continue

            # Check if in entry window
            if self._is_in_entry_window(opp["nextFundingTime"]):
                await self._execute_entry(opp)
            else:
                time_to_settlement = self.fetcher.get_time_to_next_settlement(opp["nextFundingTime"])
                seconds_remaining = time_to_settlement.total_seconds()
                reason = f"Outside entry window ({seconds_remaining:.0f}s until settlement, window: {self.config.ENTRY_MIN_SECONDS_BEFORE}-{self.config.ENTRY_MAX_SECONDS_BEFORE}s)"
                logger.info(f"Skipping {opp['symbol']}: {reason}")
                if self.config.NOTIFY_SKIPS:
                    self._notify_skip_throttled(opp["symbol"], reason)
    
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
    
    async def _execute_entry(self, opportunity: Dict) -> bool:
        """
        Execute entry for a funding opportunity
        
        Args:
            opportunity: Opportunity dict from scanner
        
        Returns:
            True if entry was successful
        """
        symbol = opportunity["symbol"]
        side = opportunity["recommendedSide"]
        funding_rate = opportunity["fundingRate"]
        price = opportunity["lastPrice"]
        next_funding_time = opportunity["nextFundingTime"]
        
        time_to_settlement = self.fetcher.get_time_to_next_settlement(next_funding_time)
        
        logger.info(f"Attempting entry: {symbol} {side} @ rate {funding_rate*100:.4f}%")
        
        # Check if symbol is available on Mudrex (skip if not)
        if not self.executor.check_symbol_available(symbol):
            logger.warning(f"Symbol {symbol} not available on Mudrex - skipping")
            return False
            
        # --- SIDE CHECK (Safety Verification) ---
        # Verify Mark Price vs Last Price spread
        tickers = self.fetcher.get_tickers([symbol])
        ticker_data = tickers.get(symbol, {})
        mark_price = ticker_data.get("markPrice", price)
        last_price = ticker_data.get("lastPrice", price)
        
        if last_price > 0:
            spread_percent = abs(mark_price - last_price) / last_price
            if spread_percent > self.config.PRICE_SPREAD_THRESHOLD:
                logger.warning(f"Entry rejected: Price spread too high ({spread_percent*100:.2f}% > {self.config.PRICE_SPREAD_THRESHOLD*100:.2f}%) Mark: {mark_price}, Last: {last_price}")
                return False
        
        # Notify opportunity
        self.notifier.notify_opportunity_detected(
            symbol=symbol,
            funding_rate=funding_rate,
            recommended_side=side,
            time_to_settlement=str(time_to_settlement).split('.')[0],
            price=price
        )
        
        # Margin percentage must be set (e.g. via Railway variable MARGIN_PERCENTAGE)
        if self.config.MARGIN_PERCENTAGE is None or self.config.MARGIN_PERCENTAGE <= 0 or self.config.MARGIN_PERCENTAGE > 100:
            logger.warning("MARGIN_PERCENTAGE not set or invalid (use 1-100) - skipping entry")
            return False
        
        # Get futures balance and compute margin from percentage
        balance = self.executor.get_futures_balance()
        if balance is None or balance <= 0:
            logger.warning(f"Cannot get futures balance or balance is zero - skipping {symbol}")
            return False
        
        margin_usd = balance * (self.config.MARGIN_PERCENTAGE / 100.0)
        min_order = self.config.MIN_ORDER_VALUE_USD
        min_lev, max_lev = self.config.MIN_LEVERAGE, self.config.MAX_LEVERAGE
        
        # Need notional >= min_order. At max leverage: margin_usd * max_lev >= min_order
        if margin_usd * max_lev < min_order:
            logger.warning(
                f"Insufficient margin for {symbol}: ${margin_usd:.2f} * {max_lev}x = ${margin_usd * max_lev:.2f} < ${min_order}. Need at least ${min_order / max_lev:.2f} margin."
            )
            return False
        
        # Scale leverage to meet min order value: leverage >= min_order / margin_usd, clamp 10-25
        leverage_needed = math.ceil(min_order / margin_usd) if margin_usd > 0 else max_lev
        leverage = max(min_lev, min(max_lev, leverage_needed))
        
        # Clamp to asset max leverage
        instrument_info = self.fetcher.get_instrument_info(symbol)
        if instrument_info:
            max_asset = int(instrument_info.get("maxLeverage", 100))
            leverage = min(leverage, max_asset)
            if leverage < min_lev:
                logger.warning(f"Asset {symbol} max leverage {max_asset} < min {min_lev}x - skipping")
                return False
        
        # Calculate position size (notional = margin_usd * leverage, must be >= min_order)
        quantity = self.executor.calculate_position_size(
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

        # Calculate Stop Loss Price (critical for avoiding liquidation with high leverage)
        # Note: Exchange stop loss is price-based. Convert margin-based stop loss to price move.
        # With leverage, STOP_LOSS_PERCENT of margin = STOP_LOSS_PERCENT/leverage of price
        price_stop_percent = self.config.STOP_LOSS_PERCENT / leverage
        sl_price = None
        if side == "LONG":
            sl_price_val = price * (1 - price_stop_percent)
            sl_price = f"{sl_price_val:.4f}"
        else:
            sl_price_val = price * (1 + price_stop_percent)
            sl_price = f"{sl_price_val:.4f}"

        # Re-check timing before placing order (execution can take time)
        # If we're no longer in the entry window, abort to avoid missing settlement
        time_to_settlement = self.fetcher.get_time_to_next_settlement(next_funding_time)
        seconds_remaining = time_to_settlement.total_seconds()
        min_seconds = float(self.config.ENTRY_MIN_SECONDS_BEFORE)
        
        if seconds_remaining < min_seconds:
            logger.warning(f"Entry aborted: Only {seconds_remaining:.0f}s until settlement (min: {min_seconds:.0f}s). Would miss funding.")
            return False

        # Execute trade
        result = self.executor.open_position(
            symbol=symbol,
            side=side,
            quantity=quantity,
            leverage=leverage,
            stop_loss_price=sl_price
        )
        
        if result.success:
            # Create and track position
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
            
            self.position_manager.add_position(position)
            
            # Slippage check: verify execution price is within acceptable range
            actual_entry = result.entry_price or price
            slippage = abs(actual_entry - price) / price if price > 0 else 0
            
            if slippage > self.config.MAX_SLIPPAGE_PERCENT:
                logger.error(f"Excessive slippage on {symbol}: {slippage*100:.3f}% > {self.config.MAX_SLIPPAGE_PERCENT*100:.3f}%. Closing position immediately.")
                success, _, _ = self.position_manager.execute_exit(
                    position_id=result.position_id,
                    reason=f"Excessive slippage: {slippage*100:.3f}%",
                    exit_price=actual_entry
                )
                if success:
                    self.notifier.notify_error(
                        "Slippage Protection",
                        f"{symbol}: Entry slippage {slippage*100:.3f}% exceeded max {self.config.MAX_SLIPPAGE_PERCENT*100:.3f}%. Position closed."
                    )
                return False
            
            # Notify entry
            self.notifier.notify_entry(
                symbol=symbol,
                side=side,
                quantity=quantity,
                entry_price=actual_entry,
                leverage=leverage,
                expected_funding_rate=funding_rate,
                position_id=result.position_id
            )
            
            logger.info(f"Entry successful: {symbol} {side} qty={quantity} leverage={leverage}x slippage={slippage*100:.3f}%")
            return True
        else:
            logger.error(f"Entry failed: {result.error}")
            self.notifier.notify_error("Entry Failed", f"{symbol}: {result.error}")
            return False
    
    async def manage_exits(self) -> None:
        """
        Check exit conditions for all active positions and execute exits.
        
        For pre_settlement positions with reversal enabled:
        - After funding settlement, close and open opposite position
        
        For reversed positions:
        - Exit on profit target, max hold, or stop loss
        """
        positions = self.position_manager.get_active_positions()
        
        for position in positions:
            try:
                # Get current PnL
                current_pnl = self.executor.get_position_pnl(position.position_id)
                
                # If PnL fetch failed (None), check if position still exists
                if current_pnl is None:
                    logger.warning(f"Could not fetch PnL for {position.position_id}. strict-checking existence...")
                    open_positions = self.executor.get_open_positions()
                    is_open = any(p["position_id"] == position.position_id for p in open_positions)
                    
                    if not is_open:
                        logger.warning(f"Position {position.position_id} confirmed missing. Closing locally.")
                        success, _, _ = self.position_manager.execute_exit(
                            position_id=position.position_id,
                            reason="Force Close: Missing on exchange"
                        )
                        continue
                    else:
                        current_pnl = 0.0 # Default to 0 if temporary API error but position exists
                
                # Get current market data
                tickers = self.fetcher.get_tickers([position.symbol])
                ticker_data = tickers.get(position.symbol, {})
                exit_price = ticker_data.get("lastPrice", position.entry_price)
                current_funding_rate = ticker_data.get("fundingRate")
                
                # Verify funding was actually received (not just assume after 30s)
                # Only for pre_settlement positions
                now = datetime.now(timezone.utc)
                if position.phase == "pre_settlement" and not position.funding_received and now > position.funding_settlement_time:
                    time_since = now - position.funding_settlement_time
                    if time_since >= timedelta(seconds=30):
                        # Verify with API instead of assuming
                        settlement_ms = int(position.funding_settlement_time.timestamp() * 1000)
                        verification = self.fetcher.verify_funding_settlement(
                            position.symbol, settlement_ms
                        )
                        
                        if verification and verification.get("verified"):
                            # Use actual funding rate from API
                            actual_rate = verification["fundingRate"]
                            entry_value = float(position.quantity) * position.entry_price
                            actual_funding = entry_value * abs(actual_rate)
                            self.position_manager.mark_funding_received(
                                position.position_id, 
                                funding_amount=actual_funding
                            )
                            logger.info(f"Verified funding for {position.symbol}: actual rate={actual_rate*100:.4f}%, amount=${actual_funding:.4f}")
                        else:
                            # Fall back to estimated if API verification fails
                            entry_value = float(position.quantity) * position.entry_price
                            estimated_funding = entry_value * abs(position.expected_funding_rate)
                            self.position_manager.mark_funding_received(
                                position.position_id,
                                funding_amount=estimated_funding
                            )
                            logger.warning(f"Could not verify funding for {position.symbol}, using estimate: ${estimated_funding:.4f}")
                
                # ================================================================
                # POST-SETTLEMENT: If in profit exit; else reverse and exit in profit
                # ================================================================
                if (self.config.SETTLEMENT_REVERSAL_ENABLED and 
                    position.phase == "pre_settlement" and 
                    position.funding_received):
                    
                    total_pnl = current_pnl + position.funding_amount
                    if total_pnl > 0:
                        # In profit after settlement - exit immediately, no reversal
                        logger.info(f"Post-settlement profit for {position.symbol}: ${total_pnl:.4f} - exiting (no reversal)")
                        success, realized_pnl, funding_amount = self.position_manager.execute_exit(
                            position_id=position.position_id,
                            reason="Post-settlement profit exit",
                            exit_price=exit_price
                        )
                        if success:
                            entry_value = float(position.quantity) * position.entry_price
                            pnl_percent = (realized_pnl / entry_value * 100) if entry_value > 0 else 0
                            self._record_trade_for_daily(realized_pnl, position.funding_amount)
                            self.notifier.notify_exit(
                                symbol=position.symbol,
                                side=position.side,
                                entry_price=position.entry_price,
                                exit_price=exit_price,
                                pnl=realized_pnl,
                                pnl_percent=pnl_percent,
                                funding_received=position.funding_amount,
                                reason="Post-settlement profit exit",
                                hold_time=str(position.hold_duration).split('.')[0]
                            )
                        continue
                    else:
                        # Not in profit - reverse and exit reversed leg in profit / max hold / SL
                        await self._execute_settlement_reversal(position, current_pnl, exit_price)
                        continue
                
                # ================================================================
                # NORMAL EXIT LOGIC
                # ================================================================
                
                # Check exit conditions
                should_exit, reason = self.position_manager.should_exit(
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
                    
                    # Execute exit
                    success, realized_pnl, funding_amount = self.position_manager.execute_exit(
                        position_id=position.position_id,
                        reason=reason,
                        exit_price=exit_price
                    )
                    
                    if success:
                        # For reversed positions, realized_pnl already includes first leg
                        # For pre_settlement, realized_pnl = current_pnl + funding
                        pnl = realized_pnl
                        entry_value = float(position.quantity) * position.entry_price
                        pnl_percent = (pnl / entry_value * 100) if entry_value > 0 else 0
                        
                        # Determine funding for notification
                        # For reversed: funding was in first leg, stored in first_leg_funding
                        # For pre_settlement: funding is in funding_amount
                        if position.phase == "reversed":
                            funding_for_notification = position.first_leg_funding
                        else:
                            funding_for_notification = funding_amount
                        
                        # Record for daily summary
                        self._record_trade_for_daily(pnl, funding_for_notification)
                        
                        # Notify exit
                        self.notifier.notify_exit(
                            symbol=position.symbol,
                            side=position.side,
                            entry_price=position.entry_price,
                            exit_price=exit_price,
                            pnl=pnl,
                            pnl_percent=pnl_percent,
                            funding_received=funding_for_notification,
                            reason=reason,
                            hold_time=str(position.hold_duration).split('.')[0]
                        )
                        
            except Exception as e:
                logger.error(f"Error managing position {position.position_id}: {e}")
    
    async def _execute_settlement_reversal(
        self, 
        position: FarmingPosition, 
        current_pnl: float, 
        exit_price: float
    ) -> None:
        """
        Execute the settlement reversal: close pre_settlement position and open opposite.
        
        Args:
            position: The pre_settlement position to reverse
            current_pnl: Current unrealized PnL of the position
            exit_price: Current market price for exit
        """
        symbol = position.symbol
        original_position_id = position.position_id
        
        logger.info(f"Executing settlement reversal for {symbol}")
        
        # Calculate stop loss price for reversed position (opposite side)
        opposite_side = "SHORT" if position.side == "LONG" else "LONG"
        price_stop_percent = self.config.STOP_LOSS_PERCENT / position.leverage
        
        if opposite_side == "LONG":
            sl_price_val = exit_price * (1 - price_stop_percent)
        else:
            sl_price_val = exit_price * (1 + price_stop_percent)
        sl_price = f"{sl_price_val:.4f}"
        
        # Step 1: Close the pre_settlement position (skip trade log - will be logged with reversed)
        success, first_leg_pnl, first_leg_funding = self.position_manager.execute_exit(
            position_id=original_position_id,
            reason="Settlement reversal",
            exit_price=exit_price,
            skip_trade_log=True  # Don't log yet, will log combined PnL when reversed closes
        )
        
        if not success:
            logger.error(f"Failed to close pre_settlement position {original_position_id} for reversal")
            self.notifier.notify_error(
                "Reversal Failed",
                f"{symbol}: Could not close pre_settlement position"
            )
            return
        
        logger.info(f"Pre_settlement position closed. First leg PnL: ${first_leg_pnl:.4f}, Funding: ${first_leg_funding:.4f}")
        
        # Step 2: Open the reversed position (opposite side)
        result = self.executor.open_position(
            symbol=symbol,
            side=opposite_side,
            quantity=position.quantity,
            leverage=position.leverage,
            stop_loss_price=sl_price
        )
        
        if not result.success:
            logger.error(f"Failed to open reversed position for {symbol}: {result.error}")
            self.notifier.notify_error(
                "Reversal Failed",
                f"{symbol}: Pre_settlement closed (PnL: ${first_leg_pnl:.4f}) but reversed open failed: {result.error}"
            )
            # Log the first leg as a standalone trade since reversal failed
            # The trade wasn't logged because skip_trade_log=True, so we need to manually record it
            self._record_trade_for_daily(first_leg_pnl, first_leg_funding)
            return
        
        # Step 3: Create the reversed position
        reversed_position = FarmingPosition(
            position_id=result.position_id,
            symbol=symbol,
            side=opposite_side,
            quantity=position.quantity,
            entry_price=result.entry_price or exit_price,
            leverage=position.leverage,
            expected_funding_rate=0.0,  # No funding expected for reversed position
            funding_settlement_time=position.funding_settlement_time,  # Keep original for reference
            entry_time=datetime.now(timezone.utc),
            # Settlement reversal fields
            phase="reversed",
            parent_position_id=original_position_id,
            first_leg_pnl=first_leg_pnl,
            first_leg_funding=first_leg_funding
        )
        
        self.position_manager.add_position(reversed_position)
        
        logger.info(f"Reversed position opened: {symbol} {opposite_side} @ {result.entry_price}")
        
        # Notify reversal
        self.notifier.notify_reversal_opened(
            symbol=symbol,
            original_side=position.side,
            reversed_side=opposite_side,
            first_leg_pnl=first_leg_pnl,
            first_leg_funding=first_leg_funding,
            entry_price=result.entry_price or exit_price,
            position_id=result.position_id
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
        """Get current strategy status"""
        return {
            "running": self.running,
            "active_positions": self.position_manager.get_active_count(),
            "max_positions": self.config.MAX_CONCURRENT_POSITIONS,
            "performance": self.position_manager.get_performance_stats()
        }
