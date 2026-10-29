# Crypto Pump & Dump Hunter (Paper Trading Only)

## Files
- `bot.py`: scanner, pump watchlist, bearish reversal detection, paper shorts
- `config.example.json`: sample symbols, thresholds, risk settings
- `config.json`: your local configuration, created from the example
- `requirements.txt`: Python dependency

## Windows setup
1. Install Python 3.11+ from https://www.python.org/downloads/ and enable "Add Python to PATH".
2. Extract this folder and create your local config:
   ```powershell
   Copy-Item config.example.json config.json
   ```
3. Open PowerShell in this folder.
4. Run:
   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   python bot.py
   ```

If PowerShell blocks activation, run this for the current terminal only:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`
Then activate the venv again.

## Google Cloud VM deployment

This is a continuously running command-line process, not a website. It has no web port or dashboard. The included systemd service starts it on boot and restarts it after failures.

Google Cloud's Always Free eligibility depends on the current region, machine, disk, and account terms. A billing account may be required, and an external IPv4 address or network traffic can still incur charges. Check Google's current Free Tier details and estimate costs before creating the VM; budget alerts do not cap spending. No hosting provider can be guaranteed to remain free.

1. In Google Cloud Console, create/select a project and enable Compute Engine. Create a small Ubuntu LTS VM that meets the current Always Free requirements. Restrict SSH access to your IP and do not open any application ports.
2. Install and initialize the Google Cloud CLI on your Windows computer, then set the project:
   ```powershell
   gcloud auth login
   gcloud config set project YOUR_PROJECT_ID
   ```
3. In this project folder, make the local config if you have not already:
   ```powershell
   Copy-Item config.example.json config.json
   ```
4. Create an upload folder on the VM and copy the app files. Replace `INSTANCE_NAME` and `ZONE` with your VM values:
   ```powershell
   gcloud compute ssh INSTANCE_NAME --zone=ZONE --command "mkdir -p ~/crypto-dump-hunter"
   gcloud compute scp .\bot.py .\requirements.txt .\config.json .\deploy\crypto-dump-hunter.service INSTANCE_NAME:~/crypto-dump-hunter/ --zone=ZONE
   gcloud compute ssh INSTANCE_NAME --zone=ZONE
   ```
5. In the VM's Linux shell, install the app and service:
   ```bash
   sudo apt update
   sudo apt install -y python3-venv python3-pip
   getent passwd cryptobot >/dev/null || sudo useradd --system --create-home --home-dir /opt/crypto-dump-hunter --shell /usr/sbin/nologin cryptobot
   sudo install -d -o cryptobot -g cryptobot -m 0750 /opt/crypto-dump-hunter
   sudo install -o cryptobot -g cryptobot -m 0644 ~/crypto-dump-hunter/bot.py /opt/crypto-dump-hunter/bot.py
   sudo install -o cryptobot -g cryptobot -m 0644 ~/crypto-dump-hunter/requirements.txt /opt/crypto-dump-hunter/requirements.txt
   sudo install -o cryptobot -g cryptobot -m 0640 ~/crypto-dump-hunter/config.json /opt/crypto-dump-hunter/config.json
   sudo -u cryptobot python3 -m venv /opt/crypto-dump-hunter/.venv
   sudo -u cryptobot /opt/crypto-dump-hunter/.venv/bin/pip install -r /opt/crypto-dump-hunter/requirements.txt
   sudo install -o root -g root -m 0644 ~/crypto-dump-hunter/crypto-dump-hunter.service /etc/systemd/system/crypto-dump-hunter.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now crypto-dump-hunter
   ```
6. Check service status and logs:
   ```bash
   sudo systemctl status crypto-dump-hunter --no-pager
   sudo journalctl -u crypto-dump-hunter -f
   ```

The VM keeps paper state and the bot log under `/opt/crypto-dump-hunter`. Back up `paper_state.json` if you need to preserve the paper portfolio across VM replacement. To update the app, copy the changed files again, install them with the commands above, then run `sudo systemctl restart crypto-dump-hunter`.

### Phone dashboard for futures paper trading

The futures simulator has a separate, password-protected mobile dashboard. It runs on this Windows PC and is reachable from a phone on the same trusted Wi-Fi network. The PC must stay on and connected. This controls paper futures only; it does not place exchange orders.

