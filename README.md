# 💰 Funding Fee Farming Strategy Bot

**Designed for Mudrex Futures**

An automated trading bot that farms extreme funding fees by detecting opportunities and opening opposing positions before settlement.

---

## 📋 Table of Contents

- [Overview](#overview)
- [How It Works](#how-it-works)
- [System Architecture](#system-architecture)
- [Strategy Logic](#strategy-logic)
- [Features](#features)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Telegram Notifications](#telegram-notifications)
- [Risk Disclaimers](#risk-disclaimers)
- [License](#license)

---

## 🎯 Overview

Perpetual futures contracts use **funding rates** to keep the contract price aligned with the spot price. When funding rates become extreme (≥0.5%), there's an opportunity to "farm" these fees by taking the opposing position.

**The Strategy:**
1. 🔍 **Detect** extreme funding rates across all trading pairs
2. ⏰ **Wait** for the optimal entry window (1-5 minutes before settlement)
3. 📈 **Enter** opposing position to receive funding fees
4. 💰 **Exit** after funding is credited with profit

---

## 🔄 How It Works

### Funding Rate Basics

| Funding Rate | Who Pays | Strategy Action |
|-------------|----------|-----------------|
| **Positive** (>0) | Longs pay Shorts | Open **SHORT** to receive |
| **Negative** (<0) | Shorts pay Longs | Open **LONG** to receive |

### Profit Calculation

```
Expected Profit = (Position Value × Funding Rate) - (2 × Trading Fee) - Slippage

Example:
├─ Position: $8 margin with 100x leverage = $800 exposure
├─ Funding Rate: 0.5% = $4.00 funding received
├─ Trading Fee: 0.06% × 2 (entry + exit) = $0.96
├─ Slippage Buffer: 0.02% × 2 = $0.32
└─ Net Profit: $4.00 - $0.96 - $0.32 = $2.72 (34% on margin!)
```

---

## 🏗️ System Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                    FUNDING FEE FARMING BOT                     │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  ┌──────────────┐    ┌─────────────────┐    ┌──────────────┐   │
│  │   Funding    │───▶│    Strategy     │───▶│    Trade     │   │
│  │   Fetcher    │    │     Engine      │    │   Executor   │   │
│  └──────────────┘    └────────┬────────┘    └──────┬───────┘   │
│         │                     │                    │           │
│         │            ┌────────▼────────┐           │           │
│         │            │    Position     │           │           │
│         │            │    Manager      │◀──────────┘           │
│         │            └────────┬────────┘                       │
│         │                     │                                │
│         │            ┌────────▼────────┐                       │
│         │            │    Telegram     │                       │
│         │            │    Notifier     │                       │
│         │            └─────────────────┘                       │
│         │                                                      │
│  ┌──────▼──────┐                            ┌──────────────┐   │
│  │  Perpetual  │                            │   Mudrex     │   │
│  │ Futures API │                            │     API      │   │
│  └─────────────┘                            └──────────────┘   │
│                                                                │
├────────────────────────────────────────────────────────────────┤
│                         DATA LAYER                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐ │
│  │ state.json  │  │ trades.json │  │     farming.log         │ │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
```

### Component Overview

| Component | Description |
|-----------|-------------|
| **Funding Fetcher** | Fetches real-time funding rates and instrument info |
| **Strategy Engine** | Main orchestration - scans opportunities, manages timing |
| **Trade Executor** | Executes trades via Mudrex API |
| **Position Manager** | Tracks positions, handles exit logic, persists state |
| **Telegram Notifier** | Sends alerts for entries, exits, and errors |

---

## 🎲 Strategy Logic

### Entry Logic Flow

```
                    ┌─────────────────────┐
                    │  Scan All Tickers   │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Funding Rate ≥ 0.5%?│
                    └──────────┬──────────┘
                               │
                    ┌──────────┴──────────┐
                    │ NO               YES│
                    ▼                     ▼
               ┌────────┐     ┌─────────────────────┐
               │  Skip  │     │ Time to Settlement  │
               └────────┘     │    1-5 minutes?     │
                              └──────────┬──────────┘
                                         │
                              ┌──────────┴──────────┐
                              │ NO               YES│
                              ▼                     ▼
                         ┌────────┐     ┌─────────────────────┐
                         │  Skip  │     │ Max Positions       │
                         └────────┘     │    Reached?         │
                                        └──────────┬──────────┘
                                                   │
                                        ┌──────────┴──────────┐
                                        │ YES              NO │
                                        ▼                     ▼
                                   ┌────────┐     ┌─────────────────────┐
                                   │  Skip  │     │ Calculate Position  │
                                   └────────┘     │       Size          │
                                                  └──────────┬──────────┘
                                                             │
                                                             ▼
                                                  ┌─────────────────────┐
                                                  │ Rate > 0: SHORT     │
                                                  │ Rate < 0: LONG      │
                                                  └──────────┬──────────┘
                                                             │
                                                             ▼
                                                  ┌─────────────────────┐
                                                  │   Open Position     │
                                                  │   via Mudrex API    │
                                                  └──────────┬──────────┘
                                                             │
                                                             ▼
                                                  ┌─────────────────────┐
                                                  │  Track & Notify     │
                                                  └─────────────────────┘
```

### Exit Logic Flow

```
                    ┌─────────────────────┐
                    │ Check Active        │
                    │   Positions         │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Settlement Passed?  │
                    └──────────┬──────────┘
                               │
                    ┌──────────┴──────────┐
                    │ NO               YES│
                    ▼                     ▼
               ┌────────────┐  ┌─────────────────────┐
               │ Keep       │  │ Mark Funding        │
               │ Holding    │  │   Received          │
               └────────────┘  └──────────┬──────────┘
                                          │
                                          ▼
                               ┌─────────────────────┐
                               │ Profit ≥ Target?    │
                               └──────────┬──────────┘
                                          │
                               ┌──────────┴──────────┐
                               │ NO               YES│
                               ▼                     ▼
                    ┌─────────────────┐   ┌─────────────────┐
                    │ Hold Time       │   │ EXIT:           │
                    │   > 30 min?     │   │ Profit Target   │
                    └────────┬────────┘   └─────────────────┘
                             │
                  ┌──────────┴──────────┐
                  │ NO               YES│
                  ▼                     ▼
       ┌─────────────────┐   ┌─────────────────┐
       │ Stop Loss Hit?  │   │ EXIT:           │
       └────────┬────────┘   │ Max Hold Time   │
                │            └─────────────────┘
     ┌──────────┴──────────┐
     │ NO               YES│
     ▼                     ▼
┌────────────┐   ┌─────────────────┐
│ Keep       │   │ EXIT:           │
│ Holding    │   │ Stop Loss       │
└────────────┘   └─────────────────┘
```

---

## ✨ Features

- 🔍 **Real-time Scanning** - Monitors all perpetual futures for extreme funding
- ⚡ **Optimized Entry** - Enters 1-5 minutes before settlement for minimal exposure
- 📊 **Smart Sizing** - Uses minimum order size with maximum leverage
- 🔔 **Telegram Alerts** - Notifications for opportunities, entries, and exits
- 💾 **State Persistence** - Survives restarts, tracks all trades
- 📈 **Performance Tracking** - Win rate, total PnL, funding earned
- 🐳 **Docker Ready** - Easy deployment with Docker Compose
- 🧪 **Dry Run Mode** - Test without real trades

---

## 🚀 Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/DecentralizedJM/funding-fee-farming-strategy.git
cd funding-fee-farming-strategy
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
# Edit .env with your API credentials
```

### 4. Run the Bot

```bash
# Dry run mode (no real trades)
DRY_RUN=true python -m src.main

# Live mode
python -m src.main
```

---

## ⚙️ Configuration

All settings are in `src/config.py` or can be overridden via environment variables:

| Setting | Default | Description |
|---------|---------|-------------|
| `EXTREME_RATE_THRESHOLD` | 0.005 (0.5%) | Minimum funding rate to farm |
| `ENTRY_MIN_MINUTES_BEFORE` | 1 | Earliest entry before settlement |
| `ENTRY_MAX_MINUTES_BEFORE` | 5 | Latest entry before settlement |
| `MAX_CONCURRENT_POSITIONS` | 3 | Maximum simultaneous positions |
| `MIN_ORDER_VALUE_USD` | 8.0 | Minimum order value |
| `USE_MAX_LEVERAGE` | true | Use maximum available leverage |
| `MIN_PROFIT_PERCENT` | 0.05 | Minimum profit to exit |
| `MAX_HOLD_MINUTES_AFTER_SETTLEMENT` | 30 | Force exit after this time |
| `DRY_RUN` | false | Enable dry-run mode |

---

## 🐳 Deployment

### Using Docker Compose

```bash
# Build and run
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

### Environment Variables

```bash
# Required
MUDREX_API_SECRET=your_api_secret

# Optional (Telegram notifications)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Optional settings
DRY_RUN=false
LOG_LEVEL=INFO
```

---

## 📱 Telegram Notifications

The bot sends notifications for:

| Event | Notification |
|-------|-------------|
| 🎯 **Opportunity Detected** | Symbol, rate, recommended side, time to settlement |
| 📈 **Position Opened** | Entry details, leverage, expected funding |
| 📉 **Position Closed** | Exit details, PnL, funding received, reason |
| ⚠️ **Errors** | Error type and details |
| 🚀 **Bot Started** | Configuration summary |
| 📊 **Daily Summary** | Trade count, total PnL, win rate |

### Example Notifications

```
🎯 FUNDING OPPORTUNITY DETECTED

DOGEUSDT
🔴 Rate: -1.2500%
📊 Bias: Shorts Pay Longs
⏰ Settlement In: 0:03:45
💰 Price: $0.0845

🎲 Recommended: Open LONG
```

```
📉 POSITION CLOSED

DOGEUSDT
📊 Side: LONG
💰 Entry: $0.0845
💰 Exit: $0.0847

💰 PROFIT: +$2.45 (+0.24%)
🎁 Funding Fee: +$3.20

📝 Reason: Profit target reached
⏱ Hold Time: 0:12:35
```

---

## ⚠️ Risk Disclaimers

> **Market Risk**: Price can move significantly during the position hold time, potentially causing losses that exceed the funding fee earned.

> **Execution Risk**: Network delays or API issues could affect entry/exit timing.

> **Funding Rate Changes**: The predicted funding rate can change before settlement. The actual rate received may differ from what was detected.

> **Leverage Risk**: High leverage amplifies both gains and losses. The bot uses maximum available leverage by default.

> **Not Financial Advice**: This bot is for educational purposes. Use at your own risk.

---

## 📁 Project Structure

```
funding-fee-farming-strategy/
├── src/
│   ├── __init__.py
│   ├── main.py                 # Entry point
│   ├── config.py               # Configuration management
│   ├── funding_fetcher.py      # Funding rate data fetcher
│   ├── trade_executor.py       # Mudrex trading execution
│   ├── position_manager.py     # Position tracking and exits
│   ├── strategy_engine.py      # Main orchestration
│   └── telegram_notifier.py    # Telegram notifications
├── data/
│   ├── state.json              # Active positions
│   └── trades.json             # Trade history
├── logs/
│   └── farming.log             # Application logs
├── tests/
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## 📄 License

MIT License - see [LICENSE](LICENSE) for details.

---

## 🔗 Links

- **Repository**: [github.com/DecentralizedJM/funding-fee-farming-strategy](https://github.com/DecentralizedJM/funding-fee-farming-strategy)
- **Mudrex SDK**: [github.com/DecentralizedJM/mudrex-api-trading-python-sdk](https://github.com/DecentralizedJM/mudrex-api-trading-python-sdk)

---

**Made with ❤️ for Mudrex Futures Trading**
