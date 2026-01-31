"""
Unit Tests for Position Manager
===============================

Tests for the should_exit() logic with various edge cases.
"""

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, MagicMock
import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from position_manager import PositionManager, FarmingPosition


@pytest.fixture
def mock_executor():
    """Create a mock trade executor"""
    executor = Mock()
    executor.get_open_positions.return_value = []
    executor.close_position.return_value = True
    return executor


@pytest.fixture
def position_manager(mock_executor, tmp_path):
    """Create a position manager with mock executor"""
    state_file = tmp_path / "state.json"
    trades_file = tmp_path / "trades.json"
    return PositionManager(
        executor=mock_executor,
        state_file=str(state_file),
        trades_log_file=str(trades_file)
    )


@pytest.fixture
def sample_position():
    """Create a sample position for testing"""
    return FarmingPosition(
        position_id="test-123",
        symbol="BTCUSDT",
        side="SHORT",
        quantity="0.001",
        entry_price=50000.0,
        leverage=10,
        expected_funding_rate=0.01,  # 1% positive rate (shorts receive)
        funding_settlement_time=datetime.now(timezone.utc) - timedelta(minutes=2),  # Past settlement
        entry_time=datetime.now(timezone.utc) - timedelta(minutes=5)
    )


class TestStopLoss:
    """Tests for stop loss logic"""
    
    def test_stop_loss_triggers_on_margin_loss(self, position_manager, sample_position):
        """Stop loss should trigger based on margin loss, not notional"""
        # With 10x leverage, entry_value = 0.001 * 50000 = $50
        # Margin = $5
        # 5% margin loss = $0.25
        current_pnl = -0.25  # 5% of margin
        
        should_exit, reason = position_manager.should_exit(
            position=sample_position,
            current_pnl=current_pnl,
            stop_loss_percent=0.05  # 5% stop loss
        )
        
        assert should_exit == True
        assert "Stop loss" in reason
    
    def test_stop_loss_does_not_trigger_below_threshold(self, position_manager, sample_position):
        """Stop loss should not trigger if loss is below threshold"""
        current_pnl = -0.10  # 2% of margin ($5), below 5% threshold
        
        should_exit, reason = position_manager.should_exit(
            position=sample_position,
            current_pnl=current_pnl,
            stop_loss_percent=0.05
        )
        
        # Should not exit due to stop loss (might exit for other reasons)
        assert "Stop loss" not in reason


class TestFundingRateReversal:
    """Tests for funding rate reversal detection"""
    
    def test_no_reversal_exit_on_small_fluctuation(self, position_manager, sample_position):
        """Small rate fluctuations should not trigger exit"""
        # Original rate was 1% positive (shorts receive)
        # Small fluctuation to 0.05% - should not exit
        
        # Position not past settlement yet
        sample_position.funding_settlement_time = datetime.now(timezone.utc) + timedelta(minutes=5)
        
        should_exit, reason = position_manager.should_exit(
            position=sample_position,
            current_pnl=0.0,
            current_funding_rate=0.0005  # 0.05% - small positive
        )
        
        assert "reversal" not in reason.lower()
    
    def test_reversal_exit_on_significant_flip(self, position_manager, sample_position):
        """Significant rate reversal should trigger exit"""
        # SHORT position with original rate 1% (receiving)
        # Rate flips to -0.6% (now paying)
        
        # Position not past settlement yet
        sample_position.funding_settlement_time = datetime.now(timezone.utc) + timedelta(minutes=5)
        
        should_exit, reason = position_manager.should_exit(
            position=sample_position,
            current_pnl=0.0,
            current_funding_rate=-0.006  # -0.6% (shorts now pay)
        )
        
        assert should_exit == True
        assert "reversal" in reason.lower()
    
    def test_long_position_reversal(self, position_manager):
        """Test reversal detection for LONG positions"""
        long_position = FarmingPosition(
            position_id="test-long",
            symbol="ETHUSDT",
            side="LONG",
            quantity="0.01",
            entry_price=3000.0,
            leverage=5,
            expected_funding_rate=-0.008,  # Negative rate (longs receive)
            funding_settlement_time=datetime.now(timezone.utc) + timedelta(minutes=5),
            entry_time=datetime.now(timezone.utc)
        )
        
        # Rate flips positive by significant amount (longs now pay)
        should_exit, reason = position_manager.should_exit(
            position=long_position,
            current_pnl=0.0,
            current_funding_rate=0.005  # 0.5% positive (longs pay)
        )
        
        assert should_exit == True
        assert "reversal" in reason.lower()


