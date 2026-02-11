"""
Position Manager
================

Tracks farming positions and handles exit logic.
"""

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from trade_executor import TradeExecutor

logger = logging.getLogger(__name__)


@dataclass
class FarmingPosition:
    """Represents a position opened for funding farming"""
    position_id: str
    symbol: str
    side: str
    quantity: str
    entry_price: float
    leverage: int
    expected_funding_rate: float
    funding_settlement_time: datetime
    entry_time: datetime
    
    # Updated during lifecycle
    funding_received: bool = False
    funding_amount: float = 0.0
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    realized_pnl: Optional[float] = None
    
    # Smart Exit State
    highest_pnl_percent: float = -1.0  # Highest recorded PnL % (start low)
    
    # Settlement Reversal Strategy
    # Phase: "pre_settlement" (initial position) or "reversed" (opposite position after settlement)
    phase: str = "pre_settlement"
    # For reversed positions: ID of the original pre_settlement position
    parent_position_id: Optional[str] = None
    # For reversed positions: PnL from the first leg (pre_settlement)
    first_leg_pnl: float = 0.0
    # For reversed positions: Funding received from first leg
    first_leg_funding: float = 0.0
    
    def to_dict(self) -> dict:
        """Convert to serializable dict"""
        data = asdict(self)
        # Convert datetime to ISO format
        data["funding_settlement_time"] = self.funding_settlement_time.isoformat()
        data["entry_time"] = self.entry_time.isoformat()
        if self.exit_time:
            data["exit_time"] = self.exit_time.isoformat()
        return data
    
    @classmethod
    def from_dict(cls, data: dict) -> "FarmingPosition":
        """Create from dict (e.g., loaded from JSON)"""
        # Parse datetime strings
        data["funding_settlement_time"] = datetime.fromisoformat(data["funding_settlement_time"])
        data["entry_time"] = datetime.fromisoformat(data["entry_time"])
        if data.get("exit_time"):
            data["exit_time"] = datetime.fromisoformat(data["exit_time"])
        
        # Backward compatibility: set defaults for new fields if missing
        if "phase" not in data:
            data["phase"] = "pre_settlement"
        if "parent_position_id" not in data:
            data["parent_position_id"] = None
        if "first_leg_pnl" not in data:
            data["first_leg_pnl"] = 0.0
        if "first_leg_funding" not in data:
            data["first_leg_funding"] = 0.0
        
        return cls(**data)
    
    @property
    def is_active(self) -> bool:
        """Check if position is still active (not exited)"""
        return self.exit_time is None
    
    @property
    def hold_duration(self) -> timedelta:
        """Get how long position has been held"""
        end_time = self.exit_time or datetime.now(timezone.utc)
        return end_time - self.entry_time
    
    @property
    def time_since_settlement(self) -> Optional[timedelta]:
        """Get time since funding settlement"""
        now = datetime.now(timezone.utc)
        if now > self.funding_settlement_time:
            return now - self.funding_settlement_time
        return None


