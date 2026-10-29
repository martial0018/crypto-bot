"""
Crypto Pump & Dump Hunter — PAPER TRADING ONLY

Features:
- Scans public spot-market tickers on Binance, Bitget, Bybit, MEXC and OKX.
- Detects pumps from closed 1-minute candles.
- Persists pumped markets on a watchlist to detect reversals after the pump.
- Uses bearish candle volume, upper-wick rejection, support break and failed recovery.
- Simulates short trades, fees, slippage, stop-loss and trailing stop.
- Manages open positions even when a symbol drops out of the current scanner shortlist.
- Optional Telegram notifications.
- Never places real exchange orders.
"""
import argparse
import ccxt
import json
import os
import time
import logging
from datetime import datetime, timezone, date
from typing import Any

CONFIG_FILE = "config.json"
STATE_FILE = "paper_state.json"
LOG_FILE = "bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)

with open(CONFIG_FILE, encoding="utf-8") as f:
    CFG = json.load(f)

EXCHANGES: dict[str, Any] = {}
LAST_ALERTS: dict[str, float] = {}
EXCHANGE_FAILURES: dict[str, dict[str, Any]] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state() -> dict:
    return {
        "balance": float(CFG["paper_starting_balance_usdt"]),
        "starting_balance": float(CFG["paper_starting_balance_usdt"]),
        "positions": {},
        "watchlist": {},
        "trades": [],
        "daily": {"date": str(date.today()), "realized_pnl": 0.0},
    }


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return default_state()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            loaded = json.load(f)
        base = default_state()
        for key in base:
            if key in loaded:
                base[key] = loaded[key]
        base.setdefault("positions", {})
        base.setdefault("watchlist", {})
        base.setdefault("trades", [])
        base.setdefault("daily", {"date": str(date.today()), "realized_pnl": 0.0})
        return base
    except (OSError, json.JSONDecodeError) as exc:
        logging.error("Could not read %s: %s; starting new paper state.", STATE_FILE, exc)
        return default_state()


state = load_state()


def save_state() -> None:
    temp = STATE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, STATE_FILE)


def get_exchange(exchange_id: str):
    if exchange_id in EXCHANGES:
        return EXCHANGES[exchange_id]
    cls = getattr(ccxt, exchange_id)
    exchange = cls({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "spot"},
    })
    EXCHANGES[exchange_id] = exchange
    return exchange


def pct_change(old: float, new: float) -> float:
    return ((new / old) - 1.0) * 100.0 if old else 0.0


def telegram_credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", CFG.get("telegram_bot_token", "")).strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", CFG.get("telegram_chat_id", "")).strip()
    return token, chat_id


def notify(message: str) -> bool:
    token, chat_id = telegram_credentials()
    if not token or not chat_id:
        return False
    try:
        import requests
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            logging.warning("Telegram rejected a notification.")
            return False
        return True
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logging.warning(
            "Telegram notification failed (%s%s).",
            type(exc).__name__,
            f", HTTP {status}" if status else "",
        )
        return False


def record_exchange_success(exchange_id: str) -> None:
    failure = EXCHANGE_FAILURES.pop(exchange_id, None)
    if failure:
        notify(f"✅ Exchange recovered: {exchange_id}\nMarket scan is working again.")


def record_exchange_failure(exchange_id: str, error: Exception) -> None:
    now = time.time()
    failure = EXCHANGE_FAILURES.setdefault(
        exchange_id, {"last_alert": None, "error": ""}
    )
    message = str(error).replace("\n", " ")[:240]
    failure["error"] = message
    cooldown = max(0, int(CFG.get("exchange_alert_cooldown_seconds", 1800)))
    last_alert = failure["last_alert"]
    if last_alert is None or now - float(last_alert) >= cooldown:
        notify(
            f"⚠️ Exchange scan failed: {exchange_id}\n"
            f"{type(error).__name__}: {message or 'No details'}"
        )
        failure["last_alert"] = now


def send_heartbeat(successful_exchanges: list[str]) -> None:
    total_net = sum(float(trade.get("net_pnl", 0)) for trade in state["trades"])
    exchange_lines = []
    for exchange_id in CFG["exchanges"]:
        status = "⚠️ issue"
        if exchange_id not in EXCHANGE_FAILURES:
            status = "✅ scanned"
        exchange_lines.append(f"{status} {exchange_id}")

    notify(
        "📊 Crypto bot heartbeat — PAPER ONLY\n"
        f"Exchanges scanned this cycle: {len(successful_exchanges)}/"
        f"{len(CFG['exchanges'])}\n"
        f"Open paper positions: {len(state['positions'])}\n"
        f"Markets watched: {len(state['watchlist'])}\n"
        f"Closed paper trades: {len(state['trades'])}\n"
        f"Realized net PnL: {total_net:.2f} USDT\n"
        + "\n".join(exchange_lines)
    )