class TestPostSettlementExit:
    """Tests for exit logic after funding settlement"""
    
    def test_profit_exit(self, position_manager, sample_position):
        """Should exit immediately if in profit after settlement"""
        sample_position.funding_received = True
        sample_position.funding_amount = 0.50  # $0.50 funding received
        
        # Entry value = $50, margin = $5
        # Current PnL slightly negative, but total with funding is positive
        current_pnl = -0.10  # -2% of margin (below stop loss)
        # Total = -0.10 + 0.50 = +0.40
        
        should_exit, reason = position_manager.should_exit(
            position=sample_position,
            current_pnl=current_pnl,
            stop_loss_percent=0.10,  # 10% stop loss (won't trigger)
            soft_loss_percent=-0.01
        )
        
        assert should_exit == True
        assert "Profit" in reason
    
    def test_small_loss_exit(self, position_manager, sample_position):
        """Should exit if loss is small (soft loss threshold)"""
        sample_position.funding_received = True
        sample_position.funding_amount = 0.30
        
        # Entry value = $50, margin = $5
        # Current PnL negative, total still slightly negative but above threshold
        current_pnl = -0.32  # -6.4% of margin (below 10% stop loss)
        # Total = -0.32 + 0.30 = -0.02 (which is -0.04% of entry_value, > -0.2% soft loss)
        
        should_exit, reason = position_manager.should_exit(
            position=sample_position,
            current_pnl=current_pnl,
            stop_loss_percent=0.10,  # 10% stop loss
            soft_loss_percent=-0.002  # -0.2%
        )
        
        assert should_exit == True
        assert "Small Loss" in reason or "Profit" in reason
    
    def test_max_hold_time_exit(self, position_manager, sample_position):
        """Should exit if max hold time exceeded"""
        # Set settlement time far in the past
        sample_position.funding_settlement_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        sample_position.funding_received = True
        sample_position.funding_amount = 0.30
        
        # Entry value = $50, margin = $5
        # PnL negative but below stop loss threshold
        current_pnl = -0.20  # -4% of margin (below 10% stop loss)
        
        should_exit, reason = position_manager.should_exit(
            position=sample_position,
            current_pnl=current_pnl,
            stop_loss_percent=0.10,  # 10% stop loss (won't trigger)
            max_hold_minutes=5  # 5 minutes max, but we're at 10 minutes
        )
        
        assert should_exit == True
        assert "Max hold" in reason


class TestWaitingForSettlement:
    """Tests for pre-settlement behavior"""
    
    def test_holds_before_settlement(self, position_manager, sample_position):
        """Should hold position before settlement (unless stop loss)"""
        # Set settlement time in the future
        sample_position.funding_settlement_time = datetime.now(timezone.utc) + timedelta(minutes=5)
        
        should_exit, reason = position_manager.should_exit(
            position=sample_position,
            current_pnl=0.0
        )
        
        assert should_exit == False
        assert "Waiting" in reason or "Holding" in reason


