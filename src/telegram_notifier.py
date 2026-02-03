"""
Telegram Notification Client
============================

Sends notifications for trade entries, exits, and alerts.
"""

import logging
import requests
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send trading notifications via Telegram"""
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        
        if not self.enabled:
            logger.warning("Telegram notifications disabled - missing bot token or chat ID")
    
    def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """
        Send a message to the configured chat
        
        Args:
            message: Message text
            parse_mode: HTML or Markdown
        
        Returns:
            True if sent successfully
        """
        if not self.enabled:
            logger.debug(f"Telegram disabled, would send: {message[:100]}...")
            return False
        
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }
            
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            
            result = response.json()
            if not result.get("ok"):
                logger.error(f"Telegram API error: {result}")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False
    
    def notify_opportunity_detected(
        self,
        symbol: str,
        funding_rate: float,
        recommended_side: str,
        time_to_settlement: str,
        price: float
    ) -> bool:
        """Notify about a detected funding opportunity"""
        
        rate_emoji = "🔴" if funding_rate < 0 else "🟢"
        direction = "Shorts Pay Longs" if funding_rate < 0 else "Longs Pay Shorts"
        
        message = f"""
🎯 <b>FUNDING OPPORTUNITY DETECTED</b>

<b>{symbol}</b>
{rate_emoji} Rate: <code>{funding_rate*100:+.4f}%</code>
📊 Bias: {direction}
⏰ Settlement In: {time_to_settlement}
💰 Price: ${price:,.2f}

🎲 <b>Recommended:</b> Open <code>{recommended_side}</code>
"""
        return self.send_message(message.strip())
    
    def notify_entry(
        self,
        symbol: str,
        side: str,
        quantity: str,
        entry_price: float,
        leverage: int,
        expected_funding_rate: float,
        position_id: str
    ) -> bool:
        """Notify about trade entry"""
        
        side_emoji = "🟢" if side == "LONG" else "🔴"
        
        message = f"""
📈 <b>POSITION OPENED</b>

<b>{symbol}</b>
{side_emoji} Side: <code>{side}</code>
📊 Quantity: <code>{quantity}</code>
💰 Entry Price: ${entry_price:,.4f}
⚡ Leverage: {leverage}x
🎯 Expected Funding: <code>{expected_funding_rate*100:+.4f}%</code>

🆔 Position: <code>{position_id[:16]}...</code>
"""
        return self.send_message(message.strip())
    
    def notify_exit(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_percent: float,
        funding_received: float,
        reason: str,
        hold_time: str
    ) -> bool:
        """Notify about trade exit"""
        
        pnl_emoji = "💰" if pnl >= 0 else "💸"
        result = "PROFIT" if pnl >= 0 else "LOSS"
        
        message = f"""
📉 <b>POSITION CLOSED</b>

<b>{symbol}</b>
📊 Side: <code>{side}</code>
💰 Entry: ${entry_price:,.4f}
💰 Exit: ${exit_price:,.4f}

{pnl_emoji} <b>{result}: ${pnl:+.4f} ({pnl_percent:+.2f}%)</b>
🎁 Funding Fee: ${funding_received:+.4f}

📝 Reason: {reason}
⏱ Hold Time: {hold_time}
"""
        return self.send_message(message.strip())
    
    def notify_reversal_opened(
        self,
        symbol: str,
        original_side: str,
        reversed_side: str,
        first_leg_pnl: float,
        first_leg_funding: float,
        entry_price: float,
        position_id: str
    ) -> bool:
        """Notify about settlement reversal - position flipped to opposite side"""
        
        first_leg_total = first_leg_pnl + first_leg_funding
        first_leg_emoji = "💰" if first_leg_total >= 0 else "💸"
        side_emoji = "🟢" if reversed_side == "LONG" else "🔴"
        
        message = f"""
🔄 <b>SETTLEMENT REVERSAL</b>

<b>{symbol}</b>
📊 Original: <code>{original_side}</code> → Reversed: <code>{reversed_side}</code>

{first_leg_emoji} First Leg PnL: ${first_leg_pnl:+.4f}
🎁 Funding Received: ${first_leg_funding:+.4f}
📊 First Leg Total: ${first_leg_total:+.4f}

{side_emoji} <b>NEW POSITION</b>
💰 Entry Price: ${entry_price:,.4f}
🆔 Position: <code>{position_id[:16]}...</code>

⏳ Waiting for profit target or max hold time...
"""
        return self.send_message(message.strip())
    
    def notify_error(self, error_type: str, details: str) -> bool:
        """Notify about errors"""
        
        message = f"""
⚠️ <b>ERROR</b>

<b>Type:</b> {error_type}
<b>Details:</b> {details}
<b>Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC
"""
        return self.send_message(message.strip())
    
    def notify_startup(self, config_summary: str) -> bool:
        """Notify about bot startup"""
        
        message = f"""
🚀 <b>FUNDING FEE FARMER STARTED</b>

{config_summary}

⏰ Started: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC
"""
        return self.send_message(message.strip())
    
    def notify_daily_summary(
        self,
        trades_count: int,
        total_pnl: float,
        total_funding: float,
        win_rate: float
    ) -> bool:
        """Send daily performance summary"""
        
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        
        message = f"""
📊 <b>DAILY SUMMARY</b>

📝 Trades: {trades_count}
{pnl_emoji} Total PnL: ${total_pnl:+.4f}
🎁 Funding Earned: ${total_funding:+.4f}
🎯 Win Rate: {win_rate:.1f}%

📅 {datetime.utcnow().strftime('%Y-%m-%d')}
"""
        return self.send_message(message.strip())

    def notify_skipped(self, symbol: str, reason: str) -> bool:
        """Notify about skipped opportunity"""
        message = f"""
🚫 <b>SKIPPED: {symbol}</b>

Reason: {reason}
"""
        return self.send_message(message.strip())
