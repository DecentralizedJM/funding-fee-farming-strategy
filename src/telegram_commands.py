"""
Telegram Command Handler
========================

Handles incoming Telegram commands to control the bot.
Commands: /kill, /live, /status, /stats
"""

import logging
import asyncio
from typing import Optional, Callable, Any
from datetime import datetime, timezone
import requests
import threading

logger = logging.getLogger(__name__)


class TelegramCommandHandler:
    """Handle Telegram commands for bot control"""
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.last_update_id = 0
        self.running = False
        self._poll_thread = None
        
        # Callbacks for commands
        self._on_kill: Optional[Callable] = None
        self._on_live: Optional[Callable] = None
        self._on_status: Optional[Callable[[], dict]] = None
        self._on_stats: Optional[Callable[[], dict]] = None
        
        if not self.enabled:
            logger.warning("Telegram commands disabled - missing bot token or chat ID")
    
    def set_callbacks(
        self,
        on_kill: Callable = None,
        on_live: Callable = None,
        on_status: Callable[[], dict] = None,
        on_stats: Callable[[], dict] = None
    ):
        """Set callback functions for commands"""
        self._on_kill = on_kill
        self._on_live = on_live
        self._on_status = on_status
        self._on_stats = on_stats
    
    def start_polling(self):
        """Start polling for commands in background thread"""
        if not self.enabled:
            return
        
        self.running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        logger.info("Telegram command polling started")
    
    def stop_polling(self):
        """Stop polling"""
        self.running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        logger.info("Telegram command polling stopped")
    
    def _poll_loop(self):
        """Background polling loop"""
        while self.running:
            try:
                self._check_updates()
            except Exception as e:
                logger.error(f"Error polling Telegram: {e}")
            
            # Poll every 2 seconds
            for _ in range(20):  # 2 seconds in 100ms chunks
                if not self.running:
                    break
                threading.Event().wait(0.1)
    
    def _check_updates(self):
        """Check for new messages/commands"""
        try:
            url = f"{self.base_url}/getUpdates"
            params = {
                "offset": self.last_update_id + 1,
                "timeout": 1,
                "allowed_updates": ["message"]
            }
            
            response = requests.get(url, params=params, timeout=5)
            data = response.json()
            
            if not data.get("ok"):
                return
            
            for update in data.get("result", []):
                self.last_update_id = update["update_id"]
                self._handle_update(update)
                
        except requests.exceptions.Timeout:
            pass  # Normal timeout
        except Exception as e:
            logger.error(f"Error checking updates: {e}")
    
    def _handle_update(self, update: dict):
        """Handle a single update"""
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip().lower()
        
        # Only respond to messages from authorized chat
        if chat_id != self.chat_id:
            logger.warning(f"Ignoring message from unauthorized chat: {chat_id}")
            return
        
        # Handle commands
        if text == "/kill":
            self._handle_kill()
        elif text == "/live":
            self._handle_live()
        elif text == "/status":
            self._handle_status()
        elif text == "/stats":
            self._handle_stats()
        elif text == "/help":
            self._handle_help()
    
    def _send_message(self, text: str):
        """Send message to chat"""
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML"
            }
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.error(f"Error sending message: {e}")
    
    def _handle_kill(self):
        """Handle /kill command"""
        logger.info("Received /kill command")
        if self._on_kill:
            self._on_kill()
            self._send_message("🛑 <b>Strategy STOPPED</b>\n\nThe bot is paused and will not enter new positions.\nUse /live to resume.")
        else:
            self._send_message("⚠️ Kill callback not configured")
    
    def _handle_live(self):
        """Handle /live command"""
        logger.info("Received /live command")
        if self._on_live:
            self._on_live()
            self._send_message("🟢 <b>Strategy LIVE</b>\n\nThe bot is now actively scanning for opportunities.")
        else:
            self._send_message("⚠️ Live callback not configured")
    
    def _handle_status(self):
        """Handle /status command"""
        logger.info("Received /status command")
        if self._on_status:
            status = self._on_status()
            
            running_emoji = "🟢 LIVE" if status.get("running") else "🔴 PAUSED"
            
            message = f"""
📊 <b>BOT STATUS</b>

<b>Status:</b> {running_emoji}
<b>Active Positions:</b> {status.get('active_positions', 0)}/{status.get('max_positions', 3)}
<b>Mode:</b> {'DRY RUN' if status.get('dry_run') else 'LIVE TRADING'}

<b>Uptime:</b> {status.get('uptime', 'N/A')}
<b>Last Scan:</b> {status.get('last_scan', 'N/A')}
"""
            self._send_message(message.strip())
        else:
            self._send_message("⚠️ Status callback not configured")
    
    def _handle_stats(self):
        """Handle /stats command"""
        logger.info("Received /stats command")
        if self._on_stats:
            stats = self._on_stats()
            
            pnl = stats.get('total_pnl', 0)
            pnl_emoji = "📈" if pnl >= 0 else "📉"
            
            message = f"""
📊 <b>TRADING STATS</b>

<b>Today:</b>
📝 Trades: {stats.get('daily_trades', 0)}
{pnl_emoji} PnL: ${stats.get('daily_pnl', 0):+.4f}
🎁 Funding: ${stats.get('daily_funding', 0):+.4f}

<b>All Time:</b>
📝 Total Trades: {stats.get('total_trades', 0)}
🎯 Win Rate: {stats.get('win_rate', 0):.1f}%
💰 Total PnL: ${pnl:+.4f}
🎁 Total Funding: ${stats.get('total_funding', 0):+.4f}
"""
            self._send_message(message.strip())
        else:
            self._send_message("⚠️ Stats callback not configured")
    
    def _handle_help(self):
        """Handle /help command"""
        message = """
🤖 <b>FUNDING FEE FARMER COMMANDS</b>

/status - Check if bot is running
/stats - View trading statistics
/kill - Pause the strategy
/live - Resume the strategy
/help - Show this help message
"""
        self._send_message(message.strip())
