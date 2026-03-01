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
        take_profit_percent: float = 0.008,
        stop_loss_percent: float = 0.005,
        trailing_stop_enabled: bool = True,
        trailing_activation_percent: float = 0.003,
        trailing_callback_percent: float = 0.002,
        max_hold_minutes: int = 15,
    ) -> Tuple[bool, str]:
        """
        Momentum exit logic.  All thresholds are percentage of NOTIONAL value.

        Exit conditions (checked in order):
        1. Stop loss
        2. Take profit
        3. Trailing stop (activated once pnl > activation threshold)
        4. Max hold time

        Returns:
            Tuple of (should_exit, reason)
        """
        entry_value = float(position.quantity) * position.entry_price
        if entry_value <= 0:
            return True, "Invalid entry value"

        pnl_pct = current_pnl / entry_value

        # 1. Stop loss
        if pnl_pct <= -stop_loss_percent:
            return True, f"Stop loss: {pnl_pct*100:.3f}% <= -{stop_loss_percent*100:.3f}%"

        # 2. Take profit
        if pnl_pct >= take_profit_percent:
            return True, f"Take profit: {pnl_pct*100:.3f}% >= {take_profit_percent*100:.3f}%"

        # 3. Trailing stop
        if trailing_stop_enabled:
            if pnl_pct > position.highest_pnl_percent:
                position.highest_pnl_percent = pnl_pct
                self.save_state()

            if position.highest_pnl_percent >= trailing_activation_percent:
                drawdown = position.highest_pnl_percent - pnl_pct
                if drawdown >= trailing_callback_percent:
                    return True, (
                        f"Trailing stop: peak {position.highest_pnl_percent*100:.3f}%, "
                        f"now {pnl_pct*100:.3f}%, drawdown {drawdown*100:.3f}%"
                    )

        # 4. Max hold time
        minutes_held = position.hold_duration.total_seconds() / 60
        if minutes_held >= max_hold_minutes:
            return True, f"Max hold time: {minutes_held:.1f}m >= {max_hold_minutes}m"

        return False, "Holding"
    
    def execute_exit(
        self,
        position_id: str,
        reason: str,
        exit_price: Optional[float] = None,
        skip_trade_log: bool = False
    ) -> Tuple[bool, float, float]:
        """
        Close position and record results.

        Returns:
            Tuple of (success, realized_pnl, funding_amount)
        """
        position = self.positions.get(position_id)
        if not position:
            logger.warning(f"Position {position_id} not found in local state")
            return False, 0.0, 0.0
        
        # Calculate PnL from exit_price (bot-side, using Bybit LTP - not Mudrex which is stale)
        actual_exit = exit_price or position.entry_price
        qty = float(position.quantity)
        direction = 1.0 if position.side == "LONG" else -1.0
        current_pnl = (actual_exit - position.entry_price) * qty * direction
        
        # Close the position via API
        success = self.executor.close_position(position_id)
        
        # --- ERROR HANDLING: Check for "Position Not Open" / 404 ---
        if not success:
            logger.warning(f"Close failed for {position_id}. Verifying if position still exists...")
            open_positions = self.executor.get_open_positions()
            is_open_on_exchange = any(p["position_id"] == position_id for p in open_positions)
            
            if not is_open_on_exchange:
                logger.warning(f"Position {position_id} not found on exchange. Assuming closed externally/liquidated.")
                success = True
                reason = f"{reason} (Force Close: Not found on exchange)"
        
        if success:
            position.exit_time = datetime.now(timezone.utc)
            position.exit_price = actual_exit
            position.exit_reason = reason
            
            # Calculate realized PnL based on position phase
            if position.phase == "reversed":
                first_leg_total = position.first_leg_pnl + position.first_leg_funding
                position.realized_pnl = first_leg_total + current_pnl
                logger.info(f"Reversed position {position_id} combined PnL: first_leg=${first_leg_total:.4f} + current=${current_pnl:.4f} = ${position.realized_pnl:.4f}")
            else:
                position.realized_pnl = current_pnl + position.funding_amount
            
            realized_pnl = position.realized_pnl
            funding_amount = position.funding_amount
            
            if not skip_trade_log:
                trade_record = position.to_dict()
                self.completed_trades.append(trade_record)
                self._log_trade(trade_record)
            
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
