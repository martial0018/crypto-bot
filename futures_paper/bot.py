"""Crypto Pump & Dump Hunter — multi-timeframe PAPER trading only.

Uses public CCXT market data. It contains no order placement, wallet, signing,
withdrawal, or exchange API-key code. It simulates short trades after a pump
and a confirmed bearish reversal.
"""
from __future__ import annotations

import json
import logging
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ccxt

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "paper_futures_state.json"
TRADES_PATH = ROOT / "paper_futures_trades.jsonl"
LOG_PATH = ROOT / "bot.log"
CONTROL_PATH = ROOT / "futures_control.json"
STOP = False
BOT_LOCK_HANDLE = None


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    if cfg.get("mode") != "paper" or cfg.get("paper_trading_only") is not True:
        raise RuntimeError("Safety guard: config must explicitly set mode='paper' and paper_trading_only=true.")
    return cfg


CFG = load_config()
logging.basicConfig(
    level=getattr(logging, str(CFG.get("log_level", "INFO")).upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger("dump_hunter")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def entries_enabled() -> bool:
    try:
        control = json.loads(CONTROL_PATH.read_text(encoding="utf-8"))
        heartbeat = float(control.get("heartbeat_epoch", 0))
    except FileNotFoundError:
        return False
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        log.error("Cannot read futures control state (%s); pausing new entries.", exc)
        return False
    heartbeat_age = time.time() - heartbeat
    return (
        control.get("accept_entries") is True
        and -5 <= heartbeat_age <= 20
    )


def acquire_bot_lock():
    lock_file = (ROOT / ".futures_paper_bot.lock").open("a+b")
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"0")
        lock_file.flush()
    lock_file.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock_file.close()
        raise RuntimeError("The futures paper simulator is already running.") from exc
    return lock_file


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"cash_usdt": float(CFG["paper_starting_balance_usdt"]), "positions": {}, "last_scan": None}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("positions"), dict):
            raise ValueError("bad state structure")
        data.setdefault("cash_usdt", float(CFG["paper_starting_balance_usdt"]))
        data.setdefault("positions", {})
        return data
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log.error("Could not load paper_state.json (%s). Refusing to overwrite it automatically.", exc)
        raise


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def log_trade(record: dict[str, Any]) -> None:
    with TRADES_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def make_exchange(exchange_id: str):
    exchange_class = getattr(ccxt, exchange_id, None)
    if exchange_class is None:
        raise ValueError(f"Unknown CCXT exchange id: {exchange_id}")
    return exchange_class({
        "enableRateLimit": True,
        "timeout": int(CFG.get("request_timeout_ms", 20000)),
        "options": {"defaultType": "swap"},
    })


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def closed_candles(exchange, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
    rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    if not rows or len(rows) < 10:
        return []
    # Exclude the most recent candle because it may still be forming.
    return rows[:-1]


def bearish_1h_confirmation(candles: list[list[float]]) -> tuple[bool, dict[str, Any]]:
    closes = [safe_float(c[4]) for c in candles]
    if len(closes) < 25:
        return False, {"reason": "not_enough_1h_candles"}
    e = ema(closes, 20)
    last, prev = closes[-1], closes[-2]
    last3 = candles[-3:]
    bearish_count = sum(1 for c in last3 if safe_float(c[4]) < safe_float(c[1]))
    below_ema = last < e[-1]
    ema_falling = e[-1] < e[-4]
    lower_close = last < prev
    # Balanced confirmation: below EMA20 plus either a falling EMA or recent bearish momentum.
    confirmed = below_ema and (ema_falling or (lower_close and bearish_count >= 2))
    return confirmed, {"close": last, "ema20": e[-1], "ema_falling": ema_falling,
                       "below_ema20": below_ema, "bearish_candles_last3": bearish_count}


def trend_4h_context(candles: list[list[float]]) -> dict[str, Any]:
    closes = [safe_float(c[4]) for c in candles]
    if len(closes) < 25:
        return {"available": False, "strong_bullish": False, "bearish": False, "reason": "not_enough_4h_candles"}
    e = ema(closes, 20)
    close = closes[-1]
    rising = e[-1] > e[-4]
    falling = e[-1] < e[-4]
    strong_bullish = close > e[-1] and rising
    bearish = close < e[-1] and falling
    return {"available": True, "close": close, "ema20": e[-1], "ema20_rising": rising,
            "ema20_falling": falling, "strong_bullish": strong_bullish, "bearish": bearish}


def detect_5m_reversal(candles: list[list[float]], pump_24h_pct: float) -> dict[str, Any] | None:
    # The 24h pump is the eligibility filter. The 5m candles only decide whether
    # the move is reversing now; a 100%+ 24h pump need not have pumped in the
    # last 15 minutes to qualify. Use the recent 60-minute high (12 closed 5m candles) as the reversal anchor.
    if len(candles) < 10:
        return None
    closed = candles
    recent = closed[-min(12, len(closed)): ]
    pump_high = max(safe_float(c[2]) for c in recent)
    last, prev = closed[-1], closed[-2]
    close = safe_float(last[4])
    if pump_high <= 0 or close <= 0:
        return None
    drawdown_pct = (1 - close / pump_high) * 100
    # Support is the low of the preceding five closed candles, excluding the latest candle.
    prior = closed[-6:-1]
    if len(prior) < 5:
        return None
    support = min(safe_float(c[3]) for c in prior)
    support_break = close < support
    bearish_last = safe_float(last[4]) < safe_float(last[1])
    bearish_prev = safe_float(prev[4]) < safe_float(prev[1])
    lower_close = close < safe_float(prev[4])
    reversal_points = sum([support_break, bearish_last, lower_close, bearish_prev])
    if drawdown_pct < float(CFG["minimum_drawdown_from_pump_high_pct"]):
        return None
    if reversal_points < int(CFG["minimum_5m_reversal_points"]):
        return None
    if CFG.get("require_support_break", True) and not support_break:
        return None
    return {"pump_pct": pump_24h_pct, "pump_24h_pct": pump_24h_pct,
            "recent_high": pump_high, "drawdown_pct": drawdown_pct, "support": support,
            "support_break": support_break, "reversal_points": reversal_points,
            "price": close, "bearish_last": bearish_last, "lower_close": lower_close}


@dataclass
class Candidate:
    exchange_id: str
    symbol: str
    price: float
    quote_volume: float
    signal_5m: dict[str, Any]
    confirm_1h: dict[str, Any]
    context_4h: dict[str, Any]


def market_candidates(exchange, exchange_id: str) -> list[tuple[str, float, float, float | None]]:
    tickers = exchange.fetch_tickers()
    quote = str(CFG.get("quote_currency", "USDT")).upper()
    minimum_volume = float(CFG.get("minimum_quote_volume_usdt", 100000))
    excluded = set(CFG.get("excluded_symbols", []))
    results: list[tuple[str, float, float, float | None]] = []
    markets = getattr(exchange, "markets", {}) or {}
    for symbol, ticker in tickers.items():
        market = markets.get(symbol, {})
        # Futures only: prefer linear USDT-settled perpetual swaps. CCXT symbols
        # commonly look like BTC/USDT:USDT rather than spot's BTC/USDT.
        if not market or market.get("active") is False:
            continue
        if not market.get("swap") or not market.get("contract"):
            continue
        if market.get("linear") is False or market.get("quote") != quote:
            continue
        if market.get("settle") and market.get("settle") != quote:
            continue
        if market.get("base") == quote or symbol in excluded:
            continue
        volume = safe_float(ticker.get("quoteVolume"))
        last = safe_float(ticker.get("last"))
        pct_raw = ticker.get("percentage")
        pct = safe_float(pct_raw, float("nan")) if pct_raw is not None else float("nan")
        # Keep symbols whose reported 24h change meets the threshold. If an exchange
        # does not report percentage, keep it as a fallback and calculate from 1h candles.
        threshold = float(CFG.get("pump_24h_threshold_pct", 15.0))
        if volume >= minimum_volume and last > 0 and (not math.isfinite(pct) or pct >= threshold):
            results.append((symbol, volume, last, pct if math.isfinite(pct) else None))
    # Prioritize the biggest 24h pumps; when change data is missing, rank by quote volume.
    results.sort(key=lambda row: (row[3] is not None, row[3] if row[3] is not None else row[1]), reverse=True)
    return results[:int(CFG["max_symbols_per_exchange"])]


def orderbook_quote_volumes(exchange, symbol: str, depth: int) -> tuple[float, float] | None:
    try:
        book = exchange.fetch_order_book(symbol, limit=depth)
        market = (getattr(exchange, "markets", {}) or {}).get(symbol, {})
        contract_size = safe_float(market.get("contractSize"), 1.0) if market.get("contract") else 1.0

        def quote_volume(levels: Any) -> float:
            total = 0.0
            for level in levels:
                if not isinstance(level, (list, tuple)) or len(level) < 2:
                    continue
                price, amount = safe_float(level[0]), safe_float(level[1])
                if price > 0 and amount > 0:
                    total += price * amount * contract_size
            return total

        return quote_volume(book.get("bids", [])), quote_volume(book.get("asks", []))
    except (ccxt.NetworkError, ccxt.ExchangeError, KeyError, TypeError, ValueError) as exc:
        log.warning("Order-book read failed for %s %s: %s", exchange.id, symbol, exc)
        return None


def evaluate_symbol(exchange, exchange_id: str, symbol: str, volume: float, ticker_change_pct: float | None = None) -> Candidate | None:
    try:
        c1h = closed_candles(exchange, symbol, "1h", int(CFG["ohlcv_limit_1h"]))
        if len(c1h) < 25:
            return None
        # Use the exchange's reported rolling 24h percentage when available. Otherwise
        # estimate from closed 1h candles (approximately 24 hours apart).
        if ticker_change_pct is not None:
            pump_24h_pct = ticker_change_pct
        else:
            base_24h = safe_float(c1h[-25][4])
            latest_1h = safe_float(c1h[-1][4])
            if base_24h <= 0 or latest_1h <= 0:
                return None
            pump_24h_pct = (latest_1h / base_24h - 1.0) * 100.0
        if pump_24h_pct < float(CFG.get("pump_24h_threshold_pct", 15.0)):
            return None
        c1 = closed_candles(exchange, symbol, "5m", int(CFG["ohlcv_limit_5m"]))
        signal1 = detect_5m_reversal(c1, pump_24h_pct)
        if not signal1:
            return None
        confirmed, confirm_data = bearish_1h_confirmation(c1h)
        if not confirmed:
            return None
        c4h = closed_candles(exchange, symbol, "4h", int(CFG["ohlcv_limit_4h"]))
        context = trend_4h_context(c4h)
        if CFG.get("skip_if_4h_strongly_bullish", True) and context.get("strong_bullish", False):
            log.info("SKIP %s %s: 4h context is strongly bullish.", exchange_id, symbol)
            return None
        return Candidate(exchange_id, symbol, float(signal1["price"]), volume, signal1, confirm_data, context)
    except (ccxt.NetworkError, ccxt.ExchangeError, KeyError, IndexError, TypeError, ValueError) as exc:
        log.debug("Market-data issue %s %s: %s", exchange_id, symbol, exc)
        return None


def open_paper_short(candidate: Candidate, state: dict[str, Any]) -> bool:
    if not entries_enabled():
        return False
    key = f"{candidate.exchange_id}:{candidate.symbol}"
    positions = state["positions"]
    if key in positions or len(positions) >= int(CFG["max_open_positions"]):
        return False
    cash = safe_float(state.get("cash_usdt"))
    leverage = float(CFG.get("leverage", 5.0))
    if leverage < 5 or leverage > 10:
        log.error("Configured leverage %.2fx is outside the supported 5x-10x paper range.", leverage)
        return False
    margin = float(CFG.get("paper_position_margin_usdt", 25.0))
    notional = margin * leverage
    fee_rate = float(CFG["fee_pct"]) / 100.0
    entry_fee = notional * fee_rate
    if margin <= 0 or cash < margin + entry_fee:
        log.warning("Paper cash too low for futures margin plus entry fee.")
        return False
    # Simulated short entry includes adverse slippage. No real order is sent.
    entry = candidate.price * (1 - float(CFG["slippage_pct"]) / 100)
    state["cash_usdt"] = cash - margin - entry_fee
    positions[key] = {
        "key": key, "exchange_id": candidate.exchange_id, "symbol": candidate.symbol,
        "entry_price": entry, "last_price": candidate.price, "lowest_price": candidate.price,
        "notional_usdt": notional, "margin_usdt": margin, "leverage": leverage,
        "entry_fee_usdt": entry_fee, "opened_at": time.time(),
        "pump_pct": candidate.signal_5m["pump_pct"], "drawdown_at_entry_pct": candidate.signal_5m["drawdown_pct"],
        "best_profit_pct": 0.0, "last_1h_confirm": candidate.confirm_1h,
        "trend_4h": candidate.context_4h,
    }
    save_state(state)
    log.info("PAPER FUTURES SHORT OPEN %s %s entry=%.8g margin=$%.2f notional=$%.2f leverage=%.1fx pump_24h=%.2f%%",
        candidate.exchange_id, candidate.symbol, entry, margin, notional, leverage, candidate.signal_5m["pump_24h_pct"])
    return True

def close_paper_short(key: str, price: float, reason: str, state: dict[str, Any]) -> None:
    pos = state["positions"].get(key)
    if not pos or price <= 0:
        return
    # Buy-to-cover with adverse slippage; linear USDT-margined futures PnL.
    exit_price = price * (1 + float(CFG["slippage_pct"]) / 100)
    entry = safe_float(pos.get("entry_price"))
    notional = safe_float(pos.get("notional_usdt"))
    margin = safe_float(pos.get("margin_usdt"))
    leverage = safe_float(pos.get("leverage"), float(CFG.get("leverage", 5.0)))
    if entry <= 0 or notional <= 0 or margin <= 0:
        log.error("Invalid futures paper position %s; leaving it open for inspection.", key)
        return
    gross_pnl = notional * ((entry - exit_price) / entry)
    fee_rate = float(CFG["fee_pct"]) / 100.0
    exit_fee = notional * (exit_price / entry) * fee_rate
    age_hours = max(0.0, (time.time() - safe_float(pos.get("opened_at"))) / 3600.0)
    funding_intervals = int(age_hours // float(CFG.get("funding_interval_hours", 8)))
    funding_rate = float(CFG.get("assumed_funding_cost_pct_per_interval", 0.01)) / 100.0
    funding_cost = notional * funding_rate * funding_intervals
    entry_fee = safe_float(pos.get("entry_fee_usdt"))
    net_pnl = gross_pnl - entry_fee - exit_fee - funding_cost
    # Return isolated margin and realized PnL; entry fee was deducted at open.
    state["cash_usdt"] = safe_float(state.get("cash_usdt")) + margin + gross_pnl - exit_fee - funding_cost
    record = {
        "timestamp_utc": now_iso(), "mode": "paper_futures", "side": "short", "key": key,
        "exchange_id": pos["exchange_id"], "symbol": pos["symbol"], "reason": reason,
        "entry_price": entry, "exit_price": exit_price, "margin_usdt": margin,
        "notional_usdt": notional, "leverage": leverage,
        "gross_pnl_usdt": round(gross_pnl, 6), "entry_fee_usdt": round(entry_fee, 6),
        "exit_fee_usdt": round(exit_fee, 6), "estimated_funding_cost_usdt": round(funding_cost, 6),
        "fees_usdt": round(entry_fee + exit_fee + funding_cost, 6),
        "net_pnl_usdt": round(net_pnl, 6),
        "return_pct_on_margin": round(net_pnl / margin * 100, 4),
        "paper": True,
    }
    log_trade(record)
    del state["positions"][key]
    save_state(state)
    log.info("PAPER FUTURES SHORT CLOSE %s reason=%s net_pnl=$%.4f margin_return=%.2f%% cash=$%.2f",
             key, reason, net_pnl, net_pnl / margin * 100, state["cash_usdt"])

def manage_positions(exchanges: dict[str, Any], state: dict[str, Any]) -> None:
    for key, pos in list(state["positions"].items()):
        exchange = exchanges.get(pos.get("exchange_id"))
        if exchange is None:
            continue
        try:
            ticker = exchange.fetch_ticker(pos["symbol"])
            price = safe_float(ticker.get("last"))
            if price <= 0:
                continue
        except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
            log.warning("Could not refresh %s price; keeping paper position open: %s", key, exc)
            continue
        entry = safe_float(pos.get("entry_price"))
        if entry <= 0:
            continue
        pos["last_price"] = price
        pos["lowest_price"] = min(safe_float(pos.get("lowest_price"), price), price)
        # Underlying price move in favor of a short, before leverage and costs.
        move_pct = (entry - price) / entry * 100
        best_profit_pct = (entry - safe_float(pos["lowest_price"])) / entry * 100 if safe_float(pos["lowest_price"]) > 0 else 0
        pos["best_profit_pct"] = max(safe_float(pos.get("best_profit_pct")), best_profit_pct)
        age_minutes = (time.time() - safe_float(pos.get("opened_at"))) / 60
        reason = None
        # Conservative approximate liquidation guard. Real liquidation prices vary
        # by exchange, maintenance tier, funding, fees, and margin mode.
        leverage = max(1.0, safe_float(pos.get("leverage"), float(CFG.get("leverage", 5.0))))
        approx_liq_adverse_pct = max(0.5, 100.0 / leverage - float(CFG.get("maintenance_margin_pct", 0.5)) - float(CFG.get("liquidation_buffer_pct", 1.0)))
        if move_pct <= -approx_liq_adverse_pct:
            reason = "approx_liquidation_guard"
        elif move_pct <= -float(CFG["stop_loss_pct"]):
            reason = "stop_loss"
        elif move_pct >= float(CFG["take_profit_pct"]):
            reason = "take_profit"
        elif (pos["best_profit_pct"] >= float(CFG["trailing_arm_profit_pct"])
              and move_pct <= pos["best_profit_pct"] - float(CFG["trailing_stop_pct"])):
            reason = "trailing_stop"
        elif age_minutes >= float(CFG["max_hold_minutes"]):
            reason = "max_hold"
        if reason:
            close_paper_short(key, price, reason, state)
        else:
            save_state(state)


def scan_once(exchanges: dict[str, Any], state: dict[str, Any]) -> None:
    total_seen = 0
    total_signals = 0
    for exchange_id, exchange in exchanges.items():
        try:
            exchange.load_markets()
            shortlist = market_candidates(exchange, exchange_id)
            total_seen += len(shortlist)
            log.info("%s: checking %d high-volume %s markets", exchange_id, len(shortlist), CFG["quote_currency"])
            orderbook_limit = max(0, int(CFG.get("max_orderbook_symbols_per_exchange", 10)))
            orderbook_depth = max(1, int(CFG.get("orderbook_depth", 20)))
            for index, (symbol, volume, _last, ticker_change_pct) in enumerate(shortlist):
                if index < orderbook_limit:
                    volumes = orderbook_quote_volumes(exchange, symbol, orderbook_depth)
                    if volumes is not None:
                        bid_volume, ask_volume = volumes
                        total_volume = bid_volume + ask_volume
                        imbalance_pct = ((bid_volume - ask_volume) / total_volume * 100) if total_volume else 0.0
                        log.info(
                            "ORDERBOOK %s %s depth=%d bid_%s=%.2f ask_%s=%.2f imbalance=%.1f%%",
                            exchange_id, symbol, orderbook_depth, CFG["quote_currency"], bid_volume,
                            CFG["quote_currency"], ask_volume, imbalance_pct,
                        )
                candidate = evaluate_symbol(exchange, exchange_id, symbol, volume, ticker_change_pct)
                if candidate:
                    total_signals += 1
                    log.info("SIGNAL %s %s pump_24h=%.2f%% drawdown_from_recent_high=%.2f%% reversal_points=%d 1h_below_ema=%s 4h_bearish=%s",
                             exchange_id, symbol, candidate.signal_5m["pump_24h_pct"], candidate.signal_5m["drawdown_pct"],
                             candidate.signal_5m["reversal_points"], candidate.confirm_1h.get("below_ema20"),
                             candidate.context_4h.get("bearish"))
                    open_paper_short(candidate, state)
        except (ccxt.NetworkError, ccxt.ExchangeError, ValueError) as exc:
            log.warning("Exchange scan failed for %s: %s", exchange_id, exc)
    state["last_scan"] = now_iso()
    save_state(state)
    log.info("Scan complete: markets_checked=%d signals=%d open_positions=%d paper_cash=$%.2f",
             total_seen, total_signals, len(state["positions"]), safe_float(state["cash_usdt"]))


def stop_handler(_signum, _frame):
    global STOP
    STOP = True


def main() -> None:
    global STOP
    global BOT_LOCK_HANDLE
    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)
    BOT_LOCK_HANDLE = acquire_bot_lock()
    # Explicit guard: this project intentionally never calls create_order or any private endpoint.
    log.warning("PAPER FUTURES ONLY — public market data only; no real orders or API keys.")
    log.info("Strategy: linear USDT perpetuals across five exchanges; leverage=%.1fx (paper only); 24h pump >= %.2f%% (no upper limit) + 5m reversal + 1h confirmation + 4h context.", float(CFG.get("leverage", 5.0)), float(CFG.get("pump_24h_threshold_pct", 15.0)))
    state = load_state()
    exchanges: dict[str, Any] = {}
    for exchange_id in CFG["exchanges"]:
        try:
            exchanges[exchange_id] = make_exchange(exchange_id)
        except (ValueError, AttributeError) as exc:
            log.error("Skipping exchange %s: %s", exchange_id, exc)
    if not exchanges:
        raise RuntimeError("No configured exchanges could be initialized.")
    try:
        while not STOP:
            started = time.time()
            scan_once(exchanges, state)
            manage_positions(exchanges, state)
            elapsed = time.time() - started
            STOP = STOP or False
            wait_for = max(1, int(CFG["scan_interval_seconds"] - elapsed))
            for _ in range(wait_for):
                if STOP:
                    break
                time.sleep(1)
    finally:
        save_state(state)
        log.info("Stopped. Paper state saved; open paper positions remain in state for next run.")


if __name__ == "__main__":
    main()