def eligible_spot_usdt_markets(exchange, tickers: dict) -> list[tuple[str, float, float]]:
    markets = exchange.load_markets()
    candidates = []
    quote = CFG["quote_currency"]

    for symbol, ticker in tickers.items():
        market = markets.get(symbol)
        if not market or not market.get("spot", False):
            continue
        if market.get("active") is False:
            continue
        if market.get("quote") != quote:
            continue

        last = ticker.get("last")
        quote_volume = ticker.get("quoteVolume")
        if not last or not quote_volume:
            continue
        try:
            last = float(last)
            quote_volume = float(quote_volume)
        except (TypeError, ValueError):
            continue
        if last <= 0 or quote_volume < float(CFG["minimum_quote_volume"]):
            continue
        candidates.append((symbol, quote_volume, last))

    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[:int(CFG["max_symbols_per_exchange"])]


def scan_exchange(exchange_id: str) -> list[tuple[str, float, float]]:
    exchange = get_exchange(exchange_id)
    logging.info("Fetching tickers: %s", exchange_id)
    tickers = exchange.fetch_tickers()
    return eligible_spot_usdt_markets(exchange, tickers)


def fetch_closed_candles(exchange, symbol: str) -> list[list[float]]:
    candles = exchange.fetch_ohlcv(
        symbol,
        timeframe=CFG["timeframe"],
        limit=int(CFG["ohlcv_limit"]),
    )
    # Most exchanges include the currently forming candle as the last row.
    # Exclude it so decisions are based only on closed candles.
    if len(candles) > 1:
        candles = candles[:-1]
    return candles


def analyse_pump_and_reversal(exchange, symbol: str) -> dict | None:
    candles = fetch_closed_candles(exchange, symbol)
    window = int(CFG["pump_window_candles"])
    if len(candles) < window + 6:
        return None

    closes = [float(c[4]) for c in candles]
    recent = candles[-window:]
    reference_close = float(candles[-(window + 1)][4])
    last_close = closes[-1]
    pump_pct = pct_change(reference_close, last_close)

    key = f"{exchange.id}:{symbol}"
    now_epoch = time.time()
    watch = state["watchlist"].get(key)

    # Create or refresh a watch entry when a pump is detected.
    if pump_pct >= float(CFG["pump_threshold_pct"]):
        if not watch:
            watch = {
                "detected_at": utc_now(),
                "detected_epoch": now_epoch,
                "pump_pct_at_detection": pump_pct,
                "peak_price": max(float(c[2]) for c in recent),
                "last_seen": utc_now(),
            }
            state["watchlist"][key] = watch
            msg = (
                f"🚨 PUMP DETECTED (PAPER MODE)\n{key}\n"
                f"Change over ~{window} candles: {pump_pct:.2f}%\n"
                f"Last close: {last_close:.8g}"
            )
            logging.warning(msg.replace("\n", " | "))
            notify(msg)
        else:
            watch["peak_price"] = max(
                float(watch.get("peak_price", 0)),
                max(float(c[2]) for c in recent),
            )
            watch["last_seen"] = utc_now()

    watch = state["watchlist"].get(key)
    if not watch:
        return None

    watch["peak_price"] = max(
        float(watch.get("peak_price", last_close)),
        max(float(c[2]) for c in recent),
    )
    watch["last_seen"] = utc_now()

    # Expire stale watch entries after configured time.
    age_minutes = (now_epoch - float(watch.get("detected_epoch", now_epoch))) / 60
    if age_minutes > float(CFG["watchlist_minutes"]):
        del state["watchlist"][key]
        return None

    # Avoid entering a short until there is evidence the pump is reversing.
    last_candle = candles[-1]
    previous_candles = candles[-6:-1]
    if len(previous_candles) < 5:
        return None

    support = min(float(c[3]) for c in previous_candles)
    recent_high = max(float(c[2]) for c in candles[-window:])
    last_open = float(last_candle[1])
    last_high = float(last_candle[2])
    last_low = float(last_candle[3])
    last_close = float(last_candle[4])

    bearish_recent = candles[-5:]
    total_volume = sum(float(c[5]) for c in bearish_recent)
    bearish_volume = sum(
        float(c[5]) for c in bearish_recent if float(c[4]) < float(c[1])
    )
    bearish_volume_ratio = bearish_volume / total_volume if total_volume else 0.0

    candle_range = last_high - last_low
    upper_wick = last_high - max(last_open, last_close)
    rejection = candle_range > 0 and upper_wick / candle_range >= 0.40
    support_break = last_close < support

    last_three_highs = max(float(c[2]) for c in candles[-3:])
    preceding_highs = max(float(c[2]) for c in candles[-8:-3])
    failed_recovery = last_three_highs < preceding_highs
    falling_close = closes[-1] < closes[-2]

    signals = {
        "bearish_volume": bearish_volume_ratio >= 0.60,
        "upper_wick_rejection": rejection,
        "support_break": support_break,
        "failed_recovery": failed_recovery and falling_close,
    }
    score = sum(bool(v) for v in signals.values())

    return {
        "price": last_close,
        "pump_pct_now": pump_pct,
        "pump_pct_at_detection": float(watch.get("pump_pct_at_detection", pump_pct)),
        "support": support,
        "recent_high": recent_high,
        "bearish_volume_ratio": bearish_volume_ratio,
        "signals": signals,
        "score": score,
        "time": utc_now(),
    }


