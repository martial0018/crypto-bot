"""Historical paper simulation for the uploaded multi-timeframe futures strategy."""
import argparse
import bisect
import heapq
import json
import logging
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ccxt

ROOT = Path(__file__).resolve().parent
UPLOADED_ARCHIVE = Path(r"C:\Users\Admin\Downloads\crypto_dump_hunter_futures_paper_5m.zip")
ARCHIVE_CONFIG = "crypto_dump_hunter_futures_paper/config.json"
DEFAULT_OUTPUT = ROOT / "futures_backtest_results.json"
FIVE_MINUTES_MS = 5 * 60 * 1000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest the uploaded USDT-margined futures paper strategy."
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-symbols", type=int, default=25)
    parser.add_argument("--config", type=Path, default=UPLOADED_ARCHIVE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            config = json.loads(archive.read(ARCHIVE_CONFIG))
    else:
        with path.open(encoding="utf-8") as file:
            config = json.load(file)
    if config.get("mode") != "paper" or config.get("paper_trading_only") is not True:
        raise ValueError("Config must explicitly set mode='paper' and paper_trading_only=true")
    return config


def select_futures_markets(exchange: Any, config: dict, maximum: int) -> list[str]:
    exchange.load_markets()
    tickers = exchange.fetch_tickers()
    quote = str(config.get("quote_currency", "USDT")).upper()
    minimum_volume = float(config.get("minimum_quote_volume_usdt", 100000))
    excluded = set(config.get("excluded_symbols", []))
    markets = exchange.markets or {}
    candidates = []

    for symbol, ticker in tickers.items():
        market = markets.get(symbol)
        if (
            not market
            or market.get("active") is False
            or not market.get("swap")
            or not market.get("contract")
            or market.get("linear") is False
            or market.get("quote") != quote
            or (market.get("settle") and market.get("settle") != quote)
            or market.get("base") == quote
            or symbol in excluded
        ):
            continue
        try:
            price = float(ticker.get("last") or 0)
            quote_volume = float(ticker.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if quote_volume <= 0 and exchange.id == "okx" and price > 0:
            info = ticker.get("info") or {}
            try:
                base_volume = float(info.get("volCcy24h") or 0)
                quote_volume = base_volume * price
            except (TypeError, ValueError):
                quote_volume = 0
        if quote_volume >= minimum_volume and price > 0:
            candidates.append((quote_volume, symbol))

    candidates.sort(reverse=True)
    return [symbol for _, symbol in candidates[:maximum]]


def fetch_history(
    exchange: Any,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[list[float]]:
    if not exchange.has.get("fetchOHLCV"):
        raise RuntimeError(f"{exchange.id} does not support OHLCV history")

    warmup_ms = 110 * 60 * 60 * 1000
    requested_start = start_ms - warmup_ms
    cursor = requested_start
    candles_by_time = {}
    limit = 300 if exchange.id == "okx" else 1000

    for _ in range(1000):
        if cursor >= end_ms:
            break
        page = exchange.fetch_ohlcv(
            symbol, timeframe="5m", since=cursor, limit=limit
        )
        if not page:
            break
        newest_ms = max(int(candle[0]) for candle in page)
        if newest_ms < cursor:
            raise RuntimeError(f"{exchange.id} history did not advance for {symbol}")
        for candle in page:
            timestamp_ms = int(candle[0])
            if requested_start <= timestamp_ms < end_ms:
                candles_by_time[timestamp_ms] = candle
        next_cursor = newest_ms + FIVE_MINUTES_MS
        if next_cursor <= cursor:
            raise RuntimeError(f"{exchange.id} history pagination stalled for {symbol}")
        cursor = next_cursor
    else:
        raise RuntimeError(f"History page limit reached for {exchange.id} {symbol}")

    return [candles_by_time[timestamp] for timestamp in sorted(candles_by_time)]


def resample_candles(
    candles: list[list[float]], interval_ms: int
) -> list[list[float]]:
    groups: dict[int, list[list[float]]] = {}
    for candle in candles:
        bucket = (int(candle[0]) // interval_ms) * interval_ms
        groups.setdefault(bucket, []).append(candle)

    expected_count = interval_ms // FIVE_MINUTES_MS
    result = []
    for bucket, rows in sorted(groups.items()):
        rows.sort(key=lambda candle: int(candle[0]))
        if len(rows) != expected_count:
            continue
        if any(
            int(rows[index][0]) - int(rows[index - 1][0]) != FIVE_MINUTES_MS
            for index in range(1, len(rows))
        ):
            continue
        result.append(
            [
                bucket,
                float(rows[0][1]),
                max(float(row[2]) for row in rows),
                min(float(row[3]) for row in rows),
                float(rows[-1][4]),
                sum(float(row[5]) for row in rows),
            ]
        )
    return result


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def make_series(candles: list[list[float]]) -> dict[str, Any]:
    one_hour = resample_candles(candles, 60 * 60 * 1000)
    four_hour = resample_candles(candles, 4 * 60 * 60 * 1000)
    return {
        "5m": candles,
        "1h": one_hour,
        "1h_close_ms": [int(row[0]) + 60 * 60 * 1000 for row in one_hour],
        "1h_ema20": ema([float(row[4]) for row in one_hour], 20),
        "4h": four_hour,
        "4h_close_ms": [int(row[0]) + 4 * 60 * 60 * 1000 for row in four_hour],
        "4h_ema20": ema([float(row[4]) for row in four_hour], 20),
    }


def signal_at(series: dict[str, Any], index: int, config: dict) -> dict | None:
    one_hour_index = bisect.bisect_right(
        series["1h_close_ms"], int(series["5m"][index][0]) + FIVE_MINUTES_MS
    ) - 1
    if one_hour_index < 24:
        return None

    hourly = series["1h"]
    hourly_ema = series["1h_ema20"]
    current_hour = float(hourly[one_hour_index][4])
    previous_24h = float(hourly[one_hour_index - 24][4])
    if previous_24h <= 0:
        return None
    pump_pct = (current_hour / previous_24h - 1.0) * 100.0
    if pump_pct < float(config.get("pump_24h_threshold_pct", 15.0)):
        return None

    last_three_hours = hourly[one_hour_index - 2 : one_hour_index + 1]
    bearish_count = sum(
        1 for row in last_three_hours if float(row[4]) < float(row[1])
    )
    below_ema = current_hour < hourly_ema[one_hour_index]
    ema_falling = (
        one_hour_index >= 3
        and hourly_ema[one_hour_index] < hourly_ema[one_hour_index - 3]
    )
    lower_close = current_hour < float(hourly[one_hour_index - 1][4])
    confirmed_1h = below_ema and (ema_falling or (lower_close and bearish_count >= 2))
    if not confirmed_1h:
        return None

    four_hour_index = bisect.bisect_right(
        series["4h_close_ms"], int(series["5m"][index][0]) + FIVE_MINUTES_MS
    ) - 1
    if four_hour_index < 0:
        return None
    four_hour = series["4h"]
    four_hour_ema = series["4h_ema20"]
    strong_bullish_4h = (
        four_hour_index >= 3
        and float(four_hour[four_hour_index][4]) > four_hour_ema[four_hour_index]
        and four_hour_ema[four_hour_index] > four_hour_ema[four_hour_index - 3]
    )
    if config.get("skip_if_4h_strongly_bullish", True) and strong_bullish_4h:
        return None

    candles = series["5m"]
    if index < 9:
        return None
    recent = candles[max(0, index - 11) : index + 1]
    recent_high = max(float(row[2]) for row in recent)
    current = candles[index]
    previous = candles[index - 1]
    close = float(current[4])
    if recent_high <= 0 or close <= 0:
        return None
    drawdown_pct = (1 - close / recent_high) * 100
    prior = candles[index - 5 : index]
    support = min(float(row[3]) for row in prior)
    support_break = close < support
    bearish_last = close < float(current[1])
    bearish_previous = float(previous[4]) < float(previous[1])
    lower_close_5m = close < float(previous[4])
    reversal_points = sum(
        (support_break, bearish_last, lower_close_5m, bearish_previous)
    )

    if drawdown_pct < float(config.get("minimum_drawdown_from_pump_high_pct", 2.5)):
        return None
    if reversal_points < int(config.get("minimum_5m_reversal_points", 2)):
        return None
    if config.get("require_support_break", True) and not support_break:
        return None
    return {
        "price": close,
        "pump_24h_pct": pump_pct,
        "drawdown_pct": drawdown_pct,
        "reversal_points": reversal_points,
    }


def close_position(
    state: dict,
    key: str,
    market_price: float,
    reason: str,
    config: dict,
    timestamp_ms: int,
) -> None:
    position = state["positions"].pop(key)
    exit_price = market_price * (1 + float(config["slippage_pct"]) / 100)
    entry = float(position["entry_price"])
    notional = float(position["notional_usdt"])
    margin = float(position["margin_usdt"])
    gross_pnl = notional * ((entry - exit_price) / entry)
    fee_rate = float(config["fee_pct"]) / 100
    exit_fee = notional * (exit_price / entry) * fee_rate
    age_hours = max(0.0, (timestamp_ms - position["opened_ms"]) / 3_600_000)
    funding_intervals = int(age_hours // float(config.get("funding_interval_hours", 8)))
    funding_cost = (
        notional
        * float(config.get("assumed_funding_cost_pct_per_interval", 0.01))
        / 100
        * funding_intervals
    )
    entry_fee = float(position["entry_fee_usdt"])
    net_pnl = gross_pnl - entry_fee - exit_fee - funding_cost
    state["cash_usdt"] += margin + gross_pnl - exit_fee - funding_cost
    state["trades"].append(
        {
            "market": key,
            "reason": reason,
            "entry_price": entry,
            "exit_price": exit_price,
            "margin_usdt": margin,
            "notional_usdt": notional,
            "leverage": position["leverage"],
            "gross_pnl_usdt": gross_pnl,
            "entry_fee_usdt": entry_fee,
            "exit_fee_usdt": exit_fee,
            "funding_cost_usdt": funding_cost,
            "net_pnl_usdt": net_pnl,
            "opened_at_utc": datetime.fromtimestamp(
                position["opened_ms"] / 1000, timezone.utc
            ).isoformat(),
            "closed_at_utc": datetime.fromtimestamp(
                timestamp_ms / 1000, timezone.utc
            ).isoformat(),
        }
    )


def backtest(
    series_by_market: dict[tuple[str, str], dict[str, Any]],
    config: dict,
    start_ms: int,
    end_ms: int,
) -> dict:
    state = {
        "cash_usdt": float(config["paper_starting_balance_usdt"]),
        "positions": {},
        "trades": [],
    }
    last_prices = {}
    indices = {key: 0 for key in series_by_market}
    events = []
    for key, series in series_by_market.items():
        if series["5m"]:
            heapq.heappush(events, (int(series["5m"][0][0]) + FIVE_MINUTES_MS, key))

    while events:
        timestamp_ms, key = heapq.heappop(events)
        if timestamp_ms > end_ms:
            break
        exchange_id, symbol = key
        series = series_by_market[key]
        index = indices[key]
        candle = series["5m"][index]
        indices[key] += 1
        if indices[key] < len(series["5m"]):
            next_candle = series["5m"][indices[key]]
            heapq.heappush(
                events, (int(next_candle[0]) + FIVE_MINUTES_MS, key)
            )
        if timestamp_ms < start_ms:
            continue

        market_key = f"{exchange_id}:{symbol}"
        open_price, high, low, close = map(float, candle[1:5])
        last_prices[market_key] = close

        position = state["positions"].get(market_key)
        position_closed = False
        if position and position["opened_ms"] < timestamp_ms:
            entry = float(position["entry_price"])
            leverage = float(position["leverage"])
            stop_price = entry * (1 + float(config["stop_loss_pct"]) / 100)
            liquidation_adverse_pct = max(
                0.5,
                100 / leverage
                - float(config.get("maintenance_margin_pct", 0.5))
                - float(config.get("liquidation_buffer_pct", 1.0)),
            )
            liquidation_price = entry * (1 + liquidation_adverse_pct / 100)
            reason = None
            exit_market_price = close
            if high >= liquidation_price:
                reason = "approx_liquidation_guard"
                exit_market_price = max(open_price, liquidation_price)
            elif high >= stop_price:
                reason = "stop_loss"
                exit_market_price = max(open_price, stop_price)
            else:
                profit_at_low = (entry - low) / entry * 100
                position["best_profit_pct"] = max(
                    float(position["best_profit_pct"]), profit_at_low
                )
                trail_arm = float(config["trailing_arm_profit_pct"])
                trail_pct = float(config["trailing_stop_pct"])
                if (
                    position["best_profit_pct"] >= trail_arm
                    and high
                    >= entry
                    * (1 + (position["best_profit_pct"] - trail_pct) / 100)
                ):
                    reason = "trailing_stop"
                    exit_market_price = max(
                        open_price,
                        entry
                        * (1 + (position["best_profit_pct"] - trail_pct) / 100),
                    )
                elif low <= entry * (1 - float(config["take_profit_pct"]) / 100):
                    reason = "take_profit"
                    exit_market_price = max(
                        low, entry * (1 - float(config["take_profit_pct"]) / 100)
                    )
                elif (
                    timestamp_ms - position["opened_ms"]
                    >= float(config["max_hold_minutes"]) * 60_000
                ):
                    reason = "max_hold"
            if reason:
                close_position(
                    state, market_key, exit_market_price, reason, config, timestamp_ms
                )
                position_closed = True

        if not position_closed and market_key not in state["positions"]:
            signal = signal_at(series, index, config)
            if signal and len(state["positions"]) < int(config["max_open_positions"]):
                leverage = float(config["leverage"])
                if not float(config.get("leverage_min", 5)) <= leverage <= float(
                    config.get("leverage_max", 10)
                ):
                    raise ValueError("Configured leverage is outside the allowed paper range")
                margin = float(config["paper_position_margin_usdt"])
                notional = margin * leverage
                fee_rate = float(config["fee_pct"]) / 100
                entry_fee = notional * fee_rate
                if margin > 0 and state["cash_usdt"] >= margin + entry_fee:
                    entry_price = signal["price"] * (
                        1 - float(config["slippage_pct"]) / 100
                    )
                    state["cash_usdt"] -= margin + entry_fee
                    state["positions"][market_key] = {
                        "entry_price": entry_price,
                        "margin_usdt": margin,
                        "notional_usdt": notional,
                        "leverage": leverage,
                        "entry_fee_usdt": entry_fee,
                        "opened_ms": timestamp_ms,
                        "best_profit_pct": 0.0,
                    }

    open_positions = []
    unrealized_pnl = 0.0
    for market_key, position in state["positions"].items():
        price = last_prices.get(market_key)
        if price is None:
            continue
        exit_price = price * (1 + float(config["slippage_pct"]) / 100)
        notional = float(position["notional_usdt"])
        entry = float(position["entry_price"])
        exit_fee = notional * (exit_price / entry) * float(config["fee_pct"]) / 100
        gross_pnl = notional * ((entry - exit_price) / entry)
        age_hours = max(0.0, (end_ms - position["opened_ms"]) / 3_600_000)
        funding_intervals = int(age_hours // float(config.get("funding_interval_hours", 8)))
        funding_cost = (
            notional
            * float(config.get("assumed_funding_cost_pct_per_interval", 0.01))
            / 100
            * funding_intervals
        )
        net_pnl = (
            gross_pnl
            - float(position["entry_fee_usdt"])
            - exit_fee
            - funding_cost
        )
        unrealized_pnl += net_pnl
        open_positions.append(
            {
                "market": market_key,
                "entry_price": entry,
                "mark_price": price,
                "unrealized_net_pnl_usdt": net_pnl,
            }
        )

    trades = state["trades"]
    total_net = sum(trade["net_pnl_usdt"] for trade in trades)
    wins = sum(trade["net_pnl_usdt"] > 0 for trade in trades)
    gross_profit = sum(max(0, trade["net_pnl_usdt"]) for trade in trades)
    gross_loss = -sum(min(0, trade["net_pnl_usdt"]) for trade in trades)
    return {
        "starting_balance_usdt": float(config["paper_starting_balance_usdt"]),
        "free_cash_usdt": state["cash_usdt"],
        "realized_net_pnl_usdt": total_net,
        "open_positions_unrealized_net_pnl_usdt": unrealized_pnl,
        "ending_equity_usdt": (
            state["cash_usdt"]
            + sum(float(pos["margin_usdt"]) for pos in state["positions"].values())
            + unrealized_pnl
        ),
        "closed_trades": len(trades),
        "winning_trades": wins,
        "win_rate_pct": wins / len(trades) * 100 if trades else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "open_positions": open_positions,
        "trades": trades,
    }


def main() -> None:
    args = parse_args()
    if args.days <= 0 or args.max_symbols <= 0:
        raise SystemExit("--days and --max-symbols must be greater than zero")
    config = load_config(args.config)
    now_ms = int(time.time() * 1000)
    end_ms = now_ms - (now_ms % FIVE_MINUTES_MS)
    start_ms = end_ms - int(timedelta(days=args.days).total_seconds() * 1000)

    series_by_market = {}
    selected_markets = {}
    coverage = {}
    errors = []
    for exchange_id in config["exchanges"]:
        exchange_class = getattr(ccxt, exchange_id, None)
        if exchange_class is None:
            errors.append({"exchange": exchange_id, "error": "Unknown CCXT exchange"})
            continue
        exchange = exchange_class(
            {
                "enableRateLimit": True,
                "timeout": int(config.get("request_timeout_ms", 20000)),
                "options": {"defaultType": "swap"},
            }
        )
        try:
            logging.info("Selecting linear USDT perpetuals on %s", exchange_id)
            symbols = select_futures_markets(exchange, config, args.max_symbols)
            selected_markets[exchange_id] = symbols
            if not symbols:
                errors.append(
                    {
                        "exchange": exchange_id,
                        "error": "No eligible linear USDT perpetual markets found",
                    }
                )
            logging.info("%s: downloading up to %d markets", exchange_id, len(symbols))
            for index, symbol in enumerate(symbols, 1):
                logging.info("%s [%d/%d] %s", exchange_id, index, len(symbols), symbol)
                try:
                    candles = fetch_history(exchange, symbol, start_ms, end_ms)
                    in_range = [
                        row for row in candles if start_ms <= int(row[0]) < end_ms
                    ]
                    expected = args.days * 24 * 12
                    first_ms = int(in_range[0][0]) if in_range else None
                    last_ms = int(in_range[-1][0]) if in_range else None
                    complete = (
                        len(in_range) >= expected - 3
                        and first_ms is not None
                        and first_ms <= start_ms + 3 * FIVE_MINUTES_MS
                        and last_ms is not None
                        and last_ms >= end_ms - 4 * FIVE_MINUTES_MS
                    )
                    coverage[f"{exchange_id}:{symbol}"] = {
                        "candles_in_range": len(in_range),
                        "expected_candles": expected,
                        "first_candle_utc": (
                            datetime.fromtimestamp(first_ms / 1000, timezone.utc).isoformat()
                            if first_ms is not None
                            else None
                        ),
                        "last_candle_utc": (
                            datetime.fromtimestamp(last_ms / 1000, timezone.utc).isoformat()
                            if last_ms is not None
                            else None
                        ),
                        "complete_history": complete,
                    }
                    if not complete:
                        errors.append(
                            {
                                "exchange": exchange_id,
                                "symbol": symbol,
                                "error": "Incomplete 5-minute candle coverage for requested period",
                            }
                        )
                        continue
                    series_by_market[(exchange_id, symbol)] = make_series(candles)
                except Exception as exc:
                    errors.append(
                        {
                            "exchange": exchange_id,
                            "symbol": symbol,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    logging.error("History failed %s %s: %s", exchange_id, symbol, exc)
        except Exception as exc:
            errors.append(
                {"exchange": exchange_id, "error": f"{type(exc).__name__}: {exc}"}
            )
            logging.error("Exchange setup failed %s: %s", exchange_id, exc)
        finally:
            exchange.close()

    results = backtest(series_by_market, config, start_ms, end_ms)
    exchange_summary = {}
    for exchange_id in config["exchanges"]:
        exchange_trades = [
            trade
            for trade in results["trades"]
            if trade["market"].startswith(f"{exchange_id}:")
        ]
        exchange_wins = sum(
            trade["net_pnl_usdt"] > 0 for trade in exchange_trades
        )
        exchange_summary[exchange_id] = {
            "markets_selected": len(selected_markets.get(exchange_id, [])),
            "markets_with_complete_history": sum(
                coverage.get(f"{exchange_id}:{symbol}", {}).get(
                    "complete_history", False
                )
                for symbol in selected_markets.get(exchange_id, [])
            ),
            "closed_trades": len(exchange_trades),
            "winning_trades": exchange_wins,
            "win_rate_pct": (
                exchange_wins / len(exchange_trades) * 100
                if exchange_trades
                else 0.0
            ),
            "realized_net_pnl_usdt": sum(
                trade["net_pnl_usdt"] for trade in exchange_trades
            ),
        }
    report = {
        "status": "complete"
        if (
            len(selected_markets) == len(config["exchanges"])
            and all(selected_markets.values())
            and not errors
        )
        else "partial",
        "period_start_utc": datetime.fromtimestamp(
            start_ms / 1000, timezone.utc
        ).isoformat(),
        "period_end_utc": datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat(),
        "days_requested": args.days,
        "strategy": "5m reversal entry + 1h bearish confirmation + 4h trend filter",
        "markets_per_exchange_limit": args.max_symbols,
        "universe_note": (
            "Top-volume active linear USDT perpetual markets selected at run time; "
            "historical pump eligibility is calculated from each market's closed 1h candles. "
            "The current market universe can introduce survivorship bias."
        ),
        "strategy_config": {
            key: config.get(key)
            for key in (
                "pump_24h_threshold_pct",
                "minimum_drawdown_from_pump_high_pct",
                "minimum_5m_reversal_points",
                "require_support_break",
                "skip_if_4h_strongly_bullish",
                "paper_starting_balance_usdt",
                "paper_position_margin_usdt",
                "leverage",
                "fee_pct",
                "slippage_pct",
                "stop_loss_pct",
                "take_profit_pct",
                "trailing_arm_profit_pct",
                "trailing_stop_pct",
                "max_hold_minutes",
                "funding_interval_hours",
                "assumed_funding_cost_pct_per_interval",
                "maintenance_margin_pct",
                "liquidation_buffer_pct",
                "max_open_positions",
            )
        },
        "selected_markets": selected_markets,
        "exchange_summary": exchange_summary,
        "market_coverage": coverage,
        "errors": errors,
        **results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2)
    logging.info(
        "Backtest %s: markets=%d, trades=%d, realized net PnL=$%.2f, "
        "ending equity=$%.2f, report=%s",
        report["status"],
        len(series_by_market),
        results["closed_trades"],
        results["realized_net_pnl_usdt"],
        results["ending_equity_usdt"],
        args.output,
    )


if __name__ == "__main__":
    main()
