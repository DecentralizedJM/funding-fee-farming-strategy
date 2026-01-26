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

from .trade_executor import TradeExecutor

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
        min_profit_percent: float = 0.05,
        max_hold_minutes: int = 30
    ) -> Tuple[bool, str]:
        """
        Determine if position should exit
        
        Args:
            position: The position to check
            current_pnl: Current unrealized PnL
            min_profit_percent: Minimum profit to exit
            max_hold_minutes: Maximum hold time after settlement
        
        Returns:
            Tuple of (should_exit, reason)
        """
        now = datetime.now(timezone.utc)
        
        # Check if settlement has occurred
        if now < position.funding_settlement_time:
            return False, "Waiting for settlement"
        
        # Mark funding as received if just passed settlement
        if not position.funding_received:
            time_since = now - position.funding_settlement_time
            if time_since >= timedelta(seconds=30):
                # Funding should be credited by now
                self.mark_funding_received(position.position_id)
        
        # Exit conditions after funding received
        if position.funding_received:
            # Calculate profit including estimated funding
            entry_value = float(position.quantity) * position.entry_price
            estimated_funding = entry_value * abs(position.expected_funding_rate)
            total_pnl = current_pnl + estimated_funding
            profit_percent = (total_pnl / entry_value) * 100 if entry_value > 0 else 0
            
            # Condition 1: Profit target reached
            if profit_percent >= min_profit_percent:
                return True, f"Profit target reached: {profit_percent:.2f}%"
            
            # Condition 2: Max hold time exceeded
            time_since_settlement = position.time_since_settlement
            if time_since_settlement and time_since_settlement >= timedelta(minutes=max_hold_minutes):
                return True, f"Max hold time exceeded: {time_since_settlement}"
            
            # Condition 3: Stop loss - significant adverse move
            if current_pnl < -estimated_funding * 2:
                return True, f"Stop loss triggered: PnL ${current_pnl:.4f}"
        
        return False, "Holding"
    
    def execute_exit(
        self,
        position_id: str,
        reason: str,
        exit_price: Optional[float] = None
    ) -> bool:
        """
        Close position and record results
        
        Args:
            position_id: Position to close
            reason: Reason for exit
            exit_price: Exit price (optional, will fetch from API if not provided)
        
        Returns:
            True if closed successfully
        """
        position = self.positions.get(position_id)
        if not position:
            logger.warning(f"Position {position_id} not found")
            return False
        
        # Get current PnL before closing
        current_pnl = self.executor.get_position_pnl(position_id) or 0.0
        
        # Close the position
        success = self.executor.close_position(position_id)
        
        if success:
            # Update position record
            position.exit_time = datetime.now(timezone.utc)
            position.exit_price = exit_price or position.entry_price
            position.exit_reason = reason
            position.realized_pnl = current_pnl + position.funding_amount
            
            # Log completed trade
            trade_record = position.to_dict()
            self.completed_trades.append(trade_record)
            
            # Remove from active positions
            del self.positions[position_id]
            
            self.save_state()
            self._log_trade(trade_record)
            
            logger.info(f"Position {position_id} closed: {reason}, PnL: ${position.realized_pnl:.4f}")
            return True
        else:
            logger.error(f"Failed to close position {position_id}")
            return False
    
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
