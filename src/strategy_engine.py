"""
Strategy Engine
================

Main orchestration engine for funding fee farming.
"""

import asyncio
import logging
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
            chat_id=config.TELEGRAM_CHAT_ID
        )
        
        # Daily summary tracking
        self._last_summary_date = None
        self._daily_trades = 0
        self._daily_pnl = 0.0
        self._daily_funding = 0.0
        
        # Pause state for telegram control
        self._paused = False
        
        logger.info("Strategy engine initialized")
    
    async def run(self) -> None:
        """Main strategy loop"""
        self.running = True
        
        # Send startup notification
        self._notify_startup()
        
        # Initialize daily tracking
        self._last_summary_date = datetime.now(timezone.utc).date()
        
        logger.info("Starting funding fee farming strategy...")
        logger.info(f"Scan interval: {self.config.SCAN_INTERVAL_SECONDS}s")
        logger.info(f"Entry window: {self.config.ENTRY_MIN_MINUTES_BEFORE}-{self.config.ENTRY_MAX_MINUTES_BEFORE} mins before settlement")
        logger.info(f"Threshold: {self.config.EXTREME_RATE_THRESHOLD * 100:.2f}%")
        
        while self.running:
            try:
                # Check for daily summary (at midnight UTC)
                await self._check_daily_summary()
                
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
            if active_count >= self.config.MAX_CONCURRENT_POSITIONS:
                break
            
            # Skip if we already have a position for this symbol
            active_symbols = {
                p.symbol for p in self.position_manager.get_active_positions()
            }
            if opp["symbol"] in active_symbols:
                continue
            
            # Check if in entry window
            if self._is_in_entry_window(opp["nextFundingTime"]):
                success = await self._execute_entry(opp)
                if success:
                    active_count += 1
    
    def _is_in_entry_window(self, next_funding_time_ms: int) -> bool:
        """
        Check if we're in the entry window before settlement
        
        Args:
            next_funding_time_ms: Next funding time in milliseconds
        
        Returns:
            True if in entry window
        """
        if not next_funding_time_ms:
            return False
        
        time_to_settlement = self.fetcher.get_time_to_next_settlement(next_funding_time_ms)
        minutes_remaining = time_to_settlement.total_seconds() / 60
        
        return (
            self.config.ENTRY_MIN_MINUTES_BEFORE <= minutes_remaining <= 
            self.config.ENTRY_MAX_MINUTES_BEFORE
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
        
        # Notify opportunity
        self.notifier.notify_opportunity_detected(
            symbol=symbol,
            funding_rate=funding_rate,
            recommended_side=side,
            time_to_settlement=str(time_to_settlement).split('.')[0],
            price=price
        )
        
        # Get asset info for leverage and sizing
        instrument_info = self.fetcher.get_instrument_info(symbol)
        
        # Determine leverage
        if self.config.USE_MAX_LEVERAGE and instrument_info:
            leverage = int(instrument_info.get("maxLeverage", self.config.DEFAULT_LEVERAGE))
        else:
            leverage = self.config.DEFAULT_LEVERAGE
        
        # Calculate position size (minimum margin)
        quantity = self.executor.calculate_position_size(
            symbol=symbol,
            price=price,
            leverage=leverage,
            min_order_value_usd=self.config.MIN_ORDER_VALUE_USD
        )
        
        if not quantity:
            logger.error(f"Could not calculate position size for {symbol}")
            return False
        
        # Execute trade
        result = self.executor.open_position(
            symbol=symbol,
            side=side,
            quantity=quantity,
            leverage=leverage
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
            
            # Notify entry
            self.notifier.notify_entry(
                symbol=symbol,
                side=side,
                quantity=quantity,
                entry_price=result.entry_price or price,
                leverage=leverage,
                expected_funding_rate=funding_rate,
                position_id=result.position_id
            )
            
            logger.info(f"Entry successful: {symbol} {side} qty={quantity} leverage={leverage}x")
            return True
        else:
            logger.error(f"Entry failed: {result.error}")
            self.notifier.notify_error("Entry Failed", f"{symbol}: {result.error}")
            return False
    
    async def manage_exits(self) -> None:
        """
        Check exit conditions for all active positions and execute exits
        """
        positions = self.position_manager.get_active_positions()
        
        for position in positions:
            try:
                # Get current PnL
                current_pnl = self.executor.get_position_pnl(position.position_id) or 0.0
                
                # Get current market data
                tickers = self.fetcher.get_tickers([position.symbol])
                ticker_data = tickers.get(position.symbol, {})
                exit_price = ticker_data.get("lastPrice", position.entry_price)
                current_funding_rate = ticker_data.get("fundingRate")
                
                # Check exit conditions
                should_exit, reason = self.position_manager.should_exit(
                    position=position,
                    current_pnl=current_pnl,
                    current_funding_rate=current_funding_rate,
                    min_profit_percent=self.config.MIN_PROFIT_PERCENT,
                    stop_loss_percent=self.config.STOP_LOSS_PERCENT,
                    max_hold_minutes=self.config.MAX_HOLD_MINUTES_AFTER_SETTLEMENT
                )
                
                if should_exit:
                    logger.info(f"Exiting {position.symbol}: {reason}")
                    
                    # Execute exit
                    success = self.position_manager.execute_exit(
                        position_id=position.position_id,
                        reason=reason,
                        exit_price=exit_price
                    )
                    
                    if success:
                        # Calculate PnL
                        pnl = current_pnl + position.funding_amount
                        entry_value = float(position.quantity) * position.entry_price
                        pnl_percent = (pnl / entry_value * 100) if entry_value > 0 else 0
                        
                        # Record for daily summary
                        self._record_trade_for_daily(pnl, position.funding_amount)
                        
                        # Notify exit
                        self.notifier.notify_exit(
                            symbol=position.symbol,
                            side=position.side,
                            entry_price=position.entry_price,
                            exit_price=exit_price,
                            pnl=pnl,
                            pnl_percent=pnl_percent,
                            funding_received=position.funding_amount,
                            reason=reason,
                            hold_time=str(position.hold_duration).split('.')[0]
                        )
                        
            except Exception as e:
                logger.error(f"Error managing position {position.position_id}: {e}")
    
    def _notify_startup(self) -> None:
        """Send startup notification with config summary"""
        
        config_summary = f"""
<b>Mode:</b> LIVE
<b>Threshold:</b> {self.config.EXTREME_RATE_THRESHOLD * 100:.2f}%
<b>Entry Window:</b> {self.config.ENTRY_MIN_MINUTES_BEFORE}-{self.config.ENTRY_MAX_MINUTES_BEFORE} mins
<b>Max Positions:</b> {self.config.MAX_CONCURRENT_POSITIONS}
<b>Min Order:</b> ${self.config.MIN_ORDER_VALUE_USD}
<b>Max Leverage:</b> {'Auto (Max)' if self.config.USE_MAX_LEVERAGE else self.config.DEFAULT_LEVERAGE}
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
