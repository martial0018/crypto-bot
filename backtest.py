"""Historical paper backtest for the scanner; never submits exchange orders."""
import argparse
import heapq
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ccxt

CONFIG_FILE = Path(__file__).with_name("config.json")
DEFAULT_OUTPUT = Path(__file__).with_name("backtest_results.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest the paper-trading strategy on historical spot candles."
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def eligible_markets(exchange: Any, tickers: dict, config: dict) -> list[str]:
    markets = exchange.load_markets()
    candidates = []
    quote = config["quote_currency"]
    minimum_volume = float(config["minimum_quote_volume"])

    for symbol, ticker in tickers.items():
        market = markets.get(symbol)
        if (
            not market
            or not market.get("spot", False)
            or market.get("active") is False
            or market.get("quote") != quote
        ):
            continue
        try:
            last = float(ticker.get("last") or 0)
            quote_volume = float(ticker.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if last <= 0 or quote_volume < minimum_volume:
            continue
        candidates.append((quote_volume, symbol))

    candidates.sort(reverse=True)
    return [
        symbol
        for _, symbol in candidates[: int(config["max_symbols_per_exchange"])]
    ]


def fetch_history(
    exchange: Any,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    interval_ms: int,
) -> list[list[float]]:
    if not exchange.has.get("fetchOHLCV"):
        raise RuntimeError(f"{exchange.id} does not support OHLCV history")

    warmup_bars = 32
    cursor = start_ms - warmup_bars * interval_ms
    candles_by_time: dict[int, list[float]] = {}
    page_limit = 300 if exchange.id == "okx" else 1000
    page_count = 0

    while cursor < end_ms and page_count < 1000:
        page = exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, since=cursor, limit=page_limit
        )
        page_count += 1
        if not page:
            break

        newest_ms = max(int(candle[0]) for candle in page)
        if newest_ms < cursor:
            raise RuntimeError(
                f"{exchange.id} returned no advancing history for {symbol}"
            )
        for candle in page:
            timestamp_ms = int(candle[0])
            if cursor <= timestamp_ms < end_ms:
                candles_by_time[timestamp_ms] = candle

        next_cursor = newest_ms + interval_ms
        if next_cursor <= cursor:
            raise RuntimeError(
                f"{exchange.id} history pagination stalled for {symbol}"
            )
        cursor = next_cursor

    if page_count >= 1000 and cursor < end_ms:
        raise RuntimeError(f"History page limit reached for {exchange.id} {symbol}")

    return [candles_by_time[timestamp] for timestamp in sorted(candles_by_time)]


def pct_change(old: float, new: float) -> float:
    return ((new / old) - 1.0) * 100.0 if old else 0.0


def reversal_signal(
    candles: list[list[float]], index: int, config: dict
) -> dict[str, Any] | None:
    window = int(config["pump_window_candles"])
    if index < window + 5:
        return None

    current = candles[index]
    reference_close = float(candles[index - window][4])
    pump_pct = pct_change(reference_close, float(current[4]))
    previous = candles[index - 5 : index]
    support = min(float(candle[3]) for candle in previous)

    bearish_recent = candles[index - 4 : index + 1]
    total_volume = sum(float(candle[5]) for candle in bearish_recent)
    bearish_volume = sum(
        float(candle[5])
        for candle in bearish_recent
        if float(candle[4]) < float(candle[1])
    )
    bearish_volume_ratio = bearish_volume / total_volume if total_volume else 0.0

    open_price, high, low, close = map(float, current[1:5])
    candle_range = high - low
    upper_wick = high - max(open_price, close)
    rejection = candle_range > 0 and upper_wick / candle_range >= 0.40
    support_break = close < support
    last_three_highs = max(float(candle[2]) for candle in candles[index - 2 : index + 1])
    preceding_highs = max(
        float(candle[2]) for candle in candles[index - 7 : index - 2]
    )
    failed_recovery = last_three_highs < preceding_highs
    falling_close = close < float(candles[index - 1][4])

    signals = {
        "bearish_volume": bearish_volume_ratio >= 0.60,
        "upper_wick_rejection": rejection,
        "support_break": support_break,
        "failed_recovery": failed_recovery and falling_close,
    }
    return {
        "price": close,
        "pump_pct": pump_pct,
        "support_break": support_break,
        "score": sum(bool(value) for value in signals.values()),
        "signals": signals,
    }


def backtest(
    series: dict[tuple[str, str], list[list[float]]],
    config: dict,
    start_ms: int,
    interval_ms: int,
) -> dict:
    state = {
        "balance": float(config["paper_starting_balance_usdt"]),
        "positions": {},
        "watchlist": {},
        "trades": [],
        "daily": {"date": None, "realized_pnl": 0.0},
    }
    last_prices: dict[str, float] = {}
    keys = sorted(series)
    indices = {key: 0 for key in keys}
    events = []
    for key in keys:
        candles = series[key]
        if candles:
            heapq.heappush(events, (int(candles[0][0]), key[0], key[1]))

    while events:
        timestamp_ms, exchange_id, symbol = heapq.heappop(events)
        key = (exchange_id, symbol)
        index = indices[key]
        candles = series[key]
        candle = candles[index]
        indices[key] += 1
        if indices[key] < len(candles):
            next_candle = candles[indices[key]]
            heapq.heappush(
                events, (int(next_candle[0]), exchange_id, symbol)
            )

        if timestamp_ms < start_ms:
            continue

        timestamp = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
        day = timestamp.date().isoformat()
        if state["daily"]["date"] != day:
            state["daily"] = {"date": day, "realized_pnl": 0.0}

        market_key = f"{exchange_id}:{symbol}"
        open_price, high, low, close = map(float, candle[1:5])
        last_prices[market_key] = close

        signal = reversal_signal(candles, index, config)
        watch = state["watchlist"].get(market_key)
        if signal and signal["pump_pct"] >= float(config["pump_threshold_pct"]):
            if not watch:
                watch = {"detected_ms": timestamp_ms}
                state["watchlist"][market_key] = watch

        watch = state["watchlist"].get(market_key)
        if watch:
            age_minutes = (timestamp_ms - watch["detected_ms"]) / 60000
            if age_minutes > float(config["watchlist_minutes"]):
                del state["watchlist"][market_key]
                watch = None

        if watch and signal:
            enough_signals = signal["score"] >= int(
                config["minimum_bearish_signals"]
            )
            support_ok = (
                not bool(config.get("require_support_break", True))
                or signal["support_break"]
            )
            if (
                enough_signals
                and support_ok
                and market_key not in state["positions"]
                and len(state["positions"]) < int(config["max_open_positions"])
                and state["daily"]["realized_pnl"]
                > -abs(float(config["max_daily_loss_usdt"]))
            ):
                collateral = float(config["paper_position_usdt"])
                fee_rate = float(config["paper_fee_rate"])
                if collateral > 0 and collateral * (1 + fee_rate) <= state["balance"]:
                    entry = signal["price"] * (
                        1 - float(config["paper_slippage_rate"])
                    )
                    state["balance"] -= collateral * (1 + fee_rate)
                    state["positions"][market_key] = {
                        "exchange": exchange_id,
                        "symbol": symbol,
                        "entry": entry,
                        "quantity": collateral / entry,
                        "collateral": collateral,
                        "entry_fee": collateral * fee_rate,
                        "lowest_price": entry,
                        "stop_price": entry
                        * (1 + float(config["stop_loss_pct"]) / 100),
                        "trailing_stop_pct": float(config["trailing_stop_pct"]),
                        "opened_ms": timestamp_ms,
                    }

        position = state["positions"].get(market_key)
        if position and position["opened_ms"] < timestamp_ms:
            stop_price = float(position["stop_price"])
            if high >= stop_price:
                # If a bar opens through the stop, model the worse opening fill.
                exit_market_price = max(open_price, stop_price)
                close_position(
                    state,
                    market_key,
                    exit_market_price,
                    "stop_loss_or_trailing_stop",
                    config,
                    timestamp_ms,
                )
            else:
                position["lowest_price"] = min(
                    float(position["lowest_price"]), low
                )
                trailing_stop = float(position["lowest_price"]) * (
                    1 + float(position["trailing_stop_pct"]) / 100
                )
                position["stop_price"] = min(stop_price, trailing_stop)

    open_positions = []
    unrealized_pnl = 0.0
    for market_key, position in state["positions"].items():
        price = last_prices.get(market_key)
        if price is None:
            continue
        exit_price = price * (1 + float(config["paper_slippage_rate"]))
        net_pnl = (
            (float(position["entry"]) - exit_price) * float(position["quantity"])
            - float(position["entry_fee"])
            - exit_price
            * float(position["quantity"])
            * float(config["paper_fee_rate"])
        )
        unrealized_pnl += net_pnl
        open_positions.append(
            {
                "market": market_key,
                "entry": position["entry"],
                "mark_price": price,
                "unrealized_net_pnl": net_pnl,
            }
        )

    trades = state["trades"]
    gross_profit = sum(max(0.0, trade["net_pnl"]) for trade in trades)
    gross_loss = -sum(min(0.0, trade["net_pnl"]) for trade in trades)
    return {
        "starting_balance_usdt": float(config["paper_starting_balance_usdt"]),
        "free_balance_usdt": state["balance"],
        "realized_net_pnl_usdt": sum(trade["net_pnl"] for trade in trades),
        "open_positions_unrealized_net_pnl_usdt": unrealized_pnl,
        "ending_equity_usdt": state["balance"]
        + sum(float(position["collateral"]) for position in state["positions"].values())
        + unrealized_pnl,
        "closed_trades": len(trades),
        "winning_trades": sum(1 for trade in trades if trade["net_pnl"] > 0),
        "win_rate_pct": (
            sum(1 for trade in trades if trade["net_pnl"] > 0) / len(trades) * 100
            if trades
            else 0.0
        ),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "open_positions": open_positions,
        "trades": trades,
    }


def close_position(
    state: dict,
    market_key: str,
    market_price: float,
    reason: str,
    config: dict,
    timestamp_ms: int,
) -> None:
    position = state["positions"].pop(market_key)
    exit_price = market_price * (1 + float(config["paper_slippage_rate"]))
    quantity = float(position["quantity"])
    exit_value = quantity * exit_price
    exit_fee = exit_value * float(config["paper_fee_rate"])
    gross_pnl = (float(position["entry"]) - exit_price) * quantity
    net_pnl = gross_pnl - float(position["entry_fee"]) - exit_fee
    state["balance"] += float(position["collateral"]) + gross_pnl - exit_fee
    state["daily"]["realized_pnl"] += net_pnl
    state["trades"].append(
        {
            "market": market_key,
            "entry": position["entry"],
            "exit": exit_price,
            "gross_pnl": gross_pnl,
            "entry_fee": position["entry_fee"],
            "exit_fee": exit_fee,
            "net_pnl": net_pnl,
            "reason": reason,
            "opened_at": datetime.fromtimestamp(
                position["opened_ms"] / 1000, timezone.utc
            ).isoformat(),
            "closed_at": datetime.fromtimestamp(
                timestamp_ms / 1000, timezone.utc
            ).isoformat(),
        }
    )


def main() -> None:
    args = parse_args()
    if args.days <= 0:
        raise SystemExit("--days must be greater than zero")

    with CONFIG_FILE.open(encoding="utf-8") as file:
        config = json.load(file)

    now_ms = int(time.time() * 1000)
    timeframe_ms = int(ccxt.Exchange.parse_timeframe(args.timeframe) * 1000)
    end_ms = now_ms - (now_ms % timeframe_ms)
    start_ms = end_ms - int(timedelta(days=args.days).total_seconds() * 1000)
    start_time = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    end_time = datetime.fromtimestamp(end_ms / 1000, timezone.utc)

    series: dict[tuple[str, str], list[list[float]]] = {}
    errors = []
    selected_markets = {}
    coverage = {}

    for exchange_id in config["exchanges"]:
        exchange = getattr(ccxt, exchange_id)(
            {
                "enableRateLimit": True,
                "timeout": 30000,
                "options": {"defaultType": "spot"},
            }
        )
        try:
            logging.info("Selecting markets on %s", exchange_id)
            markets = eligible_markets(exchange, exchange.fetch_tickers(), config)
            selected_markets[exchange_id] = markets
            logging.info(
                "%s: downloading %d symbols of %d-minute candles",
                exchange_id,
                len(markets),
                int(timeframe_ms / 60000),
            )
            for index, symbol in enumerate(markets, start=1):
                logging.info("%s [%d/%d] %s", exchange_id, index, len(markets), symbol)
                try:
                    candles = fetch_history(
                        exchange,
                        symbol,
                        args.timeframe,
                        start_ms,
                        end_ms,
                        timeframe_ms,
                    )
                    in_range = [
                        candle
                        for candle in candles
                        if start_ms <= int(candle[0]) < end_ms
                    ]
                    market_errors = []
                    coverage[f"{exchange_id}:{symbol}"] = {
                        "candles_in_range": len(in_range),
                        "first_candle_utc": (
                            datetime.fromtimestamp(
                                int(in_range[0][0]) / 1000, timezone.utc
                            ).isoformat()
                            if in_range
                            else None
                        ),
                        "last_candle_utc": (
                            datetime.fromtimestamp(
                                int(in_range[-1][0]) / 1000, timezone.utc
                            ).isoformat()
                            if in_range
                            else None
                        ),
                    }
                    if not in_range:
                        market_errors.append(
                            "No candles available in the requested period"
                        )
                    else:
                        first_ms = int(in_range[0][0])
                        last_ms = int(in_range[-1][0])
                        coverage_tolerance_ms = 3 * timeframe_ms
                        if first_ms > start_ms + coverage_tolerance_ms:
                            market_errors.append(
                                "Historical candles do not reach the start of the requested period"
                            )
                        if last_ms < end_ms - coverage_tolerance_ms:
                            market_errors.append(
                                "Historical candles do not reach the end of the requested period"
                            )
                    coverage[f"{exchange_id}:{symbol}"]["complete_history"] = (
                        not market_errors
                    )
                    for error in market_errors:
                        errors.append(
                            {
                                "exchange": exchange_id,
                                "symbol": symbol,
                                "error": error,
                            }
                        )
                    if not market_errors:
                        series[(exchange_id, symbol)] = candles
                except Exception as exc:
                    errors.append(
                        {
                            "exchange": exchange_id,
                            "symbol": symbol,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    logging.error(
                        "History failed %s %s: %s: %s",
                        exchange_id,
                        symbol,
                        type(exc).__name__,
                        exc,
                    )
        except Exception as exc:
            errors.append(
                {
                    "exchange": exchange_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            logging.error(
                "Exchange setup failed %s: %s: %s",
                exchange_id,
                type(exc).__name__,
                exc,
            )
        finally:
            exchange.close()

    results = backtest(series, config, start_ms, timeframe_ms)
    exchanges_with_markets = sum(bool(markets) for markets in selected_markets.values())
    report = {
        "status": "complete"
        if exchanges_with_markets == len(config["exchanges"]) and not errors
        else "partial",
        "period_start_utc": start_time.isoformat(),
        "period_end_utc": end_time.isoformat(),
        "days_requested": args.days,
        "timeframe": args.timeframe,
        "strategy_config": {
            "pump_window_candles": int(config["pump_window_candles"]),
            "pump_threshold_pct": float(config["pump_threshold_pct"]),
            "watchlist_minutes": float(config["watchlist_minutes"]),
            "minimum_bearish_signals": int(config["minimum_bearish_signals"]),
            "require_support_break": bool(
                config.get("require_support_break", True)
            ),
            "paper_position_usdt": float(config["paper_position_usdt"]),
            "paper_fee_rate": float(config["paper_fee_rate"]),
            "paper_slippage_rate": float(config["paper_slippage_rate"]),
            "stop_loss_pct": float(config["stop_loss_pct"]),
            "trailing_stop_pct": float(config["trailing_stop_pct"]),
            "max_open_positions": int(config["max_open_positions"]),
            "max_daily_loss_usdt": float(config["max_daily_loss_usdt"]),
        },
        "universe_note": (
            "For each exchange, tested the current top-volume eligible spot markets. "
            "This is not a historical volume-ranked universe and can introduce "
            "survivorship bias."
        ),
        "selected_markets": selected_markets,
        "markets_with_complete_history": {
            exchange_id: [
                symbol
                for market_exchange, symbol in series
                if market_exchange == exchange_id
            ]
            for exchange_id in config["exchanges"]
        },
        "market_coverage": coverage,
        "errors": errors,
        **results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2)

    logging.info(
        "Backtest %s: exchanges=%d/%d, closed_trades=%d, realized_net_pnl=%.4f USDT, "
        "open_positions=%d, ending_equity=%.2f USDT",
        report["status"],
        exchanges_with_markets,
        len(config["exchanges"]),
        results["closed_trades"],
        results["realized_net_pnl_usdt"],
        len(results["open_positions"]),
        results["ending_equity_usdt"],
    )
    logging.info("Results saved to %s", args.output)


if __name__ == "__main__":
    main()