def daily_loss_limit_reached() -> bool:
    today = str(date.today())
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "realized_pnl": 0.0}
        save_state()
    return float(state["daily"].get("realized_pnl", 0.0)) <= -abs(
        float(CFG["max_daily_loss_usdt"])
    )


def open_paper_short(key: str, signal: dict) -> None:
    if key in state["positions"]:
        return
    if len(state["positions"]) >= int(CFG["max_open_positions"]):
        return
    if daily_loss_limit_reached():
        logging.warning("Daily paper loss limit reached; no new paper entries.")
        return

    collateral = float(CFG["paper_position_usdt"])
    fee_rate = float(CFG["paper_fee_rate"])
    slippage = float(CFG["paper_slippage_rate"])
    if collateral <= 0 or collateral + collateral * fee_rate > state["balance"]:
        return

    # Selling at a slightly lower price simulates adverse short-entry slippage.
    entry_price = float(signal["price"]) * (1 - slippage)
    if entry_price <= 0:
        return

    quantity = collateral / entry_price
    entry_fee = collateral * fee_rate
    state["balance"] -= collateral + entry_fee

    state["positions"][key] = {
        "entry": entry_price,
        "quantity": quantity,
        "collateral": collateral,
        "entry_fee": entry_fee,
        "lowest_price": entry_price,
        "stop_price": entry_price * (1 + float(CFG["stop_loss_pct"]) / 100),
        "trailing_stop_pct": float(CFG["trailing_stop_pct"]),
        "opened_at": signal["time"],
        "exchange": key.split(":", 1)[0],
        "symbol": key.split(":", 1)[1],
    }
    msg = (
        f"🧪 PAPER SHORT OPENED\n{key}\nEntry: {entry_price:.8g}\n"
        f"Bearish signals: {signal['score']}/4\n"
        f"Signals: {signal['signals']}"
    )
    logging.warning(msg.replace("\n", " | "))
    notify(msg)
    save_state()


def close_paper_short(key: str, market_price: float, reason: str) -> None:
    position = state["positions"].get(key)
    if not position:
        return

    exit_price = float(market_price) * (1 + float(CFG["paper_slippage_rate"]))
    qty = float(position["quantity"])
    exit_value = qty * exit_price
    exit_fee = exit_value * float(CFG["paper_fee_rate"])
    gross_pnl = (float(position["entry"]) - exit_price) * qty
    net_pnl = gross_pnl - float(position["entry_fee"]) - exit_fee

    state["balance"] += float(position["collateral"]) + gross_pnl - exit_fee
    state["daily"]["realized_pnl"] = float(
        state["daily"].get("realized_pnl", 0.0)
    ) + net_pnl

    trade = {
        "market": key,
        "entry": position["entry"],
        "exit": exit_price,
        "gross_pnl": gross_pnl,
        "entry_fee": position["entry_fee"],
        "exit_fee": exit_fee,
        "net_pnl": net_pnl,
        "reason": reason,
        "opened_at": position["opened_at"],
        "closed_at": utc_now(),
    }
    state["trades"].append(trade)
    del state["positions"][key]

    msg = (
        f"🧪 PAPER SHORT CLOSED\n{key}\nReason: {reason}\n"
        f"Net PnL: {net_pnl:.4f} USDT\nFree balance: {state['balance']:.2f} USDT"
    )
    logging.warning(msg.replace("\n", " | "))
    notify(msg)
    save_state()


