"""
Trade Executor
==============

Executes trades via Mudrex API.
Designed for Mudrex Futures trading.
"""

import logging
import time
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass
from functools import wraps

try:
    # Try the installed package name first
    from mudrex import MudrexClient
    from mudrex.models import Position, Asset
    MUDREX_AVAILABLE = True
except ImportError:
    try:
        # Fallback for different package structures
        from mudrex_trading_sdk import MudrexClient
        from mudrex_trading_sdk.models import Position, Asset
        MUDREX_AVAILABLE = True
    except ImportError:
        MUDREX_AVAILABLE = False
        MudrexClient = None
        Position = None
        Asset = None

logger = logging.getLogger(__name__)


def retry_api_call(max_retries: int = 3, delay: float = 1.0):
    """Decorator to retry API calls with exponential backoff"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for i in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    wait_time = delay * (2 ** i)
                    logger.warning(f"API call failed: {e}. Retrying in {wait_time}s ({i+1}/{max_retries})")
                    time.sleep(wait_time)
            
            # If we get here, all retries failed
            logger.error(f"API call failed after {max_retries} retries: {last_error}")
            # Let the caller handle the final exception or return error result
            raise last_error
        return wrapper
    return decorator


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
    
    def __init__(self, api_secret: str):
        self.api_secret = api_secret
        self.client = None
        
        if MUDREX_AVAILABLE and api_secret:
            try:
                self.client = MudrexClient(api_secret=api_secret)
                logger.info("Mudrex client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Mudrex client: {e}")
        elif not MUDREX_AVAILABLE:
            logger.warning("Mudrex SDK not available - will not be able to trade")
        elif not api_secret:
            logger.warning("No API secret provided - will not be able to trade")
    
    @retry_api_call(max_retries=3, delay=1.0)
    def check_symbol_available(self, symbol: str) -> bool:
        """
        Verify symbol is tradeable on Mudrex
        
        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")
        
        Returns:
            True if symbol is available
        """
        if not self.client:
            logger.warning("No Mudrex client - assuming symbol available")
            return True
        
        try:
            asset = self.client.assets.get(symbol)
            return asset is not None and asset.is_active
        except Exception as e:
            logger.error(f"Error checking symbol {symbol}: {e}")
            return False
    
    @retry_api_call(max_retries=3, delay=1.0)
    def get_asset_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get asset information including min quantity and max leverage
        
        Args:
            symbol: Trading symbol
        
        Returns:
            Asset info dict or None
        """
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
    
    @retry_api_call(max_retries=3, delay=1.0)
    def get_futures_balance(self) -> Optional[float]:
        """
        Get available futures balance
        
        Returns:
            Available balance as float or None
        """
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
            
            # Calculate quantity for minimum order value check
            # We want Position Value ≈ min_order_value_usd (not Margin)
            # This ensures we satisfy the exchange's minimum order size without over-committing capital
            
            # Old incorrect logic: target_margin = min_order_value
            # New logic: target_position_value = min_order_value
            
            target_value = min_order_value_usd
            quantity = target_value / price
            
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
    
    @retry_api_call(max_retries=3, delay=1.0)
    def open_position(
        self,
        symbol: str,
        side: str,
        quantity: str,
        leverage: int,
        stop_loss_price: Optional[str] = None
    ) -> TradeResult:
        """
        Open a market position for funding farming
        
        Args:
            symbol: Trading symbol
            side: "LONG" or "SHORT"
            quantity: Position quantity
            leverage: Leverage to use
            stop_loss_price: Stop loss price (optional)
        
        Returns:
            TradeResult with execution details
        """
        logger.info(f"Opening {side} position: {symbol} qty={quantity} leverage={leverage}x SL={stop_loss_price}")
        
        if not self.client:
            return TradeResult(
                success=False,
                error="Mudrex client not initialized - check API secret"
            )
        
        try:
            # Set leverage first
            self.client.leverage.set(symbol, leverage=str(leverage), margin_type="ISOLATED")
            
            # Place market order
            order = self.client.orders.create_market_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                leverage=str(leverage),
                stoploss_price=stop_loss_price
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
    
    @retry_api_call(max_retries=5, delay=1.0)  # More retries for closing
    def close_position(self, position_id: str) -> bool:
        """
        Close a position
        
        Args:
            position_id: ID of position to close
        
        Returns:
            True if closed successfully
        """
        logger.info(f"Closing position: {position_id}")
        
        if not self.client:
            logger.error("Mudrex client not initialized")
            return False
        
        try:
            result = self.client.positions.close(position_id)
            logger.info(f"Position {position_id} closed: {result}")
            return result
        except Exception as e:
            error_msg = str(e).lower()
            if "position is not open" in error_msg or "code: 400" in error_msg or "404" in error_msg:
                logger.warning(f"Position {position_id} appears already closed ({e}). Marking as success.")
                return True
                
            logger.error(f"Error closing position {position_id}: {e}")
            return False
    
    @retry_api_call(max_retries=3, delay=1.0)
    def get_open_positions(self) -> List[Dict]:
        """
        Get all open positions
        
        Returns:
            List of position dicts
        """
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
    
    @retry_api_call(max_retries=3, delay=1.0)
    def get_position_pnl(self, position_id: str) -> Optional[float]:
        """
        Get current PnL of a position
        
        Args:
            position_id: Position ID
        
        Returns:
            Unrealized PnL or None
        """
        if not self.client:
            return None
        
        try:
            position = self.client.positions.get(position_id)
            return float(position.unrealized_pnl) if position else None
        except Exception as e:
            error_msg = str(e).lower()
            if "404" in error_msg or "not found" in error_msg:
                # Common case for closed/liquidated positions
                logger.warning(f"Position {position_id} PnL not found (404). Assuming closed: {e}")
                return None
            
            logger.error(f"Error getting position PnL: {e}")
            return None
