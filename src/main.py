"""
Funding Fee Farming Strategy Bot
================================

Entry point for the bot.
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path
from datetime import datetime, timezone

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from config import FarmingConfig
from strategy_engine import StrategyEngine
from telegram_commands import TelegramCommandHandler


# Configure logging
def setup_logging(log_file: str = "logs/farming.log"):
    """Setup logging configuration"""
    # Create logs directory
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    
    # Configure root logger
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file)
        ]
    )
    
    # Reduce noise from external libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def main():
    """Main entry point"""
    setup_logging()
    logger = logging.getLogger(__name__)
    
    logger.info("=" * 60)
    logger.info("FUNDING FEE FARMING STRATEGY BOT")
    logger.info("Designed for Mudrex Futures")
    logger.info("=" * 60)
    
    start_time = datetime.now(timezone.utc)
    
    try:
        # Load configuration
        config = FarmingConfig()
        
        # Log configuration warnings (non-blocking)
        warnings = config.validate()
        for warning in warnings:
            logger.warning(f"Config: {warning}")
        
        logger.info(f"Configuration loaded:")
        logger.info(f"  - Rate Threshold: {config.EXTREME_RATE_THRESHOLD * 100:.2f}%")
        logger.info(f"  - Entry Window: {config.ENTRY_MIN_MINUTES_BEFORE}-{config.ENTRY_MAX_MINUTES_BEFORE} mins")
        logger.info(f"  - Max Positions: {config.MAX_CONCURRENT_POSITIONS}")
        logger.info(f"  - Min Order: ${config.MIN_ORDER_VALUE_USD}")
        logger.info(f"  - Telegram: {'Enabled' if config.TELEGRAM_BOT_TOKEN else 'Disabled'}")
        logger.info(f"  - Mudrex API: {'Configured' if config.MUDREX_API_SECRET else 'NOT SET'}")
        
        # Initialize strategy engine
        engine = StrategyEngine(config)
        
        # Initialize telegram command handler
        cmd_handler = TelegramCommandHandler(
            bot_token=config.TELEGRAM_BOT_TOKEN,
            chat_id=config.TELEGRAM_CHAT_ID
        )
        
        # Set up command callbacks
        def get_status():
            uptime = datetime.now(timezone.utc) - start_time
            hours, remainder = divmod(int(uptime.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            
            return {
                "running": engine.running,
                "active_positions": engine.position_manager.get_active_count(),
                "max_positions": config.MAX_CONCURRENT_POSITIONS,
                "uptime": f"{hours}h {minutes}m {seconds}s",
                "last_scan": "Every 30s"
            }
        
        def get_stats():
            perf = engine.position_manager.get_performance_stats()
            return {
                "daily_trades": engine._daily_trades,
                "daily_pnl": engine._daily_pnl,
                "daily_funding": engine._daily_funding,
                "total_trades": perf.get("total_trades", 0),
                "win_rate": perf.get("win_rate", 0),
                "total_pnl": perf.get("total_pnl", 0),
                "total_funding": perf.get("total_funding", 0)
            }
        
        cmd_handler.set_callbacks(
            on_kill=engine.pause,
            on_live=engine.resume,
            on_status=get_status,
            on_stats=get_stats
        )
        
        # Start command handler
        cmd_handler.start_polling()
        
        # Handle shutdown signals
        def signal_handler(sig, frame):
            logger.info("Shutdown signal received. Stopping...")
            cmd_handler.stop_polling()
            engine.stop()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Run the strategy
        logger.info("Starting strategy engine...")
        asyncio.run(engine.run())
        
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

