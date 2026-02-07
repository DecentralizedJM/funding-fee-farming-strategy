# Railway Deployment Guide

## Quick Deploy to Railway

### 1. Connect Repository

1. Go to [Railway](https://railway.app)
2. Click **"New Project"** → **"Deploy from GitHub repo"**
3. Select `DecentralizedJM/funding-fee-farming-strategy`
4. Railway will auto-detect the Dockerfile

### 2. Configure Environment Variables

In Railway dashboard, go to **Variables** tab and add:

| Variable | Required | Description |
|----------|----------|-------------|
| `MUDREX_API_SECRET` | ✅ Yes | Your Mudrex API secret key |
| `MARGIN_PERCENTAGE` | ✅ Yes | % of futures wallet to use as margin per position (e.g. 50) |
| `TELEGRAM_BOT_TOKEN` | ✅ Yes | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ Yes | Your Telegram chat ID |
| `DRY_RUN` | Optional | Set to `true` to test without real trades |

### 3. Deploy

Railway will automatically build and deploy when you push to main.

## Environment Variables Reference

```bash
# Required
MUDREX_API_SECRET=your_mudrex_api_secret
MARGIN_PERCENTAGE=50   # e.g. 50 = 50% of futures wallet per position

# Telegram (required for notifications)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Optional - defaults shown
DRY_RUN=false
```

## Viewing Logs

1. Go to your Railway project
2. Click on the service
3. Go to **Deployments** → Click latest deployment
4. View logs in real-time

## Redeploying

Push to `main` branch and Railway auto-deploys:

```bash
git push origin main
```

Or manually redeploy from Railway dashboard.

## Troubleshooting

### Bot Not Starting
- Check `MUDREX_API_SECRET` is set correctly
- View deployment logs for errors

### No Telegram Notifications
- Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
- Make sure bot is added to the chat

### API Errors
- Check Mudrex API key permissions
- Verify sufficient balance in futures wallet