class TestEdgeCases:
    """Tests for edge cases"""
    
    def test_zero_entry_value(self, position_manager):
        """Handle zero entry value gracefully"""
        zero_position = FarmingPosition(
            position_id="test-zero",
            symbol="TESTUSDT",
            side="SHORT",
            quantity="0",  # Zero quantity
            entry_price=100.0,
            leverage=10,
            expected_funding_rate=0.01,
            funding_settlement_time=datetime.now(timezone.utc) - timedelta(minutes=2),
            entry_time=datetime.now(timezone.utc)
        )
        zero_position.funding_received = True
        zero_position.funding_amount = 0.0
        
        # Should not crash
        should_exit, reason = position_manager.should_exit(
            position=zero_position,
            current_pnl=0.0
        )
        
        # Should exit (max hold or some other reason, but not crash)
        assert isinstance(should_exit, bool)
        assert isinstance(reason, str)
    
    def test_none_funding_rate(self, position_manager, sample_position):
        """Handle None funding rate gracefully"""
        sample_position.funding_settlement_time = datetime.now(timezone.utc) + timedelta(minutes=5)
        
        should_exit, reason = position_manager.should_exit(
            position=sample_position,
            current_pnl=0.0,
            current_funding_rate=None  # API returned None
        )
        
        # Should not crash, should continue holding
        assert should_exit == False
    
    def test_very_high_leverage(self, position_manager):
        """Test with very high leverage"""
        high_lev_position = FarmingPosition(
            position_id="test-high-lev",
            symbol="BTCUSDT",
            side="SHORT",
            quantity="0.001",
            entry_price=50000.0,
            leverage=100,  # 100x leverage
            expected_funding_rate=0.01,
            funding_settlement_time=datetime.now(timezone.utc) + timedelta(minutes=5),
            entry_time=datetime.now(timezone.utc)
        )
        
        # Entry value = $50, margin = $0.50 with 100x leverage
        # 5% margin loss = $0.025
        current_pnl = -0.025
        
        should_exit, reason = position_manager.should_exit(
            position=high_lev_position,
            current_pnl=current_pnl,
            stop_loss_percent=0.05
        )
        
        assert should_exit == True
        assert "Stop loss" in reason


class TestFarmingPosition:
    """Tests for FarmingPosition dataclass"""
    
    def test_is_active(self):
        """Test is_active property"""
        position = FarmingPosition(
            position_id="test",
            symbol="BTCUSDT",
            side="SHORT",
            quantity="0.001",
            entry_price=50000.0,
            leverage=10,
            expected_funding_rate=0.01,
            funding_settlement_time=datetime.now(timezone.utc),
            entry_time=datetime.now(timezone.utc)
        )
        
        assert position.is_active == True
        
        position.exit_time = datetime.now(timezone.utc)
        assert position.is_active == False
    
    def test_time_since_settlement(self):
        """Test time_since_settlement property"""
        past_settlement = datetime.now(timezone.utc) - timedelta(minutes=5)
        position = FarmingPosition(
            position_id="test",
            symbol="BTCUSDT",
            side="SHORT",
            quantity="0.001",
            entry_price=50000.0,
            leverage=10,
            expected_funding_rate=0.01,
            funding_settlement_time=past_settlement,
            entry_time=datetime.now(timezone.utc) - timedelta(minutes=10)
        )
        
        time_since = position.time_since_settlement
        assert time_since is not None
        assert time_since.total_seconds() >= 300  # At least 5 minutes
    
    def test_serialization(self):
        """Test to_dict and from_dict"""
        original = FarmingPosition(
            position_id="test",
            symbol="BTCUSDT",
            side="SHORT",
            quantity="0.001",
            entry_price=50000.0,
            leverage=10,
            expected_funding_rate=0.01,
            funding_settlement_time=datetime.now(timezone.utc),
            entry_time=datetime.now(timezone.utc)
        )
        
        data = original.to_dict()
        restored = FarmingPosition.from_dict(data)
        
        assert restored.position_id == original.position_id
        assert restored.symbol == original.symbol
        assert restored.side == original.side
        assert restored.entry_price == original.entry_price


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
