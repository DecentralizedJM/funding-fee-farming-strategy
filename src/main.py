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

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from config import FarmingConfig
from strategy_engine import StrategyEngine

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
    
    try:
        # Load configuration
        config = FarmingConfig()
        
        logger.info(f"Configuration loaded:")
        logger.info(f"  - Dry Run: {config.DRY_RUN}")
        logger.info(f"  - Rate Threshold: {config.EXTREME_RATE_THRESHOLD * 100:.2f}%")
        logger.info(f"  - Entry Window: {config.ENTRY_MIN_MINUTES_BEFORE}-{config.ENTRY_MAX_MINUTES_BEFORE} mins")
        logger.info(f"  - Max Positions: {config.MAX_CONCURRENT_POSITIONS}")
        logger.info(f"  - Min Order: ${config.MIN_ORDER_VALUE_USD}")
        logger.info(f"  - Telegram: {'Enabled' if config.TELEGRAM_BOT_TOKEN else 'Disabled'}")
        
        # Initialize strategy engine
        engine = StrategyEngine(config)
        
        # Handle shutdown signals
        def signal_handler(sig, frame):
            logger.info("Shutdown signal received. Stopping...")
            engine.stop()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Run the strategy
        logger.info("Starting strategy engine...")
        asyncio.run(engine.run())
        
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