class PositionManager:
    """Manages farming positions and exit logic"""
    
    def __init__(
        self,
        executor: TradeExecutor,
        state_file: str = "data/state.json",
        trades_log_file: str = "data/trades.json"
    ):
        self.executor = executor
        self.state_file = state_file
        self.trades_log_file = trades_log_file
        self.positions: Dict[str, FarmingPosition] = {}
        self.completed_trades: List[dict] = []
        
        # Ensure data directory exists
        Path(state_file).parent.mkdir(parents=True, exist_ok=True)
        Path(trades_log_file).parent.mkdir(parents=True, exist_ok=True)
        
        # Load existing state
        self.load_state()
    
    def add_position(self, position: FarmingPosition) -> None:
        """
        Track a new farming position
        
        Args:
            position: FarmingPosition to track
        """
        self.positions[position.position_id] = position
        self.save_state()
        logger.info(f"Added position {position.position_id} for {position.symbol}")
    
    def get_position(self, position_id: str) -> Optional[FarmingPosition]:
        """Get a position by ID"""
        return self.positions.get(position_id)
    
    def mark_funding_received(
        self,
        position_id: str,
        funding_amount: float = 0.0
    ) -> None:
        """
        Mark that funding has been credited
        
        Args:
            position_id: Position ID
            funding_amount: Amount of funding received
        """
        if position_id in self.positions:
            self.positions[position_id].funding_received = True
            self.positions[position_id].funding_amount = funding_amount
            self.save_state()
            logger.info(f"Position {position_id} funding received: ${funding_amount:.4f}")
    
    def should_exit(
        self,
        position: FarmingPosition,
        current_pnl: float,
        current_funding_rate: Optional[float] = None,
        min_profit_percent: float = 0.05,
        stop_loss_percent: float = 0.005,
        soft_loss_percent: float = -0.002,
        trailing_stop_enabled: bool = True,
        trailing_activation_percent: float = 0.001,
        trailing_callback_percent: float = 0.0002,
        max_hold_minutes: int = 30,
        # Settlement reversal parameters
        settlement_reversal_enabled: bool = False,
        reversal_profit_target_percent: float = 0.0005,
        reversal_max_hold_minutes: int = 3
    ) -> Tuple[bool, str]:
        """
        Determine if position should exit.
        
        For pre_settlement phase (when reversal enabled):
        - Only exit on stop loss or funding rate reversal
        - Settlement reversal is triggered by strategy_engine, not here
        
        For pre_settlement phase (when reversal disabled - legacy behavior):
        - Exit on profit, small loss, stop loss, or max hold after settlement
        
        For reversed phase:
        - Exit on profit target, max hold time, or stop loss
        - No funding-based logic
        
        Args:
            position: The position to check
            current_pnl: Current unrealized PnL
            current_funding_rate: Current funding rate (for reversal check)
            min_profit_percent: Minimum profit to exit (not used in current logic)
            stop_loss_percent: Stop loss percentage (hard stop - prevents liquidation)
            soft_loss_percent: Soft loss threshold (exit if total PnL > this after funding)
            max_hold_minutes: Maximum hold time after settlement (safety cap)
            settlement_reversal_enabled: Whether settlement reversal mode is active
            reversal_profit_target_percent: Profit target for reversed position
            reversal_max_hold_minutes: Max hold time for reversed position
        
        Returns:
            Tuple of (should_exit, reason)
        """
        now = datetime.now(timezone.utc)
        
        # Calculate common values
        entry_value = float(position.quantity) * position.entry_price
        margin = entry_value / position.leverage if position.leverage > 0 else entry_value
        
        # ========================================================================
        # REVERSED PHASE: Different exit logic for the second leg
        # ========================================================================
        if position.phase == "reversed":
            return self._should_exit_reversed(
                position=position,
                current_pnl=current_pnl,
                entry_value=entry_value,
                margin=margin,
                stop_loss_percent=stop_loss_percent,
                reversal_profit_target_percent=reversal_profit_target_percent,
                reversal_max_hold_minutes=reversal_max_hold_minutes
            )
        
        # ========================================================================
        # PRE_SETTLEMENT PHASE: Original position
        # ========================================================================
        
        # Check Stop Loss (Always active) - Bug #3 fix: use margin, not notional
        if margin > 0:
            # Calculate loss as percentage of margin at risk (not notional value)
            # With 10x leverage, $0.10 loss on $2 margin = 5% margin loss
            pnl_percent_of_margin = current_pnl / margin
            if pnl_percent_of_margin <= -stop_loss_percent:
                return True, f"Stop loss triggered: {pnl_percent_of_margin*100:.2f}% of margin <= -{stop_loss_percent*100:.2f}%"
        
        # Check Funding Rate Reversal (Always active)
        # Bug #5 fix: Use relative threshold instead of absolute 0.01%
        # Only exit if rate truly reversed (crossed zero) by significant amount
        if current_funding_rate is not None:
            original_rate = position.expected_funding_rate
            # Minimum threshold: rate must exceed 0.1% OR 50% of original rate magnitude
            min_reversal = max(0.001, abs(original_rate) * 0.5)
            
            # Long position: was receiving (rate < 0), now paying if rate > threshold
            if position.side == "LONG" and current_funding_rate > min_reversal:
                return True, f"Funding rate reversal: {current_funding_rate*100:.4f}% (was {original_rate*100:.4f}%, Longs now pay)"
            # Short position: was receiving (rate > 0), now paying if rate < -threshold
            if position.side == "SHORT" and current_funding_rate < -min_reversal:
                return True, f"Funding rate reversal: {current_funding_rate*100:.4f}% (was {original_rate*100:.4f}%, Shorts now pay)"
        
        # Check if settlement has occurred
        if now < position.funding_settlement_time:
            return False, "Waiting for settlement"
        
        # Calculate time since settlement (used in multiple places)
        time_since_settlement = position.time_since_settlement or timedelta(seconds=0)
        minutes_held = time_since_settlement.total_seconds() / 60
        
        # ========================================================================
        # SETTLEMENT REVERSAL MODE: Don't exit on profit/loss, let engine trigger reversal
        # ========================================================================
        if settlement_reversal_enabled:
            # When reversal mode is enabled, we only exit pre_settlement positions on:
            # 1. Stop loss (checked above)
            # 2. Funding rate reversal (checked above)
            # The actual settlement reversal (close + open opposite) is triggered 
            # by strategy_engine when funding_received is True, NOT here.
            # 
            # Safety: Still apply hard time limit as a safety net
            if minutes_held >= max_hold_minutes:
                return True, f"Max hold time exceeded (reversal mode): {minutes_held:.1f}m"
            
            return False, "Holding for settlement reversal"
        
        # ========================================================================
        # LEGACY MODE (reversal disabled): Original exit logic
        # ========================================================================
        
        # Note: Funding verification is now handled by strategy_engine (with API verification)
        # This function just checks the funding_received flag that strategy_engine sets

        # Safety: Hard time limit (always check, regardless of funding status)
        # This was Bug #1 - previously inside funding_received block making it impossible
        if minutes_held >= max_hold_minutes:
            return True, f"Max hold time exceeded: {minutes_held:.1f}m"

        # Exit strategy after funding received: prioritize profit, then small loss
        if position.funding_received:
            # Use actual funding_amount (not estimated) - fixes Bug #2 double-counting
            funding_amount = position.funding_amount
            total_pnl = current_pnl + funding_amount
            profit_percent = (total_pnl / entry_value) if entry_value > 0 else 0

            # IDEAL: Exit if in profit (any profit is good)
            if profit_percent > 0:
                return True, f"Profit Exit: {profit_percent*100:.3f}% > 0%"

            # SECOND IDEAL: Exit if loss is small (soft loss threshold)
            # This prevents holding through larger losses while still giving chance for recovery
            if profit_percent > soft_loss_percent:
                return True, f"Small Loss Exit: {profit_percent*100:.3f}% > {soft_loss_percent*100:.3f}%"

        return False, "Holding"
    
    def _should_exit_reversed(
        self,
        position: FarmingPosition,
        current_pnl: float,
        entry_value: float,
        margin: float,
        stop_loss_percent: float,
        reversal_profit_target_percent: float,
        reversal_max_hold_minutes: int
    ) -> Tuple[bool, str]:
        """
        Determine if a reversed position should exit.
        
        Exit conditions for reversed position:
        1. Profit target reached
        2. Max hold time exceeded
        3. Stop loss triggered
        
        Args:
            position: The reversed position to check
            current_pnl: Current unrealized PnL
            entry_value: Position notional value
            margin: Position margin
            stop_loss_percent: Stop loss percentage of margin
            reversal_profit_target_percent: Profit target percentage
            reversal_max_hold_minutes: Max hold time in minutes
        
        Returns:
            Tuple of (should_exit, reason)
        """
        # Check Stop Loss (Always active)
        if margin > 0:
            pnl_percent_of_margin = current_pnl / margin
            if pnl_percent_of_margin <= -stop_loss_percent:
                return True, f"Reversed stop loss: {pnl_percent_of_margin*100:.2f}% of margin <= -{stop_loss_percent*100:.2f}%"
        
        # Check max hold time for reversed position (based on entry_time, not settlement)
        hold_duration = position.hold_duration
        minutes_held = hold_duration.total_seconds() / 60
        
        if minutes_held >= reversal_max_hold_minutes:
            return True, f"Reversed max hold time: {minutes_held:.1f}m >= {reversal_max_hold_minutes}m"
        
        # Check profit target: % of MARGIN (with leverage this gives higher ROI)
        if margin > 0:
            profit_margin_percent = current_pnl / margin
            if profit_margin_percent >= reversal_profit_target_percent:
                return True, f"Reversed profit target: {profit_margin_percent*100:.4f}% of margin >= {reversal_profit_target_percent*100:.4f}%"
        
        return False, "Reversed: Holding"
    
    def execute_exit(
        self,
        position_id: str,
        reason: str,
        exit_price: Optional[float] = None,
        skip_trade_log: bool = False
    ) -> Tuple[bool, float, float]:
        """
        Close position and record results
        
        Args:
            position_id: Position to close
            reason: Reason for exit
            exit_price: Exit price (optional, will fetch from API if not provided)
            skip_trade_log: If True, don't log trade (used for settlement reversal first leg)
        
        Returns:
            Tuple of (success, realized_pnl, funding_amount)
            - For pre_settlement with reversal: PnL and funding to store on reversed position
            - For reversed: Combined PnL (first_leg_pnl + first_leg_funding + current_pnl)
        """
        position = self.positions.get(position_id)
        if not position:
            logger.warning(f"Position {position_id} not found in local state")
            return False, 0.0, 0.0
        
        # Get current PnL before closing (snapshot)
        current_pnl = self.executor.get_position_pnl(position_id) or 0.0
        
        # Close the position via API
        success = self.executor.close_position(position_id)
        
        # --- ERROR HANDLING: Check for "Position Not Open" / 404 ---
        if not success:
            logger.warning(f"Close failed for {position_id}. Verifying if position still exists...")
            # Double check if position is actually open on exchange
            open_positions = self.executor.get_open_positions()
            is_open_on_exchange = any(p["position_id"] == position_id for p in open_positions)
            
            if not is_open_on_exchange:
                logger.warning(f"Position {position_id} not found on exchange. Assuming closed externally/liquidated.")
                # Force success to clear local state
                success = True
                reason = f"{reason} (Force Close: Not found on exchange)"
        
        if success:
            # Update position record
            position.exit_time = datetime.now(timezone.utc)
            position.exit_price = exit_price or position.entry_price
            position.exit_reason = reason
            
            # Calculate realized PnL based on position phase
            if position.phase == "reversed":
                # For reversed positions: combine first leg + current leg PnL
                first_leg_total = position.first_leg_pnl + position.first_leg_funding
                position.realized_pnl = first_leg_total + current_pnl
                logger.info(f"Reversed position {position_id} combined PnL: first_leg=${first_leg_total:.4f} + current=${current_pnl:.4f} = ${position.realized_pnl:.4f}")
            else:
                # For pre_settlement positions: current PnL + funding
                position.realized_pnl = current_pnl + position.funding_amount
            
            # Store the values to return
            realized_pnl = position.realized_pnl
            funding_amount = position.funding_amount
            
            # Log completed trade (unless skipped for settlement reversal)
            if not skip_trade_log:
                trade_record = position.to_dict()
                self.completed_trades.append(trade_record)
                self._log_trade(trade_record)
            
            # Remove from active positions
            del self.positions[position_id]
            
            self.save_state()
            
            logger.info(f"Position {position_id} closed: {reason}, PnL: ${realized_pnl:.4f}")
            return True, realized_pnl, funding_amount
        else:
            logger.error(f"Failed to close position {position_id}")
            return False, 0.0, 0.0
    
    def get_active_positions(self) -> List[FarmingPosition]:
        """Get all active farming positions"""
        return [p for p in self.positions.values() if p.is_active]
    
    def get_active_count(self) -> int:
        """Get count of active positions"""
        return len(self.get_active_positions())
    
    def save_state(self) -> None:
        """Persist current state to disk"""
        try:
            state = {
                "positions": {
                    pid: p.to_dict()
                    for pid, p in self.positions.items()
                },
                "last_updated": datetime.now(timezone.utc).isoformat()
            }
            
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2)
            
            logger.debug("State saved successfully")
        except Exception as e:
            logger.error(f"Error saving state: {e}")
    
    def load_state(self) -> None:
        """Load state from disk"""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    state = json.load(f)
                
                for pid, pdata in state.get("positions", {}).items():
                    self.positions[pid] = FarmingPosition.from_dict(pdata)
                
                logger.info(f"Loaded {len(self.positions)} positions from state")
        except Exception as e:
            logger.error(f"Error loading state: {e}")
            self.positions = {}
    
    def _log_trade(self, trade: dict) -> None:
        """Append trade to trades log file"""
        try:
            trades = []
            if os.path.exists(self.trades_log_file):
                with open(self.trades_log_file, "r") as f:
                    trades = json.load(f)
            
            trades.append(trade)
            
            with open(self.trades_log_file, "w") as f:
                json.dump(trades, f, indent=2)
        except Exception as e:
            logger.error(f"Error logging trade: {e}")
    
    def get_performance_stats(self) -> dict:
        """Get performance statistics"""
        try:
            trades = []
            if os.path.exists(self.trades_log_file):
                with open(self.trades_log_file, "r") as f:
                    trades = json.load(f)
            
            if not trades:
                return {
                    "total_trades": 0,
                    "winning_trades": 0,
                    "losing_trades": 0,
                    "win_rate": 0.0,
                    "total_pnl": 0.0,
                    "total_funding": 0.0,
                    "avg_pnl": 0.0
                }
            
            winning = [t for t in trades if (t.get("realized_pnl") or 0) > 0]
            losing = [t for t in trades if (t.get("realized_pnl") or 0) <= 0]
            total_pnl = sum(t.get("realized_pnl", 0) or 0 for t in trades)
            total_funding = sum(t.get("funding_amount", 0) or 0 for t in trades)
            
            return {
                "total_trades": len(trades),
                "winning_trades": len(winning),
                "losing_trades": len(losing),
                "win_rate": (len(winning) / len(trades) * 100) if trades else 0.0,
                "total_pnl": total_pnl,
                "total_funding": total_funding,
                "avg_pnl": total_pnl / len(trades) if trades else 0.0
            }
        except Exception as e:
            logger.error(f"Error getting performance stats: {e}")
            return {}
