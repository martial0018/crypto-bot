# Crypto Pump & Dump Hunter — USDT-M Futures Paper Trading Only

## Strategy
- Uses public CCXT market data from Binance, Bybit, OKX, MEXC, and Bitget.
- Filters for linear, USDT-settled perpetual swap markets where the exchange exposes them through CCXT. This is futures market scanning, not spot scanning or DEX scanning.
- 24-hour pump threshold is 15% with no upper limit; 100%+ pumps remain eligible.
- Uses closed 5-minute candles for balanced reversal detection, a 1-hour bearish confirmation, and 4-hour trend context.
- Logs bid and ask order-book volumes in USDT for up to 10 shortlisted markets per exchange, aggregating the nearest 20 levels on each side. Order-book data is informational only and does not change trade-entry rules.
- Simulates short futures positions with configurable 5x–10x leverage, isolated margin-style accounting, fees, slippage, stop loss, take profit, trailing stop, estimated funding cost, and an approximate liquidation guard.

## Safety and limitations
- **Paper trading only.** No order placement, wallet signing, withdrawals, or private trading API calls. Do not add API keys.
- `leverage` defaults to `5.0`; change it to `10.0` in `config.json` to test 10x. Values outside 5x–10x are rejected.
- `paper_position_margin_usdt` is margin allocated per position. Notional exposure is margin multiplied by leverage. For example, $25 margin at 5x simulates $125 notional; at 10x it simulates $250 notional.
- Liquidation price is exchange-specific. The guard is only an approximation and is not a substitute for exchange liquidation calculations. The stop-loss is checked against ticker prices and can be exceeded by gaps or rapid moves.
- Funding varies by exchange, symbol, and time. `assumed_funding_cost_pct_per_interval` is a simplified conservative cost assumption, not a live funding-rate feed.
- Fee, slippage, funding, margin, and fills are estimates. No profitability or backtesting claim is made.
- This scans eligible centralized-exchange linear perpetual markets only, not every market and not DEX-only tokens. API availability and symbol support vary.

## Windows phone dashboard

From the repository root, double-click `start_futures_dashboard.bat` and keep its window open. The first run prints a private-network URL and a one-time password. Open the URL on a phone connected to the same trusted Wi-Fi, enter the password, and use **Start paper trading** or **Pause new entries**. If the password was missed, read `%LOCALAPPDATA%\CryptoDumpHunter\futures_control_token.txt`.

The PC must remain on and connected. If Windows Firewall prompts, allow access on Private networks only. Do not configure router port forwarding or expose the dashboard to the public internet. Pausing or losing the dashboard heartbeat blocks new entries; the simulator stays running to manage open paper positions.

Run `bot.py` directly only for troubleshooting: without a fresh dashboard heartbeat, it deliberately remains paused for new entries.

## Files created
- `bot.log` — scan logs
- `paper_futures_state.json` — paper balance and open futures positions
- `paper_futures_trades.jsonl` — completed paper futures trades
- `futures_control.json` — dashboard entry switch and heartbeat

The futures state and trade files are separate from the previous spot paper bot files so the accounting is not mixed.
