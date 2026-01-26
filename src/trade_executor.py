"""
Trade Executor
==============

Executes trades via Mudrex API.
Designed for Mudrex Futures trading.
"""

import logging
import sys
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

# Add mudrex SDK to path if needed
sys.path.insert(0, '/Users/jm/.gemini/antigravity/scratch/mudrex-sdk-reference')

try:
    from mudrex import MudrexClient
    from mudrex.models import Position, Asset
    MUDREX_AVAILABLE = True
except ImportError:
    MUDREX_AVAILABLE = False
    MudrexClient = None
    Position = None
    Asset = None

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    """Result of a trade execution"""
    success: bool
    position_id: Optional[str] = None
    order_id: Optional[str] = None
    symbol: Optional[str] = None
    side: Optional[str] = None
    quantity: Optional[str] = None
    entry_price: Optional[float] = None
    leverage: Optional[int] = None
    error: Optional[str] = None


class TradeExecutor:
    """Execute trades via Mudrex API"""
    
    def __init__(self, api_secret: str, dry_run: bool = False):
        self.api_secret = api_secret
        self.dry_run = dry_run
        self.client = None
        
        if not dry_run and MUDREX_AVAILABLE and api_secret:
            try:
                self.client = MudrexClient(api_secret=api_secret)
                logger.info("Mudrex client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Mudrex client: {e}")
        elif not MUDREX_AVAILABLE:
            logger.warning("Mudrex SDK not available - running in limited mode")
        
        if dry_run:
            logger.info("Trade executor running in DRY RUN mode")
    
    def check_symbol_available(self, symbol: str) -> bool:
        """
        Verify symbol is tradeable on Mudrex
        
        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")
        
        Returns:
            True if symbol is available
        """
        if self.dry_run:
            return True
        
        if not self.client:
            logger.warning("No Mudrex client - assuming symbol available")
            return True
        
        try:
            asset = self.client.assets.get(symbol)
            return asset is not None and asset.is_active
        except Exception as e:
            logger.error(f"Error checking symbol {symbol}: {e}")
            return False
    
    def get_asset_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get asset information including min quantity and max leverage
        
        Args:
            symbol: Trading symbol
        
        Returns:
            Asset info dict or None
        """
        if self.dry_run:
            return {
                "symbol": symbol,
                "min_quantity": "0.001",
                "max_leverage": 100,
                "is_active": True
            }
        
        if not self.client:
            return None
        
        try:
            asset = self.client.assets.get(symbol)
            if asset:
                return {
                    "symbol": asset.symbol,
                    "min_quantity": asset.min_quantity,
                    "max_leverage": int(asset.max_leverage) if asset.max_leverage else 100,
                    "quantity_step": asset.quantity_step,
                    "is_active": asset.is_active
                }
        except Exception as e:
            logger.error(f"Error getting asset info for {symbol}: {e}")
        
        return None
    
    def get_futures_balance(self) -> Optional[float]:
        """
        Get available futures balance
        
        Returns:
            Available balance as float or None
        """
        if self.dry_run:
            return 1000.0  # Mock balance for dry run
        
        if not self.client:
            return None
        
        try:
            balance = self.client.wallet.get_futures_balance()
            return float(balance.available) if balance else None
        except Exception as e:
            logger.error(f"Error getting futures balance: {e}")
            return None
    
    def calculate_position_size(
        self,
        symbol: str,
        price: float,
        leverage: int,
        min_order_value_usd: float = 8.0
    ) -> Optional[str]:
        """
        Calculate minimum position size for the given parameters
        
        Args:
            symbol: Trading symbol
            price: Current price
            leverage: Leverage to use
            min_order_value_usd: Minimum order value in USD
        
        Returns:
            Quantity string or None
        """
        try:
            asset_info = self.get_asset_info(symbol)
            
            if asset_info and asset_info.get("min_quantity"):
                min_qty = float(asset_info["min_quantity"])
                qty_step = float(asset_info.get("quantity_step", min_qty))
            else:
                # Fallback: calculate from min order value
                min_qty = min_order_value_usd / price
                qty_step = 0.001
            
            # Calculate quantity for minimum margin
            # margin = quantity * price / leverage
            # We want margin ≈ min_order_value / leverage (to minimize exposure)
            target_margin = min_order_value_usd
            quantity = (target_margin * leverage) / price
            
            # Round to nearest step
            if qty_step > 0:
                quantity = round(quantity / qty_step) * qty_step
            
            # Ensure at least minimum quantity
            quantity = max(quantity, min_qty)
            
            # Format with appropriate precision
            if qty_step >= 1:
                return str(int(quantity))
            elif qty_step >= 0.1:
                return f"{quantity:.1f}"
            elif qty_step >= 0.01:
                return f"{quantity:.2f}"
            elif qty_step >= 0.001:
                return f"{quantity:.3f}"
            else:
                return f"{quantity:.6f}"
            
        except Exception as e:
            logger.error(f"Error calculating position size: {e}")
            return None
    
    def open_position(
        self,
        symbol: str,
        side: str,
        quantity: str,
        leverage: int
    ) -> TradeResult:
        """
        Open a market position for funding farming
        
        Args:
            symbol: Trading symbol
            side: "LONG" or "SHORT"
            quantity: Position quantity
            leverage: Leverage to use
        
        Returns:
            TradeResult with execution details
        """
        logger.info(f"Opening {side} position: {symbol} qty={quantity} leverage={leverage}x")
        
        if self.dry_run:
            logger.info(f"[DRY RUN] Would open {side} {quantity} {symbol} @ {leverage}x")
            return TradeResult(
                success=True,
                position_id=f"dry_run_{symbol}_{side}",
                symbol=symbol,
                side=side,
                quantity=quantity,
                leverage=leverage,
                entry_price=0.0  # Would be filled from API
            )
        
        if not self.client:
            return TradeResult(
                success=False,
                error="Mudrex client not initialized"
            )
        
        try:
            # Set leverage first
            self.client.leverage.set(symbol, leverage=str(leverage), margin_type="ISOLATED")
            
            # Place market order
            order = self.client.orders.create_market_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                leverage=str(leverage)
            )
            
            if order:
                # Get position details
                positions = self.client.positions.list_open()
                position = next(
                    (p for p in positions if p.symbol == symbol),
                    None
                )
                
                position_id = position.position_id if position else order.order_id
                entry_price = float(position.entry_price) if position else 0.0
                
                logger.info(f"Position opened successfully: {position_id}")
                
                return TradeResult(
                    success=True,
                    position_id=position_id,
                    order_id=order.order_id if hasattr(order, 'order_id') else None,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    entry_price=entry_price,
                    leverage=leverage
                )
            else:
                return TradeResult(
                    success=False,
                    error="Order creation returned None"
                )
                
        except Exception as e:
            logger.error(f"Error opening position: {e}")
            return TradeResult(
                success=False,
                error=str(e)
            )
    
    def close_position(self, position_id: str) -> bool:
        """
        Close a position
        
        Args:
            position_id: ID of position to close
        
        Returns:
            True if closed successfully
        """
        logger.info(f"Closing position: {position_id}")
        
        if self.dry_run:
            logger.info(f"[DRY RUN] Would close position {position_id}")
            return True
        
        if not self.client:
            logger.error("Mudrex client not initialized")
            return False
        
        try:
            result = self.client.positions.close(position_id)
            logger.info(f"Position {position_id} closed: {result}")
            return result
        except Exception as e:
            logger.error(f"Error closing position {position_id}: {e}")
            return False
    
    def get_open_positions(self) -> List[Dict]:
        """
        Get all open positions
        
        Returns:
            List of position dicts
        """
        if self.dry_run:
            return []
        
        if not self.client:
            return []
        
        try:
            positions = self.client.positions.list_open()
            return [
                {
                    "position_id": p.position_id,
                    "symbol": p.symbol,
                    "side": p.side.value if hasattr(p.side, 'value') else str(p.side),
                    "quantity": p.quantity,
                    "entry_price": float(p.entry_price),
                    "mark_price": float(p.mark_price) if p.mark_price else 0,
                    "unrealized_pnl": float(p.unrealized_pnl) if p.unrealized_pnl else 0,
                    "margin": float(p.margin) if p.margin else 0,
                    "leverage": int(p.leverage) if p.leverage else 1,
                }
                for p in positions
            ]
        except Exception as e:
            logger.error(f"Error getting open positions: {e}")
            return []
    
    def get_position_pnl(self, position_id: str) -> Optional[float]:
        """
        Get current PnL of a position
        
        Args:
            position_id: Position ID
        
        Returns:
            Unrealized PnL or None
        """
        if self.dry_run:
            return 0.0
        
        if not self.client:
            return None
        
        try:
            position = self.client.positions.get(position_id)
            return float(position.unrealized_pnl) if position else None
        except Exception as e:
            logger.error(f"Error getting position PnL: {e}")
            return None