1. Double-click `start_futures_dashboard.bat` on the PC. Keep its window open.
2. The first run prints a private-network URL and a one-time access password. On the phone, open the URL using the PC's Wi-Fi address and enter that password. If you miss it, read `%LOCALAPPDATA%\CryptoDumpHunter\futures_control_token.txt` on the PC. Use the browser's **Add to Home Screen** option to keep an app-like shortcut.
3. If Windows Firewall prompts, allow access on **Private networks only**. Do not configure router port forwarding or expose this dashboard to the public internet.
4. Use **Start paper trading** to enable new simulated entries, or **Pause new entries** to stop new entries while the running simulator continues managing open paper positions. If the dashboard closes or loses its heartbeat, new entries pause within 20 seconds; the simulator continues managing existing positions.

The dashboard uses a random password, short-lived browser sessions, same-origin checks, and accepts connections only from private/local IP addresses. Its state and trade history remain separate under `futures_paper/`. The computer must be on the same local network as the phone; for control away from home, use a properly secured remote-access VPN rather than exposing the dashboard port.

### Phone monitoring with Telegram

Telegram sends pump alerts, paper trade opens/closes, bot startup, exchange outage/recovery alerts, and a periodic heartbeat. Heartbeats default to every 6 hours; set `telegram_heartbeat_seconds` to `0` to disable them. Repeated exchange errors are rate-limited to one alert per exchange every 30 minutes by default.

1. In Telegram, create a bot with the official `@BotFather`, then open a chat with that bot and send `/start`.
2. Obtain your Telegram chat ID (for example, from Telegram's `@userinfobot`).
3. In PowerShell, set the two values in the same terminal you will use to start the bot. Keep the token private; do not paste it into source files, logs, or chat:
   ```powershell
   $env:TELEGRAM_BOT_TOKEN = "your-bot-token"
   $env:TELEGRAM_CHAT_ID = "your-chat-id"
   .\.venv\Scripts\python.exe .\bot.py --test-telegram
   .\.venv\Scripts\python.exe .\bot.py
   ```
   The test command sends one message and exits. Start the regular bot only after you receive the test message.
4. On a Google Cloud VM, put the values in `/etc/crypto-dump-hunter.env` as described above, then restart the service:
   ```bash
   sudo systemctl restart crypto-dump-hunter
   ```

Environment variables take precedence over the optional Telegram fields in `config.json`. For a remote text log on a phone, connect to the VM over SSH and run `sudo journalctl -u crypto-dump-hunter -f`; do not expose the SSH port to the public internet without restricting it to trusted IPs.

## Linux local setup
```bash
# Run these commands from the project directory.
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
cp config.example.json config.json
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python bot.py
```

## Historical paper backtest

Run a separate historical simulation on the current top-volume eligible spot markets for each configured exchange:

```powershell
.\.venv\Scripts\python.exe .\backtest.py --days 30 --timeframe 5m
```

The script writes `backtest_results.json` and does not change `paper_state.json` or place orders. It uses the strategy thresholds and paper fees/slippage from `config.json`. By default, it tests up to the configured `max_symbols_per_exchange` (25) markets per exchange, ranked by current 24-hour volume.

The backtest uses closed historical candles, applies stop-losses against candle highs, and marks any remaining open positions at the end of the period. Exchange history availability can vary, so check the report's status, per-market candle coverage, and errors before interpreting results. The current-volume market selection is not a historical universe and may introduce survivorship bias. A 5-minute interval also changes the strategy's effective timing: the configured 15-candle pump window spans about 75 minutes rather than 15 minutes.

## Uploaded futures strategy backtest

The uploaded futures-paper strategy uses 5-minute reversal entries, a bearish 1-hour confirmation, and a 4-hour trend filter. It is not a separate 1-hour or 4-hour entry strategy. Run its combined multi-timeframe rules over 30 days with:

```powershell
.\.venv\Scripts\python.exe .\futures_backtest.py --days 30 --max-symbols 25
```

By default, the script reads the paper-only strategy settings from `C:\Users\Admin\Downloads\crypto_dump_hunter_futures_paper_5m.zip` and writes `futures_backtest_results.json`. Use `--config` to provide a JSON config or another copy of that archive. It scans only linear USDT perpetual futures, never submits orders, and does not change either bot's paper state. The market cap keeps the run practical; `--max-symbols` can be increased if desired. Review the report's coverage/errors and its survivorship-bias note before interpreting the result.

## Important limitations
- PAPER TRADING ONLY: no real orders are placed.
- Paper shorts are synthetic simulations, not actual spot-market short positions.
- Candle volume and candle direction are only proxies for selling pressure; they are not exchange trade-by-trade aggressor data.
- The starter implementation is not backtested or production-certified.
- Public exchange APIs may rate-limit requests or change behavior. If errors repeat, reduce the number of symbols, increase scan interval, and check the exchange status.
- Free cloud compute is not guaranteed to remain free. Check provider billing, region eligibility, storage, and networking charges before creating resources.
