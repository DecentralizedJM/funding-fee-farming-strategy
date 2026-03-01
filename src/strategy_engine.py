"""
Strategy Engine — Post-Settlement Momentum
============================================

Empirical analysis of Bybit klines around funding settlements shows that
price continues moving in the direction of the prevailing trend AFTER
settlement.  For example, assets with deeply negative funding rates (bearish)
drop an average of 0.7–0.9 % in the 5–30 minutes after settlement.

Old approach (enter before settlement, collect funding fee) → negative EV
because the adverse price move dwarfs the funding received.

New approach:
  1. Track extreme-rate symbols and their next settlement times.
  2. Enter AFTER settlement in the momentum direction:
     - Negative funding → SHORT (price keeps dropping)
     - Positive funding → LONG  (price keeps rising)
  3. Exit via trailing stop, take-profit, stop-loss, or time limit.

No funding is collected or paid (position opened after settlement).
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
    """Main strategy orchestration engine. Supports multiple API accounts."""

    def __init__(self, config: FarmingConfig, account_configs: List[tuple]):
        self.config = config
        self.running = False
        self.account_configs = account_configs
        self._n_accounts = len(account_configs)

        self.fetcher = FundingDataFetcher(config.FUNDING_API_BASE_URL)

        self.executors: List[TradeExecutor] = []
        self.position_managers: List[PositionManager] = []
        for i, (api_secret, _) in enumerate(account_configs):
            ex = TradeExecutor(api_secret=api_secret)
            self.executors.append(ex)
            state_file = config.STATE_FILE.replace(".json", f"_{i + 1}.json")
            trades_log = config.TRADES_LOG_FILE.replace(".json", f"_{i + 1}.json")
            self.position_managers.append(
                PositionManager(executor=ex, state_file=state_file, trades_log_file=trades_log)
            )

        self.executor = self.executors[0] if self.executors else None
        self.position_manager = self.position_managers[0] if self.position_managers else None

        chat_ids_by_account = [ac[1] for ac in account_configs]
        self.notifier = TelegramNotifier(
            bot_token=config.TELEGRAM_BOT_TOKEN,
            chat_ids_by_account=chat_ids_by_account,
        )

        self._paused = False
        self._last_reconciliation = None
        self._reconciliation_interval = timedelta(minutes=5)

        # Daily summary tracking per account
        self._last_summary_date = None
        self._daily_trades: List[int] = [0] * self._n_accounts
        self._daily_pnl: List[float] = [0.0] * self._n_accounts
        self._daily_funding: List[float] = [0.0] * self._n_accounts

        # Settlement watchlist: symbols with extreme funding rates approaching settlement.
        # Populated before settlement, consumed after settlement.
        # key=symbol, value={funding_rate, settlement_time_ms, side, volume24h, ...}
        self._watchlist: Dict[str, Dict] = {}

        # Cache for symbol/instrument/balance lookups
        self._symbol_available_cache: Dict[str, bool] = {}
        self._instrument_cache: Dict[str, Optional[Dict]] = {}
        self._cache_ttl = timedelta(minutes=5)
        self._symbol_cache_times: Dict[str, datetime] = {}
        self._cached_balance: List[Optional[float]] = [None] * self._n_accounts
        self._balance_cache_time: List[Optional[datetime]] = [None] * self._n_accounts

        logger.info("Strategy engine initialized with %d account(s)", self._n_accounts)

    # ── cache helpers ─────────────────────────────────────────────────────

    def _get_cached_symbol_available(self, symbol: str) -> bool:
        now = datetime.now(timezone.utc)
        cached_time = self._symbol_cache_times.get(symbol)
        if cached_time and (now - cached_time) < self._cache_ttl and symbol in self._symbol_available_cache:
            return self._symbol_available_cache[symbol]
        available = self.executor.check_symbol_available(symbol)
        self._symbol_available_cache[symbol] = available
        self._symbol_cache_times[symbol] = now
        return available

    def _get_cached_instrument_info(self, symbol: str) -> Optional[Dict]:
        now = datetime.now(timezone.utc)
        cached_time = self._symbol_cache_times.get(f"inst_{symbol}")
        if cached_time and (now - cached_time) < self._cache_ttl and symbol in self._instrument_cache:
            return self._instrument_cache[symbol]
        info = self.fetcher.get_instrument_info(symbol)
        self._instrument_cache[symbol] = info
        self._symbol_cache_times[f"inst_{symbol}"] = now
        return info

    def _get_cached_balance(self, account_index: int = 0) -> Optional[float]:
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
        self._cached_balance[account_index] = None
        self._balance_cache_time[account_index] = None

    # ── main loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.running = True
        self._last_summary_date = datetime.now(timezone.utc).date()

        logger.info("Starting post-settlement momentum strategy...")
        logger.info(f"Scan interval: {self.config.SCAN_INTERVAL_SECONDS}s")
        logger.info(
            f"Entry window: {self.config.POST_SETTLEMENT_DELAY_SECONDS}–"
            f"{self.config.POST_SETTLEMENT_WINDOW_SECONDS}s AFTER settlement"
        )
        logger.info(f"Threshold: {self.config.EXTREME_RATE_THRESHOLD * 100:.2f}%")

        while self.running:
            try:
                await self._check_daily_summary()
                await self._reconcile_positions()

                min_abs_seconds = await self.scan_and_enter()

                await self.manage_exits()

                # Adaptive sleep: fast-scan near settlement
                if min_abs_seconds is not None and min_abs_seconds <= self.config.FAST_SCAN_WHEN_SECONDS_LEFT:
                    sleep_seconds = self.config.FAST_SCAN_SECONDS
                else:
                    sleep_seconds = self.config.SCAN_INTERVAL_SECONDS
                await asyncio.sleep(sleep_seconds)

            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(60)

    # ── scanning & entry ──────────────────────────────────────────────────

    async def scan_and_enter(self) -> Optional[float]:
        """
        1. Fetch extreme funding opportunities.
        2. Update the settlement watchlist (symbols whose settlement is approaching).
        3. For watchlist symbols whose settlement just occurred, enter if within
           the post-settlement entry window.
        Returns the minimum |seconds to/since settlement| for adaptive sleep.
        """
        if self._paused:
            return None

        opportunities = self.fetcher.get_extreme_funding_opportunities(
            threshold=self.config.EXTREME_RATE_THRESHOLD
        )

        now = datetime.now(timezone.utc)
        min_abs_seconds: Optional[float] = None

        # ── Step 1: Update watchlist with extreme-rate symbols ────────────
        for opp in (opportunities or []):
            symbol = opp["symbol"]
            settlement_ms = opp["nextFundingTime"]
            secs_to = self.fetcher.get_time_to_next_settlement(settlement_ms).total_seconds()

            abs_secs = abs(secs_to)
            if min_abs_seconds is None or abs_secs < min_abs_seconds:
                min_abs_seconds = abs_secs

            volume_24h = opp.get("volume24h", 0) or 0
            if volume_24h < self.config.MIN_VOLUME_24H:
                continue

            # Determine momentum side (opposite of funding-collection side):
            # Positive funding = bullish trend → LONG
            # Negative funding = bearish trend → SHORT
            side = "LONG" if opp["fundingRate"] > 0 else "SHORT"

            # Add / update watchlist if settlement is approaching
            if secs_to > 0 and secs_to <= self.config.WATCHLIST_SECONDS_BEFORE_SETTLEMENT:
                self._watchlist[symbol] = {
                    "funding_rate": opp["fundingRate"],
                    "abs_rate": opp["absRate"],
                    "settlement_time_ms": settlement_ms,
                    "side": side,
                    "volume24h": volume_24h,
                    "last_price": opp["lastPrice"],
                    "mark_price": opp["markPrice"],
                }

        # Log watchlist + countdown
        if self._watchlist:
            items = []
            for sym, w in self._watchlist.items():
                delta = (w["settlement_time_ms"] / 1000) - now.timestamp()
                items.append(f"{sym}({w['funding_rate']*100:+.3f}% {delta:+.0f}s)")
            logger.info(f"Watchlist ({len(self._watchlist)}): {', '.join(items)}")
        elif opportunities:
            syms = ", ".join(o["symbol"] for o in opportunities[:5])
            logger.info(f"Found {len(opportunities)} opportunities ({syms}) — none within watchlist window yet")

        # ── Step 2: Check watchlist for post-settlement entry ─────────────
        settled_symbols = []
        for symbol, info in list(self._watchlist.items()):
            settlement_ts = info["settlement_time_ms"] / 1000
            seconds_since = now.timestamp() - settlement_ts

            if seconds_since < self.config.POST_SETTLEMENT_DELAY_SECONDS:
                continue  # too early — let order book settle

            if seconds_since > self.config.POST_SETTLEMENT_WINDOW_SECONDS:
                logger.info(f"Removing {symbol} from watchlist — entry window closed ({seconds_since:.0f}s since settlement)")
                settled_symbols.append(symbol)
                continue

            # In entry window → attempt entry for each account
            logger.info(
                f"POST-SETTLEMENT ENTRY WINDOW: {symbol} {info['side']} | "
                f"rate={info['funding_rate']*100:+.4f}% | {seconds_since:.0f}s since settlement"
            )
            entered_any = False
            for account_index in range(self._n_accounts):
                if self._daily_pnl[account_index] <= -self.config.MAX_DAILY_LOSS_USD:
                    continue
                active_count = self.position_managers[account_index].get_active_count()
                if active_count >= self.config.MAX_CONCURRENT_POSITIONS:
                    continue
                active_symbols = {
                    p.symbol for p in self.position_managers[account_index].get_active_positions()
                }
                if symbol in active_symbols:
                    continue

                success = await self._execute_entry(info, symbol, account_index)
                if success:
                    entered_any = True

            if entered_any:
                settled_symbols.append(symbol)

        for sym in settled_symbols:
            self._watchlist.pop(sym, None)

        # Track seconds since settlement for watchlist items (for adaptive sleep)
        for info in self._watchlist.values():
            delta = abs((info["settlement_time_ms"] / 1000) - now.timestamp())
            if min_abs_seconds is None or delta < min_abs_seconds:
                min_abs_seconds = delta

        return min_abs_seconds

    async def _execute_entry(self, watchlist_info: Dict, symbol: str, account_index: int) -> bool:
        """Execute a post-settlement momentum entry."""
        side = watchlist_info["side"]
        funding_rate = watchlist_info["funding_rate"]
        settlement_ms = watchlist_info["settlement_time_ms"]
        executor = self.executors[account_index]
        position_manager = self.position_managers[account_index]

        # Fetch fresh price from Bybit
        tickers = self.fetcher.get_tickers([symbol])
        ticker = tickers.get(symbol)
        if not ticker:
            logger.warning(f"No fresh ticker for {symbol} — skipping entry")
            return False
        price = ticker["lastPrice"]
        mark_price = ticker.get("markPrice", price)

        logger.info(f"Attempting entry: {symbol} {side} @ {price} (rate was {funding_rate*100:+.4f}%)")

        if not self._get_cached_symbol_available(symbol):
            logger.warning(f"Symbol {symbol} not available on Mudrex — skipping")
            return False

        # Price spread check
        if price > 0:
            spread = abs(mark_price - price) / price
            if spread > self.config.PRICE_SPREAD_THRESHOLD:
                logger.warning(f"Spread too high ({spread*100:.2f}%) — skipping {symbol}")
                return False

        if self.config.MARGIN_PERCENTAGE is None or self.config.MARGIN_PERCENTAGE <= 0:
            logger.warning("MARGIN_PERCENTAGE not set — skipping entry")
            return False

        balance = self._get_cached_balance(account_index)
        if balance is None or balance <= 0:
            logger.warning(f"No balance for account {account_index + 1} — skipping")
            return False

        margin_usd = balance * (self.config.MARGIN_PERCENTAGE / 100.0)
        min_order = self.config.MIN_ORDER_VALUE_USD
        min_lev, max_lev = self.config.MIN_LEVERAGE, self.config.MAX_LEVERAGE

        if margin_usd * max_lev < min_order:
            logger.warning(f"Insufficient margin for {symbol}: ${margin_usd:.2f} * {max_lev}x < ${min_order}")
            return False

        leverage_needed = math.ceil(min_order / margin_usd) if margin_usd > 0 else max_lev
        leverage = max(min_lev, min(max_lev, leverage_needed))

        instrument_info = self._get_cached_instrument_info(symbol)
        if instrument_info:
            max_asset_lev = int(instrument_info.get("maxLeverage", 100))
            leverage = min(leverage, max_asset_lev)
            if leverage < min_lev:
                logger.warning(f"Asset {symbol} max leverage {max_asset_lev} < min {min_lev}x — skipping")
                return False

        quantity = executor.calculate_position_size(
            symbol=symbol, price=price, leverage=leverage,
            margin_usd=margin_usd, min_order_value_usd=min_order,
        )
        if not quantity:
            logger.error(f"Could not calculate position size for {symbol}")
            return False

        notional = margin_usd * leverage
        if notional < min_order:
            logger.warning(f"Notional ${notional:.2f} < min ${min_order} for {symbol}")
            return False

        # SL from LTP (Mudrex SL triggers on LTP)
        sl_pct = self.config.STOP_LOSS_PERCENT
        if side == "LONG":
            sl_price = f"{price * (1 - sl_pct):.4f}"
        else:
            sl_price = f"{price * (1 + sl_pct):.4f}"

        result = executor.open_position(
            symbol=symbol, side=side, quantity=quantity,
            leverage=leverage, stop_loss_price=sl_price,
        )

        if result.success:
            self._invalidate_balance_cache(account_index)
            settlement_time = datetime.fromtimestamp(settlement_ms / 1000, tz=timezone.utc)
            actual_entry = result.entry_price or price
            slippage = abs(actual_entry - price) / price if price > 0 else 0

            position = FarmingPosition(
                position_id=result.position_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                entry_price=actual_entry,
                leverage=leverage,
                expected_funding_rate=funding_rate,
                funding_settlement_time=settlement_time,
                entry_time=datetime.now(timezone.utc),
            )
            position_manager.add_position(position)

            if slippage > self.config.MAX_SLIPPAGE_PERCENT:
                logger.error(
                    f"Excessive slippage on {symbol}: {slippage*100:.3f}% — closing immediately"
                )
                position_manager.execute_exit(
                    position_id=result.position_id,
                    reason=f"Excessive slippage: {slippage*100:.3f}%",
                    exit_price=actual_entry,
                )
                return False

            self.notifier.notify_entry(
                symbol=symbol, side=side, quantity=quantity,
                entry_price=actual_entry, leverage=leverage,
                expected_funding_rate=funding_rate,
                position_id=result.position_id,
                account_index=account_index,
            )
            logger.info(
                f"Entry OK (acct {account_index + 1}): {symbol} {side} "
                f"qty={quantity} lev={leverage}x slip={slippage*100:.3f}%"
            )
            return True

        logger.error(f"Entry failed (acct {account_index + 1}): {result.error}")
        return False

    # ── exit management ───────────────────────────────────────────────────

    async def manage_exits(self) -> None:
        """Check exit conditions for all active positions."""
        for account_index in range(self._n_accounts):
            position_manager = self.position_managers[account_index]
            positions = position_manager.get_active_positions()
            for position in positions:
                try:
                    tickers = self.fetcher.get_tickers([position.symbol])
                    ticker_data = tickers.get(position.symbol, {})
                    ltp = ticker_data.get("lastPrice", position.entry_price)
                    qty = float(position.quantity)
                    direction = 1.0 if position.side == "LONG" else -1.0
                    current_pnl = (ltp - position.entry_price) * qty * direction
                    entry_value = qty * position.entry_price

                    should_exit, reason = position_manager.should_exit(
                        position=position,
                        current_pnl=current_pnl,
                        take_profit_percent=self.config.TAKE_PROFIT_PERCENT,
                        stop_loss_percent=self.config.STOP_LOSS_PERCENT,
                        trailing_stop_enabled=self.config.TRAILING_STOP_ENABLED,
                        trailing_activation_percent=self.config.TRAILING_ACTIVATION_PERCENT,
                        trailing_callback_percent=self.config.TRAILING_CALLBACK_PERCENT,
                        max_hold_minutes=self.config.MAX_HOLD_MINUTES,
                    )

                    if should_exit:
                        logger.info(f"Exiting {position.symbol}: {reason}")
                        success, realized_pnl, _ = position_manager.execute_exit(
                            position_id=position.position_id,
                            reason=reason,
                            exit_price=ltp,
                        )
                        if success:
                            pnl_pct = (realized_pnl / entry_value * 100) if entry_value > 0 else 0
                            self._record_trade_for_daily(account_index, realized_pnl, 0.0)
                            self.notifier.notify_exit(
                                symbol=position.symbol,
                                side=position.side,
                                entry_price=position.entry_price,
                                exit_price=ltp,
                                pnl=realized_pnl,
                                pnl_percent=pnl_pct,
                                funding_received=0.0,
                                reason=reason,
                                hold_time=str(position.hold_duration).split(".")[0],
                                account_index=account_index,
                            )
                except Exception as e:
                    logger.error(f"Error managing position {position.position_id}: {e}")

    # ── housekeeping ──────────────────────────────────────────────────────

    async def _check_daily_summary(self) -> None:
        today = datetime.now(timezone.utc).date()
        if self._last_summary_date and today > self._last_summary_date:
            for ai in range(self._n_accounts):
                logger.info(
                    f"Daily summary (acct {ai + 1}): "
                    f"{self._daily_trades[ai]} trades, ${self._daily_pnl[ai]:.4f} PnL"
                )
            self._daily_trades = [0] * self._n_accounts
            self._daily_pnl = [0.0] * self._n_accounts
            self._daily_funding = [0.0] * self._n_accounts
            self._last_summary_date = today

    def _record_trade_for_daily(self, account_index: int, pnl: float, funding: float) -> None:
        self._daily_trades[account_index] += 1
        self._daily_pnl[account_index] += pnl
        self._daily_funding[account_index] += funding

    async def _reconcile_positions(self) -> None:
        now = datetime.now(timezone.utc)
        if self._last_reconciliation and now - self._last_reconciliation < self._reconciliation_interval:
            return
        self._last_reconciliation = now

        for ai in range(self._n_accounts):
            local_positions = self.position_managers[ai].get_active_positions()
            if not local_positions:
                continue
            try:
                exchange_positions = self.executors[ai].get_open_positions()
                exchange_ids = {p["position_id"] for p in exchange_positions}
                for pos in local_positions:
                    if pos.position_id not in exchange_ids:
                        logger.warning(
                            f"Reconciliation (acct {ai + 1}): {pos.symbol} not on exchange — cleaning up"
                        )
                        tickers = self.fetcher.get_tickers([pos.symbol])
                        td = tickers.get(pos.symbol, {})
                        recon_price = td.get("lastPrice", pos.entry_price)
                        self.position_managers[ai].execute_exit(
                            position_id=pos.position_id,
                            reason="Reconciliation: closed/liquidated on exchange",
                            exit_price=recon_price,
                        )
            except Exception as e:
                logger.error(f"Reconciliation error (acct {ai + 1}): {e}")

    def pause(self) -> None:
        self._paused = True
        logger.info("Strategy PAUSED")

    def resume(self) -> None:
        self._paused = False
        logger.info("Strategy RESUMED")

    def stop(self) -> None:
        self.running = False
        logger.info("Strategy stopped")

    # ── status for /status command ────────────────────────────────────────

    def get_status(self) -> dict:
        total_active = sum(pm.get_active_count() for pm in self.position_managers)
        result = {
            "running": self.running,
            "paused": self._paused,
            "active_positions": total_active,
            "max_positions": self.config.MAX_CONCURRENT_POSITIONS * self._n_accounts,
            "accounts": self._n_accounts,
            "performance": (
                self.position_managers[0].get_performance_stats() if self.position_managers else {}
            ),
            "watchlist_count": len(self._watchlist),
            "watchlist_symbols": ", ".join(self._watchlist.keys()) if self._watchlist else None,
        }
        try:
            opportunities = self.fetcher.get_extreme_funding_opportunities(
                threshold=self.config.EXTREME_RATE_THRESHOLD
            )
            if opportunities:
                min_secs = min(
                    self.fetcher.get_time_to_next_settlement(o["nextFundingTime"]).total_seconds()
                    for o in opportunities
                )
                syms = ", ".join(o["symbol"] for o in opportunities[:5])
                if len(opportunities) > 5:
                    syms += f" (+{len(opportunities) - 5})"
                result["opportunity_count"] = len(opportunities)
                result["opportunity_symbols"] = syms
                result["seconds_to_settlement"] = min_secs
            else:
                result["opportunity_count"] = 0
                result["opportunity_symbols"] = None
                result["seconds_to_settlement"] = None
        except Exception:
            result["opportunity_count"] = 0
            result["opportunity_symbols"] = None
            result["seconds_to_settlement"] = None
        return result