def manage_open_positions() -> None:
    # Position management runs independently of the top-volume candidate list.
    for key, position in list(state["positions"].items()):
        exchange_id = position["exchange"]
        symbol = position["symbol"]
        try:
            exchange = get_exchange(exchange_id)
            ticker = exchange.fetch_ticker(symbol)
            price = ticker.get("last")
            if not price:
                continue
            price = float(price)

            position["lowest_price"] = min(float(position["lowest_price"]), price)
            trailing_stop = float(position["lowest_price"]) * (
                1 + float(position["trailing_stop_pct"]) / 100
            )
            effective_stop = min(float(position["stop_price"]), trailing_stop)
            position["stop_price"] = effective_stop

            if price >= effective_stop:
                close_paper_short(key, price, "stop_loss_or_trailing_stop")
        except Exception as exc:
            logging.warning("Could not manage position %s: %s", key, exc)
    save_state()


def scan_watchlist() -> None:
    for key in list(state["watchlist"].keys()):
        try:
            exchange_id, symbol = key.split(":", 1)
            exchange = get_exchange(exchange_id)
            signal = analyse_pump_and_reversal(exchange, symbol)
            if not signal:
                continue

            logging.info(
                "%s pump-at-detection=%.2f%% score=%d signals=%s",
                key,
                signal["pump_pct_at_detection"],
                signal["score"],
                signal["signals"],
            )

            require_break = bool(CFG.get("require_support_break", True))
            enough_signals = signal["score"] >= int(CFG["minimum_bearish_signals"])
            support_ok = (not require_break) or signal["signals"]["support_break"]

            if enough_signals and support_ok and key not in state["positions"]:
                open_paper_short(key, signal)
            save_state()
        except Exception as exc:
            logging.warning("Watchlist error %s: %s", key, exc)


def print_status() -> None:
    total_net = sum(float(t.get("net_pnl", 0)) for t in state["trades"])
    wins = sum(1 for t in state["trades"] if float(t.get("net_pnl", 0)) > 0)
    logging.info(
        "STATUS | free_balance=%.2f | open_positions=%d | watched=%d | "
        "closed_trades=%d | net_realized=%.4f | wins=%d",
        state["balance"], len(state["positions"]), len(state["watchlist"]),
        len(state["trades"]), total_net, wins
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the paper-trading market monitor.")
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test notification and exit.",
    )
    args = parser.parse_args()
    if args.test_telegram:
        if not all(telegram_credentials()):
            raise SystemExit(
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before testing Telegram."
            )
        if not notify("✅ Telegram test successful — bot notifications are connected."):
            raise SystemExit("Telegram test failed; check the credentials and network.")
        logging.info("Telegram test notification sent.")
        return

    logging.info("Crypto Pump & Dump Hunter running — PAPER TRADING ONLY")
    logging.info("No real orders will be submitted. Do not add exchange trading keys.")
    if all(telegram_credentials()):
        notify("🤖 Crypto bot started — PAPER ONLY; no real orders will be placed.")
    heartbeat_interval = max(
        0, int(CFG.get("telegram_heartbeat_seconds", 21600))
    )
    last_heartbeat = time.monotonic()
    while True:
        cycle_start = time.time()
        successful_exchanges = []
        for exchange_id in CFG["exchanges"]:
            try:
                exchange = get_exchange(exchange_id)
                candidates = scan_exchange(exchange_id)
                successful_exchanges.append(exchange_id)
                record_exchange_success(exchange_id)
                for symbol, quote_volume, last_price in candidates:
                    key = f"{exchange_id}:{symbol}"
                    try:
                        # Analyze each candidate; this may register new pump watches.
                        analyse_pump_and_reversal(exchange, symbol)
                    except Exception as exc:
                        logging.debug("Candidate error %s: %s", key, exc)
                save_state()
            except Exception as exc:
                logging.error("Exchange scan failed %s: %s", exchange_id, exc)
                record_exchange_failure(exchange_id, exc)

        # Watch every pumped market, even if it falls out of top-volume candidates.
        scan_watchlist()
        manage_open_positions()
        print_status()

        if (
            heartbeat_interval
            and time.monotonic() - last_heartbeat >= heartbeat_interval
        ):
            send_heartbeat(successful_exchanges)
            last_heartbeat = time.monotonic()

        elapsed = time.time() - cycle_start
        time.sleep(max(1, int(CFG["scan_interval_seconds"]) - int(elapsed)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        save_state()
        notify("⏹️ Crypto bot stopped — paper state saved.")
        logging.info("Stopped by user; paper state saved.")
